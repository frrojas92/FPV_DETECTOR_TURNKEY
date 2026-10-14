#!/usr/bin/env python3
"""TAK sidecar for FPV V19-E. Python stdlib only; does not access the SDR.

CoT output is NOT a drone geolocation: RF evidence is anchored at sensor position.
"""
import argparse
import datetime as dt
import ipaddress
import json
import math
import os
import signal
import threading
from pathlib import Path
import socket
import ssl
import sys
import time
import uuid
import xml.etree.ElementTree as ET

SENSOR_TYPE = 'a-f-G-E-S-E'
SPOT_TYPE = 'b-m-p-s-m'
RING_TYPE = 'u-d-c-c'  # CoT shape, purely visual. Not an RF range estimate.
# MIL-STD-2525/CoT: hostile air, military rotary-wing unmanned platform.
# IMPORTANT: RF alone does NOT verify an aircraft, its affiliation, type or location.
HOSTILE_UAS_TYPE = 'a-h-A-M-H-Q'
UNKNOWN = '9999999.0'


def utc(offset=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def finite(value, lo, hi):
    try:
        x = float(value)
        return x if math.isfinite(x) and lo <= x <= hi else None
    except (ValueError, TypeError, OverflowError):
        return None


def valid_location(lat, lon, hae=None, how='h-e'):
    lat, lon = finite(lat, -90, 90), finite(lon, -180, 180)
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    return {'lat': lat, 'lon': lon, 'hae': finite(hae, -1000, 100000), 'how': how}


def distance_m(first, second):
    """Haversine sensor movement in metres, used only for updating its own CoT marker."""
    if not first or not second:
        return math.inf
    a, b = math.radians(first['lat']), math.radians(second['lat'])
    dlat = b - a
    dlon = math.radians(second['lon'] - first['lon'])
    h = math.sin(dlat / 2)**2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2)**2
    return 12742000.0 * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def location_changed(current, last_sent, threshold_m=5):
    return distance_m(current, last_sent) >= float(threshold_m)


def event_xml(uid, cot_type, loc, stale_seconds, remarks, build_detail=None):
    now = utc()
    root = ET.Element('event', {'version': '2.0', 'uid': uid, 'type': cot_type,
                                'how': loc['how'], 'time': now, 'start': now,
                                'stale': utc(stale_seconds)})
    ET.SubElement(root, 'point', {
        'lat': f"{loc['lat']:.7f}", 'lon': f"{loc['lon']:.7f}",
        'hae': f"{loc['hae']:.2f}" if loc['hae'] is not None else UNKNOWN,
        'ce': UNKNOWN, 'le': UNKNOWN})
    detail = ET.SubElement(root, 'detail')
    if build_detail:
        build_detail(detail, now)
    if remarks is not None:
        ET.SubElement(detail, 'remarks').text = remarks
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def sensor_cot(cfg, loc, active, mode, region, expire=False):
    tak = cfg['tak']
    name = cfg['callsign']
    status = ('APAGADO / SIN POSICION GNSS' if expire else 'RF PERSISTENTE' if active else
              'VIDEO / ESCANEO PAUSADO' if mode == 'VIDEO' else 'SIN RF PERSISTENTE')
    desc = (f"{name} | Sensor RF omnidireccional 360 grados | {status}. "
            "Posicion del sensor; NO localiza el transmisor ni estima distancia.")
    def details(d, _now):
        ET.SubElement(d, 'contact', {'callsign': name})
        ET.SubElement(d, 'status', {'readiness': str(not expire).lower()})
        s = {'azimuth': '0', 'fov': '360', 'model': 'RF FPV 5.8 GHz isotropico'}
        range_m = finite(tak.get('display_range_m'), 1, 100000)
        if range_m is not None:
            s['range'] = str(range_m)
        ET.SubElement(d, 'sensor', s)
        ET.SubElement(d, '_fpv_rf_sensor', {'state': 'OFFLINE' if expire else 'PERSISTENT_RF' if active else 'IDLE',
                                            'coverage': '360', 'direction_known': 'false',
                                            'range_known': 'false'})
    return event_xml(cfg['uid'], SENSOR_TYPE, loc, -1 if expire else tak['sensor_stale_s'], desc, details)


