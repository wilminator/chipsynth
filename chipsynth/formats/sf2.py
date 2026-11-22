# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""SID Factory II `.sf2` files -> chipsynth songs.

Written from the file format as SID Factory II's own source defines it (build of 2026-11, GPL: `SIDFactoryII/COPYING` is
the GPL v2 text and the sources name no version, so any published version applies; read only, no code copied) and from the
driver notes and user manual that ship with it.  PLAN-FORMATS.md section 9 lists what was read and what was not verified.

What an `.sf2` is: a C64 PRG (two bytes of load address, then memory) holding a driver and the music.  At the load address
is the word 0x1337, then descriptor blocks (id, size, data) that say where everything lives: the driver's name and version,
the tables (their column / row counts, layout and address), the music data (order lists and sequences) and so on.  After the
memory image a pointer at 0x0ffb leads to the editor's own blocks: song names and count, PAL / NTSC, the text of table rows.

Slice 1 (this module): the three SID voices of song N become V1-V3.  Notes, rests, gate-offs, the order list's transpose and
loop (or stop), the tempo table (frames a row, with its wrap), the init table's tempo per song, and per instrument the ADSR,
the waveform, the starting pulse width, and the tables classified like goattracker.py does: a wave table that cycles over
relative semitones becomes `arp`, a pulse table that ramps becomes a sweep.  Commands carried: set tempo, local / persistent
ADSR, wave / pulse program pointers, arpeggio (chord).  Everything else is in the Report.

Timing: one frame is 4 ticks (96 ticks to a quarter); a row is the tempo value plus one frames (a tempo of 5 is the usual six
frames a row); the song's bpm follows the video rate (125 on PAL, 150 on NTSC).  The file records PAL / NTSC (the editor's
setting), which is the default; `region` overrides it.
"""
import re
import struct
from dataclasses import dataclass, field

from .. import song as sg
from ..synth import Instrument
from .envelope import fit_arpeggio
from .report import Report

PAL_HZ = 50.0
NTSC_HZ = 60.0
MAX_ROWS = 60 * 60 * 20 // 3        # a safety stop: rows to play looking for the end or the loop
FILE_ID = 0x1337
AUX_POINTER = 0x0FFB
LETTERS = (("c", 0), ("c", 1), ("d", 0), ("d", 1), ("e", 0), ("f", 0), ("f", 1), ("g", 0), ("g", 1), ("a", 0), ("a", 1), ("b", 0))

# header block ids
B_DESCRIPTOR, B_COMMON, B_TABLES, B_INSTR_DESC, B_MUSIC = 1, 2, 3, 4, 5
B_END = 0xFF
T_INSTRUMENTS, T_COMMANDS = 0x80, 0x81
# auxiliary block types
A_HARDWARE, A_TABLETEXT, A_SONGS = 2, 4, 5

# driver 11 commands (the first byte of a row of the Commands table)
CMD_SLIDE, CMD_VIBRATO, CMD_PORTAMENTO, CMD_ARP, CMD_FRET = 0, 1, 2, 3, 4
CMD_ADSR_NOTE, CMD_ADSR_PERSIST, CMD_FILTER, CMD_WAVE, CMD_PULSE, CMD_TEMPO, CMD_VOLUME, CMD_DEMO = 8, 9, 10, 11, 12, 13, 14, 15
COMMAND_NAMES = {CMD_SLIDE: "slide", CMD_VIBRATO: "vibrato", CMD_PORTAMENTO: "portamento", CMD_ARP: "arpeggio",
                 CMD_FRET: "fret slide", CMD_FILTER: "filter program", CMD_VOLUME: "main volume"}
SUPPORTED_DRIVERS = (11, 12, 13, 14, 15, 16)


class Sf2Error(ValueError):
    pass


def looks_like(data):
    """True for a PRG whose first memory word is the SF2 file id (0x1337)."""
    data = bytes(data[:4])
    return len(data) >= 4 and struct.unpack("<H", data[2:4])[0] == FILE_ID


def _screen_text(raw):
    """The editor's strings: letters are stored as screen codes (1-26 = a-z), the rest as ASCII."""
    out = []
    for b in raw:
        if 1 <= b <= 26:
            out.append(chr(96 + b))
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append("?")
    return "".join(out).strip()


# ------------------------------------------------------------------ the file
@dataclass
class Table:
    type: int
    id: int
    name: str
    layout: int             # 0 row major, 1 column major
    addr: int
    cols: int
    rows: int

    def where(self, col, row):
        if self.layout == 1:
            return self.addr + col * self.rows + row
        return self.addr + row * self.cols + col


