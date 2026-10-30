# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""GoatTracker 2 `.sng` files -> chipsynth songs.

Written from the file format as GoatTracker 2.77's loader and player define it (gsong.c, gcommon.h, gplay.c; GPL-2+,
read only, no code copied).  See PLAN-FORMATS.md section 4 for the layout.

Slice 1: the three SID voices become V1-V3, with notes, rests, key-offs, orderlist repeat and transpose, the restart
position as the loop point, and tempo (speed) changes.  Each instrument carries its ADSR, first waveform and starting
pulse width.  The tables are programs; what they do beyond that is listed in the Report, not guessed.

Timing: one frame is 4 ticks (96 ticks per quarter), so a song is exact to the frame at any speed, and the song's bpm is
set from the frame rate (125 on PAL, 150 on NTSC).  Multispeed N plays N frames per video frame and keeps the same grid
by using 4/N ticks per frame (N is 1, 2 or 4).
"""
import struct
from dataclasses import dataclass

from .. import song as sg
from ..synth import Instrument
from .report import Report

PAL_HZ = 50.0
NTSC_HZ = 60.0
MAX_FRAMES = 60 * 60 * 20            # a safety stop: twenty minutes at 60 Hz

REPEAT, TRANSDOWN, TRANSUP, LOOPSONG = 0xD0, 0xE0, 0xF0, 0xFF
FIRSTNOTE, LASTNOTE, REST, KEYOFF, KEYON, ENDPATT = 0x60, 0xBC, 0xBD, 0xBE, 0xBF, 0xFF
WTBL, PTBL, FTBL, STBL = range(4)
CMD_SETAD, CMD_SETSR, CMD_SETWAVE, CMD_SETTEMPO = 5, 6, 7, 15
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


def _instrument(tune, idx, ins, report):
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
        n = 0
        for left in tune.tables[WTBL][0][wp - 1:]:
            n += 1
            if left == 0xFF:
                break
        if n > 1:
            report.drop("instrument '%s': its wavetable program (%d steps: arpeggio, waveform changes)" % (label, n))
    if ins.ptr[PTBL] and ins.ptr[PTBL] - 1 < len(tune.tables[PTBL][0]):
        later = tune.tables[PTBL][0][ins.ptr[PTBL]:]
        if any(v and v < 0x80 for v in later[:1]) or (later and later[0] != 0xFF and later[0] < 0x80):
            report.drop("instrument '%s': its pulse sweep" % label)
    if ins.ptr[FTBL]:
        report.drop("instrument '%s': its filter table" % label)
    if ins.ptr[STBL]:
        report.drop("instrument '%s': its speed table (vibrato / portamento speeds)" % label)
    if ins.vibdelay:
        report.note("instrument '%s': vibrato delay %d is ignored" % (label, ins.vibdelay))
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


def _voice_events(rows, inst_names, end, loop_frame, tpf, trans_report):
    """One channel's rows -> MML-model events."""
    out, pos, octave, cur = [], 0, 4, None
    notes = []                        # (frame, pitch or None for key off, instrument)
    inst_cur = 0
    for frame, note, inst, cmd, data, trans in rows:
        if inst:
            inst_cur = inst
        if FIRSTNOTE <= note <= LASTNOTE:
            notes.append((frame, note - FIRSTNOTE + trans, inst_cur))
        elif note == KEYOFF:
            notes.append((frame, None, inst_cur))
    loop_pending = loop_frame is not None
    items = []
    for i, (frame, pitch, inst) in enumerate(notes):
        nxt = notes[i + 1][0] if i + 1 < len(notes) else end
        items.append((frame, pitch, inst, nxt))

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

    for frame, pitch, inst, nxt in items:
        loop_to(frame)
        advance(frame)
        if pitch is None:
            continue
        pitch = max(0, min(107, pitch))
        if inst != cur and inst in inst_names:
            cur = inst
            out.append(sg.Select(inst_names[inst]))
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
    names = {}
    for i, ins in enumerate(tune.instruments, 1):
        label, inst = _instrument(tune, i, ins, report)
        n = label
        while n in names.values():
            n += "_"
        names[i] = n
        names[("inst", i)] = inst
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
        song = sg.Song(title=title, author=tune.author, bpm=int(round(hz * 60 / 24 * multispeed)), voices=3, dacs=0)
        if tune.copyright:
            song.comment = tune.copyright
        song.tracks = {}
        song.__post_init__()
        used = {r[2] for c in rows for r in c if r[2]}
        for i in sorted(used):
            if i <= len(tune.instruments):
                song.instruments[names[i]] = names[("inst", i)]
        # a loop only makes sense when every channel restarts at the start of the same pattern: use channel 1's
        if loops[0] is not None and loops[0] == 0:
            loop_frame = 0
        elif loops[0] is not None:
            loop_frame = _restart_frame(tune, sub, loops[0], multispeed, report)
        for c in range(3):
            song.tracks["V%d" % (c + 1)] = _voice_events(rows[c], names, end, loop_frame, tpf, report)
        seen = {COMMAND_NAMES[r[3]] for c in rows for r in c if r[3] and r[3] != CMD_SETTEMPO and r[3] < 16}
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
