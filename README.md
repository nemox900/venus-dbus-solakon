# Solakon ONE für Venus OS (Mod)

Bindet einen Solakon ONE (WF2000M) und den Solakon PowerTracker IR als native Venus-OS-Geräte ein. Läuft auf jedem GX-Gerät (Cerbo GX, Ekrano, Raspberry Pi, VM) mit Venus OS 3.85, ohne Änderungen am Victron-Code.

## Geräte

| D-Bus-Dienst | Instanz | Inhalt |
|---|---|---|
| `com.victronenergy.battery.solakon` | 512 | Akku/BMS: SoC, SoH, Spannung, Strom, Leistung, Zelltemperatur und Zellspannung min/max, Module, Fehler |
| `com.victronenergy.solarcharger.solakon_pv1` … `pv4` | 280–283 | je MPPT-Tracker ein Gerät: Spannung, Strom, Leistung, Tageserträge (30 Tage) |
| `com.victronenergy.multi.solakon` | 276 | Hybrid-Wechselrichter mit Multi-RS-Schnittstelle; AC-Ausgang = Notstrom-Ausgang (EPS, 39201 ff., nur wenn eingeschaltet). Venus' `dbus-acsystem` macht daraus das AC-System (ESS, Zeitpläne, DVCC, Dynamic ESS) |
| `com.victronenergy.temperature.solakon_wr` / `_gehaeuse` | 24 / 25 | Wechselrichter- und Gehäusetemperatur |
| `com.victronenergy.grid.solakon_powertracker` | 30 | Netzzähler (saldierte Summe, eine virtuelle Phase) |

`dbus-solakon.py` ist der einzige Modbus-Master. Das Solakon verträgt keine parallelen Zugriffe; deshalb darf kein anderes Programm gleichzeitig per Modbus lesen. `dbus-powertracker.py` ist ein eigener Prozess (HTTP).

## Installation

Voraussetzung: Venus OS 3.x (getestet 3.85) mit Root-Zugang per SSH. Alles liegt unter `/data/dbus-solakon` (übersteht Firmware-Updates), `/data/rc.local` hängt die Dienste bei jedem Start wieder ein.

**Vom PC aus** (Git-Stand, baut das Archiv und installiert per SSH):

```
tools/deploy.sh <ip-des-gx-geraets> [ssh-port]
```

**Von Hand** mit dem Release-Archiv (`tools/make-release.sh` → `dist/dbus-solakon-<version>.tar.gz`):

```
scp dbus-solakon-0.5.1.tar.gz root@<gx>:/tmp/
ssh root@<gx>
tar xzf /tmp/dbus-solakon-0.5.1.tar.gz -C /tmp && sh /tmp/dbus-solakon/install.sh
vi /data/dbus-solakon/config.ini                 # IP-Adressen, [control] enabled
svc -t /service/dbus-solakon /service/dbus-powertracker
```

`install.sh` prüft die Voraussetzungen (Venus, velib_python, dbus-acsystem, pymodbus), kopiert die Dateien und startet die Dienste neu. Ein Update geht genauso; `config.ini`, `state.json` und die Einstellungen bleiben dabei erhalten.

**Entfernen:**

| Befehl | Wirkung |
|---|---|
| `sh /data/dbus-solakon/uninstall.sh` | Dienste stoppen (der Treiber gibt die Fernsteuerung frei, die App steuert wieder) und aus dem Start austragen. Dateien und Einstellungen bleiben, `install.sh` aktiviert alles wieder |
| `sh /data/dbus-solakon/uninstall.sh --alles` | zusätzlich `/data/dbus-solakon`, Logs und `/Settings/Solakon/*` löschen |

Die Einstellungen im Solakon selbst (Ladegrenzen, EPS) bleiben so, wie sie zuletzt gesetzt waren.

## Dateien

| Datei | Zweck |
|---|---|
| `config.default.ini` | Standardwerte, wird bei Updates ersetzt |
| `config.ini` | eigene Einstellungen (wird von `install.sh` einmalig angelegt) |
| `state.json` | Tracker-Erträge, wird vom Treiber geschrieben |
| `install.sh`, `uninstall.sh`, `start.sh` | Installation, Entfernen, Einhängen beim Boot |
| `tools/` | nur im Git: Release-Archiv bauen, per SSH verteilen |
| `service-*/` | daemontools-Dienste; Logs unter `/var/log/dbus-solakon/current` und `/var/log/dbus-powertracker/current` |

