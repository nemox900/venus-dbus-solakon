#!/usr/bin/env python3
# Solakon PowerTracker IR (Lesekopf am Hauszaehler) -> Venus-Netzzaehler.
# Eigener kleiner Prozess, nur lesend, HTTP-API des Lesekopfs (config.ini).
# Der Lesekopf liefert nur die saldierte Summe, darum genau eine virtuelle
# Phase L1 (keine echte Phasenmessung, keine Spannungen/Stroeme).
# In Venus angemeldet nur, wenn /Settings/Solakon/GridMeter = 1 (PowerTracker) ist
# oder die Einstellung fehlt (dbus-solakon laeuft nicht).

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
from vedbus import VeDbusService

VERSION = "0.7.0"
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = configparser.ConfigParser()
CONFIG.read([os.path.join(BASE, "config.default.ini"), os.path.join(BASE, "config.ini")])
URL = "http://%s/api/v1/status?extended=true" % CONFIG.get("powertracker", "host")
STALE_S = 15    # so lange darf total_rx_bytes stillstehen
METER_POWERTRACKER = 1

log = logging.getLogger("dbus-powertracker")


class PowerTracker(object):
    def __init__(self):
        s = self.s = VeDbusService("com.victronenergy.grid.solakon_powertracker",
                                   register=False)
        for p, v in (("/Mgmt/ProcessName", __file__), ("/Mgmt/ProcessVersion", VERSION),
                     ("/Mgmt/Connection", "HTTP PowerTracker"), ("/DeviceInstance", 30),
                     ("/ProductId", 0xFFFF), ("/ProductName", "Solakon PowerTracker IR"),
                     ("/CustomName", "Solakon PowerTracker (HTTP)"), ("/FirmwareVersion", None),
                     ("/Serial", None), ("/Connected", 0), ("/DeviceType", None),
                     ("/Role", "grid"), ("/NrOfPhases", 1), ("/Position", None)):
            s.add_path(p, v)
        for p in ("/Ac/Power", "/Ac/L1/Power", "/Ac/Energy/Forward", "/Ac/Energy/Reverse",
                  "/Ac/L1/Energy/Forward", "/Ac/L1/Energy/Reverse",
                  "/PowerTracker/SignalStrength", "/PowerTracker/Rssi",
                  "/PowerTracker/FirmwareText"):
            s.add_path(p, None)
        self.sysbus = dbus.SystemBus()
        self.sync()
        self.rx = None
        self.rx_t = 0
        GLib.timeout_add(2000, self.tick)
        log.info("PowerTracker-Treiber %s gestartet", VERSION)

    def sync(self):
        """Je nach Auswahl in den Solakon-Einstellungen an- oder abmelden."""
        try:
            sel = self.sysbus.get_object("com.victronenergy.settings",
                                         "/Settings/Solakon/GridMeter").GetValue(
                dbus_interface="com.victronenergy.BusItem")
            want = int(sel) == METER_POWERTRACKER
        except (dbus.exceptions.DBusException, TypeError, ValueError):
            want = True
        if want and self.s._dbusname is None:
            self.s.register()
            log.info("Netzzaehler PowerTracker angemeldet")
        elif not want and self.s._dbusname is not None:
            self.s._dbusname.__del__()
            self.s._dbusname = None
            log.info("Netzzaehler PowerTracker abgemeldet")

    def tick(self):
        try:
            self.sync()
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
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    PowerTracker()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
