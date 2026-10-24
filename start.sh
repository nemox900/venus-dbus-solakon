#!/bin/sh
# Solakon-Mod: Treiberdienste beim Start einhaengen (aufgerufen aus /data/rc.local).
# /service liegt auf einem fluechtigen Dateisystem, darum bei jedem Start neu verlinken.
BASE=$(dirname "$(readlink -f "$0")")
for n in solakon powertracker; do
	mkdir -p /var/log/dbus-$n
	[ -e /service/dbus-$n ] || ln -s "$BASE/service-$n" /service/dbus-$n
done
