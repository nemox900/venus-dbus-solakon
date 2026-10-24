#!/usr/bin/env python3
# Solakon PowerTracker IR (Lesekopf am Hauszaehler) -> Venus-Netzzaehler.
# Eigener kleiner Prozess, nur lesend, HTTP-API des Lesekopfs (config.ini).
# Der Lesekopf liefert nur die saldierte Summe, darum genau eine virtuelle
# Phase L1 (keine echte Phasenmessung, keine Spannungen/Stroeme).
# Ein normaler Venus-Zaehler wie Victron-Zaehler oder Shelly: immer angemeldet,
# Rolle (Netz, PV-Wechselrichter, Generator, Verbraucher) in den Geraete-
# Einstellungen waehlbar. Rolle und Instanz stehen wie bei dbus-shelly zusammen in
# /Settings/Devices/solakon_powertracker/ClassAndVrmInstance (z. B. "grid:30");
# aendert sich das (Rolle oder Menue "VRM-Geraeteinstanzen"), startet der Dienst neu
# und meldet sich unter der neuen Rolle bzw. Instanz an.

import configparser
import json
import logging
import os
import sys
import time
import urllib.request

sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
import dbus
import dbus.mainloop.glib
from gi.repository import GLib
from settingsdevice import SettingsDevice
from vedbus import VeDbusService

VERSION = "0.7.0"
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = configparser.ConfigParser()
CONFIG.read([os.path.join(BASE, "config.default.ini"), os.path.join(BASE, "config.ini")])
PT_HOST = CONFIG.get("powertracker", "host", fallback="").strip()
URL = "http://%s/api/v1/status?extended=true" % PT_HOST
STALE_S = 15    # so lange darf total_rx_bytes stillstehen
ROLES = ["grid", "pvinverter", "genset", "acload"]
SETTINGS = "/Settings/Devices/solakon_powertracker/"
DEFAULT_NAME = "Solakon PowerTracker (HTTP)"

log = logging.getLogger("dbus-powertracker")


class PowerTracker(object):
    def __init__(self):
        self.sysbus = dbus.SystemBus()
        self.settings = SettingsDevice(self.sysbus, {
            "instance": [SETTINGS + "ClassAndVrmInstance", "grid:30", 0, 0],
            "name": [SETTINGS + "CustomName", "", 0, 0],
            "position": [SETTINGS + "Position", 0, 0, 2]}, self.setting_changed)
        role, instance = str(self.settings["instance"]).split(":")
        if role not in ROLES:
            role = "grid"
        self.role, self.instance = role, int(instance)
        s = self.s = VeDbusService("com.victronenergy.%s.solakon_powertracker" % role,
                                   register=False)
        for p, v in (("/Mgmt/ProcessName", __file__), ("/Mgmt/ProcessVersion", VERSION),
                     ("/Mgmt/Connection", "HTTP PowerTracker"), ("/DeviceInstance", self.instance),
                     ("/ProductId", 0xFFFF), ("/ProductName", "Solakon PowerTracker IR"),
                     ("/FirmwareVersion", None), ("/Serial", None), ("/Connected", 0),
                     ("/DeviceType", None), ("/NrOfPhases", 1)):
            s.add_path(p, v)
        s.add_path("/CustomName", str(self.settings["name"]) or DEFAULT_NAME, writeable=True,
                   onchangecallback=self.set_name)
        s.add_path("/AllowedRoles", dbus.Array(ROLES, signature="s"))
        s.add_path("/Role", role, writeable=True, onchangecallback=self.set_role)
        if role == "pvinverter":
            # 0 = AC-Eingang, 1 = AC-Ausgang, 2 = AC-Eingang 2
            s.add_path("/Position", int(self.settings["position"]), writeable=True,
                       onchangecallback=self.set_position)
        if role != "grid":
            s.add_path("/IsGenericEnergyMeter", 1)   # wie Victron: Zaehler in fremder Rolle
        for p in ("/Ac/Power", "/Ac/L1/Power", "/Ac/Energy/Forward", "/Ac/Energy/Reverse",
                  "/Ac/L1/Energy/Forward", "/Ac/L1/Energy/Reverse",
                  "/PowerTracker/SignalStrength", "/PowerTracker/Rssi",
                  "/PowerTracker/FirmwareText"):
            s.add_path(p, None)
        s.register()
        self.rx = None
        self.rx_t = 0
        GLib.timeout_add(2000, self.tick)
        log.info("PowerTracker-Treiber %s gestartet", VERSION)

    def set_role(self, path, value):
        if value not in ROLES:
            return False
        # neu starten passiert ueber setting_changed (Dienstname haengt an der Rolle)
        self.settings["instance"] = "%s:%d" % (value, self.instance)
        return True

    def set_name(self, path, value):
        v = str(value).strip()
        self.settings["name"] = "" if v == DEFAULT_NAME else v
        if not v:
            GLib.idle_add(lambda: self.set_value("/CustomName", DEFAULT_NAME))
        return True

    def set_position(self, path, value):
        if value not in (0, 1, 2):
            return False
        self.settings["position"] = value
        return True

    def set_value(self, path, value):
        with self.s as w:
            w[path] = value
        return False

    def setting_changed(self, setting, old, new):
        if setting == "instance" and old != new:
            log.info("Rolle/Instanz %s -> %s, Neustart", old, new)
            # daemontools startet den Dienst wieder
            GLib.timeout_add(500, lambda: os._exit(0))
        elif setting == "name":
            self.set_value("/CustomName", str(new) or DEFAULT_NAME)

    def tick(self):
        try:
            self.update()
        except Exception:
            log.exception("Fehler im Zyklus")
        return True

    def update(self):
        now = time.monotonic()
        try:
            with urllib.request.urlopen(URL, timeout=3) as r:
                d = json.load(r)
        except Exception as e:
            log.warning("HTTP: %s", e)
            d = None
        if d is not None:
            rx = d["meter"]["total_rx_bytes"]
            if rx != self.rx:
                self.rx, self.rx_t = rx, now
        if d is None or now - self.rx_t > STALE_S:
            with self.s as w:
                for p in ("/Ac/Power", "/Ac/L1/Power"):
                    w[p] = None
                w["/Connected"] = 0
            return
        obis = {o["obis_code"]: float(o["value"]) for o in d["meter"]["obis_values"]}
        p = d["extracted"]["instantaneous_power_w"]   # + = Bezug
        fwd = obis.get("1-0:1.8.0")
        rev = obis.get("1-0:2.8.0")
        with self.s as w:
            w["/Connected"] = 1
            w["/Ac/Power"] = round(p, 1)
            w["/Ac/L1/Power"] = round(p, 1)
            w["/Ac/Energy/Forward"] = w["/Ac/L1/Energy/Forward"] = \
                round(fwd / 1000.0, 3) if fwd is not None else None
            w["/Ac/Energy/Reverse"] = w["/Ac/L1/Energy/Reverse"] = \
                round(rev / 1000.0, 3) if rev is not None else None
            w["/Serial"] = d["device"]["hw_sn"]
            # /FirmwareVersion ist bei Venus eine Zahl; Klartext getrennt
            w["/PowerTracker/FirmwareText"] = d["device"]["fw_ver"]
            w["/PowerTracker/SignalStrength"] = d["meter"].get("signal_strength")
            w["/PowerTracker/Rssi"] = d["wifi"].get("rssi")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not PT_HOST:
        # nicht eingerichtet; start.sh haengt den Dienst dann gar nicht erst ein
        log.info("PowerTracker nicht eingerichtet ([powertracker] host leer)")
        while True:
            time.sleep(3600)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    PowerTracker()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
