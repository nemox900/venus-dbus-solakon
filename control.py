# Solakon-Mod: Steuerung (Rolle der Multi-RS-Firmware).
#
# Venus (dbus-acsystem, systemcalc: ESS-Menue, Zeitplaene, Dynamic ESS, DVCC)
# schreibt die Multi-RS-Pfade am multi.solakon. Dieses Modul setzt sie in
# Fernsteuerbefehle am Solakon um:
#   App-Freigabe : 46001 = 0, 46504 = 0, 46002 = 0 (einmal beim Wechsel)
#   Abgabe X W   : 46504 = X, 46002 = Timeout, 46003 = X, 46001 = 1
#                  (X = 0: nichts abgeben, ganze PV in den Akku)
#   Netzladen X W: 46504 = 0, 46002 = Timeout, 46003 = X, 46001 = 7
# Befehle werden alle RENEW_S erneuert; Timeout im Geraet TIMEOUT_S. Stirbt
# der Treiber, kehrt das Solakon spaetestens dann in den App-Betrieb zurueck.
# Nie geschrieben: 49203 Work mode, 45005/45007, 49077/49078 (Ein/Aus), 46616.

import logging
import time

log = logging.getLogger("dbus-solakon.control")

TIMEOUT_S = 90
RENEW_S = 30
MIN_CHANGE_W = 25       # kleinere Aenderungen nicht senden
LOG_CHANGE_W = 50       # ab dieser Aenderung ins Log
PV_LOSS_W = 50          # Eigenverbrauch/Wandlung PV -> AC (gemessen 22.10.: AC = PV - ~35 W)
MIN_INTERVAL_S = 5      # Aenderungen hoechstens so oft senden (Geraet reagiert in 30-60 s)
GRID_CHARGE_START_W = 50  # Ueberschuss ab dem aus dem Netz/Fremd-PV geladen wird
MAX_EXPORT_W = 800      # Datenblatt AC-Ausgang
MAX_CHARGE_W = 1200     # Datenblatt AC-Eingang 1200 VA
NOMINAL_AC_V = 230      # Umrechnung A -> W fuer Strombegrenzungen
SETPOINT_HOLD_S = 60    # so lange gilt ein von Venus (DESS/extern) gesetzter Netz-Sollwert
MODE_HOLD_S = 30        # Mindestzeit zwischen Wechseln Abgabe <-> Netzladen (46001 1 <-> 7)

# Venus /Settings/Ess/Mode
ESS_BATTERYLIFE, ESS_OPTIMIZED, ESS_KEEP_CHARGED, ESS_EXTERNAL = 0, 1, 2, 3
# Venus /Mode
MODE_CHARGER, MODE_INVERTER, MODE_ON, MODE_OFF = 1, 2, 3, 4

# BatteryLife wie bei Victron: +5 % je Tag ohne Volladung, -5 % bei Volladung
BL_STEP = 5
BL_MAX = 80