@dataclass
class Sf2:
    """What an `.sf2` holds, undigested."""
    mem: bytearray                      # the 64K C64 address space with the file's bytes in place
    load: int
    end: int
    driver_name: str
    version: tuple                      # (major, minor, revision)
    tables: dict                        # lower-case name -> Table
    instrument_cells: list              # the descriptions of the instrument's bytes (text), may be empty
    tracks: int
    order_ptr_low: int
    seq_count: int
    seq_low: int
    seq_high: int
    order_size: int
    order_base: int
    seq_size: int
    seq_base: int
    song_names: list                    # [""] for a file without a song list
    pal: bool                           # False = NTSC; from the file's hardware block, None-like True when absent
    region_known: bool
    row_names: dict = field(default_factory=dict)      # table id -> [text per row] (the first layer)

    @property
    def major(self):
        return self.version[0]

    def cell(self, table, col, row):
        a = table.where(col, row)
        if not 0 <= a < 0x10000:
            raise Sf2Error("table '%s' reaches outside memory" % table.name)
        return self.mem[a]

    def table(self, name):
        return self.tables.get(name.lower())


def parse(data):
    """`.sf2` bytes -> Sf2.  Raises Sf2Error on anything it cannot read."""
    data = bytes(data)
    if len(data) < 8 or not looks_like(data):
        raise Sf2Error("not an SID Factory II song (the word 0x1337 should follow the load address)")
    load = struct.unpack("<H", data[:2])[0]
    body = data[2:]
    if load + len(body) > 0x10000:
        raise Sf2Error("the file does not fit in 64K at its load address")
    mem = bytearray(0x10000)
    mem[load:load + len(body)] = body
    end = load + len(body)

    def w(a):
        return mem[a] | (mem[a + 1] << 8)

    blocks, pos = {}, load + 2
    while True:
        if pos + 2 > end:
            raise Sf2Error("the descriptor blocks run past the end of the file")
        bid = mem[pos]
        if bid == B_END:
            break
        size = mem[pos + 1]
        if pos + 2 + size > end:
            raise Sf2Error("a descriptor block (id %d) runs past the end of the file" % bid)
        if bid in blocks:
            raise Sf2Error("descriptor block %d appears twice" % bid)
        blocks[bid] = bytes(mem[pos + 2:pos + 2 + size])
        pos += 2 + size
    for need, what in ((B_DESCRIPTOR, "descriptor"), (B_TABLES, "driver tables"), (B_MUSIC, "music data")):
        if need not in blocks:
            raise Sf2Error("the %s block is missing" % what)

    # descriptor: type, driver size, name\0, code top, code size, version major, minor, [revision]
    b = blocks[B_DESCRIPTOR]
    try:
        z = b.index(0, 3)
        name = _screen_text(b[3:z])
        major, minor = b[z + 5], b[z + 6]
        rev = b[z + 7] if len(b) > z + 7 else 0
    except (ValueError, IndexError):
        raise Sf2Error("the driver descriptor block is damaged")

    # tables: type, id, text field size, name\0, layout, properties, 3 rule ids, address, columns, rows, visible rows
    tables, b, i = {}, blocks[B_TABLES], 0
    try:
        while b[i] != 0xFF:
            ttype, tid = b[i], b[i + 1]
            z = b.index(0, i + 3)
            tname = _screen_text(b[i + 3:z])
            k = z + 1
            layout = b[k]
            addr, cols, rows = struct.unpack("<HHH", b[k + 5:k + 11])
            tables[tname.lower()] = Table(ttype, tid, tname, layout, addr, cols, rows)
            i = k + 12
    except (ValueError, IndexError, struct.error):
        raise Sf2Error("the driver tables block is damaged")
    for t in tables.values():
        if t.addr < load or t.addr + t.cols * t.rows > end:
            raise Sf2Error("table '%s' lies outside the file (is it truncated?)" % t.name)
    if not any(t.type == T_INSTRUMENTS for t in tables.values()) or not any(t.type == T_COMMANDS for t in tables.values()):
        raise Sf2Error("the file has no Instruments / Commands table")

    cells = []
    if B_INSTR_DESC in blocks and blocks[B_INSTR_DESC]:
        b = blocks[B_INSTR_DESC]
        try:
            i = 1
            for _ in range(b[0]):
                z = b.index(0, i)
                cells.append(_screen_text(b[i:z]))
                i = z + 1
        except (ValueError, IndexError):
            cells = []

    # music data: tracks, order list pointers lo/hi, sequence count, sequence pointers lo/hi, order list size and first,
    # sequence size and first
    try:
        m = blocks[B_MUSIC]
        tracks, order_lo, _order_hi, seq_count, seq_lo, seq_hi, osize, obase, ssize, sbase = struct.unpack("<BHHBHHHHHH", m[:18])
    except struct.error:
        raise Sf2Error("the music data block is damaged")

    for lo, hi, what in ((seq_lo, seq_lo + seq_count, "sequence pointer"), (seq_hi, seq_hi + seq_count, "sequence pointer"),
                         (obase, obase + osize, "order list")):
        if lo < load or hi > end:
            raise Sf2Error("the %s table lies outside the file (is it truncated?)" % what)
    if tracks < 1 or tracks > 12:
        raise Sf2Error("the music data block says %d tracks" % tracks)

    # the editor's own blocks
    names, pal, region_known, row_names = [""], True, False, {}
    aux = w(AUX_POINTER) if load <= AUX_POINTER < end - 1 else 0
    if aux and load <= aux < end:
        p = aux
        while p + 5 <= end:
            atype, _ver, size = mem[p], w(p + 1), w(p + 3)
            ver = w(p + 1)
            p += 5
            if atype == 0 or p + size > end:
                break
            payload = bytes(mem[p:p + size])
            p += size
            try:
                if atype == A_SONGS and size >= 2:
                    count = payload[0]
                    names = [""] * count
                    if ver >= 2:
                        q = 2
                        for n in range(count):
                            ln = payload[q]
                            names[n] = payload[q + 1:q + 1 + ln].decode("latin-1")
                            q += 1 + ln
                elif atype == A_HARDWARE and size >= 2:
                    pal, region_known = payload[1] == 0, True
                elif atype == A_TABLETEXT and ver == 2:
                    q = 1
                    for _ in range(payload[0]):
                        tid = struct.unpack("<I", payload[q:q + 4])[0]
                        layers = struct.unpack("<H", payload[q + 4:q + 6])[0]
                        q += 6
                        for layer in range(layers):
                            n = struct.unpack("<H", payload[q:q + 2])[0]
                            q += 2
                            texts = []
                            for _k in range(n):
                                ln = payload[q]
                                texts.append(payload[q + 1:q + 1 + ln].decode("latin-1"))
                                q += 1 + ln
                            if layer == 0:
                                row_names[tid] = texts
            except (IndexError, struct.error):
                pass                      # the editor's extras are never needed: a damaged one is skipped
    if not names:
        names = [""]
    return Sf2(mem, load, end, name, (major, minor, rev), tables, cells, tracks, order_lo, seq_count, seq_lo, seq_hi,
               osize, obase, ssize, sbase, names, pal, region_known, row_names)


