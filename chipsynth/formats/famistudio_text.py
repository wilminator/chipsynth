"""FamiStudio text files (File > Export > FamiStudio Text) -> chipsynth songs.

Written from the format as FamiStudio's own reader and writer define it (FamistudioTextFile.cs, MIT licensed); no code
is copied.  One line per thing: a keyword, then Key="value" attributes ("" inside a value is a quote).

    Project  DPCMSample  Instrument (Envelope, DPCMMapping)  Arpeggio  Song (PatternCustomSettings, Channel
    (Pattern (Note), PatternInstance))

What carries over: the five NES channels (Square 1 and 2 -> V1, V2; Triangle -> V3; Noise -> V4; DPCM -> D1),
notes, stops, note volume, instruments (volume envelope fitted to a d s r; duty; arpeggio), DPCM samples with their
rate, tempo, pattern lengths and the loop point.  Expansion channels (VRC6, MMC5, FDS, N163, S5B and EPSM squares) take
voices V5 and up, and an instrument whose envelopes do not fit one chipsynth instrument is played a frame at a time (see
`_Ctx.plan`).  What does not carry over is listed in the Report: VRC7 and EPSM FM, slides, vibrato effects, grooves,
speed changes, note delays and the like.
"""
import math
import re
from dataclasses import dataclass, field

from .. import dpcm, song as sg
from ..synth import Instrument
from .envelope import (FRAME_HZ, GOOD, TAIL_FRAMES, accumulate, fit_arpeggio, fit_vibrato, fit_volume, fit_wave,
                       pitch_semitones, resolve_note)
from .report import Report

PAL_HZ = 50.0070
PPQ = 96
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
LETTERS = (("c", 0), ("c", 1), ("d", 0), ("d", 1), ("e", 0), ("f", 0), ("f", 1), ("g", 0), ("g", 1), ("a", 0), ("a", 1), ("b", 0))
DUTY_PW = (12, 25, 50, 75)                       # duty cycle 0-3 as a percent
BASE = {"Square1": 0, "Square2": 1, "Triangle": 2, "Noise": 3, "DPCM": 4}
NOISE_PERIOD = (4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068)
NES_CPU = 1789773.0

# Expansion audio.  A channel is imported when its chip is named in the Project's Expansions and chipsynth can play what the
# channel makes: a pulse (with the chip's duties), a saw, or a wavetable fitted to the nearest of its waves.
VRC6_PW = (6, 12, 19, 25, 31, 38, 44, 50)         # VRC6 duty 0-7 is (n + 1) / 16
EXP_WAVE = {"VRC6Square1": "pulse", "VRC6Square2": "pulse", "VRC6Saw": "saw", "MMC5Square1": "pulse", "MMC5Square2": "pulse",
            "FDS": "table", "S5BSquare1": "pulse", "S5BSquare2": "pulse", "S5BSquare3": "pulse",
            "EPSMSquare1": "pulse", "EPSMSquare2": "pulse", "EPSMSquare3": "pulse"}
EXP_WAVE.update({"N163Wave%d" % i: "table" for i in range(1, 9)})
EXP_ORDER = (["VRC6Square1", "VRC6Square2", "VRC6Saw", "MMC5Square1", "MMC5Square2", "FDS"] + ["N163Wave%d" % i for i in range(1, 9)]
             + ["S5BSquare1", "S5BSquare2", "S5BSquare3", "EPSMSquare1", "EPSMSquare2", "EPSMSquare3"])
EXP_GROUP = {"VRC6Square1": "vrc6square", "VRC6Square2": "vrc6square", "VRC6Saw": "vrc6saw", "MMC5Square1": "mmc5", "MMC5Square2": "mmc5",
             "FDS": "fds", "S5BSquare1": "s5b", "S5BSquare2": "s5b", "S5BSquare3": "s5b",
             "EPSMSquare1": "epsm", "EPSMSquare2": "epsm", "EPSMSquare3": "epsm"}
