"""FamiStudio text files (File > Export > FamiStudio Text) -> chipsynth songs.

Written from the format as FamiStudio's own reader and writer define it (FamistudioTextFile.cs, MIT licensed); no code
is copied.  One line per thing: a keyword, then Key="value" attributes ("" inside a value is a quote).

    Project  DPCMSample  Instrument (Envelope, DPCMMapping)  Arpeggio  Song (PatternCustomSettings, Channel
    (Pattern (Note), PatternInstance))

What carries over: the five NES channels (Square 1 and 2 -> V1, V2; Triangle -> V3; Noise -> V4; DPCM -> D1),
notes, stops, note volume, instruments (volume envelope fitted to a d s r; duty; arpeggio), DPCM samples with their
rate, tempo, pattern lengths and the loop point.  What does not is listed in the Report: expansion chips, pitch
envelopes, slides, vibrato, grooves, speed changes, note delays and the like.
"""
import math
import re
from dataclasses import dataclass, field

from .. import dpcm, song as sg
from ..synth import Instrument
from .envelope import FRAME_HZ, GOOD, fit_arpeggio, fit_volume
from .report import Report

PAL_HZ = 50.0070
PPQ = 96
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
LETTERS = (("c", 0), ("c", 1), ("d", 0), ("d", 1), ("e", 0), ("f", 0), ("f", 1), ("g", 0), ("g", 1), ("a", 0), ("a", 1), ("b", 0))
DUTY_PW = (12, 25, 50, 75)                       # duty cycle 0-3 as a percent
BASE = {"Square1": 0, "Square2": 1, "Triangle": 2, "Noise": 3, "DPCM": 4}
NOISE_PERIOD = (4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068)
NES_CPU = 1789773.0


class FamiStudioError(ValueError):
    pass


# ------------------------------------------------------------------ the file
_NAME = re.compile(r'^\s*([^"=\s]+)\s*(.*?)\s*$')
_ATTR = re.compile(r'([^"=\s]+)\s*=\s*"((?:""|[^"])*)"')


def split_line(line):
    """(keyword, {attribute: value}) of one line, or None when it is blank or not a keyword line."""
    m = _NAME.match(line)
    if not m:
        return None
    rest = m.group(2)
    if _ATTR.sub("", rest).strip():
        return None
    return m.group(1), {k: v.replace('""', '"') for k, v in _ATTR.findall(rest)}


def looks_like(text):
    """True when the first non-blank line is a FamiStudio `Project` line."""
    for line in text.splitlines():
        if line.strip():
            got = split_line(line.lstrip("﻿"))
            return bool(got) and got[0] == "Project"
    return False


@dataclass
class Project:
    attrs: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)        # name -> (bytes, attrs)
    instruments: dict = field(default_factory=dict)    # name -> {"attrs", "envelopes": {type: attrs}, "maps": [attrs]}
    arpeggios: dict = field(default_factory=dict)      # name -> attrs
    songs: list = field(default_factory=list)          # [Song]


@dataclass
class FSong:
    attrs: dict
    custom: dict = field(default_factory=dict)         # pattern index -> PatternCustomSettings attrs
    channels: list = field(default_factory=list)       # [FChannel]


@dataclass
class FChannel:
    type: str
    patterns: dict = field(default_factory=dict)       # name -> [note attrs]
    instances: dict = field(default_factory=dict)      # pattern index -> pattern name


def parse(text):
    """Read the file into a Project (raises FamiStudioError when it is not one)."""
    prj, inst, song, chan, pat = None, None, None, None, None
    for n, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        got = split_line(raw)
        if got is None:
            continue
        cmd, a = got
        if cmd == "Project":
            prj = Project(attrs=a)
        elif prj is None:
            raise FamiStudioError("this is not a FamiStudio text file (it does not start with a Project line)")
        elif cmd == "DPCMSample":
            data = a.get("Data", "")
            try:
                prj.samples[a.get("Name", "")] = (bytes.fromhex(data), a)
            except ValueError:
                raise FamiStudioError("line %d: the DPCM sample data is not hexadecimal" % n)
        elif cmd == "Instrument":
            inst = {"attrs": a, "envelopes": {}, "maps": []}
            prj.instruments[a.get("Name", "")] = inst
        elif cmd == "DPCMMapping" and inst is not None:
            inst["maps"].append(a)
        elif cmd == "Envelope" and inst is not None:
            inst["envelopes"][a.get("Type", "")] = a
        elif cmd == "Arpeggio":
            prj.arpeggios[a.get("Name", "")] = a
        elif cmd == "Song":
            song = FSong(attrs=a)
            prj.songs.append(song)
        elif cmd == "PatternCustomSettings" and song is not None:
            song.custom[_int(a, "Time", 0)] = a
        elif cmd == "Channel" and song is not None:
            chan = FChannel(type=a.get("Type", ""))
            song.channels.append(chan)
        elif cmd == "Pattern" and chan is not None:
            pat = chan.patterns.setdefault(a.get("Name", ""), [])
        elif cmd == "Note" and pat is not None:
            pat.append(a)
        elif cmd == "PatternInstance" and chan is not None:
            chan.instances[_int(a, "Time", 0)] = a.get("Pattern", "")
    if prj is None:
        raise FamiStudioError("this is not a FamiStudio text file (it does not start with a Project line)")
    if not prj.songs:
        raise FamiStudioError("the project has no songs")
    return prj


