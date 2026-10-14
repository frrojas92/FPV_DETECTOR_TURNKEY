#!/usr/bin/env python3
"""FPV TAK V3.3.2: consola curses de configuración; Python stdlib.

NO modifica SDR/TFT/Web. Cambios de IP local se preparan para próxima
reconexión: jamás baja una interfaz de red en una sesión SSH.
"""
from __future__ import annotations
import copy
import curses
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile

PROJECT = Path.home() / 'detector_drone'
CONFIG = PROJECT / 'fpv_tak_config.json'
STATUS = Path('/dev/shm/fpv_tak_status.json')
BACKUPS = PROJECT / 'tak_tui_v33_backups' / 'config_edits'
SERVICE = 'fpv-tak-sensor.service'


def load():
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def validate_values(cfg):
    # import from the *installed* sensor; same validation service uses
    sys.path.insert(0, str(PROJECT))
    from fpv_tak_sensor import validate
    return validate(cfg)


def atomic_save(cfg):
    validate_values(cfg)
    if CONFIG.is_symlink():
        raise ValueError('Configuración enlazada mediante symlink: operación cancelada')
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(BACKUPS, 0o700)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    previous = BACKUPS / f'fpv_tak_config.{stamp}.json'
    shutil.copy2(CONFIG, previous)
    os.chmod(previous, 0o600)
    fd, tmp = tempfile.mkstemp(prefix='.tak-tui-', dir=PROJECT)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(cfg, out, indent=2, ensure_ascii=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return previous


def run_process(args, *, check=False):
    result = subprocess.run(args, check=False)
    if check and result.returncode:
        raise RuntimeError(f'Falló {args[0]} (código {result.returncode})')
    return result.returncode


def status_view():
    try:
        obj = json.loads(STATUS.read_text(encoding='utf-8'))
        return {k: obj.get(k) for k in ('position_source', 'position_valid', 'receiver_mode',
                'rf_persistent', 'tls_connected', 'last_error', 'updated_utc')}
    except (OSError, ValueError):
        return {}


def show_net():
    try:
        cp = subprocess.run(['ip','-br','-4','addr','show'], text=True, capture_output=True, timeout=3)
        return (cp.stdout.strip() or '(sin direcciones IPv4)')[:600]
    except (OSError, subprocess.TimeoutExpired):
        return 'No se pudo leer IPv4'


def nm_active_profile(iface):
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,32}', iface):
        raise ValueError('Interfaz inválida')
    cp = subprocess.run(['nmcli','-g','GENERAL.CONNECTION','device','show',iface],
                        capture_output=True, text=True, timeout=5)
    if cp.returncode or not cp.stdout.strip() or cp.stdout.strip() in ('--','(none)'):
        raise ValueError('NetworkManager no administra esa interfaz o no tiene perfil activo')
    return cp.stdout.strip().splitlines()[0]


def nm_stage_ipv4(iface, method, addr=None, gateway=None, dns=None):
    """Prepara perfil NetworkManager sin bajar/levantar enlace. No aplica en caliente."""
    if shutil.which('nmcli') is None:
        raise ValueError('NetworkManager/nmcli no disponible; IP solo lectura')
    conn = nm_active_profile(iface)
    if method not in ('auto','manual'):
        raise ValueError('Método IPv4 inválido')
    if method == 'manual':
        net = ipaddress.ip_interface(addr)
        if not isinstance(net, ipaddress.IPv4Interface):
            raise ValueError('Se requiere IPv4 CIDR, p.ej. 192.168.1.50/24')
        gw = ipaddress.ip_address(gateway)
        if not isinstance(gw, ipaddress.IPv4Address) or gw not in net.network:
            raise ValueError('Gateway debe estar dentro de la red IPv4 del equipo')
        clean_dns = []
        for value in (dns or '').split():
            parsed = ipaddress.ip_address(value)
            if not isinstance(parsed, ipaddress.IPv4Address):
                raise ValueError('Solo DNS IPv4')
            clean_dns.append(str(parsed))
        # DNS opcional para redes TAK aisladas, sin resolutores externos.
    else:
        clean_dns = []
    # Extraemos valores existentes para recuperación manual privada.
    cp = subprocess.run(['nmcli','-g','ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns',
                         'connection','show',conn], capture_output=True, text=True, timeout=5)
    if cp.returncode:
        raise RuntimeError('No se pudo respaldar perfil NetworkManager')
    backup_dir = PROJECT/'tak_tui_v33_backups'/'network'
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    bk = backup_dir/f'nm_{stamp}.json'
    bk.write_text(json.dumps({'profile':conn,'iface':iface,'values':cp.stdout.splitlines()}, indent=2))
    os.chmod(bk,0o600)
    if method == 'manual':
        cmd = ['sudo','nmcli','connection','modify',conn,
               'ipv4.method','manual', 'ipv4.addresses',str(net),
               'ipv4.gateway',str(gw),'ipv4.dns',' '.join(clean_dns)]
    else:
        cmd = ['sudo','nmcli','connection','modify',conn,
               'ipv4.method','auto','ipv4.addresses','',
               'ipv4.gateway','', 'ipv4.dns','']
    run_process(cmd, check=True)
    return conn, bk


