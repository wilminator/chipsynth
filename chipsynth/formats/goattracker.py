# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""GoatTracker 2 `.sng` files -> chipsynth songs.

Written from the file format as GoatTracker 2.77's loader and player define it (gsong.c, gcommon.h, gplay.c; GPL-2+,
read only, no code copied).  See PLAN-FORMATS.md section 4 for the layout.

Slice 1: the three SID voices become V1-V3, with notes, rests, key-offs, orderlist repeat and transpose, the restart
position as the loop point, and tempo (speed) changes.  Each instrument carries its ADSR, first waveform and starting
pulse width.  The tables are programs; what they do beyond that is listed in the Report, not guessed.

Slice 2 recognises three common idioms in the tables and carries them over: a wavetable that loops over relative notes
(an arpeggio -> `arp` / `arprate`), a pulse table that ramps the width (-> `pws_from` / `pws_to` / `pws_rate`), and a
speed table entry used as vibrato, from the instrument or from pattern command 4 (-> `vib_rate` / `vib_depth`, as an
instrument variant per rate and depth).  Anything else in the tables is still reported.

Slice 3 carries the pattern commands and the rest of the tables where chipsynth has an honest equivalent: portamento and
tone portamento (a note that glides: `slide`), vibrato, envelope and waveform commands, the pointer commands, master volume,
funk tempo, wavetables with absolute notes / rows that play once / waveform changes (short notes in a row, the looping tail as
`arp`), pulse tables that zigzag, and the filter (tables and commands B, C, A: one filter for the whole song is played, and
each note carries what it does to its voice).  A chipsynth note has one instrument, so anything that changes part-way
through a note makes a run of notes; each part strikes its envelope again, and the Report says so.

Timing: one frame is 4 ticks (96 ticks per quarter), so a song is exact to the frame at any speed, and the song's bpm is
set from the frame rate (125 on PAL, 150 on NTSC).  Multispeed N plays N frames per video frame and keeps the same grid
by using 4/N ticks per frame (N is 1, 2 or 4).
"""
import re
import struct
import math
from dataclasses import dataclass, replace

from .. import song as sg
from ..synth import Instrument
from .envelope import fit_arpeggio
from .report import Report

PAL_HZ = 50.0
NTSC_HZ = 60.0
MAX_FRAMES = 60 * 60 * 20            # a safety stop: twenty minutes at 60 Hz

REPEAT, TRANSDOWN, TRANSUP, LOOPSONG = 0xD0, 0xE0, 0xF0, 0xFF
FIRSTNOTE, LASTNOTE, REST, KEYOFF, KEYON, ENDPATT = 0x60, 0xBC, 0xBD, 0xBE, 0xBF, 0xFF
WTBL, PTBL, FTBL, STBL = range(4)
CMD_PORTAUP, CMD_PORTADOWN, CMD_TONEPORTA, CMD_VIBRATO, CMD_SETAD, CMD_SETSR, CMD_SETWAVE = 1, 2, 3, 4, 5, 6, 7
CMD_WAVEPTR, CMD_PULSEPTR, CMD_FILTERPTR, CMD_FILTERCTRL, CMD_FILTERCUT, CMD_MASTERVOL, CMD_FUNKTEMPO, CMD_SETTEMPO = range(8, 16)
WAVE_COMMANDS = "it runs commands"
SID_CLOCK = {"pal": 985248.0, "ntsc": 1022730.0}
C0_HZ = 440.0 * 2 ** (-57 / 12)         # note 0 (C-0) of the player's frequency table
LETTERS = (("c", 0), ("c", 1), ("d", 0), ("d", 1), ("e", 0), ("f", 0), ("f", 1), ("g", 0), ("g", 1), ("a", 0), ("a", 1), ("b", 0))
COMMAND_NAMES = ("none", "portamento up", "portamento down", "tone portamento", "vibrato", "set attack/decay",
                 "set sustain/release", "set waveform", "set wavetable pointer", "set pulsetable pointer",
                 "set filtertable pointer", "set filter control", "set filter cutoff", "master volume",
                 "funk tempo", "set tempo")


class GoatTrackerError(ValueError):
    pass


@dataclass
class Instr:
    ad: int
    sr: int
    ptr: tuple              # wave, pulse, filter, speed table pointers (1-based, 0 = none)
    vibdelay: int
    gatetimer: int
    firstwave: int
    name: str


@dataclass
class Tune:
    """What a `.sng` file holds, undigested."""
    version: str
    title: str
    author: str
    copyright: str
    orders: list            # [subtune][channel] -> list of bytes (the last one is the restart position)
    instruments: list       # index 0 is instrument number 1
    tables: list            # [WTBL..STBL] -> (left bytes, right bytes)
    patterns: list          # pattern -> list of (note, instrument, command, data)


class _Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        if self.pos + n > len(self.data):
            raise GoatTrackerError("the file ends early (truncated)")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def byte(self):
        return self.take(1)[0]


def looks_like(data):
    return bytes(data[:4]) in (b"GTS5", b"GTS4", b"GTS3")


def _text(raw):
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def parse(data):
    """`.sng` bytes -> Tune.  Raises GoatTrackerError on anything it cannot read."""
    data = bytes(data)
    if not looks_like(data):
        raise GoatTrackerError("not a GoatTracker song (expected GTS3, GTS4 or GTS5 at the start)")
    version = data[:4].decode("ascii")
    r = _Reader(data)
    r.take(4)
    title, author, copy = (_text(r.take(32)) for _ in range(3))
    subtunes = r.byte()
    if not 1 <= subtunes <= 32:
        raise GoatTrackerError("bad subtune count %d" % subtunes)
    orders = []
    for _ in range(subtunes):
        chans = []
        for _c in range(3):
            n = r.byte()
            if n > 254:
                raise GoatTrackerError("order list too long (%d)" % n)
            chans.append(list(r.take(n + 1)))
        orders.append(chans)
    count = r.byte()
    if count > 63:
        raise GoatTrackerError("too many instruments (%d)" % count)
    instruments = []
    for _ in range(count):
        ad, sr, wp, pp, fp, sp, vd, gt, fw = r.take(9)
        instruments.append(Instr(ad, sr, (wp, pp, fp, sp), vd, gt, fw, _text(r.take(16))))
    tables = []
    for _ in range(4):
        n = r.byte()
        tables.append((list(r.take(n)), list(r.take(n))))
    npat = r.byte()
    if npat > 208:
        raise GoatTrackerError("too many patterns (%d)" % npat)
    patterns = []
    for _ in range(npat):
        rows = r.byte()
        if not 1 <= rows <= 128:
            raise GoatTrackerError("bad pattern length %d" % rows)
        raw = r.take(rows * 4)
        patterns.append([tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)])
    return Tune(version, title, author, copy, orders, instruments, tables, patterns)


# ------------------------------------------------------------------ playing the lists
class _Chan:
    """One channel's sequencer: where the order list and the pattern are."""

    def __init__(self, order, patterns, report, ch):
        self.order, self.patterns, self.report, self.ch = order, patterns, report, ch
        self.songptr = self.trans = self.repeat = 0
        self.pat, self.row = None, 0
        self.done = False
        self.restart = None
        self.pending = True               # needs the next pattern from the order list

    def fetch(self):
        """Move to the next pattern in the order list (the sequencer in gplay.c)."""
        o = self.order
        body = len(o) - 1                 # the last byte is the restart position, not a pattern entry
        if self.songptr < len(o) - 1 and o[self.songptr] == LOOPSONG:
            self.restart = o[self.songptr + 1]
            self.done = True
            return
        if self.songptr >= body:
            self.done = True
            return
        if TRANSDOWN <= o[self.songptr] < LOOPSONG:
            self.trans = o[self.songptr] - TRANSUP
            self.songptr += 1
        if self.songptr < body and REPEAT <= o[self.songptr] < TRANSDOWN:
            self.repeat = o[self.songptr] - REPEAT
            self.songptr += 1
        if self.songptr >= body:
            self.done = True
            return
        num = o[self.songptr]
        if self.repeat:
            self.repeat -= 1
        else:
            self.songptr += 1
        if num >= len(self.patterns):
            self.report.drop("channel %d: order list names pattern %d, which does not exist" % (self.ch + 1, num))
            self.done = True
            return
        self.pat, self.row, self.pending = self.patterns[num], 0, False

    def next_row(self):
        """The (note, instrument, command, data) of the next row, or None when the channel is finished."""
        while self.pending or self.pat is None:
            self.fetch()
            if self.done:
                return None
        row = self.pat[self.row]
        self.row += 1
        if self.row >= len(self.pat) or self.pat[self.row][0] == ENDPATT:
            self.pending = True
        return row, self.trans


