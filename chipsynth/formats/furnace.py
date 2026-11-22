# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Furnace `.fur` modules -> chipsynth songs.

Written from the file format as Furnace's own documents describe it (papers/format.md, papers/newIns.md, and the effect
and chip pages of its manual; GPL, read only, nothing copied).  See PLAN-FORMATS.md section 9 for what is carried over.

What is read: format versions 157 (Furnace 0.6pre5) to 259, that is the INFO block with INS2 instruments and PATN
patterns (up to 239) and the INF2 block (240 and up), zlib-compressed or not.  Older versions use a different pattern and
instrument layout and are refused with a message; so are versions newer than 259.

What plays: the NES (2A03: two pulses, the triangle, the noise and the DPCM channel as D1), the C64 SID (6581, 8580 and the
6581 with PCM: its three voices) and the two pulse channels of the Game Boy.  Every other chip, and the channels that have
no honest mapping (the Game Boy wave and noise, the SID's software PCM), are left out and named in the Report.

Timing: one Furnace tick is 4 chipsynth ticks (96 ticks to a quarter note, so 24 Furnace ticks to a quarter note and the
song's bpm is the tick rate times 2.5).  A row is `speed * 4` ticks long, so speed changes, alternating speeds and grooves are
exact, and a change of the tick rate (effects Cxxx and F0xx) is a tempo event.
"""
import math
import re
import struct
import zlib
from dataclasses import dataclass, field, replace

from .. import dpcm, song as sg
from ..synth import DPCM_RATES, Instrument
from .envelope import fit_arpeggio, fit_volume, GOOD
from .famistudio_text import noise_pitch
from .report import Report

MAGIC = b"-Furnace module-"
MIN_VERSION, MAX_VERSION, MAX_VERIFIED = 157, 259, 251
MAX_UNPACKED = 64 << 20                      # a safety stop for a zlib bomb
TICKS_PER_FRAME = 4                          # chipsynth ticks in one Furnace tick
BEAT_FRAMES = 24                             # Furnace ticks in a quarter note (96 / 4)
C0 = 60                                      # a pattern note value of 60 is C-0, chipsynth pitch 0
NOTE_OFF, NOTE_RELEASE, MACRO_RELEASE, RAW_FREQ = 180, 181, 182, 183
LETTERS = (("c", 0), ("c", 1), ("d", 0), ("d", 1), ("e", 0), ("f", 0), ("f", 1), ("g", 0), ("g", 1), ("a", 0), ("a", 1), ("b", 0))
DUTY_PW = (12, 25, 50, 75)                   # NES / Game Boy duty 0-3 as a percent
MAX_VOICES, MAX_DACS = 16, 8

# the format versions that have a name in papers/format.md (only used to say what a refused file is)
VERSION_NAMES = {
    228: "0.6.8.1", 227: "0.6.8", 226: "0.6.8pre2", 225: "0.6.8pre1", 219: "0.6.7", 218: "0.6.6", 214: "0.6.5",
    212: "0.6.4", 201: "0.6.3", 197: "0.6.2", 192: "0.6.1", 181: "0.6", 180: "0.6pre18", 179: "0.6pre17",
    178: "0.6pre16", 177: "0.6pre15", 175: "0.6pre14", 174: "0.6pre13", 173: "0.6pre12", 172: "0.6pre11",
    171: "0.6pre10", 169: "0.6pre9", 166: "0.6pre8", 162: "0.6pre7", 161: "0.6pre6", 158: "0.6pre5", 143: "0.6pre4",
    133: "0.6pre3", 132: "0.6pre2", 116: "0.6pre1.5", 100: "0.6pre1", 54: "0.5.8", 35: "0.5", 27: "0.4.6", 20: "0.4", 12: "0.2",
}

# chip id -> (name, nominal channel count), from the list in papers/format.md.  The INFO block does not store channel counts,
# so every chip a pre-240 file names must be here; INF2 files carry their own counts.
CHIPS = {
    0x01: ("YMU759", 17), 0x02: ("Genesis (YM2612 + SN76489)", 10), 0x03: ("SN76489", 4), 0x04: ("Game Boy", 4),
    0x05: ("PC Engine", 6), 0x06: ("NES", 5), 0x07: ("C64 (8580)", 3), 0x08: ("Arcade (YM2151 + SegaPCM)", 13),
    0x09: ("Neo Geo CD (YM2610)", 13), 0x42: ("Genesis extended", 13), 0x43: ("SMS + OPLL", 13), 0x46: ("NES + VRC7", 11),
    0x47: ("C64 (6581)", 3), 0x49: ("Neo Geo CD extended", 16), 0x80: ("AY-3-8910", 3), 0x81: ("Amiga", 4),
    0x82: ("YM2151", 8), 0x83: ("YM2612", 6), 0x84: ("TIA", 2), 0x85: ("VIC-20", 4), 0x86: ("PET", 1), 0x87: ("SNES", 8),
    0x88: ("VRC6", 3), 0x89: ("OPLL", 9), 0x8A: ("FDS", 1), 0x8B: ("MMC5", 3), 0x8C: ("Namco 163", 8), 0x8D: ("YM2203", 6),
    0x8E: ("YM2608", 16), 0x8F: ("OPL", 9), 0x90: ("OPL2", 9), 0x91: ("OPL3", 18), 0x92: ("MultiPCM", 28),
    0x93: ("Intel 8253 beeper", 1), 0x94: ("POKEY", 4), 0x95: ("RF5C68", 8), 0x96: ("WonderSwan", 4), 0x97: ("SAA1099", 6),
    0x98: ("OPZ", 8), 0x99: ("Pokemon Mini", 1), 0x9A: ("AY8930", 3), 0x9B: ("SegaPCM", 16), 0x9C: ("Virtual Boy", 6),
    0x9D: ("VRC7", 6), 0x9E: ("YM2610B", 16), 0x9F: ("ZX Spectrum beeper", 6), 0xA0: ("YM2612 extended", 9),
    0xA1: ("Konami SCC", 5), 0xA2: ("OPL drums", 11), 0xA3: ("OPL2 drums", 11), 0xA4: ("OPL3 drums", 20),
    0xA5: ("Neo Geo (YM2610)", 14), 0xA6: ("Neo Geo extended", 17), 0xA7: ("OPLL drums", 11), 0xA8: ("Atari Lynx", 4),
    0xA9: ("SegaPCM (DefleMask)", 5), 0xAA: ("MSM6295", 4), 0xAB: ("MSM6258", 1), 0xAC: ("Commander X16 VERA", 17),
    0xAD: ("Bubble System WSG", 2), 0xAE: ("OPL4", 42), 0xAF: ("OPL4 drums", 44), 0xB0: ("X1-010", 16),
    0xB1: ("ES5506", 32), 0xB2: ("Y8950", 10), 0xB3: ("Y8950 drums", 12), 0xB4: ("Konami SCC+", 5),
    0xB5: ("tildearrow Sound Unit", 8), 0xB6: ("YM2203 extended", 9), 0xB7: ("YM2608 extended", 19), 0xB8: ("YMZ280B", 8),
    0xB9: ("Namco WSG", 3), 0xBA: ("Namco C15", 8), 0xBB: ("Namco C30", 8), 0xBC: ("MSM5232", 8),
    0xBD: ("YM2612 DualPCM extended", 11), 0xBE: ("YM2612 DualPCM", 7), 0xBF: ("T6W28", 4), 0xC0: ("PCM DAC", 1),
    0xC1: ("YM2612 CSM", 10), 0xC2: ("Neo Geo CSM", 18), 0xC3: ("YM2203 CSM", 10), 0xC4: ("YM2608 CSM", 20),
    0xC5: ("YM2610B CSM", 20), 0xC6: ("K007232", 2), 0xC7: ("GA20", 4), 0xC8: ("SM8521", 3), 0xC9: ("M114S", 16),
    0xCA: ("ZX Spectrum QuadTone", 5), 0xCB: ("Casio PV-1000", 3), 0xCC: ("K053260", 4), 0xCD: ("TED", 2),
    0xCE: ("Namco C140", 24), 0xCF: ("Namco C219", 16), 0xD0: ("Namco C352", 32), 0xD1: ("ESFM", 18),
    0xD2: ("ES5503", 32), 0xD4: ("PowerNoise", 4), 0xD5: ("Dave", 6), 0xD6: ("NDS", 16), 0xD7: ("GBA direct", 2),
    0xD8: ("GBA MinMod", 16), 0xD9: ("Bifurcator", 4), 0xDA: ("SCSP", 32), 0xDB: ("YMF271", 48), 0xDC: ("RF5C400", 32),
    0xDD: ("YM2612 XGM", 9), 0xDE: ("YM2610B extended", 19), 0xDF: ("YM2612 XGM extended", 13), 0xE0: ("QSound", 19),
    0xE1: ("PS1", 24), 0xE2: ("C64 (6581) with PCM", 4), 0xE3: ("Watara Supervision", 4), 0xE4: ("Namco Pole Position WSG", 8),
    0xE5: ("uPD1771C-017", 4), 0xE7: ("klattsch", 1), 0xE9: ("SAA1099 with timers", 8), 0xF0: ("SID2", 3), 0xF1: ("5E01", 5),
    0xF5: ("SID3", 7), 0xFC: ("Pong", 1), 0xFD: ("Dummy System", 8),
}
# the chips (and channels of them) that play: a kind per channel, None for a channel with no honest mapping
NES_KINDS = ("pulse", "pulse", "tri", "noise", "dpcm")
SID_KINDS = ("sid", "sid", "sid")
SUPPORT = {0x06: NES_KINDS, 0x07: SID_KINDS, 0x47: SID_KINDS, 0xE2: SID_KINDS + (None,), 0x04: ("gb", "gb", None, None)}
SKIP_WHY = {(0x04, 2): "the Game Boy wave channel has no honest mapping", (0x04, 3): "the Game Boy noise channel's pitch layout is not verified",
            (0xE2, 3): "the SID's software PCM channel"}
INS_NES, INS_C64, INS_GB = 34, 3, 2
EFFECT_NAMES = {
    0x01: "pitch slide up (01xx)", 0x02: "pitch slide down (02xx)", 0x03: "portamento (03xx)", 0x04: "vibrato (04xy)",
    0x05: "volume slide with portamento (05xy)", 0x06: "volume slide with vibrato (06xy)", 0x07: "tremolo (07xy)",
    0x08: "panning (08xy)", 0x0A: "volume slide (0Axy)", 0x0C: "retrigger (0Cxx)", 0x80: "panning (80xx)",
    0xE1: "note slide up (E1xy)", 0xE2: "note slide down (E2xy)", 0xE3: "vibrato shape (E3xx)", 0xE4: "vibrato range (E4xx)",
    0xE5: "fine pitch (E5xx)", 0xE6: "quick legato (E6xy)", 0xE7: "macro release (E7xx)", 0xE8: "quick legato up (E8xy)",
    0xE9: "quick legato down (E9xy)", 0xEA: "legato (EAxx)", 0xEE: "external command (EExx)", 0xF1: "single tick pitch up (F1xx)",
    0xF2: "single tick pitch down (F2xx)", 0xF3: "fine volume slide up (F3xx)", 0xF4: "fine volume slide down (F4xx)",
    0xF5: "disable macro (F5xx)", 0xF6: "enable macro (F6xx)", 0xF7: "restart macro (F7xx)", 0xF8: "single tick volume up (F8xx)",
    0xF9: "single tick volume down (F9xx)", 0xFA: "fast volume slide (FAxy)", 0xFC: "note release (FCxx)",
    0xFD: "virtual tempo numerator (FDxx)", 0xFE: "virtual tempo denominator (FExx)", 0xD3: "volume portamento (D3xx)",
    0xD4: "fast volume portamento (D4xx)", 0xDC: "delayed mute (DCxx)", 0x90: "sample offset (90xx)", 0x91: "sample offset (91xx)",
    0x92: "sample offset (92xx)", 0x11: "chip effect 11xx", 0x13: "chip effect 13xx", 0x14: "chip effect 14xx",
    0x15: "chip effect 15xx", 0x16: "chip effect 16xx", 0x17: "chip effect 17xx", 0x18: "chip effect 18xx",
    0x19: "chip effect 19xx", 0x20: "chip effect 20xx",
}
# DPCM rate index for the notes of the NES manual's frequency table (C-3 ... C-6), as pattern note values
DPCM_NOTE_TABLE = (96, 98, 100, 101, 103, 105, 107, 108, 110, 113, 115, 117, 120, 124, 127, 132)


class FurnaceError(ValueError):
    pass


# ------------------------------------------------------------------ the file
class _Reader:
    def __init__(self, data, pos=0, end=None):
        self.data, self.pos = data, pos
        self.end = len(data) if end is None else min(end, len(data))

    def take(self, n):
        if n < 0 or self.pos + n > self.end:
            raise FurnaceError("the file ends early (truncated)")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def seek(self, pos):
        if not 0 <= pos <= len(self.data):
            raise FurnaceError("a pointer in the file points outside it")
        self.pos = pos
        return self

    def u8(self):
        return self.take(1)[0]

    def i8(self):
        return struct.unpack("<b", self.take(1))[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def i16(self):
        return struct.unpack("<h", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def f32(self):
        return struct.unpack("<f", self.take(4))[0]

    def cstr(self):
        stop = self.data.find(b"\0", self.pos, self.end)
        if stop < 0:
            raise FurnaceError("the file ends early (truncated)")
        out = self.data[self.pos:stop].decode("utf-8", "replace")
        self.pos = stop + 1
        return out

    def left(self):
        return self.end - self.pos


def version_name(v):
    return "Furnace " + VERSION_NAMES[v] if v in VERSION_NAMES else "a dev build"


def unpack(data):
    """The module's bytes as Furnace stores them, after zlib when the file is compressed."""
    data = bytes(data)
    if data[:16] != MAGIC:
        try:
            d = zlib.decompressobj().decompress(data, MAX_UNPACKED + 1)
        except zlib.error:
            raise FurnaceError("not a Furnace module (it should start with -Furnace module- or be zlib-compressed)")
        if len(d) > MAX_UNPACKED:
            raise FurnaceError("the module is too big once unpacked")
        data = d
    if data[:16] != MAGIC:
        raise FurnaceError("not a Furnace module (it should start with -Furnace module-)")
    return data


def looks_like(data):
    """True when `data` is a Furnace module, plain or zlib-compressed (only the first bytes are looked at)."""
    if bytes(data[:16]) == MAGIC:
        return True
    try:
        return zlib.decompressobj().decompress(bytes(data[:4096]), 16)[:16] == MAGIC
    except zlib.error:
        return False


# ------------------------------------------------------------------ what the file holds
@dataclass
class Macro:
    code: int
    values: list
    loop: object            # step index, or None
    release: object
    mode: int
    kind: int               # 0 sequence, 1 ADSR, 2 LFO
    delay: int
    speed: int


@dataclass
class Ins:
    index: int
    version: int
    type: int
    name: str = ""
    macros: dict = field(default_factory=dict)       # macro code -> Macro
    c64: object = None                                # dict
    gb: object = None
    sm: object = None                                 # dict: sample, flags, map {pattern note: (play note, sample)}
    ne: object = None                                 # dict: map {pattern note: (pitch, delta)}


@dataclass
class Sample:
    index: int
    name: str
    depth: int
    rate: int               # the C-4 rate, Hz
    length: int
    data: bytes


@dataclass
class Cell:
    note: object = None
    ins: object = None
    vol: object = None
    fx: list = field(default_factory=list)


@dataclass
class SubSong:
    name: str
    comment: str
    hz: float
    timebase: int           # ticks per row are multiplied by timebase + 1 (0 in INF2 files, which have none)
    speeds: list
    pattern_len: int
    orders_len: int
    hl_a: int
    hl_b: int
    vt_num: int
    vt_den: int
    orders: list            # [channel][order] -> pattern index


@dataclass
class Module:
    version: int
    name: str
    author: str
    comment: str
    tuning: float
    chips: list             # [(chip id, channels)]
    subsongs: list
    instruments: dict       # index -> Ins
    samples: dict           # index -> Sample
    patterns: dict          # (subsong, channel, pattern) -> {row: Cell}
    grooves: list = field(default_factory=list)     # [[ticks per row, ...]] chosen by effect 09xx when there are any

    @property
    def channels(self):
        return sum(n for _c, n in self.chips)


def _chip_channels(cid):
    if cid not in CHIPS:
        raise FurnaceError("the module names a sound chip (id 0x%02x) this importer does not know, so its channel layout is unknown" % cid)
    return CHIPS[cid][1]


def parse(data):
    """Module bytes -> Module.  Raises FurnaceError on anything it cannot read."""
    data = unpack(data)
    if len(data) < 32:
        raise FurnaceError("the file ends early (truncated)")
    r = _Reader(data, 16)
    version = r.u16()
    if version < MIN_VERSION:
        raise FurnaceError("format version %d (%s) is older than this importer reads (%d, Furnace 0.6pre5, and newer): "
                           "open it in Furnace 0.6 or later and save it again" % (version, version_name(version), MIN_VERSION))
    if version > MAX_VERSION:
        raise FurnaceError("format version %d is newer than this importer knows (%d at most): "
                           "its layout may have changed, so it is not read" % (version, MAX_VERSION))
    r.take(2)
    ptr = r.u32()
    if ptr + 8 > len(data):
        raise FurnaceError("the song info pointer points outside the file")
    tag = data[ptr:ptr + 4]
    if tag == b"INF2" and version >= 240:
        return _parse_inf2(data, version, ptr)
    if tag == b"INFO" and version < 240:
        return _parse_info(data, version, ptr)
    raise FurnaceError("the song info block is %r, which a version %d file should not have" % (tag.decode("latin-1"), version))


def _block(data, ptr, tag):
    """A Reader over the body of the block at `ptr`, which must be `tag`."""
    r = _Reader(data)
    r.seek(ptr)
    got = r.take(4)
    if got != tag:
        raise FurnaceError("expected a %s block where the file points, found %r" % (tag.decode(), got.decode("latin-1")))
    size = r.u32()
    return _Reader(data, r.pos, r.pos + size)


def _parse_info(data, version, ptr):
    r = _block(data, ptr, b"INFO")
    timebase, speed1, speed2, _arp = r.u8(), r.u8(), r.u8(), r.u8()
    hz = r.f32()
    patlen, ordlen, hla, hlb = r.u16(), r.u16(), r.u8(), r.u8()
    ninst, nwave, nsamp, npat = r.u16(), r.u16(), r.u16(), r.u32()
    ids = []
    for b in r.take(32):
        if b == 0:
            break
        ids.append(b)
    chips = [(cid, _chip_channels(cid)) for cid in ids]
    r.take(32 + 32 + 128)                         # chip volumes, panning, flag pointers
    name, author = r.cstr(), r.cstr()
    tuning = r.f32()
    r.take(20)                                    # compatibility flags
    pointers = {k: [r.u32() for _ in range(n)] for k, n in (("ins", ninst), ("wave", nwave), ("smp", nsamp), ("pat", npat))}
    nch = sum(n for _c, n in chips)
    orders = [[r.u8() for _ in range(ordlen)] for _ in range(nch)]
    r.take(nch * 3)                               # effect columns, hidden, collapsed
    for _ in range(nch * 2):                      # channel names, short names
        r.cstr()
    comment = r.cstr()
    r.f32()                                       # master volume
    r.take(28)                                    # extended compatibility flags
    vt_num, vt_den = r.u16(), r.u16()
    first_name, first_comment = r.cstr(), r.cstr()
    extra = r.u8()
    r.take(3)
    sub_ptrs = [r.u32() for _ in range(extra)]
    for _ in range(6):                            # system name, album, and the four Japanese strings
        r.cstr()
    r.take(12 * len(chips))                       # per-chip volume, panning, balance
    r.take(4 * r.u32())                           # patchbay connections
    r.u8()                                        # automatic patchbay
    r.take(8)                                     # more compatibility flags (always present: the older ones are reserved)
    n = r.u8()
    if not 1 <= n <= 16:
        raise FurnaceError("bad speed pattern length %d" % n)
    speeds = list(r.take(16))[:n]
    grooves = []
    for _ in range(r.u8()):
        n = r.u8()
        g = list(r.take(16))[:n]
        if n >= 1:
            grooves.append(g)
    first = SubSong(first_name, first_comment, hz, timebase, speeds, patlen, ordlen, hla, hlb, vt_num, vt_den, orders)
    subs = [first]
    for p in sub_ptrs:
        subs.append(_old_subsong(_block(data, p, b"SONG"), nch, version))
    mod = _finish(data, version, name, author, comment, tuning, chips, subs, pointers)
    mod.grooves = grooves
    return mod


def _old_subsong(r, nch, version):
    timebase, speed1, speed2, _arp = r.u8(), r.u8(), r.u8(), r.u8()
    hz = r.f32()
    patlen, ordlen, hla, hlb = r.u16(), r.u16(), r.u8(), r.u8()
    vt_num, vt_den = r.u16(), r.u16()
    name, comment = r.cstr(), r.cstr()
    orders = [[r.u8() for _ in range(ordlen)] for _ in range(nch)]
    r.take(nch * 3)
    for _ in range(nch * 2):
        r.cstr()
    n = r.u8()
    if not 1 <= n <= 16:
        raise FurnaceError("bad speed pattern length %d" % n)
    speeds = list(r.take(16))[:n]
    return SubSong(name, comment, hz, timebase, speeds, patlen, ordlen, hla, hlb, vt_num, vt_den, orders)


def _parse_inf2(data, version, ptr):
    r = _block(data, ptr, b"INF2")
    name, author = r.cstr(), r.cstr()
    for _ in range(6):                            # system name, album, and the four Japanese strings
        r.cstr()
    tuning = r.f32()
    r.u8()                                        # automatic system name
    r.f32()                                       # master volume
    total, nchips = r.u16(), r.u16()
    chips = []
    for _ in range(nchips):
        cid, n = r.u16(), r.u16()
        r.take(12)
        chips.append((cid, n))
    if sum(n for _c, n in chips) != total:
        raise FurnaceError("the chips' channel counts do not add up to the total channel count")
    r.take(4 * r.u32())                           # patchbay connections
    r.u8()                                        # automatic patchbay
    elements = {}
    while True:
        kind = r.u8()
        if kind == 0:
            break
        elements[kind] = [r.u32() for _ in range(r.u32())]
    subs = []
    for p in elements.get(1, []):
        subs.append(_new_subsong(_block(data, p, b"SNG2"), total))
    comment = ""
    for p in elements.get(9, []):
        comment = _block(data, p, b"CMNT").cstr()
    pointers = {"ins": elements.get(4, []), "wave": elements.get(5, []), "smp": elements.get(6, []), "pat": elements.get(7, [])}
    mod = _finish(data, version, name, author, comment, tuning, chips, subs, pointers)
    for p in elements.get(10, []):
        g = _block(data, p, b"GROV")
        n = g.u8()
        vals = [g.u16() for _ in range(16)][:n]
        if n >= 1:
            mod.grooves.append(vals)
    return mod


def _new_subsong(r, nch):
    hz = r.f32()
    r.u8()                                        # initial arpeggio speed
    r.u8()                                        # effect speed divider
    patlen, ordlen, hla, hlb = r.u16(), r.u16(), r.u8(), r.u8()
    vt_num, vt_den = r.u16(), r.u16()
    n = r.u8()
    if not 1 <= n <= 16:
        raise FurnaceError("bad speed pattern length %d" % n)
    speeds = [r.u16() for _ in range(16)][:n]
    name, comment = r.cstr(), r.cstr()
    orders = [[r.u8() for _ in range(ordlen)] for _ in range(nch)]
    return SubSong(name, comment, hz, 0, speeds, patlen, ordlen, hla, hlb, vt_num, vt_den, orders)


def _finish(data, version, name, author, comment, tuning, chips, subs, pointers):
    if not subs:
        raise FurnaceError("the module has no songs")
    nch = sum(n for _c, n in chips)
    instruments = {i: _instrument(_block(data, p, b"INS2"), i) for i, p in enumerate(pointers["ins"])}
    samples = {i: _sample(_block(data, p, b"SMP2"), i) for i, p in enumerate(pointers["smp"])}
    patterns = {}
    for p in pointers["pat"]:
        if data[p:p + 4] == b"PATR":
            raise FurnaceError("this version %d file stores its patterns in the older PATR layout, which is not read: "
                               "open it in Furnace and save it again" % version)
        got = _read_pattern(_block(data, p, b"PATN"), version, subs, nch)
        if got is not None:
            patterns[got[0]] = got[1]
    return Module(version, name, author, comment, tuning, chips, subs, instruments, samples, patterns)


def _read_pattern(r, version, subs, nch):
    """One PATN block -> ((subsong, channel, pattern), {row: Cell}), or None for a pattern of a channel that does not exist.
    The channel is 8 bits wide in every version seen (up to 251) although the format document says 16 bits from 240; from 252
    on both are tried, the 16-bit one first, and the one that reads the whole block cleanly (with a name that is text) is used."""
    start = r.pos
    last = None
    layouts = (False,) if version <= MAX_VERIFIED else (True, False)
    for wide in layouts:
        r.pos = start
        try:
            sub = r.u8()
            chan = r.u16() if wide else r.u8()
            idx = r.u16()
            pname = r.cstr()
            if sub >= len(subs) or chan >= nch or (len(layouts) > 1 and any(ord(c) < 32 for c in pname)):
                last = None                           # not this layout: a pattern's name is text
                continue
            rows = _pattern_rows(r, subs[sub].pattern_len)
            if r.left() == 0 or version <= MAX_VERIFIED:
                return (sub, chan, idx), rows
        except FurnaceError as e:
            last = e
    if last is not None:
        raise last
    return None


def _pattern_rows(r, rows):
    """The cells of one pattern (sparse: {row: Cell}) as the PATN block packs them."""
    out, row = {}, 0
    while r.left() > 0:
        b = r.u8()
        if b == 0xFF:
            break
        if b & 0x80:
            row += (b & 0x7F) + 2
            continue
        if b == 0:
            row += 1
            continue
        mask = [0] * 16                           # effect / value presence, two bits a column, eight columns
        if b & 0x08:
            mask[0] = 1
        if b & 0x10:
            mask[1] = 1
        if b & 0x20:
            m = r.u8()
            for k in range(8):
                mask[k] |= (m >> k) & 1
        if b & 0x40:
            m = r.u8()
            for k in range(8):
                mask[8 + k] |= (m >> k) & 1
        cell = Cell()
        if b & 0x01:
            cell.note = r.u8()
            if cell.note == RAW_FREQ:
                r.u32()
        if b & 0x02:
            cell.ins = r.u8()
        if b & 0x04:
            cell.vol = r.u8()
        for col in range(8):
            code = r.u8() if mask[2 * col] else None
            val = r.u8() if mask[2 * col + 1] else None
            if code is not None:
                cell.fx.append((code, val or 0))
        if row < rows:
            out[row] = cell
        row += 1
    return out


def _macros(body):
    """The macros of an `MA` feature: {code: Macro}.  The 2-byte header length is read as the length of each macro's own
    header (8 bytes in every file seen); if that does not parse, 8 is tried."""
    hl = struct.unpack("<H", body[:2])[0] if len(body) >= 2 else 8
    last = None
    for want in ((hl, 8) if hl != 8 else (8,)):
        try:
            return _read_macros(body, want)
        except FurnaceError as e:
            last = e
    raise last


def _read_macros(body, hl):
    if hl < 8:
        raise FurnaceError("a macro header of %d bytes is too short" % hl)
    r = _Reader(body, 2)
    out = {}
    while r.left() > 0:
        code = r.u8()
        if code == 255:
            break
        length, loop, rel, mode, flags, delay, speed = (r.u8() for _ in range(7))
        r.take(hl - 8)
        word = flags >> 6
        vals = [(r.u8, r.i8, r.i16, r.i32)[word]() for _ in range(length)]
        out[code] = Macro(code, vals, loop if loop < length else None, rel if rel < length else None, mode,
                          (flags >> 1) & 3, delay, max(1, speed))
    return out


def _instrument(r, index):
    ver, typ = r.u16(), r.u16()
    ins = Ins(index, ver, typ)
    while r.left() >= 4:
        code = r.take(2).decode("latin-1")
        size = r.u16()
        body = r.take(size)
        if code == "EN":
            break
        b = _Reader(body)
        if code == "NA":
            ins.name = b.cstr()
        elif code == "MA":
            ins.macros = _macros(body)
        elif code == "64":
            f1, f2, ad, sr, duty, cutres = b.u8(), b.u8(), b.u8(), b.u8(), b.u16(), b.u16()
            ins.c64 = dict(flags1=f1, flags2=f2, ad=ad, sr=sr, duty=duty, cutoff=cutres & 0x7FF, res=cutres >> 12)
        elif code == "GB":
            env, length, flags = b.u8(), b.u8(), b.u8()
            ins.gb = dict(env=env, length=length, flags=flags)
        elif code == "SM":
            sample, flags, _wave = b.u16(), b.u8(), b.u8()
            mp = {}
            if flags & 1:
                n = 180 if ver >= 246 else 120
                for k in range(n):
                    note, smp = b.u16(), b.u16()
                    if smp != 0xFFFF:
                        mp[k if ver >= 246 else k + C0 - 0] = (note, smp)
            ins.sm = dict(sample=sample, flags=flags, map=mp)
        elif code == "NE":
            use = b.u8()
            mp = {}
            if use:
                for k in range(180 if ver >= 246 else 120):
                    pitch, delta = b.u8(), b.u8()
                    mp[k if ver >= 246 else k + C0] = (pitch, delta)
            ins.ne = dict(use=use, map=mp)
    return ins


def _sample(r, index):
    name = r.cstr()
    length, _compat, c4 = r.u32(), r.u32(), r.u32()
    depth = r.u8()
    r.take(3)                                     # loop direction, two flag bytes
    r.take(8)                                     # loop start and end
    r.take(16)                                    # presence bitfields
    return Sample(index, name, depth, c4, length, bytes(r.take(r.left())))


# ------------------------------------------------------------------ the play order, row by row
@dataclass
class Walk:
    rows: list              # [(tick, ticks, order, row)]
    end: int                # chipsynth ticks
    loop: object            # tick the song jumps back to, or None when it stops
    tempo: list             # [(tick, bpm)] from the tick rate effects
    once: bool


def _walk(mod, si, report):
    sub = mod.subsongs[si]
    nch = len(sub.orders)
    speeds = list(sub.speeds)
    hz = sub.hz
    factor = sub.timebase + 1
    rows, tempo, seen = [], [], {}
    tick = alt = 0
    order = row = 0
    loop, stop = None, False
    cells = {}                                    # (channel, order) -> {row: Cell}
    for c in range(nch):
        for o in range(sub.orders_len):
            cells[(c, o)] = mod.patterns.get((si, c, sub.orders[c][o]), {})
    for _ in range(sub.orders_len * max(1, sub.pattern_len) + 2):
        if order >= sub.orders_len:
            order = row = 0
        key = (order, row)
        if key in seen:
            loop = seen[key]
            break
        seen[key] = tick
        jump_o = jump_r = None
        for c in range(nch):
            cell = cells[(c, order)].get(row)
            if cell is None:
                continue
            for code, val in cell.fx:
                if code == 0x09 and mod.grooves:
                    if val < len(mod.grooves):
                        speeds, alt = list(mod.grooves[val]), 0
                elif code == 0x09 and val:
                    speeds[0] = val
                elif code == 0x0F and val:
                    speeds[1 if len(speeds) > 1 else 0] = val
                elif 0xC0 <= code <= 0xCF and ((code & 15) << 8 | val):
                    hz = float((code & 15) << 8 | val)
                    tempo.append((tick, hz))
                elif code == 0xF0 and val:
                    hz = val * 0.4
                    tempo.append((tick, hz))
                elif code == 0x0B:
                    jump_o = val
                elif code == 0x0D:
                    jump_r = val
                elif code == 0xFF:
                    stop = True
                elif code in (0xFD, 0xFE):
                    report.drop("the virtual tempo effects (FDxx, FExx)")
        ticks = max(1, speeds[alt % len(speeds)]) * factor * TICKS_PER_FRAME
        alt += 1
        rows.append((tick, ticks, order, row))
        tick += ticks
        if stop:
            break
        if jump_o is not None or jump_r is not None:
            if jump_o is not None:
                order, row = jump_o, jump_r or 0
            else:
                order, row = order + 1, jump_r
            row = min(row, max(0, sub.pattern_len - 1))
        else:
            row += 1
            if row >= sub.pattern_len:
                row, order = 0, order + 1
    return Walk(rows, tick, loop if not stop else None, tempo, stop)


def _bpm(hz, report, what):
    bpm = hz * 60.0 / BEAT_FRAMES
    clamped = int(round(max(30, min(300, bpm))))
    if abs(clamped - bpm) / bpm > 0.01:
        report.approx("%s: the tick rate %g Hz is outside what chipsynth can play (bpm 30 to 300), so it runs at %d bpm" % (what, hz, clamped))
    return clamped


# ------------------------------------------------------------------ names
def _ident(text, used, default="inst"):
    base = re.sub(r"[^A-Za-z0-9_]", "_", text).strip("_") or default
    if not base[0].isalpha():
        base = default + "_" + base
    name, n = base, 1
    while name in used:
        n += 1
        name = "%s_%d" % (base, n)
    used.add(name)
    return name


def _letters(text, used, default="sample"):
    base = re.sub(r"[^A-Za-z]", "_", text).strip("_") or default
    name, n = base, 0
    while name in used:
        n += 1
        s, k = "", n
        while k:
            k, r = divmod(k - 1, 26)
            s = chr(97 + r) + s
        name = base + "_" + s
    used.add(name)
    return name


# ------------------------------------------------------------------ instruments
def _expand(m):
    """A macro as one value a tick: (values, loop, release) with the loop and release as tick indexes or -1."""
    vals = [v for v in m.values for _ in range(m.speed)]
    return vals, (m.loop * m.speed if m.loop is not None else -1), (m.release * m.speed if m.release is not None else -1)


def _sweep(series, loop, label, what, report, fps=60.0):
    """A pulse width (percent a tick) as pws fields, or None when it stays put.  Anything but a repeating rise and fall is approximated."""
    if len(set(series)) <= 1 or max(series) - min(series) < 2:
        return None
    body = series[loop:] if loop >= 0 else series
    lo, hi = min(body), max(body)
    if hi - lo < 2:
        return None
    steps = [b - a for a, b in zip(body, body[1:] + body[:1]) if b != a]
    signs = [1 if s > 0 else -1 for s in steps]
    changes = sum(signs[k] != signs[k - 1] for k in range(len(signs)))
    if loop >= 0 and changes == 2:
        period = len(body)
    else:
        period = 2 * len(body)
        report.approx("instrument '%s': its %s is not a repeating rise and fall; it is played as a back-and-forth sweep" % (label, what))
    wanted = fps / max(1, period) / 0.5
    rate = max(1, min(15, round(wanted)))
    if abs(rate - wanted) > 0.15 * wanted:
        report.approx("instrument '%s': its %s sweep speed is rounded to chipsynth's nearest rate" % (label, what))
    return dict(pws_from=lo, pws_to=hi, pws_rate=rate)


def _percent(width):
    return max(1, min(99, round(width * 100 / 4096)))


def _sid_wave(bits):
    for flag, name in ((8, "noise"), (4, "pulse"), (2, "saw"), (1, "tri")):
        if bits & flag:
            return name, bits & 15 == flag
    return None, False


class _Instruments:
    """Builds the chipsynth instrument a Furnace instrument gives on one kind of channel, once each."""

    def __init__(self, mod, song, hz, report):
        self.mod, self.song, self.hz, self.report = mod, song, hz, report
        self.names, self.used, self.fits = {}, set(), {}
        self.cut_hz = {0x47: 5800.0}

    def get(self, idx, kind, chip, duty=None, arp=None):
        """The name of the instrument for Furnace instrument `idx` (None: none chosen yet) on a `kind` channel of `chip`.
        `duty` is a note-level duty/noise mode from effect 12xx, `arp` an (x, y, ticks per step) from effect 00xy."""
        key = (idx, kind, chip if kind == "sid" else 0, duty, arp)
        if key in self.names:
            return self.names[key]
        ins = self.mod.instruments.get(idx) if idx is not None else None
        if idx is not None and ins is None:
            self.report.drop("a note uses instrument %d, which the file does not have: defaults are used" % idx)
        inst = Instrument()
        inst.wave = {"pulse": "pulse", "gb": "pulse", "tri": "tri", "noise": "noise", "sid": "pulse"}[kind]
        label = (ins.name if ins and ins.name else ("ins%d" % idx if idx is not None else "default"))
        if ins is not None:
            self._build(inst, ins, label, kind, chip, duty)
        elif duty is not None and kind in ("pulse", "gb", "noise"):
            self._duty(inst, label, None, duty, kind)
        if arp is not None:
            x, y, step = arp
            if inst.arp:
                self.report.approx("instrument '%s': an arpeggio effect (00xy) replaces its arpeggio macro" % label)
            inst.arp = (0, x, y)
            inst.arprate = max(1, min(64, round(BEAT_FRAMES / step)))
        suffix = {"tri": "_tri", "noise": "_noise"}.get(kind, "") + ("" if duty is None else "_d%d" % duty) \
            + ("" if arp is None else "_arp%x%x" % arp[:2])
        name = _ident(label + suffix, self.used)
        self.song.instruments[name] = inst
        self.names[key] = name
        return name

    # ---- one instrument
    def _build(self, inst, ins, label, kind, chip, duty):
        want = {"pulse": INS_NES, "tri": INS_NES, "noise": INS_NES, "gb": INS_GB, "sid": INS_C64}[kind]
        if ins.type != want:
            self.report.approx("instrument '%s' is a type %d instrument on a channel that expects type %d: its macros are used, its chip settings are not" % (label, ins.type, want))
        macros = ins.macros
        for m in macros.values():
            if m.kind:
                self.report.drop("instrument '%s': its ADSR or LFO type macro" % label)
            if m.delay:
                self.report.approx("instrument '%s': a macro delay of %d ticks is not kept" % (label, m.delay))
        if kind == "sid":
            self._sid(inst, ins, label, chip)
        else:
            vol = macros.get(0)
            if vol is not None and vol.values and not vol.kind:
                if kind == "tri":
                    self.report.note("instrument '%s': the triangle has no volume, so its volume macro is ignored" % label)
                else:
                    self._volume(inst, label, vol)
            elif kind == "gb" and ins.gb is not None:
                v = ins.gb["env"] & 15
                inst.vol = v or 15
                if ins.gb["env"] >> 5 and not ins.gb["env"] & 16:
                    self.report.approx("instrument '%s': the Game Boy hardware envelope's fall is not kept; the starting volume is used" % label)
            self._duty(inst, label, macros.get(2), duty, kind)
        self._arp(inst, label, macros.get(1))
        if kind != "sid":
            for code, what in ((3, "waveform macro"), (4, "pitch macro"), (5, "extra macros"), (6, "extra macros"),
                               (7, "extra macros"), (15, "extra macros"), (8, "FM macros"), (9, "FM macros"),
                               (10, "FM macros"), (11, "FM macros")):
                m = macros.get(code)
                if m is not None and m.values:
                    self.report.drop("instrument '%s': its %s" % (label, what))

    def _volume(self, inst, label, m):
        vals, loop, rel = _expand(m)
        key = (tuple(vals), loop, rel)
        if key not in self.fits:
            self.fits[key] = fit_volume(vals, loop, rel, len(vals), self.hz)
        fields, err = self.fits[key]
        for k, v in fields.items():
            setattr(inst, k, min(15, v) if k == "vol" else v)
        if err > GOOD:
            self.report.approx("instrument '%s': its volume macro does not fit an attack, decay and sustain (RMS error %.2f); the closest is used" % (label, err))

    def _duty(self, inst, label, m, note_duty, kind):
        if kind == "tri":
            return
        vals, loop, _rel = _expand(m) if m is not None and not m.kind else ([], -1, -1)
        first = note_duty if note_duty is not None else (vals[0] if vals else (2 if kind != "noise" else 0))
        if kind == "noise":
            inst.nmode = "short" if first & 1 else "long"
            if note_duty is None and len(set(v & 1 for v in vals)) > 1:
                self.report.approx("instrument '%s': its noise mode changes during the note; the first is kept" % label)
            return
        inst.pw = DUTY_PW[first & 3]
        if note_duty is None and len(set(vals)) > 1:
            fields = _sweep([DUTY_PW[v & 3] for v in vals], loop, label, "duty macro", self.report, self.hz)
            if fields:
                for k, v in fields.items():
                    setattr(inst, k, v)

    def _arp(self, inst, label, m):
        if m is None or not m.values or m.kind:
            return
        if m.mode or any(abs(v) >= 1 << 29 for v in m.values):
            self.report.drop("instrument '%s': its fixed-note arpeggio macro" % label)
            return
        vals, loop, _rel = _expand(m)
        if not any(vals):
            return
        arp, rate, exact = fit_arpeggio(vals, loop, BEAT_FRAMES)
        if not arp:
            return
        inst.arp, inst.arprate = arp, rate
        if not exact:
            self.report.approx("instrument '%s': its arpeggio macro is not a repeating cycle of 8 steps or fewer; its repeating part is used" % label)

    def _sid(self, inst, ins, label, chip):
        c = ins.c64
        if c is None:
            self.report.approx("instrument '%s' has no C64 settings: pulse with the default envelope is used" % label)
            return
        inst.a, inst.d, inst.s, inst.r = c["ad"] >> 4, c["ad"] & 15, c["sr"] >> 4, c["sr"] & 15
        bits = c["flags1"] & 15
        wm = ins.macros.get(3)
        if wm is not None and wm.values and not wm.kind:
            vals = _expand(wm)[0]
            bits = vals[0] & 15
            if len(set(v & 15 for v in vals)) > 1:
                self.report.approx("instrument '%s': its waveform changes during the note; the first waveform is kept" % label)
        wave, exact = _sid_wave(bits)
        if wave is None:
            self.report.approx("instrument '%s': no waveform is switched on, so pulse is used" % label)
            wave = "pulse"
        elif not exact:
            self.report.approx("instrument '%s': its waveform combines waves, so %s is used" % (label, wave))
        inst.wave = wave
        inst.pw = _percent(c["duty"] & 0xFFF)
        dm = ins.macros.get(2)
        if dm is not None and dm.values and not dm.kind:
            if c["flags1"] & 0x80:
                vals, loop, _r = _expand(dm)
                fields = _sweep([_percent(v & 0xFFF) for v in vals], loop, label, "pulse width macro", self.report, self.hz)
                if fields:
                    for k, v in fields.items():
                        setattr(inst, k, v)
                else:
                    inst.pw = _percent(vals[0] & 0xFFF)
            else:
                self.report.drop("instrument '%s': its relative pulse width macro" % label)
        if c["flags1"] & 0x10:                    # to filter
            mode = "lp" if c["flags2"] & 1 else "bp" if c["flags2"] & 4 else "hp" if c["flags2"] & 2 else ""
            if mode:
                inst.filt = mode
                hz = 30.0 + self.cut_hz.get(chip, 12000.0) * c["cutoff"] / 2048.0
                inst.filt_cut = max(0, min(15, round(15 * math.log(max(hz, 100.0) / 100.0) / math.log(80.0))))
                inst.filt_res = c["res"]
                self.report.approx("the SID filter is shared by its three voices; chipsynth filters each instrument on its own")
        if c["flags2"] & 0xC0:
            self.report.drop("instrument '%s': ring modulation / oscillator sync" % label)
        for code, what in ((5, "filter mode"), (6, "resonance"), (7, "filter toggle"), (8, "cutoff")):
            if ins.macros.get(code) is not None and ins.macros[code].values:
                self.report.drop("instrument '%s': its %s macro" % (label, what))
        for code, what in ((0, "volume (global on the SID)"), (4, "pitch"), (15, "special")):
            if ins.macros.get(code) is not None and ins.macros[code].values:
                self.report.drop("instrument '%s': its %s macro" % (label, what))


# ------------------------------------------------------------------ DPCM samples
def _dpcm_rate_for(note):
    return min(range(16), key=lambda k: abs(DPCM_NOTE_TABLE[k] - note))


class _Samples:
    def __init__(self, mod, report):
        self.mod, self.report = mod, report
        self.built, self.used = {}, set()

    def play(self, ins, note):
        """The chipsynth sample name that note `note` plays on the DPCM channel through instrument `ins`, or None."""
        if ins is None or ins.sm is None:
            return None
        play, smp = note, None
        if ins.sm["flags"] & 1:
            got = ins.sm["map"].get(note)
            if got is None:
                return None
            play, smp = got
        elif ins.sm["sample"] != 0xFFFF:
            smp = ins.sm["sample"]
        if smp is None:
            return None
        s = self.mod.samples.get(smp)
        if s is None:
            self.report.drop("a DPCM note uses sample %d, which the file does not have" % smp)
            return None
        if s.depth == 1:                                  # hardware DPCM: the note picks one of the 16 rates, or the instrument does
            rate = None
            if ins.ne and ins.ne["use"]:
                got = ins.ne["map"].get(note)
                if got and got[0] < 16:
                    rate = got[0]
            return self._dpcm(s, rate if rate is not None else _dpcm_rate_for(play))
        if s.depth in (8, 16):                            # PCM: the note is a speed relative to the sample's C-4 rate
            return self._pcm(s, max(-48, min(36, play - DPCM_NOTE_TABLE[7])))
        self.report.drop("sample '%s' is stored as sample type %d, which is not read" % (s.name, s.depth))
        return None

    def _dpcm(self, s, rate):
        key = (s.index, rate)
        if key not in self.built:
            parts = dpcm.split_parts(s.data)
            if len(parts) > dpcm.MAX_PARTS:
                self.report.drop("sample '%s' is longer than a chipsynth sample can be" % s.name)
                self.built[key] = (None, None)
            else:
                name = _letters(s.name or "sample", self.used)
                self.built[key] = (name, dpcm.Sample(name=name, rate=rate, tracks=[parts]))
        return self.built[key][0]

    def _pcm(self, s, semis):
        key = (s.index, "pcm", semis)
        if key in self.built:
            return self.built[key][0]
        self.built[key] = (None, None)
        try:
            import numpy as np
            if s.depth == 8:
                x = np.frombuffer(s.data, dtype=np.int8).astype(np.float64) / 128.0
            else:
                x = np.frombuffer(s.data[:len(s.data) // 2 * 2], dtype="<i2").astype(np.float64) / 32768.0
            hz = max(1.0, s.rate * 2.0 ** (semis / 12.0))
            smp, _notes = dpcm.convert_wav(x, hz, dpcm.ImportOptions(rate="auto"), "")
        except (dpcm.DpcmError, ValueError) as e:
            self.report.drop("sample '%s' could not be turned into a DPCM sample (%s)" % (s.name, e))
            return None
        name = _letters(s.name or "sample", self.used)
        smp.name = name
        self.built[key] = (name, smp)
        self.report.approx("sample '%s' is PCM; it is converted to DPCM at rate %d" % (s.name, smp.rate))
        return name

    @property
    def samples(self):
        return {n: s for n, s in self.built.values() if n}


# ------------------------------------------------------------------ the conversion
@dataclass
class Imported:
    name: str
    song: sg.Song
    samples: dict


@dataclass
class Result:
    project_name: str
    songs: list
    report: Report


def load(data, song=None):
    """Module bytes -> Result.  `song` picks one subsong by number (1-based) or name; the default is all of them."""
    mod = parse(data)
    report = Report()
    report.note("%s, format version %d" % (version_name(mod.version), mod.version))
    report.note("chips: %s" % ", ".join(CHIPS.get(c, ("chip 0x%02x" % c,))[0] for c, _n in mod.chips))
    if mod.version > MAX_VERIFIED:
        report.approx("format version %d is newer than the files this importer was checked against (%d); it is read as the format document says" % (mod.version, MAX_VERIFIED))
    if abs(mod.tuning - 440.0) > 0.5:
        report.approx("the tuning is A-4 = %g Hz; chipsynth plays A4 at 440 Hz" % mod.tuning)
    picks = list(range(len(mod.subsongs)))
    if song is not None:
        picks = [i for i, s in enumerate(mod.subsongs) if str(i + 1) == str(song) or (s.name and s.name == str(song))]
        if not picks:
            raise FurnaceError("no song '%s' (the file has %d: %s)" % (song, len(mod.subsongs),
                                                                      ", ".join(s.name or "song %d" % (i + 1) for i, s in enumerate(mod.subsongs))))
    out = [_convert(mod, i, report) for i in picks]
    return Result(mod.name, out, report)


def _plan(mod):
    """[(channel, chip id, channel in chip, kind or None)] for every channel of the module."""
    out, ch = [], 0
    for cid, n in mod.chips:
        kinds = SUPPORT.get(cid)
        for k in range(n):
            out.append((ch + k, cid, k, kinds[k] if kinds and k < len(kinds) else None))
        ch += n
    return out


def _used(mod, si, ch):
    return any(c.note is not None for (s, c2, _p), rows in mod.patterns.items() if s == si and c2 == ch for c in rows.values())


def _convert(mod, si, report):
    sub = mod.subsongs[si]
    name = sub.name or mod.name or "Song"
    title = name if len(mod.subsongs) == 1 or sub.name else "%s %d" % (name, si + 1)
    label = "song '%s'" % title
    walk = _walk(mod, si, report)
    plan = _plan(mod)
    tracks, kinds = [], []                       # (channel, track name, kind, chip)
    nv = nd = 0
    skipped = {}
    for ch, cid, k, kind in plan:
        if kind is None:
            if _used(mod, si, ch):
                why = SKIP_WHY.get((cid, k)) or "the %s chip is not one chipsynth plays" % CHIPS.get(cid, ("chip 0x%02x" % cid,))[0]
                skipped.setdefault(why, []).append(ch + 1)
            continue
        if kind == "dpcm":
            if nd >= MAX_DACS:
                report.drop("%s: channel %d (more DAC tracks than chipsynth has)" % (label, ch + 1))
                continue
            nd += 1
            tracks.append((ch, "D%d" % nd, kind, cid))
        else:
            if nv >= MAX_VOICES:
                report.drop("%s: channel %d (more voices than chipsynth has)" % (label, ch + 1))
                continue
            nv += 1
            tracks.append((ch, "V%d" % nv, kind, cid))
    for why, chans in skipped.items():
        report.drop("%s: channel%s %s (%s)" % (label, "s" if len(chans) > 1 else "", ", ".join(map(str, chans)), why))
    if not tracks:
        raise FurnaceError("no channel in this file can be imported: it holds %s" % ", ".join(CHIPS.get(c, ("chip 0x%02x" % c,))[0] for c, _n in mod.chips))
    s = sg.Song(title=title, author=mod.author, bpm=_bpm(sub.hz, report, label), voices=max(1, nv), dacs=nd)
    if mod.comment.strip():
        s.comment = " ".join(mod.comment.split())[:200]
    s.once = walk.once
    s.tracks = {}
    s.__post_init__()
    ins = _Instruments(mod, s, sub.hz, report)
    smp = _Samples(mod, report)
    ctx = _Channels(mod, si, walk, ins, smp, report, sub.hz, label)
    first = first_track = True
    for ch, tname, kind, cid in tracks:
        events = ctx.channel(ch, kind, cid)
        tempo = [(t, _bpm(hz, report, label)) for t, hz in walk.tempo] if first and tname.startswith("V") else []
        if first and tname.startswith("V"):
            first = False
        s.tracks[tname] = _to_events(events, walk.end, walk.loop if first_track else None, tempo)
        first_track = False
    if walk.tempo and first:
        report.drop("%s: tick rate changes (there is no voice track to carry them)" % label)
    s.__post_init__()
    if walk.once:
        report.note("%s: it ends with a stop song effect (FFxx), so it plays once" % label)
    ctx.finish_report()
    return Imported(title, s, smp.samples)


class _Channels:
    """Turns a channel's cells into timed events."""

    def __init__(self, mod, si, walk, ins, smp, report, hz, label):
        self.mod, self.si, self.walk, self.ins, self.smp, self.report, self.hz, self.label = mod, si, walk, ins, smp, report, hz, label
        self.dropped = {}
        self.pal = hz < 55
        self.sub = mod.subsongs[si]
        self.low_note = False

    def finish_report(self):
        for chan, names in self.dropped.items():
            self.report.drop("%s: these effects on %s are not carried over: %s" % (self.label, chan, ", ".join(sorted(names))))

    def _drop(self, kind, code):
        name = EFFECT_NAMES.get(code) or "effect %02X" % code
        self.dropped.setdefault(kind, set()).add(name)

    def channel(self, ch, kind, cid):
        sub, walk, report = self.sub, self.walk, self.report
        events = []
        inst_cur, vol_cur, duty, arp, arp_step = None, 15, None, None, 1
        for tick0, rticks, order, row in walk.rows:
            pat = self.mod.patterns.get((self.si, ch, sub.orders[ch][order]))
            cell = pat.get(row) if pat else None
            if cell is None:
                continue
            delay = cut = None
            for code, val in cell.fx:
                if code == 0x00:
                    arp = (val >> 4, val & 15, arp_step) if val else None
                elif code == 0xE0:
                    arp_step = max(1, val)
                    if arp:
                        arp = arp[:2] + (arp_step,)
                elif code == 0xEC:
                    cut = val
                elif code == 0xED:
                    delay = val
                elif code == 0x12 and kind in ("pulse", "gb", "noise"):
                    duty = val & 3 if kind != "noise" else val & 1
                elif code in (0x09, 0x0B, 0x0D, 0x0F, 0xFF, 0xF0, 0xFD, 0xFE) or 0xC0 <= code <= 0xCF:
                    pass                       # the whole song's: taken in the walk
                else:
                    self._drop("the %s channels" % ("NES" if cid == 0x06 else "SID" if kind == "sid" else "Game Boy"), code)
            if cell.ins is not None:
                inst_cur = cell.ins
            if cell.vol is not None and kind != "tri":
                vol_cur = min(15, cell.vol)
            note = cell.note
            if note is None:
                if cell.vol is not None and kind != "tri" and cell.note is None:
                    self.report.approx("a volume change in the middle of a note is applied from the next note")
                continue
            t = tick0
            if delay is not None:
                if delay * TICKS_PER_FRAME >= rticks:
                    report.approx("%s: a note delayed past its row is dropped" % self.label)
                    continue
                t += delay * TICKS_PER_FRAME
            if note in (NOTE_OFF, NOTE_RELEASE):
                events.append((t, "stop"))
                continue
            if note == MACRO_RELEASE:
                report.approx("%s: a macro release is not kept; the note goes on" % self.label)
                continue
            if note == RAW_FREQ:
                report.drop("%s: a raw frequency note" % self.label)
                continue
            if note > 179:
                continue
            end_cut = t + cut * TICKS_PER_FRAME if cut is not None else None
            if kind == "dpcm":
                name = self.smp.play(self.mod.instruments.get(inst_cur), note)
                if name is None:
                    report.drop("%s: a DPCM note with no sample, or one that could not be read" % self.label)
                if name:
                    events.append((t, "play", name, end_cut))
                else:
                    events.append((t, "stop"))                 # nothing plays, but the note still ends the sample before it
                continue
            if kind == "noise" and cid == 0x06:
                pitch = noise_pitch((note - C0) & 15, self.pal)
            else:
                if note < C0:
                    self.low_note = True
                    report.approx("%s: notes below C-0 are played at C-0" % self.label)
                pitch = max(0, min(107, note - C0))
            name = self.ins.get(inst_cur, kind, cid, duty, arp if kind != "noise" else None)
            events.append((t, "note", pitch, name, vol_cur if kind != "tri" else None, end_cut))
        return events


def _to_events(events, end, loop, tempo):
    """Timed events -> MML-model events: notes run to the next event (or their cut), rests between, one voice at a time.
    `tempo` is [(tick, bpm)]: a note that spans one is split with an Extend, because a Tempo needs a place between events."""
    events = sorted(events, key=lambda e: e[0])
    timed = []                                    # (tick, kind, payload...)
    for i, ev in enumerate(events):
        tick, kind = ev[0], ev[1]
        nxt = events[i + 1][0] if i + 1 < len(events) else end
        if kind == "stop":
            timed.append((tick, "stop"))
            continue
        cut = ev[-1]
        stop = min(nxt, end) if cut is None else min(nxt, end, cut)
        if stop <= tick:
            continue
        if kind == "play":
            timed.append((tick, "play", ev[2], stop - tick))
        else:
            timed.append((tick, "note", ev[2], stop - tick, ev[3], ev[4]))
    out, pos, octave, inst, vol = [], 0, 4, None, None
    loop_tick = loop if loop else None
    loop_done = loop_tick is None
    pending = sorted(tempo)

    def advance(t):
        nonlocal pos, loop_done
        if not loop_done and loop_tick <= t:
            if loop_tick > pos:
                out.append(sg.Rest(loop_tick - pos))
                pos = loop_tick
            out.append(sg.Loop())
            loop_done = True
        if t > pos:
            out.append(sg.Rest(t - pos))
            pos = t

    def tempos_until(t):
        """Emit tempo events at ticks <= t (outside any note, which are split by the caller)."""
        while pending and pending[0][0] <= t and pending[0][0] >= pos:
            tt, bpm = pending.pop(0)
            advance(tt)
            out.append(sg.Tempo(bpm))
        while pending and pending[0][0] < pos:
            pending.pop(0)                        # inside a note that was not split (a play event): dropped

    for ev in timed:
        tick, kind = ev[0], ev[1]
        if tick < pos:
            continue
        tempos_until(tick)
        if kind == "stop":
            advance(tick)
            continue
        advance(tick)
        if kind == "play":
            out.append(sg.Play(ev[2], ev[3]))
            pos = tick + ev[3]
            continue
        _, _, pitch, dur, name, v = ev
        if name != inst:
            inst = name
            out.append(sg.Select(name))
        if v is not None and v != vol:
            vol = v
            out.append(sg.Volume(value=v))
        if pitch // 12 != octave:
            octave = pitch // 12
            out.append(sg.Octave(set=octave))
        letter, acc = LETTERS[pitch % 12]
        here, stop = tick, tick + dur
        while pending and here < pending[0][0] < stop:        # split the note at each tempo change inside it
            tt, bpm = pending.pop(0)
            out.append(sg.Note(letter, acc, tt - here) if here == tick else sg.Extend(tt - here))
            out.append(sg.Tempo(bpm))
            here = tt
        out.append(sg.Note(letter, acc, stop - here) if here == tick else sg.Extend(stop - here))
        pos = stop
    tempos_until(end)
    advance(end)
    if not loop_done:
        out.append(sg.Loop())
    return out