# ------------------------------------------------------------------ order lists and sequences
def decode_sequence(f, index):
    """Sequence `index` -> list of rows (note, instrument or None, command or None, tied).  A row is one tick of the song
    clock; an event with a duration of n is followed by n continuation rows (note None: nothing happens on them)."""
    if not 0 <= index < f.seq_count:
        return None
    addr = f.mem[f.seq_low + index] | (f.mem[f.seq_high + index] << 8)
    if not f.load <= addr < f.end:
        return None
    rows, i, dur = [], 0, 0
    while i < 0x100 and addr + i + 4 < 0x10000:
        v = f.mem[addr + i]
        i += 1
        if v == 0x7F:
            return rows
        cmd = inst = None
        tie = False
        if v >= 0xC0:
            cmd, v = v & 0x3F, f.mem[addr + i]
            i += 1
        if v >= 0xA0:
            inst, v = v & 0x1F, f.mem[addr + i]
            i += 1
        if v >= 0x80:
            dur, tie = v & 0x0F, bool(v & 0x10)
            v = f.mem[addr + i]
            i += 1
        if v >= 0x80:
            return None                 # damaged: a note byte is above 0x7f
        rows.append((v, inst, cmd, tie))
        rows.extend([(None, None, None, False)] * dur)
    return None                         # no end marker


class _Track:
    """One voice's sequencer: the order list at a byte position with its transpose, and the sequence being played."""

    def __init__(self, f, song, t):
        self.f = f
        self.base = f.order_base + (song * f.tracks + t) * f.order_size
        self.pos, self.trans = 0, 0
        self.rows, self.row = None, 0
        self.ended = False
        self.looped = False
        self.bad = set()

    def key(self):
        # the position within the sequence plus which sequence (by order list position it came from)
        return (self.ended, self.pos, self.trans, self.row)

    def _fetch(self, report, t):
        mem, base = self.f.mem, self.base
        for _ in range(64):
            if self.pos >= self.f.order_size:
                self.ended = True
                return
            b = mem[base + self.pos]
            if b == 0xFE:
                self.ended = True
                return
            if b == 0xFF:
                target = mem[base + self.pos + 1]
                self.pos = target
                self.looped = True
                continue
            if b >= 0x80:
                self.trans = (b & 0x7F) - 0x20
                self.pos += 1
                continue
            self.pos += 1
            rows = decode_sequence(self.f, b)
            if rows is None:
                if b not in self.bad:
                    self.bad.add(b)
                    report.drop("track %d: order list names sequence %02x, which is missing or damaged" % (t + 1, b))
                continue
            if not rows:
                continue
            self.rows, self.row, self.seq = rows, 0, b
            return
        self.ended = True

    def next_row(self, report, t):
        """The next row (note, instrument, command, tied) with the transpose, or None when the track has ended.  The next
        sequence is fetched as soon as the last row of this one is taken, so between two rows the track's state (order list
        position, transpose, row) always names the row that comes next."""
        if self.ended:
            return None
        if self.rows is None:
            self._fetch(report, t)
            if self.ended:
                return None
        row, trans = self.rows[self.row], self.trans
        self.row += 1
        if self.row >= len(self.rows):
            self._fetch(report, t)
        return row, trans


