# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""chipsynth songs -> GoatTracker 2 `.sng` files (GTS5).

Written from the file format as GoatTracker 2.77's loader and player define it (gsong.c, gcommon.h, gplay.c; GPL-2+,
read only, no code copied) and checked against the reader in goattracker.py: what is written must read back (tests).

V1-V3 become the three SID voices.  Everything else the song holds is left out and said so in the Report: more
voices, the DACs, echo, reverb, detune, per-note volume and pan, slides, the filter, and the noise mode.

Timing: the song is cut into rows of `g` ticks (the coarsest grid that every note start, end and tempo change lies
on, at most a sixteenth note), a row is at least 3 player frames (the gate timer needs them), and a row's length in
frames comes from the song's tempo, rounded cumulatively so that no row start is more than half a frame out.  When the
rows are not all the same length the tempo is changed on the rows that differ (command F on voice 1), which is exact.
`tempo="speed"` writes one speed instead (the nearest whole number of frames a row) and reports the error.

Instruments: ADSR, waveform and pulse width go in as they are.  An arpeggio becomes a looping wavetable, a pulse-width
sweep a pulse table that ramps up and down, and vibrato a speed table entry used by the instrument: the inverse of how
goattracker.py classifies the tables.  What does not fit is approximated or dropped, and listed.
"""
import math
import re
from dataclasses import dataclass
from functools import reduce
from types import SimpleNamespace

from . import goattracker as gt
from .report import Report

MIN_ROW = 3                  # frames: the gate timer (2) plus one
MAX_ROW = 127                # the largest speed a tempo command can set
MAX_ORDER = 253              # patterns in an order list (the list also holds the loop byte and its position)
MAX_PATTERNS, MAX_INSTRUMENTS, MAX_TABLE, MAX_PATTERN_ROWS = 208, 63, 255, 127
GATE_TIMER, FIRST_WAVE = 0x02, 0x09
MAX_NOTE = gt.LASTNOTE - gt.FIRSTNOTE
WAVES = {"pulse": 0x41, "saw": 0x21, "tri": 0x11, "noise": 0x81, "sine": 0x11}
VIB_NOTE = 48                # the note a vibrato entry is fitted at (the entries are relative, so any will do)


class GoatTrackerExportError(ValueError):
    pass


def dump(tune, region="pal", multispeed=1, name=None, tempo="exact"):
    """A compiled Tune -> (`.sng` bytes, Report).  `region` is 'pal' or 'ntsc' (the file does not record it, but the
    frame rate decides how many frames a row lasts); `multispeed` 1, 2 or 4; `tempo` 'exact' or 'speed'."""
    if multispeed not in (1, 2, 4):
        raise GoatTrackerExportError("multispeed must be 1, 2 or 4")
    if region not in ("pal", "ntsc"):
        raise GoatTrackerExportError("region must be pal or ntsc")
    if tempo not in ("exact", "speed"):
        raise GoatTrackerExportError("tempo must be exact or speed")
    return _Writer(tune, region, multispeed, name, tempo).run()


def _text32(s, report, what):
    raw = (s or "").encode("latin-1", "replace")
    if len(raw) > 31:
        report.approx("the %s is cut to 31 characters" % what)
    return raw[:31].ljust(32, b"\0")


def _ident(s):
    return re.sub(r"[^A-Za-z0-9_]", "_", s) or "i"


class _Table:
    """One of the player's tables: programs of (left, right) rows.  A jump row is (0xFF, k): k is a row of the same
    program (1 = its first) or 0 to stop; it is made absolute when the program is added."""

    def __init__(self):
        self.left, self.right, self.index = [], [], {}

    def add(self, program):
        """The 1-based pointer of `program` (an identical one is shared), or None when the table is full."""
        key = tuple(program)
        if key in self.index:
            return self.index[key]
        if len(self.left) + len(program) > MAX_TABLE:
            return None
        base = len(self.left)
        for left, right in program:
            self.left.append(left)
            self.right.append(right + base if left == 0xFF and right else right)
        self.index[key] = base + 1
        return base + 1


@dataclass
class _Ins:
    key: tuple
    name: str
    inst: object            # the chipsynth Instrument it was made from
    shift: int = 0          # semitones added to every written note (an arpeggio that goes below the note counts up from it)
    ptr: tuple = (0, 0, 0, 0)
    vibdelay: int = 0


class _Writer:
    def __init__(self, tune, region, multispeed, name, tempo):
        self.t, self.region, self.mult, self.name, self.tempo_mode = tune, region, multispeed, name, tempo
        self.hz = gt.PAL_HZ if region == "pal" else gt.NTSC_HZ
        self.fps = self.hz * multispeed
        self.r = Report()
        self.names = {}
        for n, i in tune.instruments.items():
            self.names.setdefault(tuple(vars(i).items()), n)
        self.timing = gt.Timing(self.fps, 24 * multispeed, gt.SID_CLOCK[region])
        self.tables = [_Table() for _ in range(4)]
        self.instruments = []
        self.by_key = {}
        self.vib_cache = {}
        self.moved = 0

    # ------------------------------------------------------------------ time
    def breaks(self):
        t = self.t
        tempos = {0: t.bpm}
        for tick, b in t.tempo_events:
            tempos[tick] = b
        out, secs = [], 0.0
        ticks = sorted(tempos)
        for k, tick in enumerate(ticks):
            out.append((tick, secs, tempos[tick]))
            if k + 1 < len(ticks):
                secs += (ticks[k + 1] - tick) * 60.0 / (tempos[tick] * 96)
        return out

    def frames_at(self, tick):
        tick_, secs, bpm = [b for b in self._breaks if b[0] <= tick][-1]
        return (secs + (tick - tick_) * 60.0 / (bpm * 96)) * self.fps

    # ------------------------------------------------------------------ the grid
    def grid(self, voices):
        t, r = self.t, self.r
        ticks = {t.bar_ticks, t.loop_tick, t.length} | {tk for tk, _ in t.tempo_events}
        for v in voices:
            for n in v:
                ticks |= {n.start, n.start + n.dur}
        ticks.discard(0)
        g = reduce(math.gcd, ticks) if ticks else 24
        if g > 24:
            g = max(d for d in range(1, 25) if g % d == 0)
        fastest = max(60.0 / (b * 96) * self.fps for _, _, b in self._breaks)      # frames a tick, at the fastest tempo
        need = MIN_ROW / fastest
        if g < need:
            g2 = next((c for c in (12, 16, 24, 32, 48, 96) if c >= need), 96)
            r.approx("the song's finest timing is %d ticks but a row must last %d frames at this tempo: notes are moved to the nearest "
                     "row of %d ticks (1/%g of a quarter note)" % (g, MIN_ROW, g2, 96 / g2))
            g = g2
        return g

    def row_lengths(self, g, rows):
        """Frames each row lasts, so that row k starts at the whole frame nearest its exact time."""
        exact = [self.frames_at(k * g) for k in range(rows + 1)]
        lens, pos = [], 0
        if self.tempo_mode == "speed":
            c = max(MIN_ROW, min(MAX_ROW, round(exact[-1] / rows))) if rows else 6
            lens = [c] * rows
            err = abs(c * rows - exact[-1]) / exact[-1] * 100 if exact[-1] else 0
            if err > 0.05:
                self.r.approx("one speed (%d frames a row) is written for the whole song: its tempo is %.1f%% %s" % (
                    c, err, "slow" if c * rows > exact[-1] else "fast"))
            return lens
        worst = 0.0
        for k in range(rows):
            want = int(math.floor(exact[k + 1] + 0.5 + 1e-9)) - pos
            n = max(MIN_ROW, min(MAX_ROW, want))
            lens.append(n)
            pos += n
            worst = max(worst, abs(pos - exact[k + 1]))
        if worst > 0.5 + 1e-6:
            self.r.approx("the tempo needs rows shorter than %d or longer than %d frames: timing is off by up to %.1f frames" % (MIN_ROW, MAX_ROW, worst))
        return lens

    # ------------------------------------------------------------------ instruments
    def label_of(self, inst):
        return self.names.get(tuple(vars(inst).items()))

    def gt_instrument(self, inst):
        """The index (1-based) of the GoatTracker instrument for a chipsynth one."""
        pulse = inst.wave == "pulse"
        arp = tuple(int(a) for a in inst.arp)
        sweep = pulse and inst.pws_rate and inst.pws_from and inst.pws_to and inst.pws_from != inst.pws_to
        vib = (inst.vib_rate, inst.vib_depth) if inst.vib_rate and inst.vib_depth else (0, 0)
        key = (inst.wave if inst.wave in WAVES else "pulse", inst.pw if pulse and not sweep else 0, inst.a & 15, inst.d & 15,
               inst.s & 15, inst.r & 15, arp, inst.arprate if arp else 0,
               vib, (inst.pws_from, inst.pws_to, inst.pws_rate) if sweep else (0, 0, 0))
        if key in self.by_key:
            return self.by_key[key]
        base = _ident(self.label_of(inst) or "inst%d" % (len(self.instruments) + 1))[:15]
        taken = {i.name for i in self.instruments}
        name, n = base, 1
        while name in taken:
            n += 1
            name = "%s%d" % (base[:15 - len(str(n))], n)
        if len(self.instruments) >= MAX_INSTRUMENTS:
            self.r.approx("more than %d different instruments: '%s' is played with instrument 1 (a SID song holds %d)" % (
                MAX_INSTRUMENTS, base, MAX_INSTRUMENTS))
            self.by_key[key] = 1
            return 1
        ins = _Ins(key, name, inst)
        self.instruments.append(ins)
        idx = len(self.instruments)
        self.by_key[key] = idx
        self.build(ins, name, inst, sweep, arp, vib)
        return idx

    def build(self, ins, label, inst, sweep, arp, vib):
        r = self.r
        wave = WAVES.get(inst.wave)
        if wave is None:
            r.approx("instrument '%s': the wave '%s' is played as a pulse" % (label, inst.wave))
            wave = WAVES["pulse"]
        elif inst.wave == "sine":
            r.approx("instrument '%s': the SID has no sine wave: it is played as a triangle" % label)
        if inst.nmode == "short" and inst.wave == "noise":
            r.drop("instrument '%s': the short noise mode (the SID has one noise)" % label)
        # the wavetable: the waveform, and an arpeggio when there is one
        shift = min(0, min(arp)) if arp else 0
        steps = None
        if arp and any(arp):
            fpb = 60.0 / self.t.bpm * self.fps
            steps = max(1, int(round(fpb / max(1, inst.arprate))))
            if abs(steps - fpb / max(1, inst.arprate)) > 0.1 * fpb / max(1, inst.arprate):
                r.approx("instrument '%s': an arpeggio of %d steps a beat is played at %d frames a step (whole frames only)" % (label, inst.arprate, steps))
            program = [(wave, (a - shift) & 0x7F) for a in arp for _ in range(steps)] + [(0xFF, 1)]
            if shift:
                r.approx("instrument '%s': the arpeggio goes below the note (relative notes only go up): the notes are written %d semitones lower and the arpeggio counts from there" % (label, -shift))
        else:
            program = [(wave, 0x80), (0xFF, 0)]
            shift = 0
        wp = self.tables[gt.WTBL].add(program)
        if wp is None and steps:
            r.drop("instrument '%s': its arpeggio (the wavetable is full: %d rows)" % (label, MAX_TABLE))
            shift = 0
            wp = self.tables[gt.WTBL].add([(wave, 0x80), (0xFF, 0)])
        if wp is None:
            r.drop("instrument '%s': the wavetable is full, so the instrument has none and will be silent" % label)
            wp = 0
        ins.shift = shift
        # the pulse table
        pp = 0
        if inst.wave == "pulse":
            pp = self.pulse_table(label, inst, sweep)
        # vibrato
        sp, vd = 0, 0
        if vib != (0, 0):
            sp = self.vibrato_entry(label, *vib)
            vd = 1 if sp else 0
            if sp and steps:
                r.approx("instrument '%s': the arpeggio sets the pitch on every frame, which GoatTracker may play over the vibrato" % label)
        ins.ptr, ins.vibdelay = (wp, pp, 0, sp), vd

    def pulse_table(self, label, inst, sweep):
        r = self.r
        width = lambda pc: max(1, min(4095, int(round(pc * 4096 / 100.0))))
        if not sweep:
            w = width(inst.pw)
            return self.tables[gt.PTBL].add([(0x80 | (w >> 8), w & 0xFF), (0xFF, 0)]) or self._pulse_full(label)
        a, b = width(inst.pws_from), width(inst.pws_to)
        half = max(1, round(self.fps / max(1, inst.pws_rate)))
        want = [h for h in range(1, 400) if round(self.fps / h) == inst.pws_rate]
        if want:
            half = min(want, key=lambda h: abs(h - self.fps / inst.pws_rate))
        step = int(round((b - a) / half))
        if abs(step) > 127:
            step = 127 if step > 0 else -127
            r.approx("instrument '%s': its pulse sweep is faster than the player can ramp (127 a frame): it covers %d%% to %d%% instead of %d%% to %d%%" % (
                label, inst.pws_from, round((a + step * half) * 100 / 4096), inst.pws_from, inst.pws_to))
        if step == 0:
            step = 1 if b > a else -1
        rows = []
        for sign in (1, -1):
            left = half
            while left > 0:
                n = min(127, left)
                rows.append((n, (sign * step) & 0xFF))
                left -= n
        program = [(0x80 | (a >> 8), a & 0xFF)] + rows + [(0xFF, 2)]
        got = self.tables[gt.PTBL].add(program)
        if got is None:
            r.drop("instrument '%s': its pulse sweep (the pulse table is full)" % label)
            return self.tables[gt.PTBL].add([(0x80 | (a >> 8), a & 0xFF), (0xFF, 0)]) or 0
        if round(self.fps / half) != inst.pws_rate:
            r.approx("instrument '%s': the pulse sweep speed %d is played as %d (whole frames only)" % (label, inst.pws_rate, round(self.fps / half)))
        return got

    def _pulse_full(self, label):
        self.r.drop("instrument '%s': its pulse width (the pulse table is full)" % label)
        return 0

    def vibrato_entry(self, label, rate, depth):
        """The 1-based speed table row for a vibrato of `rate` and `depth` (chipsynth's settings), or 0."""
        key = (rate, depth)
        if key not in self.vib_cache:
            best = None
            for cmp_ in range(1, 128):
                for shift in range(0, 13):
                    stub = SimpleNamespace(tables=[None, None, None, ([cmp_ | 0x80], [shift])])
                    got, _ = gt._vibrato(stub, 1, VIB_NOTE, self.timing)
                    if got is None:
                        continue
                    cost = 4 * abs(got[0] - rate) + abs(got[1] - depth)
                    if best is None or cost < best[0]:
                        best = (cost, cmp_, shift, got)
            self.vib_cache[key] = best
        best = self.vib_cache[key]
        if best is None:
            self.r.drop("instrument '%s': its vibrato (rate %d, depth %d cannot be made from a speed table entry)" % (label, rate, depth))
            return 0
        _, cmp_, shift, got = best
        if got != (rate, depth):
            self.r.approx("instrument '%s': vibrato rate %d depth %d is written as rate %d depth %d (the player's vibrato steps are coarser)" % (
                label, rate, depth, got[0], got[1]))
        self.r.approx("instrument '%s': vibrato is a sine in chipsynth and a triangle in the player" % label)
        got_ptr = self.tables[gt.STBL].add([(cmp_ | 0x80, shift)])
        if got_ptr is None:
            self.r.drop("instrument '%s': its vibrato (the speed table is full)" % label)
            return 0
        return got_ptr

    # ------------------------------------------------------------------ one voice, as rows
    def voice_events(self, v, g, rows):
        """{row: ('note', note byte, instrument) | ('off',)} for one voice."""
        r = self.r
        placed = []
        for n in sorted(v, key=lambda n: n.start):
            s = int(round(n.start / g))
            e = min(rows, max(s + 1, int(round((n.start + n.dur) / g))))
            if s >= rows:
                r.drop("a note that starts after the end of the song")
                continue
            if placed:
                ps, pe, pn = placed[-1]
                if s <= ps:
                    r.approx("two notes of one voice land on the same row: the later one is dropped")
                    continue
                if s < pe:
                    r.approx("a note overlaps the next one: it is cut short")
                    placed[-1] = (ps, s, pn)
            placed.append((s, e, n))
        events = {}
        for i, (s, e, n) in enumerate(placed):
            idx = self.gt_instrument(n.inst)
            pitch = n.pitch + self.instruments[idx - 1].shift
            folded = pitch
            while folded < 0:
                folded += 12
            while folded > MAX_NOTE:
                folded -= 12
            if folded != pitch:
                self.moved += 1
            events[s] = ("note", gt.FIRSTNOTE + folded, idx)
            nxt = placed[i + 1][0] if i + 1 < len(placed) else None
            if nxt != e and e < rows:
                events[e] = ("off",)
        return events

    # ------------------------------------------------------------------ the file
    def run(self):
        t, r = self.t, self.r
        self._breaks = self.breaks()
        voices = [list(t.voices[i]) if i < len(t.voices) else [] for i in range(3)]
        extra = [i for i in range(3, len(t.voices)) if t.voices[i]]
        if extra:
            r.drop("voices %s (a SID has three)" % ", ".join("V%d" % (i + 1) for i in extra))
        if any(t.dac[i] for i in range(min(len(t.dac), t.ndacs))):
            r.drop("the DAC tracks (a SID plays no samples)")
        if t.echo or t.reverb:
            r.drop("echo and reverb")
        if t.detune:
            r.drop("detune")
        allnotes = [n for v in voices for n in v]
        if len({n.vol * n.inst.vol for n in allnotes}) > 1:
            r.drop("the different loudness of notes and instruments (the SID has no volume for a voice; every note is as loud as its sustain level)")
        if any(n.pan not in (None, 0) for n in allnotes) or any(n.inst.pan for n in allnotes):
            r.drop("pan (the SID is mono)")
        if any(n.slide or n.inst.slide for n in allnotes):
            r.drop("slides into notes")
        if any(n.inst.filt or n.inst.fs_rate for n in allnotes):
            r.drop("the filter and its sweep")

        g = self.grid(voices)
        total = max(1, int(round(t.length / g)))
        lens = self.row_lengths(g, total)
        once = t.once
        loop_row = 0 if once else min(total - 1, int(round(t.loop_tick / g)))
        if not once and loop_row * g != t.loop_tick:
            r.approx("the loop point is moved to the nearest row")

        events = [self.voice_events(v, g, total) for v in voices]
        if self.moved:
            r.approx("%d note(s) were outside the SID's range (C-0 to G#-7) and moved by octaves" % self.moved)
        if not once:
            # a note that runs to the end of a looping song would carry on into the loop when the loop starts with a rest
            for ev in events:
                if not ev or loop_row in ev or ev[max(ev)][0] != "note":
                    continue
                before = [k for k in ev if k < loop_row]
                if not before or ev[max(before)][0] == "off":
                    ev[loop_row] = ("off",)
        tempo_rows = {}
        for k in range(total):
            if k == 0 or lens[k] != lens[k - 1]:
                tempo_rows[k] = lens[k]
        if not once and loop_row and lens[loop_row] != lens[-1]:
            tempo_rows[loop_row] = lens[loop_row]

        # the patterns
        bar_rows = t.bar_ticks // g if t.bar_ticks % g == 0 else 16
        plen = bar_rows
        while plen > 64:
            plen = (plen + 1) // 2
        plen = max(1, plen)
        chosen = used_rows = None
        for mult in range(1, MAX_PATTERN_ROWS // plen + 1):
            p = plen * mult
            layout = self.layout(events, tempo_rows, total, p, loop_row, once)
            chosen, used_rows = layout, p
            if layout[0] <= MAX_PATTERNS and layout[1] <= MAX_ORDER:
                break
        patterns, orders, restart, cut = chosen[2], chosen[3], chosen[4], chosen[5]
        if chosen[0] > MAX_PATTERNS or chosen[1] > MAX_ORDER:
            patterns, orders, restart, cut = self.fit(events, tempo_rows, total, plen, loop_row, once)
            used_rows = plen * max(1, MAX_PATTERN_ROWS // plen)
        if cut:
            r.drop("the last %d row(s) of the song (a GoatTracker song holds %d patterns and %d order list entries)" % (cut, MAX_PATTERNS, MAX_ORDER))

        if once:
            r.approx("GoatTracker has no 'play once': the song ends in a silent pattern that repeats")
        r.note("written for GoatTracker 2 (GTS5), %s timing (%g Hz), multispeed x%d, %d row%s a pattern (%d ticks a row)" % (
            "PAL" if self.region == "pal" else "NTSC", self.hz, self.mult, used_rows, "" if used_rows == 1 else "s", g))
        r.note("the file does not record PAL or NTSC, nor multispeed: open it with the same settings (the importer's --region and --multispeed)")
        if len(set(lens)) > 1:
            r.note("the tempo changes on %d row(s) (command F on voice 1) to keep the timing exact" % (len(tempo_rows) - 1))
        r.note("%d instrument(s), %d pattern(s)" % (len(self.instruments), len(patterns)))

        out = bytearray(b"GTS5")
        out += _text32(t.title or self.name or "", r, "title")
        out += _text32(t.author, r, "author")
        out += _text32(t.comment, r, "copyright line")
        out.append(1)
        for chan in orders:
            out.append(len(chan) - 1)
            out += bytes(chan)
        out.append(len(self.instruments))
        for ins in self.instruments:
            k = ins.key
            out += bytes((k[2] << 4 | k[3], k[4] << 4 | k[5], ins.ptr[0], ins.ptr[1], ins.ptr[2], ins.ptr[3], ins.vibdelay, GATE_TIMER, FIRST_WAVE))
            out += ins.name.encode("ascii", "replace")[:15].ljust(16, b"\0")
        for tab in self.tables:
            out.append(len(tab.left))
            out += bytes(tab.left) + bytes(tab.right)
        out.append(len(patterns))
        for rows in patterns:
            out.append(len(rows))
            for row in rows:
                out += bytes(row)
        return bytes(out), r

    # ------------------------------------------------------------------ patterns and order lists
    def row_of(self, ev, k, tempo=None):
        """The 4 bytes of row k of a voice's events (tempo = (frames,) to add the tempo command)."""
        what = ev.get(k)
        if what is None:
            note, ins = gt.REST, 0
        elif what[0] == "note":
            note, ins = what[1], what[2]
        else:
            note, ins = gt.KEYOFF, 0
        cmd, data = (gt.CMD_SETTEMPO, tempo) if tempo else (0, 0)
        return (note, ins, cmd, data)

    def segments(self, total, plen, loop_row, once):
        segs = []
        for lo, hi in ((0, loop_row), (loop_row, total)):
            k = lo
            while k < hi:
                segs.append((k, min(hi, k + plen)))
                k = segs[-1][1]
        return segs

    def layout(self, events, tempo_rows, total, plen, loop_row, once):
        segs = self.segments(total, plen, loop_row, once)
        return self.build_layout(events, tempo_rows, segs, loop_row, once)

    def build_layout(self, events, tempo_rows, segs, loop_row, once, cut=0):
        patterns, index = [], {}
        order = [[], [], []]
        for lo, hi in segs:
            for c in range(3):
                rows = [self.row_of(events[c], k, tempo_rows.get(k) if c == 0 else None) for k in range(lo, hi)]
                rows.append((gt.ENDPATT, 0, 0, 0))
                key = tuple(rows)
                if key not in index:
                    index[key] = len(patterns)
                    patterns.append(rows)
                order[c].append(index[key])
        restart = next((i for i, (lo, _) in enumerate(segs) if lo == loop_row), 0)
        if once:
            tail = [(gt.KEYOFF, 0, 0, 0), (gt.ENDPATT, 0, 0, 0)]
            key = tuple(tail)
            if key not in index:
                index[key] = len(patterns)
                patterns.append(tail)
            for c in range(3):
                order[c].append(index[key])
            restart = len(segs)
        lists = [chan + [gt.LOOPSONG, restart] for chan in order]
        return len(patterns), len(segs) + 1, patterns, lists, restart, cut

    def fit(self, events, tempo_rows, total, plen, loop_row, once):
        """The song cut short until it fits the limits: (patterns, order lists, restart, rows cut)."""
        segs = self.segments(total, plen * max(1, MAX_PATTERN_ROWS // plen), loop_row, once)
        while len(segs) > 1:
            segs = segs[:-1]
            got = self.build_layout(events, tempo_rows, segs, loop_row, once)
            if got[0] <= MAX_PATTERNS and got[1] <= MAX_ORDER:
                kept = segs[-1][1]
                return got[2], got[3], got[4], total - kept
        got = self.build_layout(events, tempo_rows, segs, loop_row, once)
        return got[2], got[3], got[4], total - segs[-1][1]