class Control(object):
    def __init__(self, mb, settings, state, enabled):
        self.mb = mb
        self.settings = settings   # SettingsDevice (localsettings)
        self.state = state         # persistente Datei (state.json)
        self.enabled = enabled
        self.req = {"/Ess/AcPowerSetpoint": 0.0, "/Ess/InverterPowerSetpoint": 0.0,
                    "/Ess/UseInverterPowerSetpoint": 0, "/Ess/DisableFeedIn": 0,
                    "/Ess/DisableCharge": 0, "/Ess/DisableDischarge": 0,
                    "/Ess/BatteryDischargeSetpoint": None}
        self.req_t = {}
        self.last = None          # (art, w) zuletzt bestaetigt
        self.last_t = 0.0
        self.art_t = 0.0          # letzter Wechsel Abgabe <-> Netzladen
        self.dess_warned = False
        self.reserve_active = False
        self.keep_active = False
        self.error = None
        self.text = "noch keine Messwerte"

    # ---------- Werte, die Venus schreibt ----------
    @property
    def ess_mode(self):
        return int(self.settings["essmode"])

    @property
    def mode(self):
        return int(self.settings["mode"])

    def set_request(self, path, value):
        if not self.enabled:
            log.info("Steuerung aus (config.ini): %s = %r abgewiesen", path, value)
            return False
        self.req[path] = value
        self.req_t[path] = time.monotonic()
        return True

    def grid_setpoint(self, m):
        """Netz-Sollwert: von Venus gerade gesetzt (Dynamic ESS, extern) oder ESS-Menue."""
        t = self.req_t.get("/Ess/AcPowerSetpoint")
        if t is not None and time.monotonic() - t < SETPOINT_HOLD_S:
            return float(self.req["/Ess/AcPowerSetpoint"] or 0)
        return float(m.get("grid_setpoint") or 0)

    def set_ess_mode(self, value):
        if not self.enabled or value not in (0, 1, 2, 3):
            return False
        self.settings["essmode"] = int(value)
        log.info("ESS-Modus -> %d", value)
        return True

    def set_mode(self, value):
        if not self.enabled or value not in (1, 2, 3, 4):
            return False
        self.settings["mode"] = int(value)
        log.info("Geraetemodus -> %d", value)
        return True

    # ---------- Modbus ----------
    def _write(self, addr, regs):
        try:
            if not self.mb.c.is_socket_open():
                self.mb.c.connect()
            r = self.mb.c.write_registers(addr, regs, unit=self.mb.unit)
            if r.isError():
                log.warning("Schreibfehler %d: %s", addr, r)
                return False
            time.sleep(0.3)
            return True
        except Exception as e:
            log.warning("Schreiben %d: %s", addr, e)
            self.mb.c.close()
            return False

    def write_checked(self, addr, value):
        """Einzelregister (U16) schreiben und zuruecklesen."""
        if not self._write(addr, [int(value)]):
            return False
        r = self.mb.read(addr, 1)
        return r is not None and r[0] == int(value)

    @staticmethod
    def i32(v):
        v = int(v) & 0xFFFFFFFF
        return [v >> 16, v & 0xFFFF]

    def _apply(self, art, w):
        if art == "app":
            return (self._write(46001, [0]) and self._write(46504, self.i32(0))
                    and self._write(46002, [0]))
        if art == "abgabe":
            seq = ((46504, self.i32(w)), (46002, [TIMEOUT_S]), (46003, self.i32(w)),
                   (46001, [1]))
        else:
            seq = ((46504, self.i32(0)), (46002, [TIMEOUT_S]), (46003, self.i32(w)),
                   (46001, [7]))
        return all(self._write(a, r) for a, r in seq)

    def apply(self, art, w):
        if not self._apply(art, w):
            return False
        expected = {"app": 0, "abgabe": 1, "netz": 7}[art]
        r = self.mb.read(46001, 1)
        if r is None or r[0] != expected:
            return False
        if w is None:
            return True
        r = self.mb.read(46003, 2)
        if r is not None and (r[0] << 16 | r[1]) == (int(w) & 0xFFFFFFFF):
            return True
        # Beim Aktivieren setzt das Geraet 46003 teils zurueck: einmal nachsenden
        if not self._write(46003, self.i32(w)):
            return False
        r = self.mb.read(46003, 2)
        r2 = self.mb.read(46001, 1)
        return (r is not None and (r[0] << 16 | r[1]) == (int(w) & 0xFFFFFFFF)
                and r2 is not None and r2[0] == expected)

    def release(self):
        """App-Freigabe, z. B. beim Beenden des Treibers."""
        if self.enabled and self.last not in (None, ("app", None)):
            log.info("Fernsteuerung aus (App-Freigabe)")
            self.apply("app", None)
            self.last = ("app", None)

    # ---------- BatteryLife ----------
    def batterylife_limit(self, soc, minsoc):
        bl = self.state.d.setdefault("batterylife", {"limit": minsoc, "since": time.time()})
        if soc >= 100:
            if bl.get("full") is not True:
                bl["limit"] = max(minsoc, bl["limit"] - BL_STEP)
                bl["full"] = True
            bl["since"] = time.time()
        else:
            bl["full"] = False
            if time.time() - bl["since"] > 86400:
                bl["limit"] = min(BL_MAX, bl["limit"] + BL_STEP)
                bl["since"] = time.time()
        bl["limit"] = max(minsoc, bl["limit"])
        return bl["limit"]

    # ---------- Regelung ----------
    def decide(self, m):
        """Entscheidung plus Grenzen: AC-Eingangsstrom des Geraets, Peak Shaving
        (Netzbezugsgrenze), Einspeisegrenze. -> (art, w, text)"""
        art, w, text = self._decide(m)
        if art == "app" or m.get("soc") is None:
            return art, w, text
        ac, grid, eps = m.get("ac"), m.get("grid"), m.get("eps") or 0
        # Geraetegrenze AC-Eingang: Netzladen + Notstromverbraucher <= Grenze
        lim_a = m.get("ac_in_limit_a")
        if art == "netz" and lim_a is not None:
            cap = max(0, int(lim_a * NOMINAL_AC_V - max(eps, 0)))
            if w > cap:
                w, text = cap, text + ", AC-Eingang begrenzt auf %.1f A" % lim_a
        if art == "netz" and m.get("max_charge_w") is not None and m["max_charge_w"] >= 0:
            w = min(w, int(m["max_charge_w"]))
        if grid is None or ac is None:
            return art, w, text
        # Netz nach dem Befehl (Naeherung): Verbrauch - Solakon-Abgabe
        load = grid + ac
        cmd = w if art == "abgabe" else -w
        # Peak Shaving: Netzbezug <= Grenze (Venus /Settings/CGwacs/AcInputLimit, A)
        imp_a = m.get("peak_import_a")
        if imp_a is not None and imp_a >= 0:
            limit_w = imp_a * NOMINAL_AC_V
            if load - cmd > limit_w:
                floor = m["minsoc"] if m.get("always_peak") else m.get("active_soc_limit", m["minsoc"])
                need = int(min(MAX_EXPORT_W, load - limit_w))
                if art == "netz":
                    w2 = int(max(0, limit_w - load))
                    if w2 > 0:
                        return "netz", w2, text + ", Peak Shaving: Laden auf %d W reduziert" % w2
                    art, w, cmd = "abgabe", 0, 0
                if (self.mode != MODE_CHARGER and not self.req["/Ess/DisableDischarge"]
                        and m["soc"] > floor and need > w):
                    return "abgabe", need, "Peak Shaving: Netzbezug auf %d W begrenzen, Abgabe %d W" % (
                        limit_w, need)
                if art == "abgabe" and w == 0:
                    return art, w, text + ", Peak Shaving: Akku unter %d %%" % floor
        # Einspeisegrenze (Venus /Settings/CGwacs/AcExportLimit, A)
        exp_a = m.get("peak_export_a")
        if art == "abgabe" and exp_a is not None and exp_a >= 0:
            cap = int(max(0, load + exp_a * NOMINAL_AC_V))
            if w > cap:
                w, text = cap, text + ", Einspeisung begrenzt auf %d W" % (exp_a * NOMINAL_AC_V)
        return art, w, text

    def feed_limit(self, want, m):
        """Einspeisegrenzen wie im Victron-ESS-Menue: Einspeisung verhindern (auch
        DESS-Sperre), max. Einspeisung. want = gewuenschte Abgabe in W."""
        house = m["ac"] + m["grid"]                      # Verbrauch hinter dem Zaehler
        if self.req["/Ess/DisableFeedIn"] or m.get("prevent_feedback"):
            want = min(want, house)                      # Netz nicht negativ
        if m.get("max_feedin_w") is not None and m["max_feedin_w"] >= 0:
            want = min(want, house + m["max_feedin_w"])
        return want

    def pv_surplus(self, m):
        """Abgabe fuer PV-Ueberschuss bei vollem Akku, wenn 'DC-gekoppelte PV:
        Ueberschuss einspeisen' an ist (/Settings/CGwacs/OvervoltageFeedIn); sonst 0
        (der Akku nimmt nichts mehr auf, das Solakon regelt die Module ab)."""
        if m.get("pv_feedin") and m["soc"] >= m["maxsoc"]:
            return max(0, m["pv"] - PV_LOSS_W)
        return 0

    def _decide(self, m):
        """m: Messwerte. -> (art, w, text) mit art in app | abgabe | netz"""
        mode, ess = self.mode, self.ess_mode
        if mode == MODE_OFF:
            return "app", None, "Aus: App-Freigabe"
        if ess == ESS_EXTERNAL and not self.req["/Ess/UseInverterPowerSetpoint"]:
            return "app", None, "Externe Steuerung: App-Freigabe"
        for k in ("soc", "ac", "pv", "minsoc", "ongrid", "maxsoc"):
            if m.get(k) is None:
                return "abgabe", 0, "Messwert %s fehlt: Abgabe 0 W" % k

        soc = m["soc"]
        reserve = max(m["minsoc"], m["ongrid"])
        if ess == ESS_BATTERYLIFE:
            reserve = max(reserve, self.batterylife_limit(soc, reserve))
        m["active_soc_limit"] = reserve
        charge_w = min(MAX_CHARGE_W, int(self.settings["chargepower"]))
        export_max = MAX_EXPORT_W
        if m.get("max_discharge_w") is not None and m["max_discharge_w"] >= 0:
            export_max = min(export_max, int(m["max_discharge_w"]))
        grid_charge_ok = mode != MODE_INVERTER and not self.req["/Ess/DisableCharge"]
        feed_ok = mode != MODE_CHARGER

        # Fester WR-Sollwert (Venus-Ladezeitplaene, externe Regler): + = laden
        if self.req["/Ess/UseInverterPowerSetpoint"]:
            sp = float(self.req["/Ess/InverterPowerSetpoint"] or 0)
            if sp > 0 and grid_charge_ok and soc < m["maxsoc"]:
                w = int(min(sp, charge_w))
                return "netz", w, "Zeitplan/extern: laden %d W" % w
            if sp < 0 and feed_ok and soc > reserve and not self.req["/Ess/DisableDischarge"]:
                w = int(min(-sp, export_max))
                return "abgabe", w, "Zeitplan/extern: abgeben %d W" % w
            return "abgabe", 0, "Zeitplan/extern: PV in den Akku"

        # Akkus geladen halten
        if ess == ESS_KEEP_CHARGED:
            if soc >= m["maxsoc"]:
                self.keep_active = False
            elif soc <= m["maxsoc"] - 2:
                self.keep_active = True
            if self.keep_active and grid_charge_ok and m.get("grid") is not None:
                return "netz", charge_w, "Geladen halten: %d W bis %d %%" % (charge_w, m["maxsoc"])
            if m.get("grid") is None or not feed_ok or soc < m["maxsoc"]:
                return "abgabe", 0, "Geladen halten: keine Abgabe"
            # Akku voll: PV deckt das Haus (kein Entladen), Ueberschuss nach Einstellung
            want = max(m["ac"] + m["grid"], self.pv_surplus(m))
            w = int(max(0, min(self.feed_limit(want, m), m["pv"] - PV_LOSS_W, export_max)))
            return "abgabe", w, "Geladen halten, Akku voll: PV %d W" % w

        # Optimiert (mit/ohne BatteryLife): Netz auf Sollwert regeln
        if m.get("grid") is None:
            self.reserve_active = False
            return "abgabe", 0, "Netzzaehler fehlt: Abgabe 0 W"
        if soc >= reserve:
            self.reserve_active = False
        elif soc <= reserve - 2:
            self.reserve_active = True
        if self.reserve_active and grid_charge_ok:
            w = min(charge_w, int(self.settings["reservechargepower"]))
            return "netz", w, "Reserve %d %% nachladen mit %d W" % (reserve, w)

        setpoint = self.grid_setpoint(m)
        # Verbrauch hinter dem Zaehler = Netz + Solakon-Abgabe; davon bleibt der Sollwert am Netz
        want = m["ac"] + m["grid"] - setpoint
        surplus = self.pv_surplus(m)
        if surplus > want:
            want = surplus
            setpoint_text = "PV-Ueberschuss einspeisen"
        else:
            setpoint_text = None
        want = self.feed_limit(want, m)
        if self.req["/Ess/DisableDischarge"]:
            want = min(want, m["pv"])
        if self.req["/Ess/DisableCharge"]:
            want = max(want, min(m["pv"], export_max))

        if want < -GRID_CHARGE_START_W and grid_charge_ok and soc < m["maxsoc"]:
            w = int(min(-want, charge_w))
            return "netz", w, "Ueberschuss laden %d W (Sollwert %d W)" % (w, setpoint)
        if not feed_ok:
            return "abgabe", 0, "Nur Ladegeraet: keine Abgabe"
        if soc <= reserve and not self.req["/Ess/DisableCharge"]:
            # An/unter der Reserve nur PV-Durchleitung bis zum Verbrauch, kein Akku-Entladen.
            # Die Wandlungsverluste abziehen, sonst deckt der Akku sie.
            w = int(max(0, min(want, m["pv"] - PV_LOSS_W, export_max)))
            return "abgabe", w, "Reserve %d %% erreicht: nur PV, %d W" % (reserve, w)
        w = int(max(0, min(want, export_max)))
        if setpoint_text:
            return "abgabe", w, "%s: %d W" % (setpoint_text, w)
        return "abgabe", w, "Eigenverbrauch %d W (Sollwert %d W)" % (w, setpoint)

    def run(self, m):
        """Ein Regelzyklus. Gibt Statuswerte fuer D-Bus zurueck."""
        if not self.enabled:
            self.text = "Steuerung aus (config.ini), Geraet im App-Betrieb"
            return
        art, w, text = self.decide(m)
        now = time.monotonic()
        # Pendelt der Sollwert um 0, nicht staendig zwischen Abgabe und Netzladen
        # umschalten: bis MODE_HOLD_S im alten Modus bleiben, mit 0 W
        last_art = self.last[0] if self.last else None
        if art in ("abgabe", "netz") and last_art in ("abgabe", "netz") and art != last_art:
            wait = MODE_HOLD_S - (now - self.art_t)
            if wait > 0:
                art, w = last_art, 0
                text += " (Wechsel in %d s)" % wait
        if m.get("dess_missing"):
            text += "; Dynamic ESS: Grenzen nicht eingetragen (%s)" % m["dess_missing"]
            if not self.dess_warned:
                log.warning("Dynamic ESS aktiv, aber Grenzen auf -1: %s", m["dess_missing"])
                self.dess_warned = True
        else:
            self.dess_warned = False
        self.text = text
        changed = (self.last is None or self.last[0] != art
                   or (w is not None and abs((self.last[1] or 0) - w) >= MIN_CHANGE_W))
        if art == "app":
            if self.last == ("app", None):
                return
        elif not changed and now - self.last_t < RENEW_S:
            return
        elif changed and self.last is not None and now - self.last_t < MIN_INTERVAL_S:
            return
        ok = self.apply(art, w)
        if ok:
            if self.last is None or self.last[0] != art or \
                    abs((self.last[1] or 0) - (w or 0)) >= LOG_CHANGE_W:
                log.info("OK: %s", text)
            if art != last_art:
                self.art_t = now
            self.last, self.last_t, self.error = (art, w), now, None
        else:
            self.error = "Schreiben fehlgeschlagen: %s" % text
            log.warning(self.error)
