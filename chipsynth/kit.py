# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The built-in DAC kit of the song generator (see DESIGN-MUSIC.md): a few samples made from numpy maths, encoded with
the library's own DPCM encoder, so a generated song that plays drums, a pitched bass or a shout on the DAC lanes needs no
extra files.

    kit.builtin("kit_kick")          -> dpcm.Sample, or None for a name that is not in the kit
    kit.bass_name(40)                -> "kit_bass_e_three"   (a clean tone at that note number, counted from C0)
    kit.write_files(folder, names)   -> writes NAME.dpcm for each name (a game's music/samples folder)

Names (letters and _ only, like every sample name): kit_kick kit_snare kit_hat kit_ohat kit_crash kit_tom kit_tomlo
kit_hey, and kit_bass_NOTE_OCTAVE for the pitched bass.  The MML parser falls back to this kit when no NAME.dpcm file
and no [sample] file of that name exists, so a file of the same name in music/samples (your own kick, say) wins.
"""
import re
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import dpcm
from .synth import DPCM_RATES

SR = 44100                                           # the rate the sounds are made at; dpcm.convert resamples
PREFIX = "kit_"
DRUMS = ("kit_kick", "kit_snare", "kit_hat", "kit_ohat", "kit_crash", "kit_tom", "kit_tomlo")
VOICE = ("kit_hey",)
NAMES = DRUMS + VOICE
NOTES = ("c", "cs", "d", "ds", "e", "f", "fs", "g", "gs", "a", "as", "b")
OCTAVES = ("zero", "one", "two", "three", "four", "five", "six", "seven")
BASS_RE = re.compile(r"kit_bass_(%s)_(%s)$" % ("|".join(sorted(NOTES, key=len, reverse=True)), "|".join(OCTAVES)))


def bass_name(pitch):
    """The kit sample for note number `pitch` (semitones from C0, as everywhere in chipsynth): 12 to 95."""
    pitch = max(12, min(95, int(pitch)))
    return "kit_bass_%s_%s" % (NOTES[pitch % 12], OCTAVES[pitch // 12])


def bass_pitch(name):
    """The note number a kit_bass_* name stands for, or None."""
    m = BASS_RE.match(name)
    return None if m is None else NOTES.index(m.group(1)) + 12 * OCTAVES.index(m.group(2))


def _t(seconds):
    return np.arange(int(seconds * SR)) / SR


def _taper(x, ms=8):
    n = min(len(x), int(SR * ms / 1000))
    if n > 1:
        x = x.copy()
        x[-n:] *= np.linspace(1.0, 0.0, n)
    return x


def _taper_at(x, fs, ms=8):
    n = min(len(x), int(fs * ms / 1000))
    if n > 1:
        x = x.copy()
        x[-n:] *= np.linspace(1.0, 0.0, n)
    return x


def _noise(n, seed):
    return np.random.default_rng(seed).standard_normal(n)


def _sweep(t, f_end, f_start, speed):
    """sin of a frequency that falls from f_start to f_end (Hz) with time constant 1/speed."""
    return np.sin(2 * np.pi * (f_end * t + (f_start - f_end) * (1 - np.exp(-speed * t)) / speed))


def _kick():
    t = _t(0.26)
    return _taper(_sweep(t, 46, 170, 30) * np.exp(-t * 11))


def _walk(seconds, rate, seed, persist, amp, decay, attack=0.002):
    """Noise made the way a delta sample can carry it: a +-1 step walk (each step is one DPCM bit) kept inside a
    falling envelope.  A delta sample cannot hold loud high frequencies, so this is as loud as noise gets at this rate."""
    fs = DPCM_RATES[rate]
    n = int(seconds * fs)
    u = np.random.default_rng(seed).random(n)
    t = np.arange(n) / fs
    env = (amp * np.exp(-t * decay) * np.minimum(1.0, t / attack) + 0.01).tolist()
    out = np.empty(n)
    lvl, d, step = 0.0, 1, 2.0 / 63.0
    for i, r in enumerate(u.tolist()):
        if r > persist:
            d = -d
        if lvl > env[i]:
            d = -1
        elif lvl < -env[i]:
            d = 1
        lvl += d * step
        out[i] = lvl
    return out


def _snare():
    fs = DPCM_RATES[15]
    x = _walk(0.2, 15, 11, 0.6, 0.45, 14)
    t = np.arange(len(x)) / fs
    return _taper_at(x + _sweep(t, 175, 230, 40) * np.exp(-t * 26) * 0.42, fs)


def _hat():
    return _taper_at(_walk(0.06, 15, 21, 0.5, 0.32, 55, 0.0005), DPCM_RATES[15], 4)


def _ohat():
    return _taper_at(_walk(0.3, 15, 31, 0.5, 0.3, 8, 0.0005), DPCM_RATES[15])


def _crash():
    return _taper_at(_walk(0.85, 15, 41, 0.55, 0.42, 3.2, 0.001), DPCM_RATES[15], 20)


def _tom(hi, lo, decay):
    t = _t(0.34)
    return _taper(_sweep(t, lo, hi, 14) * np.exp(-t * decay))


def _resonator(x, hz, bw):
    from scipy.signal import lfilter
    r = np.exp(-np.pi * bw / SR)
    th = 2 * np.pi * hz / SR
    return lfilter([1.0 - r], [1.0, -2 * r * np.cos(th), r * r], x)


def _hey():
    """A shouted 'hey': an aspirated h, then a buzzing /e/ that glides to /i/ (formants), pitch rising then falling."""
    t = _t(0.42)
    f0 = 135 + 55 * np.exp(-((t - 0.09) / 0.07) ** 2) - 35 * np.clip((t - 0.22) / 0.2, 0, 1)
    phase = np.cumsum(f0) / SR
    buzz = 2 * (phase % 1.0) - 1.0                                     # sawtooth: a glottal-like pulse train
    buzz = np.where(buzz > 0.0, buzz, buzz * 0.6)
    air = _noise(len(t), 51)

    def vowel(f1, f2, f3):
        return (_resonator(buzz, f1, 90) * 1.0 + _resonator(buzz, f2, 140) * 0.8 + _resonator(buzz, f3, 220) * 0.4)
    mix = np.clip((t - 0.12) / 0.2, 0, 1)
    voiced = vowel(560, 1800, 2600) * (1 - mix) + vowel(330, 2200, 2900) * mix
    h = (_resonator(air, 1500, 700) + _resonator(air, 2400, 900)) * np.clip(1 - t / 0.09, 0, 1) * 0.5
    env = np.minimum(1.0, t / 0.025) * np.where(t < 0.2, 1.0, np.exp(-(t - 0.2) * 9))
    return _taper((voiced * np.minimum(1.0, (t - 0.04).clip(0) / 0.03) + h) * env, 15)


def _bass(pitch):
    """A clean pitched tone: a sine with a touch of second harmonic and a plucked, slowly falling level."""
    f = 440.0 * 2.0 ** ((pitch - 57) / 12.0)
    t = _t(1.0)
    ph = 2 * np.pi * f * t
    x = (np.sin(ph) + 0.28 * np.sin(2 * ph) + 0.08 * np.sin(3 * ph)) * np.minimum(1.0, t / 0.004) * np.exp(-t * 2.2)
    return _taper(x, 30)


# name -> (builder, DPCM rate, fit, native).  fit=True turns a loud tone down until the delta encoder follows it;
# native=True means the builder already works at the DPCM rate and at the level a delta sample can carry (kept as is).
_SPEC = {
    "kit_kick": (_kick, 14, False, False), "kit_snare": (_snare, 15, False, True), "kit_hat": (_hat, 15, False, True),
    "kit_ohat": (_ohat, 15, False, True), "kit_crash": (_crash, 15, False, True),
    "kit_tom": (lambda: _tom(260, 150, 9), 14, False, False), "kit_tomlo": (lambda: _tom(170, 95, 8), 14, False, False),
    "kit_hey": (_hey, 15, False, False),
}


@lru_cache(maxsize=256)
def _make(name):
    if name in _SPEC:
        build, rate, fit, native = _SPEC[name]
        x = build()
    else:
        pitch = bass_pitch(name)
        if pitch is None:
            return None
        x, rate, fit, native = _bass(pitch), 15, False, False
    if native:
        return dpcm.convert(x, DPCM_RATES[rate], rate=rate, name=name, normalise=False)
    return dpcm.convert(x, SR, rate=rate, name=name, fit=fit)


def builtin(name):
    """The kit sample called `name` (a fresh copy you may change), or None."""
    if not isinstance(name, str) or not name.startswith(PREFIX):
        return None
    smp = _make(name)
    return None if smp is None else dpcm.Sample(smp.name, smp.rate, [list(t) for t in smp.tracks])


def write_files(folder, names=None):
    """Write NAME.dpcm for each name (default: the drums and the voice) into folder; returns the paths."""
    out = []
    for n in (NAMES if names is None else names):
        smp = builtin(n)
        if smp is not None:
            p = Path(folder) / (n + ".dpcm")
            dpcm.save(p, smp)
            out.append(p)
    return out
