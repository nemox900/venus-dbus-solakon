#!/bin/sh
# Solakon-Mod installieren oder aktualisieren.
#
#   sh install.sh
#
# Fragt nach der IP des Solakon und ob ein Solakon PowerTracker (Lesekopf am
# Hauszaehler) eingerichtet werden soll. Ohne Terminal (z. B. tools/deploy.sh)
# wird nicht gefragt; dann gelten die Umgebungsvariablen, sonst bleibt config.ini:
#   SOLAKON_HOST=<ip>   POWERTRACKER_HOST=<ip>   POWERTRACKER_HOST=nein
# Voraussetzung ist Venus OS ab MIN_VERSION (getestet). FORCE=1 uebergeht die Pruefung
# der Version und die Warnung vor einem vorhandenen Victron-Wechselrichter.
#
# Laeuft aus dem entpackten Paket (z. B. /tmp/dbus-solakon) oder direkt aus
# /data/dbus-solakon. Ziel ist immer /data/dbus-solakon (uebersteht Firmware-Updates).
# config.ini und state.json bleiben bei einem Update erhalten.
# Mehrfaches Ausfuehren ist unschaedlich.
set -e
BASE=/data/dbus-solakon
MIN_VERSION=3.85
SRC=$(dirname "$(readlink -f "$0")")

say() { echo "[solakon] $*"; }
fail() { echo "[solakon] FEHLER: $*" >&2; exit 1; }

# --- Voraussetzungen ---
[ "$(id -u)" = 0 ] || fail "bitte als root ausfuehren"
[ -d /opt/victronenergy ] || fail "kein Venus OS (/opt/victronenergy fehlt)"
[ -d /opt/victronenergy/dbus-systemcalc-py/ext/velib_python ] || fail "velib_python nicht gefunden"
[ -d /opt/victronenergy/dbus-acsystem ] || fail "dbus-acsystem fehlt (Venus OS 3.x mit Multi-RS-Unterstuetzung noetig)"
python3 -c "import pymodbus.client.sync" 2>/dev/null || fail "pymodbus 2.x fehlt"
ver=$(head -n 1 /opt/victronenergy/version 2>/dev/null)
say "Venus OS $ver"
# Das Menue "Solakon ESS" braucht die Schalter-/Zahlenfelder der neuen Oberflaeche,
# die Freigabe von Dynamic ESS das dbus-acsystem der neuen Versionen.
if ! echo "$ver" | awk -v min="$MIN_VERSION" '{
		v = $0; sub(/^v/, "", v); split(v, a, /[.~-]/); split(min, b, ".")
		exit !(a[1] > b[1] || (a[1] == b[1] && a[2] + 0 >= b[2] + 0)) }'; then
	[ "${FORCE:-0}" = 1 ] || fail "Venus OS $ver ist zu alt, noetig ist v$MIN_VERSION oder neuer (bitte Venus aktualisieren; FORCE=1 uebergeht das)"
	say "WARNUNG: Venus OS $ver aelter als v$MIN_VERSION, mit FORCE=1 trotzdem installiert"
fi

# --- Vorhandener Victron-Wechselrichter mit eigener Regelung? ---
# Ein VE.Bus-Multi oder Multi RS haette ein eigenes ESS; zwei Regler am selben
# Netzzaehler arbeiten gegeneinander.
fremd=$(dbus -y 2>/dev/null | grep -E '^com\.victronenergy\.(vebus|multi|acsystem)\.' | grep -v solakon || true)
if [ -n "$fremd" ]; then
	say "WARNUNG: Victron-Wechselrichter gefunden: $(echo $fremd)"
	say "Er hat eine eigene Regelung (ESS). Der Solakon-Mod ist fuer Anlagen ohne Victron-Wechselrichter gedacht."
	if [ -t 0 ]; then
		printf '[solakon] Trotzdem installieren? [j/N]: '
		read -r a
		case "$a" in [jJyY]*) ;; *) fail "abgebrochen" ;; esac
	elif [ "${FORCE:-0}" != 1 ]; then
		fail "abgebrochen (FORCE=1 installiert trotzdem)"
	fi
