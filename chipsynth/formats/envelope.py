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