EXP_GROUP.update({"N163Wave%d" % i: "n163" for i in range(1, 9)})
MAX_VOICES = 16
CARRIED_CHIPS = ("VRC6", "MMC5", "FDS", "N163", "S5B", "EPSM")      # chips with at least one channel chipsynth can play
# Channels whose pitch register FamiStudio's pitch envelope moves in units of an NES-style period (see envelope.pitch_semitones).
PITCH_K = {"Square1": 16.0, "Square2": 16.0, "Triangle": 32.0, "MMC5Square1": 16.0, "MMC5Square2": 16.0,
           "VRC6Square1": 16.0, "VRC6Square2": 16.0, "VRC6Saw": 14.0}


def chip_of(channel_type):
    """The expansion chip a FamiStudio channel type belongs to ('VRC6', 'N163', ...), or '' for the NES's own."""
    for chip in ("VRC6", "VRC7", "MMC5", "N163", "S5B", "EPSM"):
        if channel_type.startswith(chip):
            return chip
    return "FDS" if channel_type == "FDS" else ""


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
    for line in text.lstrip("\ufeff").splitlines():
        if line.strip():
            got = split_line(line)
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
    for n, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
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
        self.tpf = tpr / (row_sec * region_hz)            # ticks in one frame (the table-driven instruments step once a frame)
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
class _Plan:
    """The envelopes of one instrument that are played a frame at a time (None where the instrument has none)."""

    def __init__(self):
        self.vol = self.arp = self.pitch = self.duty = None     # (values, length, loop[, release | relative])


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
        unplayable = [e for e in expansions if e not in CARRIED_CHIPS]
        if unplayable:
            report.drop("expansion audio (%s): its channels and instruments are not imported" % ", ".join(unplayable))
        carried = [e for e in expansions if e in CARRIED_CHIPS]
        if carried:
            report.note("expansion audio (%s): its pulses, saw and wavetables are carried to voices V5 and up (chipsynth songs have 16 voices at most)"
                        % ", ".join(carried))
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
    extra = _expansion_channels(prj, fs, report)
    song = sg.Song(title=name, author=prj.attrs.get("Author", ""), bpm=tm.bpm, voices=4 + len(extra), dacs=1)
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
            if any(ch.patterns.values()) and not _expansion_wanted(prj, ch):
                report.drop("channel %s (expansion audio)" % ch.type)
            continue
        idx = BASE[ch.type]
        events = ctx.channel(ch, idx)
        track = "D1" if idx == 4 else "V%d" % (idx + 1)
        song.tracks[track] = ctx.finish(events, end, idx == 0, tm)
    for n, ch in enumerate(extra):                                  # expansion channels, on the voices after the four
        events = ctx.channel(ch, 5 + n)
        song.tracks["V%d" % (5 + n)] = [sg.Comment(text=ch.type)] + ctx.finish(events, end, False, tm)
    ctx.report_simulation()
    song.__post_init__()
    return Imported(name, song, ctx.samples)


def _expansion_wanted(prj, ch):
    """True when the channel is one chipsynth can play (so it is imported, or reported for want of a voice) rather than skipped."""
    declared = {e.strip() for e in prj.attrs.get("Expansions", "").split(",") if e.strip()}
    return ch.type in EXP_WAVE and chip_of(ch.type) in declared


def _expansion_channels(prj, fs, report):
    """The expansion channels with notes that get a voice, in the order of EXP_ORDER (the others are reported)."""
    found = {}
    for ch in fs.channels:
        if _expansion_wanted(prj, ch) and any(ch.patterns.values()):
            found.setdefault(ch.type, ch)
    ordered = [found[t] for t in EXP_ORDER if t in found]
    room = MAX_VOICES - 4
    for ch in ordered[room:]:
        report.drop("channel %s: no voice left for it (a chipsynth song has %d voices at most; V1-V4 are the NES's own)" % (ch.type, MAX_VOICES))
    return ordered[:room]


