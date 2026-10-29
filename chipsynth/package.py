# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The package file (*.csp, "chipsynth package"): several songs and what they share, in one binary file.

    pkg = Package(name="forest")
    pkg.add_song("dawn", song, lib)         # copies what the song uses from the library into the package
    save(pkg, "music/forest.csp");  pkg = load("music/forest.csp")
    tune = pkg.tune("dawn", base_lib)       # the flat Tune for TunePlayer

File layout (little endian):  b"CSPK", major (u16), minor (u16), then chunks of  tag (4 bytes), length (u32), data.
A reader skips tags it does not know and refuses a newer major version.  Chunks, in this order:
  META  name, author, comment, created, modified          INST  shared instruments       FXSE  shared effect sets
  SCAL  shared scales                                      SAMP  shared DPCM samples (BDPC bytes)
  SONG  one per song: its id, then the song (header, instruments, effect sets, scales, clips, six tracks)
Numbers are LEB128 varints (signed ones zig-zagged); text is UTF-8 with a varint length.  Saving is deterministic:
the same package always gives the same bytes.  Event tags are append-only (never renumber).
"""
import os
import re
import struct
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from . import dpcm, mml, song as sg
from .song import Song, Clip
from .synth import Instrument

MAGIC = b"CSPK"
MAJOR, MINOR = 1, 0
ID = re.compile(r"[A-Za-z0-9_.-]+$")


class PackageError(Exception):
    pass


# ------------------------------------------------------------------ low-level reader and writer
class _W:
    def __init__(self):
        self.parts = []

    def raw(self, b):
        self.parts.append(bytes(b))

    def u(self, n):
        if n < 0:
            raise PackageError("negative number %d" % n)
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
        self.raw(out)

    def s(self, n):
        self.u((n << 1) ^ (n >> 63) if n >= 0 else ((-n) << 1) - 1)

    def t(self, text):
        b = text.encode("utf-8")
        self.u(len(b))
        self.raw(b)

    def blob(self, b):
        self.u(len(b))
        self.raw(b)

    def flag(self, v):
        self.raw(b"\1" if v else b"\0")

    def f(self, x):
        self.raw(struct.pack("<d", x))

    def done(self):
        return b"".join(self.parts)


class _R:
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, n):
        if n < 0 or self.at + n > len(self.data):
            raise PackageError("the package is cut short or damaged")
        b = self.data[self.at:self.at + n]
        self.at += n
        return b

    def u(self):
        n = shift = 0
        while True:
            b = self.take(1)[0]
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                return n
            shift += 7
            if shift > 70:
                raise PackageError("the package is damaged (bad number)")

    def s(self):
        n = self.u()
        return (n >> 1) if not n & 1 else -((n + 1) >> 1)

    def t(self):
        try:
            return self.take(self.u()).decode("utf-8")
        except UnicodeDecodeError:
            raise PackageError("the package is damaged (bad text)")

    def blob(self):
        return self.take(self.u())

    def flag(self):
        return self.take(1) != b"\0"

    def f(self):
        return struct.unpack("<d", self.take(8))[0]

    @property
    def left(self):
        return len(self.data) - self.at


# ------------------------------------------------------------------ events
# One entry per event class; the position is the tag stored in the file, so only ever append.
SPEC = {
    sg.Note: "t s d", sg.Degree: "u s d", sg.Rest: "d", sg.Length: "u", sg.Octave: "O s O", sg.Volume: "O s",
    sg.Tempo: "u", sg.Tie: "", sg.Extend: "u", sg.Select: "t", sg.Fx: "t", sg.Tweak: "t t", sg.Key: "O T",
    sg.Bar: "", sg.Loop: "", sg.Once: "", sg.Repeat: "u L", sg.Call: "t", sg.Comment: "t", sg.Play: "t d",
    sg.Stop: "", sg.Level: "u",
}
CLASSES = list(SPEC)


def _put_dur(w, d):
    if d is None:
        w.u(0)
    elif isinstance(d, sg.Rel):
        w.u(2)
        w.u(d.dots)
        w.flag(d.trip)
    else:
        w.u(1)
        w.u(d)


def _get_dur(r):
    k = r.u()
    if k == 0:
        return None
    if k == 1:
        return r.u()
    if k == 2:
        dots = r.u()
        return sg.Rel(dots, r.flag())
    raise PackageError("the package is damaged (bad length)")


def _put_events(w, items):
    w.u(len(items))
    for e in items:
        spec = SPEC[type(e)].split()
        w.u(CLASSES.index(type(e)))
        for ch, f in zip(spec, fields(e)):
            v = getattr(e, f.name)
            if ch == "u":
                w.u(v)
            elif ch == "s":
                w.s(v)
            elif ch == "t":
                w.t(v)
            elif ch == "d":
                _put_dur(w, v)
            elif ch == "O":
                w.flag(v is not None)
                if v is not None:
                    w.s(v)
            elif ch == "T":
                w.flag(v is not None)
                if v is not None:
                    w.t(v)
            elif ch == "L":
                _put_events(w, v)


def _get_events(r):
    out = []
    for _ in range(r.u()):
        tag = r.u()
        if tag >= len(CLASSES):
            raise PackageError("this package uses an event this chipsynth does not know (a newer version made it)")
        cls = CLASSES[tag]
        args = []
        for ch in SPEC[cls].split():
            if ch == "u":
                args.append(r.u())
            elif ch == "s":
                args.append(r.s())
            elif ch == "t":
                args.append(r.t())
            elif ch == "d":
                args.append(_get_dur(r))
            elif ch == "O":
                args.append(r.s() if r.flag() else None)
            elif ch == "T":
                args.append(r.t() if r.flag() else None)
            elif ch == "L":
                args.append(_get_events(r))
        out.append(cls(*args))
    return out


# ------------------------------------------------------------------ instruments, effect sets, scales
def _put_inst(w, inst):
    fs = fields(Instrument)
    w.u(len(fs))
    for f in fs:
        v = getattr(inst, f.name)
        w.t(f.name)
        if isinstance(v, str):
            w.u(0)
            w.t(v)
        elif isinstance(v, tuple):
            w.u(2)
            w.u(len(v))
            for x in v:
                w.s(x)
        else:
            w.u(1)
            w.s(v)


def _get_inst(r):
    known = {f.name for f in fields(Instrument)}
    kw = {}
    for _ in range(r.u()):
        name, kind = r.t(), r.u()
        if kind == 0:
            v = r.t()
        elif kind == 1:
            v = r.s()
        elif kind == 2:
            v = tuple(r.s() for _ in range(r.u()))
        else:
            raise PackageError("the package is damaged (bad instrument)")
        if name in known:
            kw[name] = v
    return Instrument(**kw)


def _put_instruments(w, d):
    w.u(len(d))
    for k, v in d.items():
        w.t(k)
        _put_inst(w, v)


def _get_instruments(r):
    return {r.t(): _get_inst(r) for _ in range(r.u())}


def _put_fx(w, d):
    w.u(len(d))
    for k, pairs in d.items():
        w.t(k)
        w.u(len(pairs))
        for a, b in pairs:
            w.t(a)
            w.t(b)


def _get_fx(r):
    out = {}
    for _ in range(r.u()):
        k = r.t()
        out[k] = [(r.t(), r.t()) for _ in range(r.u())]
    return out


def _put_keys(w, d):
    w.u(len(d))
    for k, steps in d.items():
        w.t(k)
        w.u(len(steps))
        for x in steps:
            w.u(x)


def _get_keys(r):
    out = {}
    for _ in range(r.u()):
        k = r.t()
        out[k] = tuple(r.u() for _ in range(r.u()))
    return out


# ------------------------------------------------------------------ songs
def encode_song(song):
    w = _W()
    custom = (song.voices, song.dacs) != (4, 2)
    w.u(1 if custom else 0)                            # song format, minor revision (1: the voice and DAC counts follow)
    if custom:
        w.u(song.voices)
        w.u(song.dacs)
    for text in (song.title, song.author, song.comment):
        w.t(text)
    w.u(song.bpm)
    w.u(song.time[0])
    w.u(song.time[1])
    w.s(song.detune)
    w.flag(song.once)
    w.flag(song.echo is not None)
    if song.echo is not None:
        w.f(song.echo[0])
        w.f(song.echo[1])
        w.f(song.echo[2])
        w.flag(song.echo[3])
    w.u(song.reverb)
    for lines in (song.notes, song.footer):
        w.u(len(lines))
        for t in lines:
            w.t(t)
    _put_instruments(w, song.instruments)
    _put_fx(w, song.fx)
    _put_keys(w, song.keys)
    w.u(len(song.samples))
    for k, (file, rate) in song.samples.items():
        w.t(k)
        w.t(file)
        w.flag(rate is not None)
        if rate is not None:
            w.u(rate)
    w.u(len(song.clips))
    for c in song.clips.values():
        w.t(c.name)
        w.flag(c.dac)
        w.flag(c.raw is not None)
        if c.raw is not None:
            w.t(c.raw)
        _put_events(w, c.items)
    for name in song.names:
        _put_events(w, song.tracks.get(name, []))
    return w.done()


def decode_song(data):
    r = _R(data)
    minor = r.u()
    voices, dacs = (r.u(), r.u()) if minor >= 1 else (4, 2)
    if not 1 <= voices <= 16 or not 0 <= dacs <= 8:
        raise PackageError("a song with %d voices and %d DACs is not valid" % (voices, dacs))
    s = Song(title=r.t(), author=r.t(), comment=r.t(), voices=voices, dacs=dacs)
    s.bpm = r.u()
    s.time = (r.u(), r.u())
    s.detune = r.s()
    s.once = r.flag()
    if r.flag():
        s.echo = (r.f(), r.f(), r.f(), r.flag())
    s.reverb = r.u()
    s.notes = [r.t() for _ in range(r.u())]
    s.footer = [r.t() for _ in range(r.u())]
    s.instruments = _get_instruments(r)
    s.fx = _get_fx(r)
    s.keys = _get_keys(r)
    for _ in range(r.u()):
        k, file = r.t(), r.t()
        s.samples[k] = (file, r.u() if r.flag() else None)
    for _ in range(r.u()):
        name, dac = r.t(), r.flag()
        raw = r.t() if r.flag() else None
        s.clips[name] = Clip(name, _get_events(r), dac, raw)
    for name in s.names:
        s.tracks[name] = _get_events(r)
    return s


# ------------------------------------------------------------------ the package
@dataclass
class Package:
    name: str = ""
    author: str = ""
    comment: str = ""
    created: str = ""
    modified: str = ""
    instruments: dict = field(default_factory=dict)     # shared by all songs; a song's own override them
    fx: dict = field(default_factory=dict)
    keys: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)         # name -> dpcm.Sample
    songs: dict = field(default_factory=dict)           # id -> Song

    def library(self, base=None):
        """A library for compiling this package's songs: `base` (the game's) with the package's own on top."""
        base = base or mml.builtin_library()
        lib = dict(base)
        lib["instr"] = {**base["instr"], **self.instruments}
        lib["fx"] = {**base.get("fx", {}), **self.fx}
        lib["key"] = {**base.get("key", {}), **self.keys}
        mem = dict(base.get("_mem") or {})
        mem.update(self.samples)
        lib["_mem"] = mem
        return lib

    def tune(self, song_id, base=None):
        if song_id not in self.songs:
            raise PackageError("no song '%s' in package '%s'" % (song_id, self.name))
        return self.songs[song_id].compile(self.library(base))

    def add_song(self, song_id, song, lib=None):
        """Store a copy of `song` under `song_id`, with everything it uses from `lib` (instruments, effect sets,
        scales, samples) copied in, so the package plays without the library."""
        import copy
        if not ID.match(song_id):
            raise PackageError("a song id is letters, digits and _ . - (got %r)" % song_id)
        song = copy.deepcopy(song)
        found = embed(song, lib or mml.builtin_library())
        left = {}
        rename = {}
        for name, smp in found.items():
            new = name
            n = 0
            while new in self.samples and not _same_sample(self.samples[new], smp):
                n += 1
                new = "%s_%s" % (name, _letters(n))
            if new != name:
                rename[name] = new
            self.samples[new] = replace(smp, name=new)
        if rename:
            _rename_samples(song, rename)
        song.samples = {k: v for k, v in song.samples.items() if k not in found}
        song.warnings = []
        self.songs[song_id] = song
        return song