def _int(a, key, default=0):
    try:
        return int(a.get(key, default))
    except ValueError:
        return default


def _values(text):
    try:
        return [int(x) for x in text.split(",") if x.strip() != ""]
    except ValueError:
        return []


def note_value(name):
    """FamiStudio note name -> value: C0 is 1 (so chipsynth's pitch is value - 1).  'Stop' is 0, 'Release' 0x80."""
    if name == "Stop":
        return 0
    if name == "Release":
        return 0x80
    try:
        return int(name[-1]) * 12 + NOTE_NAMES.index(name[:-1]) + 1
    except (ValueError, IndexError):
        raise FamiStudioError("not a note name: %r" % name)


# ------------------------------------------------------------------ timing
class Timing:
    """Rows -> ticks for one song.  A row is `tpr` ticks; the song's tempo makes a tick as long as the row is on the NES."""

    def __init__(self, prj, song, report, region_hz):
        a = song.attrs
        self.famitracker = prj.attrs.get("TempoMode", "FamiStudio") == "FamiTracker"
        self.beat = max(1, _int(a, "BeatLength", 4))
        self.pattern_len = max(1, _int(a, "PatternLength", 16))
        if self.famitracker:
            self.note_len = 1
            speed, tempo = max(1, _int(a, "FamiTrackerSpeed", 6)), max(1, _int(a, "FamiTrackerTempo", 150))
            row_sec = speed * 2.5 / tempo
        else:
            self.note_len = max(1, _int(a, "NoteLength", 10))
            groove = [g for g in _values(a.get("Groove", "").replace("-", ",")) if g > 0] or [self.note_len]
            if len(groove) > 1:
                report.approx("song '%s': the groove %s is played at its average row length" % (a.get("Name", ""), "-".join(map(str, groove))))
            row_sec = (sum(groove) / len(groove)) / region_hz
        tpr = max(1, round(PPQ / self.beat))
        bpm = 60.0 / (row_sec * (PPQ / tpr))
        if bpm > 300:
            tpr = max(1, int(tpr * 300 / bpm))
        elif bpm < 30:
            tpr = max(1, -(-int(tpr * 30 * 1000 / bpm) // 1000))
        bpm = 60.0 / (row_sec * (PPQ / tpr))
        self.bpm = int(max(30, min(300, round(bpm))))
        self.tpr = tpr
        if abs(self.bpm - bpm) / bpm > 0.01:
            report.approx("song '%s': its tempo (%.1f bpm) is outside what chipsynth can play, so it runs at %d bpm" % (a.get("Name", ""), bpm, self.bpm))
        self.length = max(1, _int(a, "Length", 1))
        self.starts, self.lens, pos = [], [], 0
        for p in range(self.length):
            c = song.custom.get(p)
            ln = max(1, _int(c, "Length", self.pattern_len)) if c else self.pattern_len
            if c and not self.famitracker and (_int(c, "NoteLength", self.note_len) != self.note_len or c.get("Groove")):
                report.approx("song '%s': pattern %d has its own tempo settings, which are not used" % (a.get("Name", ""), p + 1))
            self.starts.append(pos)
            self.lens.append(ln)
            pos += ln
        self.rows = pos
        self.loop = _int(a, "LoopPoint", -1)

    def ticks(self, pattern, units):
        """Tick of `units` (FamiStudio's time unit: a row is `note_len` of them) into pattern `pattern`."""
        return int(round((self.starts[pattern] + units / self.note_len) * self.tpr))

    def dur_ticks(self, units):
        return max(1, int(round(units / self.note_len * self.tpr)))


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


def noise_pitch(low4, pal=False):
    """chipsynth pitch of a FamiStudio noise note whose value ends in `low4` (the driver writes period index low4 ^ 15, so
    a higher note is a higher noise).  The real shift rates of the top few periods are above what chipsynth can
    play, so they are kept in order just under the top of the range."""
    period = (4, 7, 14, 30, 60, 88, 118, 148, 188, 236, 354, 472, 708, 944, 1890, 3778)[low4 ^ 15] if pal else NOISE_PERIOD[low4 ^ 15]
    hz = (1662607.0 if pal else NES_CPU) / (period * 8.0)
    return max(0, min(round(57 + 12 * math.log2(hz / 440.0)), 92 + low4))


# ------------------------------------------------------------------ the converter
@dataclass
class Imported:
    name: str
    song: sg.Song
    samples: dict                 # name -> dpcm.Sample, for the Play events


@dataclass
class Result:
    project_name: str
    songs: list
    report: Report


def load(text, song=None, region=None):
    """FamiStudio text -> Result.  `song` picks one song by name or 1-based number (default: all);
    `region` 'ntsc' or 'pal' overrides the file's PAL flag."""
    prj = parse(text)
    report = Report()
    pal = (region == "pal") if region else prj.attrs.get("PAL", "false").lower() == "true"
    if prj.attrs.get("Version"):
        report.note("written by FamiStudio %s" % prj.attrs["Version"])
    report.note("%s timing, %s tempo" % ("PAL" if pal else "NTSC", prj.attrs.get("TempoMode", "FamiStudio")))
    if prj.attrs.get("Tuning", "440") != "440":
        report.approx("the tuning is %s Hz; chipsynth plays A4 at 440 Hz" % prj.attrs["Tuning"])
    expansions = [e.strip() for e in prj.attrs.get("Expansions", "").split(",") if e.strip()]
    if expansions:
        report.drop("expansion audio (%s): its channels and instruments are not imported" % ", ".join(expansions))
    chosen = prj.songs
    if song is not None:
        chosen = [s for i, s in enumerate(prj.songs, 1) if s.attrs.get("Name") == str(song) or str(i) == str(song)]
        if not chosen:
            raise FamiStudioError("no song '%s' (the file has: %s)" % (song, ", ".join(s.attrs.get("Name", "?") for s in prj.songs)))
    out = [_convert(prj, s, report, pal) for s in chosen]
    return Result(prj.attrs.get("Name", ""), out, report)


def _convert(prj, fs, report, pal):
    hz = PAL_HZ if pal else FRAME_HZ
    name = fs.attrs.get("Name", "Song")
    tm = Timing(prj, fs, report, hz)
    song = sg.Song(title=name, author=prj.attrs.get("Author", ""), bpm=tm.bpm, voices=4, dacs=1)
    if tm.pattern_len % tm.beat == 0 and 1 <= tm.pattern_len // tm.beat <= 32:
        song.time = (tm.pattern_len // tm.beat, 4)             # a bar is a pattern, so a loop point lands on a bar
    elif tm.loop > 0:
        report.approx("song '%s': its loop point is not on a bar here, so it moves back to the start of its bar" % name)
    if prj.attrs.get("Copyright"):
        song.comment = prj.attrs["Copyright"]
    song.tracks = {}
    song.__post_init__()
    ctx = _Ctx(prj, song, tm, report, pal, hz)
    end = tm.rows * tm.tpr
    for ch in fs.channels:
        if ch.type not in BASE:
            if any(ch.patterns.values()):
                report.drop("channel %s (expansion audio)" % ch.type)
            continue
        idx = BASE[ch.type]
        events = ctx.channel(ch, idx)
        track = "D1" if idx == 4 else "V%d" % (idx + 1)
        song.tracks[track] = ctx.finish(events, end, idx == 0, tm)
    song.__post_init__()
    return Imported(name, song, ctx.samples)


class _Ctx:
    def __init__(self, prj, song, tm, report, pal, hz):
        self.prj, self.song, self.tm, self.report, self.pal, self.hz = prj, song, tm, report, pal, hz
        self.inst_names, self.inst_used = {}, set()
        self.fit_cache = {}
        self.samples = {}
        self.sample_names = {}
        self.sample_used = set()

    # ---- instruments
    def instrument(self, fname, chan_type, duty):
        """Select-name of FamiStudio instrument `fname` on a channel of `chan_type`, with note-level `duty` (or None)."""
        key = (fname, chan_type, duty)
        if key in self.inst_names:
            return self.inst_names[key]
        src = self.prj.instruments.get(fname)
        inst = Instrument()
        inst.wave = {"Square1": "pulse", "Square2": "pulse", "Triangle": "tri", "Noise": "noise"}[chan_type]
        label = fname or "default"
        if src is not None:
            envs = src["envelopes"]
            if src["attrs"].get("Expansion"):
                self.report.drop("instrument '%s' uses expansion audio: plain defaults used" % label)
            vol = envs.get("Volume")
            if vol is not None and chan_type != "Triangle":
                inst = self._volume(inst, label, vol)
            elif vol is not None:
                self.report.note("instrument '%s': the triangle has no volume, so its volume envelope is ignored" % label)
            self._duty(inst, label, envs.get("DutyCycle"), duty, chan_type)
            self._arp(inst, label, envs.get("Arpeggio"))
            if envs.get("Pitch") is not None:
                self.report.drop("instrument '%s': its pitch envelope" % label)
        else:
            if duty is not None:
                self._duty(inst, label, None, duty, chan_type)
        suffix = "" if duty is None else "_d%d" % duty
        name = _ident(label + ("_" + chan_type.lower() if chan_type in ("Triangle", "Noise") else "") + suffix, self.inst_used)
        self.song.instruments[name] = inst
        self.inst_names[key] = name
        return name

    def _volume(self, inst, label, env):
        vals = _values(env.get("Values", ""))
        length = _int(env, "Length", len(vals))
        loop, rel = _int(env, "Loop", -1), _int(env, "Release", -1)
        key = (tuple(vals), length, loop, rel)
        if key not in self.fit_cache:
            self.fit_cache[key] = fit_volume(vals, loop, rel, length, self.hz)
        fields, err = self.fit_cache[key]
        for k, v in fields.items():
            setattr(inst, k, v)
        if env.get("Relative", "false").lower() == "true":
            self.report.approx("instrument '%s': a relative volume envelope is treated as absolute" % label)
        if err > GOOD:
            self.report.approx("instrument '%s': its volume envelope does not fit an attack, decay and sustain (RMS error %.2f); the closest is used" % (label, err))
        return inst

    def _duty(self, inst, label, env, note_duty, chan_type):
        if chan_type == "Triangle":
            return
        vals = _values(env.get("Values", "")) if env else []
        first = note_duty if note_duty is not None else (vals[0] if vals else 2 if chan_type != "Noise" else 0)
        if chan_type == "Noise":
            inst.nmode = "short" if first == 1 else "long"
            if len(set(vals)) > 1:
                self.report.approx("instrument '%s': its noise mode changes during the note; the first is kept" % label)
            return
        inst.pw = DUTY_PW[first & 3]
        used = set(vals)
        if note_duty is None and len(used) > 1:
            body = vals[_int(env, "Loop", -1):] if _int(env, "Loop", -1) >= 0 else []
            cycle = _periodic(body)
            if cycle and len(set(cycle)) == 2 and cycle.count(cycle[0]) in (1, len(cycle) - 1, len(cycle) // 2) and len(cycle) >= 2:
                lo, hi = sorted(set(cycle))
                inst.pws_from, inst.pws_to = DUTY_PW[lo & 3], DUTY_PW[hi & 3]
                inst.pws_rate = max(1, min(15, round(self.hz / len(cycle) / 0.5)))
                self.report.approx("instrument '%s': its duty envelope is a pulse-width sweep" % label)
            else:
                self.report.approx("instrument '%s': its duty envelope changes during the note; the first duty is kept" % label)

    def _arp(self, inst, label, env):
        if env is None:
            return
        vals = _values(env.get("Values", ""))[: _int(env, "Length", 10 ** 6)]
        frames_per_beat = 60.0 / (self.tm.bpm) * self.hz
        arp, rate, exact = fit_arpeggio(vals, _int(env, "Loop", -1), frames_per_beat)
        if not arp:
            return
        inst.arp, inst.arprate = arp, rate
        if not exact:
            self.report.approx("instrument '%s': its arpeggio envelope is not a repeating cycle of 8 steps or fewer; its repeating part is used" % label)

    # ---- samples
    def sample(self, fname, value):
        """The name of the sample DPCM note `value` plays through instrument `fname`, or None."""
        src = self.prj.instruments.get(fname)
        if src is None:
            return None
        for m in src["maps"]:
            try:
                if note_value(m.get("Note", "")) != value:
                    continue
            except FamiStudioError:
                continue
            sname = m.get("Sample", "")
            pitch = max(0, min(15, _int(m, "Pitch", 15)))
            key = (sname, pitch)
            if key not in self.sample_names:
                data = self.prj.samples.get(sname)
                if data is None:
                    self.report.drop("the DPCM sample '%s' is not in the file" % sname)
                    return None
                n = _letters(sname, self.sample_used)
                self.samples[n] = dpcm.Sample(name=n, rate=pitch, tracks=[[bytes(data[0])]])
                self.sample_names[key] = n
                if data[1].get("DmcInitialValue", "32") != "32":
                    self.report.approx("sample '%s': its initial DMC value is not kept" % sname)
            if m.get("Loop", "false").lower() == "true":
                self.report.approx("sample '%s' loops on a note; chipsynth plays it once" % sname)
            return self.sample_names[key]
        return None

    # ---- a channel's events
    def channel(self, ch, idx):
        """The channel's timed events, in order: ('note', tick-less pitch, dur, instrument name, volume), ('play', sample, dur), ('stop',)."""
        tm = self.tm
        raw = []
        for p, pname in sorted(ch.instances.items()):
            if not 0 <= p < tm.length:
                continue
            for a in ch.patterns.get(pname, []):
                t = _int(a, "Time", 0)
                if t >= tm.lens[p] * tm.note_len:
                    continue
                raw.append((tm.ticks(p, t), tm.starts[p] * tm.tpr + tm.lens[p] * tm.tpr, a))
        raw.sort(key=lambda r: r[0])
        for _, _, a in raw:
            for k in ("Speed", "NoteDelay", "CutDelay", "VibratoSpeed", "FinePitch", "SlideTarget", "PhaseReset", "DeltaCounter", "VolumeSlideTarget"):
                if k in a:
                    self.report.drop("the %s effect on %s notes" % ("slide" if k == "SlideTarget" else k, ch.type))
        events, inst_cur, vol_cur = [], None, 15
        for i, (tick, pend, a) in enumerate(raw):
            nxt = raw[i + 1][0] if i + 1 < len(raw) else None
            val = a.get("Value")
            if "Volume" in a and idx != 2:
                vol_cur = max(0, min(15, _int(a, "Volume", 15)))
            if val is None:
                if "Volume" in a and idx != 2:
                    self.report.approx("a volume change in the middle of a note on %s is applied from the next note" % ch.type)
                continue
            v = note_value(val)
            if v == 0 or v == 0x80:                                         # Stop, Release
                events.append((tick, "stop"))
                continue
            dur = tm.dur_ticks(_int(a, "Duration", tm.note_len))
            end = tick + dur
            if nxt is not None and nxt < end:
                end = nxt
            end = min(end, pend)
            if end <= tick:
                continue
            if idx == 4:
                inst_cur = a.get("Instrument", inst_cur)
                smp = self.sample(inst_cur, v)
                if smp is None:
                    self.report.drop("a DPCM note with no sample mapped to it")
                    continue
                events.append((tick, "play", smp, end - tick))
                continue
            if "Instrument" in a:
                inst_cur = a["Instrument"]
            if idx == 3:
                pitch = noise_pitch(v & 15, self.pal)
            else:
                pitch = max(0, min(107, v - 1))
            duty = _int(a, "DutyCycle", -1) if "DutyCycle" in a else None
            if duty is not None and duty < 0:
                duty = None
            name = self.instrument(inst_cur or "", ch.type, duty)
            events.append((tick, "note", pitch, end - tick, name, vol_cur if idx != 2 else None))
        return events

    def finish(self, events, end, first, tm):
        """Turn the timed events into MML-model events: rests between, one voice at a time."""
        out, pos, octave, inst, vol = [], 0, 4, None, None
        loop_tick = tm.starts[tm.loop] * tm.tpr if 0 < tm.loop < tm.length else None
        loop_done = loop_tick is None or not first

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
        for ev in events:
            tick, kind = ev[0], ev[1]
            if tick < pos:
                continue
            if kind == "stop":
                advance(tick)
            elif kind == "play":
                advance(tick)
                out.append(sg.Play(ev[2], ev[3]))
                pos = tick + ev[3]
            else:
                _, _, pitch, dur, name, v = ev
                advance(tick)
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
                out.append(sg.Note(letter, acc, dur))
                pos = tick + dur
        advance(end)
        if not loop_done:
            out.append(sg.Loop())
        return out


def _periodic(seq):
    for n in range(1, len(seq) // 2 + 1):
        if len(seq) % n == 0 and seq[:n] * (len(seq) // n) == seq:
            return seq[:n]
    return list(seq)
