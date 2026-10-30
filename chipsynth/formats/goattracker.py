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
CMD_VIBRATO, CMD_SETAD, CMD_SETSR, CMD_SETWAVE, CMD_SETTEMPO = 4, 5, 6, 7, 15
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
    while not all(finished) and frame < MAX_FRAMES:
        for c in range(3):
            if finished[c]:
                continue
            if left[c] > 0:
                left[c] -= 1
                continue
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
                    else:
                        tempo = [v, v, v]
                elif v:
                    report.approx("funk or very fast tempo %d is played as %d frames per row" % (data, v + 1))
                    tempo = [v + 1] * 3
                else:
                    report.drop("tempo command 0 (it stops the player's tick)")
            rows[c].append((frame, note, inst, cmd, data, trans))
            left[c] = tempo[c] - 1
        frame += 1
    if frame >= MAX_FRAMES:
        report.note("the song was cut at twenty minutes (no end or loop was found)")
    end = max([frame] + [r[-1][0] + 1 for r in rows if r])
    return rows, end, chans, loops


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
        why.append("it runs commands")
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


def _pulse_program(tune, ptr, timing):
    """Run an instrument's pulse table as the player does.  Returns (fields dict or None, reasons it was not carried,
    approximations).  `fields` has pws_from, pws_to and pws_rate."""
    lcol, rcol = tune.tables[PTBL]
    n = len(lcol)
    pulse, unknown, left = 0, True, 0
    states, series = {}, []
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
        return None, ["it changes the width before setting one"], []
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
        if changes != 2:
            return None, ["it is not a single rise and fall"], []
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


def _instrument(tune, idx, ins, report, timing):
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
        if why:
            report.drop("instrument '%s': its wavetable program (%d steps; %s)" % (label, rows, "; ".join(why)))
    pp = ins.ptr[PTBL]
    if pp and pp - 1 < len(tune.tables[PTBL][0]):
        fields, why, approx = _pulse_program(tune, pp, timing)
        if fields and inst.wave == "pulse":
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
    if ins.ptr[FTBL]:
        report.drop("instrument '%s': its filter table" % label)
    if ins.ptr[STBL] and not ins.vibdelay:
        report.note("instrument '%s': its speed table pointer does nothing without a vibrato delay (the player needs one)" % label)
    elif ins.vibdelay and not ins.ptr[STBL]:
        report.note("instrument '%s': vibrato delay %d is ignored (no speed table entry)" % (label, ins.vibdelay))
    elif ins.vibdelay > 1:
        report.approx("instrument '%s': vibrato delay %d is not kept; vibrato starts at once" % (label, ins.vibdelay))
    return label, inst


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


def _voice_events(rows, resolve, end, loop_frame, tpf, report):
    """One channel's rows -> MML-model events.  `resolve(instrument, pitch, command, data)` names the chipsynth
    instrument for a note (or None when the file has no such instrument)."""
    out, pos, octave, cur = [], 0, 4, None
    notes = []                        # (frame, pitch or None for key off, instrument, command, data)
    inst_cur = 0
    for frame, note, inst, cmd, data, trans in rows:
        if inst:
            inst_cur = inst
        if FIRSTNOTE <= note <= LASTNOTE:
            notes.append((frame, note - FIRSTNOTE + trans, inst_cur, cmd, data))
        else:
            if note == KEYOFF:
                notes.append((frame, None, inst_cur, 0, 0))
            if cmd == CMD_VIBRATO:
                report.drop("pattern command 'vibrato' on a row without a new note (it changes a note's vibrato part-way)")
    loop_pending = loop_frame is not None
    items = []
    for i, (frame, pitch, inst, cmd, data) in enumerate(notes):
        nxt = notes[i + 1][0] if i + 1 < len(notes) else end
        items.append((frame, pitch, inst, cmd, data, nxt))

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

    for frame, pitch, inst, cmd, data, nxt in items:
        loop_to(frame)
        advance(frame)
        if pitch is None:
            continue
        pitch = max(0, min(107, pitch))
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
    loop_to(end)
    advance(end)
    return out


class _Instruments:
    """The chipsynth instruments one song uses: the file's instruments, and a variant of one for each vibrato rate
    and depth that a speed table entry (from the instrument or from pattern command 4) asks for."""

    def __init__(self, tune, bases, song, timing, report):
        self.tune, self.bases, self.song, self.timing, self.report = tune, bases, song, timing, report
        self.names, self.vibs = set(), {}

    def __call__(self, idx, pitch, cmd, data):
        if not 1 <= idx <= len(self.tune.instruments):
            return None
        ins = self.tune.instruments[idx - 1]
        label, base = self.bases[idx]
        if cmd == CMD_VIBRATO:
            entry = data
        elif cmd == 0 and ins.vibdelay:
            entry = ins.ptr[STBL]
        else:
            entry = 0
        got = None
        if entry:
            key = (entry, pitch)
            if key not in self.vibs:
                self.vibs[key] = _vibrato(self.tune, entry, pitch, self.timing)
            got = self.vibs[key]
            if got[0] is None:
                self.report.drop("vibrato of %s: %s" % ("pattern command 4" if cmd == CMD_VIBRATO else "instrument '%s'" % label, got[1]))
                got = None
            else:
                for n in got[1]:
                    self.report.approx(n)
        name, inst = label, base
        if got:
            rate, depth = got[0]
            name = "%s_vib%d_%d" % (label, rate, depth)
            inst = replace(base, vib_rate=rate, vib_depth=depth)
        if name not in self.song.instruments:
            self.song.instruments[name] = inst
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
    bases = {}
    for i, ins in enumerate(tune.instruments, 1):
        label, inst = _instrument(tune, i, ins, report, timing)
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
        rows, end, _chans, loops = _timeline(tune, sub, multispeed, report)
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
        elif loops[0] is not None:
            loop_frame = _restart_frame(tune, sub, loops[0], multispeed, report)
        for c in range(3):
            song.tracks["V%d" % (c + 1)] = _voice_events(rows[c], resolve, end, loop_frame, tpf, report)
        seen = {COMMAND_NAMES[r[3]] for c in rows for r in c if r[3] and r[3] not in (CMD_SETTEMPO, CMD_VIBRATO) and r[3] < 16}
        for cmd in sorted(seen):
            report.drop("pattern command '%s'" % cmd)
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
