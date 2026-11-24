# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""chipsynth songs -> FamiTracker text (`# FamiTracker text export 0.4.2`), the file FamiTracker opens with File > Import
Text and FamiStudio with File > Import > FamiTracker Text.

Why this exists next to the FamiStudio writer: FamiStudio's own text format loads only in the release that wrote it,
and FamiTracker's text format has stayed the same since 0.4.2, so it loads in both programs and in any FamiStudio.

V1, V2 -> Pulse 1, 2; V3 -> Triangle; V4 -> Noise; D1 -> DPCM.  Written from the format (the lines the two programs
read and write), no code copied.  One song to a file.

    MACRO type idx loop release setting : values       type 0 volume, 1 arpeggio, 2 pitch, 4 duty
    DPCMDEF idx size "name" / DPCM : hex bytes         a sample
    INST2A03 idx vol arp pitch hipitch duty "name"     an instrument = which macro of each type it uses (-1 none)
    KEYDPCM inst octave note sample pitch loop 0 -1    which key plays which sample
    TRACK rows speed tempo "name" / COLUMNS : n n n n n / ORDER xx : pp pp pp pp pp / PATTERN pp / ROW rr : cell : cell ...

A cell is `note instrument volume effect...`: `C-4 00 F B00`, `...` for nothing, `---` for a note cut, `F-#` for a noise
pitch.  What a song holds that this does not (more voices, more DACs, echo, reverb, filter, vibrato, slide, pan,
tempo changes) is dropped and said so in the Report, as the FamiStudio writer does.