def _speed_entry(tune, entry):
    """(left, right) of speed table entry `entry` (1-based), or None when there is no such entry."""
    lcol, rcol = tune.tables[STBL]
    if not 1 <= entry <= len(lcol):
        return None
    return lcol[entry - 1], rcol[entry - 1]


def _timeline(tune, sub, hz_mult, report):
    """Play the three channels frame by frame, as far as the first LOOPSONG.  Returns the rows per channel
    as [(frame, note, instrument, command, data, transpose)] and (end frame, loop frame or None)."""
    chans = [_Chan(tune.orders[sub][c], tune.patterns, report, c) for c in range(3)]
    base = 6 * hz_mult
    tempo = [base, base, base]            # frames per row
    left = [0, 0, 0]
    rows = [[], [], []]
    loops = [None, None, None]
    frame = 0
    finished = [False, False, False]
    funk = None                           # (a, b): frames per row, alternating, once command E has set them
    funky = [False, False, False]         # the channel alternates its row lengths
    fidx = [0, 0, 0]
    pend0 = []                            # (order position, frame) each time channel 1 needs its next pattern
    while not all(finished) and frame < MAX_FRAMES:
        for c in range(3):
            if finished[c]:
                continue
            if left[c] > 0:
                left[c] -= 1
                continue
            if c == 0 and chans[0].pending:
                pend0.append((chans[0].songptr, frame))
            got = chans[c].next_row()
            if got is None:
                finished[c] = True
                if chans[c].restart is not None:
                    loops[c] = chans[c].restart
                continue
            (note, inst, cmd, data), trans = got
            if cmd == CMD_SETTEMPO:
                v = data & 0x7F
                if v >= 3:
                    v *= 1
                    if data >= 0x80:
                        tempo[c] = v
                        funky[c] = False
                    else:
                        tempo = [v, v, v]
                        funky = [False, False, False]
                elif v and funk is None:
                    report.approx("funk or very fast tempo %d is played as %d frames per row" % (data, v + 1))
                    tempo = [v + 1] * 3
                elif funk is not None and v < 2:
                    who = [c] if data >= 0x80 else [0, 1, 2]
                    for k in who:
                        funky[k], fidx[k] = True, v
                elif v:
                    report.approx("funk or very fast tempo %d is played as %d frames per row" % (data, v + 1))
                    tempo = [v + 1] * 3
                else:
                    report.drop("tempo command 0 (it stops the player's tick)")
            elif cmd == CMD_FUNKTEMPO:
                got = _speed_entry(tune, data)
                if got is None or not (got[0] and got[1]):
                    report.drop("funk tempo: speed table entry %d is missing or has a 0 tempo" % data)
                else:
                    funk = (got[0] & 0x7F, got[1] & 0x7F)
                    funky = [True, True, True]
                    fidx = [0, 0, 0]
                    report.note("funk tempo alternates %d and %d frames a row" % funk)
            if funky[c]:
                tempo[c] = max(1, funk[fidx[c]])
                fidx[c] ^= 1
            rows[c].append((frame, note, inst, cmd, data, trans))
            left[c] = tempo[c] - 1
        frame += 1
    if all(finished):
        frame -= 1                        # the last channel was found finished one frame after its last row ended
    if frame >= MAX_FRAMES:
        report.note("the song was cut at twenty minutes (no end or loop was found)")
    end = max([frame] + [r[-1][0] + 1 for r in rows if r])
    return rows, end, chans, loops, (pend0 if funk is not None else None)


# ------------------------------------------------------------------ instruments
@dataclass
class Timing:
    fps: float              # player frames a second (the video rate times the multispeed)
    beat: int               # player frames in a quarter note
    clock: float            # the SID's clock, Hz

    def sid_freq(self, note):
        """The SID frequency register for note index `note` (0 = C-0), as the player's table has it."""
        return int(C0_HZ * 2 ** (min(note, 127) / 12) * 16777216 / self.clock)


def _pulse_start(tune, ptr):
    """Starting pulse width of an instrument, in percent, or None."""
    if not ptr:
        return None
    lcol, rcol = tune.tables[PTBL]
    if ptr - 1 >= len(lcol) or lcol[ptr - 1] < 0x80:
        return None
    w = ((lcol[ptr - 1] & 0xF) << 8) | rcol[ptr - 1]
    return max(1, min(99, round(w * 100 / 4096)))


def _wave_of(bits):
    """SID waveform bits -> (chipsynth wave, exact).  Noise beats pulse beats saw beats triangle."""
    w = bits & 0xF0
    for flag, name in ((0x80, "noise"), (0x40, "pulse"), (0x20, "saw"), (0x10, "tri")):
        if w & flag:
            return name, w == flag
    return None, False


def _percent(width):
    return max(1, min(99, round(width * 100 / 4096)))


def _runs(values):
    """The distinct values in order, as runs: [0, 0, 4, 4, 7] -> [0, 4, 7]."""
    out = []
    for v in values:
        if not out or out[-1] != v:
            out.append(v)
    return out


