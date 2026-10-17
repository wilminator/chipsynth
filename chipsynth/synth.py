# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The chiptune mixer core (see DESIGN-MUSIC.md): 4 voices and 2 DACs rendered to stereo.

Voices: five waveforms (pulse with a width, sawtooth, triangle, sine, noise), a volume, an ADSR envelope
and a pan each.  Pulse and sawtooth are band-limited (PolyBLEP).  Noise is a shift-register sequence stepped
at 8x the note frequency, so a note is a noise pitch.  The DACs play NES-style 1-bit delta samples or hold a
7-bit level.  Nothing here knows about MML; callers schedule events (note on / off, pitch, volume, pan,
DAC start / level / stop) at sample times and then ask for chunks:

    s = Synth()
    s.schedule(0, "on", 0, freq=440.0, inst=Instrument(wave="sine"))
    s.schedule(22050, "off", 0)
    chunk = s.render(4096)          # float32 array (4096, 2); call again for the next chunk

Rendering in chunks gives exactly the same samples as one long render.
"""
import heapq
import itertools
from dataclasses import dataclass

import numpy as np
from scipy.signal import lfilter

RATE = 44100
VOICES, DACS = 4, 2
WAVES = ("pulse", "saw", "tri", "sine", "noise")

# SID attack times and (3x as long) decay / release times, in ms, for the rates 0-15
ATTACK_MS = (2, 8, 16, 24, 38, 56, 68, 80, 100, 250, 500, 800, 1000, 3000, 5000, 8000)
DECAY_MS = (6, 24, 48, 72, 114, 168, 204, 240, 300, 750, 1500, 2400, 3000, 9000, 15000, 24000)
DPCM_RATES = (4181.71, 4709.93, 5264.04, 5593.04, 6257.95, 7046.35, 7919.35, 8363.42,
              9419.86, 11186.1, 12604.0, 13982.0, 16885.0, 21306.0, 24858.0, 33143.0)   # NES NTSC
NOISE_CLOCK = 8           # noise steps per cycle of the note's frequency
VOICE_GAIN = 0.2          # headroom for four voices
MASK = (1 << 32) - 1      # phases are exact 32-bit integers, so chunking never changes a sample
FRAC = 24                 # fractional bits of the noise and DAC positions


def note_freq(note):
    """Frequency of a note number counted in semitones from C0 (A4 = 57 = 440 Hz)."""
    return 440.0 * 2.0 ** ((note - 57) / 12.0)


@dataclass
class Instrument:
    wave: str = "pulse"
    pw: int = 50              # pulse width, percent (1-99)
    a: int = 0                # attack, decay and release rates 0-15
    d: int = 8
    s: int = 10               # sustain level 0-15
    r: int = 4
    vol: int = 15             # 0-15
    pan: int = 0              # -8..8
    nmode: str = "long"       # noise: "long" (white) or "short" (the NES metallic sequence)
    # expression effects (all off by default)
    vib_rate: int = 0         # vibrato rate 0-15 (0 = off; 15 is about 12 Hz)
    vib_depth: int = 0        # depth 0-15 in 1/16 semitone
    slide: int = 0            # glide into each note, in 1/48 of a quarter note (0 = off)
    arp: tuple = ()           # up to 8 semitone offsets cycled through
    arprate: int = 8          # arpeggio steps per beat
    pws_from: int = 0         # pulse-width sweep between these percents (both 0 = off)
    pws_to: int = 0
    pws_rate: int = 0         # 0-15, 0 = off
    filt: str = ""            # "" (off), "lp", "bp" or "hp"
    filt_cut: int = 8         # cutoff 0-15 (about 100 Hz to 8 kHz, exponential)
    filt_res: int = 0         # resonance 0-15
    fs_from: int = 0          # cutoff sweep between these two cutoffs ...
    fs_to: int = 0
    fs_rate: int = 0          # ... at rate x 0.5 Hz (0 = off)
    send: int = 15            # level sent to the echo / reverb bus, 0-15

    @property
    def has_fx(self):
        return bool(self.vib_rate and self.vib_depth or self.arp
                    or self.pws_rate and self.pws_from and self.pws_to and self.wave == "pulse")


FILTER_BLOCK = 32         # filter coefficients are refreshed every this many samples of a note's age


def filter_cutoff_hz(cut):
    return 100.0 * 80.0 ** (min(15.0, max(0.0, cut)) / 15.0)


def biquad(mode, cut, res):
    """RBJ biquad coefficients (b, a) for a low / band / high pass at cutoff step `cut` and resonance `res`."""
    w0 = 2 * np.pi * filter_cutoff_hz(cut) / RATE
    q = 0.707 + res * 0.9
    c, alpha = np.cos(w0), np.sin(w0) / (2 * q)
    if mode == "lp":
        b = [(1 - c) / 2, 1 - c, (1 - c) / 2]
    elif mode == "hp":
        b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]
    else:
        b = [alpha, 0.0, -alpha]
    a0 = 1 + alpha
    return np.array(b) / a0, np.array([1.0, -2 * c / a0, (1 - alpha) / a0])


_NOISE = {}


def noise_table(mode):
    """The +-1 noise sequence: 32767 steps (long) or 93 steps (short), a 15-bit shift register."""
    if mode not in _NOISE:
        reg, out = 1, []
        tap = 6 if mode == "short" else 1
        for _ in range(93 if mode == "short" else 32767):
            out.append(1.0 if reg & 1 else -1.0)
            fb = (reg & 1) ^ ((reg >> tap) & 1)
            reg = (reg >> 1) | (fb << 14)
        _NOISE[mode] = np.array(out, dtype=np.float32)
    return _NOISE[mode]


def _blep(t, dt):
    """PolyBLEP step correction for phases t in [0, 1); dt (the phase step) may be a number or an array."""
    out = np.zeros_like(t)
    dt = np.broadcast_to(np.asarray(dt, dtype=np.float64), t.shape)
    m = t < dt
    x = t[m] / dt[m]
    out[m] = x + x - x * x - 1.0
    m = t > 1.0 - dt
    x = (t[m] - 1.0) / dt[m]
    out[m] = x * x + x + x + 1.0
    return out


def dpcm_decode(data, start_level=64):
    """NES delta samples: each bit (least significant first) moves a 7-bit level by +-2."""
    bits = np.unpackbits(np.frombuffer(bytes(data), dtype=np.uint8), bitorder="little")
    steps = np.where(bits == 1, 2, -2)
    lvl, out = start_level, np.empty(len(steps), dtype=np.float32)
    for i, d in enumerate(steps):                     # (clamping makes this sequential; samples are tiny)
        lvl = min(127, max(0, lvl + int(d)))
        out[i] = lvl
    return out


def pan_gains(pan):
    """Constant-power gains (left, right) for a pan of -8 (left) to 8 (right)."""
    a = (max(-8, min(8, pan)) + 8) / 16.0 * (np.pi / 2)
    return float(np.cos(a)), float(np.sin(a))


class _Voice:
    def __init__(self):
        self.inst = Instrument()
        self.freq = 440.0
        self.vol = 15
        self.pan = 0
        self.stage = "off"
        self.level = 0.0
        self.pos = 0
        self.length = 1
        self.start = 0.0
        self.phase = 0
        self.nphase = 0
        self.age = 0                # samples since the note began (the effects run on this)
        self.beat = RATE // 2       # samples in a quarter note (tempo-synced effects)
        self.slide_from = None      # frequency to glide from, and for how many samples
        self.slide_len = 0
        self.zi = np.zeros(2)       # filter state

    def gate_on(self):
        self.age = 0
        self.zi = np.zeros(2)
        self.stage, self.pos, self.start = "attack", 0, self.level
        self.length = max(1, int(ATTACK_MS[self.inst.a & 15] * RATE / 1000))

    def gate_off(self):
        if self.stage != "off":
            self.stage, self.pos, self.start = "release", 0, self.level
            self.length = max(1, int(DECAY_MS[self.inst.r & 15] * RATE / 1000))

    def _next_stage(self):
        if self.stage == "attack":
            self.stage, self.pos, self.start, self.level = "decay", 0, 1.0, 1.0
            self.length = max(1, int(DECAY_MS[self.inst.d & 15] * RATE / 1000))
        elif self.stage == "decay":
            self.stage, self.level = "sustain", (self.inst.s & 15) / 15.0
        elif self.stage == "release":
            self.stage, self.level = "off", 0.0

    def envelope(self, n):
        out = np.zeros(n, dtype=np.float32)
        i = 0
        while i < n:
            if self.stage == "off":
                break
            if self.stage == "sustain":
                out[i:] = self.level
                break
            k = min(n - i, self.length - self.pos)
            if k > 0:
                t = (self.pos + np.arange(1, k + 1)) / self.length
                if self.stage == "attack":
                    seg = self.start + (1.0 - self.start) * t
                elif self.stage == "decay":
                    sus = (self.inst.s & 15) / 15.0
                    seg = sus + (self.start - sus) * np.exp(-6.9 * t)
                else:
                    seg = self.start * np.exp(-6.9 * t)
                out[i:i + k] = seg
                self.pos += k
                i += k
                self.level = float(seg[-1])
            if self.pos >= self.length:
                self._next_stage()
        return out

    def skip(self, n):
        """Advance the oscillators n samples without making sound (they free-run while a voice is off, so
        the next note's phase never depends on how the render was cut into chunks)."""
        inc_int = int(round(min(0.49, self.freq / RATE) * 2 ** 32))
        self.phase = (self.phase + inc_int * n) & MASK
        if self.inst.wave == "noise":
            tab = noise_table(self.inst.nmode)
            step = int(round(NOISE_CLOCK * self.freq / RATE * (1 << FRAC)))
            self.nphase = (self.nphase + step * n) % (len(tab) << FRAC)

    def _freqs(self, n):
        """The frequency of each of the next n samples with vibrato, arpeggio and slide applied."""
        inst = self.inst
        t = self.age + np.arange(n, dtype=np.float64)
        semis = np.zeros(n, dtype=np.float64)
        if inst.arp:
            step = max(1.0, self.beat / max(1, inst.arprate))
            offs = np.array(inst.arp, dtype=np.float64)
            semis += offs[(t // step).astype(np.int64) % len(offs)]
        if inst.vib_rate and inst.vib_depth:
            hz = inst.vib_rate * 0.8
            semis += inst.vib_depth / 16.0 * np.sin(2 * np.pi * hz * t / RATE)
        f = self.freq * 2.0 ** (semis / 12.0)
        if self.slide_from and self.slide_len > 0:
            k = np.clip(t / self.slide_len, 0.0, 1.0)
            f = f * (self.slide_from / self.freq) ** (1.0 - k)
        return f

    def _pws(self, n):
        inst = self.inst
        t = self.age + np.arange(n, dtype=np.float64)
        hz = inst.pws_rate * 0.5
        ph = (hz * t / RATE) % 1.0
        tri = 1.0 - np.abs(2.0 * ph - 1.0)
        return (inst.pws_from + (inst.pws_to - inst.pws_from) * tri).clip(1, 99) / 100.0

    def filtered(self, sig, a0):
        """Run sig (the samples from note age a0 on) through the instrument's filter.  The coefficients change on
        absolute multiples of FILTER_BLOCK, so the sound does not depend on how the render is cut."""
        inst = self.inst
        out = np.empty_like(sig, dtype=np.float64)
        i, n = 0, len(sig)
        while i < n:
            t = a0 + i
            k = min(n - i, FILTER_BLOCK - t % FILTER_BLOCK)
            mid = (t // FILTER_BLOCK) * FILTER_BLOCK + FILTER_BLOCK // 2
            cut = inst.filt_cut
            if inst.fs_rate:
                ph = (inst.fs_rate * 0.5 * mid / RATE) % 1.0
                cut = inst.fs_from + (inst.fs_to - inst.fs_from) * (1.0 - abs(2.0 * ph - 1.0))
            b, a = biquad(inst.filt, cut, inst.filt_res)
            out[i:i + k], self.zi = lfilter(b, a, sig[i:i + k], zi=self.zi)
            i += k
        return out / (1.0 + 0.1 * inst.filt_res)         # a resonant peak is tamed so it does not clip

    def wave(self, n):
        inst = self.inst
        if inst.has_fx or (self.slide_from and self.slide_len > self.age):
            out = self._fx_wave(n)
            self.age += n
            return out
        self.age += n
        inc = min(0.49, self.freq / RATE)
        inc_int = int(round(inc * 2 ** 32))
        ph = ((np.uint64(self.phase) + np.uint64(inc_int) * np.arange(n, dtype=np.uint64))
              & np.uint64(MASK)).astype(np.float64) / 2 ** 32
        self.phase = (self.phase + inc_int * n) & MASK
        w = inst.wave
        if w == "sine":
            return np.sin(2 * np.pi * ph).astype(np.float32)
        if w == "tri":
            return (4 * np.abs(ph - 0.5) - 1).astype(np.float32)
        if w == "saw":
            return (2 * ph - 1 - _blep(ph, inc)).astype(np.float32)
        if w == "noise":
            tab = noise_table(inst.nmode)
            step = int(round(NOISE_CLOCK * self.freq / RATE * (1 << FRAC)))
            pos = np.int64(self.nphase) + np.int64(step) * np.arange(n, dtype=np.int64)
            self.nphase = (self.nphase + step * n) % (len(tab) << FRAC)
            return tab[(pos >> FRAC) % len(tab)]
        pw = max(1, min(99, inst.pw)) / 100.0
        naive = np.where(ph < pw, 1.0, -1.0)
        return (naive + _blep(ph, inc) - _blep((ph - pw) % 1.0, inc)).astype(np.float32)


    def _fx_wave(self, n):
        """wave() for a note with an effect running: the frequency (and pulse width) change sample by sample."""
        inst = self.inst
        f = self._freqs(n)
        inc = np.minimum(0.49, f / RATE)
        inc_int = np.round(inc * 2 ** 32).astype(np.uint64)
        ph = ((np.uint64(self.phase) + np.cumsum(inc_int, dtype=np.uint64) - inc_int) & np.uint64(MASK)).astype(np.float64) / 2 ** 32
        self.phase = int((np.uint64(self.phase) + inc_int.sum(dtype=np.uint64)) & np.uint64(MASK))
        w = inst.wave
        if w == "sine":
            return np.sin(2 * np.pi * ph).astype(np.float32)
        if w == "tri":
            return (4 * np.abs(ph - 0.5) - 1).astype(np.float32)
        if w == "saw":
            return (2 * ph - 1 - _blep(ph, inc)).astype(np.float32)
        if w == "noise":
            tab = noise_table(inst.nmode)
            step = np.round(NOISE_CLOCK * f / RATE * (1 << FRAC)).astype(np.int64)
            pos = np.int64(self.nphase) + np.cumsum(step) - step
            self.nphase = int((pos[-1] + step[-1]) % (len(tab) << FRAC))
            return tab[(pos >> FRAC) % len(tab)]
        if inst.pws_rate and inst.pws_from and inst.pws_to:
            pw = self._pws(n)
        else:
            pw = max(1, min(99, inst.pw)) / 100.0
        naive = np.where(ph < pw, 1.0, -1.0)
        return (naive + _blep(ph, inc) - _blep((ph - pw) % 1.0, inc)).astype(np.float32)


class _Dac:
    def __init__(self):
        self.levels = None        # decoded levels (0-127) of the part now playing, or None
        self.queue = []           # raw bytes of the parts still to come (a long sample plays as a chain, no join)
        self.pos = 0
        self.step = 1 << FRAC       # in 2^-24 samples of the sample per output sample
        self.hold = None          # a direct 7-bit level, or None
        self.vol = 15
        self.pan = 0

    def _next_part(self):
        """The part just played is finished: carry on into the next one from the level it ended on."""
        last = int(self.levels[-1]) if self.levels is not None and len(self.levels) else 64
        if self.queue:
            self.pos -= len(self.levels) << FRAC             # the fraction left over carries across the join
            self.levels = dpcm_decode(self.queue.pop(0), start_level=last)
        else:
            self.levels = None

    def render(self, n):
        if self.levels is not None:
            out = np.zeros(n, dtype=np.float32)
            done = 0
            while done < n and self.levels is not None:
                left = len(self.levels) << FRAC
                m = min(n - done, max(1, -(-(left - self.pos) // self.step)))
                idx = (np.int64(self.pos) + np.int64(self.step) * np.arange(m, dtype=np.int64)) >> FRAC
                ok = idx < len(self.levels)
                seg = out[done:done + m]
                seg[ok] = (self.levels[idx[ok]] - 64.0) / 64.0
                self.pos += self.step * m
                done += m
                if self.pos >= left:
                    self._next_part()
            return out
        if self.hold is not None:
            return np.full(n, (self.hold - 64.0) / 64.0, dtype=np.float32)
        return None


class Synth:
    def __init__(self):
        self.voices = [_Voice() for _ in range(VOICES)]
        self.dacs = [_Dac() for _ in range(DACS)]
        self.t = 0                       # samples rendered so far
        self._events = []
        self._seq = itertools.count()
        self.bus = None                  # a synthfx.Bus; the voices' `send` levels feed it

    def schedule(self, at, kind, target, **kw):
        """Queue an event at sample `at`.  Kinds: voices: on (freq, inst, vol), off, freq (freq),
        vol (vol), pan (pan); DACs: dac_start (data, rate, vol, pan), dac_level (level, vol, pan), dac_stop."""
        heapq.heappush(self._events, (int(at), next(self._seq), kind, target, kw))

    def _apply(self, kind, i, kw):
        if kind.startswith("dac"):
            d = self.dacs[i]
            if "vol" in kw:
                d.vol = kw["vol"]
            if "pan" in kw:
                d.pan = kw["pan"]
            if kind == "dac_start":
                parts = [kw["data"]] if isinstance(kw["data"], (bytes, bytearray)) else list(kw["data"])
                d.levels = dpcm_decode(parts[0])
                d.queue = [bytes(p) for p in parts[1:]]
                d.pos, d.hold = 0, None
                d.step = int(round(DPCM_RATES[kw.get("rate", 15) & 15] / RATE * (1 << FRAC)))
            elif kind == "dac_level":
                d.levels, d.queue, d.hold = None, [], kw["level"]
            elif kind == "dac_stop":
                d.levels, d.queue, d.hold = None, [], None
            return
        v = self.voices[i]
        if kind == "on":
            v.inst = kw.get("inst", v.inst)
            v.freq = kw.get("freq", v.freq)
            v.vol = kw.get("vol", 15)
            v.pan = v.inst.pan if "pan" not in kw else kw["pan"]
            v.beat = kw.get("beat", RATE // 2)
            v.slide_from = kw.get("slide_from")
            v.slide_len = kw.get("slide_len", 0)
            v.gate_on()
        elif kind == "off":
            v.gate_off()
        elif kind == "freq":
            v.freq = kw["freq"]
        elif kind == "vol":
            v.vol = kw["vol"]
        elif kind == "pan":
            v.pan = kw["pan"]

    def _mix(self, n, out, send=None):
        for v in self.voices:
            if v.stage == "off":
                v.skip(n)
                continue
            a0 = v.age
            sig = v.wave(n) * v.envelope(n) * (v.inst.vol / 15.0) * (v.vol / 15.0) * VOICE_GAIN
            if v.inst.filt:
                sig = v.filtered(sig, a0)
            gl, gr = pan_gains(v.pan)
            out[:, 0] += sig * gl * 1.4142
            out[:, 1] += sig * gr * 1.4142
            if send is not None and v.inst.send:
                k = v.inst.send / 15.0
                send[:, 0] += sig * gl * 1.4142 * k
                send[:, 1] += sig * gr * 1.4142 * k
        for d in self.dacs:
            sig = d.render(n)
            if sig is not None:
                gl, gr = pan_gains(d.pan)
                s = sig * (d.vol / 15.0) * VOICE_GAIN
                out[:, 0] += s * gl * 1.4142
                out[:, 1] += s * gr * 1.4142

    def render(self, n):
        """The next n samples as a float32 array of shape (n, 2)."""
        out = np.zeros((n, 2), dtype=np.float32)
        send = np.zeros((n, 2), dtype=np.float32) if self.bus is not None else None
        done, end = 0, self.t + n
        while done < n:
            while self._events and self._events[0][0] <= self.t:
                _, _, kind, target, kw = heapq.heappop(self._events)
                self._apply(kind, target, kw)
            nxt = self._events[0][0] if self._events else end
            seg = max(1, min(end, nxt) - self.t)
            self._mix(seg, out[done:done + seg], None if send is None else send[done:done + seg])
            done += seg
            self.t += seg
        if send is not None:
            out += self.bus.process(send)
        return out


def to_pcm16(buf):
    """float stereo samples -> interleaved little-endian 16-bit PCM bytes (clipped)."""
    return (np.clip(buf, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def demo(path="synth_demo.wav"):
    """Writes a few seconds that show each waveform, the envelope, pan, a DAC sample and the noise voice."""
    import wave
    s = Synth()
    t = 0
    notes = [("pulse", 25), ("saw", 50), ("tri", 50), ("sine", 50)]
    for k, (w, pw) in enumerate(notes):
        inst = Instrument(wave=w, pw=pw, a=1, d=8, s=9, r=7, pan=-6 + 4 * k)
        for j, semi in enumerate((0, 4, 7, 12)):
            at = t + int((k * 4 + j) * 0.22 * RATE)
            s.schedule(at, "on", k, freq=note_freq(48 + semi), inst=inst)
            s.schedule(at + int(0.18 * RATE), "off", k)
    start = int(3.8 * RATE)
    for j in range(8):
        s.schedule(start + j * int(0.15 * RATE), "on", 0, freq=note_freq(40 if j % 2 else 70),
                   inst=Instrument(wave="noise", a=0, d=5, s=0, r=3, vol=14))
    s.schedule(start, "dac_start", 0, data=bytes([0xF0, 0x0F] * 600), rate=12, pan=6)
    pcm = to_pcm16(s.render(int(5.5 * RATE)))
    with wave.open(path, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm)
    return path


if __name__ == "__main__":
    print("wrote", demo())
