# device_actions.py

def switch_light(params):
    aktion = params.get('aktion')
    if aktion == 'an':
        print("Das Licht im Wohnzimmer wurde eingeschaltet.")
    elif aktion == 'aus':
        print("Das Licht im Wohnzimmer wurde ausgeschaltet.")
    else:
        print(f"Unbekannte Aktion für Licht: {aktion}")

def set_ac_temperature(params):
    temperatur = params.get('temperatur')
    modus = params.get('modus', 'kühl')
    print(f"Klimaanlage auf {temperatur}°C im Modus '{modus}' eingestellt.")
