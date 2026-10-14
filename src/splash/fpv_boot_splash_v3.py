#!/usr/bin/env python3
"""FPV Boot Splash V3: consola de arranque (resumen) y hardware en ILI9341.

Permanece en la pantalla durante la espera de 30 s y termina ANTES de
iniciar V19-E RAMTEST. No abre HackRF ni audio y no utiliza XPT2046.

No puede reproducir mensajes anteriores a Linux en tiempo real. Consulta
el registro del kernel si el usuario dispone de permiso; en caso contrario
muestra hechos verificados desde /proc y /sys.
"""
from __future__ import annotations

import argparse
import os
import platform
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont

BG = (7, 17, 27)
CARD = (18, 33, 44)
EDGE = (42, 74, 88)
TEAL = (76, 215, 191)
WHITE = (224, 236, 238)
MUTED = (136, 159, 170)
GREEN = (103, 212, 145)
AMBER = (246, 199, 92)
DIM = (93, 115, 128)
FONT_REG = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_STOP = False


def on_stop(*_):
    global _STOP
    _STOP = True


def font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)
    except OSError:
        return ImageFont.load_default()


def file_exists(path):
    return Path(path).exists()


def hackrf_present():
    for f in Path('/sys/bus/usb/devices').glob('*/idVendor'):
        try:
            if f.read_text().strip().lower() == '1d50' and (f.parent / 'idProduct').read_text().strip().lower() == '6089':
                return True
        except (OSError, PermissionError):
            continue
    return False


def usb_audio_present():
    try:
        cards = Path('/proc/asound/cards').read_text().lower()
    except (OSError, PermissionError):
        return False
    return 'kt usb audio' in cards or 'usb-audio' in cards or 'usb audio' in cards


def checks():
    return [
        ('SPI ILI9341', file_exists('/dev/spidev0.0')),
        ('TACTIL XPT2046', file_exists('/dev/spidev0.1')),
        ('HACKRF ONE', hackrf_present()),
        ('AUDIO USB', usb_audio_present()),
        ('RAM /DEV/SHM', Path('/dev/shm').is_dir() and os.access('/dev/shm', os.W_OK)),
    ]


def root_fs():
    try:
        for line in Path('/proc/mounts').read_text().splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[1] == '/':
                return fields[2]
    except OSError:
        pass
    return None


def kernel_events_from_text(data):
    """Extraer sólo mensajes ya emitidos, con selección conservadora."""
    markers = [
        (r'Machine model:\s*(.+)', 'RPi'),
        (r'mmcblk0:\s*(?:p1\s+p2|.*GiB)', 'MMC'),
        (r'EXT4-fs\s*\([^)]*\):\s*mounted filesystem\s*(.*)', 'EXT4'),
        (r'usbcore: registered new interface driver snd-usb-audio', 'ALSA'),
    ]
    collected = []
    for expression, prefix in markers:
        regex = re.compile(expression, re.I)
        for line in data.splitlines():
            m = regex.search(line)
            if m:
                detail = re.sub(r'[\x00-\x1f\x7f\x1b]+', ' ', m.group(0)).strip()
                if prefix == 'RPi':
                    detail = 'HW ' + (m.group(1) or '').strip()
                elif prefix == 'MMC':
                    detail = 'SD: ' + detail.split('mmcblk0:', 1)[-1].strip()
                elif prefix == 'EXT4':
                    detail = 'EXT4: particion raiz montada'
                else:
                    detail = 'ALSA: driver USB cargado'
                collected.append(detail)
                break
    return collected


def boot_events():
    """Información real; no requiere privilegios ni modifica el sistema."""
    cmd_list = [
        ['journalctl', '-b', '-k', '--no-pager', '-o', 'cat', '-n', '500'],
        ['dmesg', '--color=never'],
    ]
    for cmd in cmd_list:
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=2.0, text=True, check=False)
            if result.returncode == 0:
                events = kernel_events_from_text(result.stdout)
                if events:
                    return 'KERNEL LOG', events
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
    # Fallback: no fingir que se han leído registros del kernel.
    events = ['Linux ' + platform.release()]
    filesystem = root_fs()
    if filesystem:
        events.append('ROOT: ' + filesystem + ' montada')
    if Path('/sys/block/mmcblk0').exists():
        events.append('SD/MMC: dispositivo detectado')
    return 'ESTADO DEL SISTEMA', events


def shorten(draw, line, size, width, bold=False):
    font_ = font(size, bold)
    line = line.replace('\n', ' ').strip()
    if draw.textlength(line, font=font_) <= width:
        return line
    while line and draw.textlength(line + '..', font=font_) > width:
        line = line[:-1]
    return (line.rstrip() + '..') if line else '..'