def rf_summary(region):
    if not region:
        return 'RF persistente detectada'
    freq = finite(region.get('frequency_mhz'), 0, 100000)
    db = finite(region.get('relative_db'), -200, 200)
    score = region.get('score')
    band = str(region.get('band', ''))[:4]
    channel = region.get('channel')
    channel_text = f'{band}{channel}' if band and isinstance(channel, int) else str(region.get('id','CANAL'))[:16]
    return (f"RF PERSISTENTE {channel_text} " + (f"{freq:.3f} MHz " if freq is not None else '')
            + (f"RF relativa {db:+.1f} dB " if db is not None else '')
            + (f"evidencia {score}/100 " if isinstance(score, (int, float)) else '')
            + '| No confirma dron ni ubicacion del emisor')


def spot_cot(cfg, loc, region, expire=False):
    # One fixed UID: prevents a new marker for each RF event.
    msg = rf_summary(region) if not expire else 'RF persistente finalizada; no hay confirmacion reciente'
    def details(d, _now):
        ET.SubElement(d, 'contact', {'callsign': '!! RF PERSISTENTE !! ' + cfg['callsign'] if not expire else 'RF FIN ' + cfg['callsign']})
        ET.SubElement(d, 'status', {'readiness': str(not expire).lower()})
        color = '-65536'  # red, CoT spot-map palette
        ET.SubElement(d, 'color', {'argb': color})
        ET.SubElement(d, 'usericon', {'iconsetpath': 'COT_MAPPING_SPOTMAP/b-m-p-s-m/' + color})
        ET.SubElement(d, 'link', {'uid': cfg['uid'], 'type': SENSOR_TYPE, 'relation': 'p-p'})
        if region:
            p = {'status': 'PERSISTENT_RF' if not expire else 'CLEARED'}
            for key in ('frequency_mhz', 'band', 'channel', 'score', 'relative_db', 'occupancy'):
                if key in region and region[key] is not None:
                    p[key] = str(region[key])[:32]
            ET.SubElement(d, '_fpv_rf', p)
    return event_xml(cfg['uid'] + '-RF-ALERT', SPOT_TYPE, loc,
                     -1 if expire else cfg['tak']['alert_stale_s'], msg, details)


def warning_ring_cot(cfg, loc, region, expire=False):
    """ATAK drawing circle: purely a UI attention indicator, never estimated RF range."""
    tak = cfg['tak']
    # A small radius used only to make an on-map attention highlight visible.
    # Do not reuse for operational distances or a sensor field-of-view estimate.
    radius_m = finite(tak.get('alert_visual_radius_m', 75), 15, 500)
    radius_m = radius_m if radius_m is not None else 75
    msg = ('ALERTA RF PERSISTENTE - indicador visual, radio NO es distancia de deteccion. '
           'La ubicacion corresponde solo al sensor. ' + rf_summary(region)) if not expire else 'Alerta RF finalizada'
    def details(d, _now):
        ET.SubElement(d, 'contact', {'callsign': '!! ALERTA RF !!' if not expire else 'ALERTA RF FIN'})
        shape = ET.SubElement(d, 'shape')
        ET.SubElement(shape, 'ellipse', {'major': str(radius_m), 'minor': str(radius_m), 'angle': '0'})
        ET.SubElement(d, 'strokeColor', {'value': '-65536'})  # bright red
        ET.SubElement(d, 'strokeWeight', {'value': '5.0'})
        ET.SubElement(d, 'fillColor', {'value': '419364864'})  # 0x18FF0000 (9% red)
        ET.SubElement(d, 'link', {'uid': cfg['uid'], 'type': SENSOR_TYPE, 'relation': 'p-p'})
        ET.SubElement(d, '_fpv_visual', {'purpose': 'ALERT_HIGHLIGHT',
                    'not_a_range_estimate': 'true', 'not_a_transmitter_position': 'true'})
    return event_xml(cfg['uid'] + '-RF-VISUAL-RING', RING_TYPE, loc,
                     -1 if expire else tak['alert_stale_s'], msg, details)



