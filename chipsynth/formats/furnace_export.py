# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""chipsynth songs -> Furnace `.fur` modules (format version 228), for the NES (2A03) or the C64 SID (6581).

Written from the file format as Furnace's own documents describe it (papers/format.md, papers/newIns.md) and checked
against Furnace's loader and player (v0.6.8.3, GPL, read only, built headless and run on every file; nothing copied):
what is written must load there, render to the notes chipsynth plays, and read back through furnace.py (tests).

Targets
  nes   V1, V2 -> Pulse 1, 2; V3 -> Triangle; V4 -> Noise; D1 -> DPCM (its samples are written into the module).
  sid   V1-V3 -> the three SID voices (a 6581).
Everything else the song holds (more voices, more DACs, echo, reverb, detune, per-note pan, ...) is left out and said so
in the Report.

Format version 228 (Furnace 0.6.8.1): the newest version papers/format.md documents.  Furnace 0.6.8.3 saves 232, but the
four versions in between change nothing a NES or SID module holds (they only touch the VERA and YM2612 chip flags), and a
Furnace refuses a module newer than itself, so the older number opens in every release from 0.6.8.1 on.

Timing: a Furnace tick is one frame (60 Hz NTSC, 50 Hz PAL) and a row lasts `speed` ticks.  A row is a whole number of
ticks, so a tempo that is not a whole number of frames a row gets a speed pattern (a "groove", up to 16 speeds that repeat)
whose average is the nearest the 16 allow; each tempo change selects its own with 09xx on the first voice.  A looping song
ends with 0Bxx (+ 0Dxx when the loop point is mid pattern); a song marked `once` ends with FFxx.