def _tempo_chain(f, start):
    """The tempo table from `start` up to its wrap byte (0x7f): a list of frames per row (value + 1)."""
    table = f.table("tempo")
    out = []
    for k in range(start, table.rows):
        v = f.cell(table, 0, k)
        if v == 0x7F:
            break
        out.append(v + 1)
        if len(out) > 64:
            break
    return out


def _timeline(f, song, report, ntracks):
    """Play the tracks row by row.  Returns (rows per track as [(frame, note, instrument, command, tied, transpose)],
    end frame, loop frame or None)."""
    tempo_t = f.table("tempo")
    init = f.table("init")
    start = 0
    if init is not None and song < init.rows:
        start = f.cell(init, 0, song)
    chain = _tempo_chain(f, start) if tempo_t is not None else []
    if not chain:
        report.approx("no tempo table: 6 frames a row is used" if tempo_t is None else "the tempo table is empty at row %d: 6 frames a row is used" % start)
        chain = [6]
    cmds = f.table("commands")
    tracks = [_Track(f, song, t) for t in range(ntracks)]
    rows = [[] for _ in tracks]
    seen, history = {}, []
    ci, frame = 0, 0
    loop_frame, end_frame = None, None
    for n in range(MAX_ROWS):
        key = (tuple(tr.key() for tr in tracks), tuple(chain), ci % len(chain))
        # a repeat of the whole state is the loop point (the sequence is part of the position: the byte position and the row)
        if key in seen:
            loop_frame = history[seen[key]]
            end_frame = frame
            break
        seen[key] = len(history)
        history.append(frame)
        tempo_cmd = None
        alive = False
        for t, tr in enumerate(tracks):
            got = tr.next_row(report, t)
            if got is None:
                continue
            alive = True
            (note, inst, cmd, tied), trans = got
            rows[t].append((frame, note, inst, cmd, tied, trans))
            if cmd is not None and cmds is not None and cmd < cmds.rows and f.cell(cmds, 0, cmd) == CMD_TEMPO and f.major == 11:
                tempo_cmd = f.cell(cmds, 2, cmd)
        if not alive:
            end_frame = frame
            break
        frame += chain[ci % len(chain)]
        ci += 1
        if tempo_cmd is not None and tempo_t is not None:
            new = _tempo_chain(f, tempo_cmd)
            if new:
                chain, ci = new, 0
            else:
                report.drop("a tempo command points at an empty tempo program (row %d)" % tempo_cmd)
    else:
        end_frame = frame
        report.note("the song was cut after %d rows (no end or loop was found)" % MAX_ROWS)
    return rows, end_frame, loop_frame


# ------------------------------------------------------------------ instruments
@dataclass
class Timing:
    fps: float
    beat: int


def _wave_of(bits):
    w = bits & 0xF0
    for flag, name in ((0x80, "noise"), (0x40, "pulse"), (0x20, "saw"), (0x10, "tri")):
        if w & flag:
            return name, w == flag
    return None, False


def _percent(width):
    return max(1, min(99, round(width * 100 / 4096)))


def _run_wave(f, table, start):
    """Run a wave table from row `start`, a row a frame; a jump (0x7f, row) is free and a jump to its own row ends the
    program.  Returns (frames as [(waveform, semis)], index where the repeat begins or None when the last row holds)."""
    n = table.rows
    ptr, series, seen = start, [], {}
    for _ in range(600):
        hops = 0
        while ptr < n and f.cell(table, 0, ptr) == 0x7F:
            target = f.cell(table, 1, ptr)
            if target == ptr or hops > 300:
                return series, None
            ptr, hops = target, hops + 1
        if ptr >= n:
            return series, None
        if ptr in seen:
            return series, seen[ptr]
        seen[ptr] = len(series)
        series.append((f.cell(table, 0, ptr), f.cell(table, 1, ptr)))
        ptr += 1
    return series, None


