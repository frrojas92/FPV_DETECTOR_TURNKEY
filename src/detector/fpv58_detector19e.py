#!/usr/bin/env python3
"""HackRF 5.8 GHz analog-FPV RF monitor V5.3 with live acoustic RF-strength cue (NOT drone identification).

Changes since V5.1:
* Real elapsed-time RF occupancy, minimum observation span and sampling coverage.
* Three RF states: BACKGROUND, RF_CANDIDATE, PERSISTENT_RF (+ CALIBRATING/STALE).
* Sustained-low release using observed time, rather than consecutive hit counts.
* Initial warning.wav followed by variable-pitch periodic beeps while persistent RF is fresh.
* Script-relative warning.wav, operator markers, and expanded CSV diagnostics.

Keyboard: 0/1 = TX OFF/ON LOG MARKERS, m = event marker, c = calibrate
          (TX OFF), +/- = adjust trigger, a = mute, r = reset, q = quit.
Use a known test VTX and controlled measurements before operational use.
"""

import argparse
import array
import csv
import json
import os
import curses
import math
import queue
import random
import sys
import shutil
import statistics
import subprocess
import threading
import time
import wave
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fpv_spectrum_v17 import (STOPS, palette_index, palette_rgb, xterm_index,
                              scan_columns, downsample)

BANDS = {
    'A': [5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725],
    'B': [5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866],
    'E': [5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945],
    'F': [5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880],
    'R': [5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917],
}
CHANNELS = [(band, i, fc) for band, freqs in BANDS.items()
            for i, fc in enumerate(freqs)]
HERE = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = HERE / "fpv_status.json"
DEFAULT_TOUCH_COMMAND_FILE = HERE / "fpv_touch_command.json"
# V17: single-HackRF scan/video switching; optional fpv-sdr PAL/NTSC viewer.


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def parse_line(line):
    """Parse one hackrf_sweep CSV segment: frequency bounds in Hz, bins in dB."""
    try:
        row = next(csv.reader([line]))
        if len(row) < 7:
            return None
        lo, hi, step = [float(row[i]) / 1e6 for i in (2, 3, 4)]
        if not (0 < step <= 20 and 0 < lo < hi):
            return None
        points = []
        for i, value in enumerate(row[6:]):
            if not value.strip():
                continue
            f, p = lo + (i + 0.5) * step, float(value)
            if math.isfinite(p) and f < hi + 0.001:
                points.append((f, p))
        return points or None
    except (ValueError, IndexError, OverflowError, csv.Error):
        return None


def mean_db(values):
    top = max(values)
    return top + 10 * math.log10(
        sum(10 ** ((v - top) / 10) for v in values) / len(values))


class ScanBuffer:
    """Combine adjacent HackRF frequency segments into channel measurements."""
    def __init__(self, start, stop, window, max_age=1.5):
        self.start, self.stop, self.window = start, stop, window
        self.max_age = max_age
        self.bins = {}
        self.seq = 0
        self.last_emit = {}

    def feed(self, points, now):
        self.seq += 1
        for freq, power in points:
            self.bins[round(freq, 4)] = (power, now, self.seq)
        if self.seq % 50 == 0:
            self.bins = {f: entry for f, entry in self.bins.items()
                         if now - entry[1] <= self.max_age}
        result = {}
        for band, i, fc in CHANNELS:
            key = (band, i)
            if fc - self.window / 2 < self.start or fc + self.window / 2 > self.stop:
                continue
            vals = [(f, p, seq) for f, (p, stamp, seq) in self.bins.items()
                    if abs(f - fc) <= self.window / 2 and now - stamp <= self.max_age]
            if len(vals) < 3:
                continue
            frequencies = [f for f, _, _ in vals]
            if (min(frequencies) > fc - self.window * 0.22 or
                    max(frequencies) < fc + self.window * 0.22):
                continue
            seq = max(item[2] for item in vals)
            if seq <= self.last_emit.get(key, -1):
                continue
            powers = [p for _, p, _ in vals]
            result[key] = (mean_db(powers), max(powers), len(powers))
            self.last_emit[key] = seq
        return result


class ChannelState:
    def __init__(self, smoothing):
        self.background = deque(maxlen=300)
        self.calibrated = False
        self.baseline = None
        self.noise_gate = None
        self.smooth_history = deque(maxlen=smoothing)
        self.observations = deque()  # (monotonic time, above threshold)
        self.active = False
        self.candidate = False
        self.candidate_low_since = None
        self.last_seen = None
        self.gap = None
        self.last_power = None
        self.last_peak = None
        self.last_bins = 0
        self.delta = None
        self.smoothed = None
        self.score = 0
        self.occupancy = 0.0
        self.observed_s = 0.0
        self.span_s = 0.0
        self.observation_count = 0
        self.detected_at = None
        self.low_since = None
        self.low_covered_s = 0.0
        self.low_samples = 0
        self.expired = False


def temporal_stats(observations, max_gap, min_spacing):
    """Integrate observed *elapsed time*; unmeasured gaps contribute zero.

    The midpoint rule makes an isolated spike have little occupancy. Returning
    raw duration and covered time prevents sparse samples from masquerading as
    persistent transmission.
    """
    entries = list(observations)
    if not entries:
        return 0.0, 0.0, 0.0, 0
    span = entries[-1][0] - entries[0][0]
    covered, high = 0.0, 0.0
    distinct = 0
    last_counted = -float('inf')
    for t, flag in entries:
        if t - last_counted >= min_spacing - 1e-8:
            distinct += 1
            last_counted = t
    for (t0, on0), (t1, on1) in zip(entries, entries[1:]):
        dt = t1 - t0
        if 0 < dt <= max_gap:
            covered += dt
            high += dt * (int(on0) + int(on1)) / 2
    return high / covered if covered > 0 else 0.0, covered, span, distinct


