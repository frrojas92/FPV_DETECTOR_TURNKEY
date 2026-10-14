"""Atomic RGB24 NTSC color frame exchange over /dev/shm.

Color source must be a genuine chrominance decoder; grayscale-to-RGB copies
MUST NOT be published through this format.
"""
import os
import struct
import time
from pathlib import Path

HEADER = struct.Struct('<4sBBHHQQ')  # magic, version, pixfmt, w,h,seq,timestamp
MAGIC = b'FPVR'
RGB24 = 2


def default_path():
    return Path(os.getenv('FPV_COLOR_FRAME_FILE', f'/dev/shm/fpv_color_{os.getuid()}.raw'))


def pack_rgb24(data, w, h, seq, stamp_ns=None):
    w, h = int(w), int(h)
    data = bytes(data)
    if not (1 <= w <= 720 and 1 <= h <= 576) or len(data) != 3 * w * h:
        raise ValueError('invalid RGB24 frame dimensions/payload')
    return HEADER.pack(MAGIC, 2, RGB24, w, h, int(seq),
                       int(stamp_ns or time.time_ns())) + data


def read_color(path=None, max_age_s=1.0):
    target = Path(path) if path is not None else default_path()
    try:
        blob = target.read_bytes()
        if len(blob) < HEADER.size:
            return None
        magic, version, fmt, w, h, seq, stamp = HEADER.unpack_from(blob)
        if magic != MAGIC or version != 2 or fmt != RGB24:
            return None
        if not (1 <= w <= 720 and 1 <= h <= 576) or len(blob) != HEADER.size + 3*w*h:
            return None
        age = (time.time_ns() - stamp)/1e9
        if age < 0 or age > max_age_s:
            return None
        return w, h, seq, stamp, age, blob[HEADER.size:]
    except (OSError, ValueError, struct.error):
        return None
