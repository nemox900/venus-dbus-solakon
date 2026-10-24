#!/usr/bin/env python3
# Solakon ONE -> Venus OS, nativer Treiber mit Steuerung (control.py).
#
# Ein Prozess, ein Modbus-Master (das Solakon vertraegt keine parallelen
# Zugriffe und nur kleine Bloecke), mehrere D-Bus-Geraete:
#   battery.solakon            Akku/BMS
#   solarcharger.solakon_pv1..4  je MPPT-Tracker ein Geraet
#   multi.solakon              Hybrid-Wechselrichter, RS-Schnittstelle fuer dbus-acsystem
#   temperature.solakon_wr / solakon_gehaeuse
#
# Registerquelle: Solakon ONE Modbus Protokoll 02-26 (Projektordner pdf/).
# Laeuft unveraendert auf jedem GX-Geraet mit Venus OS (Paket in /data/dbus-solakon,
# Einstellungen in config.ini, siehe README.md).
# Geschrieben wird nur mit [control] enabled = 1 in config.ini; sonst werden
# alle Schreibwuensche von Venus abgewiesen und das Geraet bleibt im App-Betrieb.

import configparser
import json
import logging
import os
import signal
import sys
import time

sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
import dbus
import dbus.mainloop.glib
from gi.repository import GLib
from pymodbus.client.sync import ModbusTcpClient
from settingsdevice import SettingsDevice
from vedbus import VeDbusService

import i18n
from control import Control
from ess_settings import EssSettings
from i18n import T

VERSION = "0.7.0"
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = configparser.ConfigParser()
CONFIG.read([os.path.join(BASE, "config.default.ini"), os.path.join(BASE, "config.ini")])
HOST = CONFIG.get("solakon", "host")
PORT = CONFIG.getint("solakon", "port")
UNIT = CONFIG.getint("solakon", "unit")
CONTROL_ENABLED = CONFIG.getboolean("control", "enabled", fallback=False)
STATE_FILE = os.path.join(BASE, "state.json")
STALE_S = 10            # so lange duerfen Schnellwerte alt sein
NOMINAL_V = 35.2        # Datenblatt: Nennspannung, 2,11 kWh je Akkumodul
# Kapazitaet kommt aus dem BMS (37635 Nennenergie, 0,01 kWh; z. B. 3 Module = 633)
AC_IN_LIMIT_A = 5.4     # Datenblatt WF2000M: AC-Eingang max. 5,4 A
INVERTER_W = 800
# Venus (dbus-acsystem) gibt Dynamic ESS nur fuer Multi RS ab einer bestimmten
# Firmware frei (ProductId 0xA440-0xA47F, Firmware >= 0x11713). Darum meldet das
# multi-Geraet diese Kennung; die echte Solakon-Firmware steht in /Solakon/FirmwareText.
# Achtung: in VRM kein Firmware-Update fuer dieses Geraet ausloesen (kommt nie an,
# das Solakon haengt nicht am Victron-Bus).
MULTI_RS_PRODUCT_ID = 0xA442
MULTI_RS_FIRMWARE = 0x11713
# Seriennummern: je Geraet eindeutig und stabil, aus der Solakon-Seriennummer
# abgeleitet (keine erfundenen Victron-Nummern, die einem echten Geraet gehoeren koennten)
SERIAL_SUFFIX = {"battery": "-BAT", "solarcharger.solakon_pv1": "-PV1",
                 "solarcharger.solakon_pv2": "-PV2", "solarcharger.solakon_pv3": "-PV3",
                 "solarcharger.solakon_pv4": "-PV4", "temperature.solakon_wr": "-TWR",
                 "temperature.solakon_gehaeuse": "-TGH", "switch.solakon_ess": "-ESS",
                 "grid.solakon_meter": "-NZ"}
# /Settings/Solakon/GridMeter: 0 = Solakon-Zaehler (Modbus) meldet sich als Netzzaehler an,
# 2 = Venus OS nimmt einen eigenen Netzzaehler (PowerTracker, Victron-Zaehler, Shelly ...).
# 1 war bis 24.10. "PowerTracker" und wird zu 2 (der PowerTracker ist jetzt ein normaler Zaehler).
METER_SOLAKON, METER_VENUS = 0, 2
# Geraete-Instanzen vergibt Venus (localsettings) ueber
# /Settings/Devices/solakon_<schluessel>/ClassAndVrmInstance wie bei Victron-Treibern:
# ist die gewuenschte Nummer belegt (z. B. 280 von einem SmartSolar, 24/25 von den
# Temperatureingaengen eines Cerbo), gibt es die naechste freie. Aendern im Menue
# "VRM-Geraeteinstanzen"; der Treiber startet dann neu.
# Netzzaehler: Venus nimmt von mehreren den mit der kleinsten Instanz; andere Zaehler
# haben ab 30 aufwaerts, damit hat der Solakon-Zaehler Vorrang, wenn er gewaehlt ist.
DEVICES = {"battery": ("battery", 512), "multi": ("multi", 276),
           "pv1": ("solarcharger", 280), "pv2": ("solarcharger", 281),
           "pv3": ("solarcharger", 282), "pv4": ("solarcharger", 283),
           "temp_wr": ("temperature", 24), "temp_gehaeuse": ("temperature", 25),
           "meter": ("grid", 10), "ess": ("switch", 91)}

log = logging.getLogger("dbus-solakon")


def s16(v):
    return v - 0x10000 if v >= 0x8000 else v


def u32(r, i):
    return (r[i] << 16) | r[i + 1]


def s32(r, i):
    v = u32(r, i)
    return v - 0x100000000 if v >= 0x80000000 else v


def text(r):
    b = b"".join(x.to_bytes(2, "big") for x in r)
    return b.split(b"\0")[0].decode("ascii", "replace").strip()


def bit(v, n):
    return v is not None and (v >> n) & 1 == 1