class Detector:
    def __init__(self, args):
        self.args = args
        self.states = {(b, i): ChannelState(args.smoothing)
                       for b, i, _ in CHANNELS}
        self.calibration_start = None

    def begin_calibration(self, now):
        self.states = {key: ChannelState(self.args.smoothing)
                       for key in self.states}
        self.calibration_start = now

    def calibration_remaining(self, now):
        return max(0.0, self.args.calibration_seconds -
                   (now - self.calibration_start))

    def update(self, measurements, now, threshold, phase):
        changes = []
        a = self.args
        for key, (power, peak, bin_count) in measurements.items():
            s = self.states[key]
            gap = None if s.last_seen is None else now - s.last_seen
            s.gap = gap
            s.last_seen = now
            s.last_power, s.last_peak, s.last_bins = power, peak, bin_count
            s.expired = False
            if not s.calibrated:
                rem = self.calibration_remaining(now)
                if rem > 0:
                    if phase != 'TX_ON':
                        s.background.append(power)
                    if s.background:
                        s.baseline = statistics.median(s.background)
                        s.delta = power - s.baseline
                        s.smoothed = s.delta
                    s.score = 0
                    continue
                if len(s.background) < a.calibration_min_samples:
                    # If coverage was sparse during the startup timer, finish
                    # learning from subsequent OFF-labeled measurements.
                    # Never create a detection from an uncalibrated channel.
                    if phase != 'TX_ON':
                        s.background.append(power)
                    if len(s.background) < a.calibration_min_samples:
                        s.score = 0
                        continue
                ordered = sorted(s.background)
                quantile = ordered[int(.95 * (len(ordered) - 1))]
                s.baseline = statistics.median(ordered)
                s.noise_gate = max(threshold, quantile - s.baseline + a.noise_margin)
                s.calibrated = True
                s.smooth_history.clear()
                s.observations.clear()
                s.candidate = False

            s.delta = power - s.baseline
            if gap is not None and gap > a.observation_max_gap:
                # Do not join two unrelated sampling periods.
                s.smooth_history.clear()
                s.observations.clear()
                s.candidate = False
                s.candidate_low_since = None
                s.low_since = None
                s.low_samples = 0
                s.low_covered_s = 0.0
            s.smooth_history.append(s.delta)
            s.smoothed = statistics.median(s.smooth_history)
            gate = max(threshold, s.noise_gate)
            above = s.smoothed >= gate
            low = s.smoothed <= a.release_threshold
            # Measure persistence from the START of a candidate episode, not
            # from older quiet samples. Otherwise prior background adds a
            # needless ~2.5s delay to initial acquisition.
            if not s.active:
                if above:
                    if not s.candidate:
                        s.candidate = True
                        s.observations.clear()
                    s.candidate_low_since = None
                elif s.candidate:
                    if s.candidate_low_since is None:
                        s.candidate_low_since = now
                    elif now - s.candidate_low_since >= a.candidate_reset_seconds:
                        s.candidate = False
                        s.candidate_low_since = None
                        s.observations.clear()
            if s.active or s.candidate:
                s.observations.append((now, above))
            # Retain one sample before the rolling window, if available.
            cutoff = now - a.occupancy_window
            while len(s.observations) > 2 and s.observations[1][0] < cutoff:
                s.observations.popleft()
            occ, covered, span, count = temporal_stats(
                s.observations, a.observation_max_gap, a.min_observation_spacing)
            s.occupancy, s.observed_s, s.span_s = occ, covered, span
            s.observation_count = count
            sufficiently_observed = (span >= a.min_presence_seconds and
                                     covered >= a.min_presence_seconds * a.min_coverage and
                                     covered >= span * a.min_coverage and
                                     count >= a.min_observations)
            if s.active:
                if low:
                    if s.low_since is None:
                        s.low_since = now
                        s.low_covered_s = 0.0
                        s.low_samples = 1
                    else:
                        if gap is not None and 0 < gap <= a.observation_max_gap:
                            s.low_covered_s += gap
                        s.low_samples += 1
                    if (now - s.low_since >= a.release_seconds and
                            s.low_covered_s >= a.release_seconds * a.min_coverage and
                            s.low_samples >= a.min_observations and
                            now - s.detected_at >= a.min_hold_seconds):
                        s.active = False
                        s.candidate = False
                        s.detected_at = None
                        s.low_since = None
                        s.low_covered_s = 0.0
                        changes.append((key, 'CLEAR', s.smoothed))
                else:
                    # Signal level has recovered, or is in the hysteresis zone.
                    s.low_since = None
                    s.low_covered_s = 0.0
                    s.low_samples = 0
            else:
                if above and s.candidate and sufficiently_observed and occ >= a.min_occupancy:
                    s.active = True
                    s.candidate = False
                    s.detected_at = now
                    s.low_since = None
                    changes.append((key, 'DETECTED', s.smoothed))

            # RF persistence score, *not* probability of a drone.
            # Low isolated peaks may show as candidates but will not sound alarms.
            if s.active:
                strength = min(20, max(0.0, s.smoothed - a.release_threshold) * 3)
                s.score = min(100, round(55 + 25 * s.occupancy + strength))
            elif s.candidate:
                strength = min(15, max(0, s.smoothed - gate) * 3)
                s.score = min(75, round(35 * s.occupancy +
                                        25 * min(1, covered / a.min_presence_seconds) +
                                        strength))
            else:
                s.score = 0
        return changes

    def expire(self, now):
        changes = []
        for key, s in self.states.items():
            if (s.last_seen is None or s.expired or
                    now - s.last_seen <= self.args.stale_seconds):
                continue
            if s.active:
                s.active = False
                changes.append((key, 'STALE_CLEAR', s.smoothed))
            s.candidate = False
            s.candidate_low_since = None
            s.score = 0
            s.low_since = None
            s.observations.clear()
            s.smooth_history.clear()
            s.expired = True
        return changes

    def status(self, key, now):
        s = self.states[key]
        if s.last_seen is None:
            return 'UNSEEN'
        if not s.calibrated:
            return 'CALIBRATING' if self.calibration_remaining(now) > 0 else 'UNCALIBRATED'
        if now - s.last_seen > self.args.stale_seconds:
            return 'STALE'
        if s.active:
            return 'PERSISTENT_RF'
        if s.candidate:
            return 'RF_CANDIDATE'
        return 'BACKGROUND'


def rf_clusters(det, now, separation):
    active = sorted((freq, band, i) for band, i, freq in CHANNELS
                    if det.status((band, i), now) == 'PERSISTENT_RF')
    groups = []
    for entry in active:
        if not groups or entry[0] - groups[-1][-1][0] > separation:
            groups.append([])
        groups[-1].append(entry)
    return groups


def cluster_text(group, det):
    best = max(group, key=lambda entry: det.states[(entry[1], entry[2])].score)
    labels = ','.join(f'{band}{i+1}' for freq, band, i in group)
    return f'{best[0]} MHz [{labels}]'


class RegionTracker:
    """Suppress repeat alarms when neighboring channel labels flicker."""
    def __init__(self, separation, rearm_seconds):
        self.separation = separation
        self.rearm = rearm_seconds
        self.regions = []  # dict(center, keys, last_seen, visible)

    def update(self, groups, now):
        events = []
        used = set()
        for group in groups:
            frequencies = [item[0] for item in group]
            center = sum(frequencies) / len(frequencies)
            keys = {(band, i) for f, band, i in group}
            choices = [(idx, reg) for idx, reg in enumerate(self.regions)
                       if idx not in used and now - reg['last_seen'] < self.rearm and
                       (keys & reg['keys'] or abs(center - reg['center']) <= self.separation)]
            if choices:
                idx, reg = min(choices, key=lambda x: abs(center - x[1]['center']))
                if not reg['visible']:
                    events.append(('REGION_REAPPEARED', center, keys, False))
                reg.update(center=center, keys=keys, last_seen=now, visible=True)
                used.add(idx)
            else:
                self.regions.append(dict(center=center, keys=keys,
                                         last_seen=now, visible=True))
                idx = len(self.regions) - 1
                used.add(idx)
                events.append(('REGION_DETECTED', center, keys, True))
        for idx, reg in enumerate(self.regions):
            if idx not in used and reg['visible']:
                reg['visible'] = False
                events.append(('REGION_CLEAR', reg['center'], reg['keys'], False))
        self.regions = [r for r in self.regions
                        if r['visible'] or now - r['last_seen'] < self.rearm]
        return events


def reader(proc, output):
    try:
        for line in proc.stdout:
            points = parse_line(line)
            if points:
                try:
                    output.put_nowait(points)
                except queue.Full:
                    try:
                        output.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        output.put_nowait(points)
                    except queue.Full:
                        pass
    finally:
        try:
            output.put_nowait(None)
        except queue.Full:
            try:
                output.get_nowait()
            except queue.Empty:
                pass
            try:
                output.put_nowait(None)
            except queue.Full:
                pass


def demo_points(args, tx_on):
    lo = random.randrange(args.start, max(args.start + 1, args.stop - 8))
    return [(f + .5, -66 + random.gauss(0, 1.0) +
             (18 if tx_on and abs(f + .5 - args.focus) < 5 else 0))
            for f in range(lo, min(args.stop, lo + 8))]


def resolve_wav(name):
    p = Path(name).expanduser()
    return p if p.is_absolute() else HERE / p


def beep_parameters(relative_db, args):
    """Map relative RF level (NOT distance) to interval and audible pitch."""
    fraction = (relative_db - args.beep_weak_db) / (args.beep_strong_db - args.beep_weak_db)
    fraction = max(0.0, min(1.0, fraction))
    interval = args.beep_slow - fraction * (args.beep_slow - args.beep_fast)
    pitch = args.tone_low + fraction * (args.tone_high - args.tone_low)
    return interval, int(round(pitch))


def v19e6_wave_period(relative_db, args, wav_seconds):
    """Cadence measured start-to-start; WAV has constant pitch and playback rate.

    Honor physical duration of warning.wav, plus a short separation so the
    warning is nearly continuous but never overlaps itself.
    """
    requested, _pitch_ignored = beep_parameters(relative_db, args)
    return max(requested, wav_seconds + 0.02)


def synth_beep(hz, duration=0.10, sample_rate=22050):
    """Generate soft-edged mono S16_LE PCM without extra Python dependencies."""
    count = int(duration * sample_rate)
    edge = max(1, int(0.008 * sample_rate))
    samples = array.array('h')
    for n in range(count):
        envelope = min(1.0, n / edge, (count - 1 - n) / edge)
        samples.append(int(9000 * max(0.0, envelope) *
                           math.sin(2 * math.pi * hz * n / sample_rate)))
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples.tobytes()


