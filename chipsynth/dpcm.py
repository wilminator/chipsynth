# Copyright (C) 1994, 2026 Michael Allen Wilmes
"""DAC samples (see DESIGN-MUSIC.md): WAV -> NES-style 1-bit delta samples, and the .dpcm files they are kept in.

A Sample is a name plus a DPCM rate (0-15, the NES table in synth.DPCM_RATES) and one or two tracks (mono, or the left
and right channels of a stereo WAV).  A track is a list of parts of at most MAX_PART bytes each: a longer recording is
cut into parts that the DAC plays back to back without a join.

    s = convert(samples, wav_rate, rate=12)       # float array (n,) or (n, 2) -> Sample
    save(path, s);  s = load(path)

File layout (little endian): b"BDPC", version 1, rate, tracks, then per track: parts (u16) and per part: size (u16) + bytes.
"""
import struct
import wave
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

from .synth import DPCM_RATES

MAGIC = b"BDPC"
MAX_PART = 4081               # the NES sample buffer: 16 * 255 + 1 bytes
MAX_PARTS = 64                # at most this many parts per track (about 20 s at DPCM rate 15)
MAX_BYTES = 1 << 20


class DpcmError(ValueError):
    pass


@dataclass
class Sample:
    name: str = ""
    rate: int = 15
    tracks: list = field(default_factory=lambda: [[]])      # [[part bytes, ...], ...] one list per channel

    @property
    def stereo(self):
        return len(self.tracks) > 1

    @property
    def size(self):
        return sum(len(p) for t in self.tracks for p in t)

    @property
    def seconds(self):
        """Playing time of the longest track."""
        bits = max((sum(len(p) for p in t) * 8 for t in self.tracks), default=0)
        return bits / DPCM_RATES[self.rate & 15]


# ------------------------------------------------------------------ WAV in
def read_wav(path):
    """(sample rate, float32 array of shape (n, channels) in -1..1) from a PCM WAV file (8, 16, 24 or 32 bit)."""
    try:
        with wave.open(str(path), "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError, OSError) as e:
        raise DpcmError("can't read %s as a PCM WAV file (%s)" % (Path(path).name, e))
    if ch not in (1, 2):
        raise DpcmError("only mono and stereo WAV files are supported (this has %d channels)" % ch)
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float32) / float(1 << 23)
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise DpcmError("unsupported WAV sample size")
    return rate, x.reshape(-1, ch)


# ------------------------------------------------------------------ conversion
def resample(x, src_rate, dst_rate):
    """x (1-D float) from src_rate to dst_rate Hz (polyphase, so downsampling is filtered)."""
    if abs(src_rate - dst_rate) < 1e-6 or not len(x):
        return np.asarray(x, dtype=np.float64)
    from scipy.signal import resample_poly
    f = Fraction(dst_rate / src_rate).limit_denominator(2000)
    return resample_poly(np.asarray(x, dtype=np.float64), f.numerator, f.denominator)