def adversary_uas_cot(cfg, loc, region, expire=False):
    """NATO-style *provisional* hostile UAV symbol tied to RF alarm.

    CoT demands a point. Its coordinates are solely the known SENSOR coordinates;
    this is NOT an aircraft geolocation or a measured air track. Label and custom
    metadata explicitly state that fact. ce/le/hae remain unknown.

    Classifying hostile and rotary-wing is a user-requested visualization policy;
    neither attribute is established by RF evidence alone.
    """
    callsign = ('RF FIN - UAS SIN POS' if expire else 'UAS ADV? SIN POS (RF)')
    body = ('RF persistente FINALIZADA. Icono OTAN provisional caducado.' if expire else
            'UAS adversario PRESUNTO, NO GEOLOCALIZADO; simbolo OTAN ' +
            'configurado por el operador, NO confirma dron, hostilidad ni ala rotatoria. ' +
            'El punto dibujado corresponde SOLO AL SENSOR, NO a la aeronave. ' +
            rf_summary(region))
    def detail(d, _now):
        ET.SubElement(d, 'contact', {'callsign': callsign})
        ET.SubElement(d, 'link', {'uid': cfg['uid'], 'type': SENSOR_TYPE, 'relation': 'p-p'})
        ET.SubElement(d, '_fpv_rf_hypothesis', {
            'source': 'PERSISTENT_RF',
            'position_status': 'NOT_GEOLOCATED',
            'point_represents': 'SENSOR_LOCATION_ONLY',
            'identification_confirmed': 'false',
            'hostile_affiliation_confirmed': 'false',
            'airframe_type_confirmed': 'false',
            'operator_visual_assessment': 'ADVERSARY_UAS',
            'expires_after_rf': 'true'})
    # h-e rather than m-g: these are not reported aircraft GPS coordinates.
    borrowed_sensor_coords = dict(loc, how='h-e', hae=None)
    return event_xml(cfg['uid'] + '-RF-UAS-HYPOTHESIS', HOSTILE_UAS_TYPE,
                     borrowed_sensor_coords,
                     -1 if expire else cfg['tak']['alert_stale_s'], body, detail)

def geochat_cot(cfg, loc, region, simulated=False):
    name, sender = cfg['callsign'], cfg['uid']
    room = cfg['tak'].get('chat_room', 'All Chat Rooms')
    mid = str(uuid.uuid4())
    msg = ('[PRUEBA] ' if simulated else '[ALERTA RF] ') + name + ': ' + rf_summary(region)
    def details(d, now):
        chat = ET.SubElement(d, '__chat', {'parent': 'RootContactGroup',
                       'id': room, 'chatroom': room, 'groupOwner': 'false',
                       'senderCallsign': name, 'messageId': mid})
        ET.SubElement(chat, 'chatgrp', {'uid0': sender, 'uid1': room, 'id': room})
        ET.SubElement(d, 'link', {'uid': sender, 'type': SENSOR_TYPE, 'relation': 'p-p'})
        ET.SubElement(d, '_fpv_alert', {'severity': 'RF_EVIDENCE', 'event': 'PERSISTENT_RF'})
        ET.SubElement(d, 'remarks', {'source': 'BAO.F.ATAK.' + sender, 'to': room, 'time': now}).text = msg
    # remarks already included inside detail with attrs; avoid a duplicate empty remarks tag.
    return event_xml('GeoChat.' + sender + '.' + room.replace(' ', '_') + '.' + mid,
                     'b-t-f', loc, 86400, None, details)


def persistent_region(snapshot, max_age=2.5):
    """Requires fresh actual RF evidence from V19-E SCAN snapshot; no fake detections in VIDEO."""
    if not isinstance(snapshot, dict) or not snapshot.get('running'):
        return None
    t = finite(snapshot.get('updated_epoch'), 1, 10**12)
    if t is None or abs(time.time() - t) > 3.0:
        return None
    if snapshot.get('receiver_mode') != 'SCAN' or snapshot.get('overall_state') != 'PERSISTENT_RF':
        return None
    regions = snapshot.get('regions')
    if not isinstance(regions, list):
        return None
    regions = [r for r in regions if isinstance(r, dict) and r.get('status') == 'PERSISTENT_RF'
               and r.get('age_s') is not None and finite(r.get('age_s'), 0, max_age) is not None]
    if not regions:
        return None
    return max(regions, key=lambda r: (finite(r.get('score'), 0, 100) or 0,
                                        finite(r.get('relative_db'), -200, 200) or -200))


