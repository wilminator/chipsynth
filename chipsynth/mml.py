# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tune language (see DESIGN-MUSIC.md): text MML -> a `Tune` of notes per voice.

This is the core subset: header (title, author, comment, tempo, time, tune, once), [instr] and [pat]
sections with the built-in instrument library as fallback, four voice lines (V1: ... V4:, P1-P4 also
accepted), notes with persistent octave prefixes, rests, ties, dots and triplets, volume, tempo,
instrument and pan changes, repeat blocks, named patterns, bar lines (checked) and the loop marker.
Key / degree mode: `key c major` then `1 3 5 8:4` (see DESIGN-MUSIC.md).  Expression: vib r,d / slide n / arp o1,o2.. / arprate n / pws from,to,rate, as instrument keys, [fx] sets or voice commands.
Not yet: echo, filter, DAC samples
(D1: / D2: lines are skipped with a warning).

    tune = parse(text)          # raises MMLError("line 3, col 7: ...") on a mistake
    tune.warnings               # things that parse but look wrong
"""
import bisect
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .synth import Instrument, RATE, WAVES

TICKS_WHOLE = 384                 # a quarter note is 96 ticks
EVENT_CAP = 200000                # note on + note off events per tune
NOTE_CAP = EVENT_CAP // 2
LETTER = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
DURATIONS = (1, 2, 4, 8, 16, 32, 64)

BUILTIN_INSTR = {
    "lead": Instrument(wave="pulse", pw=25, a=0, d=8, s=10, r=4, vol=13),
    "lead2": Instrument(wave="pulse", pw=50, a=0, d=8, s=11, r=4, vol=12),
    "bright": Instrument(wave="pulse", pw=12, a=0, d=7, s=9, r=3, vol=12),
    "organ": Instrument(wave="pulse", pw=50, a=1, d=6, s=15, r=3, vol=11),
    "arp": Instrument(wave="pulse", pw=12, a=0, d=5, s=4, r=3, vol=11),
    "bass": Instrument(wave="tri", pw=50, a=0, d=7, s=13, r=3, vol=15),
    "sawbass": Instrument(wave="saw", pw=50, a=0, d=6, s=8, r=3, vol=13),
    "pad": Instrument(wave="tri", pw=50, a=9, d=10, s=12, r=9, vol=12),
    "flute": Instrument(wave="sine", pw=50, a=3, d=8, s=12, r=5, vol=13),
    "bell": Instrument(wave="sine", pw=50, a=0, d=9, s=0, r=8, vol=13),
    "buzz": Instrument(wave="saw", pw=50, a=0, d=8, s=10, r=4, vol=11),
    "kick": Instrument(wave="noise", a=0, d=3, s=0, r=2, vol=15, send=0),
    "snare": Instrument(wave="noise", a=0, d=4, s=0, r=3, vol=13, send=0),
    "hat": Instrument(wave="noise", nmode="short", a=0, d=1, s=0, r=1, vol=9, send=0),
    "crash": Instrument(wave="noise", a=0, d=8, s=0, r=7, vol=11, send=0),
}
VOICE_LABEL = re.compile(r"^\s*([vVpP][1-4]|[dD][12])\s*:(.*)$")
SECTION = re.compile(r"^\s*\[([A-Za-z]+)\]\s*$")
WORDS = re.compile(r"(loop|once|wave|pw|adsr|pan|nmode|instr|vib|slide|arprate|arp|pws|fx|filter|fsweep|send)(?![A-Za-z])", re.I)
SECTIONS = ("instr", "pat", "fx", "sample", "key")
SKIPPED_SECTIONS = ()
SCALES = {                         # semitones above the tonic
    "major": (0, 2, 4, 5, 7, 9, 11), "ionian": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10), "aeolian": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10), "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11), "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
    "pentatonic": (0, 2, 4, 7, 9), "minor-pentatonic": (0, 3, 5, 7, 10),
}


class MMLError(Exception):
    def __init__(self, msg, line=0, col=0):
        self.msg, self.line, self.col = msg, line, col
        super().__init__(self.__str__())

    def __str__(self):
        if self.line:
            return "line %d, col %d: %s" % (self.line, self.col, self.msg)
        return self.msg


@dataclass
class Note:
    start: int                    # ticks
    dur: int
    pitch: int                    # semitones from C0 (A4 = 57)
    inst: Instrument
    vol: int = 15
    pan: object = None            # None = the instrument's pan
    slide: int = 0                # glide into this note, in 1/48 of a quarter note


@dataclass
class Tune:
    title: str = ""
    author: str = ""
    comment: str = ""
    bpm: int = 120
    tempo_events: list = field(default_factory=list)     # [(tick, bpm)], sorted
    time: tuple = (4, 4)
    detune: int = 0                                      # cents
    echo: tuple = None                                   # (seconds, feedback 0-1, mix 0-1, wide) or None
    reverb: int = 0                                      # 0 = off, 1-15
    voices: list = field(default_factory=lambda: [[], [], [], []])
    length: int = 0                                      # ticks
    loop_tick: int = 0
    once: bool = False
    warnings: list = field(default_factory=list)
    instruments: dict = field(default_factory=dict)
    dac: list = field(default_factory=lambda: [[], []])   # per DAC: [(tick, 'play'|'stop'|'level', a, b)]

    @property
    def bar_ticks(self):
        return TICKS_WHOLE * self.time[0] // self.time[1]

    @property
    def bars(self):
        return -(-self.length // self.bar_ticks) if self.length else 0

    @property
    def events(self):
        return 2 * sum(len(v) for v in self.voices)

    def _breaks(self):
        """[(tick, sample, samples per tick)] at every tempo change."""
        if getattr(self, "_cache", None) is not None:
            return self._cache
        tempos = {0: self.bpm}
        for t, b in self.tempo_events:
            tempos[t] = b
        out, sample = [], 0.0
        ticks = sorted(tempos)
        for k, t in enumerate(ticks):
            spt = RATE * 60.0 / (tempos[t] * 96)
            out.append((t, sample, spt))
            if k + 1 < len(ticks):
                sample += (ticks[k + 1] - t) * spt
        self._cache = out
        self._keys = [b[0] for b in out]
        return out

    def tick_to_sample(self, tick):
        br = self._breaks()
        i = bisect.bisect_right(self._keys, tick) - 1
        t, s, spt = br[i]
        return int(round(s + (tick - t) * spt))

    def sample_to_tick(self, sample):
        """The tick playing at `sample` (a float is fine); the inverse of tick_to_sample."""
        br = self._breaks()
        i = bisect.bisect_right([b[1] for b in br], sample) - 1
        t, s, spt = br[max(0, i)]
        return t + (sample - s) / spt

    @property
    def seconds(self):
        return self.tick_to_sample(self.length) / RATE

    def bpm_at(self, tick):
        br = self._breaks()
        i = bisect.bisect_right(self._keys, tick) - 1
        return round(RATE * 60.0 / (br[i][2] * 96), 3)


# ------------------------------------------------------------------ library
def builtin_library():
    return {"instr": dict(BUILTIN_INSTR), "pat": {}, "fx": {}, "key": {}, "sample": {}, "_sampledir": None}


def load_library(libdir):
    """music/lib/*.mml files (header-style sections) laid over the built-in instruments."""
    lib = builtin_library()
    d = Path(libdir)
    lib["_sampledir"] = d.parent / "samples"
    if d.is_dir():
        for p in sorted(d.glob("*.mml")):
            try:
                t = _Parser(p.read_text(errors="replace"), lib, header_only=True).run()
            except MMLError:
                continue
            lib["instr"].update(t.instruments)
            lib["pat"].update(getattr(t, "_patterns", {}))
            lib["fx"].update(getattr(t, "_fxsets", {}))
            lib["key"].update(getattr(t, "_keys", {}))
            lib["sample"].update(getattr(t, "_sampledefs", {}))
    return lib


# ------------------------------------------------------------------ parser
def _blank_comments(text):
    """/* ... */ blocks become spaces (newlines kept so line numbers hold)."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    return re.sub(r"/\*.*?\*/", blank, text, flags=re.S)


