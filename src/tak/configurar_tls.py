#!/usr/bin/env python3
"""Configure TAK TLS sidecar, preserving previous location and identity."""
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime

CFG = Path(os.environ.get('FPV_TAK_CONFIG','/home/cetel/detector_drone/fpv_tak_config.json'))
CERTS = Path.home()/'detector_drone/tak_certs'

def main():
    if not CFG.is_file():
        raise ValueError('No existe fpv_tak_config.json: ejecuta primero el instalador V1/V2 y configura la posición.')
    obj=json.loads(CFG.read_text(encoding='utf-8'))
    old=obj.get('tak',{})
    print('Configurar TAK TLS TCP (no cambia posición, UID, detector ni Web).')
    print('Host del servidor: introduce DNS/FQDN o IP. La validación TLS exige CA y nombre correctos.')
    host=input(f'Servidor TAK [{old.get("host","")}]: ').strip() or old.get('host','')
    if not host or host.startswith('239.') or len(host)>253:
        raise ValueError('Se requiere IP o nombre DNS de un servidor TAK, no dirección multicast')
    v=input('Puerto TCP TLS [8089]: ').strip() or '8089'
    port=int(v)
    if not 1<=port<=65535: raise ValueError('Puerto inválido')
    name=input(f'Nombre DNS del certificado TLS [igual que host {host}]: ').strip() or host
    tls={'ca_file':str(CERTS/'ca.pem'),'cert_file':str(CERTS/'client.crt'),
         'key_file':str(CERTS/'client.key'),'server_name':name}
    for val in ('ca_file','cert_file','key_file'):
        if not Path(tls[val]).is_file():
            raise ValueError(f'No existe {tls[val]}; importa primero los certificados PKCS12 en la Raspberry Pi.')
    obj['tak'].update({'transport':'tls','host':host,'port':port,'tls':tls})
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    backup=CFG.with_name(CFG.name+f'.backup-udp-{stamp}')
    backup.write_bytes(CFG.read_bytes()); os.chmod(backup,0o600)
    os.umask(0o077)
    with tempfile.NamedTemporaryFile('w',dir=CFG.parent,encoding='utf8',delete=False) as f:
        json.dump(obj,f,indent=2,ensure_ascii=False);f.write('\n');temp=f.name
    os.chmod(temp,0o600);os.replace(temp,CFG)
    print('Configuración TLS guardada en',CFG,'respaldo en',backup)
    print('Probar: python3 probar_tls.py')
    print('Activar: sudo systemctl restart fpv-tak-sensor')

if __name__=='__main__':
    try: main()
    except (OSError,ValueError,KeyboardInterrupt) as e:
        print('ERROR:',e,file=sys.stderr);sys.exit(2)
