#!/usr/bin/env python3
"""ILI9341 240x320 + XPT2046 touch for FPV detector V6/V7.

TFT wiring remains unchanged: CS=D5, DC=D25, RST=D24, SPI0 MOSI/MISO/SCLK.
TOUCH additions: T_CLK=GPIO11, T_DIN=GPIO10, T_DO=GPIO9,
                 T_CS=GPIO7/CE1, T_IRQ=GPIO4 (all 3.3V logic).

Four display pages: main, RF regions, region details, and channel table.
Touch MUTE writes one local JSON command; RF detector decides and acknowledges
by its own audio_enabled state. No hackrf_sweep or RF-processing code here.
"""
import argparse
import json
import os
import statistics
import signal
import select
import sys
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from collections import deque
from fpv_spectrum_v17 import palette_rgb, downsample
from fpv_video_transport_v181 import read_frame as read_raw_frame, default_path as raw_default_path
from fpv_color_transport_v182 import read_color as read_rgb_frame

HERE = Path(__file__).resolve().parent
W, H = 240, 320
BANDS = {
    'A': [5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725],
    'B': [5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866],
    'E': [5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945],
    'F': [5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880],
    'R': [5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917]
}
BAND_NAMES = list(BANDS)
BG = (12, 20, 29)
PANEL = (23, 36, 48)
GRAY = (132, 153, 169)
WHITE = (232, 239, 242)
CYAN = (95, 220, 211)
YELLOW = (255, 200, 88)
RED = (255, 108, 105)
GREEN = (93, 205, 151)


def font(size, bold=False):
    name = ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
            else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()

F10 = font(10)
F11 = font(11)
F12 = font(12)
F14 = font(14, True)
F17 = font(17, True)
F22 = font(22, True)
F40 = font(40, True)


def read_status(path, max_age=2.0):
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(obj, dict) or obj.get('schema') != 1:
            raise ValueError('invalid schema')
    except (OSError, ValueError, TypeError):
        return {'overall_state': 'NO_DATA', 'primary': None, 'top': []}
    if not obj.get('running', False):
        return {'overall_state': 'STOPPED', 'primary': None, 'top': []}
    if abs(time.time() - float(obj.get('updated_epoch', 0))) > max_age:
        return {'overall_state': 'DISCONNECTED', 'primary': None, 'top': []}
    return obj


def demo_data():
    now = time.time()
    return {
        'schema': 1, 'running': True, 'updated_epoch': now,
        'overall_state': 'PERSISTENT_RF', 'audio_enabled': True,
        'calibration_remaining_s': 0, 'threshold_db': 6,
        'region_count': 2, 'multiple_regions': True,
        'regions': [
            {'id':'R8','frequency_mhz':5917,'band':'R','channel':8,'score':84,
             'relative_db':9.4,'occupancy':.93,'age_s':.1,
             'low_mhz':5917,'high_mhz':5925, 'members':['R8','E7'], 'status':'PERSISTENT_RF'},
            {'id':'F4','frequency_mhz':5800,'band':'F','channel':4,'score':76,
             'relative_db':8.3,'occupancy':.89,'age_s':.1,
             'low_mhz':5800,'high_mhz':5809, 'members':['F4','R5'], 'status':'PERSISTENT_RF'}
        ],
        'primary': {'band': 'R', 'channel': 8, 'frequency_mhz': 5917,
                    'status': 'PERSISTENT_RF', 'relative_db': 9.4,
                    'score': 84, 'occupancy': 0.93, 'age_s': 0.1},
        'channels': [{'band': b, 'channel': i+1, 'frequency_mhz': f,
                      'status': 'PERSISTENT_RF' if (b, f) == ('R', 5917) else 'BACKGROUND',
                      'relative_db': 9.4 if f == 5917 else 0.5 + i/5,
                      'score': 84 if f == 5917 else 0, 'age_s': 0.2}
                     for b, freqs in BANDS.items() for i, f in enumerate(freqs)],
        'spectrum': {'start_mhz': 5640, 'stop_mhz': 5950,
                     'columns_db': [round(max(0, 1.2 + 13*__import__('math').exp(-((i-85)/4)**2)
                                + 10*__import__('math').exp(-((i-49)/5)**2)),1)
                                for i in range(96)]}
    }


def text_clipped(d, text, x, y, width, color, face):
    text = str(text)
    while text and d.textbbox((0, 0), text, font=face)[2] > width:
        text = text[:-2].rstrip() + '…' if len(text) > 2 else ''
    if text:
        d.text((x, y), text, font=face, fill=color)