class Modbus(object):
    def __init__(self):
        self.c = ModbusTcpClient(HOST, port=PORT, timeout=3)
        self.unit = UNIT

    def read(self, addr, count):
        for attempt in (1, 2):
            try:
                if not self.c.is_socket_open():
                    self.c.connect()
                r = self.c.read_holding_registers(addr, count, unit=UNIT)
                if not r.isError() and len(r.registers) == count:
                    return r.registers
                log.warning("Lesefehler %d+%d: %s", addr, count, r)
            except Exception as e:
                log.warning("Modbus %d+%d: %s", addr, count, e)
            self.c.close()
            time.sleep(0.2)
        return None


# Lesegruppen: (Name, Takt s, [(Adresse, Anzahl), ...]); max. 20 Register je Anfrage
GROUPS = [
    ("fast", 2, [(37609, 4), (39070, 8), (39118, 2), (39134, 6), (39237, 2),
                 (39279, 8), (46001, 7), (39201, 18), (38801, 15)]),
    ("medium", 10, [(37617, 4), (37624, 13), (39063, 7), (39141, 1), (46018, 4),
                    (49228, 2)]),
    ("slow", 60, [(39149, 4), (39601, 16), (39617, 16), (46607, 5), (46612, 8),
                  (49240, 1)]),
    ("once", 0, [(39002, 16), (39018, 16), (36001, 3), (37002, 3), (37005, 16),
                 (37032, 1)]),
]


class Registers(object):
    """Rohwerte je Adresse, mit Zeitstempel je Gruppe."""

    def __init__(self):
        self.raw = {}
        self.stamp = {}
        self.next = {}

    def poll(self, mb):
        now = time.monotonic()
        for name, period, blocks in GROUPS:
            if name in self.stamp and (period == 0 or now < self.next.get(name, 0)):
                continue
            ok = True
            for addr, count in blocks:
                r = mb.read(addr, count)
                if r is None:
                    ok = False
                    break
                for i, v in enumerate(r):
                    self.raw[addr + i] = v
            if ok:
                self.stamp[name] = now
                self.next[name] = now + period
            else:
                self.next[name] = now + min(period or 30, 5)
                if name == "fast":
                    break   # Geraet weg, Rest dieser Runde sparen

    def fresh(self, name, maxage):
        t = self.stamp.get(name)
        return t is not None and time.monotonic() - t <= maxage

    def g(self, a):
        return self.raw.get(a)

    def block(self, a, n):
        r = [self.raw.get(a + i) for i in range(n)]
        return None if None in r else r


def reject(path, value):
    log.info("Phase 2: Schreibwunsch abgewiesen %s = %r", path, value)
    return False


class State(object):
    """Persistente Zaehler fuer die Tracker-Ertraege (je Tag, 30 Tage)."""

    def __init__(self):
        self.d = {"day": None, "pv": {}}
        try:
            with open(STATE_FILE) as f:
                self.d.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.saved = time.monotonic()

    def save(self, force=False):
        if not force and time.monotonic() - self.saved < 300:
            return
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f)
        os.replace(tmp, STATE_FILE)
        self.saved = time.monotonic()

    def tracker(self, n):
        return self.d["pv"].setdefault(str(n), {"total": 0.0, "days": []})


