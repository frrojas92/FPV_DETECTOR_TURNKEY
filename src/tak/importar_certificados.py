#!/usr/bin/env python3
"""Local PKCS#12 importer for TAK. No key/certificate leaves this device."""
import argparse
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from zipfile import ZipFile, BadZipFile

DEFAULT_DIR = Path.home() / 'detector_drone/tak_certs'

def pkcs12_openssl(path, password, *flags, legacy=False):
    cmd = ['openssl', 'pkcs12', '-in', str(path), '-passin', 'stdin']
    if legacy:
        cmd.append('-legacy')
    cmd.extend(flags)
    # Password is fed on stdin, never through the command line or environment.
    result = subprocess.run(cmd, input=(password + '\n').encode('utf-8'), capture_output=True)
    if result.returncode:
        errmsg = result.stderr.decode('utf-8', 'replace')
        raise RuntimeError('No fue posible abrir PKCS12. Comprueba la clave o prueba --legacy. ' + errmsg[:450])
    return result.stdout

def choose(items, label, preference):
    if not items:
        raise ValueError('No hay certificados PKCS12 válidos para ' + label)
    items = sorted(items, key=lambda t: (preference(t[0]), t[0].lower()))
    print('\n' + label + ':')
    for i,(name,_) in enumerate(items,1):
        print(f' {i}. {name}')
    if len(items)==1:
        print(' Selección automática: 1')
        return items[0][1]
    idx = input('Seleccionar número [1]: ').strip() or '1'
    try:
        return items[int(idx)-1][1]
    except (ValueError, IndexError):
        raise ValueError('Selección inválida')

def prepare_zip(zip_path, dest, td):
    with ZipFile(zip_path) as z:
        members=[]
        for inf in z.infolist():
            if inf.is_dir() or not inf.filename.lower().endswith(('.p12','.pfx')):
                continue
            if inf.file_size > 8_000_000:
                raise ValueError('Archivo PKCS12 excesivamente grande dentro del ZIP')
            members.append((inf.filename,inf))
        print('Archivos PKCS12 encontrados:', ', '.join(x[0] for x in members) or '(ninguno)')
        # ATAK soft-cert packages commonly name their truststore caCert.p12.
        trust = [(n,i) for n,i in members if re.search(r'(trust|caCert|ca[._-]|root|intermediate)',n.split('/')[-1],re.I)]
        clients = [(n,i) for n,i in members if (n,i) not in trust]
        if not trust or not clients:
            raise ValueError('ZIP sin clientCert.p12 y caCert.p12 (o nombres equivalentes) claramente distinguibles. '\
                             'Puede ser un paquete de autoinscripción sin certificados. '\
                             'Usa --client /ruta/cliente.p12 --truststore /ruta/truststore.p12.')
        cc=choose(clients,'Certificado CLIENTE', lambda n: 0 if re.search(r'(client|user|device)',n,re.I) else 1)
        tt=choose(trust,'Truststore CA de TAK', lambda n: 0 if re.search(r'truststore',n,re.I) else 1)
        paths=[]
        for src,name in ((cc,'client.p12'),(tt,'truststore.p12')):
            path=Path(td)/name
            data=z.read(src)
            if len(data)>8_000_000: raise ValueError('Archivo demasiado grande')
            with path.open('xb') as f: f.write(data)
            os.chmod(path,0o600)
            paths.append(path)
        return paths

def save_secret(path,data):
    fd=os.open(path,os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data)
    except BaseException:
        try: path.unlink()
        except OSError: pass
        raise

def main():
    ap=argparse.ArgumentParser(description='Importador local TAK TLS compatible con caCert.p12/clientCert.p12. Nunca publica claves.')
    src=ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--zip',type=Path,help='Paquete TAK local con client.p12 y truststore.p12')
    src.add_argument('--client',type=Path,help='PKCS12 del cliente TAK local')
    ap.add_argument('--truststore',type=Path,help='PKCS12 de CA local (obligatorio con --client)')
    ap.add_argument('--dest',type=Path,default=DEFAULT_DIR)
    ap.add_argument('--legacy',action='store_true',help='Necesario para algunos PKCS12 antiguos con OpenSSL 3')
    args=ap.parse_args()
    if args.client and not args.truststore: ap.error('--client requiere --truststore')
    os.umask(0o077)
    dest=args.dest.expanduser().resolve()
    dest.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(dest,0o700)
    for n in ('client.crt','client.key','ca.pem'):
        if (dest/n).exists():
            raise ValueError(f'{dest/n} ya existe. Haz respaldo privado y retíralo antes de importar de nuevo.')
    with tempfile.TemporaryDirectory(prefix='tak-cert-',dir=dest) as td:
        if args.zip:
            if not args.zip.is_file(): raise ValueError('ZIP no encontrado')
            client,trust=prepare_zip(args.zip,dest,td)
        else:
            client,trust=args.client.expanduser(),args.truststore.expanduser()
        client_pw=getpass.getpass('Contraseña PKCS12 de CLIENTE (no se guarda): ')
        trust_pw=getpass.getpass('Contraseña de TRUSTSTORE (Enter = misma del cliente): ')
        if not trust_pw: trust_pw=client_pw
        crt=pkcs12_openssl(client,client_pw,'-clcerts','-nokeys',legacy=args.legacy)
        # Include any intermediate CA certificates supplied by the client bundle.
        chain=pkcs12_openssl(client,client_pw,'-cacerts','-nokeys',legacy=args.legacy)
        if b'-----BEGIN CERTIFICATE-----' in chain:
            crt += b'\n' + chain
        key=pkcs12_openssl(client,client_pw,'-nocerts','-nodes',legacy=args.legacy)
        ca=pkcs12_openssl(trust,trust_pw,'-nokeys',legacy=args.legacy)
        if b'-----BEGIN CERTIFICATE-----' not in crt or b'PRIVATE KEY-----' not in key or b'-----BEGIN CERTIFICATE-----' not in ca:
            raise ValueError('Contenido PKCS12 incompleto: falta certificado, clave privada o CA')
        # Validate matching private key and X509 certificates before storing any data.
        from ssl import create_default_context
        tmp_crt=Path(td)/'cert.pem';tmp_key=Path(td)/'key.pem';tmp_ca=Path(td)/'ca.pem'
        for p,data in ((tmp_crt,crt),(tmp_key,key),(tmp_ca,ca)): save_secret(p,data)
        ctx=create_default_context(cafile=str(tmp_ca))
        ctx.load_cert_chain(str(tmp_crt),str(tmp_key))
        created=[]
        try:
            for n,content in (('client.crt',crt),('client.key',key),('ca.pem',ca)):
                f=dest/n;save_secret(f,content);created.append(f)
        except BaseException:
            for f in created: f.unlink(missing_ok=True)
            raise
    print('\nIMPORTADO LOCALMENTE (no publicar estos archivos ni subirlos a Git):')
    for f in created: print(f,'permisos 600')
    print('Siguiente paso: python3 configurar_tls.py')

if __name__=='__main__':
    try: main()
    except (ValueError,OSError,RuntimeError,BadZipFile) as e:
        print('ERROR:',e,file=sys.stderr)
        sys.exit(2)