def tls_verify(candidate):
    from fpv_tak_sensor import Sender
    s = Sender(candidate, dry_run=False)
    try:
        s._connect()
    finally:
        s.close()


def write_line(scr, y, txt, width, attr=0):
    h, _ = scr.getmaxyx()
    if 0 <= y < h-1:
        try: scr.addnstr(y,1,str(txt),max(0,width-2),attr)
        except curses.error: pass


def prompt(scr, question, default=''):
    h,w=scr.getmaxyx()
    scr.move(h-2,0); scr.clrtoeol()
    write_line(scr,h-2,question + (f' [{default}]' if default else '') + ': ',w)
    curses.echo();curses.curs_set(1)
    try:
        scr.move(h-1,1);scr.clrtoeol()
        raw=scr.getstr(h-1,1,max(1,min(200,w-3))).decode('utf8',errors='replace').strip()
    finally:
        curses.noecho();curses.curs_set(0)
    return raw or default


def pause(scr, msg='Enter para continuar'):
    h,w=scr.getmaxyx(); write_line(scr,h-2,msg,w,curses.A_BOLD);scr.getch()


def external(scr, command, label):
    curses.def_prog_mode()
    curses.endwin()
    print('\n*** '+label+' ***',flush=True)
    try:
        rc=run_process(command)
        print('Resultado:',rc)
    except Exception as e:
        print('ERROR:',e)
    input('\nPresiona Enter para volver al TUI...')
    curses.reset_prog_mode()
    scr.clear();scr.refresh()


def apply_change(scr, candidate, *, test_tls=False):
    try:
        validate_values(candidate)
        if test_tls:
            curses.def_prog_mode();curses.endwin()
            print('Verificando autenticación TLS antes de guardar...')
            try: tls_verify(candidate)
            finally: curses.reset_prog_mode();scr.clear();scr.refresh()
        before=atomic_save(candidate)
        external(scr,['sudo','systemctl','restart',SERVICE],f'Reiniciar TAK; respaldo: {before}')
    except Exception as exc:
        pause(scr,'ERROR: '+str(exc)[:100])


def choose_position(scr, cfg):
    mode=prompt(scr,'Posición: static o auto(GNSS)', cfg['position'].get('mode','static')).lower()
    if mode not in ('static','auto','gpsd'):
        return pause(scr,'Modo no permitido')
    cfg=copy.deepcopy(cfg)
    p=cfg['position']
    if mode=='static':
        try:
            lat=float(prompt(scr,'Latitud decimal',str(p.get('lat') or '')))
            lon=float(prompt(scr,'Longitud decimal',str(p.get('lon') or '')))
            from fpv_tak_sensor import valid_location
            if valid_location(lat,lon) is None: raise ValueError('Coordenadas inválidas')
            p.update(lat=lat,lon=lon,mode='static',fallback_static=False)
        except (ValueError,TypeError) as exc:
            return pause(scr,str(exc))
    else:
        p['mode']=mode;p['fallback_static']=False
    apply_change(scr,cfg)