def touch_button(d, bounds, label, active=True, warn=False):
    fill = (76, 39, 44) if warn else (28, 64, 72) if active else (36, 43, 53)
    fg = WHITE if active else GRAY
    d.rounded_rectangle(bounds, radius=6, fill=fill, outline=(71, 101, 111), width=1)
    x0, y0, x1, y1 = bounds
    bbox = d.textbbox((0, 0), label, font=F12)
    tw = bbox[2]-bbox[0]
    d.text((x0+(x1-x0-tw)//2, y0+8), label, font=F12, fill=fg)


def valid_regions(status):
    """Only current, confirmed RF regions are operator-selectable."""
    if status.get('overall_state') in ('NO_DATA', 'STOPPED', 'DISCONNECTED', 'CALIBRATING'):
        return []
    regions = status.get('regions', [])
    return [r for r in regions if isinstance(r, dict)
            and r.get('status') == 'PERSISTENT_RF'
            and isinstance(r.get('frequency_mhz'), (int, float))
            and isinstance(r.get('score'), int)
            and r.get('age_s') is not None and float(r['age_s']) <= 2.5]


def find_selected(status, selected):
    """Find current evidence without changing the channel chosen by the user.

    The strongest channel can hop among overlapping PAL/NTSC video channels.
    The selected frequency remains fixed; this lookup is for current RF status.
    """
    if not selected:
        return None
    regions = valid_regions(status)
    for region in regions:
        if region['id'] == selected.get('id'):
            return region
    original_members = set(selected.get('members', []))
    for region in regions:
        if original_members.intersection(region.get('members', [])):
            return region
    freq = selected.get('frequency_mhz')
    if isinstance(freq, (int, float)):
        neighbors = [r for r in regions
                     if abs(r['frequency_mhz'] - freq) <= 12]
        if neighbors:
            return min(neighbors, key=lambda r: abs(r['frequency_mhz']-freq))
    return None


def paint_spectrum(d, spec, rect, full=False):
    """Compact/full vertical bar plot from current hackrf_sweep spectrum."""
    x0, y0, x1, y1 = rect
    w, h = x1-x0+1, y1-y0+1
    d.rounded_rectangle((x0,y0,x1,y1), radius=4, fill=(13, 27, 38), outline=(44, 67, 81))
    for i in (0.25, 0.5, 0.75):
        y = int(y1 - i*h)
        d.line((x0+2,y,x1-2,y),fill=(37,53,65),width=1)
    cols = (spec or {}).get('columns_db') or []
    values = downsample(cols, max(1,(w-8)//3 if full else (w-8)//2))
    if any(v is not None for v in values):
        col_width = (w-8)/len(values)
        for j, value in enumerate(values):
            if value is None:
                continue
            dblevel = max(0., min(20.,float(value)))
            bh = max(1,int((h-7)*dblevel/20)) if dblevel>0 else 0
            if bh:
                xa = int(x0+4+j*col_width)
                xb = max(xa, int(x0+4+(j+1)*col_width)-1)
                d.rectangle((xa,y1-4-bh,xb,y1-4),fill=palette_rgb(dblevel))
    else:
        d.text((x0+9,y0+h//2-7),'ESPERANDO BARRIDO...',font=F10,fill=GRAY)


def video_thumbnail(frame_path, max_age=3.0):
    """Read only fresh decoded images, never display an old video as live."""
    try:
        path = Path(frame_path)
        if not path.is_file() or time.time()-path.stat().st_mtime > max_age:
            return None
        with Image.open(path) as im:
            im.load()
            return ImageOps.contain(im.convert('RGB'),(216,155))
    except (OSError, ValueError, SyntaxError):
        return None


class VideoFrameCache:
    """Keep at most the newest raw frame in memory. PNG fallback is optional."""
    def __init__(self, raw_path=None, png_path=None):
        self.raw_path = raw_path or raw_default_path()
        self.png_path = Path(png_path) if png_path else None
        self.key = None
        self.stamp_ns = None
        self.image = None
        self.source = None

    def reset(self):
        self.key = self.stamp_ns = self.image = self.source = None

    def update(self, age_limit=3.0, color_only=False):
        """Return true on fresh color or grayscale; never invent color from Y."""
        # Keep a current color image on screen between color decoder updates.
        # Color decodes independently at a lower rate than the Y8 producer.
        color = read_rgb_frame(max_age_s=min(1.0, age_limit))
        if color is not None:
            w,h,seq,stamp_ns,age,data = color
            key = ('color', stamp_ns, seq)
            if key == self.key:
                return False
            try:
                self.image = ImageOps.contain(Image.frombytes('RGB',(w,h),data),(236,177))
                self.key, self.stamp_ns, self.source = key, stamp_ns, 'COLOR'
                return True
            except (ValueError, OSError):
                pass
        if color_only:
            return False
        frame = read_raw_frame(self.raw_path, max_age_s=age_limit)
        if frame is not None:
            w,h,seq,stamp_ns,age,data = frame
            key = ('raw', stamp_ns, seq)
            if self.key != key:
                try:
                    img = Image.frombytes('L', (w,h), data).convert('RGB')
                    self.image = ImageOps.contain(img, (236,177))
                    self.key = key
                    self.stamp_ns = stamp_ns
                    self.source = 'RAM'
                    return True
                except (ValueError, OSError):
                    pass
            return False
        # Backwards-compatible route if the upstream decoder still writes PNG.
        if self.png_path:
            try:
                stat = self.png_path.stat()
                age = max(0.0, time.time() - stat.st_mtime)
                if age <= age_limit:
                    key = ('png', stat.st_mtime_ns)
                    if self.key != key:
                        with Image.open(self.png_path) as im:
                            im.load()
                            self.image = ImageOps.contain(im.convert('RGB'), (236,177))
                        self.key = key
                        self.stamp_ns = stat.st_mtime_ns
                        self.source = 'PNG'
                        return True
            except (OSError, ValueError, SyntaxError):
                pass
        return False

    def age(self):
        if self.stamp_ns is None:
            return None
        return max(0.0, (time.time_ns() - self.stamp_ns) / 1e9)

    def valid_image(self, age_limit=3.0):
        age = self.age()
        return self.image if age is not None and age <= age_limit else None


def render(status, page='home', band='R', message='', page_index=0, selected=None, frame_path=None, video_fps=None, frame_image=None, frame_age=None, frame_source=None):
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    state = status.get('overall_state', 'NO_DATA')
    connected = state not in ('NO_DATA', 'STOPPED', 'DISCONNECTED')
    primary = (status.get('primary') or {}) if connected else {}
    regions = valid_regions(status)
    count = len(regions)
    d.rectangle((0, 0, 239, 35), fill=PANEL)
    d.rectangle((0, 0, 4, 35), fill=CYAN)
    d.text((12, 5), 'FPV DETECTOR', font=F17, fill=WHITE)
    d.text((12, 25), 'HACKRF | 5.8 GHz | V19-D', font=F10, fill=GRAY)

    if status.get('receiver_mode') == 'VIDEO' or page == 'video':
        v = status.get('video') or {}
        freq = v.get('frequency_mhz', '----')
        channel = f"{v.get('band', '?')}{v.get('channel', '?')}"
        backend = v.get('backend', 'fpvdec')
        d.rounded_rectangle((3, 39, 237, 64), radius=5, fill=PANEL, outline=CYAN)
        d.text((9, 44), f'{channel}  {freq} MHz', font=F14, fill=WHITE)
        d.text((168, 47), backend.upper(), font=F10, fill=YELLOW)
        d.rectangle((1, 68, 238, 250), fill=(7, 13, 18))
        frame = frame_image if backend in ('fpvdec','fpv-sdr') else None
        if frame is not None:
            img.paste(frame, (120-frame.width//2, 159-frame.height//2))
        else:
            d.text((38, 134), 'ESPERANDO VIDEO', font=F14, fill=WHITE)
            d.text((19, 161), 'Decoder NTSC activo', font=F12, fill=GRAY)
        d.rectangle((3, 250, 237, 272), fill=PANEL)
        if frame is not None:
            label = f'{video_fps or 0:.1f} FPS  COLOR' if frame_source == 'COLOR' else f'{video_fps or 0:.1f} FPS  B/N'
            if frame_age is not None and frame_age > 1.5:
                label += '  ATRASADO'
            d.text((8, 255), label, font=F12, fill=GREEN if frame_source == 'COLOR' else YELLOW)
        else:
            d.text((9, 255), 'BUSCANDO / SIN FOTOGRAMA', font=F10, fill=YELLOW)
        touch_button(d, (3, 278, 117, 315), 'VOLVER')
        touch_button(d, (123, 278, 237, 315), 'AUDIO')
        return img

    if page == 'spectrum':
        d.rounded_rectangle((9, 42, 231, 73),radius=5,fill=PANEL,outline=CYAN)
        d.text((17,50),'ESPECTRO RF 5.8 GHz',font=F14,fill=WHITE)
        paint_spectrum(d,status.get('spectrum'),(11,90,229,235),full=True)
        spec=status.get('spectrum') or {}
        lo,hi=spec.get('start_mhz',5640),spec.get('stop_mhz',5950)
        d.text((11,239),str(lo),font=F10,fill=GRAY)
        d.text((99,239),str(round((lo+hi)/2)),font=F10,fill=GRAY)
        d.text((200,239),str(hi),font=F10,fill=GRAY)
        d.text((12,258),'0..20 dB relativos | no dBm',font=F10,fill=GRAY)
        touch_button(d,(9,280,116,313),'INICIO')
        touch_button(d,(124,280,231,313),'SENALES')
        return img

    if page == 'signals':
        d.rounded_rectangle((9, 42, 231, 75), radius=6, fill=PANEL)
        d.text((15, 49), f'{count} REGIONES RF', font=F14, fill=RED if count >= 2 else CYAN)
        d.text((12, 79), 'Toca una region para elegir canal', font=F10, fill=GRAY)
        if not count:
            d.text((13, 145), 'SIN RF PERSISTENTE', font=F14, fill=GRAY)
        start = max(0, page_index) * 3
        for i, r in enumerate(regions[start:start+3]):
            y = 99 + i*51
            d.rounded_rectangle((9, y, 231, y+45), radius=5, fill=(27, 48, 57), outline=(60, 92, 104))
            d.text((17, y+5), f"{r['band']}{r['channel']}  {r['frequency_mhz']} MHz", font=F14, fill=WHITE)
            d.text((17, y+26), f"RF {r['score']}/100  {r['relative_db']:+.1f} dB", font=F11, fill=YELLOW)
            d.text((205, y+14), '>', font=F17, fill=CYAN)
        if count > 3:
            total_pages = (count + 2) // 3
            d.text((15, 260), f'PAGINA {page_index+1}/{total_pages}', font=F10, fill=GRAY)
            d.text((153, 260), 'BANDAS >', font=F10, fill=CYAN)
        else:
            d.text((12, 261), 'Una region no equivale a un dron.', font=F10, fill=GRAY)
        touch_button(d, (9, 280, 116, 313), 'BANDAS' if count <= 3 else 'MAS >')
        touch_button(d, (124, 280, 231, 313), 'INICIO')
        return img

    if page == 'detail':
        region = find_selected(status, selected)
        d.rounded_rectangle((9, 45, 231, 78), radius=6, fill=PANEL)
        d.text((16, 52), 'SELECCION DE RF', font=F14, fill=CYAN)
        if region:
            # The receiver always tunes the original chosen frequency, never a
            # changing highest-score neighboring channel.
            d.text((15, 91), f"{selected['frequency_mhz']}", font=F40, fill=WHITE)
            d.text((160, 118), 'MHz', font=F14, fill=GRAY)
            d.text((14, 145), f"CANAL {selected['band']}{selected['channel']}", font=F14, fill=CYAN)
            d.text((14, 171), f"Evidencia RF: {region['score']}/100", font=F12, fill=WHITE)
            d.text((14, 194), f"RF relativa: {region['relative_db']:+.1f} dB", font=F12, fill=WHITE)
            label = ','.join(region.get('members', [])[:5])
            text_clipped(d, f'RF activa: {label}', 14, 217, 210, GRAY, F11)
            d.text((14, 241), 'SINTONIZAR CANAL ELEGIDO', font=F12, fill=YELLOW)
            if message: text_clipped(d, message, 14, 259, 215, GREEN, F10)
        elif selected:
            # Do not erase the operator's selection due to a 2.5s sweep gap.
            # VIDEO entry is controlled solely by the operator.
            d.text((14, 94), f"{selected['frequency_mhz']}", font=F40, fill=WHITE)
            d.text((160, 121), 'MHz', font=F14, fill=GRAY)
            d.text((14, 151), f"CANAL {selected['band']}{selected['channel']}", font=F14, fill=CYAN)
            d.text((14, 182), 'RF SIN LECTURA RECIENTE', font=F12, fill=YELLOW)
            d.text((14, 204), 'Puedes intentar sintonizar.', font=F11, fill=GRAY)
            d.text((14, 229), 'RECIBIR aun permite sintonizar.', font=F10, fill=GRAY)
            if message: text_clipped(d, message, 14, 259, 214, YELLOW, F10)
        else:
            d.text((14, 113), 'SIN CANAL SELECCIONADO', font=F14, fill=YELLOW)
            d.text((14, 147), 'Regresa y elige una senal.', font=F12, fill=GRAY)
        touch_button(d, (9, 280, 116, 313), 'VOLVER')
        touch_button(d, (124, 280, 231, 313), 'RECIBIR', active=bool(selected))
        return img

    if page == 'channels':
        d.rounded_rectangle((9, 44, 231, 68), radius=5, fill=PANEL)
        d.text((18, 48), f'CANALES BANDA {band}', font=F14, fill=CYAN)
        d.text((12, 72), 'CH    MHz       RF REL.      ESTADO', font=F10, fill=GRAY)
        by_index = {(item.get('band'), item.get('channel')): item
                    for item in status.get('channels', []) if isinstance(item, dict)}
        for i, fc in enumerate(BANDS[band]):
            info = by_index.get((band, i+1), {})
            st = info.get('status') if connected else 'UNSEEN'
            fresh = info.get('age_s') is not None and float(info.get('age_s', 999)) < 2.5
            if not fresh: st = 'STALE'
            raw_level = info.get('relative_db')
            color = (palette_rgb(raw_level) if st in ('PERSISTENT_RF','RF_CANDIDATE')
                     and isinstance(raw_level,(int,float)) else GRAY)
            y = 91 + i * 22
            if st in ('PERSISTENT_RF', 'RF_CANDIDATE'):
                d.rectangle((8, y-1, 232, y+19), fill=(43, 34, 43))
            val = info.get('relative_db') if fresh else None
            level = f'{val:+5.1f}' if isinstance(val, (float, int)) else '   --'
            label = 'RF!' if st == 'PERSISTENT_RF' else '?' if st == 'RF_CANDIDATE' else '-' if st == 'BACKGROUND' else '~'
            d.text((13, y), f'{i+1:>2}', font=F12, fill=WHITE)
            d.text((47, y), str(fc), font=F12, fill=WHITE)
            d.text((114, y), level, font=F12, fill=color)
            d.text((190, y), label, font=F12, fill=color)
            if i < 7: d.line((12, y+20, 228, y+20), fill=(32, 48, 60), width=1)
        touch_button(d, (9, 280, 116, 313), 'OTRA BANDA')
        touch_button(d, (124, 280, 231, 313), 'SENALES')
        return img

    if state == 'PERSISTENT_RF':
        banner,accent=('VARIAS RF' if count>1 else 'RF PERSISTENTE'),RED
    elif state == 'RF_CANDIDATE': banner,accent='CANDIDATO RF',YELLOW
    elif state == 'CALIBRATING': banner,accent='CALIBRANDO',CYAN
    elif state == 'BACKGROUND': banner,accent='ESCANEANDO',GREEN
    else: banner,accent='SIN DATOS',GRAY
    d.rounded_rectangle((9,41,231,70),radius=5,fill=PANEL,outline=accent)
    d.text((15,47),banner,font=F14,fill=accent)
    d.rounded_rectangle((169,3,231,33),radius=5,fill=(32,60,73))
    d.text((174,12),'GRAFICO',font=F10,fill=CYAN)
    if state == 'CALIBRATING':
        rem=float(status.get('calibration_remaining_s',0) or 0)
        d.text((12,88),'CALIBRANDO RF',font=F14,fill=CYAN)
        d.text((15,109),f'{rem:.0f} s',font=F40,fill=WHITE)
    elif connected:
        freq=primary.get('frequency_mhz')
        shown=str(freq) if isinstance(freq,(int,float)) else '----'
        d.text((12,74),'MAYOR RF / REFERENCIA',font=F10,fill=GRAY)
        d.text((10,87),shown,font=F40,fill=WHITE)
        d.text((149,116),'MHz',font=F14,fill=GRAY)
        lvl=primary.get('relative_db')
        level=f'{float(lvl):+.1f} dB' if isinstance(lvl,(int,float)) else '-- dB'
        d.text((12,137),f"{primary.get('band','')}{primary.get('channel','')}   {level}",font=F12,
               fill=palette_rgb(lvl) if isinstance(lvl,(int,float)) else GRAY)
    else:
        d.text((13,99),'SIN DATOS RF',font=F17,fill=GRAY)
    paint_spectrum(d,status.get('spectrum'),(10,164,230,238),full=False)
    d.text((12,242),f'{count} zonas RF | gradiente 0..20 dB',font=F10,fill=GRAY)
    if message: text_clipped(d,message,11,258,216,YELLOW,F10)
    else: d.text((12,258),'Toca GRAFICO para ampliar',font=F10,fill=GRAY)
    touch_button(d,(9,280,116,313),'SENALES')
    audio_on=bool(status.get('audio_enabled',False))
    touch_button(d,(124,280,231,313),'SILENCIO' if audio_on else 'SONIDO',
                 active=connected,warn=audio_on)
    return img


class Display:
    def __init__(self, baudrate):
        try:
            import board
            import digitalio
            from adafruit_rgb_display import ili9341
        except ImportError as exc:
            raise RuntimeError('Instala adafruit-blinka, adafruit-circuitpython-rgb-display y Pillow') from exc
        self.device = ili9341.ILI9341(
            board.SPI(), cs=digitalio.DigitalInOut(board.D5),
            dc=digitalio.DigitalInOut(board.D25),
            rst=digitalio.DigitalInOut(board.D24),
            baudrate=baudrate, rotation=0)
        if (self.device.width, self.device.height) != (W, H):
            raise RuntimeError(f'Orientacion inesperada {self.device.width}x{self.device.height}')

    def image(self, img):
        self.device.image(img)

    def video_frame(self, img):
        """Fast blit: no PIL re-render of status/buttons for every video field."""
        px = 120 - img.width//2
        py = 159 - img.height//2
        try:
            self.device.image(img, x=px, y=py)
        except TypeError:
            # Older Adafruit driver without region writes (correctness fallback).
            raise RuntimeError('Partial ILI9341 updates not supported by installed driver')

    def region(self, img, box):
        """Blit only the changed rectangle via Adafruit's public image(x,y).

        Keep full-screen fallback for older library releases.
        """
        x0,y0,x1,y1 = box
        cropped = img.crop(box)
        try:
            self.device.image(cropped, x=x0, y=y0)
        except TypeError:
            self.device.image(img)



class Touch:
    """XPT2046 SPI0 CE1 + active-low PENIRQ; designed for Pi 5 Blinka GPIO."""
    def __init__(self, speed=1000000):
        try:
            import spidev
            import digitalio
            import board
        except ImportError as exc:
            raise RuntimeError('Falta spidev o Adafruit Blinka: instala python3-spidev') from exc
        self.bus = spidev.SpiDev()
        # The Adafruit Blinka SPI used by ILI9341 toggles CE0 internally,
        # even when the TFT uses a different GPIO for its explicit CS.
        # Use the independent CE1 line for XPT2046 to avoid double-selection.
        if not Path('/dev/spidev0.1').exists():
            raise RuntimeError('Falta /dev/spidev0.1 (SPI0 CE1 / GPIO7). Activa SPI y revisa los overlays de Raspberry Pi.')
        self.bus.open(0, 1)  # CE1 / GPIO7 / physical pin 26 = T_CS
        self.bus.max_speed_hz = speed
        self.bus.mode = 0
        self.irq = digitalio.DigitalInOut(board.D4)
        try:
            self.irq.switch_to_input(pull=digitalio.Pull.UP)
        except (ValueError, NotImplementedError):
            self.irq.switch_to_input()

    def down(self):
        return not bool(self.irq.value)  # low while finger presses

    def read_raw(self):
        def read(cmd):
            b = self.bus.xfer2([cmd, 0, 0])
            return (((b[1] << 8) | b[2]) >> 3) & 0x0fff
        # Commands: X 0xD0, Y 0x90; calibration absorbs orientation.
        x = [read(0xD0) for _ in range(5)]
        y = [read(0x90) for _ in range(5)]
        return statistics.median(x), statistics.median(y)

    def close(self):
        self.bus.close()
        self.irq.deinit()


class Calibration:
    """Affine 2D raw-touch -> screen mapping, using four measured reference points."""
    def __init__(self, xcoef, ycoef):
        self.xcoef, self.ycoef = xcoef, ycoef

    def map(self, x, y):
        sx = self.xcoef[0]*x + self.xcoef[1]*y + self.xcoef[2]
        sy = self.ycoef[0]*x + self.ycoef[1]*y + self.ycoef[2]
        return int(round(sx)), int(round(sy))

    @staticmethod
    def solve_3(matrix, vector):
        a = [list(map(float, matrix[i])) + [float(vector[i])] for i in range(3)]
        for col in range(3):
            pivot = max(range(col, 3), key=lambda i: abs(a[i][col]))
            if abs(a[pivot][col]) < 1e-9:
                raise ValueError('Calibration points are degenerate')
            a[col], a[pivot] = a[pivot], a[col]
            pivot_value = a[col][col]
            for j in range(col, 4):
                a[col][j] /= pivot_value
            for i in range(3):
                if i != col:
                    factor = a[i][col]
                    for j in range(col, 4):
                        a[i][j] -= factor*a[col][j]
        return [a[i][3] for i in range(3)]

    @classmethod
    def fit(cls, pairs):
        if len(pairs) < 3:
            raise ValueError('Need at least 3 points')
        # Least squares affine transform. Handles swapped/reversed touchscreen axes.
        design = [[raw_x, raw_y, 1.0] for (raw_x, raw_y), _ in pairs]
        normal = [[sum(row[i]*row[j] for row in design) for j in range(3)]
                  for i in range(3)]
        xrhs = [sum(design[k][i]*pairs[k][1][0] for k in range(len(pairs)))
                for i in range(3)]
        yrhs = [sum(design[k][i]*pairs[k][1][1] for k in range(len(pairs)))
                for i in range(3)]
        return cls(cls.solve_3(normal, xrhs), cls.solve_3(normal, yrhs))

    def save(self, path):
        path.write_text(json.dumps({'schema': 1, 'xcoef': self.xcoef, 'ycoef': self.ycoef}, indent=2),
                        encoding='utf-8')

    @classmethod
    def load(cls, path):
        obj = json.loads(path.read_text(encoding='utf-8'))
        if obj['schema'] != 1:
            raise ValueError('Wrong calibration version')
        return cls(obj['xcoef'], obj['ycoef'])


def calibration_screen(target, idx):
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((15, 90), 'CALIBRAR TOUCH', font=F17, fill=CYAN)
    d.text((15, 121), f'Punto {idx}/4', font=F14, fill=WHITE)
    d.text((15, 147), 'Toca la cruz y suelta', font=F12, fill=GRAY)
    x, y = target
    d.line((x-13, y, x+13, y), fill=YELLOW, width=2)
    d.line((x, y-13, x, y+13), fill=YELLOW, width=2)
    return img


def wait_press(touch, duration=60, manual=False):
    """Capture raw coordinates with operator feedback.

    Automatic mode accepts a stable IRQ-low sample immediately, instead of
    requiring a timed press AND a lengthy release before acknowledging it.
    Manual mode permits Enter during a held touch, ignoring PENIRQ entirely.
    Returns (raw_x, raw_y) or None.
    """
    start = time.monotonic()
    last_report = 0.0
    low_started = None
    samples = []
    print('  Estado: esperando pulsacion. Mantener dedo 0.2 s.', flush=True)
    if sys.stdin.isatty():
        print('  Alternativa: mantén el dedo sobre la cruz y pulsa ENTER en SSH.', flush=True)
    while time.monotonic() - start < duration:
        now = time.monotonic()
        down = touch.down()
        if now - last_report >= 1.0:
            print(f'  IRQ GPIO4: {"LOW / pulsado" if down else "HIGH / libre"}  '
                  f'| quedan {max(0,int(duration-(now-start)))} s', flush=True)
            last_report = now

        # Manual capture uses SPI raw data even if the interrupt wire is unreliable.
        if sys.stdin.isatty():
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                sys.stdin.readline()
                pts = [touch.read_raw() for _ in range(7)]
                x = statistics.median(p[0] for p in pts)
                y = statistics.median(p[1] for p in pts)
                print(f'  CAPTURA MANUAL  IRQ={"LOW" if down else "HIGH"} '
                      f'RAW=({x:.0f},{y:.0f})', flush=True)
                if not (100 < x < 4000 and 100 < y < 4000):
                    print('  Lectura fuera de rango. Mantén el dedo y repite.', flush=True)
                else:
                    return (x, y)

        if not manual:
            if down:
                if low_started is None:
                    low_started = now
                    samples.clear()
                    print('  PULSACION detectada (IRQ LOW). Leyendo RAW...', flush=True)
                raw = touch.read_raw()
                if 100 < raw[0] < 4000 and 100 < raw[1] < 4000:
                    samples.append(raw)
                if len(samples) >= 3 and now-low_started >= .06:
                    xs = [p[0] for p in samples[-5:]]
                    ys = [p[1] for p in samples[-5:]]
                    if max(xs)-min(xs) < 450 and max(ys)-min(ys) < 450:
                        result = (statistics.median(xs), statistics.median(ys))
                        print(f'  CAPTURA AUTOMATICA RAW={result} '
                              f'(n={len(samples)}, IRQ LOW)', flush=True)
                        return result
            else:
                if low_started is not None:
                    print(f'  Pulsacion demasiado corta/inestable '
                          f'({len(samples)} lecturas). Reintenta.', flush=True)
                low_started = None
                samples.clear()
        time.sleep(.035)
    print('  TIMEOUT: ninguna captura valida para este punto.', flush=True)
    return None


def wait_release(touch, duration=12):
    """Require a real finger release before advancing to the next cross."""
    start = time.monotonic()
    released_at = None
    print('  SUELTA el dedo antes del siguiente punto.', flush=True)
    while time.monotonic() - start < duration:
        if not touch.down():
            if released_at is None:
                released_at = time.monotonic()
            if time.monotonic()-released_at >= .14:
                print('  Dedo liberado: OK', flush=True)
                return True
        else:
            released_at = None
        time.sleep(.035)
    print('  AVISO: IRQ no registra liberacion. '
          'Puedes utilizar captura manual (ENTER) tras verificar el cable T_IRQ.', flush=True)
    return False


def calib_feedback(display, target, index, message, color=GREEN):
    img = calibration_screen(target, index)
    d = ImageDraw.Draw(img)
    d.rectangle((10, 171, 230, 194), fill=BG)
    d.text((15, 174), message, font=F12, fill=color)
    display.image(img)


def calibrate(display, touch, cal_path, manual=False):
    targets = [(30, 30), (210, 30), (30, 290), (210, 290)]
    pairs = []
    print('\nCALIBRACION XPT2046 CON DIAGNOSTICO', flush=True)
    print('IMPORTANTE: detener previamente el lanzador y la otra TFT.', flush=True)
    print('Toca la cruz en la pantalla fisica; la terminal mostrara IRQ y RAW.', flush=True)
    if manual:
        print('MODO MANUAL: mantén pulsado cada punto y presiona ENTER en SSH.', flush=True)
    for i, xy in enumerate(targets, 1):
        calib_feedback(display, xy, i, f'ESPERANDO PUNTO {i}/4', YELLOW)
        print(f'\n=== PUNTO {i}/4 pixel {xy} ===', flush=True)
        raw = wait_press(touch, duration=60, manual=manual)
        if raw is None:
            calib_feedback(display, xy, i, 'TIMEOUT - VER TERMINAL', RED)
            raise RuntimeError(f'Timeout en punto {i}. '
                               'Usa --touch-calibrate-manual o revisa T_IRQ.')
        pairs.append((raw, xy))
        print(f'PUNTO {i} ACEPTADO: RAW={raw} -> PIXEL={xy}', flush=True)
        calib_feedback(display, xy, i, f'PUNTO {i} CAPTURADO', GREEN)
        if not manual:
            if not wait_release(touch):
                raise RuntimeError('No se detecta liberacion IRQ. '
                                   'Prueba --touch-calibrate-manual.')
        else:
            print('  Suelta el dedo. Preparando siguiente punto...', flush=True)
            time.sleep(.75)
        time.sleep(.28)

    try:
        cal = Calibration.fit(pairs)
    except ValueError as error:
        raise RuntimeError('Puntos RAW incorrectos/duplicados. '
                           'Repite tocando cada esquina diferente.') from error
    worst = max(max(abs(cal.map(*raw)[j]-pixel[j]) for j in (0, 1))
                for raw, pixel in pairs)
    print(f'Error maximo del ajuste: {worst} pixeles.', flush=True)
    if worst > 25:
        raise RuntimeError(f'Error excesivo ({worst} px). '
                           'Repite presionando el centro de cada cruz.')
    cal.save(cal_path)
    display.image(render({'overall_state':'NO_DATA'}, message='TOUCH CALIBRADO'))
    print(f'CALIBRACION GUARDADA: {cal_path}', flush=True)
    return cal


def send_command(path, command, region=None):
    """Atomic IPC: only predefined touch controls are forwarded to detector."""
    if command not in ('TOGGLE_AUDIO', 'START_VIDEO', 'STOP_VIDEO'):
        raise ValueError('Unsupported touch command')
    data = {'schema': 1, 'command': command,
            'epoch': time.time(), 'id': uuid.uuid4().hex}
    if command == 'START_VIDEO':
        if not region or not isinstance(region.get('frequency_mhz'), int):
            raise ValueError('Falta frecuencia seleccionada')
        data['frequency_mhz'] = int(region['frequency_mhz'])
    path.parent.mkdir(exist_ok=True, parents=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
    os.replace(temp, path)


def handle_tap(x, y, page, band, connected, status=None, page_index=0, selected=None):
    """Return (page, band, page_index, selection, action); no RF control here."""
    status = status or {}
    regions = valid_regions(status)
    if not (0 <= x < W and 0 <= y < H):
        return page, band, page_index, selected, None
    if page == 'video':
        if y >= 273 and x < 120:
            return page, band, page_index, selected, 'STOP_VIDEO'
        if y >= 273:
            return page, band, page_index, selected, 'TOGGLE_AUDIO'
        return page, band, page_index, selected, None
    if page == 'home' and y < 38 and x >= 165:
        return 'spectrum', band, page_index, selected, None
    if page == 'spectrum':
        if y >= 270:
            return ('home' if x < 120 else 'signals'),band,0,None,None
        return page,band,page_index,selected,None
    if page == 'signals' and 249 <= y < 277 and x >= 135:
        return 'channels', band, page_index, selected, None
    if page == 'signals' and 99 <= y < 249:
        slot = (y - 99) // 51
        if (y - 99) % 51 <= 45:
            idx = page_index * 3 + slot
            if 0 <= idx < len(regions):
                return 'detail', band, page_index, dict(regions[idx]), None
    if y < 270:
        return page, band, page_index, selected, None
    left = x < 120
    if page == 'home':
        if left: return 'signals', band, 0, None, None
        return page, band, page_index, selected, 'TOGGLE_AUDIO' if connected else None
    if page == 'signals':
        if left:
            if len(regions) > 3:
                return page, band, (page_index+1) % ((len(regions)+2)//3), selected, None
            return 'channels', band, page_index, selected, None
        return 'home', band, 0, None, None
    if page == 'detail':
        if left: return 'signals', band, page_index, None, None
        return page, band, page_index, selected, 'START_VIDEO' if selected else None
    if page == 'channels':
        if left:
            idx = (BAND_NAMES.index(band)+1) % len(BAND_NAMES)
            return page, BAND_NAMES[idx], page_index, selected, None
        return 'signals', band, 0, None, None
    return page, band, page_index, selected, None


def save_video_selection(path, region):
    """Persist operator choice; does not tune HackRF or start video."""
    if not region or not isinstance(region.get('frequency_mhz'), int):
        raise ValueError('Falta frecuencia seleccionada')
    data = {
        'schema': 1, 'mode': 'SCAN', 'video_ready': False,
        'note': 'Operator-selected frequency; no RF validation on VIDEO entry',
        'requested_epoch': time.time(), 'frequency_mhz': int(region['frequency_mhz']),
        'band': str(region['band']), 'channel': int(region['channel']),
        'score': int(region['score']), 'members': list(region.get('members', [])),
    }
    path.parent.mkdir(exist_ok=True, parents=True)
    tmp = path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
    os.replace(tmp, path)


class TouchTapDetector:
    """One press -> one tap. Read on the FIRST observed PENIRQ low.

    The old 70 ms hold requirement could miss a short tap while a full SPI
    image was being transmitted. Validate two raw reads to reject spikes.
    """
    def __init__(self, release_s=0.08, min_interval_s=0.20):
        self.latched = False
        self.release_at = None
        self.last_tap_at = -1e9
        self.release_s = release_s
        self.min_interval_s = min_interval_s

    def poll(self, touch, cal, now):
        if not touch.down():
            if self.release_at is None:
                self.release_at = now
            if now - self.release_at >= self.release_s:
                self.latched = False
            return None
        self.release_at = None
        if self.latched or now - self.last_tap_at < self.min_interval_s:
            return None
        first = touch.read_raw()
        second = touch.read_raw()
        if any(not 100 < v < 4000 for v in (*first, *second)):
            return None
        if (abs(first[0] - second[0]) > 500 or
                abs(first[1] - second[1]) > 500):
            return None
        raw = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
        x, y = cal.map(*raw)
        self.latched = True
        self.last_tap_at = now
        return (x, y, raw)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--video-frame-file', type=Path, default=HERE/'fpv_video_frame.png')
    ap.add_argument('--state-file', type=Path, default=HERE/'fpv_status.json')
    ap.add_argument('--command-file', type=Path, default=HERE/'fpv_touch_command.json')
    ap.add_argument('--selection-file', type=Path, default=HERE/'fpv_video_selection.json')
    ap.add_argument('--calibration-file', type=Path, default=HERE/'fpv_touch_calibration.json')
    ap.add_argument('--refresh', type=float, default=0.30)
    ap.add_argument('--video-refresh', type=float, default=0.09,
                    help='Minimum TFT frame interval in VIDEO mode (seconds)')
    ap.add_argument('--max-age', type=float, default=2.0)
    ap.add_argument('--baudrate', type=int,
                    default=int(os.environ.get('FPV_TFT_BAUDRATE', '12000000')))
    ap.add_argument('--raw-frame-file', type=Path, default=raw_default_path())
    ap.add_argument('--freeze-after', type=float, default=3.0)
    ap.add_argument('--no-touch', action='store_true', help='Display-only fallback, no XPT2046 wires')
    ap.add_argument('--touch-calibrate', action='store_true', help='Calibracion touch automatica con diagnostico')
    ap.add_argument('--touch-calibrate-manual', action='store_true', help='Calibracion guiada usando ENTER en SSH, sin depender de T_IRQ')
    ap.add_argument('--touch-test', action='store_true', help='Print raw and calibrated XPT2046 coordinates (no display)')
    ap.add_argument('--touch-debug', action='store_true', help='Log screen taps and their mapped coordinates to TFT log')
    ap.add_argument('--preview', type=Path, help='Save PNG without physical TFT')
    ap.add_argument('--page', choices=['home', 'channels', 'signals', 'detail', 'spectrum', 'video'], default='home')
    ap.add_argument('--band', choices=BAND_NAMES, default='R')
    ap.add_argument('--demo', action='store_true', help='Synthetic data preview')
    args = ap.parse_args()
    if args.refresh <= 0 or args.max_age <= 0 or args.video_refresh <= 0:
        ap.error('--refresh and --max-age must be > 0')

    if args.preview:
        state = demo_data() if args.demo else read_status(args.state_file, args.max_age)
        regions = valid_regions(state)
        selected = regions[0] if args.page == 'detail' and regions else None
        render(state, page=args.page, band=args.band, selected=selected,
               frame_path=args.video_frame_file).save(args.preview)
        print(f'Preview saved to {args.preview}')
        return

    if args.touch_test:
        touch = Touch()
        try:
            try:
                calibration = Calibration.load(args.calibration_file)
                print('Calibration loaded:', args.calibration_file, flush=True)
            except (ValueError, OSError, KeyError, TypeError):
                calibration = None
                print('WARNING: no valid calibration found:', args.calibration_file, flush=True)
            print('Press the buttons on the TFT. Ctrl+C to quit.', flush=True)
            tapper = TouchTapDetector()
            while True:
                tap = tapper.poll(touch, calibration, time.monotonic()) if calibration else None
                if tap:
                    x, y, raw = tap
                    zone = ('SENALES / OTRA BANDA' if y >= 270 and x < 120 else
                            'AUDIO / INICIO' if y >= 270 else 'NO BUTTON')
                    print(f'raw=({raw[0]:.0f},{raw[1]:.0f}) pixels=({x},{y}) '
                          f'button={zone}', flush=True)
                elif calibration is None and touch.down():
                    print('raw:', touch.read_raw(), flush=True)
                    while touch.down():
                        time.sleep(0.03)
                time.sleep(0.02)
        except KeyboardInterrupt:
            pass
        finally:
            touch.close()
        return

    display = Display(args.baudrate)
    print(f"[V19-E-FPS30] TFT SPI={args.baudrate} Hz; VIDEO refresh={args.video_refresh:.3f}s", flush=True)
    touch = None
    # The launcher terminates the TFT on exit. Let Python run its cleanup.
    def stop_display(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_display)
    signal.signal(signal.SIGHUP, stop_display)
    try:
        if args.touch_calibrate or args.touch_calibrate_manual or not args.no_touch:
            touch = Touch()
        if args.touch_calibrate or args.touch_calibrate_manual:
            calibrate(display, touch, args.calibration_file, manual=args.touch_calibrate_manual)
            return
        cal = None
        if touch:
            try:
                cal = Calibration.load(args.calibration_file)
                print(f'TOUCH: calibracion cargada: {args.calibration_file}', flush=True)
            except (ValueError, OSError, KeyError, TypeError):
                print('TOUCH: calibration missing. Run --touch-calibrate; display will still work.', flush=True)
        page, band = args.page, args.band
        page_index, selected = 0, None
        last_frame = 0.0
        last_ui_status = 0.0
        recent_displays = deque(maxlen=120)
        last_perf_report = time.monotonic()
        last_status = {'overall_state': 'NO_DATA'}
        cache = VideoFrameCache(args.raw_frame_file, args.video_frame_file)
        last_video_mode = False
        last_cache_poll = 0.0
        video_background_drawn = False
        video_dirty = False
        last_overlay = 0.0
        tapper = TouchTapDetector()
        feedback = ''
        feedback_until = 0.0
        print('TFT running. Ctrl+C to exit.', flush=True)
        while True:
            now = time.monotonic()
            # Poll state independently from the SPI redraw rate.
            if now - last_ui_status > 0.20:
                last_status = demo_data() if args.demo else read_status(args.state_file, args.max_age)
                last_ui_status = now
            if touch and cal:
                tap = tapper.poll(touch, cal, now)
                if tap is not None:
                    sx, sy, raw = tap
                    connected = (last_status.get('overall_state') not in
                                 ('NO_DATA', 'STOPPED', 'DISCONNECTED'))
                    old_page, old_band = page, band
                    old_page_index, old_selection = page_index, selected
                    page, band, page_index, selected, command = handle_tap(
                        sx, sy, page, band, connected, last_status, page_index, selected)
                    if args.touch_debug:
                        print(f'TOUCH raw=({raw[0]:.0f},{raw[1]:.0f}) '
                              f'pixel=({sx},{sy}) page={old_page}->{page} '
                              f'band={old_band}->{band} '
                              f'action={command or "NONE"} connected={connected}', flush=True)
                    if command and not args.demo:
                        try:
                            if command == 'START_VIDEO':
                                save_video_selection(args.selection_file, selected)
                                send_command(args.command_file, command, region=selected)
                                feedback = 'SINTONIZANDO CANAL ELEGIDO'
                            else:
                                send_command(args.command_file, command)
                                feedback = 'ORDEN ENVIADA'
                        except (OSError, ValueError) as exc:
                            feedback = 'ERROR AL ENVIAR'
                            print(f'TOUCH IPC ERROR: {exc}', flush=True)
                        feedback_until = now + 1.5
                    if (page != old_page or band != old_band or command
                            or page_index != old_page_index or selected != old_selection):
                        last_frame = -1e9
                        video_background_drawn = False
                        video_dirty = True
            mode_video = last_status.get('receiver_mode') == 'VIDEO'
            if mode_video != last_video_mode:
                if mode_video:
                    page = 'video'
                    cache.reset()
                    video_background_drawn = False
                else:
                    if page == 'video':
                        page, selected, page_index = 'signals', None, 0
                    cache.reset()
                    recent_displays.clear()
                last_video_mode = mode_video
                last_frame = -1e9
                video_dirty = True
                last_overlay = 0.0
            message = feedback if now < feedback_until else ''
            if mode_video:
                # Retain dirty frames until SPI is available; we never allow
                # a new frame to disappear just because the 90ms gate was busy.
                # Max 125 polls/s: monitor the newest /dev/shm frame; do not queue old frames.
                if now - last_cache_poll >= 0.008:
                    last_cache_poll = now
                    if cache.update(args.freeze_after, color_only=(last_status.get('video') or {}).get('backend')=='fpvdec'):
                        video_dirty = True
                age = cache.age()
                fresh_image = cache.valid_image(args.freeze_after)
                due = now - last_frame >= args.video_refresh
                overlay_due = now - last_overlay >= 1.0
                if due and (video_dirty or overlay_due or not video_background_drawn):
                    render_start = time.monotonic()
                    times = [t for t in recent_displays if render_start-t <= 3.5]
                    fps = ((len(times)-1)/max(.001,times[-1]-times[0]) if len(times)>1 else 0.)
                    drawn_video = False
                    if not video_background_drawn:
                        image_out=render(last_status,'video',band,message,page_index,
                                         selected,frame_path=None,video_fps=fps,
                                         frame_image=fresh_image,frame_age=age,frame_source=cache.source)
                        display.image(image_out)
                        video_background_drawn = True
                        last_overlay = render_start
                        drawn_video = bool(fresh_image is not None and video_dirty)
                    else:
                        if video_dirty and fresh_image is not None:
                            # Fastest supported path: ONLY the 236x177 video
                            # rectangle is sent; no full-screen PIL redraw.
                            display.video_frame(fresh_image)
                            drawn_video = True
                        elif fresh_image is None and overlay_due:
                            image_out=render(last_status,'video',band,message,page_index,
                                             selected,frame_path=None,video_fps=fps,
                                             frame_image=None,frame_age=age,frame_source=cache.source)
                            display.region(image_out,(1,68,239,251))
                        if overlay_due:
                            image_out=render(last_status,'video',band,message,page_index,
                                             selected,frame_path=None,video_fps=fps,
                                             frame_image=fresh_image,frame_age=age,frame_source=cache.source)
                            display.region(image_out,(3,250,238,273))
                            last_overlay = render_start
                    display_finished=time.monotonic()
                    if drawn_video:
                        recent_displays.append(display_finished)
                    video_dirty=False
                    # Gate from START of transfer, not AFTER it finishes.
                    last_frame=render_start
                    if display_finished-last_perf_report >= 6:
                        times=[t for t in recent_displays if display_finished-t <= 6]
                        fps=((len(times)-1)/max(.001,times[-1]-times[0]) if len(times)>1 else 0.)
                        label=f'{age:.2f}s' if age is not None else 'NO_FRAME'
                        print(f'[V19-E-FPS30] TFT={fps:.1f}fps age={label} '
                              f'SPI+render={(display_finished-render_start)*1000:.0f}ms '
                              f'source={cache.source or "NONE"}',flush=True)
                        last_perf_report=display_finished
            elif now - last_frame >= args.refresh:
                regions = valid_regions(last_status)
                if page == 'signals' and page_index*3 >= len(regions):
                    page_index = 0
                display.image(render(last_status, page, band, message, page_index,
                                     selected, frame_path=args.video_frame_file))
                last_frame = time.monotonic()
            # In VIDEO, permit 30 fps if real SPI bandwidth + processing allow it.
            time.sleep(0.002 if mode_video else 0.015)
    except KeyboardInterrupt:
        print('TFT stopped.', flush=True)
    finally:
        # A TFT retains its last image; blank it when we exit normally.
        try:
            display.image(Image.new('RGB', (W, H), 'black'))
        except Exception as exc:
            print(f'TFT cleanup could not blank display: {exc}', flush=True)
        if touch:
            touch.close()


if __name__ == '__main__':
    main()