def _run_pulse(f, table, start):
    """Run a pulse table: set rows (0x8X XX, frames) hold a width, add rows (0x0X XX, frames) add a signed 12 bit step a
    frame; 0x7f jumps (a jump to its own row ends it).  Returns (series of widths, repeat index or None, unknown)."""
    n = table.rows
    ptr, pulse, unknown, left = start, 0, True, 0
    states, series = {}, []
    for fr in range(2048):
        hops = 0
        while ptr < n and f.cell(table, 0, ptr) == 0x7F:
            target = f.cell(table, 2, ptr)
            if target == ptr or hops > 300:
                ptr, left = n, 0
                break
            ptr, hops = target, hops + 1
        guard = 0
        while ptr < n and left == 0 and guard < 64:
            guard += 1
            c0, c1, c2 = (f.cell(table, 0, ptr), f.cell(table, 1, ptr), f.cell(table, 2, ptr))
            if c0 == 0x7F:
                break
            if c0 >= 0x80:
                pulse, unknown = ((c0 & 0x0F) << 8) | c1, False
                if c2:
                    left = c2
                    break
                ptr += 1
            else:
                if c2:
                    left = c2
                    break
                ptr += 1
        if ptr < n and left:
            c0, c1 = f.cell(table, 0, ptr), f.cell(table, 1, ptr)
            if c0 < 0x80:
                step = ((c0 & 0x0F) << 8) | c1
                if step >= 0x800:
                    step -= 0x1000
                pulse = (pulse + step) & 0xFFF
            left -= 1
            if not left:
                ptr += 1
        series.append(pulse)
        state = (ptr if ptr < n else -1, left, pulse)
        if state in states:
            return series, states[state], unknown
        states[state] = fr
    return series, None, unknown


def _classify_pulse(series, repeat, timing):
    """Pulse widths a frame -> (fields dict or None, reasons it was not carried, approximations)."""
    if repeat is None:
        return None, ["it does not settle into a repeat"], []
    cyc, pre = series[repeat + 1:], series[:repeat + 1]
    approx = []
    if not cyc:
        return None, [], []
    if len(set(cyc)) == 1:
        end = cyc[0]
        if all(v == end for v in pre):
            return None, [], []
        steps = [b - a for a, b in zip(pre, pre[1:] + [end])]
        if not (all(s >= 0 for s in steps) or all(s <= 0 for s in steps)):
            return None, ["it is not a plain ramp"], []
        reach = next(k for k, v in enumerate(pre + [end]) if v == end)
        approx.append("a one-way ramp is played as a back-and-forth sweep")
        lo_hi, period = (pre[0], end), 2 * max(1, reach)
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


def _run_arp(f, table, start):
    """The arpeggio table from row `start`: values below 0x70 add semitones, 0x7X jumps to start + X (the driver notes' own
    example).  Returns (the lead-in, the repeating cycle) as lists of semitones, or None when it never repeats."""
    n = table.rows
    ptr, seq, seen = start, [], {}
    for _ in range(300):
        if ptr >= n:
            return None
        if ptr in seen:
            return seq[:seen[ptr]], seq[seen[ptr]:]
        seen[ptr] = len(seq)
        v = f.cell(table, 0, ptr)
        if 0x70 <= v < 0x80:
            ptr = start + (v & 0x0F)
            if ptr in seen:
                return seq[:seen[ptr]], seq[seen[ptr]:]
            continue
        if v >= 0x80:
            return None
        seq.append(v)
        ptr += 1
    return None


