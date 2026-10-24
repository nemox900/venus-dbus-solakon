#!/bin/sh
# Solakon-Mod installieren oder aktualisieren.
#
#   sh install.sh
#
# Laeuft aus dem entpackten Paket (z. B. /tmp/dbus-solakon) oder direkt aus
# /data/dbus-solakon. Ziel ist immer /data/dbus-solakon (uebersteht Firmware-Updates).
# config.ini und state.json bleiben bei einem Update erhalten.
# Mehrfaches Ausfuehren ist unschaedlich.
set -e
BASE=/data/dbus-solakon
SRC=$(dirname "$(readlink -f "$0")")

say() { echo "[solakon] $*"; }
fail() { echo "[solakon] FEHLER: $*" >&2; exit 1; }

# --- Voraussetzungen ---
[ "$(id -u)" = 0 ] || fail "bitte als root ausfuehren"
[ -d /opt/victronenergy ] || fail "kein Venus OS (/opt/victronenergy fehlt)"
[ -d /opt/victronenergy/dbus-systemcalc-py/ext/velib_python ] || fail "velib_python nicht gefunden"
[ -d /opt/victronenergy/dbus-acsystem ] || fail "dbus-acsystem fehlt (Venus OS 3.x mit Multi-RS-Unterstuetzung noetig)"
python3 -c "import pymodbus.client.sync" 2>/dev/null || fail "pymodbus 2.x fehlt"
say "Venus OS $(cat /opt/victronenergy/version 2>/dev/null | head -n 1)"

# --- Dateien kopieren (nur wenn aus einem anderen Ordner gestartet) ---
if [ "$SRC" != "$BASE" ]; then
	mkdir -p "$BASE"
	tar -C "$SRC" -cf - --exclude=config.ini --exclude='config.ini.*' --exclude=state.json \
		--exclude=supervise --exclude=__pycache__ . | tar -C "$BASE" -xf -
	say "Dateien nach $BASE kopiert"
fi
cd "$BASE"
chmod 755 dbus-solakon.py dbus-powertracker.py install.sh uninstall.sh start.sh \
	service-*/run service-*/log/run
if [ -f config.ini ]; then
	say "config.ini vorhanden, bleibt unveraendert"
else
	cp config.default.ini config.ini
	say "config.ini angelegt (Steuerung aus). Adressen pruefen: $BASE/config.ini"
fi

# --- Start bei jedem Boot ---
[ -f /data/rc.local ] || printf '#!/bin/sh\n' > /data/rc.local
chmod 755 /data/rc.local
grep -qx "$BASE/start.sh" /data/rc.local || echo "$BASE/start.sh" >> /data/rc.local

# --- Dienste einhaengen bzw. neu starten ---
neu=""
for n in solakon powertracker; do [ -e /service/dbus-$n ] || neu="$neu $n"; done
"$BASE/start.sh"
for n in solakon powertracker; do
	case " $neu " in
		*" $n "*) ;;                                    # startet svscan selbst
		*) svc -u /service/dbus-$n; svc -t /service/dbus-$n ;;   # Update: neu starten
	esac
done
sleep 8
for n in solakon powertracker; do svstat /service/dbus-$n 2>/dev/null || true; done
say "fertig: Version $(sed -n 's/^VERSION = "\(.*\)"/\1/p' dbus-solakon.py). Log: tail -f /var/log/dbus-solakon/current"
