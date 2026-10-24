# Solakon-Mod: Einstell-Geraet "Solakon ESS-Einstellungen".
#
# Beim Multi RS blendet Venus das ESS-Menue aus (die Einstellungen liegen dort
# im Geraet und werden mit VictronConnect gesetzt). Unser Regler (control.py)
# liest sie aber aus den normalen Venus-Einstellungen /Settings/CGwacs/*.
# Dieses Geraet zeigt sie als com.victronenergy.switch mit Zahlenfeldern,
# Auswahllisten und Schaltern an: in der Geraeteliste und im Schalter-Bereich
# der Oberflaeche, auch ueber VRM. Es schreibt dieselben Einstellungen, die
# das ESS-Menue eines VE.Bus-Systems schreiben wuerde.

import logging

import dbus
from gi.repository import GLib

from i18n import T

log = logging.getLogger("dbus-solakon.settings")

# /SwitchableOutput/x/Settings/Type (Venus OS 3.6x+)
TYPE_TOGGLE, TYPE_DROPDOWN, TYPE_NUMERIC = 1, 6, 8
FUNCTION_MANUAL = 2
STATUS_OFF, STATUS_ON = 0x00, 0x09
MODULE_CONNECTED = 0x100
SETTINGS = "com.victronenergy.settings"
EPS_VALUES = [0, 2, 3]      # 46613: aus, EPS, USV (1 ist im Protokoll nicht belegt)
MODE_VALUES = [3, 1, 2, 4]  # Venus /Mode: Ein, nur Ladegeraet, nur Wechselrichter, Aus
# /Settings/Solakon/GridMeter: 0 Solakon-Zaehler, 2 Netzzaehler von Venus OS
# (PowerTracker, Victron-Zaehler, Shelly ...; welcher, regelt Venus ueber die Rolle)
METER_VALUES = [0, 2]


def unlimited(maxval):
    """-1 (= unbegrenzt) wird als Hoechstwert angezeigt und zurueck als -1 geschrieben."""
    return (lambda v: maxval if v is None or v < 0 else v,
            lambda d: -1 if d >= maxval else d)


def amps_as_watts():
    """Venus speichert die Netzbezugsgrenze in A (-1 = aus); angezeigt in W (x 230 V,
    Summe aller Phasen), 0 W = aus."""
    return (lambda v: 0 if v is None or v < 0 else v * 230,
            lambda d: -1 if d <= 0 else d / 230.0)


def zero_off():
    """-1 (= aus) wird als 0 angezeigt, 0 als -1 geschrieben."""
    return (lambda v: 0 if v is None or v < 0 else v,
            lambda d: -1 if d <= 0 else d)


