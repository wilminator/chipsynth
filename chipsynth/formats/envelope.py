"""Fit a tracker's per-frame envelopes to chipsynth's instrument settings (a d s r vol, arp, pulse width)."""
import math

import numpy as np

from ..synth import ATTACK_MS, DECAY_MS

FRAME_HZ = 60.0988
FIT_FRAMES = 120            # how much of an envelope the fit looks at (two seconds)
GOOD = 0.08                 # an RMS error (0..1 of full volume) above this is reported as approximate


def resolve(values, loop, release, length, frames, released_at=None):
    """The value of an envelope on each of `frames` frames, as the NES driver plays it: it steps one value a frame, jumps to
    `loop` at the end (or holds the last value), and from `release` on only plays once the note is released
    (`released_at` is the frame of the release, None = held for ever)."""
    values = list(values)[:length] or [0]
    n = len(values)
    out, idx = [], 0
    for f in range(frames):
        if idx < 0:
            out.append(out[-1])
            continue
        out.append(values[idx])
        idx += 1
        if release >= 0 and idx == release and (released_at is None or f < released_at):
            idx = loop if loop >= 0 else release - 1
        elif idx >= n:
            idx = loop if loop >= 0 and release < 0 else n - 1
    return out


def adsr(a, d, s, r, gate, t):
    """chipsynth's envelope (synth.Voice.envelope) at times t (seconds) for a gate of `gate` seconds."""
    t = np.asarray(t, dtype=np.float64)
    out = np.zeros_like(t)
    att, dec = ATTACK_MS[a] / 1000.0, DECAY_MS[d] / 1000.0
    sus = s / 15.0
    up = t < att
    out[up] = t[up] / att
    td = t - att
    dn = ~up
    out[dn] = sus + (1.0 - sus) * np.exp(-6.9 * td[dn] / dec)
    # the gate closes at `gate`: release from wherever the level was
    if gate is not None:
        at = float(np.interp(gate, t, out)) if len(t) else 0.0
        rel = t >= gate
        out[rel] = at * np.exp(-6.9 * (t[rel] - gate) / (DECAY_MS[r] / 1000.0))
    return out


def fit_volume(values, loop, release, length, hz=FRAME_HZ):
    """Fit a volume envelope (0-15 a frame).  Returns (dict of Instrument fields a, d, s, r, vol, error)."""
    if not values or max(values[:length] or [0]) == 0:
        return dict(a=0, d=8, s=10, r=0, vol=15), 0.0
    peak = max(values[:length])
    gate_frames = FIT_FRAMES
    target = np.array(resolve(values, loop, release, length, gate_frames), dtype=np.float64) / peak
    t = (np.arange(gate_frames) + 0.5) / hz                     # the middle of each frame
    best = (9.0, 0, 8, 10)
    for a in range(16):
        for d in range(16):
            for s in range(16):
                e = float(np.sqrt(np.mean((adsr(a, d, s, 0, None, t) - target) ** 2)))
                if e < best[0]:
                    best = (e, a, d, s)
    err, a, d, s = best
    r = 0
    if release >= 0:
        at = release / hz                                   # a note held past the release point plays the tail
        tail = np.array(resolve(values, loop, release, length, release + 60, released_at=release), dtype=np.float64) / peak
        base = tail[release - 1] if release > 0 else 1.0
        rest = tail[release:]
        tt = (np.arange(len(rest)) + 0.5) / hz
        rbest = (9.0, 0)
        for rr in range(16):
            e = float(np.sqrt(np.mean((base * np.exp(-6.9 * tt / (DECAY_MS[rr] / 1000.0)) - rest) ** 2))) if len(rest) else 0.0
            if e < rbest[0]:
                rbest = (e, rr)
        r = rbest[1]
    return dict(a=a, d=d, s=s, r=r, vol=int(peak)), err


def fit_arpeggio(values, loop, frames_per_beat):
    """An arpeggio envelope (semitone offsets a frame) as (offsets tuple, steps per beat, exact?).  A repeating cycle of at
    most 8 steps is exact; anything else keeps its repeating part (or its first 8 steps) and is not."""
    values = [int(v) for v in values]
    if not values or not any(values):
        return (), 8, True
    body = values[loop:] if loop >= 0 else values
    # collapse runs: the frames each distinct step lasts
    runs = []
    for v in body:
        if runs and runs[-1][0] == v:
            runs[-1][1] += 1
        else:
            runs.append([v, 1])
    step = min(r[1] for r in runs) if runs else 1
    seq = []
    for v, n in runs:
        seq += [v] * max(1, round(n / step))
    cycle = _cycle(seq)
    exact = loop == 0 and len(cycle) <= 8 and all(r[1] % step == 0 for r in runs)
    cycle = cycle[:8]
    rate = max(1, min(64, round(frames_per_beat / step)))
    return tuple(cycle), rate, exact


