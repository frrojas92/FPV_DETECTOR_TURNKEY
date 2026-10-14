#!/usr/bin/env python3
"""V18.1: one latest 8-bit grayscale FPV frame, atomically published in tmpfs.

The timestamp is the *publication* time, not the camera exposure time.
This protocol must not be used to claim end-to-end video latency.
"""
import os
import struct
import time
from pathlib import Path

MAGIC = b'FPVR'
VERSION = 1
# magic, version, pixel_format (1=Y8), width, height, sequence, unix_ns
HEADER = struct.Struct('<4sBBHHQQ')


def default_path():
    return Path(os.environ.get('FPV_RAW_FRAME_FILE',
                     f'/dev/shm/fpv_frame_{os.getuid()}.raw'))


def pack_frame(gray_bytes, width, height, sequence, unix_ns=None):
    width, height = int(width), int(height)
    if not (1 <= width <= 720 and 1 <= height <= 576):
        raise ValueError('invalid image dimensions')
    payload = bytes(gray_bytes)
    if len(payload) != width * height:
        raise ValueError('wrong frame byte length')
    if unix_ns is None:
        unix_ns = time.time_ns()
    return (HEADER.pack(MAGIC, VERSION, 1, width, height,
                        int(sequence), int(unix_ns)) + payload)


def unpack_frame(blob):
    if len(blob) < HEADER.size:
        raise ValueError('incomplete frame header')
    magic, version, pix_fmt, w, h, seq, unix_ns = HEADER.unpack_from(blob)
    if magic != MAGIC or version != VERSION or pix_fmt != 1:
        raise ValueError('incompatible frame format')
    if not (1 <= w <= 720 and 1 <= h <= 576):
        raise ValueError('invalid dimensions')
    if len(blob) != HEADER.size + w*h:
        raise ValueError('incomplete or extra frame data')
    return w, h, seq, unix_ns, blob[HEADER.size:]


def read_frame(path=None, max_age_s=3.0):
    """Return (w,h,seq,unix_ns,age_s,payload), or None for missing/old/corrupt."""
    target = Path(path) if path is not None else default_path()
    try:
        data = target.read_bytes()  # os.replace ensures fully formed snapshots
        w, h, seq, stamp, payload = unpack_frame(data)
        age = (time.time_ns() - stamp) / 1e9
        if not 0.0 <= age <= max_age_s:
            return None
        return w, h, seq, stamp, age, payload
    except (OSError, ValueError, struct.error):
        return None