def read_state(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf8'))
    except (OSError, ValueError):
        return {}


class GpsdReceiver:
    """Non-blocking gpsd TCP WATCH TPV reader; disconnected receivers are retried."""
    def __init__(self, host, port, max_age):
        self.host, self.port, self.max_age = host, int(port), float(max_age)
        self.sock = None
        self.buf = b''
        self.last_position = None
        self.last_time = 0.0
        self.next_retry = 0.0

    def close(self):
        if self.sock:
            try: self.sock.close()
            except OSError: pass
        self.sock = None

    def read(self):
        now = time.monotonic()
        if self.sock is None and now >= self.next_retry:
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=0.3)
                self.sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
                self.sock.setblocking(False)
                self.buf = b''
            except OSError:
                self.close()
                self.next_retry = now + 3
        if self.sock:
            try:
                for _ in range(25):
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise OSError('gpsd disconnected')
                    self.buf += chunk
                    if len(self.buf) > 65536:
                        self.buf = self.buf[-8192:]
                    while b'\n' in self.buf:
                        line, self.buf = self.buf.split(b'\n', 1)
                        try: data = json.loads(line)
                        except ValueError: continue
                        if data.get('class') == 'TPV':
                            if int(data.get('mode') or 0) < 2:
                                # Fix lost: stop publishing GNSS immediately.
                                self.last_position = None
                                self.last_time = 0.0
                                continue
                            loc = valid_location(data.get('lat'), data.get('lon'),
                                                 data.get('altHAE'), how='m-g')
                            if loc:
                                self.last_position = loc
                                self.last_time = time.monotonic()
            except BlockingIOError:
                pass
            except (OSError, ValueError, TypeError):
                self.close()
                self.next_retry = now + 3
        return self.last_position if (self.last_position and now - self.last_time < self.max_age) else None


class Sender:
    """Transport CoT via LAN UDP or persistent mutual-TLS TCP.

    A separate process owns this connection. It never opens the HackRF.
    """
    def __init__(self, cfg, dry_run):
        self.cfg = cfg
        self.dry_run = dry_run
        self.transport = cfg['tak'].get('transport', 'udp').lower()
        self.sock = None
        self.context = None
        self.next_retry = 0.0
        self.connected_at = None
        if self.dry_run:
            return
        if self.transport == 'udp':
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            addr = ipaddress.ip_address(cfg['tak']['host']) if self._is_ip(cfg['tak']['host']) else None
            if addr is not None and addr.is_multicast:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                                     int(cfg['tak'].get('multicast_ttl', 1)))
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                if cfg['tak'].get('interface_ip'):
                    self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(cfg['tak']['interface_ip']))
        elif self.transport == 'tls':
            conf = cfg['tak']['tls']
            self.context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH,
                                                      cafile=conf['ca_file'])
            self.context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.context.load_cert_chain(conf['cert_file'], conf['key_file'])
            # Verification and hostname checking are always ON; never fall back insecurely.
            self.context.verify_mode = ssl.CERT_REQUIRED
            self.context.check_hostname = True
        else:
            raise ValueError('Transporte TAK desconocido: ' + self.transport)

    @staticmethod
    def _is_ip(value):
        try: ipaddress.ip_address(value); return True
        except ValueError: return False

    def close(self):
        if self.sock:
            try: self.sock.close()
            except OSError: pass
        self.sock = None
        self.connected_at = None

    def _connect(self):
        if time.monotonic() < self.next_retry:
            raise ConnectionError('Reconexión TAK TLS en espera')
        host = self.cfg['tak']['host']
        port = int(self.cfg['tak']['port'])
        server_name = self.cfg['tak']['tls'].get('server_name') or host
        raw = None
        try:
            raw = socket.create_connection((host, port), timeout=5)
            self.sock = self.context.wrap_socket(raw, server_hostname=server_name)
            self.sock.settimeout(5)
            self.connected_at = time.monotonic()
            self.next_retry = 0.0
            print(f'TAK TLS: conectado a {host}:{port} (nombre verificado: {server_name})', flush=True)
        except (OSError, ValueError):
            if raw is not None:
                try: raw.close()
                except OSError: pass
            self.close()
            self.next_retry = time.monotonic() + 3.0
            raise

    def send(self, packet):
        if self.dry_run:
            sys.stdout.write(packet.decode() + '\n')
            return
        if self.transport == 'udp':
            self.sock.sendto(packet, (self.cfg['tak']['host'], int(self.cfg['tak']['port'])))
            return
        if not self.sock:
            self._connect()
        try:
            # TAK CoT XML is sent over a TLS byte stream; newline-delimited events.
            self.sock.sendall(packet + b'\n')
        except (OSError, ssl.SSLError):
            self.close()
            self.next_retry = time.monotonic() + 3.0
            raise