class _Ctx:
    def __init__(self, prj, song, tm, report, pal, hz):
        self.prj, self.song, self.tm, self.report, self.pal, self.hz = prj, song, tm, report, pal, hz
        self.inst_names, self.inst_used = {}, set()
        self.fit_cache = {}
        self.samples = {}
        self.sample_names = {}
        self.sample_used = set()
        self.plans = {}               # (instrument, channel type, note duty, note arpeggio) -> _Plan or None
        self.sim_names = {}
        self.sim_stats = {}           # instrument label -> [sum of squared pitch rounding, frames, worst]
        self.tick_warned = False

    # ---- instruments
    def instrument(self, fname, chan_type, duty):
        """Select-name of FamiStudio instrument `fname` on a channel of `chan_type`, with note-level `duty` (or None)."""
        exp = chan_type in EXP_WAVE
        group = EXP_GROUP.get(chan_type, chan_type)
        key = (fname, group, duty)
        if key in self.inst_names:
            return self.inst_names[key]
        src = self.prj.instruments.get(fname)
        inst = Instrument()
        inst.wave = self._base_wave(chan_type)
        label = fname or "default"
        if src is not None:
            envs = src["envelopes"]
            mine = src["attrs"].get("Expansion", "")
            if mine and not (exp and mine == chip_of(chan_type)):
                self.report.drop("instrument '%s' uses expansion audio: plain defaults used" % label)
            vol = envs.get("Volume")
            if vol is not None and chan_type != "Triangle":
                inst = self._volume(inst, label, vol)
            elif vol is not None:
                self.report.note("instrument '%s': the triangle has no volume, so its volume envelope is ignored" % label)
            self._duty(inst, label, envs.get("DutyCycle"), duty, chan_type)
            self._arp(inst, label, envs.get("Arpeggio"))
            self._pitch(inst, label, envs.get("Pitch"), chan_type)
            if EXP_WAVE.get(chan_type) == "table":
                inst.wave, inst.pw = self._table(label, src, chan_type)
            self._chip_notes(label, src, chan_type)
        else:
            if duty is not None:
                self._duty(inst, label, None, duty, chan_type)
            if EXP_WAVE.get(chan_type) == "table":
                inst.wave, inst.pw = self._table(label, None, chan_type)
        name = _ident(label + self._suffix(chan_type) + ("" if duty is None else "_d%d" % duty), self.inst_used)
        self.song.instruments[name] = inst
        self.inst_names[key] = name
        return name

    @staticmethod
    def _base_wave(chan_type):
        if chan_type in EXP_WAVE:
            return "pulse" if EXP_WAVE[chan_type] == "table" else EXP_WAVE[chan_type]
        return {"Square1": "pulse", "Square2": "pulse", "Triangle": "tri", "Noise": "noise"}[chan_type]

    @staticmethod
    def _suffix(chan_type):
        if chan_type in ("Triangle", "Noise"):
            return "_" + chan_type.lower()
        return "_" + EXP_GROUP[chan_type] if chan_type in EXP_WAVE else ""

    def _table(self, label, src, chan_type):
        """(wave, pulse width) closest to a wavetable instrument's waveform; the choice is always reported as approximate."""
        fds = chan_type == "FDS"
        chip = "FDS" if fds else "N163"
        envs = src["envelopes"] if src else {}
        env = envs.get("FDSWave" if fds else "N163Wave")
        attrs = src["attrs"] if src else {}
        vals = _values(env.get("Values", ""))[: _int(env, "Length", 10 ** 6)] if env else []
        if vals:
            size = 64 if fds else max(1, _int(attrs, "N163WaveSize", len(vals)))
            count = max(1, len(vals) // size)
            wave, pw, err = fit_wave(vals[:size])
            how = {"pulse": "a %d%% pulse" % pw}.get(wave, wave)
            self.report.approx("instrument '%s' (%s wavetable): played as %s, the nearest wave chipsynth has (RMS shape error %.2f)" % (label, chip, how, err))
            if count > 1:
                self.report.approx("instrument '%s' (%s): its %d waves in sequence are not played; the first is used" % (label, chip, count))
        else:
            preset = {"Sine": ("sine", 50), "Triangle": ("tri", 50), "Sawtooth": ("saw", 50), "Square50%": ("pulse", 50), "Square25%": ("pulse", 25)}
            wave, pw = preset.get(attrs.get("FdsWavePreset" if fds else "N163WavePreset", ""), ("pulse", 50))
            self.report.approx("instrument '%s' (%s wavetable): no waveform in the file; played as %s" % (label, chip, wave))
        return wave, pw

    def _chip_notes(self, label, src, chan_type):
        """What a chip's instrument says that chipsynth cannot play, once per instrument."""
        a, envs = src["attrs"], src["envelopes"]
        if chan_type == "FDS" and (_int(a, "FdsModDepth", 0) and _int(a, "FdsModSpeed", 0)):
            self.report.approx("instrument '%s': its FDS frequency modulation (depth %s, speed %s) is not played" % (label, a.get("FdsModDepth"), a.get("FdsModSpeed")))
        if chan_type == "VRC6Saw" and a.get("Vrc6SawMasterVolume", "Half") != "Full":
            self.report.note("instrument '%s': the VRC6 saw master volume is not applied" % label)
        if chan_type.startswith(("S5B", "EPSM")):
            mix = _values(envs.get("MixerSettings", {}).get("Values", ""))
            if any(m != 2 for m in mix) or "NoiseFreq" in envs or a.get("S5BEnvelopeShape", a.get("EPSMSquareEnvelopeShape", "0")) not in ("0", ""):
                self.report.approx("instrument '%s': the noise, hardware envelope and mixer of its %s square are not played; the tone is" % (label, "S5B" if chan_type.startswith("S5B") else "EPSM"))

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

    @staticmethod
    def _duty_table(chan_type):
        """(percent of each duty value, the duty used when there is none): VRC6 has eight duties, the NES and MMC5 four."""
        return (VRC6_PW, 7) if chan_type.startswith("VRC6") else (DUTY_PW, 2)

    def _duty(self, inst, label, env, note_duty, chan_type):
        if chan_type == "Triangle" or chan_type == "VRC6Saw" or EXP_WAVE.get(chan_type) == "table" or chan_type.startswith(("S5B", "EPSM")):
            return
        table, default = self._duty_table(chan_type)
        vals = _values(env.get("Values", "")) if env else []
        first = note_duty if note_duty is not None else (vals[0] if vals else default if chan_type != "Noise" else 0)
        if chan_type == "Noise":
            inst.nmode = "short" if first == 1 else "long"
            if len(set(vals)) > 1:
                self.report.approx("instrument '%s': its noise mode changes during the note; the first is kept" % label)
            return
        inst.pw = table[first % len(table)] if chan_type.startswith("VRC6") else table[first & 3]
        used = set(vals)
        if note_duty is None and len(used) > 1:
            cycle = self._duty_sweep(vals, _int(env, "Loop", -1))
            if cycle:
                lo, hi = sorted(set(cycle))
                inst.pws_from, inst.pws_to = table[lo % len(table)], table[hi % len(table)]
                inst.pws_rate = max(1, min(15, round(self.hz / len(cycle) / 0.5)))
                self.report.approx("instrument '%s': its duty envelope is a pulse-width sweep" % label)
            else:
                self.report.approx("instrument '%s': its duty envelope changes during the note; the first duty is kept" % label)

    @staticmethod
    def _duty_sweep(vals, loop):
        """The repeating cycle of a duty envelope that is a pulse-width sweep (two duties, one of them held for one frame or half
        the cycle), or None."""
        body = vals[loop:] if loop >= 0 else []
        cycle = _periodic(body)
        if cycle and len(set(cycle)) == 2 and cycle.count(cycle[0]) in (1, len(cycle) - 1, len(cycle) // 2) and len(cycle) >= 2:
            return cycle
        return None

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

    # ---- pitch envelope as vibrato
    def _vibrato(self, env, chan_type):
        """(rate, depth, error) when a pitch envelope is a small repeating swing chipsynth's vibrato can play, else None.
        The swing is measured on C4: a unit of FamiStudio's pitch is a step of the period register, so it is wider on a high note."""
        if env is None or chan_type not in PITCH_K or env.get("Relative", "false").lower() == "true":
            return None
        vals = _values(env.get("Values", ""))[: _int(env, "Length", 10 ** 6)]
        loop = _int(env, "Loop", -1)
        if loop < 0 or loop >= len(vals):
            return None
        offs = [pitch_semitones(v, 48, PITCH_K[chan_type]) for v in vals]
        if max(abs(o) for o in offs) >= 1.0:
            return None
        got = fit_vibrato(offs[loop:], self.hz)
        if got is None or got[2] > 0.1:
            return None
        return got

    def _pitch(self, inst, label, env, chan_type):
        """The fit for a pitch envelope on an instrument that is played as one note: vibrato when it is one, else left out."""
        if env is None:
            return
        got = self._vibrato(env, chan_type)
        if got is None:
            self.report.drop("instrument '%s': its pitch envelope" % label)
            return
        inst.vib_rate, inst.vib_depth = got[0], got[1]
        self.report.approx("instrument '%s': its pitch envelope is played as vibrato (rate %d, depth %d; RMS error %.2f semitone, measured on C4)" % (label, got[0], got[1], got[2]))

    # ---- table-driven instruments, played a frame at a time
    def plan(self, fname, chan_type, duty, arp_name):
        """What to simulate frame by frame for a note of instrument `fname` on `chan_type`, or None when one chipsynth note with
        the fitted instrument is good enough (every envelope fits, with the error under GOOD)."""
        key = (fname, EXP_GROUP.get(chan_type, chan_type), duty, arp_name)
        if key not in self.plans:
            self.plans[key] = self._make_plan(fname, chan_type, duty, arp_name)
        return self.plans[key]

    def _make_plan(self, fname, chan_type, duty, arp_name):
        label = fname or "default"
        src = self.prj.instruments.get(fname)
        envs = src["envelopes"] if src else {}
        reasons, plan = [], _Plan()
        noise = chan_type == "Noise"
        env = envs.get("Volume")
        if env is not None and chan_type != "Triangle":
            vals = _values(env.get("Values", ""))
            length = _int(env, "Length", len(vals))
            loop, rel = _int(env, "Loop", -1), _int(env, "Release", -1)
            if vals and max(vals[:length] or [0]) > 0:
                plan.vol = (vals, length, loop, rel)
                fkey = (tuple(vals), length, loop, rel)
                if fkey not in self.fit_cache:
                    self.fit_cache[fkey] = fit_volume(vals, loop, rel, length, self.hz)
                if self.fit_cache[fkey][1] > GOOD:
                    reasons.append("volume envelope")
        if not noise:
            if arp_name:
                arp = self.prj.arpeggios.get(arp_name)
                if arp is None:
                    self.report.drop("the arpeggio '%s' on a note is not in the file" % arp_name)
                else:
                    vals = _values(arp.get("Values", ""))
                    plan.arp = (vals, _int(arp, "Length", len(vals)), _int(arp, "Loop", -1))
                    if any(vals[: plan.arp[1]]):
                        reasons.append("arpeggio '%s'" % arp_name)
            else:
                env = envs.get("Arpeggio")
                if env is not None:
                    vals = _values(env.get("Values", ""))[: _int(env, "Length", 10 ** 6)]
                    got, _, exact = fit_arpeggio(vals, _int(env, "Loop", -1), 60.0 / self.tm.bpm * self.hz)
                    if got:
                        plan.arp = (vals, len(vals), _int(env, "Loop", -1))     # used when the instrument is simulated for another reason too
                        if not exact:
                            reasons.append("arpeggio envelope")
            env = envs.get("Pitch")
            if env is not None and chan_type in PITCH_K and self._vibrato(env, chan_type) is None:
                vals = _values(env.get("Values", ""))
                plan.pitch = (vals, _int(env, "Length", len(vals)), _int(env, "Loop", -1), env.get("Relative", "false").lower() == "true")
                reasons.append("pitch envelope")
        env = envs.get("DutyCycle")
        if env is not None and duty is None and chan_type not in ("Triangle", "VRC6Saw") and EXP_WAVE.get(chan_type) != "table" and not chan_type.startswith(("S5B", "EPSM")):
            vals = _values(env.get("Values", ""))
            plan.duty = (vals, _int(env, "Length", len(vals)), _int(env, "Loop", -1))
            if len(set(vals)) > 1 and not (not noise and self._duty_sweep(vals, plan.duty[2])):
                reasons.append("noise mode envelope" if noise else "duty envelope")
        if not reasons:
            return None
        if self.tm.tpf < 1.0:
            if not self.tick_warned:
                self.report.approx("this song's frames are shorter than one tick (%.2f ticks), so table-driven instruments cannot be played frame by frame; they are fitted instead" % self.tm.tpf)
                self.tick_warned = True
            return None
        if envs.get("Pitch") is not None and chan_type not in PITCH_K:
            self.report.drop("instrument '%s': its pitch envelope" % label)
        elif plan.pitch is None and envs.get("Pitch") is not None:
            self.report.drop("instrument '%s': its pitch envelope is a small swing; it is not played when the note is stepped frame by frame" % label)
        if envs.get("Volume") is not None and chan_type == "Triangle":
            self.report.note("instrument '%s': the triangle has no volume, so its volume envelope is ignored" % label)
        if arp_name and noise:
            self.report.drop("the arpeggio '%s' on a noise note" % arp_name)
        if envs.get("Volume") is not None and envs["Volume"].get("Relative", "false").lower() == "true":
            self.report.approx("instrument '%s': a relative volume envelope is treated as absolute" % label)
        self.report.approx("instrument '%s' on %s: played frame by frame as short notes, because its %s does not fit one chipsynth instrument" % (label, chan_type, " and ".join(reasons)))
        return plan

    def sim_instrument(self, fname, chan_type, duty_idx):
        """Select-name of the flat instrument (full sustain, no fade) whose pulse width or noise mode is duty `duty_idx`."""
        group = EXP_GROUP.get(chan_type, chan_type)
        key = (fname, group, duty_idx)
        if key in self.sim_names:
            return self.sim_names[key]
        label = fname or "default"
        inst = Instrument(wave=self._base_wave(chan_type), a=0, d=0, s=15, r=0, vol=15)
        if EXP_WAVE.get(chan_type) == "table":
            inst.wave, inst.pw = self._table(label, self.prj.instruments.get(fname), chan_type)
        elif duty_idx is not None:
            if chan_type == "Noise":
                inst.nmode = "short" if duty_idx == 1 else "long"
            else:
                table, _ = self._duty_table(chan_type)
                inst.pw = table[duty_idx % len(table)]
        name = _ident(label + self._suffix(chan_type) + "_sim" + ("" if duty_idx is None else "_d%d" % duty_idx), self.inst_used)
        self.song.instruments[name] = inst
        self.sim_names[key] = name
        return name

    def simulate(self, plan, chan_type, idx, pitch, tick, end, limit, vol_cur, note_duty, fname):
        """The events of one note played a frame at a time: a short note wherever the pitch, volume or duty changes.  Frame f
        starts `round(f * ticks a frame)` ticks into the note.  A volume envelope with a release point goes on past the end of
        the note, up to `limit`, as FamiStudio plays it."""
        tpf = self.tm.tpf

        def at(f):
            return int(round(f * tpf))
        gate = end - tick
        nfg = 0
        while at(nfg) < gate:
            nfg += 1
        nf, cut, released = nfg, gate, None
        if plan.vol and plan.vol[3] >= 0 and chan_type != "Triangle":
            released = nfg
            room = limit - tick
            while nf < nfg + TAIL_FRAMES and at(nf) < room:
                nf += 1
        if plan.vol and chan_type != "Triangle":
            vals, length, loop, rel = plan.vol
            venv = resolve_note(vals, loop, rel, length, nf, released if rel >= 0 else None)
        else:
            venv = [15] * nf
        if released is not None:                                    # the tail ends where the envelope falls silent
            while nf > nfg and venv[nf - 1] == 0:
                nf -= 1
            venv = venv[:nf]
            cut = max(gate, min(limit - tick, at(nf)))
        aenv = resolve_note(plan.arp[0], plan.arp[2], -1, plan.arp[1], nf) if plan.arp else [0] * nf
        penv = None
        if plan.pitch:
            vals, length, loop, relative = plan.pitch
            penv = accumulate(vals, loop, length, nf) if relative else resolve_note(vals, loop, -1, length, nf)
        if chan_type in ("Triangle", "VRC6Saw") or EXP_WAVE.get(chan_type) == "table" or chan_type.startswith(("S5B", "EPSM")):
            denv = [None] * nf
        else:
            default = 0 if chan_type == "Noise" else self._duty_table(chan_type)[1]
            if note_duty is not None:
                denv = [note_duty] * nf
            elif plan.duty and plan.duty[0]:
                denv = resolve_note(plan.duty[0], plan.duty[2], -1, plan.duty[1], nf)
            else:
                denv = [default] * nf
        stat = self.sim_stats.setdefault(fname or "default", [0.0, 0, 0.0])
        runs = []                                                   # [first frame, state] for each stretch of frames that sound the same
        for f in range(nf):
            base = pitch + aenv[f]
            exact = base + (pitch_semitones(penv[f], base, PITCH_K[chan_type]) if penv else 0.0)
            p = max(0, min(107, int(round(exact))))
            if penv:
                stat[0] += (p - exact) ** 2
                stat[1] += 1
                stat[2] = max(stat[2], abs(p - exact))
            vol = None if idx == 2 else int(round(venv[f] * vol_cur / 15.0))
            state = (p, vol, denv[f])
            if not runs or runs[-1][1] != state:
                runs.append([f, state])
        out = []
        for n, (f0, (p, vol, d)) in enumerate(runs):
            if vol == 0:
                continue
            f1 = runs[n + 1][0] if n + 1 < len(runs) else nf
            start, stop = at(f0), min(at(f1), cut)
            if stop <= start:
                continue
            out.append((tick + start, "note", p, stop - start, self.sim_instrument(fname, chan_type, d), vol))
        return out

    def report_simulation(self):
        for label, (sq, n, worst) in self.sim_stats.items():
            if n and math.sqrt(sq / n) > 0.05:
                self.report.approx("instrument '%s': its pitch envelope moves in steps smaller than a semitone, which chipsynth notes cannot, so each frame is rounded to a whole semitone (RMS error %.2f, worst %.2f semitone)" % (label, math.sqrt(sq / n), worst))
        if self.sim_names:
            self.report.approx("frame-by-frame notes start on ticks (a frame is %.2f ticks here), so each step is within half a tick of its frame, and the volume of a step is the frame's volume times the note's volume, rounded to 0-15" % self.tm.tpf)

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
            for k in ("Speed", "NoteDelay", "CutDelay", "VibratoSpeed", "FinePitch", "SlideTarget", "PhaseReset", "DeltaCounter", "VolumeSlideTarget",
                      "FdsModSpeed", "FdsModDepth", "EnvelopePeriod"):
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
            arp_name = a.get("Arpeggio")
            plan = self.plan(inst_cur or "", ch.type, duty, arp_name)
            if plan is not None:                                            # a table-driven instrument: a short note a frame
                limit = min(nxt if nxt is not None else pend, pend)
                events.extend(self.simulate(plan, ch.type, idx, pitch, tick, end, limit, vol_cur, duty, inst_cur or ""))
                continue
            if arp_name and any(_values(self.prj.arpeggios.get(arp_name, {}).get("Values", ""))):
                self.report.drop("the arpeggio '%s' on %s notes (it is played only frame by frame)" % (arp_name, ch.type))
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
