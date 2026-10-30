"""chipsynth songs -> FamiStudio text (the file FamiStudio's File > Import > FamiStudio Text opens).

V1, V2 -> Square 1, 2; V3 -> Triangle; V4 -> Noise; D1 -> DPCM.  Everything else the song holds (more voices, more
DACs, echo, reverb, filter, vibrato, slide, pan, pulse-width sweeps, tempo changes) is dropped and said so in the Report.
The volume ADSR and the arpeggio are sampled at the frame rate into FamiStudio envelopes.  The file says it was written
by FamiStudio `version`, because FamiStudio only reads files whose version starts like its own (4.5 by default).
"""
import math

import numpy as np

from .envelope import FRAME_HZ, adsr
from .famistudio_text import DUTY_PW, NOTE_NAMES, PAL_HZ, noise_pitch
from .report import Report

PPQ = 96
MAX_PATTERN_ROWS = 64
GRIDS = (24, 12, 8, 6, 4, 3, 2, 1)                  # ticks a row can last: 96 / g rows to a beat
CHANNELS = ("Square1", "Square2", "Triangle", "Noise")
DPCM_NOTE0 = 13                                      # C1: the first note a sample is mapped to
SAMPLE_BYTES = 4081                                  # the most the NES driver can play from one sample


def _attr(key, value):
    return ' %s="%s"' % (key, str(value).replace('"', '""'))