Instruments: the ADSR of a NES voice is a volume macro sampled once a tick (a long envelope is stretched), with the release
tail after a release point that a note release (not a note off) plays; a note shorter than its envelope gets a macro of its
own so the tail starts where the note ended.  Arpeggio, pulse width sweep and noise mode are macros too.  A SID voice keeps
its ADSR in the chip's own registers.
"""
import math
import struct
import zlib
from dataclasses import dataclass, field
from functools import reduce

import numpy as np

from ..synth import DECAY_MS, filter_cutoff_hz
from .envelope import adsr
from .famistudio_text import DUTY_PW, noise_pitch
from .report import Report

VERSION = 228
MAGIC = b"-Furnace module-"
TARGETS = ("nes", "sid")
CHIP_IDS = {"nes": 0x06, "sid": 0x47}
CHIP_NAMES = {"nes": "NES (2A03)", "sid": "C64 (6581)"}
CHANNELS = {"nes": ("Pulse 1", "Pulse 2", "Triangle", "Noise", "DPCM"), "sid": ("Voice 1", "Voice 2", "Voice 3")}
INS_NES, INS_C64 = 34, 3
HZ = {"ntsc": 60.0, "pal": 50.0}
C0 = 60                                       # a pattern note of 60 is C-0 (chipsynth pitch 0)
NOTE_OFF, NOTE_RELEASE = 180, 181
PULSE_SHIFT = -12                             # Furnace's NES pulse sounds an octave above the note's name (measured), the triangle and the SID do not
NOISE_BASE = C0 + 32                          # a noise note's low four bits pick its period
MAX_ROWS, MAX_ORDERS, MAX_PATTERNS, MAX_INSTRUMENTS, MAX_SAMPLES = 256, 256, 256, 255, 255
MAX_MACRO = 255
SID_TOP = 95                                  # the highest note a SID voice plays (the 16-bit frequency register)
DPCM_BYTES = 4081                             # the most one NES DPCM sample holds (16 * 255 + 1)
GRIDS = (12, 16, 24, 32, 48, 96)
MAC_VOL, MAC_ARP, MAC_DUTY, MAC_PITCH = 0, 1, 2, 4
NES_DPCM_MAP = 120                            # entries in a sample map (C-0 up)


class FurnaceExportError(ValueError):
    pass


def dump(tune, target="nes", region="ntsc", name=None, compress=True):
    """A compiled Tune -> (`.fur` bytes, Report).  `target` is 'nes' or 'sid', `region` 'ntsc' or 'pal' (the frame rate
    and the chip's clock); `compress` writes the zlib-packed form Furnace itself saves (the loader reads both)."""
    if target not in TARGETS:
        raise FurnaceExportError("target must be one of %s" % ", ".join(TARGETS))
    if region not in HZ:
        raise FurnaceExportError("region must be ntsc or pal")
    data, report = _Writer(tune, target, region, name).run()
    return (zlib.compress(data, 9) if compress else data), report


# ------------------------------------------------------------------ bytes
class _Buf:
    def __init__(self):
        self.b = bytearray()

    def u8(self, v):
        self.b.append(v & 255)
        return self

    def u16(self, v):
        self.b += struct.pack("<H", v & 0xFFFF)
        return self

    def i16(self, v):
        self.b += struct.pack("<h", v)
        return self

    def u32(self, v):
        self.b += struct.pack("<I", v & 0xFFFFFFFF)
        return self

    def f32(self, v):
        self.b += struct.pack("<f", v)
        return self

    def s(self, text):
        self.b += text.encode("utf-8", "replace") + b"\0"
        return self

    def raw(self, data):
        self.b += data
        return self

    def tell(self):
        return len(self.b)

    def patch32(self, pos, v):
        self.b[pos:pos + 4] = struct.pack("<I", v)


def _block(tag, body):
    return tag + struct.pack("<I", len(body)) + bytes(body)


def _text(s, limit=None):
    s = " ".join(str(s or "").replace("\0", " ").split())
    return s[:limit] if limit else s


# ------------------------------------------------------------------ macros
@dataclass
class Macro:
    code: int
    values: list
    loop: int = 255
    rel: int = 255
    speed: int = 1

    def pack(self):
        v = [int(x) for x in self.values][:MAX_MACRO]
        lo, hi = min(v), max(v)
        if lo >= 0 and hi <= 255:
            word, fmt = 0, "B"
        elif lo >= -128 and hi <= 127:
            word, fmt = 64, "b"
        elif lo >= -32768 and hi <= 32767:
            word, fmt = 128, "<h"
        else:
            word, fmt = 192, "<i"
        out = bytes((self.code, len(v), self.loop if self.loop < len(v) else 255, self.rel if self.rel < len(v) else 255,
                     0, word, 0, max(1, self.speed)))
        return out + b"".join(struct.pack(fmt, x) for x in v)


def _same(vals):
    """vals with the repeated tail cut to one value, and the index where that last run starts."""
    last = len(vals) - 1
    while last > 0 and vals[last - 1] == vals[-1]:
        last -= 1
    return vals[:last + 1], last


@dataclass
class _Ins:
    name: str
    kind: str                      # nes: pulse tri noise dpcm; sid: sid
    macros: list = field(default_factory=list)
    sid: dict = None               # the 64 feature's fields
    sample_map: dict = None        # dpcm: {key note: (sample index, rate)}


class _Writer:
    def __init__(self, tune, target, region, name):
        self.t, self.target, self.region, self.name = tune, target, region, name
        self.pal = region == "pal"
        self.hz = HZ[region]
        self.r = Report()
        self.nch = len(CHANNELS[target])
        self.names = {}
        for n, i in tune.instruments.items():
            self.names.setdefault(tuple(sorted(vars(i).items())), n)
        self.insts = []                      # [_Ins]
        self.by_key = {}
        self.said = set()
        self.cache = {}
        self.samples = []                    # [(name, bytes, rate)]
        self.sample_ix = {}

    def say(self, msg, drop=False):
        if msg not in self.said:
            self.said.add(msg)
            (self.r.drop if drop else self.r.approx)(msg)

    # ------------------------------------------------------------------ the song's time
    def voices(self):
        t = self.t
        n = 4 if self.target == "nes" else 3
        return [list(t.voices[i]) if i < len(t.voices) else [] for i in range(n)]

    def dac_events(self):
        t = self.t
        return list(t.dac[0]) if (self.target == "nes" and t.dac and t.ndacs) else []

    def tempo_map(self):
        t = self.t
        tempos = {0: t.bpm}
        for tick, b in t.tempo_events:
            tempos[tick] = b
        out, last = [], None
        for tick in sorted(tempos):
            if tempos[tick] != last:
                out.append((tick, tempos[tick]))
                last = tempos[tick]
        return out

    def grid(self, voices, dacs):
        t = self.t
        ticks = {t.bar_ticks, t.loop_tick, t.length} | {tk for tk, _ in t.tempo_events}
        for v in voices:
            for n in v:
                ticks |= {n.start, n.start + n.dur}
        ticks |= {e[0] for e in dacs}
        ticks.discard(0)
        g = reduce(math.gcd, ticks) if ticks else 24
        if g > 24:
            g = max(d for d in range(1, 25) if g % d == 0)
        fastest = max(60.0 / (b * 96) * self.hz for _, b in self.tempo_map())      # frames a tick at the fastest tempo
        if g * fastest < 1.0:
            g2 = next((c for c in GRIDS if c * fastest >= 1.0), 96)
            self.r.approx("the song's finest timing is %d ticks but a row cannot be shorter than one frame at this tempo: "
                          "notes are moved to the nearest row of %d ticks (1/%g of a quarter note)" % (g, g2, 96 / g2))
            g = g2
        return g

    @staticmethod
    def groove(rf):
        """Speeds (1-16 of them, repeated) whose mean is nearest `rf` frames a row, cumulatively rounded."""
        rf = max(1.0, rf)
        found = []
        for n in range(1, 17):
            total = max(n, int(math.floor(rf * n + 0.5)))
            found.append((abs(total / n - rf) / rf, n, total))
        best = min(f[0] for f in found)
        _, n, total = min((f for f in found if f[0] <= best + 0.0005), key=lambda f: f[1])
        speeds = [int(math.floor((i + 1) * total / n + 0.5) - math.floor(i * total / n + 0.5)) for i in range(n)]
        return [max(1, min(255, s)) for s in speeds]

    # ------------------------------------------------------------------ instruments
    def label(self, inst, kind):
        return self.names.get(tuple(sorted(vars(inst).items()))) or "inst"

    def add(self, key, make):
        if key in self.by_key:
            return self.by_key[key]
        if len(self.insts) >= MAX_INSTRUMENTS - (1 if self.target == "nes" else 0):
            return None
        ins = make()
        taken = {i.name for i in self.insts}
        base, n = _text(ins.name, 40) or "inst", 1
        nm = base
        while nm in taken:
            n += 1
            nm = "%s_%d" % (base, n)
        ins.name = nm
        self.insts.append(ins)
        self.by_key[key] = len(self.insts) - 1
        return self.by_key[key]

    def envelope(self, inst):
        """(levels, settle, step, plateau): the held ADSR as whole 0-15 volumes a tick (each lasting `step` frames), the first
        index it stays at its sustain level from, and that level."""
        key = ("env", inst.a & 15, inst.d & 15, inst.s & 15, inst.vol)
        if key in self.cache:
            return self.cache[key]
        plateau = int(math.floor(inst.s / 15.0 * inst.vol + 0.5))
        step = 1
        while True:
            t = (np.arange(250) * step + step / 2.0) / self.hz
            lv = np.floor(adsr(inst.a & 15, inst.d & 15, inst.s & 15, 0, None, t) * inst.vol + 0.5).astype(int)
            settle = next((k for k in range(250) if (lv[k:] == plateau).all()), None)
            if settle is not None or step >= 32:
                break
            step += 1
        if settle is None:
            settle = 249
        if step > 1:
            self.say("instrument '%s': its envelope is longer than %d frames, so each volume value lasts %d frames" % (
                self.names.get(tuple(sorted(vars(inst).items()))) or "inst", MAX_MACRO, step))
        self.cache[key] = (lv, settle, step, plateau)
        return self.cache[key]

    def vol_macro(self, inst, gate=None):
        """The volume Macro of a NES voice.  `gate` is the frames a note that is released is held; it is used when that is
        before the envelope settles, so the release tail starts from where the note ended (otherwise the macro is shared)."""
        lv, settle, step, plateau = self.envelope(inst)
        tau = DECAY_MS[inst.r & 15] / 1000.0
        per_note = gate is not None and step == 1 and 1 <= gate < settle
        if per_note:
            vals, rel = [int(x) for x in lv[:gate]], gate - 1
            top = adsr(inst.a & 15, inst.d & 15, inst.s & 15, 0, None, np.array([gate / self.hz]))[0] * inst.vol
        else:
            vals, rel = [int(x) for x in lv[:settle + 1]], settle
            top = inst.s / 15.0 * inst.vol
        tail = []
        if top >= 0.5:
            # the macro repeats its last held value on the tick of the release, so the tail starts one tick later
            for m in range(240):
                if len(vals) + len(tail) >= MAX_MACRO - 1:
                    break
                v = max(0, int(math.floor(top * math.exp(-6.9 * (m + 1.5) * step / self.hz / tau) + 0.5)))
                tail.append(v)
                if v == 0:
                    break
            if not tail or tail[-1] != 0:
                tail.append(0)
        return Macro(MAC_VOL, (vals + tail)[:MAX_MACRO], 255, rel if tail else 255, step)

    def effective_gate(self, inst, kind, gate):
        """`gate` when a note that long gets a volume macro of its own (it ends before its envelope settles), else None."""
        if gate is None or kind == "tri":
            return None
        _lv, settle, step, _p = self.envelope(inst)
        return gate if (step == 1 and 1 <= gate < settle) else None

    def duty_values(self, inst, label):
        snap = lambda pw: min(range(4), key=lambda i: abs(DUTY_PW[i] - pw))
        if inst.wave == "pulse" and inst.pws_rate and inst.pws_from and inst.pws_to:
            n = max(2, min(MAX_MACRO, round(2.0 * self.hz / inst.pws_rate)))
            vals = []
            for i in range(n):
                tri = 1.0 - abs(2.0 * (i / n) - 1.0)
                vals.append(snap(min(99, max(1, inst.pws_from + (inst.pws_to - inst.pws_from) * tri))))
            self.say("instrument '%s': the pulse-width sweep is sampled once a frame and played with the NES's four widths (12, 25, 50, 75)" % label)
            return vals, 0
        duty = snap(inst.pw)
        if inst.wave == "pulse" and abs(DUTY_PW[duty] - inst.pw) > 3:
            self.say("instrument '%s': pulse width %d%% is played as %d%% (the NES has 12, 25, 50 and 75)" % (label, inst.pw, DUTY_PW[duty]))
        return [duty], 255

    def arp_values(self, inst, label):
        step = max(1.0, 60.0 / max(1, self.t.bpm) * self.hz / max(1, inst.arprate))     # frames to a step
        n = len(inst.arp)
        total = max(n, round(step * n))
        if total > MAX_MACRO:
            self.say("instrument '%s': the arpeggio is longer than a macro (%d values): it is cut and sped up" % (label, MAX_MACRO))
            step, total = step * MAX_MACRO / total, MAX_MACRO
        elif abs(step - round(step)) > 0.05 * step:
            self.say("instrument '%s': the arpeggio's steps are not a whole number of frames long: they are rounded to frames" % label)
        return [int(inst.arp[int(f / step) % n]) for f in range(total)], 0

    def vibrato_macro(self, inst, label):
        """A pitch macro (1/128 semitone steps a tick) that is chipsynth's vibrato: a sine of rate x 0.8 Hz and depth/16
        semitone, looped over whole cycles so the rate stays near."""
        period = self.hz / (inst.vib_rate * 0.8)                 # ticks in one cycle
        best = None
        for cycles in range(1, 9):
            n = int(round(cycles * period))
            if n < 2 or n > MAX_MACRO:
                continue
            err = abs(n / (cycles * period) - 1.0)
            if best is None or err < best[0] - 0.005:
                best = (err, cycles, n)
        if best is None:
            self.say("instrument '%s': its vibrato is too slow for a macro" % label, drop=True)
            return None
        err, cycles, n = best
        amp = inst.vib_depth / 16.0 * 128.0
        vals = [int(round(amp * math.sin(2 * math.pi * cycles * (i + 0.5) / n))) for i in range(n)]
        if err > 0.02:
            self.say("instrument '%s': the vibrato rate is %.0f%% off (a macro steps once a frame)" % (label, err * 100))
        return Macro(MAC_PITCH, vals, 0)

    def nes_instrument(self, inst, kind, gate):
        """Index of the NES instrument for chipsynth instrument `inst` on a `kind` channel (pulse, tri, noise).  `gate` is how
        many frames a note that gets a release is held (see vol_macro)."""
        label = self.label(inst, kind)
        gate = self.effective_gate(inst, kind, gate)
        arp = tuple(int(a) for a in inst.arp) if kind != "noise" else ()
        key = ("nes", kind, inst.a & 15, inst.d & 15, inst.s & 15, inst.r & 15, inst.vol, arp, inst.arprate if arp else 0,
               (inst.pw, inst.pws_from, inst.pws_to, inst.pws_rate) if kind == "pulse" else (0,),
               inst.nmode if kind == "noise" else "", gate, (inst.vib_rate, inst.vib_depth) if kind != "noise" else 0)

        def make():
            macros = []
            if kind != "tri":
                macros.append(self.vol_macro(inst, gate))
            elif inst.a or inst.s < 15 or inst.vol < 15:
                self.say("instrument '%s': the triangle has no volume, so its volume envelope is not played" % label)
            if inst.arp and any(inst.arp):
                if kind == "noise":
                    self.say("instrument '%s': the arpeggio is not played on the noise channel" % label, drop=True)
                else:
                    vals, loop = self.arp_values(inst, label)
                    macros.append(Macro(MAC_ARP, vals, loop))
            if inst.vib_rate and inst.vib_depth:
                if kind == "noise":
                    self.say("instrument '%s': vibrato is not played on the noise channel" % label, drop=True)
                else:
                    m = self.vibrato_macro(inst, label)
                    if m:
                        macros.append(m)
            if kind == "pulse":
                vals, loop = self.duty_values(inst, label)
                macros.append(Macro(MAC_DUTY, vals, loop))
            elif kind == "noise":
                macros.append(Macro(MAC_DUTY, [1 if inst.nmode == "short" else 0]))
            return _Ins(label + ("" if gate is None else ".g%d" % gate), kind, macros)
        got = self.add(key, make)
        if got is None:
            self.say("more than %d instruments: later ones play with instrument 0" % MAX_INSTRUMENTS, drop=True)
            return 0
        return got

    def sid_instrument(self, inst):
        label = self.label(inst, "sid")
        wave = {"pulse": 4, "saw": 2, "tri": 1, "noise": 8, "sine": 1}.get(inst.wave)
        if wave is None:
            self.say("instrument '%s': the wave '%s' is played as a pulse" % (label, inst.wave))
            wave = 4
        elif inst.wave == "sine":
            self.say("instrument '%s': the SID has no sine wave: it is played as a triangle" % label)
        if inst.wave == "noise" and inst.nmode == "short":
            self.say("instrument '%s': the short noise mode (the SID has one noise)" % label, drop=True)
        arp = tuple(int(a) for a in inst.arp)
        sweep = inst.wave == "pulse" and inst.pws_rate and inst.pws_from and inst.pws_to
        key = ("sid", wave, inst.pw if wave == 4 else 0, inst.a & 15, inst.d & 15, inst.s & 15, inst.r & 15, arp,
               inst.arprate if arp else 0, (inst.pws_from, inst.pws_to, inst.pws_rate) if sweep else (0, 0, 0),
               (inst.filt, inst.filt_cut, inst.filt_res) if inst.filt else "", (inst.vib_rate, inst.vib_depth))

        def make():
            duty = max(1, min(4095, int(round(inst.pw * 4096 / 100.0)))) if wave == 4 else 2048
            macros = []
            f1 = wave
            if sweep:
                n = max(2, min(MAX_MACRO, round(2.0 * self.hz / inst.pws_rate)))
                vals = []
                for i in range(n):
                    tri = 1.0 - abs(2.0 * (i / n) - 1.0)
                    pc = inst.pws_from + (inst.pws_to - inst.pws_from) * tri
                    vals.append(max(1, min(4095, int(round(pc * 4096 / 100.0)))))
                macros.append(Macro(MAC_DUTY, vals, 0))
                f1 |= 0x80                                   # an absolute pulse width
                self.say("instrument '%s': the pulse-width sweep is sampled once a frame" % label)
            if arp and any(arp):
                vals, loop = self.arp_values(inst, label)
                macros.append(Macro(MAC_ARP, vals, loop))
            if inst.vib_rate and inst.vib_depth:
                m = self.vibrato_macro(inst, label)
                if m:
                    macros.append(m)
            f2, cut, res = 0, 0, 0
            if inst.filt:
                f1 |= 0x10 | 0x40                            # through the filter; set it on every note
                f2 |= {"lp": 1, "hp": 2, "bp": 4}[inst.filt]
                hz = filter_cutoff_hz(inst.filt_cut)
                cut = max(0, min(2047, int(round((hz - 30.0) * 2048.0 / 5800.0))))
                res = max(0, min(15, inst.filt_res))
                self.say("the SID has one filter for its three voices: the filter of the last note played is the one that sounds")
                if inst.fs_rate:
                    self.say("instrument '%s': the filter sweep" % label, drop=True)
            return _Ins(label, "sid", macros, dict(f1=f1, f2=f2, ad=(inst.a & 15) << 4 | (inst.d & 15), sr=(inst.s & 15) << 4 | (inst.r & 15),
                                                   duty=duty, cut=cut, res=res))
        got = self.add(key, make)
        if got is None:
            self.say("more than %d instruments: later ones play with instrument 0" % MAX_INSTRUMENTS, drop=True)
            return 0
        return got

    # ------------------------------------------------------------------ DPCM
    def dpcm_key(self, smp):
        """The key (note from C-0) that plays chipsynth sample `smp` at its rate, or None."""
        key = (smp.name, smp.rate & 15)
        if key in self.sample_ix:
            return self.sample_ix[key][0]
        if len(self.sample_ix) >= NES_DPCM_MAP:
            self.say("more than %d samples: the later ones are left out" % NES_DPCM_MAP, drop=True)
            return None
        data = b"".join(bytes(p) for p in (smp.tracks[0] if smp.tracks else []))
        if len(smp.tracks) > 1:
            self.r.approx("sample '%s' is stereo: only its left track is written" % smp.name)
        if len(data) > DPCM_BYTES:
            self.r.approx("sample '%s' is %d bytes; the NES plays at most %d from one sample, so the rest is cut" % (smp.name, len(data), DPCM_BYTES))
            data = data[:DPCM_BYTES]
        # a hardware DPCM sample is 16n+1 bytes; padding is the DPCM's own silence
        want = max(1, (len(data) + 14) // 16 * 16 + 1) if len(data) % 16 != 1 else len(data)
        if want != len(data):
            self.r.note("sample '%s' is padded by %d byte%s of DPCM silence (a hardware sample is 16n+1 bytes)"
                        % (smp.name, want - len(data), "" if want - len(data) == 1 else "s"))
            data = data + b"\xaa" * (want - len(data))
        for ix, (nm, d, rate) in enumerate(self.samples):
            if nm == smp.name and d == data:
                sample = ix
                break
        else:
            sample = len(self.samples)
            if sample >= MAX_SAMPLES:
                self.say("more than %d samples: the later ones are left out" % MAX_SAMPLES, drop=True)
                return None
            self.samples.append((smp.name, data, smp.rate & 15))
        k = len(self.sample_ix)
        self.sample_ix[key] = (k, sample, smp.rate & 15)
        return k

    # ------------------------------------------------------------------ cells
    def place(self, items, total_rows, what="note"):
        """[(start row, end row, payload)] without overlap and nothing past the end."""
        out, lost = [], 0
        for s, e, p in sorted(items, key=lambda x: (x[0], x[1])):
            e = max(e, s + 1)
            if s >= total_rows:
                continue
            if out and s < out[-1][1]:
                if s <= out[-1][0]:
                    lost += 1
                    continue
                out[-1] = (out[-1][0], s, out[-1][2])
            out.append((s, min(e, total_rows), p))
        if lost:
            self.r.approx("%d %s%s began on the same row as another one and %s left out" % (lost, what, "" if lost == 1 else "s", "was" if lost == 1 else "were"))
        return out

    def run(self):
        t, r = self.t, self.r
        voices = self.voices()
        dacs = self.dac_events()
        extra = [i for i in range(len(voices), t.nvoices) if i < len(t.voices) and t.voices[i]]
        if extra:
            r.drop("voices %s (%s has %d melodic channels)" % (", ".join("V%d" % (i + 1) for i in extra), CHIP_NAMES[self.target], len(voices)))
        if self.target == "sid" and any(t.dac[i] for i in range(min(len(t.dac), t.ndacs))):
            r.drop("the DAC tracks (a SID plays no samples)")
        if self.target == "nes" and t.ndacs > 1 and any(t.dac[i] for i in range(1, min(len(t.dac), t.ndacs))):
            r.drop("DAC tracks D2 and up (the NES has one DPCM channel)")
        if t.echo or t.reverb:
            r.drop("echo and reverb")
        if t.detune:
            r.drop("detune")
        allnotes = [n for v in voices for n in v]
        if any(n.slide or n.inst.slide for n in allnotes):
            r.drop("slides into notes")
        if any(n.pan not in (None, 0) for n in allnotes) or any(n.inst.pan for n in allnotes):
            r.drop("pan (the %s is mono)" % ("NES" if self.target == "nes" else "SID"))
        if self.target == "nes" and any(n.inst.filt or n.inst.fs_rate for n in allnotes):
            r.drop("the filter and its sweep (the NES has none)")
        if any(e[1] == "level" for e in dacs):
            r.drop("DAC level changes")

        g = self.grid(voices, dacs)
        tempos = self.tempo_map()
        total = max(1, -(-t.length // g))
        loop_row = 0 if t.once else min(total - 1, int(round(t.loop_tick / g)))
        if not t.once and loop_row * g != t.loop_tick:
            r.approx("the loop point is moved to the nearest row")
        segs, starts = self.speeds(g, total, tempos, loop_row)

        # ---- the speed effects, on the first channel
        fx0 = {}
        grooves = []
        if len(segs) > 1 or len(segs[0][2]) > 1:
            for _, _, sp in segs:
                if sp not in grooves:
                    grooves.append(sp)
            if len(grooves) == 1 and len(grooves[0]) == 1:
                grooves = []
        for row, _bpm, sp in segs:
            if row == 0 and (t.once or loop_row > 0):
                continue                                       # the song starts with its first speed pattern
            if row == 0 and len(segs) == 1 and len(sp) == 1:
                continue
            fx0.setdefault(row, []).append((0x09, grooves.index(sp) if grooves else sp[0]))

        # ---- the channels' cells
        chans = []
        if self.target == "nes":
            for v in range(4):
                chans.append(self.melodic_nes(v, voices[v], g, total, starts))
            chans.append(self.dpcm_cells(dacs, g, total))
        else:
            for v in range(3):
                chans.append(self.melodic_sid(voices[v], g, total))
        if not t.once:
            for ch in chans[:len(voices)]:
                self.cut_at_loop(ch, loop_row)

        # ---- patterns
        plen = self.pattern_length(max(1, t.bar_ticks // g), total)
        npat = -(-total // plen)
        if npat > MAX_ORDERS:
            total = MAX_ORDERS * plen
            npat = MAX_ORDERS
            r.drop("the end of the song: a Furnace song holds %d orders of %d rows, which is %d rows" % (MAX_ORDERS, plen, total))
        last = total - 1
        if t.once:
            fx0.setdefault(last, []).append((0xFF, 0))
        else:
            fx0.setdefault(last, []).append((0x0B, loop_row // plen))
            if loop_row % plen:
                fx0[last].append((0x0D, loop_row % plen))
        ncols = [1] * len(chans)
        ncols[0] = max(1, max((len(v) for v in fx0.values()), default=1))
        fx = [dict() for _ in chans]
        fx[0] = fx0
        patterns, orders = [], []
        for c, cells in enumerate(chans):
            seen, uniq, order = {}, [], []
            for k in range(npat):
                rows = []
                for local in range(plen):
                    row = k * plen + local
                    rows.append((cells.get(row) if row < total else None, tuple(fx[c].get(row, ())) if row < total else ()))
                key = tuple(rows)
                if key not in seen:
                    if len(uniq) >= MAX_PATTERNS:
                        r.drop("channel %d: more than %d different patterns (its later patterns repeat the first one)" % (c + 1, MAX_PATTERNS))
                        seen[key] = 0
                    else:
                        seen[key] = len(uniq)
                        uniq.append(key)
                order.append(seen[key])
            patterns.append(uniq)
            orders.append(order)
        first = segs[0][2]
        r.note("written as Furnace format version %d for %s, %s timing (%g Hz), %s, %d row%s a pattern, %d order%s" % (
            VERSION, CHIP_NAMES[self.target], "PAL" if self.pal else "NTSC", self.hz,
            ("a %d-speed groove" % len(first)) if len(first) > 1 else "speed %d" % first[0], plen, "" if plen == 1 else "s", npat, "" if npat == 1 else "s"))
        if len(segs) > 1:
            r.note("the tempo changes %d time%s (09xx on the first channel selects the speed pattern)" % (len(segs) - 1, "" if len(segs) == 2 else "s"))
        if t.once:
            r.note("it ends with a stop song effect (FFxx) on the last row: the notes' release after it is not played")
        r.note("%d instrument%s, %d sample%s" % (len(self.insts), "" if len(self.insts) == 1 else "s", len(self.samples), "" if len(self.samples) == 1 else "s"))
        if self.target == "nes":
            r.note("the NES's %s clock (%s)" % ("PAL" if self.pal else "NTSC", "1.662607 MHz" if self.pal else "1.789773 MHz"))
        if self.pal and self.samples:
            r.approx("DPCM rates in chipsynth are the NTSC ones; a PAL NES plays each rate a little slower")
        return self.assemble(patterns, orders, ncols, plen, segs, grooves), r

    def speeds(self, g, total, tempos, loop_row):
        """([(first row, bpm, speed pattern)], frame each row starts at): the rows a tempo change or the loop point starts."""
        t, r = self.t, self.r
        bounds = {0}
        for tick, _b in tempos[1:]:
            bounds.add(min(total - 1, int(round(tick / g))))
        if not t.once:
            bounds.add(loop_row)
        segs = []
        for row in sorted(bounds):
            bpm = [b for tk, b in tempos if tk <= row * g][-1]
            segs.append((row, bpm, self.groove(g * 60.0 * self.hz / (bpm * 96.0))))
        if len({tuple(s[2]) for s in segs}) == 1 and len(segs[0][2]) == 1:
            segs = segs[:1]                                    # one speed all through: nothing to switch
        if min(g * 60.0 * self.hz / (b * 96.0) for _, b, _ in segs) < 1.0:
            r.approx("the tempo is faster than one frame a row: it is played at one frame a row")
        starts = [0]
        for k, (row, _bpm, sp) in enumerate(segs):
            end = segs[k + 1][0] if k + 1 < len(segs) else total
            for i in range(row, end):
                starts.append(starts[-1] + sp[(i - row) % len(sp)])

        def secs_at(tick):
            s, pt, pb = 0.0, 0, tempos[0][1]
            for tk, b in tempos[1:]:
                if tk >= tick:
                    break
                s += (tk - pt) * 60.0 / (pb * 96.0)
                pt, pb = tk, b
            return s + (tick - pt) * 60.0 / (pb * 96.0)
        worst = max(abs(starts[row] - secs_at(row * g) * self.hz) for row in range(0, total + 1, max(1, total // 500)))
        if worst > 1.0:
            r.approx("the row lengths are whole frames (speed patterns of at most 16): the song's timing is off by up to %.0f ms" % (worst / self.hz * 1000))
        return segs, starts

    def pattern_length(self, bar_rows, total):
        base = bar_rows if bar_rows <= MAX_ROWS else 64
        multiples = [base * k for k in range(1, MAX_ROWS // base + 1)]
        short = [p for p in multiples if 16 <= p <= 64]
        order = short + [p for p in multiples if p not in short and p >= 16] + [p for p in multiples if p < 16]
        fits = [p for p in order if -(-total // p) <= MAX_ORDERS]
        return fits[0] if fits else (order[-1] if order else MAX_ROWS)

    def cut_at_loop(self, cells, loop_row):
        """A voice still sounding at the end of a looping song is stopped on the loop's first row, unless something starts there
        or it already sounds through it."""
        if not cells:
            return
        rows = sorted(cells)
        last = cells[rows[-1]]
        if last[0] in (NOTE_OFF, NOTE_RELEASE) or loop_row in cells:
            return
        before = [k for k in rows if k < loop_row]
        if before and cells[before[-1]][0] not in (NOTE_OFF, NOTE_RELEASE):
            return
        cells[loop_row] = (NOTE_OFF, None, None)

    # ------------------------------------------------------------------ voices
    def melodic_nes(self, v, notes, g, total, starts):
        kind = ("pulse", "pulse", "tri", "noise")[v]
        items = []
        for n in notes:
            if n.inst.wave != kind:
                self.say("V%d plays a %s wave; the %s channel plays it as its own sound" % (v + 1, n.inst.wave, CHANNELS["nes"][v]))
            items.append((int(round(n.start / g)), int(round((n.start + n.dur) / g)), n))
        placed = self.place(items, total)
        cells = {}
        low = 0
        for i, (s, e, n) in enumerate(placed):
            nxt = placed[i + 1][0] if i + 1 < len(placed) else None
            released = nxt != e and e < total
            idx = self.nes_instrument(n.inst, kind, (starts[e] - starts[s]) if released else None)
            if v == 3:
                low4 = min(range(16), key=lambda k: (abs(noise_pitch(k, self.pal) - n.pitch), k))
                note = NOISE_BASE + low4
            else:
                note = C0 + max(0, min(119, n.pitch + (PULSE_SHIFT if v < 2 else 0)))
                low += self.below_range(v, n.pitch)
            cells[s] = (note, idx, None if kind == "tri" else max(0, min(15, n.vol)))
            if released:
                cells[e] = (NOTE_OFF if kind == "tri" else NOTE_RELEASE, None, None)
        if low:
            self.say("%d note%s on %s lie below the NES's lowest %s note and play at that pitch" % (
                low, "" if low == 1 else "s", CHANNELS["nes"][v], "triangle" if v == 2 else "pulse"))
        return cells

    def below_range(self, v, pitch):
        clock = 1662607.0 if self.pal else 1789773.0
        period = clock / ((32 if v == 2 else 16) * 440.0 * 2 ** ((pitch - 57) / 12.0)) - 1
        return 1 if period > 2047.5 else 0

    def melodic_sid(self, notes, g, total):
        items = [(int(round(n.start / g)), int(round((n.start + n.dur) / g)), n) for n in notes]
        placed = self.place(items, total)
        cells = {}
        moved = 0
        for i, (s, e, n) in enumerate(placed):
            idx = self.sid_instrument(n.inst)
            pitch = n.pitch
            if pitch > SID_TOP:
                moved += 1
                pitch -= 12 * -(-(pitch - SID_TOP) // 12)
            cells[s] = (C0 + max(0, pitch), idx, None)
            nxt = placed[i + 1][0] if i + 1 < len(placed) else None
            if nxt != e and e < total:
                cells[e] = (NOTE_RELEASE, None, None)
        if moved:
            self.say("%d note%s above the SID's highest note (B-7) were moved down by octaves" % (moved, "" if moved == 1 else "s"))
        if len({n.vol * n.inst.vol for n in notes}) > 1:
            self.r.drop("the different loudness of notes and instruments (the SID's volume is one setting for all three voices; every note is as loud as its sustain level)")
        return cells

    def dpcm_cells(self, events, g, total):
        plays = [e for e in events if e[1] == "play"]
        cells = {}
        if not plays:
            return cells
        items = []
        for e in plays:
            k = self.dpcm_key(e[2])
            if k is not None:
                items.append((int(round(e[0] / g)), int(round(e[0] / g)) + 1, k))
        placed = self.place(items, total, "sample")
        for s, _, k in placed:
            cells[s] = (C0 + k, "dpcm", None)
        for e in events:
            if e[1] == "stop":
                row = int(round(e[0] / g))
                if row < total and row not in cells:
                    cells[row] = (NOTE_OFF, None, None)
        return cells

    # ------------------------------------------------------------------ the file
    def assemble(self, patterns, orders, ncols, plen, segs, grooves):
        t, r = self.t, self.r
        nch = self.nch
        out = _Buf()
        out.raw(MAGIC).u16(VERSION).u16(0).u32(32).u32(0).u32(0)
        has_dpcm = self.target == "nes" and bool(self.samples)
        ins_list = list(self.insts)
        if has_dpcm:
            dpcm_ins = len(ins_list)
            ins_list.append(_Ins("DPCM", "dpcm", [], None, {k: (smp, rate) for k, smp, rate in self.sample_ix.values()}))
        else:
            dpcm_ins = None
        # ---- INFO
        info = _Buf()
        sp = segs[0][2]
        info.u8(0).u8(sp[0]).u8(sp[1] if len(sp) > 1 else sp[0]).u8(1).f32(self.hz)
        info.u16(plen).u16(len(orders[0])).u8(4).u8(16)
        info.u16(len(ins_list)).u16(0).u16(len(self.samples)).u32(sum(len(p) for p in patterns))
        info.raw(bytes([CHIP_IDS[self.target]]) + bytes(31))
        info.raw(bytes([64]) + bytes(31)).raw(bytes(32))                 # chip volume, panning
        flag_pos = info.tell()
        info.raw(bytes(128))                                             # the chip flag pointers (patched below)
        info.s(_text(self.name or t.title or "", 255)).s(_text(t.author, 255)).f32(440.0)
        info.raw(bytes([0, 2, 2, 1, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1]))        # compatibility flags, as a new song has
        ptr_pos = info.tell()
        info.raw(bytes(4 * (len(ins_list) + len(self.samples) + sum(len(p) for p in patterns))))
        for c in range(nch):
            info.raw(bytes(orders[c]))
        for c in range(nch):
            info.u8(ncols[c])
        info.raw(bytes([3] * nch)).raw(bytes(nch))                       # shown, not collapsed
        for _ in range(nch * 2):
            info.s("")
        info.s(_text(t.comment, 255)).f32(1.0)
        info.raw(bytes([0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 0, 0, 1, 4, 0, 0, 1, 1, 0, 0, 0, 0, 2, 0, 1, 0, 0, 0]))    # extended compatibility flags
        info.u16(150).u16(150)                                           # virtual tempo
        info.s("").s("").u8(0).raw(bytes(3))                             # first subsong name, notes, no more subsongs
        for _ in range(6):
            info.s("")
        info.f32(1.0).f32(0.0).f32(0.0)                                  # chip volume, panning, front / rear
        info.u32(0).u8(1)                                                # no patchbay connections, automatic
        info.raw(bytes(8))                                               # more compatibility flags
        info.u8(len(sp)).raw(bytes(sp) + bytes(16 - len(sp)))            # the first speed pattern
        info.u8(len(grooves))
        for gr in grooves:
            info.u8(len(gr)).raw(bytes(gr) + bytes(16 - len(gr)))
        adir_pos = info.tell()
        info.raw(bytes(12))
        body = bytes(info.b)
        out.raw(_block(b"INFO", body))
        info_start = 32
        base = info_start + 8
        # ---- blocks after INFO: chip flags, asset directories, instruments, samples, patterns
        tail = bytearray()
        pos = lambda: 32 + 8 + len(body) + len(tail)
        flags_ptr = 0
        if self.pal:
            flags_ptr = pos()
            tail += _block(b"FLAG", b"clockSel=1\n\0")
        adir = []
        for n in (len(ins_list), 0, len(self.samples)):
            adir.append(pos())
            d = _Buf().u32(1 if n else 0)
            if n:
                d.s("").u16(n).raw(bytes(range(n)))
            tail += _block(b"ADIR", d.b)
        ins_ptr = []
        for i in ins_list:
            ins_ptr.append(pos())
            tail += self.instrument_block(i)
        smp_ptr = []
        for nm, data, rate in self.samples:
            smp_ptr.append(pos())
            tail += self.sample_block(nm, data, rate)
        pat_ptr = []
        for c, uniq in enumerate(patterns):
            for ix, key in enumerate(uniq):
                pat_ptr.append(pos())
                tail += self.pattern_block(c, ix, key, ncols[c])
        # ---- the pointers
        fixed = bytearray(body)
        p = ptr_pos
        for ptr in ins_ptr + smp_ptr + pat_ptr:
            fixed[p:p + 4] = struct.pack("<I", ptr)
            p += 4
        if flags_ptr:
            fixed[flag_pos:flag_pos + 4] = struct.pack("<I", flags_ptr)
        for k in range(3):
            fixed[adir_pos + 4 * k:adir_pos + 4 * k + 4] = struct.pack("<I", adir[k])
        return bytes(out.b[:32]) + _block(b"INFO", fixed) + bytes(tail)

    def instrument_block(self, ins):
        b = _Buf()
        b.u16(VERSION).u16(INS_C64 if ins.kind == "sid" else INS_NES)
        b.raw(b"NA").u16(len(ins.name.encode("utf-8")) + 1).raw(ins.name.encode("utf-8") + b"\0")
        if ins.kind == "sid":
            s = ins.sid
            body = _Buf().u8(s["f1"]).u8(s["f2"]).u8(s["ad"]).u8(s["sr"]).u16(s["duty"]).u16(s["cut"] | s["res"] << 12).u8(0x10)
            b.raw(b"64").u16(body.tell()).raw(body.b)
        if ins.macros:
            body = _Buf().u16(8)
            for m in sorted(ins.macros, key=lambda m: m.code):
                body.raw(m.pack())
            body.u8(255)
            b.raw(b"MA").u16(body.tell()).raw(body.b)
        if ins.kind == "dpcm":
            k = ins.sample_map
            sm = _Buf().u16(0xFFFF).u8(1 | 2).u8(0)
            ne = _Buf().u8(1)
            for note in range(NES_DPCM_MAP):
                got = k.get(note)
                sm.u16(note).u16(got[0] if got else 0xFFFF)
                ne.u8(got[1] if got else 255).u8(255)
            b.raw(b"SM").u16(sm.tell()).raw(sm.b)
            b.raw(b"NE").u16(ne.tell()).raw(ne.b)
        b.raw(b"EN").u16(0)
        return _block(b"INS2", b.b)

    def sample_block(self, name, data, rate):
        from ..synth import DPCM_RATES
        hz = int(round(DPCM_RATES[rate & 15]))
        b = _Buf().s(_text(name, 60) or "sample").u32((len(data) - 1) * 8).u32(hz).u32(hz).u8(1).u8(0).u8(0).u8(0)
        b.u32(0xFFFFFFFF).u32(0xFFFFFFFF)
        for _ in range(4):
            b.u32(0xFFFFFFFF)                                            # plays on every chip (a NES reads the first)
        b.raw(data)
        return _block(b"SMP2", b.b)

    def pattern_block(self, chan, index, rows, ncols):
        b = _Buf().u8(0).u8(chan).u16(index).s("")
        empty = 0
        for cell, effects in rows:
            mask = 0
            note = ins = vol = None
            if cell is not None:
                note, ins, vol = cell
                if note is not None:
                    mask |= 1
                if ins is not None:
                    mask |= 2
                if vol is not None:
                    mask |= 4
            fx = list(effects)[:ncols]
            emask = 0
            for k, _fx in enumerate(fx):
                emask |= 3 << (2 * k)
            if fx:
                mask |= 8 | 16
            if len(fx) > 1:
                mask |= 32
            if len(fx) > 4:
                mask |= 64
            if mask == 0:
                empty += 1
                if empty > 127:
                    b.u8(128 | (empty - 2))
                    empty = 0
                continue
            if empty > 1:
                b.u8(128 | (empty - 2))
            elif empty:
                b.u8(0)
            empty = 0
            b.u8(mask)
            if mask & 32:
                b.u8(emask & 255)
            if mask & 64:
                b.u8(emask >> 8)
            if mask & 1:
                b.u8(note)
            if mask & 2:
                b.u8(ins if ins != "dpcm" else self.dpcm_index())
            if mask & 4:
                b.u8(vol)
            for k, (code, val) in enumerate(fx):
                if k == 0:
                    b.u8(code).u8(val)
            for k, (code, val) in enumerate(fx):
                if 1 <= k < 4:
                    b.u8(code).u8(val)
            for k, (code, val) in enumerate(fx):
                if k >= 4:
                    b.u8(code).u8(val)
        if empty > 1:
            b.u8(128 | (empty - 2))
        elif empty:
            b.u8(0)
        b.u8(255)
        return _block(b"PATN", b.b)

    def dpcm_index(self):
        return len(self.insts)
