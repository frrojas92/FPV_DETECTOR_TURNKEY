#!/usr/bin/env python3
"""Configuración interactiva sin tocar ajustes del detector."""
from pathlib import Path
import ipaddress
import json
import os
import uuid
import sys
from fpv_tak_sensor import valid_location

HERE = Path(__file__).resolve().parent
CFG = Path(os.environ.get('FPV_TAK_CONFIG', '/home/cetel/detector_drone/fpv_tak_config.json'))


def ask(label, default):
    val = input(f'{label} [{default}]: ').strip()
    return val if val else str(default)


def main():
    if CFG.exists():
        obj = json.loads(CFG.read_text())
    else:
        obj = json.loads((HERE/'config.example.json').read_text())
    if 'REEMPLAZAR' in obj.get('uid',''):
        obj['uid'] = 'FPV-RF-SENSOR-' + uuid.uuid4().hex[:12].upper()
    print('\nConfiguración FPV -> TAK | sin tocar V19-E')
    print('UID sensor:', obj['uid'], '| persistente en este equipo')
    obj['callsign'] = ask('Indicativo visible en ATAK', obj['callsign'])
    host = ask('Destino TAK UDP (multicast o IP del ATAK)', obj['tak']['host'])
    if not host or len(host) > 253: raise ValueError('Destino TAK no válido')
    obj['tak']['host'] = host
    obj['tak']['port'] = int(ask('Puerto UDP', obj['tak']['port']))
    if not 1 <= obj['tak']['port'] <= 65535: raise ValueError('Puerto inválido')
    print('\nPosición: 1=Fija (medida con GPS) | 2=GPSD (GNSS conectado) | 3=Deshabilitada')
    choice = input('Seleccione [1]: ').strip() or '1'
    if choice == '1':
        obj['position']['mode'] = 'static'
        lat = float(input('Latitud decimal real (-90 a 90): ').strip())
        lon = float(input('Longitud decimal real (-180 a 180): ').strip())
        val = input('Altitud HAE en metros (opcional, Enter si desconocida): ').strip()
        hae = float(val) if val else None
        if not valid_location(lat, lon, hae): raise ValueError('Ubicación inválida; nunca se enviará 0,0')
        obj['position'].update({'lat':lat,'lon':lon,'hae':hae})
    elif choice == '2':
        obj['position']['mode'] = 'gpsd'
        obj['position']['gpsd_host'] = ask('Servidor gpsd', obj['position']['gpsd_host'])
        obj['position']['gpsd_port'] = int(ask('Puerto gpsd', obj['position']['gpsd_port']))
    elif choice == '3':
        obj['position']['mode'] = 'disabled'
    else:
        raise ValueError('Selección inválida')
    CFG.parent.mkdir(parents=True, exist_ok=True)
    if CFG.exists():
        backup = CFG.with_suffix('.json.bak')
        backup.write_bytes(CFG.read_bytes())
        print('Respaldo:', backup)
    CFG.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.chmod(CFG, 0o600)
    print('\nArchivo:', CFG)
    print('Comprobación sin transmitir:')
    print(f'python3 {HERE / "fpv_tak_sensor.py"} --config {CFG} --once --dry-run')
    print('Aplicar: sudo systemctl restart fpv-tak-sensor')


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyboardInterrupt) as exc:
        print('ERROR, no se aplicaron cambios:', exc, file=sys.stderr)
        sys.exit(2)