def note_name(value):
    return "%s%d" % (NOTE_NAMES[(value - 1) % 12], (value - 1) // 12)


def dump(tune, region="ntsc", version="4.5.3", name=None):
    """A compiled Tune -> (FamiStudio text, Report)."""
    return _Writer(tune, region, version, name).run()


class _Writer:
    def __init__(self, tune, region, version, name):
        self.t, self.version, self.name = tune, version, name
        self.pal = region == "pal"
        self.hz = PAL_HZ if self.pal else FRAME_HZ
        self.r = Report()
        self.inst_used = {}               # (inst key, kind) -> FamiStudio name
        self.inst_lines = []
        self.names = {}                   # id(Instrument) -> chipsynth name
        for n, i in tune.instruments.items():
            self.names.setdefault(id(i), n)

    # ---- timing
    def grid(self):
        t = self.t
        ticks = [t.bar_ticks, t.loop_tick, t.length]
        for v in self.t.voices[:4]:
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
            self.r.drop("tempo changes (FamiStudio needs speed effects for those): the first tempo is used all through")
        bpm = t.bpm_at(0) if hasattr(t, "bpm_at") else t.bpm
        row_frames = 60.0 / (bpm * PPQ) * g * self.hz
        best = None
        for n in range(1, 9):
            total = max(n, round(row_frames * n))
            err = abs(total / n - row_frames) / row_frames
            if best is None or err < best[0] - 1e-9:
                best = (err, n, total)
        err, n, total = best
        base, extra = divmod(total, n)
        groove = [base + 1 if (i * extra) // n != ((i + 1) * extra) // n else base for i in range(n)]
        if base < 1:
            groove = [1]
            self.r.approx("the tempo is faster than one frame a row: it is played at one frame a row")
        elif err > 0.005:
            self.r.approx("the tempo is %.1f%% off after rounding to whole frames" % (err * 100))
        return groove

    # ---- instruments
    def inst_name(self, inst, kind):
        key = (id(inst), kind)
        if key in self.inst_used:
            return self.inst_used[key]
        base = self.names.get(id(inst)) or "Inst%d" % (len(self.inst_used) + 1)
        name = base + ("_noise" if kind == "noise" else "")
        n = 1
        taken = set(self.inst_used.values())
        while name in taken:
            n += 1
            name = "%s_%d" % (base, n)
        self.inst_used[key] = name
        self.inst_lines += self.inst_text(inst, name, kind, base)
        return name

    def inst_text(self, inst, name, kind, label):
        lines = ["\tInstrument" + _attr("Name", name)]
        if kind != "tri":
            vol = self.volume_env(inst)
            if vol:
                lines.append("\t\tEnvelope" + _attr("Type", "Volume") + _attr("Length", len(vol[0]))
                             + (_attr("Loop", vol[1]) if vol[1] >= 0 else "") + (_attr("Release", vol[2]) if vol[2] >= 0 else "")
                             + _attr("Values", ",".join(map(str, vol[0]))))
        if kind == "noise":
            lines.append("\t\tEnvelope" + _attr("Type", "DutyCycle") + _attr("Length", 1) + _attr("Values", 1 if inst.nmode == "short" else 0))
        elif kind == "sq":
            duty = min(range(4), key=lambda i: abs(DUTY_PW[i] - inst.pw))
            if inst.wave == "pulse" and abs(DUTY_PW[duty] - inst.pw) > 3:
                self.r.approx("instrument '%s': pulse width %d%% is played as %d%% (the NES has 12, 25, 50 and 75)" % (label, inst.pw, DUTY_PW[duty]))
            lines.append("\t\tEnvelope" + _attr("Type", "DutyCycle") + _attr("Length", 1) + _attr("Values", duty))
            if inst.wave not in ("pulse", "tri", "noise"):
                self.r.approx("instrument '%s': a %s wave is played as a pulse" % (label, inst.wave))
        if inst.arp:
            step = max(1, round(60.0 / max(1, self.t.bpm) * self.hz / max(1, inst.arprate)))
            vals = [int(o) for o in inst.arp for _ in range(step)]
            lines.append("\t\tEnvelope" + _attr("Type", "Arpeggio") + _attr("Length", len(vals)) + _attr("Loop", 0)
                         + _attr("Values", ",".join(map(str, vals))))
        dropped = [w for w, on in (("vibrato", inst.vib_rate and inst.vib_depth), ("slide", inst.slide), ("pulse-width sweep", inst.pws_rate),
                                   ("filter", inst.filt), ("pan", inst.pan)) if on]
        if dropped:
            self.r.drop("instrument '%s': %s" % (label, ", ".join(dropped)))
        return lines

    def volume_env(self, inst):
        """(values, loop, release) sampling the ADSR once a frame, or None for a flat full volume."""
        t = (np.arange(240) + 0.5) / self.hz                # the middle of each frame
        level = adsr(inst.a & 15, inst.d & 15, inst.s & 15, inst.r & 15, None, t) * inst.vol
        vals = [int(max(0, min(15, round(x)))) for x in level]
        last = len(vals) - 1
        while last > 0 and vals[last - 1] == vals[-1]:
            last -= 1
        gate = vals[:last + 1]
        if gate == [15]:
            return None
        sus = gate[-1]
        rel_vals = []
        from ..synth import DECAY_MS
        rsec = DECAY_MS[inst.r & 15] / 1000.0
        if sus and rsec * self.hz >= 2:
            k = 1
            while k < 90:
                v = int(round(level[-1] * math.exp(-6.9 * (k / self.hz) / rsec)))
                rel_vals.append(max(0, v))
                if v <= 0:
                    break
                k += 1
        values = gate + rel_vals
        return values[:255], len(gate) - 1, (len(gate) if rel_vals else -1)

    # ---- the file
    def run(self):
        t, r = self.t, self.r
        g = self.grid()
        groove = self.tempo(g)
        nl = min(groove)
        bar_rows = max(1, t.bar_ticks // g)
        plen = bar_rows if bar_rows <= MAX_PATTERN_ROWS else 16
        total_rows = max(1, -(-t.length // g))
        npat = -(-total_rows // plen)
        loop_row = t.loop_tick // g
        loop = -1 if t.once else min(npat - 1, loop_row // plen)
        if not t.once and loop_row % plen:
            r.approx("the loop point is not at the start of a pattern: it moves back to pattern %d" % (loop + 1))
        if t.nvoices > 4:
            r.drop("voices %s (FamiStudio's NES has four melodic channels)" % ", ".join("V%d" % (i + 1) for i in range(4, t.nvoices)))
        if t.ndacs > 1 and any(t.dac[i] for i in range(1, min(len(t.dac), t.ndacs))):
            r.drop("DAC tracks D2 and up (FamiStudio has one DPCM channel)")
        if t.echo or t.reverb:
            r.drop("echo and reverb")
        if t.detune:
            r.drop("detune")

        chans = [self.melodic(i, g, plen, npat, nl) for i in range(4)]
        dpcm_lines, dpcm_chan, samples = self.dpcm(g, plen, npat, nl)

        out = ["Project" + _attr("Version", self.version) + _attr("TempoMode", "FamiStudio")
               + (_attr("Name", self.name or t.title) if (self.name or t.title) else "")
               + (_attr("Author", t.author) if t.author else "") + (_attr("Copyright", t.comment) if t.comment else "")
               + (_attr("PAL", "true") if self.pal else "")]
        for sname, data in samples:
            out.append("\tDPCMSample" + _attr("Name", sname) + _attr("Data", data.hex()))
        out += self.inst_lines
        out += dpcm_lines
        out.append("\tSong" + _attr("Name", self.name or t.title or "Song") + _attr("Length", npat) + _attr("LoopPoint", loop)
                   + _attr("PatternLength", plen) + _attr("BeatLength", PPQ // g) + _attr("NoteLength", nl)
                   + _attr("Groove", "-".join(map(str, groove))) + _attr("GroovePaddingMode", "Beginning"))
        for ctype, lines in zip(CHANNELS + ("DPCM",), chans + [dpcm_chan]):
            out.append("\t\tChannel" + _attr("Type", ctype))
            out += lines
        r.note("written for FamiStudio %s, %s timing, %d row%s a pattern" % (self.version, "PAL" if self.pal else "NTSC", plen, "" if plen == 1 else "s"))
        return "\n".join(out) + "\n", r

    def rows(self, items, g, nl, total_rows):
        """[(start tick, dur ticks, payload)] -> [(start row, dur rows, payload)] with no overlap and nothing past the end."""
        out = []
        for start, dur, p in sorted(items, key=lambda x: x[0]):
            s = int(round(start / g))
            d = max(1, int(round(dur / g)))
            if out and s < out[-1][0] + out[-1][1]:
                if s <= out[-1][0]:
                    continue
                out[-1] = (out[-1][0], s - out[-1][0], out[-1][2])
            out.append((s, min(d, max(1, total_rows - s)), p))
        return out

    def patterns(self, notes, plen, npat, nl, attrs_of):
        """Lay [(row, dur rows, payload)] out in patterns of `plen` rows (a note over a pattern's end goes on in the next
        without a new attack).  Returns the Channel's lines."""
        pats = [[] for _ in range(npat)]
        for row, dur, p in notes:
            r0 = row
            first = True
            while dur > 0 and r0 // plen < npat:
                k, local = divmod(r0, plen)
                take = min(dur, plen - local)
                pats[k].append((local, take, p, first))
                r0 += take
                dur -= take
                first = False
        # a stop where a note ends and nothing follows
        for i in range(len(notes)):
            row, dur, p = notes[i]
            end = row + dur
            nxt = notes[i + 1][0] if i + 1 < len(notes) else None
            if nxt != end and end // plen < npat:
                pats[end // plen].append((end % plen, None, None, True))
        names, seen, lines, inst_of = [], {}, [], {}
        instances = []
        for k, items in enumerate(pats):
            if not items:
                continue
            items.sort(key=lambda x: (x[0], x[1] is None))
            clean = []
            for it in items:
                if clean and clean[-1][0] == it[0]:
                    continue                      # a stop on the row a note starts: the note wins
                clean.append(it)
            key = tuple((l, d, str(p), f) for l, d, p, f in clean)
            if key not in seen:
                seen[key] = "P%d" % (len(seen) + 1)
                lines.append("\t\t\tPattern" + _attr("Name", seen[key]))
                for l, d, p, f in clean:
                    lines.append("\t\t\t\t" + attrs_of(l * nl, d, p, f))
            instances.append((k, seen[key]))
        for k, nm in instances:
            lines.append("\t\t\tPatternInstance" + _attr("Time", k) + _attr("Pattern", nm))
        return lines

    def melodic(self, v, g, plen, npat, nl):
        t = self.t
        total_rows = npat * plen
        kind = ("sq", "sq", "tri", "noise")[v]
        if v >= len(t.voices) or not t.voices[v]:
            return []
        notes = []
        for n in t.voices[v]:
            if kind != "tri" and n.inst.wave == "tri" or kind == "sq" and n.inst.wave in ("saw", "sine", "noise"):
                self.r.approx("V%d plays a %s wave; the %s channel plays it as its own sound" % (v + 1, n.inst.wave, CHANNELS[v]))
            notes.append((n.start, n.dur, n))
        placed = self.rows(notes, g, nl, total_rows)
        state = {"vol": 15}
        for _, _, n in placed:
            if n.slide:
                self.r.drop("slide into a note")
            if n.pan is not None:
                self.r.drop("per-note pan")
                break

        def attrs(time, dur, n, first):
            if n is None:
                return "Note" + _attr("Time", time) + _attr("Value", "Stop")
            val = self.value(v, n.pitch)
            s = "Note" + _attr("Time", time) + _attr("Value", note_name(val)) + _attr("Duration", dur * nl)
            if first:
                s += _attr("Instrument", self.inst_name(n.inst, kind))
                if kind != "tri" and n.vol != state["vol"]:
                    s += _attr("Volume", n.vol)
                    state["vol"] = n.vol
            else:
                s += _attr("Attack", "false")
            return s
        return self.patterns(placed, plen, npat, nl, attrs)

    def value(self, v, pitch):
        if v == 3:
            low4 = min(range(16), key=lambda k: (abs(noise_pitch(k, self.pal) - pitch), k))
            return 16 + low4
        return max(1, min(96, pitch + 1))

    def dpcm(self, g, plen, npat, nl):
        t = self.t
        events = t.dac[0] if t.dac else []
        plays = [e for e in events if e[1] == "play"]
        if any(e[1] == "level" for e in events):
            self.r.drop("DAC level changes")
        if not plays:
            return [], [], []
        order, mapping, samples = {}, [], []
        for e in plays:
            smp = e[2]
            key = (smp.name, smp.rate)
            if key not in order:
                order[key] = len(order)
                tracks = smp.tracks[0] if smp.tracks else []
                data = b"".join(bytes(p) for p in tracks)
                if len(smp.tracks) > 1:
                    self.r.approx("sample '%s' is stereo: only its left track is written" % smp.name)
                if len(data) > SAMPLE_BYTES:
                    self.r.approx("sample '%s' is %d bytes; the NES driver plays at most %d" % (smp.name, len(data), SAMPLE_BYTES))
                if not any(s[0] == smp.name for s in samples):
                    samples.append((smp.name, data))
                mapping.append((DPCM_NOTE0 + order[key], smp.name, smp.rate))
        if len(order) > 36:
            self.r.drop("more than 36 different samples")
        lines = ["\tInstrument" + _attr("Name", "DPCM")]
        for val, sname, rate in mapping:
            lines.append("\t\tDPCMMapping" + _attr("Note", note_name(val)) + _attr("Sample", sname) + _attr("Pitch", rate & 15)
                         + _attr("Loop", "false") + _attr("Bank", 0))
        # a play lasts until the next play or stop
        items = []
        stops = sorted(e[0] for e in events if e[1] == "stop")
        for i, e in enumerate(plays):
            end = t.length
            nxt = [x[0] for x in plays[i + 1:i + 2]] + [s for s in stops if s > e[0]][:1]
            if nxt:
                end = min(nxt)
            items.append((e[0], end - e[0], (e[2].name, e[2].rate)))
        placed = self.rows(items, g, nl, npat * plen)
        val_of = {k: DPCM_NOTE0 + i for k, i in order.items()}

        def attrs(time, dur, p, first):
            if p is None:
                return "Note" + _attr("Time", time) + _attr("Value", "Stop")
            s = "Note" + _attr("Time", time) + _attr("Value", note_name(val_of[p])) + _attr("Duration", dur * nl)
            return s + (_attr("Instrument", "DPCM") if first else _attr("Attack", "false"))
        return lines, self.patterns(placed, plen, npat, nl, attrs), samples
