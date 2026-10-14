#!/usr/bin/env python3
"""Cambiar SOLO fuente de posición del puente TAK. No toca SoftCert ni SDR."""
import argparse
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from fpv_tak_sensor import valid_location, finite

DEFAULT = Path.home() / 'detector_drone' / 'fpv_tak_config.json'


def main():
    parser = argparse.ArgumentParser(description='Posición TAK: GPSD automático, fija o desactivada')
    parser.add_argument('mode', choices=['auto','gpsd','static','disabled','status'])
    parser.add_argument('--config', type=Path, default=DEFAULT)
    parser.add_argument('--lat', type=float)
    parser.add_argument('--lon', type=float)
    parser.add_argument('--hae', type=float)
    parser.add_argument('--fallback-static', action='store_true', help='solo en modo auto, opt-in EXPLÍCITO')
    parser.add_argument('--gpsd-host', default='127.0.0.1')
    parser.add_argument('--gpsd-port', type=int, default=2947)
    parser.add_argument('--max-age', type=float, default=8, help='segundos máximos sin TPV válido')
    parser.add_argument('--movement', type=float, default=5, help='metros horizontales para refresco rápido')
    args = parser.parse_args()
    if args.mode == 'status':
        data = json.loads(args.config.read_text())
        p = data['position']
        print('Modo:',p['mode'],'| GPSD:',f"{p.get('gpsd_host','127.0.0.1')}:{p.get('gpsd_port',2947)}",
              '| fallback estático explícito:',bool(p.get('fallback_static', False)))
        try:
            st = json.loads(Path('/dev/shm/fpv_tak_status.json').read_text())
            print('Fuente actual:', st.get('position_source'), '| Coordenadas válidas:',st.get('position_valid'),
                  '| Conexión GPSD:', st.get('gpsd_connected'))
        except (OSError, ValueError):
            print('Todavía no hay estado vivo del puente TAK.')
        return
    if not (1 <= args.gpsd_port <= 65535): parser.error('gpsd-port inválido')
    if finite(args.max_age,1,120) is None: parser.error('--max-age debe ser 1–120 s')
    if finite(args.movement,0.5,1000) is None: parser.error('--movement debe ser 0.5–1000 m')
    if args.fallback_static and args.mode!='auto': parser.error('--fallback-static requiere modo auto')
    cfg=json.loads(args.config.read_text())
    p=cfg.setdefault('position',{})
    if args.mode=='static':
        if not valid_location(args.lat,args.lon,args.hae): parser.error('Modo static requiere --lat y --lon válidos (no 0,0)')
        p.update(lat=args.lat,lon=args.lon,hae=args.hae)
    if args.fallback_static and not valid_location(p.get('lat'),p.get('lon'),p.get('hae')):
        parser.error('No hay coordenadas estáticas válidas para fallback')
    p.update(mode=args.mode, gpsd_host=args.gpsd_host, gpsd_port=args.gpsd_port,
             gpsd_max_age_s=args.max_age, movement_threshold_m=args.movement,
             fallback_static=args.fallback_static)
    # Never overwrite server information, TLS or certificates.
    stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup=args.config.with_name(args.config.name+'.bak-pos-v32-'+stamp)
    import shutil
    shutil.copy2(args.config,backup)
    os.chmod(backup,0o600)
    fd,tmp=tempfile.mkstemp(prefix='.tak-pos-v32-',dir=args.config.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(cfg,f,indent=2,ensure_ascii=False)
            f.write('\n')
            f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600)
        os.replace(tmp,args.config)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print(f'Posición actualizada a {args.mode}. Copia privada: {backup}')
    print('Reinicia SOLO el puente TAK: sudo systemctl restart fpv-tak-sensor')
    if args.mode in ('auto','gpsd'):
        print('Sin GNSS válido el puente NO enviará posición, salvo fallback estático habilitado expresamente.')


if __name__=='__main__':
    main()
