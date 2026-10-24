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

log = logging.getLogger("dbus-solakon.settings")

# /SwitchableOutput/x/Settings/Type (Venus OS 3.6x+)
TYPE_TOGGLE, TYPE_DROPDOWN, TYPE_NUMERIC = 1, 6, 8
FUNCTION_MANUAL = 2
STATUS_OFF, STATUS_ON = 0x00, 0x09
MODULE_CONNECTED = 0x100
SETTINGS = "com.victronenergy.settings"
EPS_VALUES = [0, 2, 3]      # 46613: aus, EPS, USV (1 ist im Protokoll nicht belegt)


def unlimited(maxval):
    """-1 (= unbegrenzt) wird als Hoechstwert angezeigt und zurueck als -1 geschrieben."""
    return (lambda v: maxval if v is None or v < 0 else v,
            lambda d: -1 if d >= maxval else d)


def zero_off():
    """-1 (= aus) wird als 0 angezeigt, 0 als -1 geschrieben."""
    return (lambda v: 0 if v is None or v < 0 else v,
            lambda d: -1 if d <= 0 else d)


class EssSettings(object):
    def __init__(self, drv, service_factory):
        self.drv = drv
        cg = "/Settings/CGwacs/"
        de = "/Settings/DynamicEss/"
        # (Kanal, Name, Typ, Quelle, Einheit, Nachkommastellen, min, max, Schritt, Umrechnung)
        # Quelle: ("venus", Pfad) | ("solakon", Schluessel) | ("reg", Adresse) | ("eps",)
        self.outputs = [
            ("netz_sollwert", "Netz-Sollwert", TYPE_NUMERIC, ("venus", cg + "AcPowerSetPoint"),
             "W", 0, -800, 1200, 10, None),
            ("einspeisung_aus", "Einspeisung verhindern", TYPE_TOGGLE,
             ("venus", cg + "PreventFeedback"), None, 0, 0, 1, 1, None),
            ("max_einspeisung", "Max. Einspeisung (800 = unbegrenzt)", TYPE_NUMERIC,
             ("venus", cg + "MaxFeedInPower"), "W", 0, 0, 800, 10, unlimited(800)),
            ("max_entladen", "Max. Abgabe Wechselrichter (800 = unbegrenzt)", TYPE_NUMERIC,
             ("venus", cg + "MaxDischargePower"), "W", 0, 0, 800, 10, unlimited(800)),
            ("max_laden", "Max. Netzladen (1200 = unbegrenzt)", TYPE_NUMERIC,
             ("venus", cg + "MaxChargePower"), "W", 0, 0, 1200, 10, unlimited(1200)),
            ("peak_bezug", "Peak Shaving: Netzbezug (0 = aus)", TYPE_NUMERIC,
             ("venus", cg + "AcInputLimit"), "A", 1, 0, 63, 0.5, zero_off()),
            ("peak_einspeisung", "Peak Shaving: Einspeisung (0 = aus)", TYPE_NUMERIC,
             ("venus", cg + "AcExportLimit"), "A", 1, 0, 63, 0.5, zero_off()),
            ("peak_immer", "Peak Shaving auch unter der Reserve", TYPE_TOGGLE,
             ("venus", cg + "AlwaysPeakShave"), None, 0, 0, 1, 1, None),
            ("ladeleistung", "Ladeleistung „Geladen halten“", TYPE_NUMERIC,
             ("solakon", "chargepower"), "W", 0, 0, 1200, 50, None),
            ("reserve_laden", "Nachladeleistung unter der Reserve", TYPE_NUMERIC,
             ("solakon", "reservechargepower"), "W", 0, 0, 1200, 50, None),
            ("max_soc", "Max. SoC", TYPE_NUMERIC, ("reg", 46610), "%", 0, 10, 100, 1, None),
            ("min_soc_phys", "Min. SoC (Akkuschutz, auch bei Netzausfall)", TYPE_NUMERIC,
             ("reg", 46609), "%", 0, 10, 100, 1, None),
            ("eps", "Notstrom-Ausgang", TYPE_DROPDOWN, ("eps",), None, 0, 0, 2, 1, None),
            # Dynamic ESS: normalerweise von VRM gesetzt; -1 = nicht eingetragen
            ("dess_laden", "Dynamic ESS: max. Laden (Geraet 1,2)", TYPE_NUMERIC,
             ("venus", de + "BatteryChargeLimit"), "kW", 1, -1, 1.2, 0.1, None),
            ("dess_entladen", "Dynamic ESS: max. Entladen (Geraet 0,8)", TYPE_NUMERIC,
             ("venus", de + "BatteryDischargeLimit"), "kW", 1, -1, 0.8, 0.1, None),
            ("dess_bezug", "Dynamic ESS: max. Netzbezug", TYPE_NUMERIC,
             ("venus", de + "GridImportLimit"), "kW", 1, -1, 50, 0.1, None),
            ("dess_einspeisung", "Dynamic ESS: max. Einspeisung", TYPE_NUMERIC,
             ("venus", de + "GridExportLimit"), "kW", 1, -1, 50, 0.1, None),
        ]
        s = self.s = service_factory("com.victronenergy.switch.solakon_ess", 91,
                                     "Solakon ESS-Einstellungen", "Solakon ESS")
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
                         ("Group", "Solakon ESS"), ("ShowUIControl", 1),
                         ("Function", FUNCTION_MANUAL), ("ValidFunctions", 1 << FUNCTION_MANUAL)):
                s.add_path(b + "Settings/" + k, v)
            if typ == TYPE_NUMERIC:
                for k, v in (("DimmingMin", lo), ("DimmingMax", hi), ("StepSize", step),
                             ("Unit", unit), ("Decimals", dec)):
                    s.add_path(b + "Settings/" + k, v)
            if typ == TYPE_DROPDOWN:
                s.add_path(b + "Settings/Labels", dbus.Array(["Aus", "EPS", "USV"],
                                                             signature="s"))
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
            else:
                ok = False
        except dbus.exceptions.DBusException as e:
            log.warning("%s: %s", name, e)
            ok = False
        log.info("Einstellung %s -> %s: %s", name, value, "ok" if ok else "abgewiesen")
        # Anzeige kurz danach auf den echten Stand bringen (auch bei Ablehnung)
        GLib.timeout_add(500, lambda: self.refresh() and False)
        return ok
