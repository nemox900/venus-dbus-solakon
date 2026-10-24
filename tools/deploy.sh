#!/bin/sh
# Paket per SSH auf ein GX-Geraet spielen und installieren.
#
#   tools/deploy.sh <host> [port]
#
# Baut das Release-Archiv (tools/make-release.sh), entpackt es auf dem Geraet
# nach /tmp/dbus-solakon und startet dort install.sh. Anmeldung als root
# (SSH-Schluessel oder Passwort). Anderer SSH-Befehl: SSH="..." tools/deploy.sh ...
set -e
HOST=${1:?Aufruf: $0 <host> [port]}
PORT=${2:-22}
SSH=${SSH:-ssh}
cd "$(dirname "$(readlink -f "$0")")/.."
PKG=$(tools/make-release.sh)
echo "[deploy] $PKG -> root@$HOST:$PORT"
$SSH -p "$PORT" "root@$HOST" \
	'rm -rf /tmp/dbus-solakon && tar xzf - -C /tmp && sh /tmp/dbus-solakon/install.sh && rm -rf /tmp/dbus-solakon' < "$PKG"
