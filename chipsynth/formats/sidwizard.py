# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""SID-Wizard `.swm` ("SWM1") songs -> chipsynth songs.

Written from the file format as SID-Wizard 1.97 defines it (SWM-spec.src, SWMconvert.c and player.asm, read only, WTF
licence; and pysidwizard's reader, Apache-2.0, read only), nothing copied.  PLAN-FORMATS.md section 4 lists what was read
and where the two sources agree.

The three SID voices become V1-V3, with notes, rests, gate-offs (note column `$7E`), the three orderlists (pattern
numbers, transpose, per-channel tempo, the loop position), tempo (the subtune's single or funk tempo, and the tempo
pattern effects `$10`, `$11`, `$13`, `$14`), and per instrument the ADSR, the first waveform, the starting pulse width,
a wave-table arpeggio, and a pulse-width sweep.  Everything else is listed in the Report, not guessed.

Timing: the player runs `frame speed` calls to the video frame (header byte 4, 1-8) and a tempo is a number of player
calls a row lasts.  One video frame is 4 ticks (96 ticks to the quarter note), so a player call is 4/N ticks and a song is
exact to the tick when N is 1, 2 or 4.  The file does not record PAL or NTSC: `region` says (default pal).
"""
import math
import re
from dataclasses import dataclass, field

from .. import song as sg
from ..synth import Instrument
from .envelope import fit_arpeggio
from .report import Report

PAL_HZ = 50.0
NTSC_HZ = 60.0
MAX_FRAMES = 60 * 60 * 20 * 4           # a safety stop: twenty minutes of player calls at 60 Hz, x4 multispeed
FIRST_PASS_CAP = 60 * 60 * 20           # the longest a song may play before its loop (player calls)

MAGICS = (b"SWM1",)
P00_MAGIC = b"C64File\x00"
P00_HEADER = 0x1A
HEADER = 64
PACKED_MIN, PACKED_MAX = 0x70, 0x77
GATEOFF_FX, GATEON_FX = 0x7E, 0x7D
LEGATO = 0x3F
LETTERS = (("c", 0), ("c", 1), ("d", 0), ("d", 1), ("e", 0), ("f", 0), ("f", 1), ("g", 0), ("g", 1), ("a", 0), ("a", 1), ("b", 0))
TEMPO_FX, FUNK_FX, PROGRAM_FX, TRACK_TEMPO_FX, TRACK_FUNK_FX, TRACK_PROGRAM_FX = 0x10, 0x11, 0x12, 0x13, 0x14, 0x15
BIGFX_NAMES = {
    0x01: "pitch slide up", 0x02: "pitch slide down", 0x03: "tone portamento", 0x04: "set waveform control",
    0x05: "set attack/decay", 0x06: "set sustain/release", 0x07: "select chord", 0x08: "set vibrato",
    0x09: "jump in wave table", 0x0A: "jump in pulse table", 0x0B: "jump in filter table", 0x0C: "set arpeggio speed",
    0x0D: "detune", 0x0E: "set pulse width", 0x0F: "set filter cutoff", 0x12: "tempo program",
    0x15: "track tempo program", 0x16: "select vibrato type", 0x1C: "shift filter cutoff", 0x1D: "track delay",
    0x1E: "note delay", 0x1F: "set filter control",
}
SMALLFX_NAMES = {
    0x2: "set attack", 0x3: "set decay", 0x4: "set waveform", 0x5: "set sustain (note volume)", 0x6: "set release",
    0x7: "select chord", 0x8: "set vibrato amplitude", 0x9: "set vibrato period", 0xA: "set main volume",
    0xB: "set filter band", 0xC: "set arpeggio speed", 0xD: "detune", 0xE: "set waveform control", 0xF: "set filter resonance",
}
NOTEFX_NAMES = {0x78: "auto-portamento", 0x79: "sync on", 0x7A: "sync off", 0x7B: "ring on", 0x7C: "ring off", 0x7D: "gate on",
                0x7F: "note effect"}
TUNINGS = {1: "432 Hz (Verdi)", 2: "just intonation for C"}


class SidWizardError(ValueError):
    pass


@dataclass
class Row:
    note: int = 0           # 0 none, 1-95 note, 0x60-0x6F vibrato amplitude, 0x78-0x7F note effects
    inst: int = 0           # 1-62 instrument, 0x3F legato, 0x40-0x7F small effects
    fx: int = 0             # 1-0x1F big effect (with `val`), 0x20-0xFF small effect
    val: int = 0


@dataclass
class Pattern:
    rows: list
    length: int             # the editor's row count (the player plays the rows the data holds)


@dataclass
class Instr:
    name: str
    raw: bytes              # the whole instrument: 16 header bytes, then the wave, pulse and filter tables, each ended by $FF

    @property
    def ctrl(self):
        return self.raw[0]

    @property
    def ad(self):
        return self.raw[3]

    @property
    def sr(self):
        return self.raw[4]

    @property
    def vibrato(self):
        return self.raw[5]

    @property
    def vibdelay(self):
        return self.raw[6]

    @property
    def arpspeed(self):
        return self.raw[7]

    @property
    def chord(self):
        return self.raw[8]

    @property
    def octave(self):
        v = self.raw[9]
        return v - 256 if v >= 128 else v

    @property
    def pw_ptr(self):
        return self.raw[10]

    @property
    def filt_ptr(self):
        return self.raw[11]

    @property
    def firstwave(self):
        return self.raw[15]


@dataclass
class Tune:
    """What a `.swm` file holds, undigested."""
    magic: str
    frame_speed: int
    default_pattern_len: int
    driver: int
    tuning: int
    author: str
    name: str                # the PC64 (.P00) name, or ""
    sequences: list          # orderlist bytes; subtune s, channel c is sequences[3 * s + c]
    patterns: list           # pattern n (1-based) is patterns[n - 1]
    instruments: list        # instrument n (1-based) is instruments[n - 1]
    chords: bytes
    tempos: bytes
    subtune_tempos: list     # (left, right) per subtune

    @property
    def subtunes(self):
        return 1 if not self.sequences else (len(self.sequences) - 1) // 3 + 1


# ------------------------------------------------------------------ reading the file
def _layout(data):
    """(offset of the SWM1 header, the P00 name or "") or None."""
    data = bytes(data)
    if data[:8] == P00_MAGIC:
        at = P00_HEADER + 2
        if data[at:at + 4] in MAGICS:
            return at, data[8:24].split(b"\0", 1)[0].decode("latin-1").strip()
        return None
    for at in (0, 2):                           # a bare module, or a PRG with its two-byte load address
        if data[at:at + 4] in MAGICS:
            return at, ""
    return None


def looks_like(data):
    return _layout(data) is not None


def _text(raw):
    s = "".join(chr(b) if 32 <= b < 127 else " " for b in raw)
    return s.strip()


def unpack(packed, prev=0):
    """Expand the packed empty rows of a pattern: a byte $70-$77 straight after a $00 (or another such byte) stands for
    2-9 further $00 bytes.  `prev` is the byte before the first one (the converters read it from the file, whatever it is)."""
    out = bytearray()
    for b in packed:
        if PACKED_MIN <= b <= PACKED_MAX and (prev == 0 or PACKED_MIN <= prev <= PACKED_MAX):
            out += b"\0" * (b - PACKED_MIN + 2)
        else:
            out.append(b)
        prev = b
    return bytes(out)


def _rows(stream, where):
    """The bytes of an unpacked pattern -> rows: a note byte; if its bit 7 is set an instrument byte; if that one's bit 7
    is set an effect byte; an effect below $20 takes a value byte."""
    rows, i, n = [], 0, len(stream)
    while i < n:
        b = stream[i]
        i += 1
        row = Row(note=b & 0x7F)
        if b & 0x80:
            if i >= n:
                raise SidWizardError("%s ends in the middle of a row" % where)
            c = stream[i]
            i += 1
            row.inst = c & 0x7F
            if c & 0x80:
                if i >= n:
                    raise SidWizardError("%s ends in the middle of a row" % where)
                row.fx = stream[i]
                i += 1
                if row.fx < 0x20:
                    if i >= n:
                        raise SidWizardError("%s ends in the middle of a row" % where)
                    row.val = stream[i]
                    i += 1
        rows.append(row)
    return rows


def parse(data):
    """`.swm` bytes -> Tune.  The data is laid out front to back as: the 64-byte header, the sequences, the patterns, the
    instruments, the chord table, the tempo table, the subtunes' tempos; each sequence, pattern and instrument is followed
    by its size, so it is read from the end."""
    data = bytes(data)
    found = _layout(data)
    if found is None:
        raise SidWizardError("not a SID-Wizard song (expected SWM1 at the start, after a PRG load address, or in a .P00 file)")
    at, p00name = found
    if len(data) < at + HEADER:
        raise SidWizardError("the file ends early (shorter than the 64-byte header)")
    h = data[at:at + HEADER]
    seq_n, pat_n, ins_n, chord_n, tempo_n = h[12], h[13], h[14], h[15], h[16]
    start = at + HEADER
    cur = len(data)
    subtunes = 1 if seq_n == 0 else (seq_n - 1) // 3 + 1

    def back(n, what):
        nonlocal cur
        if cur - n < start:
            raise SidWizardError("the file ends early (no room for %s)" % what)
        cur -= n
        return data[cur:cur + n]

    st = back(2 * subtunes, "the subtune tempos")
    subtune_tempos = [(st[2 * i], st[2 * i + 1]) for i in range(subtunes)]
    tempos = back(tempo_n, "the tempo table")
    chords = back(chord_n, "the chord table")
    instruments = []
    for k in range(ins_n):
        name = _text(back(8, "instrument %d's name" % (k + 1)))
        size = back(1, "instrument %d's size" % (k + 1))[0]
        body = back(size, "instrument %d" % (k + 1))
        raw = bytes(body) + b"\xff"            # the size byte stands where the last table's $FF was
        if len(raw) < 17:
            raise SidWizardError("instrument %d is too short (%d bytes)" % (k + 1, size))
        pw, fl = raw[10], raw[11]
        if not 16 <= pw <= fl <= len(raw):
            raise SidWizardError("instrument %d: its pulse and filter table pointers (%d, %d) are not inside it" % (k + 1, pw, fl))
        instruments.append(Instr(name, raw))
    instruments.reverse()
    patterns = []
    for k in range(pat_n):
        length = back(1, "pattern %d's length" % (k + 1))[0]
        size = back(1, "pattern %d's size" % (k + 1))[0]
        if size == 0:                           # an unused slot: empty but lengthy
            patterns.append(Pattern([], length))
            continue
        end = cur
        j = size - 2                            # the unpacked body is size-1 bytes, then the $FF that stands for the size
        while j >= 0:
            b = back(1, "pattern %d" % (k + 1))[0]
            prev = data[cur - 1] if cur - 1 >= start else 0
            if PACKED_MIN <= b <= PACKED_MAX and (prev == 0 or PACKED_MIN <= prev <= PACKED_MAX):
                j -= b - PACKED_MIN + 1
            j -= 1
        stream = unpack(data[cur:end], data[cur - 1] if cur - 1 >= start else 0)
        patterns.append(Pattern(_rows(stream, "pattern %d" % (k + 1)), length))
    patterns.reverse()
    sequences = []
    for k in range(seq_n):
        size = back(1, "sequence %d's size" % k)[0]
        sequences.append(bytes(back(size, "sequence %d" % k)))
    sequences.reverse()
    if cur != start:
        raise SidWizardError("the data does not add up to the header's counts (%d bytes left over)" % (cur - start))
    return Tune("SWM1", h[4], h[11], h[19], h[20], _text(h[24:64]), p00name, sequences, patterns, instruments,
                bytes(chords), bytes(tempos), subtune_tempos)


# ------------------------------------------------------------------ tempo
def _tempo_of(pair):
    """A subtune's tempo bytes -> the row lengths in player calls, repeating.  Bit 7 of the first byte is a single tempo;
    otherwise it alternates with the second (whose bit 7 only ends the program)."""
    left, right = pair
    if left & 0x80:
        return [left & 0x7F]
    return [left & 0x7F, right & 0x7F]


# ------------------------------------------------------------------ playing the lists
class _Chan:
    """One channel's orderlist walker (the sequencer in player.asm)."""

    def __init__(self, data, patterns, default_len, report, ch):
        self.data, self.patterns, self.default_len, self.report, self.ch = data, patterns, default_len, report, ch
        self.pos = 0
        self.trans = 0
        self.tempo = None                 # a per-channel tempo set by the orderlist, taken at the next row
        self.done = False
        self.visited = {}                 # orderlist position -> frame it was first reached
        self.loop = None                  # (frame of the loop target, frame the loop was taken)
        self.looped = False
        self.end_frame = None
        self.idle = 0

    def fetch(self, frame):
        """The rows of the next pattern, or None when the channel is finished."""
        d = self.data
        while True:
            if self.pos >= len(d):
                self.report.note("channel %d's orderlist has no end marker; it ends there" % (self.ch + 1))
                return self._end(frame)
            self.visited.setdefault(self.pos, frame)
            b = d[self.pos]
            if b < 0x80:
                self.pos += 1
                self.idle = 0
                if b == 0 or b > len(self.patterns):
                    if b:
                        self.report.drop("channel %d: the orderlist names pattern %d, which does not exist" % (self.ch + 1, b))
                    return [Row()] * max(1, self.default_len)
                pat = self.patterns[b - 1]
                if not pat.rows:
                    return [Row()] * max(1, pat.length or self.default_len)
                return pat.rows
            if b == 0xFE:
                return self._end(frame)
            if b == 0xFF:
                if self.pos + 1 >= len(d):
                    return self._end(frame)
                target = d[self.pos + 1]
                if target >= 0x80:
                    self.report.drop("channel %d: a jump to another subtune at the end of its orderlist (the channel ends there)" % (self.ch + 1))
                    return self._end(frame)
                self.idle += 1
                if self.idle > 2 or target not in self.visited:
                    self.report.note("channel %d's orderlist loops to position %d, which plays nothing" % (self.ch + 1, target))
                    return self._end(frame)
                if self.loop is None:
                    self.loop = (self.visited[target], frame)
                self.looped = True
                self.pos = target
                continue
            self.pos += 1
            if b <= 0x9F:
                self.trans = b - 0x90
            elif b <= 0xAF:
                self.report.drop("orderlist main volume (seen on channel %d)" % (self.ch + 1))
            elif b <= 0xEF:
                self.tempo = b - 0xB0

    def _end(self, frame):
        self.done = True
        self.end_frame = frame
        return None


def _simulate(tune, sub, until, report):
    """Play the three channels row by row.  Returns (rows per channel, channels, end frame).  A row is
    (frame, Row, transpose).  With `until` None it stops once every channel has ended or taken its loop once; with a frame
    number it plays on (channels that loop wrap round) to that frame."""
    seq = tune.sequences
    chans = []
    for c in range(3):
        i = 3 * sub + c
        data = seq[i] if i < len(seq) else b"\xfe"
        chans.append(_Chan(data, tune.patterns, tune.default_pattern_len or 32, report, c))
    base = _tempo_of(tune.subtune_tempos[sub] if sub < len(tune.subtune_tempos) else (0x86, 0x86))
    prog = [list(base) for _ in range(3)]
    idx = [0, 0, 0]
    cnt = [0, 0, 0]
    cur = [None, None, None]
    ri = [0, 0, 0]
    rows = [[], [], []]
    frame = 0
    while frame < MAX_FRAMES:
        if until is None:
            if all(ch.done or ch.looped for ch in chans):
                break
            if frame > FIRST_PASS_CAP:
                report.note("the song was cut at twenty minutes (no end or loop was found)")
                break
        elif frame >= until:
            break
        for c in range(3):
            ch = chans[c]
            if ch.done:
                continue
            if cnt[c] >= max(1, prog[c][idx[c]]):
                idx[c] = (idx[c] + 1) % len(prog[c])
                cnt[c] = 0
            if cnt[c] == 0:
                if cur[c] is None or ri[c] >= len(cur[c]):
                    cur[c] = ch.fetch(frame)
                    ri[c] = 0
                    if cur[c] is None:
                        continue
                    if ch.tempo is not None:
                        prog[c], idx[c] = [ch.tempo], 0
                        ch.tempo = None
                row = cur[c][ri[c]]
                ri[c] += 1
                if row.fx in (TEMPO_FX, FUNK_FX, TRACK_TEMPO_FX, TRACK_FUNK_FX):
                    if row.fx in (TEMPO_FX, TRACK_TEMPO_FX):
                        new = [row.val & 0x7F]
                    else:
                        new = [row.val >> 4, row.val & 0x0F]
                    for k in (range(3) if row.fx in (TEMPO_FX, FUNK_FX) else (c,)):
                        prog[k], idx[k] = list(new), 0
                if row.note or row.inst or row.fx:
                    rows[c].append((frame, row, ch.trans))
            cnt[c] += 1
        frame += 1
    end = frame
    if until is None:
        end = max([ch.end_frame for ch in chans if ch.done] + [ch.loop[1] for ch in chans if ch.loop] + [0]) or frame
    return rows, chans, end


# ------------------------------------------------------------------ instruments
@dataclass
class Timing:
    fps: float              # player calls a second (the video rate times the frame speed)
    beat: int               # player calls in a quarter note


def _wave_of(bits):
    """SID waveform bits -> (chipsynth wave, exact).  Noise beats pulse beats saw beats triangle."""
    w = bits & 0xF0
    for flag, name in ((0x80, "noise"), (0x40, "pulse"), (0x20, "saw"), (0x10, "tri")):
        if w & flag:
            return name, w == flag
    return None, False


def _percent(width):
    return max(1, min(99, round(width * 100 / 4096)))


def _table_rows(raw, start, stop):
    """The (offset, b0, b1, b2) rows of a table that starts at `start` in an instrument, up to its $FF or `stop`."""
    out, i = [], start
    while i < stop and raw[i] != 0xFF:
        if i + 3 > stop:
            break
        out.append((i, raw[i], raw[i + 1], raw[i + 2]))
        i += 3
    return out


def _wave_run(ins, frames=400):
    """Run the wave/arpeggio table from a note's first call, as the player does.  Returns (pitch offsets a call, waveform
    bytes a call, set of flags) where the flags say what the table does that a fit cannot carry."""
    raw = ins.raw
    end = ins.pw_ptr
    flags = set()
    pos, cnt, pitch, wave = 16, -1, 0, None
    speed = ins.arpspeed & 0x3F
    pitches, waves = [], []
    for _ in range(frames):
        cnt -= 1
        if cnt < 0:
            cnt = speed
            b0 = raw[pos] if pos < end else 0xFF
            if b0 == 0xFE:
                target = raw[pos + 1] if pos + 1 < end else 0xFF
                if target < 16 or target >= end or (target - 16) % 3 or raw[target] >= 0xFE or raw[target] < 0x10:
                    flags.add("jump")
                    b0 = 0xFF
                else:
                    pos = target
                    b0 = raw[pos]
            if b0 != 0xFF and pos + 3 > end - 1:
                flags.add("jump")
                b0 = 0xFF
            if b0 != 0xFF:
                if b0 < 0x10:
                    cnt = b0
                else:
                    wave = b0
                p, det = raw[pos + 1], raw[pos + 2]
                pos += 3
                if det not in (0xFF, 0):
                    flags.add("detune")
                if p == 0x7F:
                    flags.add("chord")
                elif p == 0x80:
                    pass
                elif p < 0x7F:
                    pitch = p
                elif p >= 0xE0:
                    pitch = p - 256
                else:
                    flags.add("absolute")
        pitches.append(pitch)
        waves.append(wave)
    return pitches, waves, flags


def _period(series, limit=128):
    """The shortest P with series[t] == series[t + P] all the way along (so the series is periodic from its start), or None."""
    n = len(series)
    for p in range(1, min(limit, n // 2) + 1):
        if all(series[t] == series[t + p] for t in range(n - p)):
            return p
    return None


def _wave_program(ins, timing):
    """Returns (arpeggio (offsets, rate) or None, reasons it was not carried, approximations, waveform bytes seen)."""
    pitches, waves, flags = _wave_run(ins)
    seen = sorted({w for w in waves if w is not None})
    why, approx = [], []
    if "chord" in flags:
        why.append("it plays a chord")
    if "absolute" in flags:
        why.append("it uses absolute notes")
    if "detune" in flags:
        why.append("it detunes")
    if "jump" in flags:
        why.append("it jumps to a row that is not a waveform row")
    if why:
        return None, why, approx, seen
    if not any(pitches):
        return None, why, approx, seen
    p = _period(pitches)
    if p is None:
        if pitches[-1] != 0 and len(set(pitches[40:])) == 1:
            why.append("it ends on a different pitch")
        else:
            why.append("it plays some rows once before it settles")
        return None, why, approx, seen
    steady = pitches[:p]
    starts = [k for k in range(p) if steady[k] != steady[k - 1]]
    if starts:                                  # begin on a change of pitch, preferring the played note itself
        k = next((k for k in starts if steady[k] == 0), starts[0])
        steady = steady[k:] + steady[:k]
    arp, rate, exact = fit_arpeggio(steady, 0, timing.beat)
    if rate > 32:
        rate, exact = 32, False
    if not exact:
        approx.append("the arpeggio is not a cycle of equal steps and 8 or fewer notes; it is rounded")
    if len({w for w in waves[:p] if w is not None}) > 1:
        approx.append("the arpeggio's waveform changes are not carried")
    return (arp, rate), why, approx, seen


def _pulse_run(ins, frames=4096):
    """Run the pulse table from a note's first call.  Returns (widths a call, loop index or None, flags)."""
    raw = ins.raw
    lo, hi = ins.pw_ptr, ins.filt_ptr
    if raw[lo] == 0xFF:
        return [], None, set()
    pos, sweep, width = lo, 0, None
    flags = set()
    series, states = [], {}
    loop = None
    for f in range(frames):
        b0 = raw[pos] if lo <= pos < hi else 0xFF
        b1 = raw[pos + 1] if pos + 1 < hi else 0
        if b0 == 0xFE:
            target = b1
            if target != pos:
                if not lo <= target < hi or (target - lo) % 3:
                    flags.add("jump")
                    b0 = 0xFF
                else:
                    t0, t1 = raw[target], raw[target + 1] if target + 1 < hi else 0
                    if t0 < 0x80:
                        pos, sweep = target, 0
                        b0 = None
                    elif t0 < 0xFE:
                        width = ((t0 & 0x0F) << 8) | t1
                        pos, sweep = target + 3, 0
                        b0 = None
                    else:
                        b0 = 0xFF
            else:
                b0 = None
        if b0 is not None:
            if b0 < 0x80:
                if sweep == b0:
                    pos += 3
                    sweep = 0
                else:
                    sweep += 1
                    if width is None:
                        flags.add("unset")
                    else:
                        width = (width + (b1 - 256 if b1 >= 128 else b1)) & 0xFFF
            elif b0 < 0xFE:
                width = ((b0 & 0x0F) << 8) | b1
                pos += 3
                sweep = 0
        series.append(width)
        state = (pos, sweep, width)
        if state in states:
            loop = states[state]
            break
        states[state] = f
    return series, loop, flags


def _pulse_program(ins, timing):
    """Returns (fields dict or None, reasons it was not carried, approximations, the starting width or None)."""
    series, i, flags = _pulse_run(ins)
    if not series:
        return None, [], [], None
    first = next((w for w in series if w is not None), None)
    if "jump" in flags:
        return None, ["it jumps to a row that does not exist"], [], first
    if "unset" in flags or (series and series[0] is None):
        return None, ["it changes the width before setting one"], [], first
    if i is None:
        return None, ["it does not settle into a repeat"], [], first
    cyc, pre = series[i + 1:], series[:i + 1]
    approx = []
    if len(set(cyc)) == 1:
        end = cyc[0]
        if all(v == end for v in pre):
            return None, [], [], first
        steps = [b - a for a, b in zip(pre, pre[1:] + [end])]
        if not (all(s >= 0 for s in steps) or all(s <= 0 for s in steps)):
            return None, ["it is not a plain ramp"], [], first
        reach = next(k for k, v in enumerate(pre + [end]) if v == end)
        approx.append("a one-way ramp is played as a back-and-forth sweep")
        lo_hi = (pre[0], end)
        period = 2 * max(1, reach)
    else:
        lo, hi = min(cyc), max(cyc)
        d = [cyc[(k + 1) % len(cyc)] - cyc[k] for k in range(len(cyc))]
        signs = [1 if x > 0 else -1 for x in d if x]
        changes = sum(signs[k] != signs[k - 1] for k in range(len(signs)))
        if changes != 2:
            return None, ["it is not a single rise and fall"], [], first
        up, down = sum(x > 0 for x in d), sum(x < 0 for x in d)
        holds = len(d) - up - down
        period = len(cyc)
        if min(up, down) <= 1:
            period *= 2
            approx.append("a one-way ramp that jumps back is played as a back-and-forth sweep")
        elif abs(up - down) > 1 or holds:
            approx.append("the pulse sweep is lopsided or pauses; it is played as an even sweep")
        start = pre[0]
        lo_hi = (hi, lo) if start == hi and start != lo else (lo, hi)
        if start not in (lo, hi):
            approx.append("the pulse sweep starts in the middle; it is played from its lowest width")
    a, b = _percent(lo_hi[0]), _percent(lo_hi[1])
    if a == b:
        return None, [], [], first
    wanted = timing.fps / period / 0.5
    rate = max(1, min(15, round(wanted)))
    if abs(rate - wanted) > 0.1 * wanted:
        approx.append("the sweep speed is rounded to the nearest chipsynth rate (%d)" % rate)
    return dict(pws_from=a, pws_to=b, pws_rate=rate), [], approx, first


def _instrument(idx, ins, report, timing):
    inst = Instrument()
    label = ins.name if re.search(r"[A-Za-z0-9]", ins.name) else "instr%d" % idx
    inst.a, inst.d, inst.s, inst.r = ins.ad >> 4, ins.ad & 15, ins.sr >> 4, ins.sr & 15
    arp, why, approx, waves = _wave_program(ins, timing)
    first = None
    for _i, b0, _b1, _b2 in _table_rows(ins.raw, 16, ins.pw_ptr):
        if 0x10 <= b0 < 0xFE:
            first = b0
            break
    if first is None and ins.ctrl & 8 and ins.firstwave & 0xF0:
        first = ins.firstwave
    wave, exact = _wave_of(first or 0)
    if wave is None:
        report.approx("instrument '%s': no waveform found, so pulse is used" % label)
        wave = "pulse"
    elif not exact:
        report.approx("instrument '%s': waveform %02x combines waves, so %s is used" % (label, first & 0xF0, wave))
    inst.wave = wave
    if arp:
        inst.arp, inst.arprate = arp
        report.note("instrument '%s': its wave table is an arpeggio of %s semitones, %d steps a beat" % (label, ",".join(map(str, arp[0])), arp[1]))
    for a in approx:
        report.approx("instrument '%s': %s" % (label, a))
    if why:
        report.drop("instrument '%s': its wave table program (%s)" % (label, "; ".join(why)))
    elif not arp and len({w & 0xF0 for w in waves}) > 1:
        report.approx("instrument '%s': its wave table changes waveform; the first waveform is used" % label)
    fields, pwhy, papprox, start = _pulse_program(ins, timing)
    if inst.wave != "pulse":
        fields, pwhy, papprox, start = None, [], [], None
    if start is not None:
        inst.pw = _percent(start)
    if fields:
        for k, v in fields.items():
            setattr(inst, k, v)
        report.note("instrument '%s': its pulse table is a sweep between %d%% and %d%%" % (label, fields["pws_from"], fields["pws_to"]))
        for a in papprox:
            report.approx("instrument '%s': %s" % (label, a))
    if pwhy:
        report.drop("instrument '%s': its pulse sweep (%s)" % (label, "; ".join(pwhy)))
    if len(ins.raw) - ins.filt_ptr > 1:
        report.drop("instrument '%s': its filter table" % label)
    if ins.vibrato >> 4:
        report.drop("instrument '%s': its vibrato" % label)
    if ins.octave:
        report.note("instrument '%s': octave shift %+d semitones is added to its notes" % (label, ins.octave))
    return label, inst


# ------------------------------------------------------------------ the song
@dataclass
class Imported:
    name: str
    song: sg.Song
    samples: dict = field(default_factory=dict)


@dataclass
class Result:
    project_name: str
    songs: list
    report: Report


def _voice_events(rows, resolve, instruments, end, loop_tick, tick, report, ch):
    """One channel's rows -> MML-model events.  `tick(frame)` is the tick a player call starts at."""
    notes = []                        # (frame, pitch or None for a gate-off, instrument number)
    inst_cur = 0
    named = set()
    for frame, row, trans in rows:
        if row.inst and row.inst < 0x3F:
            inst_cur = row.inst
        elif row.inst == LEGATO:
            named.add("legato (the note is struck again)")
        elif row.inst >= 0x40:
            named.add("small effect '%s'" % SMALLFX_NAMES.get(row.inst >> 4, "?"))
        if row.fx >= 0x20:
            named.add("small effect '%s'" % SMALLFX_NAMES.get(row.fx >> 4, "?"))
        elif row.fx and row.fx not in (TEMPO_FX, FUNK_FX, TRACK_TEMPO_FX, TRACK_FUNK_FX):
            named.add("pattern effect '%s'" % BIGFX_NAMES.get(row.fx, "$%02x" % row.fx))
        n = row.note
        if 1 <= n <= 0x5F:
            shift = instruments[inst_cur - 1].octave if 1 <= inst_cur <= len(instruments) else 0
            notes.append((frame, n - 1 + trans + shift, inst_cur))
        elif n == GATEOFF_FX:
            notes.append((frame, None, inst_cur))
        elif 0x60 <= n <= 0x6F:
            named.add("note-column vibrato amplitude")
        elif n in NOTEFX_NAMES:
            named.add("note effect '%s'" % NOTEFX_NAMES[n])
    for what in sorted(named):
        if what.startswith("legato"):
            report.approx("channel %d: %s" % (ch + 1, what))
        else:
            report.drop("%s (seen on channel %d)" % (what, ch + 1))
    out, pos, octave, cur = [], 0, 4, None
    items = []
    for i, (frame, pitch, inst) in enumerate(notes):
        nxt = notes[i + 1][0] if i + 1 < len(notes) else end
        items.append((frame, pitch, inst, nxt))
    loop_pending = loop_tick is not None

    def advance(t):
        nonlocal pos
        if t > pos:
            out.append(sg.Rest(t - pos))
            pos = t

    def loop_to(t):
        nonlocal loop_pending
        if loop_pending and loop_tick <= t:
            advance(loop_tick)
            out.append(sg.Loop())
            loop_pending = False

    for frame, pitch, inst, nxt in items:
        t0 = tick(frame)
        loop_to(t0)
        advance(t0)
        if pitch is None:
            continue
        pitch = max(0, min(107, pitch))
        name = resolve(inst)
        if name is not None and name != cur:
            cur = name
            out.append(sg.Select(name))
        if pitch // 12 != octave:
            octave = pitch // 12
            out.append(sg.Octave(set=octave))
        letter, acc = LETTERS[pitch % 12]
        dur = max(1, tick(nxt) - t0)
        out.append(sg.Note(letter, acc, dur))
        pos = t0 + dur
    loop_to(tick(end))
    advance(tick(end))
    return out


def _lcm(a, b):
    return a * b // math.gcd(a, b)


def load(data, region="pal", subtune=None, name=None):
    """`.swm` bytes -> Result.  `region` is 'pal' (50 Hz) or 'ntsc' (60 Hz): the file does not say.  `subtune` picks one
    subtune (1-based); the default is all.  `name` is the song's title (a `.swm` has none; a .P00 file's own name is used
    when `name` is not given)."""
    tune = parse(data)
    report = Report()
    pal = region != "ntsc"
    hz = PAL_HZ if pal else NTSC_HZ
    n = tune.frame_speed if 1 <= tune.frame_speed <= 8 else 1
    report.note("%s timing (%g Hz), %s, frame speed x%d, driver %d" % ("PAL" if pal else "NTSC", hz, tune.magic, n, tune.driver))
    report.note("the file does not record PAL or NTSC: this comes from the option")
    report.note("SID-Wizard starts a note two player calls into its row (its hard restart); notes here start on the row")
    if tune.frame_speed != n:
        report.approx("frame speed %d is not 1-8; it is played as x1" % tune.frame_speed)
    if tune.tuning:
        report.approx("the song is tuned to %s; it is played at 440 Hz" % TUNINGS.get(tune.tuning, "tuning %d" % tune.tuning))
    if 4 % n:
        report.approx("frame speed x%d does not divide a 4-tick frame evenly; row starts are rounded to the nearest tick" % n)

    def tick(frame):
        return (frame * 4 + n // 2) // n if 4 % n else frame * 4 // n

    timing = Timing(hz * n, 24 * n)
    bases = {}
    for i, ins in enumerate(tune.instruments, 1):
        label, inst = _instrument(i, ins, report, timing)
        label = re.sub(r"[^A-Za-z0-9_]", "_", label) or "instr%d" % i
        if not label[0].isalpha():
            label = "i" + label
        while label in [b[0] for b in bases.values()]:
            label += "_"
        bases[i] = (label, inst)
    count = tune.subtunes
    if subtune is not None and not 1 <= subtune <= count:
        raise SidWizardError("this file has %d subtune(s); no subtune %d" % (count, subtune))
    picks = range(count) if subtune is None else [subtune - 1]
    songs = []
    for sub in picks:
        rows, chans, end = _simulate(tune, sub, None, report)
        loop_frame = None
        loopers = [ch for ch in chans if ch.loop]
        if loopers:
            loop_frame = max(ch.loop[0] for ch in loopers)
            cycle = 1
            for ch in loopers:
                cycle = _lcm(cycle, max(1, ch.loop[1] - ch.loop[0]))
            if len({ch.loop[1] - ch.loop[0] for ch in loopers}) > 1 or any(ch.loop[0] != loop_frame for ch in loopers):
                report.approx("subtune %d: the channels loop at different places or lengths; the song is unrolled to one shared loop" % (sub + 1))
            if loop_frame + cycle > FIRST_PASS_CAP:
                cycle = max(ch.loop[1] - ch.loop[0] for ch in loopers)
                report.approx("subtune %d: the channels' loops do not line up within twenty minutes; the longest loop is used" % (sub + 1))
            if any(ch.done for ch in chans):
                report.note("subtune %d: a channel ends while others loop; it stays silent in the song's loop" % (sub + 1))
            end = loop_frame + cycle
            rows, chans, _ = _simulate(tune, sub, end, report)
        title = name or tune.name or "SID-Wizard song"
        if count > 1:
            title = "%s %d" % (title, sub + 1)
        song = sg.Song(title=title, author=tune.author, bpm=int(round(hz * 60 / 24)), voices=3, dacs=0)
        song.tracks = {}
        song.__post_init__()
        used = {}

        def resolve(idx, used=used):
            if not 1 <= idx <= len(tune.instruments):
                if idx:
                    report.drop("a pattern selects instrument %d, which the file does not have" % idx)
                else:
                    report.approx("a note plays before any instrument is selected; the default instrument is used")
                return None
            label, inst = bases[idx]
            if label not in song.instruments:
                song.instruments[label] = inst
            return label

        for c in range(3):
            song.tracks["V%d" % (c + 1)] = _voice_events(rows[c], resolve, tune.instruments, end,
                                                         tick(loop_frame) if loop_frame is not None else None, tick, report, c)
        songs.append(Imported(title, song, {}))
    return Result(name or tune.name or "SID-Wizard song", songs, report)