def validate(cfg):
    for key in ('uid', 'callsign', 'tak', 'position', 'input'):
        if key not in cfg: raise ValueError('Falta campo de configuración: ' + key)
    if cfg['position']['mode'] not in ('static', 'gpsd', 'auto', 'disabled'):
        raise ValueError('Modo posición inválido')
    if not (1 <= int(cfg['tak']['port']) <= 65535):
        raise ValueError('Puerto TAK inválido')
    transport = cfg['tak'].get('transport', 'udp').lower()
    if transport not in ('udp', 'tls'):
        raise ValueError('Transporte TAK debe ser udp o tls')
    if transport == 'tls':
        tls = cfg['tak'].get('tls', {})
        for required in ('ca_file', 'cert_file', 'key_file'):
            if not tls.get(required):
                raise ValueError('TLS requiere tak.tls.' + required)
        if not cfg['tak']['host'] or cfg['tak']['host'].startswith('239.'):
            raise ValueError('TLS requiere un servidor TAK real, no multicast')
    if cfg['position']['mode'] == 'static':
        loc = valid_location(cfg['position'].get('lat'), cfg['position'].get('lon'), cfg['position'].get('hae'))
        if not loc:
            raise ValueError('Coordenadas estáticas inválidas o no configuradas; no se enviará 0,0')
    for key, fallback in (('heartbeat_s', 10), ('sensor_stale_s', 45),
                          ('alert_repeat_s', 15), ('alert_stale_s', 35)):
        if float(cfg['tak'].get(key, fallback)) <= 0:
            raise ValueError('Intervalo TAK inválido: ' + key)
    if cfg['tak'].get('volatile_alerts', False):
        rep = float(cfg['tak'].get('alert_repeat_s', 3))
        ttl = float(cfg['tak'].get('alert_stale_s', 12))
        if rep < 1 or rep > 10 or ttl < max(6, rep * 2) or ttl > 30:
            raise ValueError('Alertas volátiles: repetición 1–10 s, TTL entre max(6,2*repetición) y 30 s')
        if cfg['tak'].get('geochat', False):
            raise ValueError('GeoChat no puede estar activo en modo totalmente volátil: permanece en historial')
    if finite(cfg['position'].get('movement_threshold_m', 5), 0.5, 1000) is None:
        raise ValueError('position.movement_threshold_m inválido (0.5 a 1000)')
    if cfg['position']['mode'] == 'auto' and cfg['position'].get('fallback_static', False):
        if not valid_location(cfg['position'].get('lat'), cfg['position'].get('lon'),
                              cfg['position'].get('hae')):
            raise ValueError('fallback_static requiere coordenadas estáticas válidas')
    return cfg


def expire_markers(sender, cfg, loc, region, *, active, expire_sensor=True, fast=False):
    '''Best-effort fast CoT expiry; never GeoChat or synthetic coordinates.

    The caller must already have a valid last-known SENSOR location. At shutdown
    never initiate a new TLS session: stale TTL still works if TAK is offline.
    '''
    if not loc or (sender.transport == 'tls' and not sender.dry_run and sender.sock is None):
        return
    packets = []
    # Legacy SpotMap red pin is never published in active form by V3.3.1.
    # Send its expiry for compatibility with V3.3 maps on normal shutdown.
    # Circle and NATO hypothesis retain their original stable UIDs.
    # CoT con stale en el pasado: no crea un objetivo activo.
    packets.append(spot_cot(cfg, loc, region, expire=True))
    packets.append(warning_ring_cot(cfg, loc, region, expire=True))
    packets.append(adversary_uas_cot(cfg, loc, region, expire=True))
    if expire_sensor:
        packets.append(sensor_cot(cfg, loc, False, 'OFFLINE', region, expire=True))
    # Bounded exit. If there is no network, rely on the normal stale TTL.
    for pkt in packets:
        original_timeout = None
        try:
            if fast and sender.sock is not None:
                original_timeout = sender.sock.gettimeout()
                sender.sock.settimeout(0.9)
            sender.send(pkt)
        except (OSError, ConnectionError) as exc:
            print('TAK: no se pudo emitir caducidad inmediata; se aplicará TTL:', exc, flush=True)
            break
        finally:
            if original_timeout is not None and sender.sock is not None:
                try:
                    sender.sock.settimeout(original_timeout)
                except OSError:
                    pass