class _Instruments:
    """The chipsynth instruments the song uses: a base per instrument, and a variant for each command that changes how a note
    sounds (local / persistent ADSR, wave / pulse program, arpeggio)."""

    def __init__(self, f, song, timing, report):
        self.f, self.song, self.timing, self.report = f, song, timing, report
        self.instr = f.table("instruments")
        self.wave, self.pulse = f.table("wave"), f.table("pulse")
        self.arp = f.table("arp")
        self.cache, self.names = {}, set()
        self.full = f.major in (11, 14) and self.instr.cols >= 6
        self.simple = f.major in (12, 13, 15, 16)
        self.labels = {}
        names = f.row_names.get(self.instr.id, [])
        for i in range(self.instr.rows):
            label = re.sub(r"[^A-Za-z0-9_]", "_", names[i].strip()) if i < len(names) and names[i].strip() else ""
            if not label or not label[0].isalpha():
                label = "i%02x%s" % (i, "_" + label if label else "")
            self.labels[i] = label

    def col(self, c, i):
        return self.f.cell(self.instr, c, i) if c < self.instr.cols else 0

    def build(self, i, ov):
        """Instrument i with overrides ov (dict: adsr, wave, pulse, arp) -> (Instrument, shift, fixed pitch or None)."""
        f, rep = self.f, self.report
        label = self.labels[i]
        inst = Instrument()
        ad, sr = ov.get("adsr") or (self.col(0, i), self.col(1, i))
        inst.a, inst.d, inst.s, inst.r = ad >> 4, ad & 15, sr >> 4, sr & 15
        shift, fixed, wave_bits, pw12 = 0, None, None, None
        wave_ptr = pulse_ptr = None
        if self.full or f.major in (15, 16):
            wave_ptr = ov.get("wave", self.col(5 if self.full else 4, i))
            if self.full:
                pulse_ptr = ov.get("pulse", self.col(4, i))
                flags = self.col(2, i)
                if flags & 0x40 and f.major == 11:
                    rep.drop("instrument '%s': its filter program (the filter is not carried)" % label)
                if f.major == 14 and flags & 0x40:
                    rep.drop("instrument '%s': its filter program (the filter is not carried)" % label)
            else:
                width = self.col(2, i)
                pw12 = ((width & 0x0F) << 8) | (width & 0xF0)
                if self.col(3, i):
                    rep.drop("instrument '%s': its linear pulse sweep" % label)
        elif f.major in (12, 13):
            wave_bits = self.col(2, i)
            width = self.col(3, i)
            pw12 = ((width & 0x0F) << 8) | (width & 0xF0) if f.major == 12 else (width & 0x0F) << 8
            if f.major == 13:
                rep.drop("instrument '%s': its Hubbard pulsating, sweep, dive, noise and arpeggio settings" % label)
        else:
            rep.drop("driver %s: only the ADSR of instrument '%s' is read" % (f.driver_name, label))
        if wave_ptr is not None and self.wave is not None:
            wave_bits, shift, fixed = self._wave_program(label, wave_ptr, inst)
        elif wave_ptr is not None:
            rep.drop("instrument '%s': the driver has no wave table" % label)
        if wave_bits is not None:
            wave, exact = _wave_of(wave_bits)
            if wave is None:
                rep.approx("instrument '%s': no waveform found (%02x), so pulse is used" % (label, wave_bits))
                wave = "pulse"
            elif not exact:
                rep.approx("instrument '%s': waveform %02x combines waves, so %s is used" % (label, wave_bits & 0xF0, wave))
            if wave_bits & 0x06:
                rep.approx("instrument '%s': ring modulation / hard sync (waveform bits %02x) are not carried" % (label, wave_bits & 0x06))
            inst.wave = wave
        elif wave_ptr is None:
            inst.wave = "pulse"
        if pw12 is not None:
            inst.pw = _percent(pw12) if pw12 else 50
        if pulse_ptr is not None and self.pulse is not None:
            self._pulse_program(label, pulse_ptr, inst)
        if ov.get("arp") is not None and self.arp is not None:
            self._arp_command(label, ov["arp"], inst, shift)
        return inst, shift, fixed

    def _wave_program(self, label, ptr, inst):
        f, rep = self.f, self.report
        series, loop = _run_wave(f, self.wave, ptr)
        if not series:
            rep.approx("instrument '%s': its wave program is empty (wave row %d), so pulse with no pitch change is used" % (label, ptr))
            return 0x41, 0, None
        steady = series[loop:] if loop is not None else series[-1:]
        pre = series[:loop] if loop is not None else series[:-1]
        timeline = (pre + steady * 48)[:48]
        counts = {}
        for k, (wv, _s) in enumerate(timeline):
            cls = _wave_of(wv)[0]
            if cls:
                counts.setdefault(cls, [0, k, wv])[0] += 1
        if counts:
            best = sorted(counts.values(), key=lambda c: (-c[0], c[1]))[0]
            wave_bits = best[2]
        else:
            wave_bits = series[0][0]
        if len({_wave_of(wv)[0] for wv, _s in series} - {None}) > 1:
            rep.approx("instrument '%s': its wave program changes waveform; the one played longest is used" % label)
        absolute = [s for _w, s in steady if s >= 0x80]
        shift, fixed = 0, None
        if absolute:
            fixed = absolute[0] - 0x80
            if fixed > 107:
                rep.approx("instrument '%s': its fixed note %d is above the top of the driver's 96-note table (a noise pitch trick); 107 is used" % (label, fixed))
            if len(set(s for _w, s in steady)) > 1:
                rep.approx("instrument '%s': its wave program steps through several fixed notes; the first is used" % label)
            else:
                rep.note("instrument '%s': its wave program plays the fixed note %d" % (label, fixed))
        else:
            offsets = [s for _w, s in steady]
            if len(set(offsets)) == 1:
                shift = offsets[0]
            else:
                arp, rate, exact = fit_arpeggio(offsets, 0, self.timing.beat)
                if rate > 32:
                    rate, exact = 32, False
                inst.arp, inst.arprate = arp, rate
                rep.note("instrument '%s': its wave table is an arpeggio of %s semitones, %d steps a beat" % (label, ",".join(map(str, arp)), rate))
                if not exact:
                    rep.approx("instrument '%s': the arpeggio is not a cycle of equal steps and 8 or fewer notes; it is rounded" % label)
        if pre and any(s != (steady[0][1] if steady else 0) for _w, s in pre):
            rep.approx("instrument '%s': its first %d frame(s) of pitch differ from the repeating part; the repeating part is used" % (label, len(pre)))
        return wave_bits, shift, fixed

    def _pulse_program(self, label, ptr, inst):
        f, rep = self.f, self.report
        if ptr >= self.pulse.rows:
            rep.drop("instrument '%s': its pulse program (the pointer is outside the table)" % label)
            return
        first = [f.cell(self.pulse, c, ptr) for c in range(3)]
        if first[0] >= 0x80 and first[0] != 0x7F:
            inst.pw = _percent(((first[0] & 0x0F) << 8) | first[1]) if (((first[0] & 0x0F) << 8) | first[1]) else inst.pw
        series, repeat, unknown = _run_pulse(f, self.pulse, ptr)
        if unknown and any(v for v in series):
            rep.drop("instrument '%s': its pulse program (it changes the width before setting one)" % label)
            return
        fields, why, approx = _classify_pulse(series, repeat, self.timing)
        if fields and inst.wave == "pulse":
            for k, v in fields.items():
                setattr(inst, k, v)
            rep.note("instrument '%s': its pulse table is a sweep between %d%% and %d%%" % (label, fields["pws_from"], fields["pws_to"]))
            for a in approx:
                rep.approx("instrument '%s': %s" % (label, a))
        elif fields:
            rep.note("instrument '%s': its pulse sweep is ignored because the waveform is %s" % (label, inst.wave))
        if why:
            rep.drop("instrument '%s': its pulse sweep (%s)" % (label, "; ".join(why)))

    def _arp_command(self, label, arp, inst, shift):
        index, speed = arp
        got = _run_arp(self.f, self.arp, index)
        if not got or not got[1]:
            self.report.drop("arpeggio command: table row %d does not make a repeating chord" % index)
            return
        lead, cycle = got
        if lead:
            self.report.approx("arpeggio command: the lead-in step(s) %s before the repeat are not played" % ",".join(map(str, lead)))
        if shift or inst.arp:
            self.report.drop("arpeggio command on instrument '%s' (the instrument's own wave program already moves the pitch)" % label)
            return
        step = speed + 1
        rate = max(1, min(32, round(self.timing.beat / step)))
        inst.arp, inst.arprate = tuple(cycle[:8]), rate
        self.report.approx("arpeggio command: the step length is taken as speed + 1 = %d frame(s) (the driver's own count was not verified)" % step)
        if len(cycle) > 8:
            self.report.approx("arpeggio command: only the first 8 of %d steps are kept" % len(cycle))

    def resolve(self, i, ov, pitch):
        """(chipsynth instrument name, pitch) for a note of instrument i with the overrides ov."""
        if not 0 <= i < self.instr.rows:
            return None, pitch
        key = (i, tuple(sorted(ov.items())))
        if key not in self.cache:
            inst, shift, fixed = self.build(i, ov)
            name = self.labels[i]
            for k, v in sorted(ov.items()):
                name += "_" + k + ("%02x%02x" % v if isinstance(v, tuple) and len(v) == 2 else "%02x" % v if isinstance(v, int) else "")
            name = re.sub(r"[^A-Za-z0-9_]", "_", name)
            while name in self.names:
                name += "_"
            self.names.add(name)
            self.song.instruments[name] = inst
            self.cache[key] = (name, shift, fixed)
        name, shift, fixed = self.cache[key]
        return name, (fixed if fixed is not None else pitch + shift)


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