def _letters(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return s


def _same_sample(a, b):
    return a.rate == b.rate and a.tracks == b.tracks


def _walk(items):
    for e in items:
        yield e
        if isinstance(e, sg.Repeat):
            yield from _walk(e.items)


def _all_events(song):
    for items in song.tracks.values():
        yield from _walk(items)
    for c in song.clips.values():
        yield from _walk(c.items)


def _rename_samples(song, rename):
    for e in _all_events(song):
        if isinstance(e, sg.Play) and e.sample in rename:
            e.sample = rename[e.sample]


def embed(song, lib):
    """Copy the library instruments, effect sets and scales the song uses into the song; returns {name: Sample} for
    the samples its DAC tracks play (the caller decides where they live)."""
    samples = {}
    parser = mml._Parser("", lib)
    parser.sampledefs = dict(song.samples)
    for e in _all_events(song):
        if isinstance(e, sg.Select) and e.name not in song.instruments:
            inst = lib["instr"].get(e.name)
            if inst is not None:
                song.instruments[e.name] = replace(inst)
        elif isinstance(e, sg.Fx) and e.name not in song.fx:
            pairs = lib.get("fx", {}).get(e.name)
            if pairs is not None:
                song.fx[e.name] = list(pairs)
        elif isinstance(e, sg.Key) and e.scale and e.scale not in song.keys and e.scale not in mml.SCALES:
            steps = lib.get("key", {}).get(e.scale)
            if steps is not None:
                song.keys[e.scale] = tuple(steps)
        elif isinstance(e, sg.Play) and e.sample not in samples:
            smp = parser.find_sample(e.sample)
            if smp is not None:
                samples[e.sample] = smp
    return samples


# ------------------------------------------------------------------ the file
def dumps(pkg):
    w = _W()
    w.raw(MAGIC)
    w.raw(struct.pack("<HH", MAJOR, MINOR))

    def chunk(tag, data):
        w.raw(tag)
        w.raw(struct.pack("<I", len(data)))
        w.raw(data)
    m = _W()
    for text in (pkg.name, pkg.author, pkg.comment, pkg.created, pkg.modified):
        m.t(text)
    chunk(b"META", m.done())
    for tag, put, d in ((b"INST", _put_instruments, pkg.instruments), (b"FXSE", _put_fx, pkg.fx),
                        (b"SCAL", _put_keys, pkg.keys)):
        if d:
            c = _W()
            put(c, d)
            chunk(tag, c.done())
    if pkg.samples:
        c = _W()
        c.u(len(pkg.samples))
        for k, smp in pkg.samples.items():
            c.t(k)
            c.blob(dpcm.dumps(smp))
        chunk(b"SAMP", c.done())
    for sid, song in pkg.songs.items():
        c = _W()
        c.t(sid)
        c.raw(encode_song(song))
        chunk(b"SONG", c.done())
    return w.done()


def loads(data):
    if data[:4] != MAGIC:
        raise PackageError("this is not a chipsynth package")
    if len(data) < 8:
        raise PackageError("the package is cut short or damaged")
    major, minor = struct.unpack_from("<HH", data, 4)
    if major > MAJOR:
        raise PackageError("this package is version %d.%d; this chipsynth reads up to %d.x (update chipsynth)"
                           % (major, minor, MAJOR))
    pkg = Package()
    at = 8
    while at < len(data):
        if at + 8 > len(data):
            raise PackageError("the package is cut short or damaged")
        tag = data[at:at + 4]
        (n,) = struct.unpack_from("<I", data, at + 4)
        body = data[at + 8:at + 8 + n]
        if len(body) != n:
            raise PackageError("the package is cut short or damaged")
        at += 8 + n
        r = _R(body)
        if tag == b"META":
            pkg.name, pkg.author, pkg.comment, pkg.created, pkg.modified = (r.t() for _ in range(5))
        elif tag == b"INST":
            pkg.instruments = _get_instruments(r)
        elif tag == b"FXSE":
            pkg.fx = _get_fx(r)
        elif tag == b"SCAL":
            pkg.keys = _get_keys(r)
        elif tag == b"SAMP":
            for _ in range(r.u()):
                name = r.t()
                try:
                    pkg.samples[name] = dpcm.loads(r.blob(), name)
                except dpcm.DpcmError as e:
                    raise PackageError("sample '%s': %s" % (name, e))
        elif tag == b"SONG":
            sid = r.t()
            pkg.songs[sid] = decode_song(body[r.at:])
        # any other tag: written by a newer minor version; skipped
    return pkg


def save(pkg, path):
    """Write atomically (a temporary file beside it, then a rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(dumps(pkg))
    os.replace(tmp, p)


def load(path):
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise PackageError("can't read %s (%s)" % (p.name, e))
    pkg = loads(data)
    if not pkg.name:
        pkg.name = p.stem
    return pkg
