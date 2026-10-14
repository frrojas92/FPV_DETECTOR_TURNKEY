#!/usr/bin/env python3
"""Shared FPV spectrum palette and scan-bin aggregation, independently implemented.

Color stops are visually aligned with the terminal spectrum shown in
https://github.com/lukeswitz/fpv-sdr (the original fpv_spectrum.py has a GPLv3
license; this module is a separate implementation, without code copied from it).

Our power measurements come from hackrf_sweep and are NOT calibrated dBm/dBFS.
"""
import math
import statistics

# Same approximate color progression for TUI and TFT: blue -> cyan ->
# turquoise -> yellow -> orange -> red. These values are graphics, not likelihood.
STOPS = ((40, 60, 100), (0, 110, 200), (0, 200, 150),
         (210, 210, 0), (240, 110, 0), (255, 40, 40))


def palette_index(delta_db):
    """Discrete color index: 0..5 for relative energy 0..20 dB."""
    if delta_db is None or not math.isfinite(float(delta_db)):
        return 0
    return max(0, min(5, int(max(0.0, min(20.0, float(delta_db))) * 5.0 / 20.0 + 0.5)))


def palette_rgb(delta_db):
    """Interpolated RGB using the identical physical scale on both screens."""
    if delta_db is None or not math.isfinite(float(delta_db)):
        return STOPS[0]
    t = min(1.0, max(0.0, float(delta_db) / 20.0)) * 5
    i = min(4, int(t))
    u = t - i
    return tuple(int(round(STOPS[i][k] * (1-u) + STOPS[i+1][k] * u)) for k in range(3))


def scan_columns(scan_buffer, now, width=96, max_age=1.5):
    """Map fresh hackrf_sweep bins to columns; output dB above sweep median.

    A column takes the MAX of fresh frequency bins in its interval; invalid
    coverage is represented by None rather than fabricating a low-power value.
    Baseline is the median of all fresh original RF bins, not the median of peaks.
    """
    width = max(1, int(width))
    lo, hi = float(scan_buffer.start), float(scan_buffer.stop)
    span = hi - lo
    if span <= 0:
        return [None] * width
    buckets = [[] for _ in range(width)]
    fresh_values = []
    for freq, entry in scan_buffer.bins.items():
        power, timestamp = entry[:2]
        if now - timestamp > max_age or not math.isfinite(power) or not lo <= freq < hi:
            continue
        index = min(width - 1, int((freq - lo) / span * width))
        buckets[index].append(power)
        fresh_values.append(power)
    if not fresh_values:
        return [None] * width
    floor = statistics.median(fresh_values)
    return [round(max(values) - floor, 1) if values else None for values in buckets]


def downsample(columns, n):
    """Combine stored spectral bins for a smaller TFT view without extrapolation."""
    n = int(n)
    if n <= 0:
        return []
    if not columns:
        return [None] * n
    result = []
    for j in range(n):
        a = int(j * len(columns) / n)
        b = max(a + 1, int((j+1) * len(columns) / n))
        values = [x for x in columns[a:b] if isinstance(x, (int, float))]
        result.append(round(max(values), 1) if values else None)
    return result


def xterm_index(rgb):
    """Nearest xterm-256 cube entry; no changes to the terminal palette."""
    r, g, b = rgb
    levels = [round(v / 255 * 5) for v in (r,g,b)]
    return 16 + 36 * levels[0] + 6 * levels[1] + levels[2]