def _wave_program(tune, wp, timing):
    """Run an instrument's wavetable from row `wp` (1-based) the way the player does: a row takes a frame, a delay row
    (0x00-0x0f) holds the pitch and then takes effect after that many frames, a jump (0xff) is free.  Returns
    (arpeggio fields or None, reasons it was not carried, approximations, rows walked)."""
    lcol, rcol = tune.tables[WTBL]
    n = len(lcol)
    ptr, frame, starts, events, waves = wp, 0, {}, [], set()
    commands = absolute = False
    loop_at, rows = None, 0
    for _ in range(4 * 255):
        if ptr == 0 or ptr > n:
            break
        if lcol[ptr - 1] == 0xFF:
            ptr = rcol[ptr - 1]
            continue
        if ptr in starts:
            loop_at = starts[ptr]
            break
        starts[ptr] = frame
        left, right = lcol[ptr - 1], rcol[ptr - 1]
        rows += 1
        if left <= 0x0F:
            when, length = frame + left, left + 1
            if right > 0x80:
                absolute = True
        else:
            when, length = frame, 1
            if left >= 0xF0:
                commands = True
            else:
                waves.add(left & 0xFE if left < 0xE0 else left)
                if right > 0x80:
                    absolute = True
        events.append((when, right))
        frame += length
        ptr += 1
    why, approx, steady = [], [], None
    moving = any(r not in (0, 0x80) for _w, r in events)
    if commands:
        why.append(WAVE_COMMANDS)
    if absolute:
        why.append("it uses absolute notes (0x81 and up), which are not relative to the played note")
    if moving and not why:
        if loop_at is None:
            why.append("it does not repeat")
        elif loop_at != 0:
            why.append("it plays some rows once before it loops")
        else:
            cycle = frame
            seq, cur, k = [], 0, 0
            pending = sorted(events + [(w + cycle, r) for w, r in events])
            for t in range(2 * cycle):
                while k < len(pending) and pending[k][0] <= t:
                    if pending[k][1] != 0x80:
                        cur = pending[k][1]
                    k += 1
                seq.append(cur)
            steady = seq[cycle:]
            starts_at = [k for k in range(cycle) if steady[k] != steady[k - 1]]
            if starts_at:                 # begin on a change of pitch, preferring the played note itself
                k = next((k for k in starts_at if steady[k] == 0), starts_at[0])
                steady = steady[k:] + steady[:k]
            if _runs(seq[:cycle]) != _runs(steady):
                approx.append("its first pass differs from the repeats; the repeating part is used")
    if len(waves) > 1:
        if steady is None:
            why.append("it changes waveform")
        else:
            approx.append("the arpeggio's waveform changes are not carried")
    if why or steady is None:
        return None, why, approx, rows
    arp, rate, exact = fit_arpeggio(steady, 0, timing.beat)
    if rate > 32:
        rate, exact = 32, False
    if not exact:
        approx.append("the arpeggio is not a cycle of equal steps and 8 or fewer notes; it is rounded")
    return (arp, rate), why, approx, rows


