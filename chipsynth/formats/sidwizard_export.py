# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""chipsynth songs -> SID-Wizard `.swm` files ("SWM1", as SID-Wizard 1.97 reads them).

Written from the file format as SID-Wizard 1.97 defines it (SWM-spec.src, SWMconvert.c and player.asm, read only, WTF
licence; and pysidwizard's reader, Apache-2.0, read only), nothing copied, and checked against the reader in
sidwizard.py (what is written must read back; tests) and against the format author's own converter and the real 6502
player (PLAN-FORMATS.md section 9 says how).

V1-V3 become the three SID voices.  Everything else the song holds is left out and said so in the Report: more voices,
the DACs, echo, reverb, detune, per-note volume and pan, slides, the filter, and the noise mode.

Timing: the song is cut into rows of `g` ticks (the coarsest grid that every note start, end and tempo change lies on, at
most a sixteenth note), a row is at least 3 video frames (the hard restart takes two), and a row's length in frames comes
from the song's tempo, rounded cumulatively so that no row start is more than half a frame out.  When the rows are not all
the same length the tempo is changed on the rows that differ (pattern effect $10 on voice 1, which sets all three
channels), which is exact.  `tempo="speed"` writes one speed instead (the nearest whole number of frames a row) and
reports the error.  The file records the frame speed ("multispeed", header byte 4) but not PAL or NTSC.  The frame speed
is not GoatTracker's multispeed: the player's extra calls only play the instrument tables (arpeggios, and pulse sweeps
for instruments that ask), so rows and vibrato stay at one step a video frame and only arpeggios and sweeps get finer.

Instruments: ADSR, waveform and pulse width go in as they are.  An arpeggio becomes a looping wave table (with the
instrument's arpeggio speed), a pulse-width sweep a pulse table that ramps up and down, and vibrato the instrument's
vibrato byte, fitted to what the player's calculated vibrato makes of it.  What does not fit is approximated or dropped,
and listed.
"""
import math
import re
import statistics
from dataclasses import dataclass
from functools import reduce

from ..synth import Instrument
from .report import Report

PAL_HZ = 50.0
NTSC_HZ = 60.0
LOAD_ADDRESS = 0x3333            # the load address SID-Wizard's own converters write
HEADER = 64
MIN_ROW = 3                      # video frames: the hard restart takes two, the note starts on the third
MAX_ROW = 127                    # the largest tempo a tempo command can set
MAX_PATTERNS = 100               # the editor's limit (maxptnamount in SWM-spec.src)
MAX_INSTRUMENTS = 36             # instrument numbers the player accepts are 1-36
MAX_ORDER = 124                  # patterns in an orderlist (a sequence holds 126 bytes: these, the loop byte and its position)
MAX_PATTERN_BYTES = 248          # the unpacked bytes of a pattern (the editor's maxptnlen is 249 with its end mark)
MAX_PATTERN_ROWS = 128
MAX_INSTRUMENT_BYTES = 127       # an instrument without its name
MAX_NOTE = 94                    # note byte 1-95: C-0 to B-7
GATEOFF = 0x7E
TEMPO_FX = 0x10                  # pattern effect: set the single tempo of all three channels
PACKED_MIN, PACKED_MAX = 0x70, 0x77
SEQ_LOOP, SEQ_END = 0xFF, 0xFE
WAVES = {"pulse": 0x41, "saw": 0x21, "tri": 0x11, "noise": 0x81, "sine": 0x11}
FIRST_FRAME = {0x41: 0x09, 0x21: 0x09, 0x11: 0x09, 0x81: 0x01}    # byte 15: the one waveform of the note's first call (test bit, or gate alone for noise)
CONTROL = 0x1A                   # instrument byte 0: hard restart, first-call waveform, normal vibrato
HR_AD, HR_SR = 0x0F, 0xF0        # instrument bytes 1-2: the hard restart's ADSR (the editor's default)
PAL_CLOCK = 985248.0


class SidWizardExportError(ValueError):
    pass


def dump(tune, region="pal", multispeed=1, name=None, tempo="exact"):
    """A compiled Tune -> (`.swm` bytes, Report).  `region` is 'pal' or 'ntsc' (the file does not record it, but the frame
    rate decides how many video frames a row lasts); `multispeed` 1, 2 or 4 (the file's frame speed); `tempo` 'exact' or
    'speed'."""
    if multispeed not in (1, 2, 4):
        raise SidWizardExportError("multispeed must be 1, 2 or 4")
    if region not in ("pal", "ntsc"):
        raise SidWizardExportError("region must be pal or ntsc")
    if tempo not in ("exact", "speed"):
        raise SidWizardExportError("tempo must be exact or speed")
    return _Writer(tune, region, multispeed, name, tempo).run()


# ------------------------------------------------------------------ the player's frequency table, for the vibrato fit
def _freq_table():
    """The 96 values of the player's table: index i is C-0 + (i - 1) semitones (index 0 is B-1), at 440 Hz, for the PAL clock.
    Rounded up, which is within one register unit of what the player writes (measured; the source's own table is one lower)."""
    return [int(math.ceil(440.0 * 2 ** ((i - 1 - 57) / 12.0) * (1 << 24) / PAL_CLOCK)) for i in range(96)]


FREQ = _freq_table()


def vibrato_swing(note, half, amp):
    """What the player's calculated vibrato does to a note: (peak deviation in register units, calls in a cycle) for a
    vibrato byte whose low nibble is `half` (the half period in calls) and high nibble `amp`, on note byte `note` (1 = C-0).
    The size of a step comes from the player's exponent table (the high bytes of the frequency table, shifted by 11
    notes), looked up at note + 4 * amp; the sign changes every `half` calls, from a quarter of a cycle in."""
    if not amp or not half:
        return 0.0, 2 * max(1, half)
    y = note + 4 * amp
    step = 0 if y < 11 else (FREQ[y - 11] >> 8) if y < 107 else FREQ[y - 107]
    pos, cnt, lo, hi = 0, half >> 1, 0, 0
    for _ in range(2 * half):
        cnt = (cnt or 2 * half) - 1
        pos += step if cnt < half else -step
        lo, hi = min(lo, pos), max(hi, pos)
    return (hi - lo) / 2.0, 2 * half


def _ident(s):
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def _text(s, n, report, what):
    """`s` as n bytes of capitals (the editor's text is capitals), padded with spaces."""
    s = " ".join((s or "").split())
    if s != s.upper() and any(c.isalpha() for c in s):
        report.approx("the %s is written in capitals" % what)
    raw = s.upper().encode("latin-1", "replace")
    raw = bytes(b if 32 <= b < 127 else 63 for b in raw)
    if len(raw) > n:
        report.approx("the %s is cut to %d characters" % (what, n))
    return raw[:n].ljust(n, b" ")


def pack(stream):
    """Pattern bytes -> packed: after a $00, a run of 2-9 more $00 bytes is one byte $70-$77 (two to nine rests)."""
    out, i, n = bytearray(), 0, len(stream)
    while i < n:
        if stream[i]:
            out.append(stream[i])
            i += 1
            continue
        j = i
        while j < n and stream[j] == 0:
            j += 1
        out.append(0)
        rest = j - i - 1
        while rest >= 2:
            chunk = min(rest, 9)
            out.append(PACKED_MIN + chunk - 2)
            rest -= chunk
        if rest == 1:
            out.append(0)
        i = j
    return bytes(out)


@dataclass
class _Ins:
    key: tuple
    name: str
    body: bytes              # the instrument without its name: 16 header bytes and the wave and pulse tables (each with its $FF); the filter table is empty


class _Writer:
    def __init__(self, tune, region, multispeed, name, tempo):
        self.t, self.region, self.mult, self.name, self.tempo_mode = tune, region, multispeed, name, tempo
        self.hz = PAL_HZ if region == "pal" else NTSC_HZ
        # SID-Wizard's "frame speed" runs only the instrument tables (waveform / arpeggio, and the pulse table when the
        # instrument asks) several times a video frame: the sequencer (rows, tempo, hard restart) and the vibrato run once
        # a frame.  So rows and vibrato are timed in video frames (`fps`), the tables in `tfps`.
        self.fps = self.hz
        self.tfps = self.hz * multispeed
        self.r = Report()
        self.names = {}
        for n, i in tune.instruments.items():
            self.names.setdefault(tuple(vars(i).items()), n)
        self.instruments = []
        self.by_key = {}
        self.ref_pitch = {}
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
            r.approx("the song's finest timing is %d ticks but a row must last %d video frames at this tempo: notes are moved to the "
                     "nearest row of %d ticks (1/%g of a quarter note)" % (g, MIN_ROW, g2, 96 / g2))
            g = g2
        return g

    def row_lengths(self, g, rows):
        """Video frames each row lasts, so that row k starts at the whole frame nearest its exact time."""
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
            self.r.approx("the tempo needs rows shorter than %d or longer than %d video frames: timing is off by up to %.1f frames" % (MIN_ROW, MAX_ROW, worst))
        return lens

    # ------------------------------------------------------------------ instruments
    def label_of(self, inst):
        return self.names.get(tuple(vars(inst).items()))

    def key_of(self, inst):
        pulse = inst.wave == "pulse"
        arp = tuple(int(a) for a in inst.arp) if any(inst.arp) else ()
        sweep = pulse and inst.pws_rate and inst.pws_from and inst.pws_to and inst.pws_from != inst.pws_to
        vib = (inst.vib_rate, inst.vib_depth) if inst.vib_rate and inst.vib_depth else (0, 0)
        return (inst.wave if inst.wave in WAVES else "pulse", inst.pw if pulse and not sweep else 0, inst.a & 15, inst.d & 15,
                inst.s & 15, inst.r & 15, arp, inst.arprate if arp else 0, vib,
                (inst.pws_from, inst.pws_to, inst.pws_rate) if sweep else (0, 0, 0))

    def instrument(self, inst):
        """The number (1-based) of the SID-Wizard instrument for a chipsynth one."""
        key = self.key_of(inst)
        if key in self.by_key:
            return self.by_key[key]
        base = _ident(self.label_of(inst) or "") or "INST%d" % (len(self.instruments) + 1)
        base = base[:8]
        if len(self.instruments) >= MAX_INSTRUMENTS:
            self.r.approx("more than %d different instruments: '%s' is played with instrument 1 (the player takes numbers 1-%d)" % (
                MAX_INSTRUMENTS, base, MAX_INSTRUMENTS))
            self.by_key[key] = 1
            return 1
        taken = {i.name for i in self.instruments}
        name, n = base, 1
        while name in taken:
            n += 1
            name = "%s%d" % (base[:8 - len(str(n))], n)
        label = self.label_of(inst) or name.lower()
        ins = _Ins(key, name, self.build(label, inst, key))
        self.instruments.append(ins)
        self.by_key[key] = len(self.instruments)
        return len(self.instruments)

    def build(self, label, inst, key):
        r = self.r
        wave = WAVES.get(inst.wave)
        if wave is None:
            r.approx("instrument '%s': the wave '%s' is played as a pulse" % (label, inst.wave))
            wave = WAVES["pulse"]
        elif inst.wave == "sine":
            r.approx("instrument '%s': the SID has no sine wave: it is played as a triangle" % label)
        if inst.nmode == "short" and inst.wave == "noise":
            r.drop("instrument '%s': the short noise mode (the SID has one noise)" % label)
        arp, vib = key[6], key[8]
        # the wave table: one row (waveform, pitch, detune) a note of an arpeggio, which loops; the arpeggio speed is in the header
        speed = 0
        if arp:
            fpb = 60.0 / self.t.bpm * self.tfps
            ideal = fpb / max(1, inst.arprate)
            steps = max(1, min(64, int(round(ideal))))
            if abs(steps - ideal) > 0.1 * ideal:
                r.approx("instrument '%s': an arpeggio of %d steps a beat is played at %d table calls a step (whole calls only)" % (label, inst.arprate, steps))
            if len(self.t.tempo_events):
                r.note("instrument '%s': arpeggio steps are timed from the song's first tempo" % label)
            speed = steps - 1
            rows = [bytes((wave, a & 0xFF, 0)) for a in arp]
            rows.append(bytes((0xFE, 16, 0)))            # jump back to the first row
            if len(arp) > 8:
                r.approx("instrument '%s': an arpeggio of %d notes" % (label, len(arp)))
        else:
            rows = [bytes((wave, 0, 0))]
        wtab = b"".join(rows) + b"\xff"
        pw_ptr = 16 + len(wtab)
        ptab = self.pulse_table(label, inst, key, pw_ptr) if inst.wave == "pulse" else b"\xff"
        if self.mult > 1 and key[9] != (0, 0, 0):
            speed |= 0x40                                # bit 6 of the arpeggio speed: the pulse table runs at the frame speed too
        vbyte = 0
        if vib != (0, 0):
            vbyte = self.vibrato_byte(label, key, vib)
            if arp and vbyte:
                r.approx("instrument '%s': the arpeggio sets the pitch at every step, which also restarts the vibrato" % label)
        # the filter table is empty: its $FF is the size byte the file puts in its place, so its pointer is the body's length
        head = bytes((CONTROL, HR_AD, HR_SR, (key[2] << 4) | key[3], (key[4] << 4) | key[5], vbyte, 0, speed,
                      1, 0, pw_ptr, pw_ptr + len(ptab), 0, 0, 0, FIRST_FRAME[wave]))
        body = head + wtab + ptab
        if len(body) > MAX_INSTRUMENT_BYTES:         # cannot happen with the programs above (at most 8 arpeggio rows and 4 pulse rows)
            raise SidWizardExportError("instrument '%s' does not fit in %d bytes" % (label, MAX_INSTRUMENT_BYTES))
        return body

    def pulse_table(self, label, inst, key, base):
        """The pulse table of a pulse instrument; `base` is where it starts in the instrument (its jump needs an address)."""
        r = self.r
        width = lambda pc: max(1, min(4095, int(round(pc * 4096 / 100.0))))
        setrow = lambda w: bytes((0x80 | (w >> 8), w & 0xFF, 0))
        if key[9] == (0, 0, 0):
            return setrow(width(inst.pw)) + b"\xff"
        a, b = width(inst.pws_from), width(inst.pws_to)
        want = 2.0 * self.tfps / inst.pws_rate                    # table calls in a cycle (up and down)
        # an up sweep and a down sweep of c calls each; each sweep row also takes an advance call and the jump one: 2c + 3 calls a cycle
        c = max(1, min(127, int(round((want - 3) / 2.0))))
        step = int(round((b - a) / float(c)))
        if abs(step) > 127:
            step = 127 if step > 0 else -127
            r.approx("instrument '%s': its pulse sweep is faster than the player can ramp (127 a call): it covers %d%% to %d%% instead of %d%% to %d%%" % (
                label, inst.pws_from, round((a + step * c) * 100 / 4096), inst.pws_from, inst.pws_to))
        if step == 0:
            step = 1 if b > a else -1
        got = 2.0 * self.tfps / (2 * c + 3)
        if round(got) != inst.pws_rate:
            r.approx("instrument '%s': the pulse sweep speed %d is played as %.1f (whole calls only)" % (label, inst.pws_rate, got))
        rows = [setrow(a), bytes((c, step & 0xFF, 0)), bytes((c, (-step) & 0xFF, 0)), bytes((0xFE, base + 3, 0))]
        return b"".join(rows) + b"\xff"

    def vibrato_byte(self, label, key, vib):
        """The instrument's vibrato byte (amplitude high nibble, half period low nibble) nearest chipsynth's (rate, depth)."""
        rate, depth = vib
        note = self.ref_pitch.get(key, 48) + 1
        best = None
        for half in range(1, 16):
            hz = self.fps / (2.0 * half)
            for amp in range(1, 16):
                peak, _ = vibrato_swing(note, half, amp)
                semis = 12 * math.log2(1 + peak / FREQ[min(95, note)])
                got = (hz / 0.8, semis * 16)
                cost = 4 * abs(got[0] - rate) + abs(got[1] - depth)
                if best is None or cost < best[0]:
                    best = (cost, half, amp, got)
        _, half, amp, got = best
        if (int(round(got[0])), int(round(got[1]))) != (rate, depth):
            self.r.approx("instrument '%s': vibrato rate %d depth %d is written as rate %d depth %d (the player's vibrato has coarser steps)" % (
                label, rate, depth, int(round(got[0])), int(round(got[1]))))
        self.r.approx("instrument '%s': vibrato is a sine in chipsynth and a triangle in the player" % label)
        return (amp << 4) | half

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
            idx = self.instrument(n.inst)
            arp = self.instruments[idx - 1].key[6] if idx <= len(self.instruments) else ()
            lo = max(0, -min(arp + (0,)))
            hi = MAX_NOTE - max(arp + (0,))
            pitch = n.pitch
            folded = pitch
            while folded < lo and folded + 12 <= hi:
                folded += 12
            while folded > hi and folded - 12 >= lo:
                folded -= 12
            folded = max(lo, min(hi, folded))
            if folded != pitch:
                self.moved += 1
            events[s] = ("note", folded + 1, idx)
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
        by = {}
        for n in allnotes:
            by.setdefault(self.key_of(n.inst), []).append(n.pitch)
        self.ref_pitch = {k: int(statistics.median(p)) for k, p in by.items()}

        g = self.grid(voices)
        total = max(1, int(round(t.length / g)))
        lens = self.row_lengths(g, total)
        once = t.once
        loop_row = 0 if once else min(total - 1, int(round(t.loop_tick / g)))
        if not once and loop_row * g != t.loop_tick:
            r.approx("the loop point is moved to the nearest row")

        events = [self.voice_events(v, g, total) for v in voices]
        if self.moved:
            r.approx("%d note(s) were outside the SID's range (C-0 to B-7) or the arpeggio's, and moved by octaves" % self.moved)
        if not self.instruments:
            self.instrument(Instrument())
        if not once:
            # a note that runs to the end of a looping song would carry on into the loop when the loop starts with a rest
            for ev in events:
                if not ev or loop_row in ev or ev[max(ev)][0] != "note":
                    continue
                before = [k for k in ev if k < loop_row]
                if not before or ev[max(before)][0] == "off":
                    ev[loop_row] = ("off",)
        tempo_rows = {}
        for k in range(1, total):
            if lens[k] != lens[k - 1]:
                tempo_rows[k] = lens[k]
        if not once and lens[loop_row] != lens[-1] and loop_row not in tempo_rows:
            tempo_rows[loop_row] = lens[loop_row]

        # the patterns
        bar_rows = t.bar_ticks // g if t.bar_ticks % g == 0 else 16
        plen = bar_rows
        while plen > 64:
            plen = (plen + 1) // 2
        plen = max(1, plen)
        while plen > 1 and self.layout(events, tempo_rows, total, plen, loop_row, once)[6] > MAX_PATTERN_BYTES:
            plen = (plen + 1) // 2
        chosen, used_rows = None, plen
        for mult in range(1, MAX_PATTERN_ROWS // plen + 1):
            p = plen * mult
            layout = self.layout(events, tempo_rows, total, p, loop_row, once)
            if layout[6] > MAX_PATTERN_BYTES:
                break
            chosen, used_rows = layout, p
            if layout[0] <= MAX_PATTERNS and layout[1] <= MAX_ORDER:
                break
        if chosen is None:
            chosen = self.layout(events, tempo_rows, total, plen, loop_row, once)
        patterns, orders, restart, cut = chosen[2], chosen[3], chosen[4], chosen[5]
        if chosen[0] > MAX_PATTERNS or chosen[1] > MAX_ORDER:
            patterns, orders, restart, cut = self.fit(events, tempo_rows, total, used_rows, loop_row, once)
        if cut:
            r.drop("the last %d row(s) of the song (a SID-Wizard song holds %d patterns and %d orderlist entries)" % (cut, MAX_PATTERNS, MAX_ORDER))

        if once:
            r.note("the song plays once: its orderlists end (a last silent pattern lets the final notes release)")
        r.note("written for SID-Wizard (SWM1), %s timing (%g Hz), frame speed x%d, %d row%s a pattern (%d ticks a row)" % (
            "PAL" if self.region == "pal" else "NTSC", self.hz, self.mult, used_rows, "" if used_rows == 1 else "s", g))
        r.note("the file does not record PAL or NTSC: open it with the same region (the importer's --region); it does record the frame speed")
        if self.mult > 1:
            r.note("frame speed x%d: instrument tables (arpeggios, pulse sweeps) run %d times a frame; rows, tempo and vibrato run once" % (self.mult, self.mult))
        r.note("SID-Wizard starts a note two video frames into its row (its hard restart), so every note sounds that much later than its row")
        if len(set(lens)) > 1:
            r.note("the tempo changes on %d row(s) (pattern effect $10 on voice 1) to keep the timing exact" % len(tempo_rows))
        r.note("%d instrument(s), %d pattern(s)" % (len(self.instruments), len(patterns)))

        title = (t.title or self.name or "").strip()
        author = (t.author or "").strip()
        info = "%s:%s" % (author, title) if author and title else author or title
        head = bytearray(HEADER)
        head[0:4] = b"SWM1"
        head[4] = self.mult
        head[5], head[6], head[7] = 4, 1, 1
        head[8:11] = b"\xff\xff\xff"
        head[11] = 32
        head[12], head[13], head[14] = 3, len(patterns), len(self.instruments)
        head[24:64] = _text(info, 40, r, "title and author line")
        out = bytearray(LOAD_ADDRESS.to_bytes(2, "little"))
        out += head
        for chan in orders:
            out += bytes(chan) + bytes((len(chan),))
        for rows in patterns:
            stream = b"".join(rows)
            out += pack(stream) + bytes((len(stream) + 1, len(rows)))
        for ins in self.instruments:
            out += ins.body + bytes((len(ins.body),)) + ins.name.encode("ascii").ljust(8, b" ")
        out += bytes((0x80 | lens[0], 0x84))
        return bytes(out), r

    # ------------------------------------------------------------------ patterns and orderlists
    def row_bytes(self, ev, k, tempo=None):
        what = ev.get(k)
        note, inst = 0, 0
        if what is not None:
            if what[0] == "note":
                note, inst = what[1], what[2]
            else:
                note = GATEOFF
        if tempo:
            return bytes((note | 0x80, inst | 0x80, TEMPO_FX, tempo))
        if inst:
            return bytes((note | 0x80, inst))
        return bytes((note,))

    def segments(self, total, plen, loop_row, once):
        segs = []
        for lo, hi in ((0, loop_row), (loop_row, total)):
            k = lo
            while k < hi:
                segs.append((k, min(hi, k + plen)))
                k = segs[-1][1]
        return segs

    def layout(self, events, tempo_rows, total, plen, loop_row, once):
        return self.build_layout(events, tempo_rows, self.segments(total, plen, loop_row, once), loop_row, once)

    def build_layout(self, events, tempo_rows, segs, loop_row, once, cut=0):
        """(pattern count, orderlist entries, patterns, orderlists, restart position, rows cut, the biggest pattern's bytes).
        A pattern is the list of its rows' bytes; the orderlists hold its 1-based number."""
        patterns, index = [], {}
        order = [[], [], []]
        biggest = 0
        for lo, hi in segs:
            for c in range(3):
                rows = tuple(self.row_bytes(events[c], k, tempo_rows.get(k) if c == 0 else None) for k in range(lo, hi))
                biggest = max(biggest, sum(map(len, rows)))
                if rows not in index:
                    index[rows] = len(patterns) + 1
                    patterns.append(list(rows))
                order[c].append(index[rows])
        restart = next((i for i, (lo, _) in enumerate(segs) if lo == loop_row), 0)
        if once:
            tail = (bytes((GATEOFF,)), bytes((0,)))
            if tail not in index:
                index[tail] = len(patterns) + 1
                patterns.append(list(tail))
            for c in range(3):
                order[c].append(index[tail])
            lists = [chan + [SEQ_END] for chan in order]
        else:
            lists = [chan + [SEQ_LOOP, restart] for chan in order]
        return len(patterns), len(segs) + 1, patterns, lists, restart, cut, biggest

    def fit(self, events, tempo_rows, total, plen, loop_row, once):
        """The song cut short until it fits the limits: (patterns, orderlists, restart, rows cut)."""
        segs = self.segments(total, plen, loop_row, once)
        while len(segs) > 1:
            segs = segs[:-1]
            got = self.build_layout(events, tempo_rows, segs, loop_row, once)
            if got[0] <= MAX_PATTERNS and got[1] <= MAX_ORDER:
                return got[2], got[3], got[4], total - segs[-1][1]
        got = self.build_layout(events, tempo_rows, segs, loop_row, once)
        return got[2], got[3], got[4], total - segs[-1][1]
