#!/bin/sh
# Release-Archiv aus dem aktuellen Git-Stand bauen: dist/dbus-solakon-<version>.tar.gz
# Auf dem GX-Geraet:  tar xzf dbus-solakon-*.tar.gz -C /tmp && sh /tmp/dbus-solakon/install.sh
set -e
cd "$(dirname "$(readlink -f "$0")")/.."
V=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' dbus-solakon.py)
[ -z "$(git status --porcelain)" ] || echo "WARNUNG: nicht eingecheckte Aenderungen sind nicht im Archiv" >&2
mkdir -p dist
git archive --prefix=dbus-solakon/ -o "dist/dbus-solakon-$V.tar.gz" HEAD
echo "dist/dbus-solakon-$V.tar.gz"
