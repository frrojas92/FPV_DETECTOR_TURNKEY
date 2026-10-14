#!/usr/bin/env python3
"""Import ATAK SoftCert ZIP into an already installed FPV TAK Sensor.

Single operation: parse config.pref, import PKCS#12, authenticate to TAK via TLS,
atomically replace config/certs with local backups, and restart only the TAK sidecar.
No private key or password is sent outside this device (besides normal TLS auth).
Never installs config.pref in the persistent config directory.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from zipfile import ZipFile, BadZipFile
import xml.etree.ElementTree as ET

from importar_certificados import pkcs12_openssl
from fpv_tak_sensor import Sender, validate

DEFAULT_PROJECT = Path.home() / 'detector_drone'
CERT_NAMES = ('client.crt', 'client.key', 'ca.pem')
MAX_ZIP = 20_000_000
MAX_MEMBER = 8_000_000


def choose_single(items, label):
    if not items: raise ValueError(f'Falta {label} en ZIP SoftCert')
    if len(items) != 1: raise ValueError(f'Múltiples archivos {label}; un paquete debe indicar uno solo: {items}')
    return items[0]


def read_zip(path: Path):
    if not path.is_file() or path.stat().st_size > MAX_ZIP:
        raise ValueError('ZIP inexistente o excede límite de 20 MB')
    with ZipFile(path) as z:
        entries = [i for i in z.infolist() if not i.is_dir()]
        for i in entries:
            if i.file_size > MAX_MEMBER:
                raise ValueError('Entrada ZIP demasiado grande: ' + i.filename)
        def named(name):
            names = [i for i in entries if Path(i.filename).name.lower() == name.lower()]
            return choose_single(names, name)
        client, ca, pref = named('clientCert.p12'), named('caCert.p12'), named('config.pref')
        content = z.read(pref)
        if len(content) > 1_000_000: raise ValueError('config.pref demasiado grande')
        if b'<!DOCTYPE' in content.upper(): raise ValueError('DOCTYPE no admitido en config.pref')
        root = ET.fromstring(content)
        return z.read(client), z.read(ca), root


def parse_preferences(root, forced_index=None):
    # Accept both global ATAK and per-connection variants without exposing passwords.
    groups = {}
    for group in root.findall('.//preference'):
        name = group.get('name', '')
        values = groups.setdefault(name, {})
        for entry in group.findall('entry'):
            key = entry.get('key', '')
            if key and key not in values:
                values[key] = (entry.text or '').strip()
    streams = groups.get('cot_streams', {})
    global_prefs = groups.get('com.atakmap.app_preferences', {})
    candidates = []
    for key, raw in streams.items():
        match = re.fullmatch(r'connectString(\d+)', key)
        if not match: continue
        index = int(match.group(1))
        parts = raw.rsplit(':', 2)
        if len(parts) != 3: continue
        host, port_raw, transport = parts
        if transport.lower() not in ('ssl', 'tls') or not host or '\n' in host or '\r' in host: continue
        if streams.get(f'enabled{index}', 'true').lower() == 'false': continue
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535): continue
        except ValueError: continue
        candidates.append((index, host.strip('[]'), port))
    candidates.sort()
    if forced_index is not None:
        candidates = [c for c in candidates if c[0] == forced_index]
    if len(candidates) != 1:
        possible = ', '.join(f'{i}={h}:{p}' for i, h, p in candidates) or '(ninguna)'
        raise ValueError('Se requiere una sola conexión TAK TLS habilitada; encontradas: '
                         + possible + '. Para varias, utiliza --connection N.')
    i, host, port = candidates[0]
    def value(name):
        return (streams.get(f'{name}{i}') or global_prefs.get(f'{name}{i}')
                or global_prefs.get(name) or streams.get(name))
    return host, port, value('clientPassword'), value('caPassword')


def pem_from_zip(client_bytes, ca_bytes, client_pw, ca_pw, stage: Path, legacy=False):
    # OpenSSL only receives the password on stdin. All processing in tmp mode 0700.
    p_client, p_ca = stage/'client.p12', stage/'ca.p12'
    p_client.write_bytes(client_bytes); p_ca.write_bytes(ca_bytes)
    p_client.chmod(0o600);p_ca.chmod(0o600)
    cr = pkcs12_openssl(p_client, client_pw, '-clcerts', '-nokeys', legacy=legacy)
    chain = pkcs12_openssl(p_client, client_pw, '-cacerts', '-nokeys', legacy=legacy)
    if b'-----BEGIN CERTIFICATE-----' in chain: cr += b'\n' + chain
    key = pkcs12_openssl(p_client, client_pw, '-nocerts', '-nodes', legacy=legacy)
    trust = pkcs12_openssl(p_ca, ca_pw, '-nokeys', legacy=legacy)
    if b'-----BEGIN CERTIFICATE-----' not in cr or b'PRIVATE KEY-----' not in key or b'-----BEGIN CERTIFICATE-----' not in trust:
        raise ValueError('PKCS12 inválido o incompleto (cliente, clave o CA)')
    for name, content in (('client.crt', cr), ('client.key', key), ('ca.pem', trust)):
        file = stage/name
        file.write_bytes(content)
        file.chmod(0o600)
    p_client.unlink(); p_ca.unlink()


def tls_test(config):
    sender = Sender(config, False)
    try:
        sender._connect()
        print('TLS verificado:', sender.sock.version(), 'servidor', config['tak']['host'])
    finally:
        sender.close()


def apply_transaction(project: Path, new_config: dict, stage: Path, restart: bool):
    dest = project/'tak_certs'
    cfg = project/'fpv_tak_config.json'
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    if dest.is_symlink() or cfg.is_symlink():
        raise ValueError('No se permite reemplazar configuración o carpeta de certificados simbólicos')
    dest.chmod(0o700)
    backups = dest/'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    backups.chmod(0o700)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    this_backup = backups/stamp
    this_backup.mkdir(mode=0o700)
    old_files = [cfg] + [dest/n for n in CERT_NAMES]
    backup_members = {}
    for f in old_files:
        if f.is_symlink(): raise ValueError('Archivo simbólico inesperado: ' + f.name)
        if f.exists():
            target = this_backup/('config.json' if f == cfg else f.name)
            shutil.copy2(f, target)
            target.chmod(0o600)
            backup_members[f] = target
    payload = json.dumps(new_config, indent=2, ensure_ascii=False) + '\n'
    config_stage = stage/'fpv_tak_config.new.json'
    config_stage.write_text(payload, encoding='utf8')
    config_stage.chmod(0o600)
    changed = []
    try:
        for name in CERT_NAMES:
            target = dest/name
            os.replace(stage/name, target)
            changed.append(target)
        os.replace(config_stage, cfg)
        changed.append(cfg)
        if restart:
            subprocess.run(['sudo', 'systemctl', 'restart', 'fpv-tak-sensor.service'], check=True)
    except BaseException:
        print('ERROR aplicando cambio: restaurando configuración anterior...', file=sys.stderr)
        for f in reversed(changed):
            if f in backup_members:
                shutil.copy2(backup_members[f], f)
                f.chmod(0o600)
            else: f.unlink(missing_ok=True)
        if restart:
            subprocess.run(['sudo', 'systemctl', 'restart', 'fpv-tak-sensor.service'], check=False)
        raise
    print('Configuración activada. Copia privada anterior:', this_backup)


def main():
    ap = argparse.ArgumentParser(description='FPV TAK: cargar SoftCert ATAK ZIP y configurar conexión TLS automáticamente')
    ap.add_argument('zip', type=Path, help='ZIP de ATAK: caCert.p12 + clientCert.p12 + config.pref')
    ap.add_argument('--project', type=Path, default=DEFAULT_PROJECT)
    ap.add_argument('--connection', type=int, default=None, help='Índice connectStringN, si hay varios servidores')
    ap.add_argument('--server-name', default=None, help='Nombre del certificado servidor si difiere del host en config.pref')
    ap.add_argument('--legacy', action='store_true', help='Permite PKCS12 antiguos de OpenSSL 3 (sin desactivar TLS)')
    ap.add_argument('--ask-passwords', action='store_true', help='Pide las contraseñas aunque estén en config.pref')
    ap.add_argument('--no-restart', action='store_true', help='No reiniciar el servicio al completar')
    args = ap.parse_args()
    os.umask(0o077)
    project = args.project.expanduser().resolve()
    cfg_file = project/'fpv_tak_config.json'
    if not cfg_file.is_file(): raise ValueError('Instala primero FPV TAK Sensor y configura la posición del equipo')
    cert_dir = project/'tak_certs'
    cert_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    cert_dir.chmod(0o700)
    old = json.loads(cfg_file.read_text(encoding='utf8'))
    client_data, ca_data, pref = read_zip(args.zip.expanduser())
    host, port, client_pw, ca_pw = parse_preferences(pref, args.connection)
    print(f'SoftCert ATAK detectado: servidor {host}:{port} TLS, sin configuración manual de red')
    if args.ask_passwords or not client_pw:
        client_pw = getpass.getpass('Contraseña clientCert.p12: ')
    if args.ask_passwords or not ca_pw:
        ca_pw = getpass.getpass('Contraseña caCert.p12 [Enter = misma]: ') or client_pw
    new = copy.deepcopy(old)
    new['tak'].update({'host': host, 'port': port, 'transport': 'tls',
                       'alert_visual_ring': True, 'alert_visual_radius_m': 75,
                       'adversary_uas_marker': True})
    new['tak']['tls'] = {
        'ca_file': str(project/'tak_certs/ca.pem'),
        'cert_file': str(project/'tak_certs/client.crt'),
        'key_file': str(project/'tak_certs/client.key'),
        'server_name': args.server_name or (
            old.get('tak', {}).get('tls', {}).get('server_name') if old.get('tak', {}).get('host') == host else None) or host,
    }
    # Only test the staged certs: a failed handshake never overwrites an existing live installation.
    with tempfile.TemporaryDirectory(prefix='.softcert-stage-', dir=project/'tak_certs') as d:
        stage = Path(d)
        stage.chmod(0o700)
        pem_from_zip(client_data, ca_data, client_pw, ca_pw, stage, args.legacy)
        staged = copy.deepcopy(new)
        staged['tak']['tls'].update({
            'ca_file': str(stage/'ca.pem'), 'cert_file': str(stage/'client.crt'),
            'key_file': str(stage/'client.key')})
        validate(staged)
        print('Verificando mTLS y certificado del servidor antes de activar...')
        tls_test(staged)
        validate(new)
        apply_transaction(project, new, stage, restart=not args.no_restart)
    print('Listo. Sensor/alertas usando TCP/TLS. Ver: journalctl -u fpv-tak-sensor -n 30')
    print('Nota: la posición preexistente se conservó; nunca se estima la posición del transmisor.')
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, RuntimeError, BadZipFile, ET.ParseError, subprocess.CalledProcessError) as e:
        print('ERROR:', str(e), file=sys.stderr)
        print('La configuración TAK existente no debería haberse alterado si falló la validación TLS.', file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print('Cancelado; cambios no aplicados.', file=sys.stderr)
        sys.exit(130)