def _pulse_program(tune, ptr, timing, start=None):
    """Run an instrument's pulse table as the player does.  Returns (fields dict or None, reasons it was not carried,
    approximations).  `fields` has pws_from, pws_to and pws_rate.  A table that changes the width before it sets one
    works from the width the player had before (the file does not say): `start`, or half the period."""
    lcol, rcol = tune.tables[PTBL]
    n = len(lcol)
    pulse, unknown, left = (0, True, 0) if start is None else (start, False, 0)
    states, series = {}, []
    ptr0 = ptr
    for f in range(4096):
        if ptr and ptr <= n and lcol[ptr - 1] == 0xFF:
            ptr = rcol[ptr - 1]
            if ptr and ptr <= n and lcol[ptr - 1] == 0xFF:
                ptr = 0
        if ptr and ptr <= n:
            if not left:
                if lcol[ptr - 1] >= 0x80:
                    pulse = ((lcol[ptr - 1] & 0xF) << 8) | rcol[ptr - 1]
                    unknown = False
                    ptr += 1
                else:
                    left = lcol[ptr - 1]
            if left:
                step = rcol[ptr - 1]
                pulse = (pulse + (step if step < 0x80 else step - 0x100)) & 0xFFF
                left -= 1
                if not left:
                    ptr += 1
        series.append(pulse)
        state = (ptr if ptr <= n else 0, left, pulse)
        if state in states:
            i = states[state]
            break
        states[state] = f
    else:
        return None, ["it does not settle into a repeat"], []
    cyc, pre = series[i + 1:], series[:i + 1]
    approx = []
    if unknown:
        fields, why, approx = _pulse_program(tune, ptr0, timing, start=0x800)
        return fields, why, ["the table changes the width before it sets one; the width the player had then is not in the file, so it starts from 50%"] + approx
    if len(set(cyc)) == 1:
        end = cyc[0]
        if all(v == end for v in pre):
            return None, [], []
        steps = [b - a for a, b in zip(pre, pre[1:] + [end])]
        if not (all(s >= 0 for s in steps) or all(s <= 0 for s in steps)):
            return None, ["it is not a plain ramp"], []
        reach = next(k for k, v in enumerate(pre + [end]) if v == end)
        approx.append("a one-way ramp is played as a back-and-forth sweep")
        lo_hi = (pre[0], end)
        period = 2 * max(1, reach)
    else:
        lo, hi = min(cyc), max(cyc)
        d = [cyc[(k + 1) % len(cyc)] - cyc[k] for k in range(len(cyc))]
        signs = [1 if x > 0 else -1 for x in d if x]
        changes = sum(signs[k] != signs[k - 1] for k in range(len(signs)))
        zigzag = 1
        if changes != 2:
            # several rises and falls in a cycle: a cycle that is the same sweep repeated is that sweep, exactly
            for size in range(1, len(cyc)):
                if len(cyc) % size == 0 and cyc[:size] * (len(cyc) // size) == cyc:
                    cyc = cyc[:size]
                    break
            d = [cyc[(k + 1) % len(cyc)] - cyc[k] for k in range(len(cyc))]
            signs = [1 if x > 0 else -1 for x in d if x]
            changes = sum(signs[k] != signs[k - 1] for k in range(len(signs)))
            if changes != 2:
                zigzag = changes // 2
                if zigzag > 8:
                    return None, ["it zigzags %d times in a cycle, which is not a sweep" % zigzag], []
                approx.append("the pulse zigzags %d times in a cycle; it is played as an even sweep between its lowest and highest width" % zigzag)
        up, down = sum(x > 0 for x in d), sum(x < 0 for x in d)
        holds = len(d) - up - down
        period = len(cyc) / zigzag if zigzag > 1 else len(cyc)
        if zigzag > 1:
            pass
        elif min(up, down) <= 1:
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
        return None, [], []
    wanted = timing.fps / period / 0.5
    rate = max(1, min(15, round(wanted)))
    if abs(rate - wanted) > 0.1 * wanted:
        approx.append("the sweep speed is rounded to the nearest chipsynth rate (%d)" % rate)
    return dict(pws_from=a, pws_to=b, pws_rate=rate), [], approx


def _vibrato(tune, entry, note, timing):
    """A speed table entry played as vibrato on `note`.  Returns ((rate, depth), approx notes) or (None, reason)."""
    lcol, rcol = tune.tables[STBL]
    if not 1 <= entry <= len(lcol):
        return None, "speed table entry %d does not exist" % entry
    cmp_, speed = lcol[entry - 1], rcol[entry - 1]
    if cmp_ >= 0x80:
        cmp_ &= 0x7F
        speed = (timing.sid_freq(note + 1) - timing.sid_freq(note)) >> min(rcol[entry - 1], 31)
    if speed == 0:
        return None, "speed table entry %d has speed 0" % entry
    vt, pos, seen, where = 0, 0, {}, []
    for f in range(1024):
        if vt in seen:
            break
        seen[vt] = f
        where.append(pos)
        if vt < 0x80 and vt > cmp_:
            vt ^= 0xFF
        vt = (vt + 2) & 0xFF
        pos += -speed if vt & 1 else speed
    else:
        return None, "speed table entry %d never settles into a cycle" % entry
    first = seen[vt]
    if pos != where[first]:
        return None, "speed table entry %d drifts the pitch instead of swinging it" % entry
    cycle = where[first:]
    amp = (max(cycle) - min(cycle)) / 2.0
    length = len(cycle)
    base = timing.sid_freq(note)
    semis = 12 * math.log2((base + amp) / base) if base else 0.0
    depth = round(semis * 16)
    if depth < 1:
        return None, "speed table entry %d is too shallow to hear (under 1/32 of a semitone)" % entry
    wanted = timing.fps / length / 0.8
    rate = max(1, min(15, round(wanted)))
    notes = ["vibrato is a triangle swing in the player and a sine in chipsynth; its rate and depth are rounded"]
    if depth > 15 or abs(rate - wanted) > 0.15 * wanted:
        notes.append("some vibrato is faster or deeper than chipsynth's top setting and is limited to it")
    return (rate, min(15, depth)), notes


def _instrument(tune, idx, ins, report, timing, defer=None):
    """`defer` (a dict) takes the instruments whose wavetable is played note by note (see `_Plan`) instead of being fitted;
    when it is given, the song-level planner is in use and it also carries the filter tables (`_FilterSim`)."""
    inst = Instrument()
    label = ins.name or "instr%d" % idx
    inst.a, inst.d, inst.s, inst.r = ins.ad >> 4, ins.ad & 15, ins.sr >> 4, ins.sr & 15
    first = ins.firstwave
    wp = ins.ptr[WTBL]
    if wp and wp - 1 < len(tune.tables[WTBL][0]):
        left = tune.tables[WTBL][0][wp - 1]
        if 0x10 <= left < 0xE0:
            first = left
    wave, exact = _wave_of(first)
    if wave is None:
        report.approx("instrument '%s': no waveform found (first wave %02x), so pulse is used" % (label, first))
        wave = "pulse"
    elif not exact:
        report.approx("instrument '%s': waveform %02x combines waves, so %s is used" % (label, first & 0xF0, wave))
    inst.wave = wave
    pw = _pulse_start(tune, ins.ptr[PTBL])
    if pw is not None:
        inst.pw = pw
    if wp and wp - 1 < len(tune.tables[WTBL][0]):
        arp, why, approx, rows = _wave_program(tune, wp, timing)
        if arp:
            inst.arp, inst.arprate = arp
            report.note("instrument '%s': its wavetable is an arpeggio of %s semitones, %d steps a beat" % (label, ",".join(map(str, arp[0])), arp[1]))
        for a in approx:
            report.approx("instrument '%s': %s" % (label, a))
        if why and defer is not None and WAVE_COMMANDS not in why:
            defer[idx] = why                       # played as short notes, one for each change the table makes
        elif why:
            report.drop("instrument '%s': its wavetable program (%d steps; %s)" % (label, rows, "; ".join(why)))
    pp = ins.ptr[PTBL]
    if pp and pp - 1 < len(tune.tables[PTBL][0]):
        fields, why, approx = _pulse_program(tune, pp, timing)
        pulse_later = defer is not None and idx in defer and any(
            _wave_of(l)[0] == "pulse" for l in tune.tables[WTBL][0][wp - 1:] if 0x10 <= l < 0xE0)
        if fields and (inst.wave == "pulse" or pulse_later):
            for k, v in fields.items():
                setattr(inst, k, v)
            report.note("instrument '%s': its pulse table is a sweep between %d%% and %d%%" % (label, fields["pws_from"], fields["pws_to"]))
            for a in approx:
                report.approx("instrument '%s': %s" % (label, a))
        elif fields:
            report.note("instrument '%s': its pulse sweep is ignored because the waveform is %s" % (label, inst.wave))
        if why:
            report.drop("instrument '%s': its pulse sweep (%s)" % (label, "; ".join(why)))
    elif pp:
        report.drop("instrument '%s': its pulse sweep (the table pointer is outside the table)" % label)
    if ins.ptr[FTBL] and defer is None:
        report.drop("instrument '%s': its filter table" % label)
    if ins.ptr[STBL] and not ins.vibdelay:
        report.note("instrument '%s': its speed table pointer does nothing without a vibrato delay (the player needs one)" % label)
    elif ins.vibdelay and not ins.ptr[STBL]:
        report.note("instrument '%s': vibrato delay %d is ignored (no speed table entry)" % (label, ins.vibdelay))
    elif ins.vibdelay > 1:
        report.approx("instrument '%s': vibrato delay %d is not kept; vibrato starts at once" % (label, ins.vibdelay))
    return label, inst


# ------------------------------------------------------------------ what the commands and tables do to a note
@dataclass
class Atom:
    """One chipsynth note of a played note: `dur` frames at `pitch`, with instrument changes `over` (see
    `_Instruments.variant`) and, for a slide into it, `slide` (chipsynth's unit: 1/48 of a quarter note).  `part` numbers
    the stretch of the played note it belongs to."""
    dur: int
    pitch: int
    over: dict
    slide: int = 0
    part: int = 0


def _wave_note(note, right, base):
    """The note a wavetable row asks for: 0x80 keeps it, 0x00-0x7f is relative to the played note, above 0x80 is absolute."""
    if right == 0x80:
        return note
    if right < 0x80:
        return (base + right) & 0x7F
    return right - 0x80


def _wave_silent(bits):
    """A waveform byte that makes no sound, or that ends the gate (the release starts)."""
    return bits is not None and (bits >= 0xE0 or _wave_of(bits)[0] is None or not bits & 1)


def _wave_trace(tune, wp, base, wave0, limit=8192):
    """Run a wavetable from row `wp` (1-based) on a note of pitch index `base`, one frame at a time, the way `_wave_program`
    reads the player: a row is a frame, a delay row (0x00-0x0f) holds and then takes effect, a jump is free.  Returns
    (states, loop, flags).  `states` is [(note, waveform byte)] a frame (the waveform is `wave0` until a row sets one);
    `loop` is None when the table ended (the last state holds) or (start, period) when states[start:start + period] repeats
    for ever; `flags` holds WAVE_COMMANDS when the table runs commands (they are skipped)."""
    lcol, rcol = tune.tables[WTBL]
    n = len(lcol)
    ptr, wait, note, wave = wp, None, base, wave0
    states, seen, flags = [], {}, set()
    while len(states) < limit:
        hops = 0
        while 0 < ptr <= n and lcol[ptr - 1] == 0xFF and hops < 256:
            ptr, hops = rcol[ptr - 1], hops + 1
        if not 0 < ptr <= n or hops >= 256:
            return states, None, flags
        if wait is None:
            key = (ptr, note, wave)
            if key in seen:
                start = seen[key]
                period = len(states) - start
                if not period:
                    return states, None, flags
                while start > 0 and states[start - 1] == states[start - 1 + period]:
                    start -= 1
                return states, (start, period), flags
            seen[key] = len(states)
        left, right = lcol[ptr - 1], rcol[ptr - 1]
        if left <= 0x0F:
            if wait is None:
                wait = left
            if wait:
                wait -= 1
                states.append((note, wave))
                continue
            wait = None
            note = _wave_note(note, right, base)
        elif left >= 0xF0:
            flags.add(WAVE_COMMANDS)
        else:
            wave = left
            note = _wave_note(note, right, base)
        ptr += 1
        states.append((note, wave))
    return states, None, flags


def _pieces(states, loop, length):
    """Cut a trace into runs of one state: [start, end, note, waveform, cycle], cycle being None or the states of one
    period of an endless repeat that starts at `start`.  Covers frames 0 to `length` (empty when the table never ran)."""
    pre = states if loop is None else states[:loop[0]]
    out = []
    for f, st in enumerate(pre[:length]):
        if out and (out[-1][2], out[-1][3]) == st:
            out[-1][1] = f + 1
        else:
            out.append([f, f + 1, st[0], st[1], None])
    if loop is None:
        if out:
            out[-1][1] = length                 # the last state holds
        return out
    start, period = loop
    if start < length:
        cyc = states[start:start + period]
        out.append([start, length, cyc[0][0], cyc[0][1], cyc])
    return out


class _FilterSim:
    """The SID filter is one shared filter that a filter table (started by an instrument or by command A) and commands B and C
    steer.  It is played here for the whole song, a frame at a time, and each note then asks what the filter was doing to its
    voice while it sounded (chipsynth's filter is per note: a mode, a cutoff, a resonance and a cutoff sweep)."""
    MODES = ((0x10, "lp"), (0x20, "bp"), (0x40, "hp"))

    def __init__(self, tune, rows, end, timing, report):
        self.timing, self.report = timing, report
        self.mode, self.res, self.mask, self.cut = [], [], [], []
        events = []
        cur = [0, 0, 0]
        for c in range(3):
            for i, (frame, note, inst, cmd, data, _t) in enumerate(rows[c]):
                if inst:
                    cur[c] = inst
                if FIRSTNOTE <= note <= LASTNOTE and cmd != CMD_TONEPORTA and 1 <= cur[c] <= len(tune.instruments):
                    fp = tune.instruments[cur[c] - 1].ptr[FTBL]
                    if fp:
                        events.append((frame, c, i, "ptr", fp))
                if cmd == CMD_FILTERPTR:
                    events.append((frame, c, i, "ptr", data))
                elif cmd == CMD_FILTERCTRL:
                    events.append((frame, c, i, "ctrl", data))
                elif cmd == CMD_FILTERCUT:
                    events.append((frame, c, i, "cut", data))
        self.used = bool(events)
        if not events:
            return
        events.sort()
        lcol, rcol = tune.tables[FTBL]
        n = len(lcol)
        mode = res = mask = cutoff = 0
        ptr = time = 0
        k = 0
        for f in range(end):
            while k < len(events) and events[k][0] <= f:
                kind, data = events[k][3], events[k][4]
                if kind == "ptr":
                    ptr, time = data, 0
                    if data > n:
                        report.drop("filter table pointer %d is outside the filter table" % data)
                        ptr = 0
                elif kind == "ctrl":
                    res, mask = data >> 4, data & 15
                    if not data:
                        ptr, mode = 0, 0
                    elif not mode:
                        report.note("pattern command B chose resonance and voices for the filter while no filter type was set "
                                    "(a filter table sets it), so it had no effect then")
                else:
                    cutoff = data
                k += 1
            hops = 0
            while 0 < ptr <= n and lcol[ptr - 1] == 0xFF and hops < 256:
                ptr, hops = rcol[ptr - 1], hops + 1
            if 0 < ptr <= n and hops < 256:
                left, right = lcol[ptr - 1], rcol[ptr - 1]
                if left >= 0x80:
                    mode, res, mask = left & 0x70, right >> 4, right & 15
                    ptr += 1
                    if 0 < ptr <= n and lcol[ptr - 1] == 0:
                        cutoff = rcol[ptr - 1]
                        ptr += 1
                elif left == 0:
                    cutoff = right
                    ptr += 1
                else:
                    if not time:
                        time = left
                    cutoff = max(0, min(255, cutoff + (right if right < 0x80 else right - 0x100)))
                    time -= 1
                    if not time:
                        ptr += 1
            self.mode.append(mode)
            self.res.append(res)
            self.mask.append(mask)
            self.cut.append(cutoff)

    @staticmethod
    def units(c8):
        """A SID cutoff byte as chipsynth's 0-15 cutoff (about 30 Hz plus 46 Hz a step, as the 6581 is, on chipsynth's curve)."""
        hz = 30.0 + c8 * 46.4
        return max(0.0, min(15.0, 15.0 * math.log(max(hz, 1.0) / 100.0) / math.log(80.0)))

    def over(self, ch, f0, f1):
        """What the filter does to voice `ch` from frame f0 to f1, as instrument changes: {} when it does not touch the voice."""
        n = len(self.mode)
        if f0 >= n:
            return {}
        f1 = min(max(f1, f0 + 1), n)
        frames = range(f0, f1)
        on = [bool(self.mode[f] and (self.mask[f] >> ch) & 1) for f in frames]
        if not on[0]:
            if any(on):
                self.report.approx("a filter that is switched on part-way through a note is not carried")
            return {}
        self.report.approx("the SID filter is one shared filter and chipsynth's is per note: each note carries the filter as it "
                           "is when the note starts (its cutoff curve and resonance are chipsynth's, not the chip's)")
        names = [name for flag, name in self.MODES if self.mode[f0] & flag]
        mode = names[0]
        if len(names) > 1:
            self.report.approx("filter types %s together are played as %s" % ("+".join(names), mode))
        if len({(self.mode[f], self.res[f], o) for f, o in zip(frames, on)}) > 1:
            self.report.approx("a filter whose mode, resonance or routing changes during a note keeps the settings it had at the start")
        res = self.res[f0]
        u = [self.units(self.cut[f]) for f in frames]
        start = u[0]
        far = max(u, key=lambda x: abs(x - start))
        if round(far) == round(start):
            return {"filt": (mode, int(round(start)), res)}
        reach = next(t for t, x in enumerate(u) if abs(x - start) >= abs(far - start) * 0.99)
        rate = max(1, min(15, round(self.timing.fps / max(1, reach))))
        self.report.approx("a filter sweep is played as a cutoff swing back and forth between two settings, at the speed of its first rise or fall")
        return {"filt": (mode, int(round(start)), res), "fs": (int(round(start)), int(round(far)), rate)}


class _Plan:
    """Turns a note, with the commands and tables that act on it, into a run of chipsynth notes (atoms).  A chipsynth note has
    one instrument, so a change of waveform, of the wavetable's pitch, of vibrato, or a slide that starts part-way, is played as
    short notes in a row; each part strikes its envelope again.  Notes that none of this touches never come here."""

    def __init__(self, tune, bases, defer, timing, report, res, fsim, tpf):
        self.tune, self.bases, self.defer, self.timing, self.report = tune, bases, defer, timing, report
        self.res, self.fsim, self.tpf = res, fsim, tpf
        self.traces, self.pulses = {}, {}

    def needs(self, idx, cmd, mids, ch, frame, nxt):
        if not 1 <= idx <= len(self.tune.instruments):
            return False
        if cmd in (CMD_PORTAUP, CMD_PORTADOWN, CMD_TONEPORTA, CMD_SETAD, CMD_SETSR, CMD_SETWAVE, CMD_WAVEPTR, CMD_PULSEPTR) or mids:
            return True
        if idx in self.defer:
            return True
        return bool(self.fsim and self.fsim.over(ch, frame, nxt))

    def _speed(self, data, who):
        """The 16-bit speed of speed table entry `data` (left column high), 0 for no entry, None when the entry is missing."""
        if not data:
            return 0
        got = _speed_entry(self.tune, data)
        if got is None:
            self.report.drop("%s: speed table entry %d does not exist" % (who, data))
            return None
        return (got[0] << 8) | got[1]

    def _pulse_fields(self, ptr):
        """(pw, from, to, rate) of the pulse table that pointer command 9 asks for (0 or outside the table: a plain width)."""
        if ptr not in self.pulses:
            fields = {}
            if ptr and ptr <= len(self.tune.tables[PTBL][0]):
                fields, why, approx = _pulse_program(self.tune, ptr, self.timing)
                fields = fields or {}
                for a in approx:
                    self.report.approx("pulse table %d: %s" % (ptr, a))
                if why:
                    self.report.drop("pulse table %d: its pulse sweep (%s)" % (ptr, "; ".join(why)))
            elif ptr:
                self.report.drop("pulse table pointer %d is outside the pulse table" % ptr)
            pw = _pulse_start(self.tune, ptr)
            self.pulses[ptr] = (pw if pw is not None else 50, fields.get("pws_from", 0), fields.get("pws_to", 0), fields.get("pws_rate", 0))
        return self.pulses[ptr]

    def _pitch_of(self, f, ref):
        """The semitone nearest to SID frequency `f`, counted from the note `ref`."""
        f = max(1.0, min(65535.0, f))
        return ref + int(round(12 * math.log2(f / self.timing.sid_freq(ref))))

    def _glide(self, ref, f0, speed, n, first, over, part):
        """Atoms for n frames of a slide that changes the SID frequency by `speed` (signed) a frame, from f0.  Returns
        (atoms, end frequency).  A slide is a note that glides into a pitch over its length, so each stretch of up to
        chipsynth's longest slide is one note that ends on the semitone the slide has reached; a slide that starts with the
        note first plays the note itself for a frame."""
        atoms = []
        done = 0
        cur = max(0, min(107, self._pitch_of(f0, ref)))
        if first and n > 1:
            atoms.append(Atom(1, cur, dict(over), 0, part))
            done = 1
        most = max(1, int(384 // self.tpf))
        while done < n:
            k = min(most, n - done)
            f = max(1.0, min(65535.0, f0 + speed * (done + k)))
            end = max(0, min(107, self._pitch_of(f, ref)))
            if end != cur:
                atoms.append(Atom(k, end, dict(over), max(1, min(192, int(math.ceil(k * self.tpf / 2.0)))), part))
            else:
                atoms.append(Atom(k, end, dict(over), 0, part))
            cur = end
            done += k
        return atoms, max(1.0, min(65535.0, f0 + speed * n))

    def atoms(self, idx, pitch, cmd, data, mids, nframes, ch, frame, prev):
        """The notes a played note becomes.  `mids` are the commands on the rows after it, [(frame offset, command, data)];
        `prev` is the pitch the voice last played (for a tone portamento)."""
        tune, rep = self.tune, self.report
        ins = tune.instruments[idx - 1]
        label, base = self.bases[idx]
        tone = cmd == CMD_TONEPORTA
        ph = {"vib": 0, "who": "", "porta": None, "wave": None, "ad": None, "sr": None, "pulse": None}
        own_wp = ins.ptr[WTBL]
        progs = [[0, own_wp]]

        def apply(c, d, off):
            if c in (CMD_PORTAUP, CMD_PORTADOWN):
                speed = self._speed(d, "pattern command '%s'" % COMMAND_NAMES[c])
                ph["porta"] = (c, speed) if speed else None
            elif c == CMD_VIBRATO:
                ph["vib"], ph["who"] = d, "pattern command 4"
            elif c == CMD_SETAD:
                ph["ad"] = d
            elif c == CMD_SETSR:
                ph["sr"] = d
            elif c == CMD_SETWAVE:
                ph["wave"] = d
            elif c == CMD_WAVEPTR:
                if off:
                    progs.append([off, d])
                else:
                    progs[0][1] = d
            elif c == CMD_PULSEPTR:
                ph["pulse"] = d

        if cmd == 0 and ins.vibdelay:
            ph["vib"], ph["who"] = ins.ptr[STBL], "instrument '%s'" % label
        if not tone:
            apply(cmd, data, 0)
        phases = [(0, dict(ph))]
        for off, c, d in sorted(mids):
            if c in (CMD_SETAD, CMD_SETSR):
                rep.drop("pattern command '%s' on a row without a new note (it changes the envelope of a note that is sounding)" % COMMAND_NAMES[c])
                continue
            apply(c, d, off)
            phases.append((off, dict(ph)))
        # the wavetable: the instrument's, or the one a pointer command starts; a note that slides in does not start one
        pieces = []
        for k, (start, wp) in enumerate(progs):
            end = progs[k + 1][0] if k + 1 < len(progs) else nframes
            length = end - start
            if length <= 0:
                continue
            traced = (not tone) and (idx in self.defer or wp != own_wp or start > 0)
            got = []
            done = 0 if (wp == 0 or not base.arp) else None       # the frame from which the table no longer changes anything
            if traced and 0 < wp <= len(tune.tables[WTBL][0]):
                wave0 = ins.firstwave if start == 0 and 0x10 <= ins.firstwave < 0xE0 and ins.firstwave & 1 else None
                key = (wp, pitch, wave0)
                if key not in self.traces:
                    self.traces[key] = _wave_trace(tune, wp, pitch, wave0)
                states, loop, flags = self.traces[key]
                if WAVE_COMMANDS in flags:
                    rep.drop("instrument '%s': its wavetable runs commands, which are skipped" % label)
                got = _pieces(states, loop, length)
                done = None if loop is not None else start + len(states)
            elif traced:
                done = 0
            if not got:
                got = [[0, length, pitch, None, None]]
            for pc in got:
                pieces.append([pc[0] + start, pc[1] + start, pc[2], pc[3], pc[4], traced, done])
        cuts = {0} | {pc[0] for pc in pieces} | {off for off, _ in phases}
        cuts = sorted(c for c in cuts if 0 <= c < nframes) + [nframes]
        atoms, drift_piece, fcur, stopped = [], None, None, False
        for part, (b0, b1) in enumerate(zip(cuts, cuts[1:])):
            pc = next(p for p in pieces if p[0] <= b0 < p[1])
            phase = [p for off, p in phases if off <= b0][-1]
            over, note, wbits, arp = {}, pc[2], pc[3], ()
            if pc[4] is not None:
                rot = (b0 - pc[0]) % len(pc[4])
                cyc = pc[4][rot:] + pc[4][:rot]
                note, wbits = cyc[0]
                if len(set(cyc)) > 1:
                    if len({w for _n, w in cyc}) == 1:
                        arp = tuple(max(-24, min(24, n - note)) for n, _w in cyc)
                        if any(abs(n - note) > 24 for n, _w in cyc):
                            rep.approx("instrument '%s': an arpeggio step more than two octaves away is limited to two octaves" % label)
                    else:
                        rep.approx("instrument '%s': its repeating wavetable part changes waveform; the part is played as its first step" % label)
            if phase["wave"] is not None:
                if pc[6] is not None and b0 >= pc[6]:
                    wbits = phase["wave"]
                else:
                    rep.note("pattern command 'set waveform' does nothing while the instrument's wavetable is running (as in GoatTracker)")
            if wbits is not None:
                if _wave_silent(wbits):
                    rep.approx("instrument '%s': waveform %02x makes no sound or ends the gate; the note ends there" % (label, wbits))
                    stopped = True
                    break
                wave, exact = _wave_of(wbits)
                if not exact:
                    rep.approx("instrument '%s': waveform %02x combines waves, so %s is used" % (label, wbits & 0xF0, wave))
                if wave != base.wave:
                    over["wave"] = wave
            p = max(0, min(107, note))
            if pc[5] and (arp or base.arp):
                over["arp"] = ()
                if arp:
                    steps, rate, exact = fit_arpeggio(arp, 0, self.timing.beat)
                    if rate > 32:
                        rate, exact = 32, False
                    if not exact:
                        rep.approx("instrument '%s': the arpeggio is not a cycle of equal steps and 8 or fewer notes; it is rounded" % label)
                    over["arp"], over["arprate"] = steps, rate
            if phase["vib"]:
                got_v = self.res.vibrato(phase["vib"], p, phase["who"] or "pattern command 4")
                if got_v:
                    over["vib"] = got_v
            if phase["ad"] is not None and (phase["ad"] >> 4, phase["ad"] & 15) != (base.a, base.d):
                over["ad"] = (phase["ad"] >> 4, phase["ad"] & 15)
            if phase["sr"] is not None and (phase["sr"] >> 4, phase["sr"] & 15) != (base.s, base.r):
                over["sr"] = (phase["sr"] >> 4, phase["sr"] & 15)
            if phase["pulse"] is not None:
                pf = self._pulse_fields(phase["pulse"])
                if pf != (base.pw, base.pws_from, base.pws_to, base.pws_rate):
                    over["pulse"] = pf
            if self.fsim:
                over.update(self.fsim.over(ch, frame + b0, frame + b1))
            n = b1 - b0
            if id(pc) != drift_piece:
                drift_piece, fcur = id(pc), None
            if phase["porta"] and (arp or base.arp or over.get("arp")):
                rep.drop("instrument '%s': portamento on a note whose wavetable plays an arpeggio (the wavetable sets the pitch again at each step)" % label)
                atoms.append(Atom(n, p, over, 0, part))
            elif phase["porta"]:
                direction, speed = phase["porta"]
                signed = speed if direction == CMD_PORTAUP else -speed
                f0 = fcur if fcur is not None else float(self.timing.sid_freq(note))
                new, fcur = self._glide(note, f0, signed, n, not atoms and fcur is None, over, part)
                atoms.extend(new)
                rep.approx("portamento is played as a glide to the nearest semitone (the player slides the frequency in even steps, chipsynth glides "
                           "in pitch) and is taken to last until the next note or command (not checked against the player)")
            else:
                if fcur is not None:                # a slide that was stopped leaves the pitch where it got to
                    p = max(0, min(107, self._pitch_of(fcur, note)))
                atoms.append(Atom(n, p, over, 0, part))
        if tone and atoms:
            speed = self._speed(data, "tone portamento")
            if speed == 0:
                rep.approx("tone portamento 00 (tie note) changes the pitch of the held note; here the note is struck again")
            elif speed and prev is not None and prev != pitch:
                frames = int(math.ceil(abs(self.timing.sid_freq(pitch) - self.timing.sid_freq(prev)) / float(speed)))
                units = int(math.ceil(frames * self.tpf / 2.0))
                if units > 192:
                    rep.approx("a tone portamento slower than chipsynth's longest slide (192/48 of a beat) arrives early")
                atoms[0].slide = max(1, min(192, units))
            if speed:
                rep.approx("tone portamento: the player slides the held note; here a new note glides in from the last pitch, so its envelope is struck again")
        merged = []
        for a in atoms:
            if merged and not a.slide and not merged[-1].slide and merged[-1].pitch == a.pitch and merged[-1].over == a.over:
                merged[-1].dur += a.dur
            else:
                merged.append(a)
        if len({a.part for a in merged}) > 1 and not stopped:
            rep.approx("instrument '%s': a note changes waveform, pitch or effect part-way through; it is played as several notes in a row, "
                       "so the envelope is struck again at each change" % label)
        return merged


# ------------------------------------------------------------------ the song
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


MID_COMMANDS = (CMD_PORTAUP, CMD_PORTADOWN, CMD_VIBRATO, CMD_SETAD, CMD_SETSR, CMD_SETWAVE, CMD_WAVEPTR, CMD_PULSEPTR)


def _voice_events(rows, resolve, end, loop_frame, tpf, report, ch=0, plan=None, vols=()):
    """One channel's rows -> MML-model events.  `resolve(instrument, pitch, command, data)` names the chipsynth
    instrument for a note (or None when the file has no such instrument).  `plan` (a `_Plan`) plays the notes that
    commands or tables change, `vols` is the master volume as [(frame, 0-15)]."""
    out, pos, octave, cur = [], 0, 4, None
    notes = []                        # (frame, pitch or None for key off, instrument, command, data, commands on the rows after it)
    inst_cur = 0
    sounding = None                   # the commands on the rows after the note that is sounding
    for frame, note, inst, cmd, data, trans in rows:
        if inst:
            inst_cur = inst
        if FIRSTNOTE <= note <= LASTNOTE:
            sounding = []
            notes.append((frame, note - FIRSTNOTE + trans, inst_cur, cmd, data, sounding))
            continue
        if note == KEYOFF:
            notes.append((frame, None, inst_cur, 0, 0, None))
        if plan is not None and cmd in MID_COMMANDS and sounding is not None and note != KEYOFF:
            sounding.append((frame, cmd, data))
        elif cmd == CMD_VIBRATO:
            report.drop("pattern command 'vibrato' on a row without a new note (it changes a note's vibrato part-way)")
        elif plan is not None and cmd in MID_COMMANDS:
            report.drop("pattern command '%s' on a row with no note sounding" % COMMAND_NAMES[cmd])
        elif plan is not None and cmd == CMD_TONEPORTA:
            report.drop("pattern command 'tone portamento' on a row without a new note (it changes where a slide is going)")
        if note == KEYOFF:
            sounding = None
    loop_pending = loop_frame is not None
    items = []
    for i, (frame, pitch, inst, cmd, data, mids) in enumerate(notes):
        nxt = notes[i + 1][0] if i + 1 < len(notes) else end
        items.append((frame, pitch, inst, cmd, data, nxt, [(f - frame, c, d) for f, c, d in mids or ()]))

    def advance(frame):
        nonlocal pos
        t = frame * tpf
        if t > pos:
            out.append(sg.Rest(t - pos))
            pos = t

    def loop_to(frame):
        nonlocal loop_pending
        if loop_pending and loop_frame <= frame:
            advance(loop_frame)
            out.append(sg.Loop())
            loop_pending = False

    nvol, last = 0, None
    for frame, pitch, inst, cmd, data, nxt, mids in items:
        loop_to(frame)
        advance(frame)
        if pitch is None:
            continue
        while nvol < len(vols) and vols[nvol][0] <= frame:
            out.append(sg.Volume(value=vols[nvol][1]))
            nvol += 1
        pitch = max(0, min(107, pitch))
        if plan is not None and plan.needs(inst, cmd, mids, ch, frame, nxt):
            for atom in plan.atoms(inst, pitch, cmd, data, mids, nxt - frame, ch, frame, last):
                name = resolve.variant(inst, atom.over)
                if name != cur:
                    cur = name
                    out.append(sg.Select(name))
                if atom.slide:
                    out.append(sg.Tweak("slide", str(atom.slide)))
                if atom.pitch // 12 != octave:
                    octave = atom.pitch // 12
                    out.append(sg.Octave(set=octave))
                letter, acc = LETTERS[atom.pitch % 12]
                out.append(sg.Note(letter, acc, atom.dur * tpf))
                pos += atom.dur * tpf
                last = atom.pitch
            continue
        name = resolve(inst, pitch, cmd, data)
        if name is not None and name != cur:
            cur = name
            out.append(sg.Select(name))
        if pitch // 12 != octave:
            octave = pitch // 12
            out.append(sg.Octave(set=octave))
        letter, acc = LETTERS[pitch % 12]
        dur = (nxt - frame) * tpf
        out.append(sg.Note(letter, acc, dur))
        pos = frame * tpf + dur
        last = pitch
    loop_to(end)
    advance(end)
    return out


class _Instruments:
    """The chipsynth instruments one song uses: the file's instruments, and a variant of one for each vibrato rate
    and depth that a speed table entry (from the instrument or from pattern command 4) asks for, and for each other change
    of a note's instrument (`variant`)."""

    def __init__(self, tune, bases, song, timing, report):
        self.tune, self.bases, self.song, self.timing, self.report = tune, bases, song, timing, report
        self.names, self.vibs = set(), {}

    def vibrato(self, entry, pitch, who):
        """(rate, depth) of speed table entry `entry` as vibrato on `pitch`, or None (and a line in the report) when it cannot be."""
        key = (entry, pitch)
        if key not in self.vibs:
            self.vibs[key] = _vibrato(self.tune, entry, pitch, self.timing)
        got = self.vibs[key]
        if got[0] is None:
            self.report.drop("vibrato of %s: %s" % (who, got[1]))
            return None
        for n in got[1]:
            self.report.approx(n)
        return got[0]

    def __call__(self, idx, pitch, cmd, data):
        if not 1 <= idx <= len(self.tune.instruments):
            return None
        ins = self.tune.instruments[idx - 1]
        label, base = self.bases[idx]
        if cmd == CMD_VIBRATO:
            entry = data
        elif cmd not in (1, 2, 3) and ins.vibdelay:      # a tempo, ADSR or other command does not stop the instrument's vibrato
            entry = ins.ptr[STBL]
        else:
            entry = 0
        got = None
        if entry:
            got = self.vibrato(entry, pitch, "pattern command 4" if cmd == CMD_VIBRATO else "instrument '%s'" % label)
        name, inst = label, base
        if got:
            rate, depth = got
            name = "%s_vib%d_%d" % (label, rate, depth)
            inst = replace(base, vib_rate=rate, vib_depth=depth)
        if name not in self.song.instruments:
            self.song.instruments[name] = inst
        return name

    def variant(self, idx, over):
        """The instrument `idx` with the changes `over` (vib, wave, arp / arprate, ad, sr, pulse, filt, fs), named after them."""
        label, base = self.bases[idx]
        fields, tags = {}, []
        if over.get("vib"):
            rate, depth = over["vib"]
            fields.update(vib_rate=rate, vib_depth=depth)
            tags.append("vib%d_%d" % (rate, depth))
        if over.get("wave") and over["wave"] != base.wave:
            fields["wave"] = over["wave"]
            tags.append(over["wave"])
        if "arp" in over and (over["arp"] != base.arp or (over["arp"] and over.get("arprate", base.arprate) != base.arprate)):
            fields["arp"] = over["arp"]
            if over["arp"]:
                fields["arprate"] = over["arprate"]
                tags.append("arp" + "_".join(str(x).replace("-", "m") for x in over["arp"]) + "r%d" % over["arprate"])
            else:
                tags.append("arpoff")
        if "ad" in over:
            fields.update(a=over["ad"][0], d=over["ad"][1])
            tags.append("ad%X%X" % over["ad"])
        if "sr" in over:
            fields.update(s=over["sr"][0], r=over["sr"][1])
            tags.append("sr%X%X" % over["sr"])
        if "pulse" in over:
            pw, lo, hi, rate = over["pulse"]
            fields.update(pw=pw, pws_from=lo, pws_to=hi, pws_rate=rate)
            tags.append("pw%d_%d_%d_%d" % over["pulse"])
        if over.get("filt"):
            mode, cut, res = over["filt"]
            fields.update(filt=mode, filt_cut=cut, filt_res=res)
            tags.append("f%s%d_%d" % (mode, cut, res))
        if over.get("fs"):
            lo, hi, rate = over["fs"]
            fields.update(fs_from=lo, fs_to=hi, fs_rate=rate)
            tags.append("fs%d_%d_%d" % over["fs"])
        name = label + "".join("_" + t for t in tags)
        if name not in self.song.instruments:
            self.song.instruments[name] = replace(base, **fields)
        return name


def load(data, region="pal", multispeed=1, subtune=None):
    """`.sng` bytes -> Result.  `region` is 'pal' (50 Hz) or 'ntsc' (60 Hz): the file does not say.
    `multispeed` is 1, 2 or 4.  `subtune` picks one subtune (1-based); the default is all."""
    if multispeed not in (1, 2, 4):
        raise GoatTrackerError("multispeed must be 1, 2 or 4")
    tune = parse(data)
    report = Report()
    pal = region != "ntsc"
    hz = (PAL_HZ if pal else NTSC_HZ)
    report.note("%s timing (%g Hz), %s, multispeed x%d" % ("PAL" if pal else "NTSC", hz, tune.version, multispeed))
    report.note("the file does not record PAL or NTSC, nor multispeed: these come from the options")
    tpf = 4 // multispeed
    timing = Timing(hz * multispeed, 24 * multispeed, SID_CLOCK["pal" if pal else "ntsc"])
    bases, defer = {}, {}
    for i, ins in enumerate(tune.instruments, 1):
        label, inst = _instrument(tune, i, ins, report, timing, defer)
        label = re.sub(r"[^A-Za-z0-9_]", "_", label)
        if not label[0].isalpha():
            label = "i" + label
        while label in [b[0] for b in bases.values()]:
            label += "_"
        bases[i] = (label, inst)
    count = len(tune.orders)
    picks = range(count) if subtune is None else [subtune - 1]
    if subtune is not None and not 1 <= subtune <= count:
        raise GoatTrackerError("this file has %d subtune(s); no subtune %d" % (count, subtune))
    songs = []
    for sub in picks:
        rows, end, _chans, loops, pend0 = _timeline(tune, sub, multispeed, report)
        loop_frame = None
        for c in range(3):
            if loops[c] is not None:
                report.note("subtune %d channel %d restarts at order position %d" % (sub + 1, c + 1, loops[c]))
        title = tune.title or "Song"
        if count > 1:
            title = "%s %d" % (title, sub + 1)
        song = sg.Song(title=title, author=tune.author, bpm=int(round(hz * 60 / 24)), voices=3, dacs=0)
        if tune.copyright:
            song.comment = tune.copyright
        song.tracks = {}
        song.__post_init__()
        resolve = _Instruments(tune, bases, song, timing, report)
        # a loop only makes sense when every channel restarts at the start of the same pattern: use channel 1's
        if loops[0] is not None and loops[0] == 0:
            loop_frame = 0
        elif loops[0] is not None and pend0 is not None:       # funk tempo: the timeline knows the frame
            loop_frame = next((f for sp, f in pend0 if sp >= loops[0]), None)
        elif loops[0] is not None:
            loop_frame = _restart_frame(tune, sub, loops[0], multispeed, report)
        fsim = _FilterSim(tune, rows, end, timing, report)
        plan = _Plan(tune, bases, defer, timing, report, resolve, fsim if fsim.used else None, tpf)
        vols = []
        for f, v in sorted((r[0], r[4]) for c in rows for r in c if r[3] == CMD_MASTERVOL and r[4] < 0x10):
            if not vols or vols[-1][1] != v:
                vols.append((f, v))
        if vols:
            report.approx("master volume changes apply to the notes that start after the change; a note that is sounding keeps its volume")
        if any(r[3] == CMD_MASTERVOL and r[4] >= 0x10 for c in rows for r in c):
            report.note("pattern command D with a number above 15 is the player's timing mark; it is ignored")
        for c in range(3):
            song.tracks["V%d" % (c + 1)] = _voice_events(rows[c], resolve, end, loop_frame, tpf, report, c, plan, vols)
        songs.append(Imported(title, song, {}))
    return Result(tune.title or "GoatTracker song", songs, report)


def _restart_frame(tune, sub, position, mult, report):
    """The frame where channel 1 reaches order position `position` (None when it cannot be found)."""
    ch = _Chan(tune.orders[sub][0], tune.patterns, report, 0)
    tempo, frame = 6 * mult, 0
    for _ in range(MAX_FRAMES):
        if ch.songptr >= position and ch.pending:
            return frame
        got = ch.next_row()
        if got is None:
            return None
        (_n, _i, cmd, data), _t = got
        if cmd == CMD_SETTEMPO and (data & 0x7F) >= 3:
            tempo = data & 0x7F
        frame += tempo
    return None