def delta_encode(x):
    """Floats in -1..1 (already at the DPCM rate) -> bytes of 1-bit deltas: the 7-bit level starts at 64 and each bit
    moves it up or down by 2 towards the signal (the NES DMC).  The tail is padded with alternating bits."""
    target = 64.0 + np.clip(x, -1.0, 1.0) * 63.0
    bits = np.empty((len(target) + 7) // 8 * 8, dtype=np.uint8)
    lvl = 64
    tl = target.tolist()
    for i, t in enumerate(tl):
        if lvl < t:
            bits[i] = 1
            if lvl < 126:
                lvl += 2
        else:
            bits[i] = 0
            if lvl > 1:
                lvl -= 2
    for j in range(len(tl), len(bits)):
        bits[j] = j & 1
    return np.packbits(bits, bitorder="little").tobytes()


def split_parts(data, max_part=MAX_PART):
    if not data:
        return [b"\x55"]                               # silence: 01010101
    return [data[i:i + max_part] for i in range(0, len(data), max_part)]


def convert(x, src_rate, rate=15, name="", stereo=False, normalise=True, fit=False, max_bytes=MAX_BYTES):
    """Float samples (n,) or (n, 2) at src_rate Hz -> Sample at DPCM rate index `rate`.
    stereo=True keeps two tracks (left, right) when the input has two channels; otherwise the channels are mixed.
    A delta sample can only move 2/64 of the range per step, so a loud sample is distorted by slope overload (as on the
    NES); fit=True turns the level down until the steepest 1% of the slopes fit, giving a cleaner but quieter sample."""
    if not 0 <= rate <= 15:
        raise DpcmError("rate is 0 to 15")
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    chans = [x[:, 0], x[:, 1]] if (stereo and x.shape[1] > 1) else [x.mean(axis=1)]
    dst = DPCM_RATES[rate]
    chans = [resample(c, src_rate, dst) for c in chans]
    if normalise:
        peak = max((float(np.abs(c).max()) for c in chans if len(c)), default=0.0)
        if peak > 0:
            chans = [c / peak * 0.98 for c in chans]
    if fit:
        steep = max((float(np.percentile(np.abs(np.diff(c)), 99)) for c in chans if len(c) > 1), default=0.0)
        if steep > 2.0 / 63.0 / 1.0 * 0.5:
            chans = [c * (2.0 / 63.0 * 0.5 / steep) for c in chans]
    tracks = []
    for c in chans:
        data = delta_encode(c)
        tracks.append(split_parts(data))
    s = Sample(name, rate, tracks)
    if s.size > max_bytes or any(len(t) > MAX_PARTS for t in tracks):
        raise DpcmError("that sample is too long (%d bytes; the most is %d parts of %d): use a lower rate "
                        "number or a shorter recording" % (s.size, MAX_PARTS, MAX_PART))
    return s


def decode_preview(sample, track=0):
    """The sample as float -1..1 at its DPCM rate (for a preview or a test)."""
    from .synth import dpcm_decode
    out, last = [], 64
    for part in sample.tracks[track]:
        lv = dpcm_decode(part, start_level=last)
        out.append((lv - 64.0) / 64.0)
        last = int(lv[-1]) if len(lv) else last
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


# ------------------------------------------------------------------ files
def dumps(s):
    out = [MAGIC, struct.pack("<BBB", 1, s.rate & 15, len(s.tracks))]
    for t in s.tracks:
        out.append(struct.pack("<H", len(t)))
        for p in t:
            out.append(struct.pack("<H", len(p)))
            out.append(bytes(p))
    return b"".join(out)


def loads(data, name=""):
    try:
        if data[:4] != MAGIC:
            raise DpcmError("not a Blobs sample file")
        ver, rate, ntr = struct.unpack_from("<BBB", data, 4)
        if ver != 1 or rate > 15 or ntr not in (1, 2):
            raise DpcmError("unsupported sample file")
        at, tracks = 7, []
        for _ in range(ntr):
            (np_,) = struct.unpack_from("<H", data, at)
            at += 2
            parts = []
            for _ in range(np_):
                (ln,) = struct.unpack_from("<H", data, at)
                at += 2
                if not 0 < ln <= MAX_PART or at + ln > len(data):
                    raise DpcmError("damaged sample file")
                parts.append(bytes(data[at:at + ln]))
                at += ln
            if not parts:
                raise DpcmError("damaged sample file")
            tracks.append(parts)
        return Sample(name, rate, tracks)
    except struct.error:
        raise DpcmError("damaged sample file")


def save(path, sample):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(dumps(sample))


def load(path):
    p = Path(path)
    try:
        return loads(p.read_bytes(), p.stem)
    except OSError as e:
        raise DpcmError("can't read %s (%s)" % (p.name, e))


# ------------------------------------------------------------------ command line
def demo_wavs(folder):
    """Writes kick.wav, snare.wav and zap.wav (44.1 kHz mono) into folder, to try the Sample import tool with."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    r = 44100
    t = np.arange(int(0.35 * r)) / r
    kick = np.sin(2 * np.pi * (45 * t + 90 * (1 - np.exp(-t * 25)) / 25)) * np.exp(-t * 12)
    rng = np.random.default_rng(7)
    t2 = np.arange(int(0.25 * r)) / r
    snare = (rng.standard_normal(len(t2)) * 0.7 + np.sin(2 * np.pi * 190 * t2) * 0.5) * np.exp(-t2 * 16)
    t3 = np.arange(int(0.4 * r)) / r
    zap = np.sin(2 * np.pi * (1800 * t3 - 3500 * t3 ** 2 / 2)) * np.exp(-t3 * 3)
    for name, x in (("kick", kick), ("snare", snare), ("zap", zap)):
        pcm = (np.clip(x / max(1e-9, np.abs(x).max()), -1, 1) * 30000).astype("<i2")
        with wave.open(str(folder / (name + ".wav")), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(r)
            w.writeframes(pcm.tobytes())
    return ["kick", "snare", "zap"]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m blobs.dpcm", description="WAV -> Blobs DAC samples (music/samples)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="write kick/snare/zap demo WAVs into GAMEDIR/music/wav")
    d.add_argument("gamedir")
    c = sub.add_parser("convert", help="convert a WAV into GAMEDIR/music/samples/NAME.dpcm")
    c.add_argument("wav")
    c.add_argument("gamedir")
    c.add_argument("--name")
    c.add_argument("--rate", type=int, default=12)
    c.add_argument("--stereo", action="store_true", help="keep left and right as two tracks")
    c.add_argument("--fit", action="store_true", help="turn it down until it follows cleanly")
    a = ap.parse_args(argv)
    root = Path(a.gamedir)
    if a.cmd == "demo":
        for n in demo_wavs(root / "music" / "wav"):
            print("wrote music/wav/%s.wav" % n)
        return 0
    rate, x = read_wav(a.wav)
    name = a.name or "".join(ch for ch in Path(a.wav).stem if ch.isalpha() or ch == "_") or "sample"
    s = convert(x, rate, a.rate, name, a.stereo, fit=a.fit)
    save(root / "music" / "samples" / (name + ".dpcm"), s)
    print("music/samples/%s.dpcm: %d bytes, %.1f s, rate %d, %d part(s)" % (name, s.size, s.seconds, s.rate,
                                                                              len(s.tracks[0])))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