def configure_net_ui(scr):
    if shutil.which('nmcli') is None:
        return pause(scr,'nmcli no instalado: consulta IPv4 solamente; no modificaré redes')
    iface=prompt(scr,'Interfaz, ej. eth0','eth0')
    method=prompt(scr,'Modo auto (DHCP) o manual (IP fija)','auto').lower()
    if method not in ('auto','manual'):
        return pause(scr,'Selecciona auto o manual')
    addr=gw=dns=None
    if method=='manual':
        addr=prompt(scr,'IPv4/prefijo (ej. 192.168.1.50/24)')
        gw=prompt(scr,'Gateway IPv4')
        dns=prompt(scr,'DNS IPv4 (opcional en red TAK aislada)','')
    text_confirm=prompt(scr,'La IP se aplicará al RECONECTAR la red, NO ahora. Escribe PREPARAR')
    if text_confirm!='PREPARAR':return pause(scr,'Operación cancelada')
    # sudo prompt can be interactive. Exit curses like all external operations.
    curses.def_prog_mode();curses.endwin()
    try:
        conn,bk=nm_stage_ipv4(iface,method,addr,gw,dns)
        print('Perfil preparado:',conn,'\nRespaldo privado:',bk)
        print('ATENCIÓN: Se aplicará solo al reactivar el perfil/reiniciar; SSH podría perder IP.')
    except Exception as exc:
        print('ERROR:',exc)
    input('\nEnter para volver...')
    curses.reset_prog_mode();scr.clear();scr.refresh()


def draw(scr):
    cfg=load();st=status_view(); h,w=scr.getmaxyx();scr.erase()
    if h<20 or w<66:
        write_line(scr,1,'Amplía el terminal (mínimo 66x20)',w,curses.A_BOLD)
        scr.refresh();return
    scr.bkgd(' ',0)
    write_line(scr,1,'FPV // CONFIGURACIÓN TAK                     SENSOR V3.3.2',w,curses.A_BOLD)
    write_line(scr,2,'='*(w-2),w,curses.A_DIM)
    name=cfg.get('callsign','?');host=cfg['tak'].get('host','?');port=cfg['tak'].get('port','?')
    write_line(scr,3,f'UNIDAD {name}     TAK TLS {host}:{port}',w)
    write_line(scr,4,f'ESTADO {"CONECTADO" if st.get("tls_connected") else "SIN SESIÓN TLS"}    POS {st.get("position_source","SIN ESTADO")}   RF {"ACTIVA" if st.get("rf_persistent") else "INACTIVA"}',w)
    write_line(scr,5,f'ALERTAS VOLÁTILES: {"SÍ" if cfg["tak"].get("volatile_alerts") else "NO"}   caducidad {cfg["tak"].get("alert_stale_s")}s / renovación {cfg["tak"].get("alert_repeat_s")}s   GeoChat {"ON" if cfg["tak"].get("geochat") else "OFF"}',w)
    write_line(scr,6,'RED IPv4 '+show_net().replace('\n','  |  ')[:w-13],w,curses.A_DIM)
    options=[
       '1  Importar SoftCert ZIP (servidor/certificados automáticos)',
       '2  Cambiar nombre / callsign de la unidad',
       '3  Cambiar servidor TAK IP/DNS y puerto TLS',
       '4  Configurar IPv4 de la Raspberry Pi (DHCP/estática; diferido)',
       '5  Coordenadas estáticas o GNSS gpsd',
       '6  Ajustar caducidad/renovación de alertas volátiles',
       '7  Comprobar conexión TLS (sin transmitir CoT)',
       '8  DESCONECTAR TAK (caducar alertas y detener servicio)',
       '9  CONECTAR TAK (activar servicio)',
       'L  Ver registros   R  Refrescar   Q  Salir'
    ]
    for y,line in enumerate(options,8):
        write_line(scr,y,line,w,curses.A_BOLD if line.startswith(('8  ','9  ')) else 0)
    write_line(scr,h-3,'NOTA: pérdida total de red impide borrado inmediato; expiración <= TTL.',w,curses.A_DIM)
    scr.refresh()