def run(cfg, *, dry_run=False, once=False, simulate=False, stopping=None):
    stopping = stopping if stopping is not None else threading.Event()
    sender = Sender(cfg, dry_run)
    pos_cfg = cfg['position']
    mode_cfg = pos_cfg['mode']
    gps = (GpsdReceiver(pos_cfg.get('gpsd_host', '127.0.0.1'),
                        pos_cfg.get('gpsd_port', 2947),
                        pos_cfg.get('gpsd_max_age_s', 8))
           if mode_cfg in ('gpsd', 'auto') else None)
    static_loc = valid_location(pos_cfg.get('lat'), pos_cfg.get('lon'),
                                pos_cfg.get('hae'), 'h-e')
    last_sensor = 0.0
    last_marker = 0.0
    last_chat = -1e10
    chat_pending = False
    active = False
    last_region = None
    last_position = None      # only a valid latest position, never a drone location
    last_sent_position = None
    last_source = None
    last_mode = 'INACTIVO'
    # Do not display the legacy red SpotMap marker. This one-time event clears
    # its previous UID on upgraded units, using the real sensor position.
    legacy_spot_clear_pending = True
    last_error = ''
    status_path = Path('/dev/shm/fpv_tak_status.json')
    count = 0
    poll = max(0.1, float(cfg['input'].get('poll_seconds', 0.25)))
    move_threshold = float(pos_cfg.get('movement_threshold_m', 5))
    print('TAK V3.3.1: posición', mode_cfg, '| UID estable:', cfg['uid'],
          '| alerta volátil:', bool(cfg['tak'].get('volatile_alerts', False)), flush=True)
    try:
        while not stopping.is_set():
            snap = ({'running': True, 'updated_epoch': time.time(), 'receiver_mode': 'SCAN',
                     'overall_state': 'PERSISTENT_RF', 'regions': [
                         {'status': 'PERSISTENT_RF', 'age_s': 0.1, 'band': 'R', 'channel': 8,
                          'frequency_mhz': 5917, 'score': 98, 'relative_db': 13.0}]}
                    if simulate else read_state(cfg['input']['state_file']))
            region = persistent_region(snap, float(cfg['input'].get('max_rf_age_s', 2.5)))
            now = time.monotonic()
            gnss_loc = gps.read() if gps else None
            if mode_cfg == 'static':
                loc, source = static_loc, 'STATIC'
            elif mode_cfg in ('gpsd', 'auto') and gnss_loc:
                loc, source = gnss_loc, 'GNSS'
            elif mode_cfg == 'auto' and pos_cfg.get('fallback_static', False) and static_loc:
                loc, source = static_loc, 'STATIC_FALLBACK'
            else:
                loc, source = None, 'NO_FIX'
            current_mode = str(snap.get('receiver_mode', 'UNKNOWN'))
            changed = (loc is not None and
                       (location_changed(loc, last_sent_position, move_threshold) or source != last_source))
            try:
                if loc:
                    # Clear the V3.3 red SpotMap UID once, even without an active RF
                    # candidate. An expired event cannot create an active marker.
                    if legacy_spot_clear_pending:
                        sender.send(spot_cot(cfg, loc, None, expire=True))
                        legacy_spot_clear_pending = False
                    # A verified sensor coordinate; same CoT UID after movement.
                    rising = region is not None and not active
                    if region is None and active:
                        expire_markers(sender, cfg, loc, last_region, active=True, expire_sensor=False)
                        active = False
                        chat_pending = False
                    elif region is not None:
                        active = True
                        if rising:
                            chat_pending = True
                        last_region = region
                        if rising or changed or now - last_marker >= float(cfg['tak']['alert_repeat_s']):
                            # Only circle + provisional NATO UAV; NO red RF pin.
                            if cfg['tak'].get('alert_visual_ring', True):
                                sender.send(warning_ring_cot(cfg, loc, region))
                            if cfg['tak'].get('adversary_uas_marker', True):
                                sender.send(adversary_uas_cot(cfg, loc, region))
                            last_marker = now
                        if (chat_pending and cfg['tak'].get('geochat', True)
                                and now - last_chat >= float(cfg['tak'].get('chat_cooldown_s', 30))):
                            sender.send(geochat_cot(cfg, loc, region, simulated=simulate))
                            last_chat = now
                            chat_pending = False
                    if changed or now - last_sensor >= float(cfg['tak']['heartbeat_s']) or current_mode != last_mode:
                        sender.send(sensor_cot(cfg, loc, active, current_mode, region))
                        last_sensor = now
                        last_sent_position = loc
                        last_source = source
                    last_error = ''
                else:
                    if last_position is not None:
                        # GPS lost: expire on last-known sensor coordinate, but do not
                        # keep advertising that stale location as a live sensor.
                        expire_markers(sender, cfg, last_position, last_region,
                                       active=active, expire_sensor=True, fast=True)
                        print('TAK: posición GNSS perdida, marcadores caducados', flush=True)
                        active = False
                        chat_pending = False
                        last_sent_position = None
                        last_source = None
                    last_error = 'SIN POSICION VALIDA: no se transmiten eventos activos'
                last_mode = current_mode
                last_position = loc
            except (OSError, ConnectionError) as exc:
                last_error = f'Error transporte TAK: {exc}'
                print(last_error, flush=True) if count % max(1, int(20/poll)) == 0 else None
            if not dry_run:
                try:
                    data = {'updated_utc': utc(), 'sensor_uid': cfg['uid'],
                            'position_mode': mode_cfg, 'position_source': source,
                            'position_valid': loc is not None,
                            'latitude': loc['lat'] if loc else None,
                            'longitude': loc['lon'] if loc else None,
                            'gpsd_connected': gps.sock is not None if gps else None,
                            'rf_persistent': active, 'receiver_mode': current_mode,
                            'adversary_uas_hypothesis': bool(active and cfg['tak'].get('adversary_uas_marker', True)),
                            'last_error': last_error, 'tak_host': cfg['tak']['host'],
                            'tak_port': cfg['tak']['port'], 'transport': sender.transport,
                            'tls_connected': sender.sock is not None if sender.transport == 'tls' else None}
                    tmp = status_path.with_name(status_path.name + '.' + str(os.getpid()) + '.tmp')
                    tmp.write_text(json.dumps(data), encoding='utf8'); os.replace(tmp, status_path)
                except OSError:
                    pass
            if last_error and (count % max(1, int(60/poll)) == 0):
                print(last_error, flush=True)
            if once:
                break
            count += 1
            stopping.wait(poll)  # SIGTERM interruptible, unlike time.sleep()
    finally:
        if stopping.is_set() and not once:
            print('TAK: apagado ordenado; intentando caducar marcadores', flush=True)
            expire_markers(sender, cfg, last_position, last_region,
                           active=active, expire_sensor=True, fast=True)
        if gps:
            gps.close()
        sender.close()

def main():
    p = argparse.ArgumentParser(description='Puente TAK RF omnidireccional, V19-E RAMTEST, sin tocar SDR')
    p.add_argument('--config', default='/home/cetel/detector_drone/fpv_tak_config.json')
    p.add_argument('--dry-run', action='store_true', help='muestra XML, no envía')
    p.add_argument('--once', action='store_true', help='una sola iteración')
    p.add_argument('--simulate', action='store_true', help='solo con --dry-run (prueba sintética)')
    args = p.parse_args()
    if args.simulate and not args.dry_run: p.error('Simulación solo autorizada con --dry-run')
    try:
        cfg = validate(json.loads(Path(args.config).read_text(encoding='utf8')))
    except (ValueError, OSError, KeyError) as e:
        p.exit(2, f'Configuración no válida: {e}\n')
    stopping = threading.Event()
    def request_stop(_signum, _frame):
        stopping.set()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run(cfg, dry_run=args.dry_run, once=args.once, simulate=args.simulate,
        stopping=stopping)

if __name__ == '__main__': main()
