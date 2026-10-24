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
| `com.victronenergy.switch.solakon_ess` | 91 | **Solakon ESS-Einstellungen**: Zahlenfelder, Auswahllisten und Schalter (siehe unten) |

Seriennummern: Jedes Gerät hat eine eigene, aus der Solakon-Seriennummer abgeleitete Nummer (`<SN>` am Wechselrichter, `<SN>-BAT`, `-PV1` … `-PV4`, `-TWR`, `-TGH`, `-ESS`). Sie sind stabil über Neuinstallationen und gehören zu keinem echten Victron-Gerät. Die BMS-Seriennummer steht in `/Solakon/BmsSerial`.

Kennung für Dynamic ESS: Venus gibt Dynamic ESS nur für einen Multi RS ab einer bestimmten Firmware frei. Darum meldet `multi.solakon` die ProductId `0xA442` und die Firmware `0x11713`. Die echte Solakon-Firmware steht in `/Solakon/FirmwareText`. **In VRM kein Firmware-Update für dieses Gerät auslösen**, es kann nicht ankommen.

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

## ESS-Einstellungen

Bei einem Multi RS blendet Venus das ESS-Menü aus (*Einstellungen → ESS* zeigt nur einen Hinweis), weil diese Werte beim echten Gerät mit VictronConnect gesetzt werden. Unter *Geräteliste → AC-System → ESS* gibt es nur Modus, Min-SoC und Dynamic ESS. Alles steht deshalb im Gerät **Solakon ESS** (Geräteliste und Schalter-Bereich, auch über VRM). Die Oberfläche sortiert nach dem Namen und zeigt nur etwa 30 Zeichen, darum die Oberbegriffe vorne.

