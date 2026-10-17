# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
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


@dataclass
class ImportOptions:
    """WAV import options.  The defaults reproduce plain convert() output byte for byte.

    rate        DPCM rate index 0-15 (synth.DPCM_RATES), or None / "auto" for the highest rate whose result fits the
                size cap (MAX_PARTS parts per track, and max_bytes when set)
    channel     "mix" (average the channels), "left", "right", or "stereo" (two tracks, D1 + D2)
    normalize   scale the peak to 0.98 (the default); False keeps the WAV's own level
    trim_db     cut leading and trailing silence below this level (dBFS, e.g. -48); None keeps it
    fade_ms     linear fade-out over the last this-many milliseconds; 0 for none
    max_seconds cut the recording to at most this long (WAV time); None for no limit
    max_bytes   cut the result to at most this many bytes per track; None for no limit
    truncate    when the result still exceeds the DPCM size cap, cut it to fit instead of failing
    fit         the convert() loudness fit (cleaner, quieter)
    """
    rate: object = 12
    channel: str = "mix"
    normalize: bool = True
    trim_db: object = None
    fade_ms: float = 0.0
    max_seconds: object = None
    max_bytes: object = None
    truncate: bool = False
    fit: bool = False


CHANNELS = ("mix", "left", "right", "stereo")


def trim_silence(x, db=-48.0):
    """x (n, channels) without the leading and trailing frames quieter than db dBFS (at least one frame is kept)."""
    if not len(x):
        return x
    loud = np.nonzero(np.abs(x).max(axis=1) > 10.0 ** (db / 20.0))[0]
    if not len(loud):
        return x[:1]
    return x[loud[0]:loud[-1] + 1]


def fade_out(x, src_rate, ms):
    """x (n, channels) with a linear fade to silence over its last ms milliseconds."""
    k = min(len(x), int(round(src_rate * ms / 1000.0)))
    if k <= 0:
        return x
    x = np.array(x, dtype=np.float64)
    x[-k:] *= np.linspace(1.0, 0.0, k, endpoint=False).reshape(-1, 1) if x.ndim > 1 else np.linspace(1.0, 0.0, k, endpoint=False)
    return x


def size_limit(rate, max_bytes=None):
    """Most bytes per track a sample at this DPCM rate index may hold (the part cap, MAX_BYTES, and max_bytes)."""
    lim = min(MAX_PARTS * MAX_PART, MAX_BYTES)
    return min(lim, max_bytes) if max_bytes else lim


def auto_rate(seconds, max_bytes=None):
    """The highest DPCM rate index at which `seconds` of audio fits the size cap (0 when none does)."""
    for r in range(15, -1, -1):
        if int(seconds * DPCM_RATES[r] / 8) + 1 <= size_limit(r, max_bytes):
            return r
    return 0


def prepare(x, src_rate, opts=None):
    """Applies the import options that work on the WAV itself.  Returns (x, rate, stereo, notes): x is (n, 1) or
    (n, 2) floats ready for convert(); notes lists what was done, for the preview."""
    o = opts or ImportOptions()
    if o.channel not in CHANNELS:
        raise DpcmError("channel is one of %s" % ", ".join(CHANNELS))
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    notes = []
    stereo = False
    if x.shape[1] > 1:
        if o.channel == "left":
            x = x[:, :1]
        elif o.channel == "right":
            x = x[:, 1:2]
        elif o.channel == "stereo":
            stereo = True
        else:
            x = x.mean(axis=1, keepdims=True)
    n0 = len(x)
    if o.trim_db is not None:
        x = trim_silence(x, o.trim_db)
        if len(x) != n0:
            notes.append("trimmed %.3f s of silence" % ((n0 - len(x)) / src_rate))
    if o.max_seconds is not None:
        k = max(1, int(o.max_seconds * src_rate))
        if len(x) > k:
            x = x[:k]
            notes.append("cut to %.3f s" % (k / src_rate))
    rate = o.rate
    if rate is None or rate == "auto":
        rate = auto_rate(len(x) / src_rate, o.max_bytes)
        notes.append("auto rate %d" % rate)
    rate = int(rate)
    if not 0 <= rate <= 15:
        raise DpcmError("rate is 0 to 15")
    cap = None
    if o.max_bytes:
        cap = o.max_bytes
    if o.truncate:
        cap = size_limit(rate, cap)
    if cap:
        k = max(1, int(cap * 8 / DPCM_RATES[rate] * src_rate))
        if len(x) > k:
            x = x[:k]
            notes.append("cut to %d bytes" % cap)
    if o.fade_ms > 0:
        x = fade_out(x, src_rate, o.fade_ms)
        notes.append("%g ms fade-out" % o.fade_ms)
    return x, rate, stereo, notes


