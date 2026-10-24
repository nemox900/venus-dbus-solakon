# Solakon-Mod: Deutsch/Englisch.
#
# Die Sprache kommt aus der Venus-Oberflaeche (/Settings/Gui/Language): "de" ergibt
# Deutsch, jede andere Sprache Englisch. Gelesen wird sie beim Start; aendert sie
# sich, starten die Treiber neu (Geraete- und Menuenamen werden beim Anlegen gesetzt).

import dbus

LANG = "de"


def read_language(bus):
    try:
        v = bus.get_object("com.victronenergy.settings", "/Settings/Gui/Language").GetValue(
            dbus_interface="com.victronenergy.BusItem")
    except dbus.exceptions.DBusException:
        return None
    return "de" if str(v).lower().startswith("de") else "en"


def set_language(code):
    global LANG
    LANG = "de" if code == "de" else "en"


def T(de, en):
    return de if LANG == "de" else en