def _cycle(seq):
    for n in range(1, len(seq) + 1):
        if len(seq) % n == 0 and seq[:n] * (len(seq) // n) == seq:
            return seq[:n]
    return seq


# ------------------------------------------------------------------ per-frame simulation
NES_CPU = 1789773.0
TAIL_FRAMES = 180           # how long the tail of a released note is followed (three seconds)


def resolve_note(values, loop, release, length, frames, released_at=None):
    """The value of an envelope on each of `frames` frames for a note released at frame `released_at` (None = never).
    Like `resolve`, but a release that arrives while the envelope is still before its release point jumps straight
    to it (FamiStudio: "terminates the loop by jumping to the release point")."""
    values = list(values)[:length] or [0]
    n = len(values)
    out, idx, freed = [], 0, False
    for f in range(frames):
        if released_at is not None and f >= released_at and not freed:
            freed = True
            if 0 <= release < n and idx < release:
                idx = release
        out.append(values[min(max(idx, 0), n - 1)])
        idx += 1
        if release >= 0 and idx == release and not freed:
            idx = loop if loop >= 0 else release - 1
        elif idx >= n:
            idx = loop if loop >= 0 and release < 0 else n - 1
    return out


def accumulate(values, loop, length, frames):
    """A relative envelope: each frame adds its value to the running total, and the total stops changing where a
    non-looping envelope ends."""
    values = list(values)[:length] or [0]
    n, out, idx, total = len(values), [], 0, 0
    for _ in range(frames):
        if idx >= 0:
            total += values[idx]
            idx += 1
            if idx >= n:
                idx = loop if loop >= 0 else -1
        out.append(total)
    return out


def pitch_semitones(units, pitch, k=16.0):
    """Semitones that `units` steps of an NES period register move a note of chipsynth `pitch` (positive = higher):
    the register holds cpu / (k f) - 1, with k 16 for the squares and the VRC6 pulses, 32 for the triangle."""
    f = 440.0 * 2.0 ** ((pitch - 57) / 12.0)
    p1 = NES_CPU / (k * f)
    return 12.0 * math.log2(p1 / max(1.0, p1 - units))


def fit_vibrato(offsets, hz):
    """A looping pitch swing (semitone offsets a frame, one cycle) as chipsynth's vibrato: (rate 1-15, depth 1-15, error).
    chipsynth's vibrato is a sine of `rate x 0.8` Hz and `depth / 16` semitone; the error is the RMS difference in
    semitones against the best phase of that sine.  None when the swing is a constant or is too fast or too deep."""
    cyc = _cycle([round(o, 4) for o in offsets])
    if len(cyc) < 3:
        return None
    lo, hi = min(cyc), max(cyc)
    amp = (hi - lo) / 2.0
    depth = int(round(amp * 16))
    rate = int(round(hz / len(cyc) / 0.8))
    if depth < 1 or depth > 15 or rate < 1 or rate > 15:
        return None
    mid = (hi + lo) / 2.0
    x = np.arange(len(cyc))
    best = min(float(np.sqrt(np.mean((np.array(cyc) - mid - depth / 16.0 * np.sin(2 * np.pi * (x / len(cyc) + s / 32.0))) ** 2)))
               for s in range(32))
    return rate, depth, best


WAVE_POINTS = 64


def fit_wave(values):
    """The nearest chipsynth wave to a one-cycle wavetable (FDS 64 steps of 0-63, N163 4-32 steps of 0-15).  Phase and
    polarity do not change the sound, so every shift and the inverted wave are tried.  Returns (wave, pulse width,
    error): the error is the RMS difference of the two waves, each scaled to an RMS of 1 (0 = the same shape)."""
    v = np.asarray(values, dtype=np.float64)
    if len(v) == 0:
        return "pulse", 50, 2.0
    x = v[(np.arange(WAVE_POINTS) * len(v)) // WAVE_POINTS]       # the table is held step by step
    x = x - x.mean()
    rms = float(np.sqrt(np.mean(x * x)))
    if rms < 1e-9:
        return "pulse", 50, 2.0                      # a flat table makes no sound; nothing fits
    x = x / rms
    ph = np.arange(WAVE_POINTS) / WAVE_POINTS
    cands = [("sine", 50, np.sin(2 * np.pi * ph)), ("tri", 50, 4 * np.abs(ph - 0.5) - 1), ("saw", 50, 2 * ph - 1)]
    cands += [("pulse", max(1, round(100.0 * k / WAVE_POINTS)), np.where(np.arange(WAVE_POINTS) < k, 1.0, -1.0)) for k in range(1, WAVE_POINTS // 2 + 1)]
    fx = np.fft.rfft(x)
    best = (3.0, "pulse", 50)
    for name, pw, c in cands:
        c = c - c.mean()
        c = c / math.sqrt(float(np.mean(c * c)))
        corr = float(np.max(np.abs(np.fft.irfft(fx * np.conj(np.fft.rfft(c)), WAVE_POINTS)))) / WAVE_POINTS
        err = math.sqrt(max(0.0, 2.0 - 2.0 * corr))
        if err < best[0] - 1e-9:
            best = (err, name, pw)
    return best[1], best[2], best[0]