class EssSettings(object):
    def __init__(self, drv, service_factory):
        self.drv = drv
        cg = "/Settings/CGwacs/"
        de = "/Settings/DynamicEss/"
        eps_labels = [T("Aus", "Off"), T("EPS (bei Netzausfall)", "EPS (on grid failure)"),
                      T("USV (unterbrechungsfrei)", "UPS (uninterrupted)")]
        mode_labels = [T("Ein: Venus regelt", "On: Venus controls"),
                       T("Nur laden", "Charge only"), T("Nur abgeben", "Discharge only"),
                       T("Aus: App regelt", "Off: app controls")]
        meter_labels = ["Solakon (Modbus)", "Venus OS"]
        ess_labels = [T("Optimiert + BatteryLife", "Optimized + BatteryLife"),
                      T("Optimiert", "Optimized"),
                      T("Akku voll halten", "Keep batteries charged"),
                      T("Extern (App, Zeitplan)", "External (app, schedule)")]  # 0..3
        # (Kanal, Name, Typ, Quelle, Einheit bzw. Auswahltexte, Nachkommastellen, min, max,
        #  Schritt, Umrechnung)
        # Die Oberflaeche sortiert nach dem Namen und zeigt nur ~30 Zeichen: Oberbegriff
        # vorne (Betrieb, Einspeisung, Max. Leistung, Netzladen, Notstrom, SoC, VRM-DESS,
        # WR-Abgabe; englisch Control, EPS, Feed-in, Grid charge, Inverter, Peak shaving, SoC,
        # VRM DESS, Inverter), Erklaerungen in README.md. "Limit (800=aus)": Hoechstwert =
        # kein Limit.
        # Quelle: ("venus", Pfad) | ("solakon", Schluessel) | ("reg", Adresse) |
        #         ("mode",) | ("essmode",) | ("meter",) | ("eps",)
        self.outputs = [
            ("01_betrieb", T("Betrieb: An / Aus", "Control: On / Off"), TYPE_DROPDOWN, ("mode",),
             mode_labels, 0, 0, len(MODE_VALUES) - 1, 1, None),
            ("02_essmodus", T("Betrieb: ESS-Modus", "Control: ESS mode"), TYPE_DROPDOWN, ("essmode",),
             ess_labels, 0, 0, len(ess_labels) - 1, 1, None),
            ("02b_netzzaehler", T("Betrieb: Netzz\u00e4hler", "Control: grid meter"), TYPE_DROPDOWN, ("meter",),
             meter_labels, 0, 0, len(meter_labels) - 1, 1, None),
            ("03_netz_sollwert", T("Betrieb: Netz-Sollwert", "Control: grid setpoint"), TYPE_NUMERIC,
             ("venus", cg + "AcPowerSetPoint"), "W", 0, -800, 1200, 10, None),
            ("04_einspeisung_aus", T("Einspeisung: verhindern", "Feed-in: prevent"), TYPE_TOGGLE,
             ("venus", cg + "PreventFeedback"), None, 0, 0, 1, 1, None),
            ("05_max_einspeisung", T("Einspeisung: Limit (800=aus)", "Feed-in: limit (800=off)"), TYPE_NUMERIC,
             ("venus", cg + "MaxFeedInPower"), "W", 0, 0, 800, 10, unlimited(800)),
            ("06_pv_ueberschuss", T("Einspeisung: PV-\u00dcberschuss", "Feed-in: PV excess"), TYPE_TOGGLE,
             ("venus", cg + "OvervoltageFeedIn"), None, 0, 0, 1, 1, None),
            ("07_max_laden", T("Netzladen: Limit (1200=aus)", "Grid charge: limit (1200=off)"), TYPE_NUMERIC,
             ("venus", cg + "MaxChargePower"), "W", 0, 0, 1200, 10, unlimited(1200)),
            ("08_ladeleistung", T("Netzladen: Akku voll halten", "Grid charge: keep charged"), TYPE_NUMERIC,
             ("solakon", "chargepower"), "W", 0, 0, 1200, 50, None),
            ("09_reserve_laden", T("Netzladen: unter Reserve", "Grid charge: below reserve"), TYPE_NUMERIC,
             ("solakon", "reservechargepower"), "W", 0, 0, 1200, 50, None),
            ("10_eps", T("Notstrom-Ausgang", "EPS output"), TYPE_DROPDOWN, ("eps",),
             eps_labels, 0, 0, len(EPS_VALUES) - 1, 1, None),
            # Peak Shaving (Victron: "Netzbezug begrenzen"). Die Einspeisegrenze
            # AcExportLimit steht nicht im Menue, "Einspeisung: Limit" deckt das ab;
            # der Regler beachtet sie trotzdem, falls sie gesetzt ist.
            ("11_peak_bezug", T("Max. Leistung: Netzbezug", "Peak shaving: grid import"), TYPE_NUMERIC,
             ("venus", cg + "AcInputLimit"), "W", 0, 0, 14490, 230, amps_as_watts()),
            ("13_peak_immer", T("Max. Leistung: bis Akkuschutz", "Peak shaving: to min SoC"), TYPE_TOGGLE,
             ("venus", cg + "AlwaysPeakShave"), None, 0, 0, 1, 1, None),
            ("14_max_soc", T("SoC: max. (Ladeende)", "SoC: max (charge end)"), TYPE_NUMERIC, ("reg", 46610),
             "%", 0, 10, 100, 1, None),
            ("15_min_soc_phys", T("SoC: min. (Akkuschutz)", "SoC: min (protection)"), TYPE_NUMERIC, ("reg", 46609),
             "%", 0, 10, 100, 1, None),
            ("16_reserve", T("SoC: Reserve (Netzbetrieb)", "SoC: reserve (on grid)"), TYPE_NUMERIC, ("reg", 46611),
             "%", 0, 10, 100, 1, None),
            # Dynamic ESS: normalerweise von VRM gesetzt; -1 = nicht eingetragen
            ("16b_dess_kapazitaet", T("VRM-DESS: Akkukapazit\u00e4t", "VRM DESS: battery capacity"), TYPE_NUMERIC,
             ("venus", de + "BatteryCapacity"), "kWh", 1, 0, 50, 0.1, None),
            ("17_dess_laden", T("VRM-DESS: Akku laden max.", "VRM DESS: charge max"), TYPE_NUMERIC,
             ("venus", de + "BatteryChargeLimit"), "kW", 1, -1, 1.2, 0.1, None),
            ("18_dess_entladen", T("VRM-DESS: Akku entladen max.", "VRM DESS: discharge max"), TYPE_NUMERIC,
             ("venus", de + "BatteryDischargeLimit"), "kW", 1, -1, 0.8, 0.1, None),
            ("19_dess_bezug", T("VRM-DESS: Hausanschluss Bezug", "VRM DESS: house grid import"), TYPE_NUMERIC,
             ("venus", de + "GridImportLimit"), "kW", 1, -1, 50, 0.1, None),
            ("20_dess_einspeisung", T("VRM-DESS: Einspeisung max.", "VRM DESS: feed-in max"), TYPE_NUMERIC,
             ("venus", de + "GridExportLimit"), "kW", 1, -1, 50, 0.1, None),
            ("21_max_entladen", T("WR-Abgabe: Limit (800=aus)", "Inverter: limit (800=off)"), TYPE_NUMERIC,
             ("venus", cg + "MaxDischargePower"), "W", 0, 0, 800, 10, unlimited(800)),
        ]
        s = self.s = service_factory("com.victronenergy.switch.solakon_ess", "ess",
                                     T("Solakon ESS-Einstellungen", "Solakon ESS Settings"),
                                     "Solakon ESS")
        s.add_path("/State", MODULE_CONNECTED)
        self.value_path = {}
        for ch, name, typ, src, unit, dec, lo, hi, step, conv in self.outputs:
            b = "/SwitchableOutput/%s/" % ch
            value_path = b + ("State" if typ == TYPE_TOGGLE else "Dimming")
            s.add_path(b + "Name", name)
            s.add_path(b + "Status", STATUS_OFF)
            if typ == TYPE_TOGGLE:
                s.add_path(b + "State", None, writeable=True,
                           onchangecallback=lambda p, v, c=ch: self.write(c, v))
            else:
                s.add_path(b + "State", 1)
                s.add_path(b + "Dimming", None, writeable=True,
                           onchangecallback=lambda p, v, c=ch: self.write(c, v))
            for k, v in (("Type", typ), ("ValidTypes", 1 << typ), ("CustomName", ""),
                         ("Group", ""), ("ShowUIControl", 1),
                         ("Function", FUNCTION_MANUAL), ("ValidFunctions", 1 << FUNCTION_MANUAL)):
                s.add_path(b + "Settings/" + k, v)
            if typ == TYPE_NUMERIC:
                for k, v in (("DimmingMin", lo), ("DimmingMax", hi), ("StepSize", step),
                             ("Unit", unit), ("Decimals", dec)):
                    s.add_path(b + "Settings/" + k, v)
            if typ == TYPE_DROPDOWN:
                s.add_path(b + "Settings/Labels", dbus.Array(unit, signature="s"))
            self.value_path[ch] = value_path
        self.by_ch = {o[0]: o for o in self.outputs}

    # ---------- Werte lesen ----------
    def venus_get(self, path):
        try:
            v = self.drv.sysbus.get_object(SETTINGS, path).GetValue(
                dbus_interface="com.victronenergy.BusItem")
        except dbus.exceptions.DBusException:
            return None
        return None if isinstance(v, dbus.Array) else v

    def source_value(self, src):
        d = self.drv
        if src[0] == "venus":
            v = self.venus_get(src[1])
            return None if v is None else float(v)
        if src[0] == "solakon":
            return float(d.settings[src[1]])
        if src[0] == "reg":
            return d.reg.g(src[1]) if d.reg.fresh("slow", 300) else None
        if src[0] == "eps":
            v = d.reg.g(46613) if d.reg.fresh("slow", 300) else None
            return EPS_VALUES.index(v) if v in EPS_VALUES else None
        if src[0] == "mode":
            v = int(d.settings["mode"])
            return MODE_VALUES.index(v) if v in MODE_VALUES else None
        if src[0] == "essmode":
            return int(d.settings["essmode"])
        if src[0] == "meter":
            v = int(d.settings["gridmeter"])
            return METER_VALUES.index(v) if v in METER_VALUES else None
        return None

    def refresh(self):
        """Anzeige aus den Quellen nachfuehren (alle 10 s und nach jedem Schreiben)."""
        with self.s as w:
            w["/Connected"] = 1
            for ch, name, typ, src, unit, dec, lo, hi, step, conv in self.outputs:
                v = self.source_value(src)
                if v is not None and conv:
                    v = conv[0](v)
                if v is not None:
                    v = int(round(v)) if dec == 0 else round(v, dec)
                w[self.value_path[ch]] = v
                b = "/SwitchableOutput/%s/" % ch
                w[b + "Status"] = STATUS_ON if (typ == TYPE_TOGGLE and v) else STATUS_OFF
        return True

    # ---------- Werte schreiben ----------
    def venus_set(self, path, value):
        obj = self.drv.sysbus.get_object(SETTINGS, path)
        cur = obj.GetValue(dbus_interface="com.victronenergy.BusItem")
        v = dbus.Double(float(value)) if isinstance(cur, dbus.Double) else dbus.Int32(int(round(value)))
        return obj.SetValue(v, dbus_interface="com.victronenergy.BusItem") == 0

    def write(self, ch, value):
        ch, name, typ, src, unit, dec, lo, hi, step, conv = self.by_ch[ch]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        if not lo <= value <= hi:
            log.info("%s = %r abgewiesen (%s bis %s)", name, value, lo, hi)
            return False
        raw = conv[1](value) if conv else value
        if src[0] == "venus" and "/DynamicEss/" in src[1] and -1 < raw < 0:
            # -1 = nicht eingetragen; Zwischenwerte gibt es nicht: von -1 aus mit "+"
            # direkt auf 0, von 0 aus mit "-" zurueck auf -1
            cur = self.venus_get(src[1])
            raw = 0 if cur is not None and cur < 0 else -1
        d = self.drv
        try:
            if src[0] == "venus":
                ok = self.venus_set(src[1], raw)
            elif src[0] == "solakon":
                ok = d.set_power(src[1], int(raw))
            elif src[0] == "reg":
                ok = d.set_soc_limit(src[1], raw)
            elif src[0] == "eps":
                ok = d.set_eps(EPS_VALUES[int(raw)])
            elif src[0] == "mode":
                ok = d.ctl.set_mode(MODE_VALUES[int(raw)])
            elif src[0] == "essmode":
                ok = d.ctl.set_ess_mode(int(raw))
            elif src[0] == "meter":
                ok = d.set_gridmeter(METER_VALUES[int(raw)])
            else:
                ok = False
        except dbus.exceptions.DBusException as e:
            log.warning("%s: %s", name, e)
            ok = False
        log.info("Einstellung %s -> %s: %s", name, value, "ok" if ok else "abgewiesen")
        # Anzeige kurz danach auf den echten Stand bringen (auch bei Ablehnung)
        GLib.timeout_add(500, lambda: self.refresh() and False)
        return ok