def main(scr):
    curses.curs_set(0)
    while True:
        try: draw(scr)
        except Exception as exc: return pause(scr,'ERROR leyendo estado/config: '+str(exc))
        c=scr.getch()
        if c in (ord('q'),ord('Q')):break
        if c in (ord('r'),ord('R'),curses.KEY_RESIZE):continue
        if c in (ord('1'),):
            path=prompt(scr,'Ruta completa ZIP SoftCert (secreto: no subir a Git)')
            if path:
                p=Path(path).expanduser()
                if p.is_file():
                    flag=prompt(scr,'Activar compatibilidad OpenSSL PKCS12 --legacy? (s/N)','N')
                    legacy=flag.strip().lower() in ('s','si','sí','y','yes')
                    cmd=['python3',str(PROJECT/'cargar_softcert.py'),str(p)]
                    if legacy:
                        cmd.append('--legacy')
                    external(scr,cmd,'Importación SoftCert con verificación mTLS')
                else:pause(scr,'No existe el archivo ZIP')
        elif c==ord('2'):
            cfg=copy.deepcopy(load());name=prompt(scr,'Nombre visible TAK, máximo 40',cfg['callsign'])
            if not (1<=len(name)<=40) or any(x in name for x in '\r\n<>'):
                pause(scr,'Nombre no válido')
            else:
                cfg['callsign']=name;apply_change(scr,cfg)
        elif c==ord('3'):
            cfg=copy.deepcopy(load());tak=cfg['tak']
            host=prompt(scr,'Servidor TAK (IP o DNS)',tak['host'])
            raw=prompt(scr,'Puerto TLS',str(tak['port']))
            try:
                port=int(raw)
                if not (1<=port<=65535) or not host or '\n' in host or '/' in host or ' ' in host:
                    raise ValueError('Host/puerto inválidos')
                tak['host']=host;tak['port']=port
                srv=prompt(scr,'Nombre TLS en certificado del servidor (DNS)',tak.get('tls',{}).get('server_name') or host)
                tak.setdefault('tls',{})['server_name']=srv
                apply_change(scr,cfg,test_tls=True)
            except ValueError as exc:pause(scr,str(exc))
        elif c==ord('4'):
            configure_net_ui(scr)
        elif c==ord('5'):
            choose_position(scr,load())
        elif c==ord('6'):
            cfg=copy.deepcopy(load());tak=cfg['tak']
            try:
                ttl=int(prompt(scr,'Caducidad sin conexión (6–30 s)',str(tak.get('alert_stale_s',12))))
                every=int(prompt(scr,'Renovación de alertas (1–10 s)',str(tak.get('alert_repeat_s',3))))
                tak.update(volatile_alerts=True,alert_stale_s=ttl,alert_repeat_s=every,geochat=False)
                apply_change(scr,cfg)
            except ValueError as exc:pause(scr,'Valores inválidos: '+str(exc))
        elif c==ord('7'):
            cfg=load()
            curses.def_prog_mode();curses.endwin()
            try:
                tls_verify(cfg)
                print('OK: verificación mTLS exitosa (sin eventos CoT).')
            except Exception as exc: print('FALLO TLS:',exc)
            input('\nEnter para continuar...')
            curses.reset_prog_mode();scr.clear();scr.refresh()
        elif c==ord('8'):
            external(scr,['sudo','systemctl','stop',SERVICE], 'Apagado ordenado: CoT stale inmediatamente si sigue conectado')
        elif c==ord('9'):
            external(scr,['sudo','systemctl','start',SERVICE], 'Conectar al TAK Server')
        elif c in (ord('l'),ord('L')):
            external(scr,['journalctl','-u',SERVICE,'-n','40','--no-pager'], 'Últimos registros del puente')


if __name__=='__main__':
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit('Se requiere terminal interactiva SSH (no ejecutar desde cron).')
    if not CONFIG.is_file(): sys.exit('No existe '+str(CONFIG))
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