def _voice(rows, f, cmds, resolver, end, loop_frame, tpf, report, t):
    """One track's rows -> MML-model events."""
    items = []                          # (frame, pitch or None, instrument, overrides)
    inst, persist, last_pitch, gate = 0, None, None, False
    ties = 0
    for frame, note, new_inst, cmd, tied, trans in rows:
        if new_inst is not None and new_inst != inst:
            inst, persist = new_inst, None
        ctype = None
        if cmd is not None and cmds is not None and cmd < cmds.rows:
            ctype = f.cell(cmds, 0, cmd)
            p1 = f.cell(cmds, 1, cmd) if cmds.cols > 1 else 0
            p2 = f.cell(cmds, 2, cmd) if cmds.cols > 2 else 0
        local = {}
        if ctype is not None and f.major in (11, 14):
            if ctype == CMD_ADSR_PERSIST and f.major == 11:
                persist = (p1, p2)
            elif ctype == CMD_ADSR_NOTE and f.major == 11:
                local["adsr"] = (p1, p2)
            elif ctype == CMD_WAVE and f.major == 11:
                local["wave"] = p2
            elif ctype == CMD_PULSE and f.major == 11:
                local["pulse"] = p2
            elif ctype == CMD_ARP and f.major == 11:
                local["arp"] = (p2, p1)
            elif ctype in (CMD_TEMPO, CMD_DEMO) and f.major == 11:
                pass
            elif ctype in COMMAND_NAMES:
                report.drop("command '%s' (%s)" % (COMMAND_NAMES[ctype], "not carried" if f.major == 11 else "driver %d" % f.major))
            else:
                report.drop("command %02x (not known for this driver)" % ctype)
        elif ctype is not None:
            report.drop("commands of driver %s (their meaning differs by driver and was not verified)" % f.driver_name)
        if persist and "adsr" not in local:
            local["adsr"] = persist
        if note is None:
            continue
        if 1 <= note < 0x70:
            if tied:
                ties += 1
            last_pitch = note + trans
            gate = True
            items.append((frame, last_pitch, inst, local))
        elif note == 0:
            if gate:
                items.append((frame, None, inst, {}))
            gate = False
        elif note == 0x7E and not gate and last_pitch is not None:
            gate = True
            items.append((frame, last_pitch, inst, local))
    if ties:
        report.approx("tied notes (%d) are played as new notes: a tie keeps the envelope running in SID Factory II" % ties)
    out, pos, octave, cur = [], 0, 4, None
    loop_pending = loop_frame is not None

    def advance(frame):
        nonlocal pos
        tick = frame * tpf
        if tick > pos:
            out.append(sg.Rest(tick - pos))
            pos = tick

    def loop_to(frame):
        nonlocal loop_pending
        if loop_pending and loop_frame <= frame:
            advance(loop_frame)
            out.append(sg.Loop())
            loop_pending = False

    for k, (frame, pitch, ins, ov) in enumerate(items):
        nxt = items[k + 1][0] if k + 1 < len(items) else end
        loop_to(frame)
        advance(frame)
        if pitch is None:
            continue
        name, pitch = resolver.resolve(ins, ov, pitch)
        if name is None:
            report.drop("track %d: a note uses instrument %02x, which the file does not have" % (t + 1, ins))
            continue
        pitch = max(0, min(107, pitch))
        if name != cur:
            cur = name
            out.append(sg.Select(name))
        if pitch // 12 != octave:
            octave = pitch // 12
            out.append(sg.Octave(set=octave))
        letter, acc = LETTERS[pitch % 12]
        dur = max(1, (nxt - frame) * tpf)
        out.append(sg.Note(letter, acc, dur))
        pos = frame * tpf + dur
    loop_to(end)
    advance(end)
    return out


