#!/usr/bin/env python3
"""Inspecciona un paquete TAK sin exponer claves ni contraseñas en la terminal."""
import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile, BadZipFile

SENSITIVE = re.compile(r'(password|passwd|passphrase|credential|secret|token)',re.I)

def info(zip_path):
    if not zip_path.is_file():
        raise ValueError('No existe ZIP: ' + str(zip_path))
    with ZipFile(zip_path) as z:
        names = [i for i in z.infolist() if not i.is_dir()]
        basenames = {Path(i.filename).name.lower(): i for i in names}
        for name in ('caCert.p12','clientCert.p12','config.pref'):
            print(f'{name}:', 'PRESENTE' if name.lower() in basenames else 'NO ENCONTRADO')
        prefs = [i for i in names if Path(i.filename).name.lower()=='config.pref']
        if not prefs:
            print('Sin config.pref: configurar servidor manualmente.');return
        if len(prefs)>1:
            print('Varios config.pref: usando',prefs[0].filename)
        item=prefs[0]
        if item.file_size>1_000_000:
            raise ValueError('config.pref demasiado grande para análisis seguro')
        root=ET.fromstring(z.read(item))
        connections=[]
        for p in root.iter('preference'):
            if p.get('name') != 'cot_streams': continue
            for e in p.findall('entry'):
                if re.fullmatch(r'connectString\d*', e.get('key','')):
                    val=(e.text or '').strip()
                    # Last two colons delimit port and transport (host may be an IPv6 literal).
                    try:
                        host,port,transport=val.rsplit(':',2)
                        if not (host and 1<=int(port)<=65535): raise ValueError()
                    except ValueError:
                        print('Entrada connectString con formato no reconocido (omitida)')
                        continue
                    connections.append((host,port,transport))
        print('\nConexiones TAK encontradas:')
        if not connections: print(' (ninguna)')
        for host,port,transport in connections:
            print(f' Servidor: {host}  Puerto: {port}  Transporte: {transport}')
        has_sensitive=False
        for e in root.iter('entry'):
            if SENSITIVE.search(e.get('key','')):
                has_sensitive=True
                break
        if has_sensitive:
            print('\nAVISO: config.pref puede incluir contraseñas. No compartir ni subir a Git.')
        print('No se muestran contraseñas ni contenido de certificados.')

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('zip', type=Path)
    args=ap.parse_args()
    try: info(args.zip.expanduser())
    except (ValueError,OSError,BadZipFile,ET.ParseError) as e:
        print('ERROR:',e,file=sys.stderr);return 2
    return 0

if __name__=='__main__':
    sys.exit(main())