def convert_wav(x, src_rate, opts=None, name=""):
    """Float samples -> (Sample, notes) under ImportOptions.  Same as convert() with the default options."""
    o = opts or ImportOptions()
    x, rate, stereo, notes = prepare(x, src_rate, o)
    s = convert(x, src_rate, rate, name, stereo, normalise=o.normalize, fit=o.fit,
                max_bytes=size_limit(rate, o.max_bytes))
    return s, notes


def preview(x, src_rate, opts=None, name=""):
    """What an import would write, without writing: a dict with size, seconds, rate, hz, parts, tracks, notes and
    error (None, or why it can't be made).  Size and duration are exact (the conversion is run)."""
    try:
        s, notes = convert_wav(x, src_rate, opts, name)
    except DpcmError as e:
        return {"error": str(e), "size": 0, "seconds": 0.0, "rate": None, "hz": None, "parts": 0, "tracks": 0,
                "notes": [], "sample": None}
    return {"error": None, "size": s.size, "seconds": s.seconds, "rate": s.rate, "hz": DPCM_RATES[s.rate],
            "parts": len(s.tracks[0]), "tracks": len(s.tracks), "notes": notes, "sample": s}


def format_preview(info, wav_rate=None, wav_seconds=None):
    """One or two lines of text for a preview() result."""
    if info["error"]:
        return "cannot import: %s" % info["error"]
    head = "%d bytes, %.2f s, rate %d (%d Hz), %d part(s)%s" % (
        info["size"], info["seconds"], info["rate"], info["hz"], info["parts"],
        ", 2 tracks" if info["tracks"] > 1 else "")
    if wav_rate and wav_seconds is not None:
        head = "%d Hz %.2f s WAV -> " % (wav_rate, wav_seconds) + head
    return head + ("; " + ", ".join(info["notes"]) if info["notes"] else "")


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
    ap = argparse.ArgumentParser(prog="python -m chipsynth.dpcm", description="WAV -> Blobs DAC samples (music/samples)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="write kick/snare/zap demo WAVs into GAMEDIR/music/wav")
    d.add_argument("gamedir")
    c = sub.add_parser("convert", help="convert a WAV into GAMEDIR/music/samples/NAME.dpcm")
    c.add_argument("wav")
    c.add_argument("gamedir")
    c.add_argument("--name")
    c.add_argument("--rate", default="12", help="DPCM rate 0-15 (default 12), or auto for the highest that fits")
    c.add_argument("--stereo", action="store_true", help="keep left and right as two tracks (same as --channel stereo)")
    c.add_argument("--channel", choices=CHANNELS, help="mix (default), left, right or stereo")
    c.add_argument("--no-normalize", action="store_true", help="keep the WAV's own level")
    c.add_argument("--trim", type=float, nargs="?", const=-48.0, metavar="DB",
                   help="trim leading/trailing silence below DB dBFS (default -48)")
    c.add_argument("--fade", type=float, default=0.0, metavar="MS", help="fade-out over the last MS milliseconds")
    c.add_argument("--max-seconds", type=float, metavar="S", help="cut the recording to S seconds")
    c.add_argument("--max-bytes", type=int, metavar="N", help="cut the result to N bytes per track")
    c.add_argument("--truncate", action="store_true", help="cut a too-long result to the DPCM size cap, not fail")
    c.add_argument("--fit", action="store_true", help="turn it down until it follows cleanly")
    c.add_argument("--preview", action="store_true", help="show size, duration and rate; write nothing")
    a = ap.parse_args(argv)
    root = Path(a.gamedir)
    if a.cmd == "demo":
        for n in demo_wavs(root / "music" / "wav"):
            print("wrote music/wav/%s.wav" % n)
        return 0
    rate, x = read_wav(a.wav)
    name = a.name or "".join(ch for ch in Path(a.wav).stem if ch.isalpha() or ch == "_") or "sample"
    try:
        r = "auto" if a.rate == "auto" else int(a.rate)
    except ValueError:
        raise DpcmError("rate is 0 to 15, or auto")
    opts = ImportOptions(rate=r, channel=a.channel or ("stereo" if a.stereo else "mix"), normalize=not a.no_normalize,
                         trim_db=a.trim, fade_ms=a.fade, max_seconds=a.max_seconds, max_bytes=a.max_bytes,
                         truncate=a.truncate, fit=a.fit)
    if a.preview:
        info = preview(x, rate, opts, name)
        print(format_preview(info, rate, len(x) / rate))
        return 1 if info["error"] else 0
    s, _ = convert_wav(x, rate, opts, name)
    save(root / "music" / "samples" / (name + ".dpcm"), s)
    print("music/samples/%s.dpcm: %d bytes, %.1f s, rate %d, %d part(s)" % (name, s.size, s.seconds, s.rate,
                                                                              len(s.tracks[0])))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