def load(data, region=None, song=None):
    """`.sf2` bytes -> Result.  `region` is 'pal', 'ntsc' or None (what the file says; PAL when it does not).
    `song` picks one song of the file (1-based); the default is all."""
    f = parse(data)
    report = Report()
    if region not in (None, "pal", "ntsc"):
        raise Sf2Error("region must be 'pal' or 'ntsc'")
    pal = f.pal if region is None else region == "pal"
    hz = PAL_HZ if pal else NTSC_HZ
    report.note("%s, driver %d.%02d.%02d%s; %s timing (%g Hz)" % (
        f.driver_name, f.version[0], f.version[1], f.version[2], "" if f.tracks == 3 else ", %d tracks" % f.tracks,
        "PAL" if pal else "NTSC", hz))
    if region is None and not f.region_known:
        report.note("the file does not record PAL or NTSC: PAL is used")
    if f.major not in SUPPORTED_DRIVERS or not f.driver_name.lower().startswith("driver"):
        report.drop("driver '%s' (%d.%02d) is not one of the standard drivers 11-16: notes and timing are read, instruments are not" % (
            f.driver_name, f.version[0], f.version[1]))
    if f.tracks > 3:
        report.drop("tracks 4-%d (SID %d and up): a song has three SID voices here" % (f.tracks, 2))
    count = len(f.song_names)
    if song is not None and not 1 <= song <= count:
        raise Sf2Error("this file has %d song(s); no song %d" % (count, song))
    picks = range(count) if song is None else [song - 1]
    if f.table("instruments") is None or f.table("commands") is None:
        raise Sf2Error("the file has no Instruments / Commands table")
    songs = []
    for s in picks:
        rows, end, loop_frame = _timeline(f, s, report, min(3, f.tracks))
        title = f.song_names[s] or "Song"
        if count > 1 and not f.song_names[s]:
            title = "Song %d" % (s + 1)
        sng = sg.Song(title=title, bpm=int(round(hz * 60 / 24)), voices=3, dacs=0)
        sng.tracks = {}
        sng.__post_init__()
        timing = Timing(hz, 24)
        resolver = _Instruments(f, sng, timing, report)
        cmds = f.table("commands")
        for t in range(3):
            sng.tracks["V%d" % (t + 1)] = _voice(rows[t] if t < len(rows) else [], f, cmds, resolver, end, loop_frame, 4, report, t)
        if loop_frame is not None:
            report.note("song %d loops back to its frame %d" % (s + 1, loop_frame))
        songs.append(Imported(title, sng, {}))
    return Result("SF2 song", songs, report)