class PulseAudio:
    """Beep in a separate thread, never delaying the RF scan or the curses TUI."""
    def __init__(self, args, wav):
        self.args, self.wav = args, wav
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.target = None  # (center MHz, relative strength dB)
        self.closed = False
        self.error = None
        self.pulse_count = 0
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name='rf-beep-audio')
        self.thread.start()

    def set_target(self, target):
        with self.lock:
            old = self.target
            self.target = target
        # Only wake immediately when the audio target enters/exits/changes.
        # Power changes should not prematurely trigger extra beeps.
        if (old is None) != (target is None) or (
                old is not None and target is not None and old[0] != target[0]):
            self.wake.set()

    def get_target(self):
        with self.lock:
            return self.target

    def _aplay(self, argv, raw=None):
        if not shutil.which('aplay'):
            self.error = 'aplay not installed: sudo apt install alsa-utils'
            return False
        try:
            result = subprocess.run(
                argv, input=raw, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=4.0)
            if result.returncode != 0:
                self.error = result.stderr.decode('utf-8', 'replace').strip()[:120] or 'aplay failed'
                return False
            self.error = None
            return True
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.error = str(exc)
            return False

    def _play_intro(self):
        if not self.args.no_intro and self.wav.is_file():
            return self._aplay(['aplay', '-q', '-D', self.args.audio_device, str(self.wav)])
        return True

    def _play_wave(self):
        """Play original warning.wav without pitch shifting or resampling."""
        if not self.wav.is_file():
            self.error = f'No existe WAV: {self.wav}'
            return False
        return self._aplay(['aplay', '-q', '-D', self.args.audio_device, str(self.wav)])

    def _wav_duration(self):
        try:
            with wave.open(str(self.wav), 'rb') as f:
                return f.getnframes() / f.getframerate()
        except (OSError, EOFError, ValueError, wave.Error, ZeroDivisionError) as exc:
            self.error = f'WAV no legible: {exc}'
            return 0.25

    def _play_tone(self, hz):
        pcm = synth_beep(hz, self.args.beep_duration)
        return self._aplay([
            'aplay', '-q', '-D', self.args.audio_device,
            '-t', 'raw', '-f', 'S16_LE', '-c', '1', '-r', '22050'], raw=pcm)

    def _sleep_interruptible(self, delay):
        """Wait without busy looping on ordinary strength changes."""
        until = time.monotonic() + delay
        while not self.closed:
            remaining = until - time.monotonic()
            if remaining <= 0 or self.get_target() is None:
                return
            if self.wake.wait(min(0.10, remaining)):
                self.wake.clear()
                # Leaving a signal should stop promptly, even during a pause.
                if self.get_target() is None:
                    return

    def _loop(self):
        previous_center = None
        wav_seconds = self._wav_duration() if self.args.audio_style == 'wav' else 0.0
        while not self.closed:
            target = self.get_target()
            if target is None:
                previous_center = None
                self.wake.wait(0.2)
                self.wake.clear()
                continue
            center, relative_db = target
            if center != previous_center:
                if self.args.audio_style == 'tone':
                    # Legacy behavior: intro WAV once then synthesized pitched beeps.
                    self._play_intro()
                previous_center = center
                if self.get_target() is None:
                    continue
            interval, hz = beep_parameters(relative_db, self.args)
            t0 = time.monotonic()
            if self.args.audio_style == 'wav':
                interval = v19e6_wave_period(relative_db, self.args, wav_seconds)
                ok = self._play_wave()
            else:
                ok = self._play_tone(hz)
            if ok:
                self.pulse_count += 1
            # Cadence varies with RF dB, not with estimated distance.
            # A persistent signal remains the only audible trigger.
            remaining = max((interval if ok else 1.0) - (time.monotonic() - t0), 0.02)
            self._sleep_interruptible(remaining)

    def close(self):
        self.closed = True
        self.set_target(None)
        self.wake.set()
        self.thread.join(timeout=4.2)


def best_audio_target(detector, now, max_age):
    """Choose one fresh persistent RF channel; never claim a distance."""
    choices = []
    for band, i, frequency in CHANNELS:
        s = detector.states[(band, i)]
        if (detector.status((band, i), now) != 'PERSISTENT_RF' or
                s.last_seen is None or now - s.last_seen > max_age or
                s.smoothed is None):
            continue
        choices.append((s.score, s.smoothed, frequency))
    if not choices:
        return None
    score, db, freq = max(choices)
    return (freq, db)


def init_spectrum_colors():
    """Prepare same continuous blue -> red 0..20 dB palette used on TFT.

    xterm-256: 21 small color steps, nearest 256-color approximation of
    fpv_spectrum_v17.palette_rgb. ANSI-only terminals fall back to six stops.
    """
    try:
        curses.start_color()
        curses.use_default_colors()
        fallback = (curses.COLOR_BLUE, curses.COLOR_CYAN, curses.COLOR_GREEN,
                    curses.COLOR_YELLOW, curses.COLOR_YELLOW, curses.COLOR_RED)
        for i, rgb in enumerate(STOPS):
            idx = xterm_index(rgb) if curses.COLORS >= 256 else fallback[i]
            curses.init_pair(20 + i, idx, -1)
        if curses.COLORS >= 256 and curses.COLOR_PAIRS > 61:
            for level in range(21):
                curses.init_pair(40 + level, xterm_index(palette_rgb(level)), -1)
        # V19-E.5: foreground with guaranteed black/white contrast on the
        # SAME quantized hue used by each plotted RF bar (21 backgrounds).
        if curses.COLOR_PAIRS > 91:
            fallback = (curses.COLOR_BLUE, curses.COLOR_CYAN, curses.COLOR_GREEN,
                        curses.COLOR_YELLOW, curses.COLOR_YELLOW, curses.COLOR_RED)
            if curses.COLORS >= 256:
                for level in range(21):
                    background = xterm_index(palette_rgb(level))
                    foreground = (16 if v19e5_light_background(background) else 231)
                    curses.init_pair(70 + level, foreground, background)
            else:
                for level in range(6):
                    foreground = (curses.COLOR_WHITE if level in (0, 5)
                                  else curses.COLOR_BLACK)
                    curses.init_pair(70 + level, foreground, fallback[level])
        return True
    except (curses.error, ValueError):
        return False


def v19e2_color_attr(value, colors_ok):
    """Color based exclusively on relative dB, never classification/score."""
    if not colors_ok or value is None:
        return curses.A_NORMAL
    try:
        db = float(value)
        if not math.isfinite(db):
            return curses.A_NORMAL
        db = max(0., min(20., db))
        if curses.COLORS >= 256 and curses.COLOR_PAIRS > 61:
            return curses.color_pair(40 + int(round(db)))
        return curses.color_pair(20 + palette_index(db))
    except (ValueError, TypeError, curses.error):
        return curses.A_NORMAL


def v19e4_chart_values(values, terminal_width):
    """Visible bars; SAME reduction used to draw graph and color the RF table.

    If terminal shrinks, columns are aggregated across the ENTIRE scan band,
    so R8 and other high-frequency channels remain mapped correctly.
    """
    if not values or terminal_width < 100:
        return []
    n = min(int(terminal_width) - 9, len(values))
    return downsample(values, n) if n < len(values) else list(values[:n])