def compose(events_title, events, state, seconds_left, total, tick=0):
    w, h = 240, 320
    im = Image.new('RGB', (w, h), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w-1, h-1), outline=EDGE, width=2)
    d.rounded_rectangle((12, 10, 228, 14), radius=2, fill=TEAL)
    d.text((14, 24), 'FPV DETECTOR', font=font(18, True), fill=WHITE)
    d.text((14, 48), 'LINUX BOOT  /  V19-E RAMTEST', font=font(9), fill=TEAL)
    d.line((14, 68, 226, 68), fill=EDGE)
    d.text((14, 78), events_title, font=font(10, True), fill=MUTED)
    # Scroll de eventos reales previamente obtenidos; no representa logs en vivo.
    events = list(events) or ['Sin datos del sistema']
    start = (tick // 5) % max(1, len(events))
    for i in range(3):
        text = events[(start + i) % len(events)] if len(events) >= 3 else (events[i] if i < len(events) else '')
        if text:
            d.text((15, 99 + i * 17), '> ' + shorten(d, text, 9, 200), font=font(9), fill=WHITE)
    d.line((14, 159, 226, 159), fill=EDGE)
    d.text((14, 166), 'COMPROBACION DE HARDWARE', font=font(9, True), fill=MUTED)
    for i, (name, ok) in enumerate(state):
        y = 187 + 18 * i
        d.rounded_rectangle((14, y + 3, 22, y + 11), radius=2, fill=GREEN if ok else AMBER)
        d.text((28, y), name, font=font(10), fill=WHITE)
        label = 'OK' if ok else 'WAIT'
        d.text((196 if ok else 180, y), label, font=font(9, True), fill=GREEN if ok else AMBER)
    d.line((14, 280, 226, 280), fill=EDGE)
    d.text((14, 286), f'DETECTOR EN {seconds_left:02d} S', font=font(12, True), fill=TEAL)
    # Barra de tiempo, no representa porcentaje de preparación del hardware.
    d.rounded_rectangle((14, 309, 226, 314), radius=2, fill=CARD)
    fraction = max(0, min(1, (total - seconds_left) / max(total, 1)))
    if fraction:
        d.rounded_rectangle((14, 309, 14 + int(212 * fraction), 314), radius=2, fill=TEAL)
    return im


def run(wait_seconds=30, fps=0.9, rotation=0, baudrate=12000000):
    deadline = time.monotonic() + 15
    while not file_exists('/dev/spidev0.0') and not _STOP:
        if time.monotonic() >= deadline:
            print('ERROR: SPI0.0 no disponible', file=sys.stderr, flush=True)
            return 1
        time.sleep(.2)
    if _STOP:
        return 143

    import board
    import digitalio
    from adafruit_rgb_display import ili9341

    pins = []
    try:
        cs = digitalio.DigitalInOut(board.D5); pins.append(cs)
        dc = digitalio.DigitalInOut(board.D25); pins.append(dc)
        rst = digitalio.DigitalInOut(board.D24); pins.append(rst)
        lcd = ili9341.ILI9341(board.SPI(), cs=cs, dc=dc, rst=rst,
                              rotation=rotation, baudrate=baudrate)
        if (lcd.width, lcd.height) != (240, 320):
            raise RuntimeError(f'TFT: tamaño {lcd.width}x{lcd.height}; se requiere 240x320 (rotation=0)')
        title, events = boot_events()
        print(f'Splash V3: {title}; {len(events)} eventos de referencia', flush=True)
        began = time.monotonic()
        deadline = began + wait_seconds
        next_frame = began
        prev_state = None
        prev_left = None
        frame_every = 1 / max(.2, fps)
        while not _STOP:
            now = time.monotonic()
            remaining = max(0, int(deadline - now + .999999))
            hw = checks()
            tick = int(now - began)
            # Imagen sólo cuando algo cambió o vence la cadencia.
            key = (tuple(hw), remaining, tick // 5)
            if key != prev_state or now >= next_frame:
                lcd.image(compose(title, events, hw, remaining, wait_seconds, tick))
                prev_state = key
                next_frame = time.monotonic() + frame_every
            if remaining != prev_left:
                print('Splash V3: faltan {}s | {}'.format(
                    remaining, ' '.join('{}:{}'.format(n, 'OK' if ok else 'WAIT') for n, ok in hw)),
                    flush=True)
                prev_left = remaining
            if now >= deadline:
                break
            time.sleep(min(.15, max(.01, next_frame - time.monotonic())))
        print('Splash V3: liberando SPI antes de RAMTEST', flush=True)
        return 143 if _STOP else 0
    finally:
        for pin in reversed(pins):
            try:
                pin.deinit()
            except Exception:
                pass
        # Cuando el proceso sale se libera también el dispositivo SPI.


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--preview', type=Path, help='Generar PNG sin acceder a hardware')
    ap.add_argument('--wait-seconds', type=int, default=30)
    ap.add_argument('--fps', type=float, default=.9)
    args = ap.parse_args()
    if not 1 <= args.wait_seconds <= 600:
        ap.error('El tiempo de espera debe estar entre 1 y 600 segundos')
    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        im = compose('KERNEL LOG',
                     ['Machine model: Raspberry Pi 5', 'mmcblk0: p1 p2', 'EXT4: particion raiz montada'],
                     [('SPI ILI9341', True), ('TACTIL XPT2046', True),
                      ('HACKRF ONE', True), ('AUDIO USB', True), ('RAM /DEV/SHM', True)],
                     args.wait_seconds // 2, args.wait_seconds)
        im.save(args.preview)
        print('Preview:', args.preview)
        return 0
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    try:
        return run(args.wait_seconds, args.fps,
                   rotation=int(os.environ.get('FPV_SPLASH_ROTATION', '0')),
                   baudrate=int(os.environ.get('FPV_SPLASH_BAUDRATE', '12000000')))
    except Exception as exc:
        print(f'ERROR splash V3: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        raise


if __name__ == '__main__':
    sys.exit(main())
