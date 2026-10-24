#!/bin/sh
# Solakon-Mod entfernen.
#
#   sh uninstall.sh          Dienste stoppen und austragen; Dateien, config.ini,
#                            Ertraege (state.json) und Einstellungen bleiben
#   sh uninstall.sh --alles  zusaetzlich /data/dbus-solakon, Logs und die
#                            Einstellungen /Settings/Solakon/* loeschen
#
# Beim Stoppen gibt der Treiber die Fernsteuerung frei (46001 = 0), die
# Hersteller-App steuert danach wieder. Geraeteeinstellungen (Ladegrenzen,
# EPS) bleiben im Solakon so, wie sie zuletzt eingestellt waren.
BASE=/data/dbus-solakon
ALLES=0
[ "${1:-}" = "--alles" ] && ALLES=1

say() { echo "[solakon] $*"; }
[ "$(id -u)" = 0 ] || { echo "bitte als root ausfuehren" >&2; exit 1; }

for n in solakon powertracker; do
	if [ -e /service/dbus-$n ]; then
		svc -d /service/dbus-$n
		rm /service/dbus-$n
	fi
	# supervise-Prozesse beenden (bleiben sonst nach dem Entfernen des Links haengen)
	[ -d "$BASE/service-$n" ] && svc -dx "$BASE/service-$n" "$BASE/service-$n/log" 2>/dev/null
done
sleep 3
if grep -q "Fernsteuerung aus" /var/log/dbus-solakon/current 2>/dev/null; then
	say "Fernsteuerung freigegeben: $(grep "Fernsteuerung aus" /var/log/dbus-solakon/current | tail -n 1 | cut -c26-)"
fi
[ -f /data/rc.local ] && sed -i "\#^$BASE/start.sh\$#d" /data/rc.local
say "Dienste gestoppt und aus dem Start entfernt"

if [ $ALLES = 1 ]; then
	python3 - <<'PY' || say "Einstellungen konnten nicht entfernt werden"
import dbus
b = dbus.SystemBus()
paths = [p for p in b.get_object("com.victronenergy.settings", "/Settings/Solakon")
         .GetValue(dbus_interface="com.victronenergy.BusItem").keys()]
full = ["/Settings/Solakon/" + p for p in paths]
if full:
    b.get_object("com.victronenergy.settings", "/").RemoveSettings(
        dbus.Array(full, signature="s"), dbus_interface="com.victronenergy.Settings")
print("[solakon] Einstellungen entfernt: %d" % len(full))
PY
	rm -rf /var/log/dbus-solakon /var/log/dbus-powertracker
	cd /
	rm -rf "$BASE"
	say "alles entfernt"
else
	say "Dateien bleiben in $BASE (komplett entfernen: sh $BASE/uninstall.sh --alles)"
fi