## Steuerung

Eingeschaltet mit `[control] enabled = 1` in `config.ini` (Standard 0 = nur lesen, Hersteller-App steuert). Danach den Dienst neu starten (`svc -t /service/dbus-solakon`).

Der Treiber übernimmt die Rolle der Multi-RS-Firmware. Venus gibt über das AC-System vor, `control.py` setzt das per Fernsteuerung (46001–46007, 46504) ins Solakon um:

| Venus | Verhalten |
|---|---|
| Gerätemodus **Ein** | Regelung durch Venus |
| Gerätemodus **Nur Ladegerät** | keine Abgabe |
| Gerätemodus **Nur Wechselrichter** | kein Laden aus dem Netz |
| Gerätemodus **Aus** | App-Freigabe: Fernsteuerung aus, die Hersteller-App steuert (das Gerät wird nicht ausgeschaltet) |
| ESS **Optimiert** (mit/ohne BatteryLife) | Netz auf den Sollwert regeln; unter der Reserve nur PV, 2 % darunter Nachladen mit `ReserveChargePower`. BatteryLife: Grenze +5 % je Tag ohne Volladung, −5 % bei Volladung, max. 80 % |
| ESS **Akkus geladen halten** | Netzladen mit `ChargePower` bis Max-SoC (2 % Hysterese) |
| ESS **Externe Steuerung** | App-Freigabe, solange niemand `/Ess/UseInverterPowerSetpoint` setzt |
| Ladezeitpläne, Dynamic ESS | nativ: Venus setzt Sollwerte/Sperren, der Treiber führt sie aus |
| Netz-Sollwert, max. Einspeisung, Einspeisung verhindern, WR-Leistungsgrenze | aus dem ESS-Menü (`/Settings/CGwacs/...`) |
| Min-SoC (Netzreserve) | 46611 |
| **AC-Eingangsstrom** (AC-System, 0–5,4 A) | begrenzt Netzladen plus Notstromverbraucher auf Grenze × 230 V. Software-Grenze im Regler, wird in `/Settings/Solakon/AcInCurrentLimit` gespeichert. Gilt nur, solange Venus steuert |
| **Peak Shaving** (ESS-Menü: Netzbezug begrenzen `AcInputLimit`, Einspeisung begrenzen `AcExportLimit`, „Immer“ `AlwaysPeakShave`) | Netzbezug über der Grenze: Netzladen wird gedrosselt, sonst gibt der Akku bis 800 W zusätzlich ab. Normal nur über der aktiven Reserve, bei „Immer“ bis zum physischen Min-SoC (46609). Die Grenzen in A gelten für die **Summe** aller Phasen (× 230 V), weil der Zähler nur die Summe liefert |
| Netz-Sollwert | aus dem ESS-Menü (`AcPowerSetPoint`); setzt Venus selbst einen Sollwert (Dynamic ESS, extern), gilt der für 60 s vorrangig |
| Max. Ladeleistung (`MaxChargePower`) | begrenzt das Netzladen zusätzlich |

Zusätzliche Pfade am `multi.solakon`: `/Solakon/SetEpsOutput` (46613: 0 aus, 2 EPS, 3 USV; `/Solakon/EpsOutputChanged` = 1, wenn die Einstellung von außen geändert wurde), `/Solakon/SetMaxSoc` (46610), `/Solakon/SetMinSocPhysical` (46609), `/Solakon/ChargePower`, `/Solakon/ReserveChargePower` (0–1200 W), Status in `/Solakon/ControlText` und `/Solakon/ControlError`.

Grenzen: Abgabe max. 800 W, Netzladen max. 1200 W (Datenblatt). Befehle werden alle 30 s mit 90 s Timeout erneuert; endet der Treiber, gibt er die Fernsteuerung sofort ab, spätestens nach 90 s übernimmt das Gerät wieder selbst. Nie geschrieben werden: Work mode 49203, Ein/Aus 49077/49078, 45005/45007, Export Power Limit 46616, MPPT-Schalter 49210.