| Eintrag | Bedeutung | schreibt |
|---|---|---|
| **Betrieb: An / Aus** | Wer regelt das Solakon? *Ein: Venus regelt*. *Nur laden*: keine Abgabe. *Nur abgeben*: kein Laden aus dem Netz. *Aus: App regelt*: Venus gibt die Fernsteuerung ab, die Hersteller-App regelt, das Gerät läuft weiter. **Steht hier „Aus“, wirken alle anderen Einstellungen nicht.** | `/Mode` (wie Gerätemodus im AC-System) |
| **Betrieb: ESS-Modus** | *Optimiert*: Netz auf den Sollwert regeln, Akku für das Haus nutzen. *+ BatteryLife*: zusätzlich wandernde Untergrenze, damit der Akku regelmäßig voll wird. *Akku voll halten*: aus dem Netz laden bis Max-SoC, nicht entladen. *Extern*: App regelt, außer ein Zeitplan oder Dynamic ESS gibt etwas vor. | `/Settings/Ess/Mode` (wie AC-System → ESS) |
| **Betrieb: Netz-Sollwert** | Wie viel Watt am Zähler aus dem Netz kommen sollen. 0 = möglichst nichts beziehen, positiv = etwas Bezug zulassen (Puffer gegen Einspeisen), negativ = einspeisen. | `CGwacs/AcPowerSetPoint` |
| **Einspeisung: verhindern** | Ein = nie ins Netz einspeisen. | `PreventFeedback` |
| **Einspeisung: Limit (800=aus)** | Höchstens so viel Watt ins Netz. 800 = kein Limit. | `MaxFeedInPower` |
| **Einspeisung: PV-Überschuss** | Ein = bei vollem Akku die Solarleistung ins Netz abgeben (bis Limit/800 W). Aus = Abgabe nur bis zum Hausverbrauch, der Rest wird an den Modulen abgeregelt. Victron: „DC-gekoppelte PV: Überschuss einspeisen“. | `OvervoltageFeedIn` |
| **Netzladen: Limit (1200=aus)** | Höchstens so viel Watt aus dem Netz in den Akku, gilt für alle Fälle. 1200 = kein Limit. | `MaxChargePower` |
| **Netzladen: Akku voll halten** | Ladeleistung im ESS-Modus „Akku voll halten“. | `/Settings/Solakon/ChargePower` |
| **Netzladen: unter Reserve** | Ladeleistung, wenn der Akku 2 % unter die Reserve fällt. | `/Settings/Solakon/ReserveChargePower` |
| **Notstrom-Ausgang** | *Aus*. *EPS*: schaltet nur bei Netzausfall ein. *USV*: unterbrechungsfrei, immer versorgt. Die Last zählt mit zu den 800 W. | Register 46613 |
| **Peak Shaving: Bezug (0=aus)** | Netzbezug des ganzen Hauses höchstens so viele Ampere (× 230 V, Summe aller Phasen). Darüber gibt der Akku zusätzlich ab (bis 800 W), Netzladen wird gedrosselt. | `AcInputLimit` |
| **Peak Shaving: Einsp. (0=aus)** | Einspeisung höchstens so viele Ampere. | `AcExportLimit` |
| **Peak Shaving: immer aktiv** | Aus = Peak Shaving nur über der Reserve. Ein = bis zum Akkuschutz-SoC. | `AlwaysPeakShave` |
| **SoC: max. (Ladeende)** | Bis hierhin wird geladen. | Register 46610 |
| **SoC: min. (Akkuschutz)** | Darunter wird nie entladen, auch nicht bei Netzausfall (Notstrom). | Register 46609 |
| **SoC: Reserve (Netzbetrieb)** | Darunter kein Entladen für das Haus, solange Netz da ist (bleibt für Notstrom). Wie „Min-SoC“ im AC-System. | Register 46611 |
| **VRM-DESS: max. …** | Grenzen für Dynamic ESS (kW). Normalerweise trägt VRM sie ein. -1 = nicht eingetragen: Dann bricht Dynamic ESS ab. Passend: Laden 1,2, Entladen 0,8. | `/Settings/DynamicEss/*Limit` |
| **WR-Abgabe: Limit (800=aus)** | Höchstens so viel Watt gibt das Solakon ins Haus ab. 800 = kein Limit. | `MaxDischargePower` |

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
| ESS **Akkus geladen halten** | Netzladen mit `ChargePower` bis Max-SoC (2 % Hysterese); bei vollem Akku deckt die PV das Haus (ohne Entladen), Überschuss je nach „PV-Überschuss einspeisen“ |
| ESS **Externe Steuerung** | App-Freigabe, solange niemand `/Ess/UseInverterPowerSetpoint` setzt |
| Ladezeitpläne, Dynamic ESS | nativ: Venus setzt Sollwerte/Sperren, der Treiber führt sie aus |
| Netz-Sollwert, max. Einspeisung, Einspeisung verhindern, WR-Leistungsgrenze | aus dem ESS-Menü (`/Settings/CGwacs/...`) |
| Min-SoC (Netzreserve) | 46611 |
| **AC-Eingangsstrom** (AC-System, 0–5,4 A) | begrenzt Netzladen plus Notstromverbraucher auf Grenze × 230 V. Software-Grenze im Regler, wird in `/Settings/Solakon/AcInCurrentLimit` gespeichert. Gilt nur, solange Venus steuert |
| **Peak Shaving** (ESS-Menü: Netzbezug begrenzen `AcInputLimit`, Einspeisung begrenzen `AcExportLimit`, „Immer“ `AlwaysPeakShave`) | Netzbezug über der Grenze: Netzladen wird gedrosselt, sonst gibt der Akku bis 800 W zusätzlich ab. Normal nur über der aktiven Reserve, bei „Immer“ bis zum physischen Min-SoC (46609). Die Grenzen in A gelten für die **Summe** aller Phasen (× 230 V), weil der Zähler nur die Summe liefert |
| Netz-Sollwert | aus dem ESS-Menü (`AcPowerSetPoint`); setzt Venus selbst einen Sollwert (Dynamic ESS, extern), gilt der für 60 s vorrangig |
| Max. Ladeleistung (`MaxChargePower`) | begrenzt das Netzladen zusätzlich |

Zusätzliche Pfade am `multi.solakon`: `/Solakon/SetEpsOutput` (46613: 0 aus, 2 EPS, 3 USV; `/Solakon/EpsOutputChanged` = 1, wenn die Einstellung von außen geändert wurde), `/Solakon/SetMaxSoc` (46610), `/Solakon/SetMinSocPhysical` (46609), `/Solakon/ChargePower`, `/Solakon/ReserveChargePower` (0–1200 W), Status in `/Solakon/ControlText` und `/Solakon/ControlError`.

Zwischen Abgabe und Netzladen wird frühestens nach 30 s gewechselt (bis dahin 0 W im alten Modus), damit das Gerät bei einem Sollwert um 0 nicht ständig umschaltet.

Grenzen: Abgabe max. 800 W, Netzladen max. 1200 W (Datenblatt). Befehle werden alle 30 s mit 90 s Timeout erneuert; endet der Treiber, gibt er die Fernsteuerung sofort ab, spätestens nach 90 s übernimmt das Gerät wieder selbst. Nie geschrieben werden: Work mode 49203, Ein/Aus 49077/49078, 45005/45007, Export Power Limit 46616, MPPT-Schalter 49210.