class _Voice:
    def __init__(self, index):
        self.index = index
        self.pos = 0
        self.octave = 4
        self.boundary = 0
        self.deflen = TICKS_WHOLE // 8
        self.vol = 15
        self.inst = replace(BUILTIN_INSTR["lead"])
        self.pan = None
        self.tie = False
        self.last = None              # the last Note, for ties
        self.slide_next = None        # `slide n`: applies to the next note only
        self.key = None               # (tonic semitone, scale offsets) while `key` is on: digits are degrees
        self.barstart = 0
        self.bar = 1
        self.dac = None               # 0 or 1 for a DAC line (D1: / D2:)


class _Parser:
    def __init__(self, text, lib=None, header_only=False):
        self.text = _blank_comments(text)
        self.lib = lib or builtin_library()
        self.header_only = header_only
        self.tune = Tune()
        self.patterns = {}
        self.fxsets = {}
        self.keys = {}
        self.voices = [_Voice(i) for i in range(4)]
        self.dvoices = [_Voice(4), _Voice(5)]
        for k, dv in enumerate(self.dvoices):
            dv.dac = k
        self.sampledefs = {}          # [sample] name -> (file name, rate override or None)
        self._samples = {}
        self.tempo = {}
        self.loop = None
        self.notes = 0

    # ---- helpers
    def warn(self, msg, line=0):
        if len(self.tune.warnings) < 50:
            self.tune.warnings.append(("line %d: " % line if line else "") + msg)

    def err(self, msg, line, col):
        raise MMLError(msg, line, col)

    def find_instr(self, name):
        if name in self.tune.instruments:
            return self.tune.instruments[name]
        if name in self.lib["instr"]:
            return self.lib["instr"][name]
        return None

    def find_scale(self, name):
        name = name.lower()
        if name in self.keys:
            return self.keys[name]
        if name in SCALES:
            return SCALES[name]
        return self.lib.get("key", {}).get(name)

    def define_key(self, n, col, s):
        """[key] name = 0 2 3 5 7 8 10   (semitones above the tonic, ascending, 0 first, up to 12 notes)"""
        m = re.match(r"([A-Za-z][A-Za-z0-9_-]*)\s*=?\s*([0-9 ,]+)$", s)
        if not m:
            self.err("a key line is: name = 0 2 4 5 7 9 11", n, col)
        try:
            steps = tuple(int(x) for x in m.group(2).replace(",", " ").split())
        except ValueError:
            self.err("a key line is: name = 0 2 4 5 7 9 11", n, col)
        if not 2 <= len(steps) <= 12 or steps[0] != 0 or list(steps) != sorted(set(steps)) or steps[-1] > 11:
            self.err("a scale starts at 0, goes up, stays below 12 and has 2 to 12 notes", n, col)
        self.keys[m.group(1).lower()] = steps

    def find_fx(self, name):
        if name in self.fxsets:
            return self.fxsets[name]
        return self.lib.get("fx", {}).get(name)

    def define_fx(self, n, col, s):
        parts = s.split()
        if len(parts) < 2 or "=" in parts[0]:
            self.err("an fx line is a name then settings: wobble vib=5,3 pws=25,75,6", n, col)
        pairs = []
        probe = Instrument()
        for kv in parts[1:]:
            k, eq, v = kv.partition("=")
            if not eq:
                self.err("'%s' should be key=value" % kv, n, col)
            self.inst_key(probe, k.lower(), v, n, col)
            pairs.append((k.lower(), v))
        self.fxsets[parts[0]] = pairs

    def apply_fx(self, v, name, line, col):
        pairs = self.find_fx(name)
        if pairs is None:
            self.err("no fx set '%s' (define it under [fx])" % name, line, col)
        inst = replace(v.inst)
        for k, val in pairs:
            self.inst_key(inst, k, val, line, col)
        v.inst = inst

    def define_sample(self, n, col, s):
        """[sample] name = file [rate=N]   (the file is NAME.dpcm in music/samples; name alone means the same file)."""
        m = re.match(r"([A-Za-z_]+)\s*(?:=\s*)?(.*)$", s)
        if not m:
            self.err("a sample line is: name = file rate=12  (names are letters and _)", n, col)
        name, rest = m.group(1), m.group(2).split()
        file, rate = name, None
        for tok in rest:
            k, eq, v = tok.partition("=")
            if eq and k.lower() == "rate":
                rate = self.num_in(v, 0, 15, "sample rate", n, col)
            elif not eq and tok is rest[0]:
                file = tok
            else:
                self.err("'%s': a sample line is name = file rate=12" % tok, n, col)
        self.sampledefs[name] = (file, rate)

    def find_sample(self, name):
        """A Sample by name: this tune's [sample] section, then the library's, then NAME.dpcm in the sample folder."""
        if name in self._samples:
            return self._samples[name]
        from . import dpcm
        mem = self.lib.get("_mem", {})                # samples held in memory (the import tool's preview)
        if name in mem:
            self._samples[name] = mem[name]
            return mem[name]
        file, rate = self.sampledefs.get(name) or self.lib.get("sample", {}).get(name) or (name, None)
        folder = self.lib.get("_sampledir")
        smp = None
        if folder is not None and re.fullmatch(r"[A-Za-z_]+", file):
            path = Path(folder) / (file + ".dpcm")
            if path.is_file():
                try:
                    smp = dpcm.load(path)
                except dpcm.DpcmError:
                    smp = None
        if smp is None and re.fullmatch(r"kit_[A-Za-z_]+", file):
            from . import kit                             # the generator's built-in drums, bass and voice
            smp = kit.builtin(file)
        if smp is not None and rate is not None:
            smp = replace(smp, rate=rate)
        self._samples[name] = smp
        return smp

    def find_pat(self, name):
        if name in self.patterns:
            return self.patterns[name]
        return self.lib["pat"].get(name)

    # ---- the whole file
    def run(self):
        lines = self.text.split("\n")
        section = None
        voice_lines = []
        for n, raw in enumerate(lines, 1):
            line = raw.split("//", 1)[0]
            if not line.strip():
                continue
            m = SECTION.match(line)
            if m:
                section = m.group(1).lower()
                if section not in SECTIONS:
                    self.err("unknown section [%s] (instr, pat, fx, sample, key)" % m.group(1), n, line.index("[") + 1)
                if section in SKIPPED_SECTIONS:
                    self.warn("section [%s] is not supported yet and is ignored" % section, n)
                continue
            vm = VOICE_LABEL.match(line)
            if vm:
                voice_lines.append((n, line, vm))
                section = None
                continue
            self.header_line(n, line, section)
        self.tune._patterns = dict(self.patterns)
        self.tune._fxsets = dict(self.fxsets)
        self.tune._keys = dict(self.keys)
        self.tune._sampledefs = dict(self.sampledefs)
        if self.header_only:
            return self.tune
        for n, line, vm in voice_lines:
            self.voice_line(n, line, vm)
        return self.finish()

    # ---- header
    def header_line(self, n, line, section):
        col = len(line) - len(line.lstrip()) + 1
        s = line.strip()
        word, _, rest = s.partition(" ")
        rest = rest.strip()
        if section in SKIPPED_SECTIONS:
            return
        if section == "instr":
            return self.define_instr(n, col, s)
        if section == "pat":
            return self.define_pat(n, col, s)
        if section == "fx":
            return self.define_fx(n, col, s)
        if section == "key":
            return self.define_key(n, col, s)
        if section == "sample":
            return self.define_sample(n, col, s)
        w = word.lower()
        t = self.tune
        if w in ("title", "author", "comment"):
            setattr(t, w, rest)
        elif w == "tempo":
            t.bpm = self.number(rest, 30, 300, "tempo", n, col)
        elif w == "time":
            m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", rest)
            if not m or int(m.group(2)) not in (1, 2, 4, 8, 16, 32) or not 1 <= int(m.group(1)) <= 32:
                self.err("time must look like 4/4 or 6/8", n, col)
            t.time = (int(m.group(1)), int(m.group(2)))
        elif w == "tune":
            t.detune = self.number(rest.lstrip("+") if rest.lstrip("+").lstrip("-").isdigit() else rest, -1200, 1200, "tune", n, col)
        elif w == "once":
            t.once = True
        elif w == "echo":
            t.echo = self.parse_echo(rest, n, col)
        elif w == "reverb":
            t.reverb = 0 if rest.lower() == "off" else self.number(rest, 0, 15, "reverb", n, col)
        elif w == "import":
            self.do_import(n, col, rest)
        elif w == "pat":
            self.define_pat(n, col, rest)
        elif w == "key":
            self.define_key(n, col, rest)
        elif w == "fx":
            if "=" in rest:
                self.define_fx(n, col, rest)
            else:
                self.do_import(n, col, "fx." + rest)
        elif w == "instr":
            if "=" in rest or len(rest.split()) > 1:
                self.define_instr(n, col, rest)
            else:
                self.do_import(n, col, "instr." + rest)
        else:
            self.err("don't know '%s' here (a voice line starts V1: .. V4:)" % word, n, col)

    def number(self, s, lo, hi, what, n, col):
        try:
            v = int(s.strip())
        except ValueError:
            self.err("%s needs a number" % what, n, col)
        if not lo <= v <= hi:
            self.err("%s must be %d to %d" % (what, lo, hi), n, col)
        return v

    def define_instr(self, n, col, s):
        parts = s.split()
        if not parts or "=" in parts[0]:
            self.err("an instrument line starts with its name: flute wave=tri a=3 d=8 s=12 r=5", n, col)
        name, inst = parts[0], Instrument()
        if len(parts) == 1:
            self.err("instrument '%s' has no settings" % name, n, col)
        for kv in parts[1:]:
            k, eq, v = kv.partition("=")
            k = k.lower()
            if not eq:
                self.err("'%s' should be key=value" % kv, n, col)
            self.inst_key(inst, k, v, n, col)
        self.tune.instruments[name] = inst

    def inst_key(self, inst, k, v, n, col):
        """Apply one key=value to an Instrument (an [instr] line, an [fx] set or an imported tweak)."""
        try:
            if k == "wave":
                if v.lower() not in WAVES:
                    self.err("wave must be one of " + ", ".join(WAVES), n, col)
                inst.wave = v.lower()
            elif k in ("a", "d", "s", "r", "vol"):
                iv = int(v)
                if not 0 <= iv <= 15:
                    self.err("%s must be 0 to 15" % k, n, col)
                setattr(inst, k, iv)
            elif k == "pw":
                iv = int(v)
                if not 1 <= iv <= 99:
                    self.err("pw must be 1 to 99", n, col)
                inst.pw = iv
            elif k == "pan":
                iv = int(v)
                if not -8 <= iv <= 8:
                    self.err("pan must be -8 to 8", n, col)
                inst.pan = iv
            elif k == "nmode":
                if v.lower() not in ("long", "short"):
                    self.err("nmode must be long or short", n, col)
                inst.nmode = v.lower()
            elif k == "vib":
                inst.vib_rate, inst.vib_depth = self.parse_vib(v, n, col)
            elif k == "slide":
                inst.slide = self.num_in(v, 0, 192, "slide", n, col)
            elif k == "arp":
                inst.arp = self.parse_arp(v, n, col)
            elif k == "arprate":
                inst.arprate = self.num_in(v, 1, 32, "arprate", n, col)
            elif k == "pws":
                inst.pws_from, inst.pws_to, inst.pws_rate = self.parse_pws(v, n, col)
            elif k == "filter":
                inst.filt, inst.filt_cut, inst.filt_res = self.parse_filter(v, n, col)
            elif k == "fsweep":
                inst.fs_from, inst.fs_to, inst.fs_rate = self.parse_fsweep(v, n, col)
            elif k == "send":
                inst.send = self.num_in(v, 0, 15, "send", n, col)
            else:
                self.warn("instrument key '%s' is not supported yet and is ignored" % k, n)
        except ValueError:
            self.err("%s needs a number" % k, n, col)

    def num_in(self, v, lo, hi, what, n, col):
        try:
            iv = int(v)
        except ValueError:
            self.err("%s needs a number" % what, n, col)
        if not lo <= iv <= hi:
            self.err("%s must be %d to %d" % (what, lo, hi), n, col)
        return iv

    def parse_echo(self, v, n, col):
        """echo 240ms,40,30  or  echo 240,40,30,wide  (time in ms 20-1000, feedback 0-90 %, mix 0-100 %) or off."""
        if v.lower() == "off":
            return None
        parts = [p.strip() for p in v.split(",")]
        wide = False
        if parts and parts[-1].lower() == "wide":
            wide = True
            parts.pop()
        if len(parts) != 3:
            self.err("echo needs time,feedback,mix (echo 240ms,40,30), optionally ,wide, or off", n, col)
        t = parts[0].lower()
        if t.endswith("ms"):
            t = t[:-2]
        ms = self.num_in(t, 20, 1000, "echo time (ms)", n, col)
        fb = self.num_in(parts[1], 0, 90, "echo feedback", n, col)
        mix = self.num_in(parts[2], 0, 100, "echo mix", n, col)
        return (ms / 1000.0, fb / 100.0, mix / 100.0, wide)

    def parse_vib(self, v, n, col):
        if v.lower() == "off":
            return 0, 0
        parts = v.split(",")
        if len(parts) != 2:
            self.err("vib needs rate,depth (vib 5,3) or off", n, col)
        return self.num_in(parts[0], 0, 15, "vib rate", n, col), self.num_in(parts[1], 0, 15, "vib depth", n, col)

    def parse_arp(self, v, n, col):
        if v.lower() == "off":
            return ()
        offs = [self.num_in(x, -24, 24, "arp offset", n, col) for x in v.split(",")]
        if len(offs) > 8:
            self.err("arp takes at most 8 offsets", n, col)
        return tuple(offs)

    def parse_filter(self, v, n, col):
        if v.lower() == "off":
            return "", 8, 0
        parts = v.split(",")
        if len(parts) not in (2, 3) or parts[0].lower() not in ("lp", "bp", "hp"):
            self.err("filter needs lp, bp or hp then cutoff[,resonance] (filter lp,8,4) or off", n, col)
        res = self.num_in(parts[2], 0, 15, "filter resonance", n, col) if len(parts) == 3 else 0
        return parts[0].lower(), self.num_in(parts[1], 0, 15, "filter cutoff", n, col), res

    def parse_fsweep(self, v, n, col):
        if v.lower() == "off":
            return 0, 0, 0
        parts = v.split(",")
        if len(parts) != 3:
            self.err("fsweep needs from,to,rate (fsweep 2,12,4) or off", n, col)
        return (self.num_in(parts[0], 0, 15, "fsweep from", n, col), self.num_in(parts[1], 0, 15, "fsweep to", n, col),
                self.num_in(parts[2], 0, 15, "fsweep rate", n, col))

    def parse_pws(self, v, n, col):
        if v.lower() == "off":
            return 0, 0, 0
        parts = v.split(",")
        if len(parts) != 3:
            self.err("pws needs from,to,rate (pws 25,75,6) or off", n, col)
        return (self.num_in(parts[0], 1, 99, "pws from", n, col), self.num_in(parts[1], 1, 99, "pws to", n, col),
                self.num_in(parts[2], 0, 15, "pws rate", n, col))

    def define_pat(self, n, col, s):
        m = re.match(r"([A-Za-z0-9_.-]+)\s*=?\s*(.*)$", s)
        if not m:
            self.err("a pattern line is: name = notes", n, col)
        name, body = m.group(1), m.group(2).strip()
        if not body:
            return self.do_import(n, col, "pat." + name)
        self.patterns[name] = (body, n, col + s.index(body))

    def do_import(self, n, col, spec):
        sec, dot, key = spec.strip().partition(".")
        if not dot:
            sec, key = "instr", spec.strip()
        if sec == "instr":
            inst = self.find_instr(key)
            if inst is None:
                self.err("no instrument '%s' in the library" % key, n, col)
            self.tune.instruments.setdefault(key, inst)
        elif sec == "key":
            k = self.find_scale(key)
            if k is None:
                self.err("no scale '%s' in the library" % key, n, col)
            self.keys.setdefault(key, k)
        elif sec == "fx":
            f = self.find_fx(key)
            if f is None:
                self.err("no fx set '%s' in the library" % key, n, col)
            self.fxsets.setdefault(key, f)
        elif sec == "pat":
            p = self.find_pat(key)
            if p is None:
                self.err("no pattern '%s' in the library" % key, n, col)
            self.patterns.setdefault(key, p)
        elif sec == "sample":
            if self.find_sample(key) is None:
                self.err("no sample '%s' in the sample folder" % key, n, col)
            self.sampledefs.setdefault(key, (key, None))
        else:
            self.err("can't import from [%s] yet" % sec, n, col)

    # ---- voices
    def voice_line(self, n, line, vm):
        label = vm.group(1).upper()
        if label[0] == "D":
            v = self.dvoices[int(label[1]) - 1]
        else:
            v = self.voices[int(label[1]) - 1]
        body = vm.group(2)
        self.run_text(v, body, n, vm.start(2) + 1, 0)

    def run_text(self, v, text, line, col0, depth):
        """Interpret MML `text` on voice v; col0 is the column of text[0] (for error positions)."""
        i, size = 0, len(text)

        def err(msg, at=None):
            self.err(msg, line, col0 + (i if at is None else at))

        while i < size:
            c = text[i]
            if c.isspace():
                i += 1
                continue
            cl = c.lower()
            if c == "|":
                self.bar_line(v, line, col0 + i)
                i += 1
            elif c == "[":
                j, d = i + 1, 1
                while j < size and d:
                    d += (text[j] == "[") - (text[j] == "]")
                    j += 1
                if d:
                    err("[ has no matching ]")
                body, k = text[i + 1:j - 1], j
                m = re.match(r"x?(\d+)", text[k:])
                count = 2
                if m:
                    count, k = int(m.group(1)), k + m.end()
                if not 1 <= count <= 99:
                    err("a repeat count is 1 to 99", k - 1)
                for _ in range(count):
                    self.run_text(v, body, line, col0 + i + 1, depth + 1)
                i = k
            elif c == "]":
                err("] without a [")
            elif c == "!":
                m = re.match(r"[A-Za-z0-9_.-]+", text[i + 1:])
                if not m:
                    err("! needs a pattern name")
                p = self.find_pat(m.group(0))
                if p is None:
                    err("no pattern '%s'" % m.group(0))
                if depth > 8:
                    err("patterns call each other too deeply")
                self.run_pattern(v, p, line, col0 + i, depth)
                i += 1 + m.end()
            elif v.dac is not None and (c.isalpha() or c == "_"):
                i = self.dac_word(v, text, i, line, col0)
            elif c == "@":
                m = re.match(r"[A-Za-z0-9_.-]+", text[i + 1:])
                if not m:
                    err("@ needs an instrument name")
                self.select(v, m.group(0), line, col0 + i)
                i += 1 + m.end()
            elif c in "<>":
                v.octave += 1 if c == ">" else -1
                if not 0 <= v.octave <= 8:
                    err("octave %d is out of range 0 to 8" % v.octave)
                i += 1
            elif c == "&":
                v.tie = True
                i += 1
                m = re.match(r"\s*(\d+)(?![0-9]*[a-gA-G])", text[i:])
                if m:                                   # &8 : lengthen the last note
                    ticks, used = self.duration(text, i + m.start(1), v, line, col0)
                    self.extend(v, ticks, line, col0 + i)
                    i += used + m.start(1)
                    v.tie = False
            elif c.isdigit() and v.key is not None:
                i = self.degree(v, text, i, line, col0)
            elif cl in LETTER and v.key is not None:
                err("letter notes are not used while a key is on: write degrees (1-9), or `key off`")
            elif c.isdigit():
                m = re.match(r"(\d+)([a-gA-G])", text[i:])
                if m and len(m.group(1)) == 1:
                    v.octave = int(m.group(1))
                    i += 1                              # the note follows
                elif m:
                    err("octave is 0 to 8")
                else:
                    err("a number needs a note before it (c8) or a letter after it (5c)")
            elif re.match(r"key(?![A-Za-z])", text[i:], re.I):
                i = self.set_key(v, text, i, line, col0)
            elif WORDS.match(text[i:]):
                word = re.match(r"[A-Za-z]+", text[i:]).group(0).lower()
                j = i + len(word)
                if word == "loop":
                    if self.loop is None:
                        self.loop = v.pos
                    i = j
                elif word == "once":
                    self.tune.once = True
                    i = j
                else:
                    m2 = re.match(r"\s+(\S+)", text[j:])
                    if not m2:
                        err("%s needs a value" % word)
                    self.tweak(v, word, m2.group(1), line, col0 + i)
                    i = j + m2.end()
            elif cl in LETTER:
                i = self.note(v, text, i, line, col0)
            elif cl in "rp" and not re.match(r"pw|pan|pat", text[i:], re.I):
                ticks, used = self.duration(text, i + 1, v, line, col0)
                v.pos += ticks
                v.tie, v.last = False, None
                i += 1 + used
            elif cl == "l":
                ticks, used = self.duration(text, i + 1, v, line, col0, need=True)
                v.deflen = ticks
                i += 1 + used
            elif cl == "o":
                m = re.match(r"o(\d)([a-gA-G][#+\-]*)?", text[i:], re.I)
                if not m:
                    err("o needs an octave 0 to 8")
                if int(m.group(1)) > 8:
                    err("octave is 0 to 8")
                v.octave = int(m.group(1))
                if m.group(2):
                    ls = LETTER[m.group(2)[0].lower()]
                    v.boundary = (ls + sum({"#": 1, "+": 1, "-": -1}[x] for x in m.group(2)[1:])) % 12
                i += m.end()
                if i < size and (text[i].isdigit() or text[i] in ".;"):
                    err("write a space before the note after o4a: o4a is the octave boundary form")
            elif cl == "t" and re.match(r"t\d", text[i:], re.I):
                m = re.match(r"t(\d+)", text[i:], re.I)
                bpm = int(m.group(1))
                if not 30 <= bpm <= 300:
                    err("tempo is 30 to 300")
                self.tempo[v.pos] = bpm
                i += m.end()
            elif cl == "v":
                m = re.match(r"v(?:(\d+)|([+-])(\d*))", text[i:], re.I)
                if not m:
                    err("v needs a volume 0 to 15 (or v+ / v-)")
                if m.group(1) is not None:
                    nv = int(m.group(1))
                    if nv > 15:
                        err("volume is 0 to 15")
                    v.vol = nv
                else:
                    step = int(m.group(3) or 1)
                    v.vol = max(0, min(15, v.vol + (step if m.group(2) == "+" else -step)))
                i += m.end()
            else:
                m = re.match(r"[A-Za-z]+", text[i:])
                err("don't know '%s'" % (m.group(0) if m else c))

    def dac_word(self, v, text, i, line, col0):
        """One word on a DAC line: a sample name (plays it), r (rest), l8 (default length), v12, t140, pan -3, stop,
        level 64, loop, once.  Returns the new position."""
        m = re.match(r"[A-Za-z_]+", text[i:])
        w = m.group(0)
        wl, j = w.lower(), i + len(w)
        ev = self.tune.dac[v.dac]

        def num(what, lo, hi, signed=False):
            mm = re.match(r"\s*(-?\d+)" if signed else r"\s*(\d+)", text[j:])
            if not mm:
                self.err("%s needs a number" % what, line, col0 + i)
            val = int(mm.group(1))
            if not lo <= val <= hi:
                self.err("%s is %d to %d" % (what, lo, hi), line, col0 + i)
            return val, j + mm.end()
        if wl == "r":
            ticks, used = self.duration(text, j, v, line, col0)
            v.pos += ticks
            return j + used
        if wl == "l":
            ticks, used = self.duration(text, j, v, line, col0, need=True)
            v.deflen = ticks
            return j + used
        if wl == "v":
            v.vol, j = num("volume", 0, 15)
            return j
        if wl == "t":
            bpm, j = num("tempo", 30, 300)
            self.tempo[v.pos] = bpm
            return j
        if wl == "pan":
            v.pan, j = num("pan", -8, 8, signed=True)
            return j
        if wl == "loop":
            if self.loop is None:
                self.loop = v.pos
            return j
        if wl == "once":
            self.tune.once = True
            return j
        if wl == "stop":
            ev.append((v.pos, "stop", None, None))
            return j
        if wl == "level":
            lvl, j = num("level", 0, 127)
            ev.append((v.pos, "level", lvl, (v.vol, v.pan or 0)))
            return j
        smp = self.find_sample(w)
        if smp is None:
            self.err("no sample '%s' (declare it under [sample]; the file goes in music/samples)" % w, line, col0 + i)
        ticks, used = self.duration(text, j, v, line, col0)
        ev.append((v.pos, "play", smp, (v.vol, v.pan or 0)))
        v.pos += ticks
        return j + used

    def run_pattern(self, v, p, line, col, depth):
        body, pline, pcol = p
        try:
            self.run_text(v, body, pline, pcol, depth + 1)
        except MMLError as e:
            if e.line == pline and (line, col) != (pline, pcol):
                raise MMLError("%s (in a pattern used at line %d, col %d)" % (e.msg, line, col), e.line, e.col)
            raise

    def select(self, v, name, line, col):
        inst = self.find_instr(name)
        if inst is None:
            self.err("no instrument '%s' (define it under [instr], or use a library name)" % name, line, col)
        v.inst = inst
        v.pan = None

    def tweak(self, v, word, arg, line, col):
        inst = replace(v.inst)
        try:
            if word == "instr":
                return self.select(v, arg, line, col)
            if word == "fx":
                return self.apply_fx(v, arg, line, col)
            if word == "slide":
                v.slide_next = self.num_in(arg, 0, 192, "slide", line, col)
                return
            if word in ("vib", "arp", "arprate", "pws", "send", "filter", "fsweep"):
                self.inst_key(inst, word, arg, line, col)
            elif word == "wave":
                if arg.lower() not in WAVES:
                    self.err("wave must be one of " + ", ".join(WAVES), line, col)
                inst.wave = arg.lower()
            elif word == "pw":
                if not 1 <= int(arg) <= 99:
                    self.err("pw must be 1 to 99", line, col)
                inst.pw = int(arg)
            elif word == "adsr":
                a, d, s, r = (int(x) for x in arg.split(","))
                if not all(0 <= x <= 15 for x in (a, d, s, r)):
                    self.err("adsr values are 0 to 15", line, col)
                inst.a, inst.d, inst.s, inst.r = a, d, s, r
            elif word == "pan":
                if not -8 <= int(arg) <= 8:
                    self.err("pan is -8 to 8", line, col)
                v.pan = int(arg)
                return
            elif word == "nmode":
                if arg.lower() not in ("long", "short"):
                    self.err("nmode is long or short", line, col)
                inst.nmode = arg.lower()
        except ValueError:
            self.err("%s needs numbers (adsr 0,8,10,4)" % word, line, col)
        v.inst = inst

    def duration(self, text, i, v, line, col0, need=False):
        """Parse digits, dots and a triplet mark at text[i:]; returns (ticks, characters used)."""
        m = re.match(r"(\d*)(\.*)(;?)", text[i:])
        digits, dots, trip = m.group(1), m.group(2), m.group(3)
        if need and not digits:
            self.err("l needs a length such as l8", line, col0 + i)
        if digits:
            nv = int(digits)
            if nv not in DURATIONS:
                self.err("length %d is not 1, 2, 4, 8, 16, 32 or 64" % nv, line, col0 + i)
            ticks = TICKS_WHOLE / nv
        else:
            ticks = v.deflen
        if len(dots) > 2:
            self.err("at most two dots", line, col0 + i)
        base = ticks
        for k in range(len(dots)):
            ticks += base / (2 << k)
        if trip:
            ticks = ticks * 2 / 3
        return max(1, int(round(ticks))), m.end()

    def extend(self, v, ticks, line, col):
        last = v.last
        if last is None or last.start + last.dur != v.pos:
            self.err("& needs a note just before it to hold", line, col)
        last.dur += ticks
        v.pos += ticks

    def note(self, v, text, i, line, col0):
        letter = text[i].lower()
        j = i + 1
        acc = 0
        while j < len(text) and text[j] in "#+-":
            acc += -1 if text[j] == "-" else 1
            j += 1
        b = v.boundary
        pitch = v.octave * 12 + b + ((LETTER[letter] - b) % 12) + acc
        if not 0 <= pitch <= 8 * 12 + 11:
            self.err("note is out of range (octave 0 to 8)", line, col0 + i)
        ticks, used = self.duration(text, j, v, line, col0)
        self.place_note(v, pitch, ticks, line, col0 + i)
        return j + used

    def place_note(self, v, pitch, ticks, line, col):
        """Put a note of `pitch` and `ticks` at the voice's position (joining a tie)."""
        if v.tie and v.last is not None and v.last.pitch == pitch and v.last.start + v.last.dur == v.pos:
            v.last.dur += ticks
        else:
            self.notes += 1
            if self.notes > NOTE_CAP:
                self.err("tune is too long: over %d events (limit %d)" % (2 * NOTE_CAP, EVENT_CAP), line, col)
            n = Note(v.pos, ticks, pitch, v.inst, v.vol, v.pan, v.slide_next if v.slide_next is not None else v.inst.slide)
            v.slide_next = None
            self.tune.voices[v.index].append(n)
            v.last = n
        v.tie = False
        v.pos += ticks
        return None

    def set_key(self, v, text, i, line, col0):
        """`key c major`, `key f# dorian`, `key a minor-pentatonic`, a [key] name, or `key off`."""
        m = re.match(r"key\s+off(?![A-Za-z])", text[i:], re.I)
        if m:
            v.key = None
            return i + m.end()
        m = re.match(r"key\s+([a-gA-G][#+\-]*)\s+([A-Za-z][A-Za-z0-9_-]*)", text[i:], re.I)
        if not m:
            self.err("key needs a tonic and a scale (key c major, key a minor, key off)", line, col0 + i)
        tonic = LETTER[m.group(1)[0].lower()] + sum({"#": 1, "+": 1, "-": -1}[x] for x in m.group(1)[1:])
        scale = self.find_scale(m.group(2))
        if scale is None:
            self.err("no scale '%s' (major minor dorian phrygian lydian mixolydian locrian pentatonic "
                     "minor-pentatonic, or one from [key])" % m.group(2), line, col0 + i)
        v.key = (tonic % 12, scale)
        return i + m.end()

    def degree(self, v, text, i, line, col0):
        """A scale degree 1.. (n+2) of the key, `+` / `-` a semitone up / down, `:8.` a length."""
        d = int(text[i])
        tonic, scale = v.key
        n = len(scale)
        if not 1 <= d <= n + 2:
            self.err("degree %d is outside this scale (1 to %d)" % (d, n + 2), line, col0 + i)
        j, acc = i + 1, 0
        while j < len(text) and text[j] in "#+-":
            acc += -1 if text[j] == "-" else 1
            j += 1
        idx = d - 1
        pitch = v.octave * 12 + tonic + scale[idx % n] + 12 * (idx // n) + acc
        if not 0 <= pitch <= 8 * 12 + 11:
            self.err("note is out of range (octave 0 to 8)", line, col0 + i)
        if j < len(text) and text[j] == ":":
            ticks, used = self.duration(text, j + 1, v, line, col0)
            used += 1
        else:
            ticks, used = v.deflen, 0
        return self.place_note(v, pitch, ticks, line, col0 + i) or j + used

    def bar_line(self, v, line, col):
        bt = self.tune.bar_ticks
        have = v.pos - v.barstart
        if have and have != bt:
            self.warn("voice %d bar %d is %d ticks long, a %d/%d bar is %d"
                      % (v.index + 1, v.bar, have, self.tune.time[0], self.tune.time[1], bt), line)
        if have:
            v.bar += 1
        v.barstart = v.pos

    # ---- end
    def finish(self):
        t = self.tune
        t.length = max([v.pos for v in self.voices + self.dvoices] + [0])
        bt = t.bar_ticks
        for v in self.voices:
            if t.voices[v.index] and v.pos < t.length:
                self.warn("voice %d is %d ticks shorter than the longest voice, so it goes quiet before the loop"
                          % (v.index + 1, t.length - v.pos))
        t.tempo_events = sorted(self.tempo.items())
        if self.loop is not None:
            loop = self.loop
            if loop % bt:
                loop -= loop % bt
                self.warn("the loop marker was moved back to the start of its bar")
            t.loop_tick = loop
        if t.events > EVENT_CAP:
            raise MMLError("tune is too long: %d events (limit %d)" % (t.events, EVENT_CAP))
        if not t.length:
            self.warn("the tune has no notes")
        return t


def parse(text, lib=None):
    """MML text -> Tune.  Raises MMLError (with .line / .col) on a mistake."""
    return _Parser(text, lib).run()


# ------------------------------------------------------------------ writing MML
NOTE_NAMES = ("c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b")


def note_name(pitch):
    return NOTE_NAMES[pitch % 12]


def duration_text(ticks, default=TICKS_WHOLE // 8):
    """MML for a length in ticks as one or more tied pieces ('4', '8.', '4&16' ...).  The first
    piece is '' when it equals the default length."""
    out, left = [], ticks
    sizes = []
    for n in DURATIONS:
        base = TICKS_WHOLE // n
        sizes.append((base, "%d" % n))
        if base % 2 == 0:
            sizes.append((base * 3 // 2, "%d." % n))
        if base % 4 == 0:
            sizes.append((base * 7 // 4, "%d.." % n))
    sizes = sorted(set(sizes), reverse=True)
    while left > 0:
        for size, txt in sizes:
            if size <= left:
                out.append(txt)
                left -= size
                break
        else:
            out.append("64")
            left = 0
    return out