Timing: a row is a whole number of ticks; FamiTracker plays `speed` ticks to a row and a tick lasts 150 / tempo frames on
NTSC (125 / tempo on PAL), so (speed, tempo) is chosen to land the row length within a fraction of a percent, and the
player's tempo counter spreads the odd frames over the rows.  The loop is a `Bxx` on the last row of the last pattern
(a song marked once gets `C00`, which halts).
"""
from .envelope import FRAME_HZ, arp_sequence, pws_sequence, release_frames, volume_envelope
from .famistudio_text import DUTY_PW, SQUARE_SHIFT, PAL_HZ, noise_pitch
from .report import Report

HEADER = "# FamiTracker text export 0.4.2"
PPQ = 96
GRIDS = (24, 12, 8, 6, 4, 3, 2, 1)                  # ticks a row can last
SEMITONES = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")
MAX_PATTERNS = 128                                   # FamiTracker: 128 frames in the order list, 128 patterns a channel
MAX_ROWS = 256                                       # rows in a pattern
MAX_MACRO = 252                                      # values in a sequence
MAX_MACROS = 128                                     # sequences of one type
MAX_INSTRUMENTS = 64
SEQ_VOLUME, SEQ_ARPEGGIO, SEQ_PITCH, SEQ_DUTY = 0, 1, 2, 4
CHANNELS = ("Pulse 1", "Pulse 2", "Triangle", "Noise", "DPCM")
DPCM_NOTE0 = 13                                      # C-1: the key of the first sample
SAMPLE_KEYS = 36
SQUARE_LOW = 21                                      # the lowest pitch (A1, 55 Hz) an 11-bit period reaches
SAMPLE_BYTES = 4081                                  # the most the NES driver plays from one sample
EMPTY = ("...", "..", ".")


def note_text(value):
    """FamiTracker's name for note `value` (1 = C-0 ... 96 = B-7)."""
    return "%s%d" % (SEMITONES[(value - 1) % 12], (value - 1) // 12)


def _text(s, limit=None):
    """A string for inside quotes: no quotes, no line breaks."""
    s = " ".join(str(s).replace('"', "'").split())
    return s[:limit] if limit else s


def dump(tune, region="ntsc", name=None):
    """A compiled Tune -> (FamiTracker text, Report)."""
    return _Writer(tune, region, name).run()


def speed_and_tempo(row_frames, pal):
    """(speed, tempo, frames a row really lasts on average) closest to `row_frames` frames a row.  A row takes `speed` ticks
    and a tick 150 / tempo frames (125 / tempo on PAL); tempo is 32-255 and a row cannot be shorter than a frame."""
    base = 125.0 if pal else 150.0
    found = []
    for speed in range(1, 32):
        lo, hi = 32, int(min(255, base * speed))
        if hi < lo:
            continue
        tempo = max(lo, min(hi, round(base * speed / row_frames)))
        real = base * speed / tempo
        found.append((abs(real - row_frames) / row_frames, speed, tempo, real))
    if not found:
        return 1, 150, 1.0
    best = min(f[0] for f in found)
    # of those as good as the best to a tenth of a percent, the tempo nearest the default (150 / 125, so a whole number
    # of frames a row is speed = frames, the numbers a person would pick), then the smaller speed
    err, speed, tempo, real = min((f for f in found if f[0] <= best + 0.001), key=lambda f: (abs(f[2] - base), f[1]))
    return speed, tempo, real


class _Writer:
    def __init__(self, tune, region, name):
        self.t, self.name = tune, name
        self.pal = region == "pal"
        self.hz = PAL_HZ if self.pal else FRAME_HZ
        self.r = Report()
        self.names = {}                              # id(Instrument) -> chipsynth name
        for n, i in tune.instruments.items():
            self.names.setdefault(id(i), n)
        self.macros = {SEQ_VOLUME: [], SEQ_ARPEGGIO: [], SEQ_PITCH: [], SEQ_DUTY: []}     # type -> [(values, loop)]
        self.insts = []                              # [(name, vol, arp, pitch, duty)] the macro index of each (-1: none)
        self.inst_of = {}                            # (id(Instrument), kind) -> index in self.insts
        self.by_content = {}                         # (kind, vol, arp, duty) -> index: instruments that sound the same share
        self.said = set()
        self.row_frames = 6.0
        self.vol_cache = {}                          # id(Instrument) -> volume_envelope result
        self.low = 0                                 # pulse notes below what a period can reach
        self.cut_release = []                        # instruments whose release tail is not played

    # ---- timing
    def grid(self):
        t = self.t
        ticks = [t.bar_ticks, t.loop_tick, t.length]
        for v in t.voices[:4]:
            for n in v:
                ticks += [n.start, n.dur]
        for ev in (t.dac[0] if t.dac else []):
            ticks.append(ev[0])
        for g in GRIDS:
            if all(x % g == 0 for x in ticks):
                return g
        self.r.approx("the song's timing is finer than 1/16 of a quarter note by less than a row: notes are moved to the nearest row of 1/16 (6 ticks)")
        return 6

    def tempo(self, g):
        t = self.t
        if len({b for _, b in t.tempo_events} | {t.bpm}) > 1:
            self.r.drop("tempo changes (a Fxx tempo effect is not read by FamiStudio): the first tempo is used all through")
        bpm = t.bpm_at(0) if hasattr(t, "bpm_at") else t.bpm
        row_frames = 60.0 / (bpm * PPQ) * g * self.hz
        if row_frames < 1.0:
            self.r.approx("the tempo is faster than one frame a row: it is played at one frame a row")
        speed, tempo, real = speed_and_tempo(max(1.0, row_frames), self.pal)
        self.row_frames = real                       # frames a row really lasts
        err = abs(real - max(1.0, row_frames)) / max(1.0, row_frames)
        if err > 0.005:
            self.r.approx("the tempo is %.1f%% off after rounding to FamiTracker's speed and tempo" % (err * 100))
        return speed, tempo

    def pattern_length(self, bar_rows, loop_row, total_rows):
        """Rows to a pattern: a whole number of bars, 16-64 when it can be, with the loop point on a pattern's first row."""
        base = bar_rows if bar_rows <= MAX_ROWS else 64
        multiples = [base * k for k in range(1, MAX_ROWS // base + 1)]
        short = [p for p in multiples if 16 <= p <= 64]
        order = short + [p for p in multiples if p not in short and p >= 16] + [p for p in multiples if p < 16]
        fits = [p for p in order if -(-total_rows // p) <= MAX_PATTERNS]
        for p in fits:
            if loop_row % p == 0:
                return p
        return fits[0] if fits else order[-1] if order else MAX_ROWS

    # ---- macros and instruments
    def macro(self, kind, values, loop, release=-1):
        """Index of the macro with these values, adding it; -1 when there is no room."""
        values = tuple(int(v) for v in values)[:MAX_MACRO]
        key = (values, min(loop, len(values) - 1), release if release < len(values) else -1)
        lst = self.macros[kind]
        if key in lst:
            return lst.index(key)
        if len(lst) >= MAX_MACROS:
            self.say("more than %d sequences of one kind: the later ones are left out" % MAX_MACROS, drop=True)
            return -1
        lst.append(key)
        return len(lst) - 1

    def say(self, msg, drop=False):
        if msg not in self.said:
            self.said.add(msg)
            (self.r.drop if drop else self.r.approx)(msg)

    def volume_values(self, inst):
        """The volume macro (values, loop, release) sampling the ADSR once a frame, or None for a flat full volume.  The
        release part is played after a `===` cell (see envelope.volume_envelope)."""
        key = id(inst)
        if key not in self.vol_cache:
            self.vol_cache[key] = volume_envelope(inst, self.hz, MAX_MACRO)
        return self.vol_cache[key]

    def duty_values(self, inst, label):
        """The duty macro (values, loop) of a pulse instrument: its width, or its width sweep sampled once a frame."""
        snap = lambda pw: min(range(4), key=lambda i: abs(DUTY_PW[i] - pw))
        vals = pws_sequence(inst, self.hz, MAX_MACRO, snap)
        if vals:
            self.say("instrument '%s': the pulse-width sweep is sampled once a frame and played with the NES's four widths (12, 25, 50, 75)" % label)
            return vals, 0
        duty = snap(inst.pw)
        if inst.wave == "pulse" and abs(DUTY_PW[duty] - inst.pw) > 3:
            self.say("instrument '%s': pulse width %d%% is played as %d%% (the NES has 12, 25, 50 and 75)" % (label, inst.pw, DUTY_PW[duty]))
        return [duty], -1

    def arp_values(self, inst, label):
        step = 60.0 / max(1, self.t.bpm) * self.hz / max(1, inst.arprate)             # frames to a step
        vals, scaled = arp_sequence(inst.arp, step, MAX_MACRO)
        if scaled:
            self.say("instrument '%s': the arpeggio is longer than FamiTracker's %d-value sequences: it is sped up to fit" % (label, MAX_MACRO))
        elif step < 1.0:
            self.say("instrument '%s': the arpeggio steps faster than once a frame: it is played at one step a frame" % label)
        return vals, 0

    def inst_index(self, inst, kind):
        key = (id(inst), kind)
        if key in self.inst_of:
            return self.inst_of[key]
        label = self.names.get(id(inst)) or "Inst%d" % (len(self.insts) + 1)
        vol = arp = duty = -1
        if kind != "tri":
            v = self.volume_values(inst)
            if v:
                vol = self.macro(SEQ_VOLUME, *v)
        elif inst.a or inst.s < 15 or inst.vol < 15:
            self.say("instrument '%s': the triangle has no volume, so its volume envelope is not played" % label)
        if kind == "noise":
            duty = self.macro(SEQ_DUTY, [1 if inst.nmode == "short" else 0], -1)
        elif kind == "sq":
            duty = self.macro(SEQ_DUTY, *self.duty_values(inst, label))
        if inst.arp:
            if kind == "noise":
                self.say("instrument '%s': the arpeggio is not played on the noise channel" % label, drop=True)
            else:
                arp = self.macro(SEQ_ARPEGGIO, *self.arp_values(inst, label))
        dropped = [w for w, on in (("vibrato", inst.vib_rate and inst.vib_depth), ("slide", inst.slide),
                                   ("pulse-width sweep", inst.pws_rate and kind != "sq"), ("filter", inst.filt), ("pan", inst.pan)) if on]
        if dropped:
            self.r.drop("instrument '%s': %s" % (label, ", ".join(dropped)))
        if inst.r and inst.s and kind != "tri" and label not in self.cut_release:
            self.cut_release.append(label)
        content = (kind, vol, arp, duty)
        if content in self.by_content:
            self.inst_of[key] = self.by_content[content]
            return self.inst_of[key]
        if len(self.insts) >= MAX_INSTRUMENTS - 1:                    # one is kept for the DPCM keys
            self.r.drop("instrument '%s': FamiTracker has %d instruments: its notes use instrument 00" % (label, MAX_INSTRUMENTS))
            self.inst_of[key] = 0
            return 0
        taken = {i[0] for i in self.insts}
        nm, n = _text(label, 60) or "Inst", 1
        base = nm
        while nm in taken:
            n += 1
            nm = "%s_%d" % (base, n)
        self.insts.append((nm, vol, arp, -1, duty))
        self.by_content[content] = self.inst_of[key] = len(self.insts) - 1
        return self.inst_of[key]

    # ---- notes
    def place(self, items, g, total_rows):
        """[(start tick, dur ticks, payload)] -> [(first row, end row, payload)], no overlap and nothing past the end."""
        out = []
        lost = 0
        for start, dur, p in sorted(items, key=lambda x: x[0]):
            s = int(round(start / g))
            e = s + max(1, int(round(dur / g)))
            if s >= total_rows:
                continue
            if out and s < out[-1][1]:
                if s <= out[-1][0]:
                    lost += 1
                    continue
                out[-1] = (out[-1][0], s, out[-1][2])
            out.append((s, min(e, total_rows), p))
        if lost:
            self.r.approx("%d note%s began on the same row as another one and %s left out" % (lost, "" if lost == 1 else "s", "was" if lost == 1 else "were"))
        return out

    def cut_rows(self, placed, cells, total_rows, tails=None):
        """A cut (`---`) where a note ends and nothing follows; a note with a release tail gets the release (`===`) there and
        its cut where the tail has died away (when that is before the next note)."""
        for i, (s, e, _) in enumerate(placed):
            nxt = placed[i + 1][0] if i + 1 < len(placed) else None
            if nxt == e or e >= total_rows or e in cells:
                continue
            tail = tails[i] if tails else 0
            if tail:
                cells[e] = ("===", "..", ".")
                end = e + max(1, int(-(-tail // self.row_frames)))
                if end < (nxt if nxt is not None else total_rows) and end not in cells:
                    cells[end] = ("---", "..", ".")
            else:
                cells[e] = ("---", "..", ".")

    def melodic(self, v, g, total_rows):
        t = self.t
        if v >= len(t.voices) or not t.voices[v]:
            return {}
        kind = ("sq", "sq", "tri", "noise")[v]
        want = ("pulse", "pulse", "tri", "noise")[v]
        items = []
        for n in t.voices[v]:
            if n.inst.wave != want:
                self.say("V%d plays a %s wave; the %s channel plays it as its own sound" % (v + 1, n.inst.wave, CHANNELS[v]))
            if n.slide:
                self.r.drop("slide into a note")
            if n.pan is not None:
                self.r.drop("per-note pan")
            items.append((n.start, n.dur, n))
        placed = self.place(items, g, total_rows)
        cells = {}
        for s, _, n in placed:
            if v == 3:
                low4 = min(range(16), key=lambda k: (abs(noise_pitch(k, self.pal) - n.pitch), k))
                text = "%X-#" % low4
            else:
                text = note_text(max(1, min(96, n.pitch - SQUARE_SHIFT if kind == "sq" else n.pitch + 1)))
                if kind == "sq" and n.pitch < SQUARE_LOW:
                    self.low += 1
            vol = "." if kind == "tri" else "%X" % max(0, min(15, n.vol))
            cells[s] = (text, "%02X" % self.inst_index(n.inst, kind), vol)
        spt = 60.0 / (max(1, t.bpm) * PPQ)                    # seconds a tick lasts
        tails = []
        for s_, e_, n in placed:
            if kind == "tri":
                tails.append(0)
                continue
            gate = min(n.dur, (e_ - s_) * g) * spt
            tails.append(release_frames(n.inst, self.volume_values(n.inst), gate))
        self.cut_rows(placed, cells, total_rows, tails)
        return cells

    def dpcm(self, g, total_rows):
        """(cells, [(key note, sample index, rate)], [(name, bytes)])."""
        t = self.t
        events = t.dac[0] if t.dac else []
        if t.ndacs > 1 and any(t.dac[i] for i in range(1, min(len(t.dac), t.ndacs))):
            self.r.drop("DAC tracks D2 and up (FamiTracker has one DPCM channel)")
        if any(e[1] == "level" for e in events):
            self.r.drop("DAC level changes")
        plays = [e for e in events if e[1] == "play"]
        if not plays:
            return {}, [], []
        order, keys, samples, index = {}, [], [], {}
        for e in plays:
            smp = e[2]
            key = (smp.name, smp.rate)
            if key in order:
                continue
            if len(order) >= SAMPLE_KEYS:
                self.say("more than %d different samples: the later ones are left out" % SAMPLE_KEYS, drop=True)
                continue
            order[key] = len(order)
            if smp.name not in index:
                data = b"".join(bytes(p) for p in (smp.tracks[0] if smp.tracks else []))
                if len(smp.tracks) > 1:
                    self.r.approx("sample '%s' is stereo: only its left track is written" % smp.name)
                if len(data) > SAMPLE_BYTES:
                    self.r.approx("sample '%s' is %d bytes; the NES driver plays at most %d" % (smp.name, len(data), SAMPLE_BYTES))
                index[smp.name] = len(samples)
                samples.append((_text(smp.name, 60) or "sample", data))
            keys.append((DPCM_NOTE0 + order[key], index[smp.name], smp.rate & 15))
        items = []
        for e in plays:
            key = (e[2].name, e[2].rate)
            if key in order:
                items.append((e[0], 1, order[key]))
        placed = self.place(items, g, total_rows)
        cells = {}
        for s, _, k in placed:
            cells[s] = (note_text(DPCM_NOTE0 + k), "@DPCM", ".")
        for e in events:
            if e[1] == "stop":
                row = int(round(e[0] / g))
                if row < total_rows and row not in cells:
                    cells[row] = ("---", "..", ".")
        return cells, keys, samples

    # ---- the file
    def run(self):
        t, r = self.t, self.r
        g = self.grid()
        speed, tempo = self.tempo(g)
        bar_rows = max(1, t.bar_ticks // g)
        total_rows = max(1, -(-t.length // g))
        loop_row = 0 if t.once else t.loop_tick // g
        plen = self.pattern_length(bar_rows, loop_row, total_rows)
        if -(-total_rows // plen) > MAX_PATTERNS:
            total_rows = MAX_PATTERNS * plen
            r.drop("the end of the song: FamiTracker holds %d patterns of %d rows, which is %d rows" % (MAX_PATTERNS, plen, total_rows))
        npat = -(-total_rows // plen)
        loop = min(npat - 1, loop_row // plen)
        if not t.once and loop_row >= total_rows:
            loop = 0
        if not t.once and loop_row % plen:
            r.approx("the loop point is not at the start of a pattern: it moves back to pattern %d" % (loop + 1))
        if t.nvoices > 4:
            r.drop("voices %s (the NES has four melodic channels)" % ", ".join("V%d" % (i + 1) for i in range(4, t.nvoices)))
        if t.echo or t.reverb:
            r.drop("echo and reverb")
        if t.detune:
            r.drop("detune")

        chans = [self.melodic(i, g, total_rows) for i in range(4)]
        dcells, keys, samples = self.dpcm(g, total_rows)
        if self.low:
            r.approx("%d pulse note%s below A1 (55 Hz), where the NES's 11-bit period runs out: FamiTracker plays %s wrongly"
                     % (self.low, "" if self.low == 1 else "s", "it" if self.low == 1 else "them"))
        dpcm_inst = None
        if keys:
            dpcm_inst = len(self.insts)
            self.insts.append(("DPCM", -1, -1, -1, -1))
        chans.append({row: (n, "%02X" % dpcm_inst if i == "@DPCM" else i, v) for row, (n, i, v) in dcells.items()})

        last = total_rows - 1
        jump = "B%02X" % loop if not t.once else "C00"
        rows = [[chans[c].get(row, EMPTY) for row in range(npat * plen)] for c in range(5)]
        patterns, order = [], []                     # per channel: [rows of text], its order list
        for c in range(5):
            seen, uniq, orders = {}, [], []
            for k in range(npat):
                lines = []
                for local in range(plen):
                    row = k * plen + local
                    cell = rows[c][row]
                    fx = jump if row == last else "..."
                    lines.append(" ".join(cell) + " " + fx)
                key = tuple(lines)
                if key not in seen:
                    seen[key] = len(uniq)
                    uniq.append(lines)
                orders.append(seen[key])
            patterns.append(uniq)
            order.append(orders)
        if self.cut_release:
            r.approx("the release (r) of %s is played after a note that is followed by a rest; a note that runs straight into the next one, or is "
                     "let go while its level is still rising, is cut where it ends" % ", ".join("'%s'" % n for n in self.cut_release))

        title = _text(self.name or t.title or "", 32)
        author = _text(t.author, 32)
        out = [HEADER, "", "# Song information",
               'TITLE           "%s"' % title, 'AUTHOR          "%s"' % author, 'COPYRIGHT       "%s"' % _text(t.comment, 32), "",
               "# Song comment", 'COMMENT         ""', "",
               "# Global settings", "MACHINE         %d" % (1 if self.pal else 0), "FRAMERATE       0", "EXPANSION       0",
               "VIBRATO         1", "SPLIT           32", "", "# Macros"]
        for kind in (SEQ_VOLUME, SEQ_ARPEGGIO, SEQ_PITCH, SEQ_DUTY):
            for idx, (vals, lp, rel) in enumerate(self.macros[kind]):
                out.append("MACRO%8d %4d %4d %4d %3d : %s" % (kind, idx, lp if rel < 0 else -1, rel - 1 if rel >= 0 else -1, 0, " ".join(map(str, vals))))
        out.append("")
        if samples:
            out.append("# DPCM samples")
            for idx, (nm, data) in enumerate(samples):
                out.append('DPCMDEF%4d%6d "%s"' % (idx, len(data), nm))
                for i in range(0, max(1, len(data)), 32):
                    out.append("DPCM : " + " ".join("%02X" % b for b in data[i:i + 32]))
            out.append("")
        out.append("# Instruments")
        for idx, (nm, vol, arp, pit, dut) in enumerate(self.insts):
            out.append('INST2A03%4d%6d%4d%4d%4d%4d "%s"' % (idx, vol, arp, pit, -1, dut, nm))
            if idx == dpcm_inst:
                for note, smp, rate in keys:
                    out.append("KEYDPCM%4d%4d%4d%6d%4d%4d%6d%4d" % (idx, (note - 1) // 12, (note - 1) % 12, smp, rate, 0, 0, -1))
        out += ["", "# Tracks", 'TRACK%4d%4d%4d "%s"' % (plen, speed, tempo, _text(self.name or t.title or "Song", 60)),
                "COLUMNS : 1 1 1 1 1", ""]
        for k in range(npat):
            out.append("ORDER %02X : %s" % (k, " ".join("%02X" % order[c][k] for c in range(5))))
        out.append("")
        for p in range(max(len(x) for x in patterns)):
            out.append("PATTERN %02X" % p)
            for row in range(plen):
                out.append("ROW %02X" % row + "".join(
                    " : " + (patterns[c][p][row] if p < len(patterns[c]) else "... .. . ...") for c in range(5)))
            out.append("")
        out.append("# End of export")
        r.note("written as FamiTracker text 0.4.2, %s timing, speed %d tempo %d, %d row%s a pattern, %d pattern%s in the order"
               % ("PAL" if self.pal else "NTSC", speed, tempo, plen, "" if plen == 1 else "s", npat, "" if npat == 1 else "s"))
        return "\n".join(out) + "\n", r
