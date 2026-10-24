#!/bin/sh
# Solakon-Mod: Treiberdienste beim Start einhaengen (aufgerufen aus /data/rc.local).
# /service liegt auf einem fluechtigen Dateisystem, darum bei jedem Start neu verlinken.
# Der PowerTracker ist optional: nur mit [powertracker] host in config.ini.
BASE=$(dirname "$(readlink -f "$0")")
pt_host=$(sed -n '/^\[powertracker\]/,/^\[/s/^host *= *//p' "$BASE/config.ini" 2>/dev/null | tr -d ' ')
dienste=solakon
if [ -n "$pt_host" ]; then
	dienste="solakon powertracker"
elif [ -e /service/dbus-powertracker ]; then
	svc -d /service/dbus-powertracker
	rm /service/dbus-powertracker
	svc -dx "$BASE/service-powertracker" "$BASE/service-powertracker/log" 2>/dev/null
fi
for n in $dienste; do
	mkdir -p /var/log/dbus-$n
	[ -e /service/dbus-$n ] || ln -s "$BASE/service-$n" /service/dbus-$n
done