class Solakon(object):
    def __init__(self):
        self.mb = Modbus()
        self.reg = Registers()
        self.state = State()
        self.last_t = None
        self.info_done = False
        self.sysbus = dbus.SystemBus()
        self.venus = {}
        self.venus_t = 0
        self.settings = SettingsDevice(self.sysbus, {
            "essmode": ["/Settings/Solakon/EssMode", 3, 0, 3],
            "mode": ["/Settings/Solakon/Mode", 3, 1, 4],
            "chargepower": ["/Settings/Solakon/ChargePower", 1200, 0, 1200],
            "reservechargepower": ["/Settings/Solakon/ReserveChargePower", 300, 0, 1200],
            "acinlimit": ["/Settings/Solakon/AcInCurrentLimit", AC_IN_LIMIT_A, 0.0, AC_IN_LIMIT_A],
            "gridmeter": ["/Settings/Solakon/GridMeter", METER_SOLAKON, 0, 2],
        }, None)
        self.ctl = Control(self.mb, self.settings, self.state, CONTROL_ENABLED)
        dev = {}
        for key, (cls, inst) in DEVICES.items():
            base = "/Settings/Devices/solakon_%s/" % key
            dev["inst_" + key] = [base + "ClassAndVrmInstance", "%s:%d" % (cls, inst), 0, 0]
            dev["name_" + key] = [base + "CustomName", "", 0, 0]
        self.devset = SettingsDevice(self.sysbus, dev, self.device_setting_changed)
        self.services = {}          # Schluessel -> Dienst
        self.default_name = {}      # Schluessel -> Name, wenn keiner vergeben ist

        self.batt = self.new_service("com.victronenergy.battery.solakon", "battery",
                                     T("Solakon ONE Akku", "Solakon ONE Battery"),
                                     T("Solakon Speicher", "Solakon Battery"))
        for p in ("/Soc", "/Soh", "/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
                  "/Dc/0/Temperature", "/Capacity", "/ConsumedAmphours", "/TimeToGo",
                  "/System/MinCellTemperature", "/System/MaxCellTemperature",
                  "/System/MinCellVoltage", "/System/MaxCellVoltage",
                  "/System/NrOfModulesOnline", "/System/NrOfBatteries",
                  "/History/ChargedEnergy", "/History/DischargedEnergy",
                  "/Info/MaxChargeCurrent", "/Info/MaxDischargeCurrent",
                  "/Alarms/InternalFailure", "/Alarms/HighTemperature",
                  "/Alarms/LowTemperature", "/Alarms/CellImbalance",
                  "/Solakon/BmsFault1", "/Solakon/BmsFault2", "/Solakon/BmsFault3",
                  "/Solakon/BmsFault4", "/Solakon/BmsFault5", "/Solakon/BmsFault6",
                  "/Solakon/ForceChargeFlag", "/Solakon/RemainEnergyRaw",
                  "/Solakon/FccRaw", "/Solakon/DesignEnergyRaw",
                  "/Solakon/MaxChargePower", "/Solakon/MaxDischargePower",
                  "/Solakon/BmsSerial"):
            self.batt.add_path(p, None)
        self.batt.add_path("/InstalledCapacity", None)
        self.capacity_ah = None

        self.pv = {}
        for n in range(1, 5):
            s = self.new_service("com.victronenergy.solarcharger.solakon_pv%d" % n, "pv%d" % n,
                                 "Solakon ONE MPPT %d" % n, "Solakon MPPT %d" % n)
            for p in ("/Pv/V", "/Pv/I", "/Yield/Power", "/Dc/0/Voltage", "/Dc/0/Current",
                      "/State", "/Yield/User", "/Yield/System",
                      "/History/Overall/MaxPvVoltage", "/History/Overall/DaysAvailable"):
                s.add_path(p, None)
            s.add_path("/Mode", 1)
            s.add_path("/NrOfTrackers", 1)
            s.add_path("/ErrorCode", 0)
            s.add_path("/Load/State", None)
            for d in range(30):
                for k in ("Yield", "MaxPower", "MaxPvVoltage"):
                    s.add_path("/History/Daily/%d/%s" % (d, k), None)
            self.pv[n] = s

        m = self.multi = self.new_service("com.victronenergy.multi.solakon", "multi",
                                          "Solakon ONE Hybrid",
                                          T("Solakon Wechselrichter", "Solakon Inverter"),
                                          MULTI_RS_PRODUCT_ID)
        m.add_path("/N2kSystemInstance", 0)
        m.add_path("/Devices/0/Gateway", "solakon")
        m.add_path("/Devices/0/Nad", 1)
        m.add_path("/Ac/NumberOfPhases", 1)
        m.add_path("/Ac/NumberOfAcInputs", 1)
        m.add_path("/Ac/ActiveIn/ActiveInput", 0)
        m.add_path("/Ac/In/1/Type", 1)  # Netz
        m.add_path("/Ac/In/1/CurrentLimitIsAdjustable", 1 if CONTROL_ENABLED else 0)
        m.add_path("/Ac/Out/L1/NominalInverterPower", INVERTER_W)
        m.add_path("/Capabilities/HasAcPassthroughSupport", 0)
        for p in ("/Ac/In/1/L1/P", "/Ac/In/1/L1/F", "/Ac/ActiveIn/L1/P", "/Ac/ActiveIn/P",
                  "/Ac/Out/L1/P", "/Ac/Out/P", "/Ac/Out/L1/V", "/Ac/Out/L1/I",
                  "/Ac/Out/L1/F", "/Dc/0/Voltage", "/Dc/0/Current",
                  "/Dc/0/Power", "/Soc", "/State", "/Ac/NoFeedInReason",
                  "/Ess/ActiveSocLimit", "/Ess/BatteryDischargeCapacity", "/Ess/Sustain",
                  "/Alarms/HighTemperature", "/Alarms/GridLost", "/Alarms/Overload",
                  "/Alarms/PhaseRotation",
                  "/Solakon/Alarm1", "/Solakon/Alarm2", "/Solakon/Alarm3",
                  "/Solakon/Status1", "/Solakon/Island", "/Solakon/RemoteControl",
                  "/Solakon/RemoteCountdown", "/Solakon/PowerState", "/Solakon/IdleState",
                  "/Solakon/MinSocPhysical", "/Solakon/MaxSoc", "/Solakon/EpsOutput",
                  "/Solakon/ExportPowerLimit", "/Solakon/CloudStatus",
                  "/Solakon/ReactivePower", "/Solakon/PowerFactor"):
            m.add_path(p, None)
        # Multi-RS-Schnittstelle (Pflichtpfade fuer dbus-acsystem und Steuerpfade)
        ctl = self.ctl
        g = self.guarded
        m.add_path("/Mode", int(self.settings["mode"]), writeable=True,
                   onchangecallback=g(lambda p, v: ctl.set_mode(v)))
        m.add_path("/Settings/Ess/Mode", int(self.settings["essmode"]), writeable=True,
                   onchangecallback=g(lambda p, v: ctl.set_ess_mode(v)))
        m.add_path("/Settings/Ess/MinimumSocLimit", None, writeable=True,
                   onchangecallback=g(lambda p, v: self.set_soc_limit(46611, v)))
        for p, v in (("/Ess/AcPowerSetpoint", 0), ("/Ess/InverterPowerSetpoint", 0),
                     ("/Ess/UseInverterPowerSetpoint", 0), ("/Ess/DisableFeedIn", 0),
                     ("/Ess/DisableCharge", 0), ("/Ess/DisableDischarge", 0),
                     ("/Ess/BatteryDischargeSetpoint", None)):
            m.add_path(p, v, writeable=True, onchangecallback=g(ctl.set_request))
        # AC-Eingangsgrenze: begrenzt Netzladen + Notstromverbraucher (Software-Grenze im
        # Regler, kein Geraeteregister); max. 5,4 A laut Datenblatt
        m.add_path("/Ac/In/1/CurrentLimit", float(self.settings["acinlimit"]), writeable=True,
                   onchangecallback=g(lambda p, v: self.set_ac_limit(v)))
        # Nicht unterstuetzt: PV-Abschaltung (49210 ungeklaert), Durchleitung
        m.add_path("/Pv/Disable", 0, writeable=True, onchangecallback=g(reject))
        m.add_path("/Ac/Control/IgnoreAcIn1", 0, writeable=True, onchangecallback=g(reject))
        m.add_path("/Capabilities/HasDynamicEssSupport", 1 if CONTROL_ENABLED else 0)
        # Solakon-eigene Einstellungen
        # Notstrom-Ausgang (46613): 0 aus, 2 EPS, 3 USV; 1 ist im Protokoll nicht belegt
        m.add_path("/Solakon/SetEpsOutput", None, writeable=True,
                   onchangecallback=g(lambda p, v: self.set_eps(v)))
        m.add_path("/Solakon/EpsOutputChanged", 0)
        m.add_path("/Solakon/SetMaxSoc", None, writeable=True,
                   onchangecallback=g(lambda p, v: self.set_soc_limit(46610, v)))
        m.add_path("/Solakon/SetMinSocPhysical", None, writeable=True,
                   onchangecallback=g(lambda p, v: self.set_soc_limit(46609, v)))
        for key, path in (("chargepower", "/Solakon/ChargePower"),
                          ("reservechargepower", "/Solakon/ReserveChargePower")):
            m.add_path(path, int(self.settings[key]), writeable=True,
                       onchangecallback=g(lambda p, v, k=key: self.set_power(k, v)))
        m.add_path("/Solakon/ControlEnabled", 1 if CONTROL_ENABLED else 0)
        m.add_path("/Solakon/ControlText", None)
        m.add_path("/Solakon/ControlError", None)

        self.temp = {}
        for key, name, custom in (
                ("wr", T("Solakon Wechselrichter", "Solakon Inverter"),
                 T("Solakon Temp. Wechselrichter", "Solakon Temp. Inverter")),
                ("gehaeuse", T("Solakon Gehäuse", "Solakon Enclosure"),
                 T("Solakon Temp. Gehäuse", "Solakon Temp. Enclosure"))):
            s = self.new_service("com.victronenergy.temperature.solakon_" + key,
                                 "temp_" + key, name, custom)
            s.add_path("/Temperature", None)
            s.add_path("/TemperatureType", 2)   # generisch
            s.add_path("/Status", None)
            self.temp[key] = s

        # Netzzaehler aus dem Solakon (38801 ff. "Meter1/CT1"). Das Solakon gibt nur die
        # Summenleistung seines Zaehlers weiter (Phasen, Spannungen, Zaehlerstaende = 0),
        # darum eine virtuelle Phase wie beim PowerTracker. Angemeldet nur, wenn er in
        # /Settings/Solakon/GridMeter gewaehlt ist (Venus nimmt sonst den Netzzaehler
        # mit der kleinsten Instanz, die Wahl waere Zufall).
        m = self.meter = self.new_service("com.victronenergy.grid.solakon_meter", "meter",
                                          T("Solakon ONE Netzz\u00e4hler",
                                            "Solakon ONE Grid Meter"),
                                          T("Solakon Netzz\u00e4hler (Modbus)",
                                            "Solakon Grid Meter (Modbus)"))
        for p, v in (("/Role", "grid"), ("/NrOfPhases", 1), ("/DeviceType", None),
                     ("/Position", None)):
            m.add_path(p, v)
        for p in ("/Ac/Power", "/Ac/L1/Power", "/Ac/Energy/Forward", "/Ac/Energy/Reverse"):
            m.add_path(p, None)

        self.acsystem_named = False
        if int(self.settings["gridmeter"]) not in (METER_SOLAKON, METER_VENUS):
            self.settings["gridmeter"] = METER_VENUS
        self.all = [self.batt, self.multi] + list(self.pv.values()) + list(self.temp.values()) \
            + [self.meter]
        # Einstell-Geraet: nicht in self.all, bleibt auch ohne Solakon-Verbindung bedienbar
        self.ess = EssSettings(self, self.new_service)
        for s in self.all + [self.ess.s]:
            if s is not self.meter:
                s.register()
        self.sync_meter()
        GLib.timeout_add(2000, self.tick)
        GLib.timeout_add(10000, self.ess.refresh)
        GLib.timeout_add(30000, self.check_language)
        self.ess.refresh()
        log.info("Treiber %s gestartet", VERSION)

    # ---------------------------------------------------------------
    def new_service(self, name, key, product, custom, product_id=0xFFFF):
        """Dienst anlegen; Instanz und eigener Name aus /Settings/Devices/solakon_<key>/."""
        s = VeDbusService(name, bus=dbus.bus.BusConnection(dbus.bus.BusConnection.TYPE_SYSTEM),
                          register=False)
        s.add_path("/Mgmt/ProcessName", __file__)
        s.add_path("/Mgmt/ProcessVersion", VERSION)
        s.add_path("/Mgmt/Connection", "Modbus-TCP Solakon ONE")
        s.add_path("/DeviceInstance", int(str(self.devset["inst_" + key]).split(":")[1]))
        s.add_path("/ProductId", product_id)
        s.add_path("/ProductName", product)
        s.add_path("/CustomName", str(self.devset["name_" + key]) or custom, writeable=True,
                   onchangecallback=lambda p, v, k=key: self.set_custom_name(k, v))
        s.add_path("/FirmwareVersion", None)
        s.add_path("/Solakon/FirmwareText", None)
        s.add_path("/Serial", None)
        s.add_path("/Connected", 0)
        self.services[key] = s
        self.default_name[key] = custom
        return s

    def set_custom_name(self, key, value):
        """Name aus der Oberflaeche; leer oder Standardname = Standard (folgt der Sprache)."""
        v = str(value).strip()
        self.devset["name_" + key] = "" if v == self.default_name[key] else v
        if not v:
            # Anzeige auf den Standardnamen zuruecksetzen, nachdem der Schreibzugriff durch ist
            GLib.idle_add(lambda: self.set_all(self.services[key], {
                "/CustomName": self.default_name[key]}) and False)
        return True

    def device_setting_changed(self, setting, old, new):
        if setting.startswith("inst_") and old != new:
            # Neue Instanz (Menue "VRM-Geraeteinstanzen"): Dienste neu anlegen = Neustart
            log.info("Instanz %s: %s -> %s, Neustart", setting[5:], old, new)
            self.restart()
        elif setting.startswith("name_"):
            key = setting[5:]
            if key in self.services:
                self.set_all(self.services[key], {"/CustomName": str(new) or
                                                  self.default_name[key]})

    def restart(self):
        """Sauber beenden (gibt die Fernsteuerung frei), daemontools startet neu."""
        GLib.timeout_add(500, lambda: os.kill(os.getpid(), signal.SIGTERM) and False)

    def check_language(self):
        lang = i18n.read_language(self.sysbus)
        if lang and lang != i18n.LANG:
            log.info("Sprache %s -> %s, Neustart", i18n.LANG, lang)
            self.restart()
            return False
        return True

    def guarded(self, fn):
        """Schreib-Callback fuer multi-Pfade. Wird ein Wert abgewiesen, meldet der
        Treiber den gueltigen Wert neu, damit dbus-acsystem (das den Wert schon
        uebernommen hat) und die Oberflaeche wieder den echten Stand zeigen."""
        def cb(path, value):
            ok = bool(fn(path, value))
            if not ok:
                GLib.timeout_add(300, self.resync, path)
            return ok
        return cb

    def resync(self, path):
        v = self.multi[path]
        with self.multi as w:
            w[path] = None
        with self.multi as w:
            w[path] = v
        return False

    def set_soc_limit(self, addr, v):
        """46609 Min-SoC, 46610 Max-SoC, 46611 Netzreserve: pruefen, schreiben, zuruecklesen."""
        if not CONTROL_ENABLED:
            return reject(addr, v)
        try:
            v = int(round(float(v)))
        except (TypeError, ValueError):
            return False
        R = self.reg
        lim = {a: R.g(a) for a in (46609, 46610, 46611)}
        lim[addr] = v
        if not (10 <= v <= 100) or None in lim.values() \
                or max(lim[46609], lim[46611]) >= lim[46610]:
            log.info("Ladegrenze %d = %r abgewiesen (%s)", addr, v, lim)
            return False
        old = R.g(addr)
        if self.ctl.write_checked(addr, v):
            R.raw[addr] = v
            log.info("Ladegrenze %d: %s -> %d", addr, old, v)
            return True
        log.warning("Ladegrenze %d nicht bestaetigt, zurueck auf %s", addr, old)
        self.ctl.write_checked(addr, old)
        return False

    def set_eps(self, v):
        if not CONTROL_ENABLED:
            return reject(46613, v)
        try:
            v = int(v)
        except (TypeError, ValueError):
            return False
        if v not in (0, 2, 3):
            log.info("EPS-Ausgang %r abgewiesen (erlaubt 0, 2, 3)", v)
            return False
        old = self.reg.g(46613)
        if self.ctl.write_checked(46613, v):
            self.reg.raw[46613] = self.eps_known = v
            log.info("EPS-Ausgang: %s -> %d", old, v)
            return True
        log.warning("EPS-Ausgang %d nicht bestaetigt", v)
        return False

    def set_ac_limit(self, v):
        if not CONTROL_ENABLED:
            return reject("/Ac/In/1/CurrentLimit", v)
        try:
            v = round(float(v), 1)
        except (TypeError, ValueError):
            return False
        if not 0 <= v <= AC_IN_LIMIT_A:
            log.info("AC-Eingangsgrenze %r abgewiesen (0 bis %.1f A)", v, AC_IN_LIMIT_A)
            return False
        self.settings["acinlimit"] = v
        log.info("AC-Eingangsgrenze -> %.1f A", v)
        return True

    def name_acsystem(self):
        """dbus-acsystem nennt das AC-System "AC system (0)". Einmalig einen
        sprechenden Namen setzen, aber nur solange niemand selbst einen vergeben hat."""
        if self.acsystem_named:
            return
        try:
            o = self.sysbus.get_object("com.victronenergy.settings",
                                       "/Settings/AcSystem/0/CustomName")
            want = T("Solakon AC-System", "Solakon AC system")
            cur = str(o.GetValue(dbus_interface="com.victronenergy.BusItem"))
            # nur leer oder unser Standardname (andere Sprache); eigener Name bleibt
            if cur in ("", "Solakon AC-System", "Solakon AC system") and cur != want:
                o.SetValue(want, dbus_interface="com.victronenergy.BusItem")
                log.info("AC-System benannt: %s", want)
            self.acsystem_named = True
        except dbus.exceptions.DBusException:
            pass        # dbus-acsystem hat die Einstellung noch nicht angelegt

    def sync_meter(self):
        """Solakon-Netzzaehler je nach Auswahl in Venus an- oder abmelden."""
        want = int(self.settings["gridmeter"]) == METER_SOLAKON
        m = self.meter
        if want and m._dbusname is None:
            m.register()
            log.info("Netzzaehler Solakon (Modbus) angemeldet")
        elif not want and m._dbusname is not None:
            m._dbusname.__del__()       # gibt den Dienstnamen frei (vedbus macht es genauso)
            m._dbusname = None
            log.info("Netzzaehler Solakon (Modbus) abgemeldet")

    def set_gridmeter(self, v):
        if v not in (METER_SOLAKON, METER_VENUS):
            return False
        self.settings["gridmeter"] = v
        log.info("Netzzaehler-Quelle -> %d", v)
        self.sync_meter()
        return True

    def set_power(self, key, v):
        if not CONTROL_ENABLED:
            return reject(key, v)
        try:
            v = int(v)
        except (TypeError, ValueError):
            return False
        if not 0 <= v <= 1200:
            return False
        self.settings[key] = v
        return True

    def read_venus(self):
        """Netzleistung und ESS-Grenzen aus Venus (alle 2 s bzw. 10 s)."""
        def get(service, path):
            try:
                v = self.sysbus.get_object(service, path).GetValue(
                    dbus_interface="com.victronenergy.BusItem")
            except dbus.exceptions.DBusException:
                return None
            return None if isinstance(v, dbus.Array) else float(v)
        # Summe aller Phasen: unsere Zaehler melden nur L1, ein externer 3-Phasen-Zaehler alle
        # Ohne Netzzaehler nimmt Venus den AC-Eingang des Wechselrichters als "Netz"
        # (/Ac/Grid/DeviceType ist dann leer) - das waere unsere eigene Abgabe.
        ph = []
        if get("com.victronenergy.system", "/Ac/Grid/DeviceType") is not None:
            ph = [get("com.victronenergy.system", "/Ac/Grid/L%d/Power" % n) for n in (1, 2, 3)]
            ph = [v for v in ph if v is not None]
        self.venus["grid"] = sum(ph) if ph else None
        if time.monotonic() - self.venus_t > 10:
            s = "com.victronenergy.settings"
            self.venus["max_feedin_w"] = get(s, "/Settings/CGwacs/MaxFeedInPower")
            self.venus["prevent_feedback"] = get(s, "/Settings/CGwacs/PreventFeedback")
            # "DC-gekoppelte PV: Ueberschuss einspeisen"
            self.venus["pv_feedin"] = get(s, "/Settings/CGwacs/OvervoltageFeedIn")
            self.venus["max_discharge_w"] = get(s, "/Settings/CGwacs/MaxDischargePower")
            self.venus["max_charge_w"] = get(s, "/Settings/CGwacs/MaxChargePower")
            self.venus["grid_setpoint"] = get(s, "/Settings/CGwacs/AcPowerSetPoint")
            # Peak Shaving (ESS-Menue): Grenzen in A, -1 = aus; gelten hier fuer die
            # Summe aller Phasen, weil der Netzzaehler nur die Summe liefert
            self.venus["peak_import_a"] = get(s, "/Settings/CGwacs/AcInputLimit")
            self.venus["peak_export_a"] = get(s, "/Settings/CGwacs/AcExportLimit")
            self.venus["always_peak"] = get(s, "/Settings/CGwacs/AlwaysPeakShave")
            # Dynamic ESS rechnet mit diesen Grenzen; -1 (nicht eingetragen) laesst
            # die Berechnung in Venus abbrechen
            missing = []
            if (get(s, "/Settings/DynamicEss/Mode") or 0) > 0:
                for key, name in (("BatteryChargeLimit", "Laden"),
                                  ("BatteryDischargeLimit", "Entladen"),
                                  ("GridImportLimit", "Netzbezug"),
                                  ("GridExportLimit", "Einspeisung")):
                    v = get(s, "/Settings/DynamicEss/" + key)
                    if v is None or v < 0:
                        missing.append(name)
            self.venus["dess_missing"] = ", ".join(missing) or None
            self.venus_t = time.monotonic()

    def control(self, soc, ac, pv):
        R = self.reg
        self.read_venus()
        slow = R.fresh("slow", 300)
        eps = self.multi["/Ac/Out/L1/P"]
        m = dict(self.venus, soc=soc, ac=ac, pv=pv, eps=eps,
                 ac_in_limit_a=float(self.settings["acinlimit"]),
                 minsoc=R.g(46609) if slow else None,
                 ongrid=R.g(46611) if slow else None,
                 maxsoc=R.g(46610) if slow else None)
        self.ctl.run(m)
        with self.multi as w:
            w["/Mode"] = int(self.settings["mode"])
            w["/Settings/Ess/Mode"] = int(self.settings["essmode"])
            if m.get("active_soc_limit") is not None:
                w["/Ess/ActiveSocLimit"] = m["active_soc_limit"]
            w["/Solakon/ControlText"] = self.ctl.text
            w["/Solakon/ControlError"] = self.ctl.error
            w["/Solakon/ChargePower"] = int(self.settings["chargepower"])
            w["/Solakon/ReserveChargePower"] = int(self.settings["reservechargepower"])
            w["/Ac/In/1/CurrentLimit"] = float(self.settings["acinlimit"])

    def tick(self):
        try:
            self.reg.poll(self.mb)
            self.sync_meter()
            self.name_acsystem()
            self.publish()
            self.state.save()
        except Exception:
            log.exception("Fehler im Zyklus")
        return True

    def set_all(self, s, values):
        with s as w:
            for k, v in values.items():
                w[k] = v

    def invalidate(self):
        for s in self.all:
            with s as w:
                for p in list(s._dbusobjects):
                    if p.startswith("/Mgmt") or p in ("/DeviceInstance", "/ProductId",
                            "/ProductName", "/CustomName", "/FirmwareVersion", "/Serial",
                            "/InstalledCapacity", "/N2kSystemInstance", "/Devices/0/Gateway",
                            "/Devices/0/Nad", "/Ac/NumberOfPhases", "/Ac/NumberOfAcInputs",
                            "/Mode", "/Settings/Ess/Mode", "/Ac/In/1/CurrentLimit",
                            "/TemperatureType", "/NrOfTrackers", "/Role",
                            "/NrOfPhases") or "/Ess/" in p \
                            or p.startswith("/History/Daily") or p.startswith("/Yield/User"):
                        continue
                    w[p] = None
                w["/Connected"] = 0

    def publish(self):
        R = self.reg
        if not R.fresh("fast", STALE_S):
            self.invalidate()
            return
        self.info()

        bv = R.g(37609) / 10.0
        braw = s16(R.g(37610)) / 10.0
        soc = R.g(37612)
        bp = s32([R.g(39237), R.g(39238)], 0)       # + = laden
        bi = abs(braw) if bp > 0 else (-abs(braw) if bp < 0 else 0.0)
        pv_total = s32([R.g(39118), R.g(39119)], 0)
        ac = s32([R.g(39134), R.g(39135)], 0)       # + = Abgabe
        q = s32([R.g(39136), R.g(39137)], 0)
        pf = s16(R.g(39138)) / 1000.0
        freq = s16(R.g(39139)) / 100.0
        remote = R.g(46001)
        countdown = R.g(46007)
        med = R.fresh("medium", 60)
        slow = R.fresh("slow", 300)

        # --- Netzzaehler (38814: Summe, 0,1 W, + = Bezug; 38801: 1 = Zaehler verbunden)
        mp = s32([R.g(38814), R.g(38815)], 0) / 10.0 if R.g(38801) == 1 else None
        self.set_all(self.meter, {"/Connected": 1 if mp is not None else 0,
                                  "/Ac/Power": mp, "/Ac/L1/Power": mp})

        # --- Akku
        b = {"/Connected": 1, "/Soc": soc, "/Dc/0/Voltage": bv, "/Dc/0/Current": round(bi, 1),
             "/Dc/0/Power": bp}
        design = R.g(37635) if med else None
        if design:
            self.capacity_ah = round(design * 10.0 / NOMINAL_V, 1)
            b["/InstalledCapacity"] = self.capacity_ah
        cap = self.capacity_ah
        if cap:
            b["/Capacity"] = round(cap * soc / 100.0, 1)
            b["/ConsumedAmphours"] = round(-cap * (100 - soc) / 100.0, 1)
        minsoc = R.g(46609) if slow else None
        if bp < -5 and minsoc is not None and soc > minsoc and cap:
            wh = (soc - minsoc) / 100.0 * cap * NOMINAL_V
            b["/TimeToGo"] = int(wh / -bp * 3600)
        else:
            b["/TimeToGo"] = None
        if med:
            tmax, tmin = s16(R.g(37617)) / 10.0, s16(R.g(37618)) / 10.0
            vmax, vmin = R.g(37619) / 1000.0, R.g(37620) / 1000.0
            faults = [R.g(a) for a in range(37626, 37632)]
            spread = vmax - vmin
            b.update({
                "/Soh": R.g(37624), "/Dc/0/Temperature": tmax,
                "/System/MaxCellTemperature": tmax, "/System/MinCellTemperature": tmin,
                "/System/MaxCellVoltage": vmax, "/System/MinCellVoltage": vmin,
                "/Alarms/InternalFailure": 2 if any(faults) else 0,
                # Grenzen aus dem Datenblatt (Betrieb -20...55 C); 0 C Warnung,
                # weil LiFePO4 darunter nicht geladen werden darf
                "/Alarms/HighTemperature": 2 if tmax > 55 else (1 if tmax > 50 else 0),
                "/Alarms/LowTemperature": 2 if tmin < -20 else (1 if tmin < 0 else 0),
                "/Alarms/CellImbalance": 2 if spread > 0.2 else (1 if spread > 0.1 else 0),
                "/Solakon/ForceChargeFlag": R.g(37636),
                "/Solakon/RemainEnergyRaw": R.g(37632), "/Solakon/FccRaw": R.g(37633),
                "/Solakon/DesignEnergyRaw": R.g(37635),
                "/Solakon/MaxChargePower": s32([R.g(46018), R.g(46019)], 0),
                "/Solakon/MaxDischargePower": s32([R.g(46020), R.g(46021)], 0),
            })
            for i, f in enumerate(faults):
                b["/Solakon/BmsFault%d" % (i + 1)] = f
        if slow:
            b.update({"/History/ChargedEnergy": u32([R.g(39605), R.g(39606)], 0) / 100.0,
                      "/History/DischargedEnergy": u32([R.g(39609), R.g(39610)], 0) / 100.0,
                      "/Info/MaxChargeCurrent": s16(R.g(46607)) / 10.0,
                      "/Info/MaxDischargeCurrent": s16(R.g(46608)) / 10.0})
        self.set_all(self.batt, b)

        # --- PV, je Tracker ein Geraet
        status1 = R.g(39063) if med else None
        self.roll_day()
        now = time.monotonic()
        dt = 0 if self.last_t is None else min(now - self.last_t, 10)
        self.last_t = now
        pv_sum = 0
        for n in range(1, 5):
            v = s16(R.g(39070 + 2 * (n - 1))) / 10.0
            i = s16(R.g(39071 + 2 * (n - 1))) / 100.0
            p = s32([R.g(39279 + 2 * (n - 1)), R.g(39280 + 2 * (n - 1))], 0)
            pv_sum += p
            t = self.state.tracker(n)
            t["total"] += max(p, 0) * dt / 3600000.0
            today = t["days"][0]
            today["Yield"] += max(p, 0) * dt / 3600000.0
            today["MaxPower"] = max(today["MaxPower"], p)
            today["MaxPvVoltage"] = max(today["MaxPvVoltage"], v)
            t["vmax"] = max(t.get("vmax", 0), v)
            if bit(status1, 6):
                state = 2       # Fehler
            elif p > 0:
                state = 252     # Externe Steuerung (Solakon-Firmware regelt)
            else:
                state = 0       # aus
            d = {"/Connected": 1, "/Pv/V": v, "/Pv/I": i, "/Yield/Power": p,
                 "/Dc/0/Voltage": bv, "/Dc/0/Current": round(p / bv, 2) if bv > 0 else None,
                 "/State": state, "/Yield/User": round(t["total"], 3),
                 "/Yield/System": round(t["total"], 3),
                 "/History/Overall/MaxPvVoltage": t["vmax"],
                 "/History/Overall/DaysAvailable": len(t["days"])}
            for k, day in enumerate(t["days"][:30]):
                d["/History/Daily/%d/Yield" % k] = round(day["Yield"], 3)
                d["/History/Daily/%d/MaxPower" % k] = day["MaxPower"]
                d["/History/Daily/%d/MaxPvVoltage" % k] = day["MaxPvVoltage"]
            self.set_all(self.pv[n], d)

        # --- Hybrid-WR (RS-Schnittstelle)
        mp = {"/Connected": 1, "/Ac/In/1/L1/P": -ac, "/Ac/ActiveIn/L1/P": -ac,
              "/Ac/ActiveIn/P": -ac, "/Ac/In/1/L1/F": freq,
              "/Dc/0/Voltage": bv, "/Soc": soc,
              # Bilanz, keine eigene Messung: DC-Seite des WR = Akku - PV
              "/Dc/0/Power": bp - pv_sum,
              "/Dc/0/Current": round((bp - pv_sum) / bv, 1) if bv > 0 else None,
              "/Solakon/RemoteControl": remote,
              "/Solakon/RemoteCountdown": countdown, "/Solakon/ReactivePower": q,
              "/Solakon/PowerFactor": pf}
        if med:
            a1, a2, a3 = R.g(39067), R.g(39068), R.g(39069)
            power_state = R.g(49228)
            if power_state == 0:
                state = 0
            elif bit(status1, 6):
                state = 2
            elif remote == 0:
                state = 252
            elif ac > 5:
                state = 9
            elif ac < -5:
                state = 3
            else:
                state = None
            mp.update({"/State": state, "/Solakon/Status1": status1,
                       "/Solakon/Island": R.g(39065),
                       "/Solakon/Alarm1": a1, "/Solakon/Alarm2": a2, "/Solakon/Alarm3": a3,
                       "/Alarms/HighTemperature": 2 if bit(a2, 3) else 0,
                       "/Alarms/GridLost": 2 if bit(a1, 7) or bit(R.g(39065), 0) else 0,
                       "/Alarms/Overload": 2 if bit(a1, 14) or bit(a2, 14) else 0,
                       "/Alarms/PhaseRotation": 0,
                       "/Solakon/PowerState": power_state, "/Solakon/IdleState": R.g(49229)})
            self.set_all(self.temp["wr"], {"/Connected": 1, "/Status": 0,
                                           "/Temperature": s16(R.g(39141)) / 10.0})
            self.set_all(self.temp["gehaeuse"], {"/Connected": 1, "/Status": 0,
                                                 "/Temperature": s16(R.g(37611)) / 10.0})
        # Notstrom-Ausgang als AC-Ausgang (39201 ff.), nur wenn er eingeschaltet ist
        eps = R.g(46613) if slow else None
        if eps in (2, 3):
            mp.update({"/Ac/Out/L1/V": R.g(39201) / 10.0,
                       "/Ac/Out/L1/I": s32([R.g(39204), R.g(39205)], 0) / 1000.0,
                       "/Ac/Out/L1/P": s32([R.g(39210), R.g(39211)], 0),
                       "/Ac/Out/L1/F": s16(R.g(39218)) / 100.0,
                       "/Ac/Out/P": s32([R.g(39216), R.g(39217)], 0)})
        else:
            mp.update({"/Ac/Out/L1/V": None, "/Ac/Out/L1/I": None, "/Ac/Out/L1/F": None,
                       "/Ac/Out/L1/P": 0 if eps == 0 else None,
                       "/Ac/Out/P": 0 if eps == 0 else None})
        if eps is not None:
            if getattr(self, "eps_known", None) is None:
                self.eps_known = eps
            elif eps != self.eps_known:
                log.warning("EPS-Ausgang von aussen geaendert: %s -> %s", self.eps_known, eps)
                self.eps_known = eps
                mp["/Solakon/EpsOutputChanged"] = 1
        if slow:
            ongrid = R.g(46611)
            mp.update({"/Solakon/MinSocPhysical": R.g(46609), "/Solakon/MaxSoc": R.g(46610),
                       "/Settings/Ess/MinimumSocLimit": ongrid,
                       "/Ess/ActiveSocLimit": max(ongrid, R.g(46609)),
                       "/Solakon/EpsOutput": R.g(46613),
                       "/Solakon/SetEpsOutput": R.g(46613),
                       "/Solakon/ExportPowerLimit": s32([R.g(46616), R.g(46617)], 0),
                       "/Solakon/CloudStatus": R.g(49240),
                       "/Solakon/SetMaxSoc": R.g(46610),
                       "/Solakon/SetMinSocPhysical": R.g(46609)})
        self.set_all(self.multi, mp)
        self.control(soc, ac, pv_sum)

    def roll_day(self):
        today = time.strftime("%Y-%m-%d")
        if self.state.d.get("day") == today:
            return
        for n in range(1, 5):
            t = self.state.tracker(n)
            t["days"].insert(0, {"Yield": 0.0, "MaxPower": 0, "MaxPvVoltage": 0.0})
            del t["days"][30:]
        self.state.d["day"] = today
        self.state.save(force=True)

    def info(self):
        if self.info_done or not self.reg.fresh("once", 1e9):
            return
        R = self.reg
        model = text(R.block(39002, 16)) or "Solakon ONE"
        sn = text(R.block(39018, 16))
        bms_sn = text(R.block(37005, 16))
        # Venus erwartet /FirmwareVersion als Zahl (DVCC wertet Bits aus); Klartext getrennt
        fw = R.g(36001)
        fwtext = "%d/%d/%d" % (R.g(36001), R.g(36002), R.g(36003))
        modules = R.g(37032)
        for s in self.all + [self.ess.s]:
            short = s.name.split("com.victronenergy.", 1)[1]
            suffix = SERIAL_SUFFIX.get(short.split(".")[0] if short.startswith("battery") else short, "")
            with s as w:
                w["/Serial"] = sn + suffix
                w["/FirmwareVersion"] = MULTI_RS_FIRMWARE if s is self.multi else fw
                w["/Solakon/FirmwareText"] = fwtext
        with self.multi as w:
            w["/ProductName"] = "%s Hybrid" % model
        with self.batt as w:
            # 37032: Anzahl Akkumodule laut BMS (passt zur Nennenergie 37635: 3 x 2,11 kWh)
            w["/System/NrOfModulesOnline"] = modules
            w["/System/NrOfBatteries"] = 1
        with self.batt as w:
            w["/Solakon/BmsSerial"] = bms_sn
        log.info("Geraet %s SN %s FW %s, BMS %s, Module %s", model, sn, fwtext, bms_sn,
                 modules)
        self.info_done = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    i18n.set_language(i18n.read_language(dbus.SystemBus()) or "de")
    drv = Solakon()
    loop = GLib.MainLoop()

    def stop(*_):
        # Fernsteuerung sofort abgeben, nicht erst nach dem Geraete-Timeout
        drv.ctl.release()
        loop.quit()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, stop)
    try:
        loop.run()
    finally:
        drv.state.save(force=True)


if __name__ == "__main__":
    main()