fi

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
	say "config.ini vorhanden"
else
	cp config.default.ini config.ini
	say "config.ini angelegt (Steuerung aus)"
fi

# --- Adressen: fragen (mit Terminal) oder aus Umgebungsvariablen ---
ini_get() {     # Abschnitt Schluessel
	sed -n "/^\[$1\]/,/^\[/s/^$2 *= *//p" config.ini | head -n 1 | tr -d ' '
}
ini_set() {     # Abschnitt Schluessel Wert
	if [ -n "$(sed -n "/^\[$1\]/,/^\[/{/^$2 *=/p}" config.ini)" ]; then
		sed -i "/^\[$1\]/,/^\[/s/^$2 *=.*/$2 = $3/" config.ini
	else
		grep -q "^\[$1\]" config.ini || printf '\n[%s]\n' "$1" >> config.ini
		sed -i "/^\[$1\]/a $2 = $3" config.ini
	fi
}
sol=$(ini_get solakon host); [ -n "$sol" ] || sol=$(sed -n '/^\[solakon\]/,/^\[/s/^host *= *//p' config.default.ini)
pt=$(ini_get powertracker host)
if [ -t 0 ]; then
	printf '[solakon] IP-Adresse des Solakon ONE (Modbus-TCP) [%s]: ' "$sol"
	read -r a; [ -n "$a" ] && sol=$a
	if [ -n "$pt" ]; then frage="J/n"; else frage="j/N"; fi
	printf '[solakon] Solakon PowerTracker (Lesekopf am Hauszaehler) als Netzzaehler einrichten? [%s]: ' "$frage"
	read -r a
	case "$a" in
		[jJyY]*) ja=1 ;;
		[nN]*) ja=0 ;;
		*) [ -n "$pt" ] && ja=1 || ja=0 ;;
	esac
	if [ $ja = 1 ]; then
		printf '[solakon] IP-Adresse des PowerTracker [%s]: ' "$pt"
		read -r a; [ -n "$a" ] && pt=$a
		[ -n "$pt" ] || { say "keine IP angegeben, PowerTracker wird nicht eingerichtet"; ja=0; }
	fi
	[ $ja = 1 ] || pt=""
else
	[ -n "${SOLAKON_HOST:-}" ] && sol=$SOLAKON_HOST
	case "${POWERTRACKER_HOST:-}" in
		"") ;;
		nein|no|0) pt="" ;;
		*) pt=$POWERTRACKER_HOST ;;
	esac
fi
ini_set solakon host "$sol"
ini_set powertracker host "$pt"
say "Solakon: $sol"
if [ -n "$pt" ]; then
	say "PowerTracker: $pt (im Menue Solakon ESS unter \"Betrieb: Netzzaehler\" waehlbar)"
else
	say "PowerTracker: nicht eingerichtet"
fi

# --- Start bei jedem Boot ---
[ -f /data/rc.local ] || printf '#!/bin/sh\n' > /data/rc.local
chmod 755 /data/rc.local
grep -qx "$BASE/start.sh" /data/rc.local || echo "$BASE/start.sh" >> /data/rc.local

# --- Dienste einhaengen bzw. neu starten ---
dienste=solakon; [ -n "$pt" ] && dienste="solakon powertracker"
neu=""
for n in $dienste; do [ -e /service/dbus-$n ] || neu="$neu $n"; done
"$BASE/start.sh"
for n in $dienste; do
	case " $neu " in
		*" $n "*) ;;                                    # startet svscan selbst
		*) svc -u /service/dbus-$n; svc -t /service/dbus-$n ;;   # Update: neu starten
	esac
done
sleep 8
for n in $dienste; do svstat /service/dbus-$n 2>/dev/null || true; done
say "fertig: Version $(sed -n 's/^VERSION = "\(.*\)"/\1/p' dbus-solakon.py). Log: tail -f /var/log/dbus-solakon/current"