def v19e4_bar_level_at_frequency(chart_values, frequency_mhz, start_mhz, stop_mhz):
    """Return the exact plotted dB level at the channel frequency, not smoothed RF.

    No interpolation or label guessing: same visible column -> same color.
    Missing graph bins stay uncolored in the table (never invent measurements).
    """
    if not chart_values or stop_mhz <= start_mhz:
        return None
    if not (start_mhz <= frequency_mhz < stop_mhz):
        return None
    idx = min(len(chart_values) - 1, max(0, int(
        (frequency_mhz - start_mhz) / (stop_mhz - start_mhz) * len(chart_values))))
    value = chart_values[idx]
    if value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def draw_spectrum(st, values, args, colors_ok):
    """Full-band RF bars, each independently colored by its own spectral dB.

    Like paint_spectrum() on the ILI9341; no grey blanket and no status masks.
    """
    h, w = st.getmaxyx()
    if h < 26 or w < 100 or not values:
        return
    x0, y0 = 7, 23
    width = min(w - 9, len(values))
    # Reserve bottom rows for the color scale when the terminal has enough room.
    # Short terminals retain the original, compact spectrum layout.
    chart_h = min(12, max(2, h - (30 if h >= 33 else 27)))
    if width < 20 or chart_h < 2:
        return
    try:
        st.addnstr(22, 1,
                   'ESPECTRO RF 5.8 GHz | gradiente por nivel (+0..+20 dB) | no dBm',
                   w - 2, curses.A_BOLD)
    except curses.error:
        pass
    gradient = ' ▁▂▃▄▅▆▇█'
    # Narrow terminals: preserve the entire band (including 5917 MHz/R8).
    chart_values = v19e4_chart_values(values, w)
    for i, val in enumerate(chart_values):
        if val is None:
            continue
        try:
            v = float(val)
            if not math.isfinite(v):
                continue
        except (TypeError, ValueError):
            continue
        strength = max(0., min(20., v))
        total_eighths = int(round(strength / 20. * chart_h * 8))
        color = v19e2_color_attr(strength, colors_ok)
        for row in range(chart_h):
            sub = total_eighths - (chart_h - row - 1) * 8
            if sub <= 0:
                continue
            try:
                st.addch(y0 + row, x0 + i, gradient[min(8, sub)], color)
            except curses.error:
                pass
    for row, label in ((0, '20'), (chart_h // 2, '10'), (chart_h - 1, '0')):
        try:
            st.addstr(y0 + row, 1, label.rjust(3))
        except curses.error:
            pass
    axis = y0 + chart_h
    try:
        st.addnstr(axis, x0, '┴' + '─' * (width - 2) + '┴', width)
        st.addnstr(axis + 1, x0, f'{args.start} MHz', 12)
        st.addnstr(axis + 1, x0 + max(0, (width - 12) // 2),
                   f'{(args.start + args.stop) // 2} MHz', 12)
        st.addnstr(axis + 1, x0 + max(0, width - 10), f'{args.stop} MHz', 10)
    except curses.error:
        pass



def draw_v19e3_color_legend(st, colors_ok):
    """Continuous bottom legend using exactly the same RF palette as the bars.

    21 nominal color steps are interpolated across the available width. It
    intentionally does NOT encode drone confidence or absolute dBm.
    """
    h, w = st.getmaxyx()
    if h < 33 or w < 100:
        return False
    x0 = 7
    n = min(120, w - x0 - 2)
    if n < 24:
        return False

    try:
        st.addnstr(h - 4, x0, 'ESCALA CROMATICA RF | misma paleta que la TFT',
                   n, curses.A_BOLD)
    except curses.error:
        pass

    for i in range(n):
        db = 20.0 * i / max(1, n - 1)
        try:
            st.addstr(h - 3, x0 + i, '█', v19e2_color_attr(db, colors_ok))
        except curses.error:
            pass

    marks = [(0, '0 dB - Bajo'),
             (n // 2 - len('10 dB - Medio') // 2, '10 dB - Medio'),
             (n - len('20 dB - Alto'), '20 dB - Alto')]
    for offset, label in marks:
        try:
            st.addnstr(h - 2, x0 + offset, label, len(label))
        except curses.error:
            pass
    try:
        st.addnstr(h - 1, 1,
                   'Nivel RF relativo al fondo: no son dBm, distancia ni probabilidad de dron.',
                   w - 2, curses.A_DIM)
    except curses.error:
        pass
    return True


def v19e5_light_background(xterm_code):
    """Contrast-aware text on the EXACT xterm color used for the spectrum bar.

    xterm RGB cube is indexed 16..231; choose black text if its luminance is
    >=0.179, otherwise white text. This maximizes WCAG contrast between the
    two choices. Color is never inferred from the RF score.
    """
    code = max(16, min(231, int(xterm_code))) - 16
    rgb = (code // 36, (code // 6) % 6, code % 6)
    srgb = [0 if v == 0 else 55 + 40 * v for v in rgb]
    linear = [((v / 255.0) / 12.92 if v / 255.0 <= 0.04045
               else ((v / 255.0 + 0.055) / 1.055) ** 2.4)
              for v in srgb]
    luminance = sum(a*b for a,b in zip(linear, (0.2126, 0.7152, 0.0722)))
    return luminance >= 0.179


def v19e5_ready_for_video(state, score, relative_db, threshold, min_score=80):
    """Visual cue only; it does NOT authorize/block video demodulation.

    Valid persistent status already incorporates freshness/calibration and the
    detector's temporal RF evidence. "Ready" means an RF signal worth
    inspecting, NOT a confirmed FPV transmission or a hostile aircraft.
    """
    try:
        return (state == 'PERSISTENT_RF'
                and float(score) >= float(min_score)
                and relative_db is not None
                and math.isfinite(float(relative_db))
                and float(relative_db) >= float(threshold))
    except (ValueError, TypeError):
        return False


def v19e5_highlight_attr(bar_db, colors_ok):
    """Same hue as the bar, now used as a high-contrast row background."""
    if not colors_ok or bar_db is None:
        return None
    try:
        db = float(bar_db)
        if not math.isfinite(db) or curses.COLOR_PAIRS <= 91:
            return None
        db = max(0.0, min(20.0, db))
        level = int(round(db)) if curses.COLORS >= 256 else palette_index(db)
        return curses.color_pair(70 + level) | curses.A_BOLD
    except (ValueError, TypeError, curses.error):
        return None


def draw(st, det, args, phase, threshold, segments, updates, rate,
         sound, message, wav_path, audio_target, audio_error, spectrum=None, colors_ok=False):
    height, width = st.getmaxyx()
    st.erase()

    def write(y, x, value, attr=0):
        if 0 <= y < height and 0 <= x < width:
            try:
                st.addnstr(y, x, str(value), max(0, width-x-1), attr)
            except curses.error:
                pass

    if width < 100 or height < 22:
        write(0, 0, 'Terminal >=100 columns x 22 rows required. Press q to quit.')
        st.refresh()
        return
    now = time.monotonic()
    # Use exactly the chart's visible frequency buckets for all row colors.
    # Row dB and spectrum dB have different baselines; colors correspond
    # to the plotted bar, while numeric RF measurements remain untouched.
    v19e4_bars = v19e4_chart_values(spectrum or [], width)
    write(0, 1, 'HACKRF 5.8 GHz | FPV DETECTOR V17 | RF EVIDENCE, NOT DRONE CONFIRMATION', curses.A_BOLD)
    write(1, 1, f'Segments {segments} | updates {updates} | {rate:.1f} seg/s | detection +{threshold:.1f} / release +{args.release_threshold:.1f} dB')
    write(2, 1, f'Operator: {phase} | Audio: {"ON" if sound else "OFF"} ({"OK" if wav_path.is_file() else "WAV MISSING"}) | 0 OFF 1 ON m mark c calibrate +/- threshold a audio r reset q quit')
    column_width = (width - 2) // 5
    for j, band in enumerate(BANDS):
        x = 1 + j * column_width
        write(4, x, f'{band} CH  MHz   dB SC age', curses.A_BOLD | curses.A_UNDERLINE)
        for i, frequency in enumerate(BANDS[band]):
            key = (band, i)
            s = det.states[key]
            state = det.status(key, now)
            delta = '  N/A' if s.smoothed is None else f'{s.smoothed:+5.1f}'
            age = '--' if s.last_seen is None else f'{now-s.last_seen:3.1f}'
            mark = {'PERSISTENT_RF': '!', 'RF_CANDIDATE': '?', 'STALE': '~',
                    'CALIBRATING': 'C', 'UNCALIBRATED': 'C'}.get(state, ' ')
            # V19-E.4: table text uses the color of this channel's VISIBLE bar.
            bar_db = v19e4_bar_level_at_frequency(
                v19e4_bars, frequency, args.start, args.stop)
            attr = v19e2_color_attr(bar_db, colors_ok)
            if state == 'PERSISTENT_RF':
                attr |= curses.A_BOLD
            elif state in ('STALE', 'UNSEEN', 'CALIBRATING', 'UNCALIBRATED'):
                attr |= curses.A_DIM
            row_text = f'{mark}{i+1:2} {frequency:4} {delta:>5} {s.score:2} {age:>4}'
            # V19-E.5 highlights only STRONG + PERSISTENT entries; all other
            # rows keep their original spectrum-matched foreground gradient.
            ready = v19e5_ready_for_video(
                state, s.score, s.smoothed, threshold,
                getattr(args, 'video_highlight_score', 80))
            highlighted = v19e5_highlight_attr(bar_db, colors_ok) if ready else None
            if highlighted is not None:
                # Fill ONLY this band's cell, never the neighbouring band.
                # Space padding makes the RF hue visible behind the entire row.
                cell_width = max(0, min(column_width - 1, width - x - 1))
                if cell_width:
                    try:
                        st.addnstr(6+i, x, row_text.ljust(cell_width), cell_width, highlighted)
                    except curses.error:
                        pass
            else:
                write(6+i, x, row_text, attr)
    focus_key = next(((b, i) for b, i, f in CHANNELS if f == args.focus), None)
    if focus_key:
        state = det.states[focus_key]
        status = det.status(focus_key, now)
        if state.baseline is None or state.smoothed is None:
            focus = f'{args.focus} MHz | {status} | awaiting fresh samples'
        else:
            focus = (f'{args.focus} MHz | {status} | dB {state.smoothed:+.1f} | '
                     f'occupancy {state.occupancy*100:.0f}% | observed {state.observed_s:.1f}s | '
                     f'score {state.score}/100 | gate +{(state.noise_gate or threshold):.1f} dB')
    else:
        focus = f'{args.focus} MHz | no matching FPV channel'
    write(15, 1, 'FOCUS: ' + focus, curses.A_BOLD)
    groups = rf_clusters(det, now, args.cluster_mhz)
    write(16, 1, 'PERSISTENT REGIONS: ' + (', '.join(cluster_text(g,det) for g in groups[:6]) if groups else 'none'))
    remaining = det.calibration_remaining(now)
    write(17, 1, (f'CALIBRATING TX OFF: {remaining:.1f}s | ' if remaining > 0 else '') + message)
    write(18, 1, f'Window {args.occupancy_window:.1f}s | persist {args.min_presence_seconds:.1f}s / {args.min_occupancy:.0%} | release {args.release_seconds:.1f}s | stale {args.stale_seconds:.1f}s')
    write(19, 1, f'RF states: BACKGROUND  ? candidate  ! persistent  ~ stale  C calibrating | logs: {args.logdir}')
    if audio_target is not None:
        interval, pitch = beep_parameters(audio_target[1], args)
        if args.audio_style == 'wav':
            wave_seconds = getattr(args, 'warning_wav_seconds', 0.233)
            interval = v19e6_wave_period(audio_target[1], args, wave_seconds)
            sound_label = 'warning.wav / tono fijo'
        else:
            sound_label = f'{pitch} Hz'
        cue = (f'BEEPING: {audio_target[0]} MHz | +{audio_target[1]:.1f} dB '
               f'| {sound_label} | cada {interval:.2f}s | device {args.audio_device}')
    else:
        cue = f'BEEPING: silent | ALSA device {args.audio_device}'
    if audio_error:
        cue += f' | AUDIO ERROR: {audio_error}'
    write(20, 1, cue)
    draw_spectrum(st, spectrum or [], args, colors_ok)
    if not draw_v19e3_color_legend(st, colors_ok):
        write(height-1, 1, '0 dB: bajo | 10 dB: medio | 20 dB: alto (RF relativa; no dBm)')
    st.refresh()


def make_rf_regions(channels, separation_mhz, max_age):
    """Coarse grouping of fresh persistent channel-centre candidates.

    A group is RF evidence, never a verified video feed or drone count.
    Close channels are grouped to avoid counting the same wideband VTX twice.
    """
    entries = sorted(
        (ch for ch in channels
         if ch['status'] == 'PERSISTENT_RF'
         and ch['age_s'] is not None and ch['age_s'] <= max_age
         and ch['calibrated']),
        key=lambda ch: ch['frequency_mhz'])
    grouped = []
    for ch in entries:
        if not grouped or ch['frequency_mhz'] - grouped[-1][-1]['frequency_mhz'] > separation_mhz:
            grouped.append([])
        grouped[-1].append(ch)
    result = []
    for group in grouped:
        best = max(group, key=lambda ch: (ch['score'], ch['relative_db'] or -999))
        members = [f"{x['band']}{x['channel']}" for x in group]
        result.append({
            'id': f"{best['band']}{best['channel']}",
            'frequency_mhz': best['frequency_mhz'],
            'band': best['band'], 'channel': best['channel'],
            'score': best['score'], 'relative_db': best['relative_db'],
            'occupancy': best['occupancy'], 'age_s': best['age_s'],
            'low_mhz': group[0]['frequency_mhz'],
            'high_mhz': group[-1]['frequency_mhz'],
            'members': members, 'member_count': len(members),
            'status': 'PERSISTENT_RF',
        })
    result.sort(key=lambda x: (x['score'], x['relative_db'] or -999), reverse=True)
    return result


def make_tft_snapshot(det, args, phase, threshold, sound, segments, updates, now, spectrum=None):
    """Read-only snapshot. No changes to RF detection or audio behavior."""
    channels = []
    for band, i, freq in CHANNELS:
        state = det.states[(band, i)]
        status = det.status((band, i), now)
        age = None if state.last_seen is None else round(now - state.last_seen, 2)
        channels.append({
            'band': band, 'channel': i + 1, 'frequency_mhz': freq,
            'status': status,
            'relative_db': None if state.smoothed is None else round(state.smoothed, 2),
            'score': state.score,
            'occupancy': round(state.occupancy, 3),
            'age_s': age,
            'calibrated': state.calibrated,
        })
    # Prioritize fresh persistent signals, then fresh candidates; never use
    # old or uncalibrated RF samples as evidence of a current signal.
    live = [x for x in channels if x['age_s'] is not None and
            x['age_s'] <= args.audio_max_age and
            x['status'] in ('PERSISTENT_RF', 'RF_CANDIDATE')]
    priority = {'PERSISTENT_RF': 2, 'RF_CANDIDATE': 1}
    live.sort(key=lambda x: (priority[x['status']], x['score'],
                             x['relative_db'] if x['relative_db'] is not None else -999),
              reverse=True)
    if live:
        primary = live[0]
    else:
        primary = next((x for x in channels if x['frequency_mhz'] == args.focus), None)
        if primary is not None and (primary['age_s'] is None or
                                    primary['age_s'] > args.stale_seconds):
            primary = dict(primary, status='STALE', score=0, relative_db=None)
    remaining = det.calibration_remaining(now)
    # Display confirmation requires a fresh channel; missing RF samples
    # must not remain shown as a current alarm.
    persistent = sum(x['status'] == 'PERSISTENT_RF' for x in live)
    candidates = sum(x['status'] == 'RF_CANDIDATE' for x in live)
    overall = ('CALIBRATING' if remaining > 0 else
               'PERSISTENT_RF' if persistent else
               'RF_CANDIDATE' if candidates else 'BACKGROUND')
    regions = make_rf_regions(channels, args.cluster_mhz, args.audio_max_age)
    return {
        'schema': 1, 'running': True,
        'receiver_mode': 'SCAN', 'scanning': True, 'video': None,
        'updated_epoch': time.time(), 'updated_utc': utc(),
        'overall_state': overall,
        'calibration_remaining_s': round(remaining, 1),
        'phase': phase, 'audio_enabled': sound,
        'threshold_db': round(threshold, 1),
        'segments': segments, 'channel_updates': updates,
        'persistent_channels': persistent,
        'candidate_channels': candidates,
        'regions': regions, 'region_count': len(regions),
        'multiple_regions': len(regions) >= 2,
        'primary': primary, 'top': live[:6], 'channels': channels,
        'notice': 'RF energy evidence only; not drone identification or range',
        'spectrum': {'start_mhz': args.start, 'stop_mhz': args.stop,
                     'columns_db': downsample(spectrum or [], 96), 'max_relative_db': 20}
    }


def save_tft_state(path, payload):
    """Atomic replace avoids a partial JSON file being read by the TFT."""
    if path is None:
        return
    temp = path.with_name(path.name + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open('w', encoding='utf-8') as f:
            json.dump(payload, f, separators=(',', ':'), allow_nan=False)
        os.replace(temp, path)
    except OSError:
        # Screen is optional: a filesystem problem must not stop the RF monitor.
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass



def poll_touch_command(path, started_epoch, last_token):
    """Validate one recent local UI command; no shell command is accepted."""
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        command = payload.get('command')
        token = payload.get('id')
        sent = float(payload.get('epoch', 0))
        allowed = ('TOGGLE_AUDIO', 'START_VIDEO', 'STOP_VIDEO')
        if (payload.get('schema') != 1 or not isinstance(token, str) or
                len(token) > 90 or token == last_token or command not in allowed or
                not (started_epoch - 0.2 <= sent <= time.time() + 1) or
                time.time() - sent > 4.0):
            return last_token, None
        if command == 'START_VIDEO':
            f = payload.get('frequency_mhz')
            if type(f) is not int or f not in {freq for _, _, freq in CHANNELS}:
                return token, None
        return token, payload
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return last_token, None


def reset_signal_states_for_resume(detector):
    """Keep calibrated baselines but invalidate everything sampled before VIDEO."""
    for s in detector.states.values():
        s.smooth_history.clear()
        s.observations.clear()
        s.active = s.candidate = s.expired = False
        s.candidate_low_since = s.last_seen = s.gap = None
        s.last_power = s.last_peak = s.delta = s.smoothed = None
        s.last_bins = s.score = s.observation_count = 0
        s.occupancy = s.observed_s = s.span_s = 0.0
        s.detected_at = s.low_since = None
        s.low_covered_s = 0.0
        s.low_samples = 0


def make_video_snapshot(frequency_mhz, band, channel, elapsed, sound, frame_path=None, frame_age=None, backend="iq"):

    return {
        'schema': 1, 'running': True, 'updated_epoch': time.time(),
        'updated_utc': utc(), 'overall_state': 'VIDEO_MODE',
        'receiver_mode': 'VIDEO', 'scanning': False, 'audio_enabled': sound,
        'video': {'frequency_mhz': frequency_mhz, 'band': band,
                  'channel': channel, 'elapsed_s': round(elapsed, 1),
                  'backend': backend, 'frame_path': str(frame_path or ''),
                  'frame_age_s': frame_age, 'frame_ready': frame_age is not None and frame_age < 3.0,
                  'iq_capture': backend == 'iq',
                  'message': ('Decodificador fpv-sdr' if backend == 'fpv-sdr' else 'Decodificador fpvdec color' if backend == 'fpvdec' else 'Solo I/Q; no video')},
        'regions': [], 'region_count': 0, 'multiple_regions': False,
        'primary': None, 'channels': [], 'top': [],
        'notice': 'ESCANEO PAUSADO POR EL OPERADOR',
    }


def draw_video(st, video, elapsed, message):
    height, width = st.getmaxyx()
    st.erase()
    def put(row, value):
        if 0 <= row < height and width > 2:
            try:
                st.addnstr(row, 1, value, width - 2)
            except curses.error:
                pass
    put(1, 'FPV DETECTOR V17 | VIDEO FPV | ESCANEO PAUSADO')
    put(3, f"SELECCION: {video['band']}{video['channel']}  {video['frequency_mhz']} MHz")
    put(5, f'Recepcion de canal activa: {elapsed:.1f} s')
    put(7, 'fpvdec COLOR -> /dev/shm -> TFT (ultimo fotograma).')
    put(9, message)
    put(12, 'Pulse v para VOLVER a escaneo, q para salir.')
    st.refresh()


def stop_child(proc, timeout=2.5):
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    try:
        if proc.stdout:
            proc.stdout.close()
    except OSError:
        pass


def run(st, args):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    st.nodelay(True)
    colors_ok = init_spectrum_colors()
    wav = resolve_wav(args.wav)
    process = None
    reader_thread = None
    video_process = None
    video_log = None
    receiver_mode = 'SCAN'
    video_restart_due = None
    last_video_cmd = None
    video_info = None
    video_since = 0.0
    q = queue.Queue(maxsize=1000)
    command = ['DEMO'] if args.demo else [
        'hackrf_sweep', '-f', f'{args.start}:{args.stop}',
        '-w', str(args.binwidth), '-l', str(args.lna), '-g', str(args.vga)]
    if not args.demo and (not shutil.which('hackrf_sweep') or (args.video_backend == 'iq' and not shutil.which('hackrf_transfer'))):
        raise RuntimeError('hackrf_sweep/hackrf_transfer missing; sudo apt install hackrf')

    def start_scanner():
        nonlocal process, q, reader_thread
        q = queue.Queue(maxsize=1000)  # discard old reader's EOF and buffered samples
        if not args.demo:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, bufsize=1)
            reader_thread = threading.Thread(target=reader, args=(process, q), daemon=True)
            reader_thread.start()

    def stop_scanner():
        nonlocal process, reader_thread
        stop_child(process)
        if reader_thread:
            reader_thread.join(timeout=1.0)
        process, reader_thread = None, None

    start_scanner()
    logdir = Path(args.logdir).expanduser()
    logdir.mkdir(parents=True, exist_ok=True)
    base = 'fpv_v53_' + datetime.now().strftime('%Y%m%d_%H%M%S')
    channels_file = logdir / (base + '_channels.csv')
    events_file = logdir / (base + '_events.csv')
    detector = Detector(args)
    detector.begin_calibration(time.monotonic())
    buffer = ScanBuffer(args.start, args.stop, args.window, args.coverage_age)
    tracker = RegionTracker(args.cluster_mhz, args.rearm_seconds)
    spectrum = [None] * 120
    video_frame = Path(args.video_frame_file).expanduser().resolve()
    phase = 'UNMARKED'
    sound = not args.no_audio
    threshold = args.threshold
    audio = PulseAudio(args, wav)
    previous_audio_target = None
    segments = updates = 0
    times = deque(maxlen=150)
    last_demo = 0.0
    message = 'Keep TX OFF while calibrating.'
    last_mode_error = ''

    tft_state_path = (None if args.no_tft_state else Path(args.state_file).expanduser().resolve())
    last_tft_write = -1e9
    touch_path = Path(args.touch_command_file).expanduser().resolve()
    touch_started_epoch = time.time()
    last_touch_token = None
    last_touch_poll = -1e9
    with channels_file.open('w', newline='', buffering=1) as channels_fp, \
         events_file.open('w', newline='', buffering=1) as events_fp:
        cw, ew = csv.writer(channels_fp), csv.writer(events_fp)
        cw.writerow(['utc','segment','phase','band','channel','center_mhz',
                     'baseline_db','mean_db','delta_db','smoothed_db','peak_db',
                     'bin_count','active','rf_score','rf_state','measurement_gap_s',
                     'calibrated','noise_gate_db','occupancy','observed_s',
                     'span_s','distinct_observations'])
        ew.writerow(['utc','segment','phase','event','channel','frequency_mhz',
                     'smoothed_db','note','rf_score','occupancy','observed_s'])

        def event(kind, channel='', freq='', value='', note='', score='', occupancy='', observed=''):
            ew.writerow([utc(),segments,phase,kind,channel,freq,value,note,
                         score,occupancy,observed])

        def enter_video(region):
            nonlocal receiver_mode, video_info, video_since, video_process, video_log
            nonlocal message, buffer, tracker, previous_audio_target, last_mode_error, last_video_cmd, video_restart_due
            if receiver_mode != 'SCAN':
                return
            freq = int(region['frequency_mhz'])
            audio.set_target(None)
            previous_audio_target = None
            stop_scanner()  # HackRF must have only ONE owning process
            time.sleep(0.15)
            try:
                if args.demo:
                    video_process = subprocess.Popen(['sleep', '3600'],
                                                      stdout=subprocess.DEVNULL,
                                                      stderr=subprocess.DEVNULL)
                else:
                    video_log = (logdir / 'fpv_v17_video.log').open('ab', buffering=0)
                    if args.video_backend == 'fpv-sdr':
                        viewer = Path(args.fpv_sdr_dir).expanduser() / 'fpv_viewer.py'
                        if not viewer.is_file():
                            raise RuntimeError('Falta fpv-sdr/fpv_viewer.py; instala el repo o usa --video-backend iq')
                        try:
                            video_frame.unlink(missing_ok=True)  # never show an old frame
                        except OSError:
                            pass
                        video_cmd = [args.video_python, str(viewer), '--sdr', 'hackrf',
                                     '--freq', str(freq * 1000000),
                                     '--samp-rate', str(args.video_sample_rate),
                                     '--lna', str(args.lna), '--vga', str(args.vga),
                                     '--standard', args.video_standard, '--no-window',
                                     '--no-keys', '--frame-out', str(video_frame)]
                        video_process = subprocess.Popen(video_cmd, cwd=str(viewer.parent),
                                                         stdin=subprocess.DEVNULL, stdout=video_log, stderr=video_log)
                    elif args.video_backend == 'fpvdec':
                        exe = Path(args.fpvdec_exe).expanduser().resolve()
                        if not exe.is_file():
                            raise RuntimeError(f'Falta fpvdec V19-E en {exe}; ejecuta preparar_fpvdec_v19e.py')
                        # C++ outputs latest genuine NTSC RGB frame directly to RAM.
                        # No PNG, PPM, Pillow, or Python bridge in the realtime path.
                        # Auto gain + AFC remain in fpvdec's new headless loop.
                        rgb_out = Path(args.fpvdec_color_out).expanduser()
                        rgb_out.parent.mkdir(parents=True, exist_ok=True)
                        rgb_out.unlink(missing_ok=True)
                        video_cmd = [str(exe),
                                     '--channel', f"{region['band']}{region['channel']}",
                                     '--rate', str(args.video_sample_rate),
                                     '--mode', 'color', '--no-amp',
                                     '--gain', 'auto',
                                     '--dump-frames', f'shm:{rgb_out}',
                                     '--frames', '1000000']
                        video_process = subprocess.Popen(video_cmd, cwd=str(HERE),
                                                         stdin=subprocess.DEVNULL, stdout=video_log, stderr=video_log)
                    else:
                        video_cmd = ['hackrf_transfer', '-r', '/dev/null',
                                     '-f', str(freq * 1000000), '-s', str(args.video_sample_rate),
                                     '-a', '0', '-l', str(args.lna), '-g', str(args.vga)]
                        video_process = subprocess.Popen(video_cmd, stdout=video_log, stderr=video_log)
                last_video_cmd = video_cmd if not args.demo else None
                video_restart_due = None
                time.sleep(0.20)
                if video_process.poll() is not None:
                    raise RuntimeError(f'IQ receiver exited: {video_process.returncode}')
                receiver_mode = 'VIDEO'
                video_info = {'frequency_mhz': freq, 'band': region['band'],
                              'channel': region['channel']}
                video_since = time.monotonic()
                event('VIDEO_ENTER', f"{region['band']}{region['channel']}", freq,
                      note=f'Scan paused; backend={args.video_backend}; one HackRF')
                message = f'Recepcion {args.video_backend} {freq} MHz; escaneo PAUSADO'
            except Exception as exc:
                last_mode_error = str(exc)
                event('VIDEO_ERROR', freq=freq, note=last_mode_error)
                stop_child(video_process)
                video_process = None
                if video_log:
                    video_log.close()
                    video_log = None
                reset_signal_states_for_resume(detector)
                buffer = ScanBuffer(args.start, args.stop, args.window, args.coverage_age)
                tracker = RegionTracker(args.cluster_mhz, args.rearm_seconds)
                try:
                    start_scanner()
                except OSError as error:
                    last_mode_error = f'Cannot restart scanner: {error}'
                message = 'Video capture unavailable: ' + last_mode_error

        def leave_video():
            nonlocal receiver_mode, video_info, video_process, video_log, buffer, tracker
            nonlocal message, last_mode_error, last_video_cmd, video_restart_due
            if receiver_mode != 'VIDEO':
                return
            freq = video_info['frequency_mhz']
            stop_child(video_process)
            video_process = None
            last_video_cmd = None
            video_restart_due = None
            if video_log:
                video_log.close()
                video_log = None
            time.sleep(0.15)
            # Preserve RF baseline but never reuse old candidates/ages.
            reset_signal_states_for_resume(detector)
            buffer = ScanBuffer(args.start, args.stop, args.window, args.coverage_age)
            tracker = RegionTracker(args.cluster_mhz, args.rearm_seconds)
            receiver_mode = 'SCAN'
            video_info = None
            event('VIDEO_EXIT', freq=freq, note='IQ released, returning to sweep')
            try:
                start_scanner()
                message = 'Escaneo reanudado; calibracion RF conservada'
            except OSError as exc:
                last_mode_error = str(exc)
                message = 'ERROR restarting scan: ' + last_mode_error

        def validate_region(freq):
            """Frequency authorized by channel table; RF level never gates manual VIDEO."""
            for b, i, fc in CHANNELS:
                if int(fc) == int(freq):
                    return {'frequency_mhz': int(fc), 'band': b, 'channel': i+1}
            return None

        event('START', note=' '.join(command))
        event('CALIBRATION_START', note='Keep transmitter OFF until complete')
        try:
            running = True
            while running:
                now = time.monotonic()
                key = st.getch()
                while key != -1:
                    if key in (ord('q'), ord('Q')):
                        running = False
                        break
                    if key in (ord('v'), ord('V')) and receiver_mode == 'VIDEO':
                        leave_video()
                    if key in (ord('0'), ord('1')):
                        phase = 'TX_OFF' if key == ord('0') else 'TX_ON'
                        event('MARK', note='operator TX label (no RF control)')
                    elif receiver_mode == 'SCAN' and key in (ord('c'), ord('C'), ord('r'), ord('R')):
                        detector.begin_calibration(time.monotonic())
                        audio.set_target(None)
                        tracker = RegionTracker(args.cluster_mhz, args.rearm_seconds)
                        event('CALIBRATION_START', note='Operator recalibration: keep TX OFF')
                        message = 'Recalibrating (TX OFF required)'
                    elif key in (ord('m'), ord('M')):
                        event('MARK', note='operator manual marker')
                    elif key in (ord('+'), ord('=')):
                        threshold += 1.0
                        event('THRESHOLD', note=f'+{threshold:.1f} dB')
                    elif key == ord('-'):
                        threshold = max(args.release_threshold+0.5, threshold-1.0)
                        event('THRESHOLD', note=f'+{threshold:.1f} dB')
                    elif key in (ord('a'), ord('A')):
                        sound = not sound
                        event('AUDIO', note='ON' if sound else 'OFF')
                    key = st.getch()
                if not running:
                    break
                # Nonblocking touchscreen IPC: only toggles the same sound flag as 'a'.
                # Invalid/stale commands are ignored; no RF logic is changed.
                if now - last_touch_poll >= 0.10:
                    last_touch_poll = now
                    last_touch_token, touch_action = poll_touch_command(
                        touch_path, touch_started_epoch, last_touch_token)
                    if touch_action:
                        kind = touch_action['command']
                        if kind == 'TOGGLE_AUDIO':
                            sound = not sound
                            event('AUDIO_TOUCH', note='ON' if sound else 'OFF')
                        elif kind == 'START_VIDEO' and receiver_mode == 'SCAN':
                            region = validate_region(touch_action['frequency_mhz'])
                            if region:
                                enter_video(region)
                            else:
                                event('VIDEO_REJECTED', freq=touch_action['frequency_mhz'],
                                      note='Selected region not fresh/persistent')
                                message = 'Seleccion obsoleta: reintenta desde SENIALES'
                        elif kind == 'STOP_VIDEO' and receiver_mode == 'VIDEO':
                            leave_video()
                if receiver_mode == 'VIDEO':
                    audio.set_target(None)
                    if video_process and video_process.poll() is not None:
                        event('VIDEO_ERROR', note='Decoder exited; holding VIDEO until operator says VOLVER')
                        stop_child(video_process)
                        video_process = None
                        video_restart_due = now + 3.0
                        message = 'VIDEO PAUSADO: reintentando backend, sweep sigue OFF'
                    if (video_process is None and last_video_cmd and video_restart_due is not None
                            and now >= video_restart_due):
                        try:
                            video_process = subprocess.Popen(last_video_cmd, cwd=str(HERE),
                                     stdin=subprocess.DEVNULL, stdout=video_log, stderr=video_log)
                            video_restart_due = None
                            message = 'VIDEO reintentando; sweep sigue pausado'
                            event('VIDEO_RESTART', note='Decoder launched; RF scan still paused')
                        except OSError as error:
                            video_restart_due = now + 3.0
                            message = f'VIDEO backend error: {error}'
                    if receiver_mode != 'VIDEO':
                        continue
                    if tft_state_path is not None and now - last_tft_write >= args.state_interval:
                        age = None
                        if args.video_backend == 'fpv-sdr' and video_frame.is_file():
                            age = max(0.0, time.time() - video_frame.stat().st_mtime)
                        save_tft_state(tft_state_path, make_video_snapshot(
                            video_info['frequency_mhz'], video_info['band'],
                            video_info['channel'], now - video_since, sound,
                            frame_path=video_frame, frame_age=age, backend=args.video_backend))
                        last_tft_write = now
                    draw_video(st, video_info, now - video_since, message)
                    time.sleep(0.04)
                    continue
                batch = []
                if args.demo:
                    if now-last_demo >= 0.012:
                        batch.append(demo_points(args, phase == 'TX_ON'))
                        last_demo = now
                else:
                    for _ in range(120):
                        try:
                            item = q.get_nowait()
                        except queue.Empty:
                            break
                        if item is None:
                            running = False
                            message = 'HackRF sweep ended'
                            break
                        batch.append(item)
                for points in batch:
                    now = time.monotonic()
                    segments += 1
                    times.append(now)
                    measurements = buffer.feed(points, now)
                    if not measurements:
                        continue
                    updates += len(measurements)
                    changes = detector.update(measurements, now, threshold, phase)
                    for key, kind, level in changes:
                        band, i = key
                        s = detector.states[key]
                        event(kind, f'{band}{i+1}', BANDS[band][i],
                              f'{level:.3f}', 'RF persistent evidence; not drone confirmation',
                              s.score, f'{s.occupancy:.3f}', f'{s.observed_s:.3f}')
                    stamp = utc()
                    for (band, i), (power, peak, n_bins) in measurements.items():
                        s = detector.states[(band, i)]
                        fmt = lambda x: '' if x is None else f'{x:.3f}'
                        cw.writerow([stamp,segments,phase,band,i+1,BANDS[band][i],
                                    fmt(s.baseline),fmt(power),fmt(s.delta),fmt(s.smoothed),
                                    fmt(peak),n_bins,int(s.active),s.score,
                                    detector.status((band,i),now),fmt(s.gap),
                                    int(s.calibrated),fmt(s.noise_gate),
                                    fmt(s.occupancy),fmt(s.observed_s),fmt(s.span_s),
                                    s.observation_count])
                now = time.monotonic()
                for key, kind, value in detector.expire(now):
                    band, i = key
                    s = detector.states[key]
                    event(kind, f'{band}{i+1}', BANDS[band][i],
                          '' if value is None else f'{value:.3f}',
                          'No fresh measurements: signal unverified', 0)
                for kind, center, keys, newly_confirmed in tracker.update(
                        rf_clusters(detector, now, args.cluster_mhz), now):
                    label = ','.join(f'{band}{i+1}' for band,i in sorted(keys))
                    event(kind, label, f'{center:.1f}', note='persistent RF region')
                # One audio target at a time, selected from RECENT confirmed RF.
                # Using current channel measurement age avoids sounding indefinitely
                # based on old strength readings during a scan coverage gap.
                audio_target = (best_audio_target(detector, now, args.audio_max_age)
                                if sound else None)
                audio.set_target(audio_target)
                if audio_target is not None and previous_audio_target is None:
                    interval, hz = beep_parameters(audio_target[1], args)
                    if args.audio_style == 'wav':
                        interval = v19e6_wave_period(audio_target[1], args,
                                                    args.warning_wav_seconds)
                        cue_desc = 'warning.wav tono fijo'
                    else:
                        cue_desc = f'{hz} Hz'
                    event('BEEP_START', freq=audio_target[0],
                          value=f'{audio_target[1]:.3f}',
                          note=f'{cue_desc}; {interval:.2f}s; RF relativa, NO distancia')
                elif audio_target is None and previous_audio_target is not None:
                    event('BEEP_STOP', freq=previous_audio_target[0],
                          note='RF cleared, stale, or audio muted')
                previous_audio_target = audio_target
                rate = ((len(times)-1)/(times[-1]-times[0])
                        if len(times)>1 and times[-1]>times[0] else 0.0)
                n_active = sum(s.active for s in detector.states.values())
                if not message.startswith('Audio problem'):
                    message = f'{n_active} persistent channel(s); updates {updates}'
                # Calculate spectrum from SAME current sweep bins (no second HackRF owner).
                spectrum = scan_columns(buffer, now, width=120, max_age=args.coverage_age)
                if tft_state_path is not None and now - last_tft_write >= args.state_interval:
                    save_tft_state(tft_state_path, make_tft_snapshot(
                        detector, args, phase, threshold, sound, segments, updates, now,
                        spectrum=spectrum))
                    last_tft_write = now
                draw(st, detector, args, phase, threshold, segments, updates,
                     rate, sound, message, wav, audio_target, audio.error,
                     spectrum=spectrum, colors_ok=colors_ok)
                time.sleep(0.04)
            event('STOP', note='user quit or process ended')
        finally:
            if tft_state_path is not None:
                save_tft_state(tft_state_path, {
                    'schema': 1, 'running': False,
                    'updated_epoch': time.time(), 'updated_utc': utc(),
                    'overall_state': 'STOPPED', 'primary': None,
                    'top': [], 'channels': [], 'audio_enabled': False,
                    'notice': 'RF monitor stopped'
                })
            audio.close()
            stop_child(video_process)
            if video_log:
                video_log.close()
            stop_scanner()
    print(f'Channel log: {channels_file}\nEvent log:   {events_file}')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    for name, default in [('start',5640),('stop',5950),('focus',5917),
                          ('binwidth',1000000),('lna',16),('vga',20),
                          ('smoothing',3)]:
        p.add_argument('--'+name,type=int,default=default)
    for name, default in [('window',8.0),('threshold',6.0),
                          ('release-threshold',3.0),('stale-seconds',6.0),
                          ('coverage-age',1.5),('cooldown',15.0),
                          ('cluster-mhz',12.0),('calibration-seconds',12.0),
                          ('noise-margin',3.0),('occupancy-window',2.5),
                          ('min-presence-seconds',1.5),('min-occupancy',0.80),
                          ('observation-max-gap',0.75),('min-coverage',0.75),
                          ('min-observation-spacing',0.25),
                          ('release-seconds',2.5),('min-hold-seconds',2.0),
                          ('candidate-reset-seconds',0.75),
                          ('rearm-seconds',5.0)]:
        p.add_argument('--'+name,type=float,default=default)
    p.add_argument('--calibration-min-samples',type=int,default=10)
    p.add_argument('--min-observations',type=int,default=3)
    p.add_argument('--wav',default='warning.wav',help='relative to script folder')
    p.add_argument('--logdir',default='logs')
    p.add_argument('--video-highlight-score', type=int, default=80,
                   help='Minimum RF evidence score for TUI row highlight ONLY (0..100)')
    p.add_argument('--video-sample-rate', type=int, default=10000000,
                   help='I/Q rate for fpv-sdr or transfer fallback')
    p.add_argument('--video-backend', choices=('fpv-sdr','fpvdec','iq'), default='fpv-sdr',
                   help='fpv-sdr renders real frames; iq just validates switching')
    p.add_argument('--fpv-sdr-dir', default='~/fpv-sdr',
                   help='Upstream lukeswitz/fpv-sdr clone (install separately)')
    p.add_argument('--fpvdec-exe', default='~/detector_drone/v19e_fpvdec_src/build-v19e/fpvdec',
                   help='Path to fpvdec executable from 5G8atv-rf-hackrf-decoder')
    p.add_argument('--fpvdec-color-out', default=f'/dev/shm/fpv_color_{os.getuid()}.raw',
                   help='RGB frames published directly by fpvdec V19-E')
    p.add_argument('--fpvdec-bridge', default=str(HERE / 'fpvdec_rgb_bridge_v19d.py'),
                   help='Helper that converts fpvdec frame dumps into one live PNG for the TFT')
    p.add_argument('--fpvdec-workdir', default='/dev/shm/fpvdec_live',
                   help='RAM directory used by the fpvdec helper to store temporary PPM frames')
    p.add_argument('--video-python', default='python3',
                   help='Python with GNU Radio, Soapy HackRF and gr-ntsc-rc')
    p.add_argument('--video-standard', choices=('ntsc','pal'), default='ntsc')
    p.add_argument('--video-frame-file', default=str(HERE / 'fpv_video_frame.png'))
    p.add_argument('--no-audio',action='store_true')
    p.add_argument('--audio-device', default='plughw:CARD=Audio,DEV=0',
                   help='ALSA output. USB card shown as Audio in aplay -l; fallback: plughw:2,0')
    p.add_argument('--audio-max-age', type=float, default=2.5,
                   help='Only beep if selected channel has a fresh RF measurement')
    p.add_argument('--audio-style', choices=('wav','tone'), default='wav',
                   help='wav: repetir warning.wav a cadencia RF; tone: beeps sinteticos originales')
    p.add_argument('--beep-weak-db', type=float, default=6.0,
                   help='Relative dB at slow/low-pitched end, NOT distance')
    p.add_argument('--beep-strong-db', type=float, default=20.0,
                   help='Relative dB at fast/high-pitched end, NOT distance')
    p.add_argument('--beep-slow', type=float, default=1.20,
                   help='Seconds between beep starts for weaker RF')
    p.add_argument('--beep-fast', type=float, default=0.22,
                   help='Seconds between beep starts for stronger RF')
    p.add_argument('--tone-low', type=int, default=550,
                   help='Tone in Hz at weaker RF')
    p.add_argument('--tone-high', type=int, default=1450,
                   help='Tone in Hz at stronger RF')
    p.add_argument('--beep-duration', type=float, default=0.10,
                   help='Length of each synthesized beep in seconds')
    p.add_argument('--no-intro', action='store_true',
                   help='Disable initial warning.wav; keep repeating variable beeps')
    p.add_argument('--audio-test', action='store_true',
                   help='Test weak-to-strong beeps without HackRF or curses')
    p.add_argument('--demo',action='store_true')
    p.add_argument('--touch-command-file', default=str(DEFAULT_TOUCH_COMMAND_FILE),
                   help='File for local XPT2046 touch MUTE/UNMUTE events')
    p.add_argument('--state-file', default=str(DEFAULT_STATE_FILE),
                   help='Atomic JSON output for separate SPI display')
    p.add_argument('--state-interval', type=float, default=0.25,
                   help='Seconds between TFT state snapshots')
    p.add_argument('--no-tft-state', action='store_true',
                   help='Disable writing a TFT state file')
    args = p.parse_args()
    # Wave duration only affects the minimum repeat period in fixed-WAV mode.
    args.warning_wav_seconds = 0.233
    try:
        with wave.open(str(resolve_wav(args.wav)), 'rb') as f:
            args.warning_wav_seconds = f.getnframes() / f.getframerate()
    except (OSError, EOFError, ValueError, wave.Error, ZeroDivisionError):
        pass
    if args.video_sample_rate < 8000000 or args.video_sample_rate > 20000000:
        p.error('video-sample-rate must be between 8 and 20 Msps')
    if args.state_interval <= 0:
        p.error('state-interval must be positive')
    if (args.audio_max_age <= 0 or args.beep_strong_db <= args.beep_weak_db or
            args.beep_fast <= args.beep_duration or args.beep_slow < args.beep_fast or
            args.beep_duration <= 0 or args.tone_low <= 0 or
            args.tone_high < args.tone_low):
        p.error('Invalid audio mapping settings')
    if (args.start >= args.stop or args.window <= 0 or args.binwidth <= 0 or
            args.release_threshold >= args.threshold or args.stale_seconds <= 0 or
            args.coverage_age <= 0 or args.min_presence_seconds <= 0 or
            args.occupancy_window < args.min_presence_seconds or
            not 0 < args.min_occupancy <= 1 or not 0 < args.min_coverage <= 1 or
            args.observation_max_gap <= 0 or args.min_observation_spacing <= 0 or
            args.release_seconds <= 0 or args.candidate_reset_seconds <= 0 or args.min_hold_seconds < 0 or
            args.cluster_mhz <= 0 or args.calibration_seconds <= 0 or
            args.calibration_min_samples < 3 or args.min_observations < 2 or
            args.noise_margin < 0 or args.smoothing < 1 or args.rearm_seconds <= 0):
        p.error('Invalid detector settings')
    if args.audio_test:
        a = PulseAudio(args, resolve_wav(args.wav))
        try:
            for db in (6, 10, 14, 18, 22):
                interval, tone = beep_parameters(db, args)
                if args.audio_style == 'wav':
                    interval = v19e6_wave_period(db, args, args.warning_wav_seconds)
                    tone_info = 'warning.wav / tono fijo'
                else:
                    tone_info = f'{tone} Hz'
                print(f'TEST {db:+.0f} dB -> {tone_info} / {interval:.2f}s, device {args.audio_device}', flush=True)
                a.set_target((5917, float(db)))
                time.sleep(2.0)
            a.set_target(None)
        finally:
            a.close()
        if a.error:
            p.exit(2, 'Audio error: ' + a.error + '\n')
        return
    try:
        curses.wrapper(run, args)
    except curses.error as error:
        p.exit(2, f'Cannot start the TUI ({error}); use an interactive terminal with TERM=xterm-256color\n')


if __name__ == '__main__':
    main()
