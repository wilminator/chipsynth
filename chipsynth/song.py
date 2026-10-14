# Copyright (C) 2026 Michael Allen Wilmes
"""The Song model: what the composer edits and the package format stores (PLAN-CHIPSYNTH.md, Part B).

MML is only an import / export format (`chipsynth.songmml`).  A `Song` keeps the structure a person wrote:
named clips (MML's `[pat]` patterns), repeats and clip calls are real objects, not flattened away.
`Song.compile(lib)` expands it into the flat `Tune` that `TunePlayer` plays; the player never changes.

A track is a list of events, one per MML token, so the model is notation-level and loses nothing:
  Note / Degree / Rest         a note by letter (or scale degree) and length; length None = the current default
  Length Octave Volume Tempo   state changes: `l8`, `o4` / `<` / `>`, `v12`, `t140`
  Tie Extend                   `&` and `&8`: a held note
  Select Fx Tweak Key          `@lead`, `fx wobble`, `vib 5,3` / `pan -3` / `slide 12`, `key c major`
  Bar Loop Once                `|`, the loop point, play-once
  Repeat Call                  `[ .. ]3` and `!clip`
  Comment                      a note to the human
  Play Stop Level              on DAC tracks (D1, D2): a sample, stop, and its level
Pitches stay relative to the voice's octave when played, exactly as in MML, so a clip called after `o3` and after
`o5` sounds two octaves apart.  (A transpose argument on `Call` is a later extension.)  The piano roll works on the
absolute notes of `compile()`; editing writes back through the same event types.
"""
from dataclasses import dataclass, field, replace

from . import mml
from .mml import MMLError, TICKS_WHOLE, LETTER

TRACKS = ("V1", "V2", "V3", "V4", "D1", "D2")


class SongError(Exception):
    """A Song that cannot be written as MML (for example a length that no MML token can say)."""


@dataclass(frozen=True)
class Rel:
    """A length written relative to the default length: `.` / `..` and `;` (triplet) with no number."""
    dots: int = 0
    trip: bool = False


def explicit_ticks(n, dots=0, trip=False):
    """Ticks of a written length: 1/n note, dots, triplet mark (the same arithmetic the parser uses)."""
    ticks = TICKS_WHOLE / n
    base = ticks
    for k in range(dots):
        ticks += base / (2 << k)
    if trip:
        ticks = ticks * 2 / 3
    return max(1, int(round(ticks)))


def ticks_of(dur, deflen):
    """A length field -> ticks: None is the default length, an int is ticks, Rel is relative to the default."""
    if dur is None:
        return deflen
    if isinstance(dur, int):
        return dur
    ticks = deflen
    base = ticks
    for k in range(dur.dots):
        ticks += base / (2 << k)
    if dur.trip:
        ticks = ticks * 2 / 3
    return max(1, int(round(ticks)))


# ------------------------------------------------------------------ events
@dataclass
class Note:
    letter: str                   # c d e f g a b
    acc: int = 0                  # +1 sharp, -1 flat
    dur: object = None            # None | int ticks | Rel


@dataclass
class Degree:
    degree: int                   # 1.. in the key
    acc: int = 0
    dur: object = None


@dataclass
class Rest:
    dur: object = None


@dataclass
class Length:
    ticks: int                    # `l8`: the new default length


@dataclass
class Octave:
    set: int = None               # `o4`, or the digit in `4c`
    shift: int = 0                # `<` is -1, `>` is +1
    boundary: int = None          # `o4a`: the octave starts at this pitch class (0-11)


@dataclass
class Volume:
    value: int = None             # `v12`
    delta: int = 0                # `v+` / `v-3`


@dataclass
class Tempo:
    bpm: int


@dataclass
class Tie:
    pass


@dataclass
class Extend:
    ticks: int                    # `&8`: lengthen the note just before


@dataclass
class Select:
    name: str                     # `@name` / `instr name`


@dataclass
class Fx:
    name: str


@dataclass
class Tweak:
    word: str                     # wave pw adsr pan nmode vib slide arp arprate pws filter fsweep send
    arg: str


@dataclass
class Key:
    tonic: int = None             # 0-11, or None for `key off`
    scale: str = None


@dataclass
class Bar:
    pass


@dataclass
class Loop:
    pass


@dataclass
class Once:
    pass


@dataclass
class Repeat:
    count: int
    items: list = field(default_factory=list)


@dataclass
class Call:
    name: str


@dataclass
class Comment:
    text: str


@dataclass
class Play:
    sample: str
    dur: object = None


@dataclass
class Stop:
    pass


@dataclass
class Level:
    level: int


@dataclass
class Clip:
    """A named run of events (MML's `[pat]`).  `raw` holds the original text of a pattern that was never used and
    does not read as MML on its own; it is written back unchanged."""
    name: str
    items: list = field(default_factory=list)
    dac: bool = False
    raw: str = None


@dataclass
class Piece:
    """One event's contribution to the compiled tune: where a note, a held extension or a rest landed.  An event used
    several times (a clip called twice, a repeat) gives one Piece per occurrence, all pointing at the same event."""
    voice: int
    start: int
    dur: int
    kind: str                     # "note" | "extend" | "rest"
    event: object
    container: list               # the list the event sits in (a track, a clip or a repeat body)
    state: tuple                  # (octave, boundary, key) of the voice just before the event
    pitch: int = None
    note: object = None           # the compiled mml.Note this piece belongs to


@dataclass
class Trace:
    """What `Song.compile(trace=...)` learned about where the tune came from (the composer edits through it)."""
    pieces: list = field(default_factory=lambda: [[] for _ in range(4)])     # per melodic voice, in time order
    ends: list = field(default_factory=lambda: [0] * 4)                      # tick where each voice's events end
    final: list = field(default_factory=lambda: [(4, 0, None)] * 4)          # each voice's final state

    def groups(self, voice):
        """{id(compiled note): [its pieces]} for one voice."""
        out = {}
        for p in self.pieces[voice]:
            if p.note is not None:
                out.setdefault(id(p.note), []).append(p)
        return out


@dataclass
class Song:
    title: str = ""
    author: str = ""
    comment: str = ""
    bpm: int = 120
    time: tuple = (4, 4)
    detune: int = 0
    once: bool = False
    echo: tuple = None
    reverb: int = 0
    instruments: dict = field(default_factory=dict)   # name -> Instrument defined (or imported) by this song
    fx: dict = field(default_factory=dict)            # name -> [(key, value text)]
    keys: dict = field(default_factory=dict)          # name -> scale steps
    samples: dict = field(default_factory=dict)       # name -> (file, rate or None)
    clips: dict = field(default_factory=dict)         # name -> Clip
    tracks: dict = field(default_factory=dict)        # "V1".."V4", "D1", "D2" -> [events]
    notes: list = field(default_factory=list)         # comment lines above the header
    footer: list = field(default_factory=list)        # comment lines after the last track
    warnings: list = field(default_factory=list)      # what the importer noticed (not saved)

    def __post_init__(self):
        for name in TRACKS:
            self.tracks.setdefault(name, [])

    @classmethod
    def from_mml(cls, text, lib=None):
        from .songmml import from_mml
        return from_mml(text, lib)

    def to_mml(self):
        from .songmml import to_mml
        return to_mml(self)

    def compile(self, lib=None, trace=None):
        """Expand clips and repeats into the flat `Tune` the player plays (raises MMLError on a mistake).  Pass a
        `Trace` to learn which event made each note."""
        return _Compiler(self, lib or mml.builtin_library(), trace).run()


# ------------------------------------------------------------------ Song -> Tune
class _Compiler:
    """Plays the events through the same voice state and note placement the MML parser uses, so a Song compiles to
    exactly the Tune that parsing its MML would give."""

    def __init__(self, song, lib, trace=None):
        self.song = song
        self.trace = trace
        self.cur = None
        p = self.p = mml._Parser("", lib)
        t = p.tune
        t.title, t.author, t.comment = song.title, song.author, song.comment
        t.bpm, t.time, t.detune, t.once = song.bpm, tuple(song.time), song.detune, song.once
        t.echo, t.reverb = song.echo, song.reverb
        t.instruments = {k: replace(v) for k, v in song.instruments.items()}
        t.warnings = list(song.warnings)
        p.fxsets = {k: list(v) for k, v in song.fx.items()}
        p.keys = dict(song.keys)
        p.sampledefs = dict(song.samples)

    def run(self):
        p = self.p
        for k, name in enumerate(TRACKS):
            v = p.voices[k] if k < 4 else p.dvoices[k - 4]
            self.play(v, self.song.tracks.get(name, ()), 0)
        if self.trace is not None:
            for i in range(4):
                v = p.voices[i]
                self.trace.ends[i] = v.pos
                self.trace.final[i] = (v.octave, v.boundary, v.key)
        return p.finish()

    def err(self, msg):
        raise MMLError(msg)

    def play(self, v, items, depth):
        for e in items:
            self.cur = items
            getattr(self, "do_" + type(e).__name__)(v, e, depth)

    def note_piece(self, v, kind, e, start, dur, state, pitch=None):
        if self.trace is not None and v.index < 4:
            self.trace.pieces[v.index].append(Piece(v.index, start, dur, kind, e, self.cur, state, pitch, v.last))

    # ---- notes
    def do_Note(self, v, e, depth):
        if v.key is not None:
            self.err("letter notes are not used while a key is on: write degrees (1-9), or `key off`")
        b = v.boundary
        pitch = v.octave * 12 + b + ((LETTER[e.letter] - b) % 12) + e.acc
        if not 0 <= pitch <= 8 * 12 + 11:
            self.err("note is out of range (octave 0 to 8)")
        start, ticks, state = v.pos, ticks_of(e.dur, v.deflen), (v.octave, v.boundary, v.key)
        self.p.place_note(v, pitch, ticks, 0, 0)
        self.note_piece(v, "note", e, start, ticks, state, pitch)

    def do_Degree(self, v, e, depth):
        if v.key is None:
            self.err("a degree needs a key (key c major)")
        tonic, scale = v.key
        n = len(scale)
        if not 1 <= e.degree <= n + 2:
            self.err("degree %d is outside this scale (1 to %d)" % (e.degree, n + 2))
        idx = e.degree - 1
        pitch = v.octave * 12 + tonic + scale[idx % n] + 12 * (idx // n) + e.acc
        if not 0 <= pitch <= 8 * 12 + 11:
            self.err("note is out of range (octave 0 to 8)")
        start, ticks, state = v.pos, ticks_of(e.dur, v.deflen), (v.octave, v.boundary, v.key)
        self.p.place_note(v, pitch, ticks, 0, 0)
        self.note_piece(v, "note", e, start, ticks, state, pitch)

    def do_Rest(self, v, e, depth):
        start, ticks, state = v.pos, ticks_of(e.dur, v.deflen), (v.octave, v.boundary, v.key)
        v.pos += ticks
        v.tie, v.last = False, None
        self.note_piece(v, "rest", e, start, ticks, state)

    def do_Tie(self, v, e, depth):
        v.tie = True

    def do_Extend(self, v, e, depth):
        v.tie = True
        start, state = v.pos, (v.octave, v.boundary, v.key)
        self.p.extend(v, e.ticks, 0, 0)
        v.tie = False
        self.note_piece(v, "extend", e, start, e.ticks, state)

    # ---- state
    def do_Length(self, v, e, depth):
        v.deflen = e.ticks

    def do_Octave(self, v, e, depth):
        if e.set is not None:
            v.octave = e.set
        v.octave += e.shift
        if e.boundary is not None:
            v.boundary = e.boundary % 12
        if not 0 <= v.octave <= 8:
            self.err("octave %d is out of range 0 to 8" % v.octave)

    def do_Volume(self, v, e, depth):
        if e.value is not None:
            if not 0 <= e.value <= 15:
                self.err("volume is 0 to 15")
            v.vol = e.value
        else:
            v.vol = max(0, min(15, v.vol + e.delta))

    def do_Tempo(self, v, e, depth):
        if not 30 <= e.bpm <= 300:
            self.err("tempo is 30 to 300")
        self.p.tempo[v.pos] = e.bpm

    def do_Select(self, v, e, depth):
        self.p.select(v, e.name, 0, 0)

    def do_Fx(self, v, e, depth):
        self.p.apply_fx(v, e.name, 0, 0)

    def do_Tweak(self, v, e, depth):
        self.p.tweak(v, e.word, e.arg, 0, 0)

    def do_Key(self, v, e, depth):
        if e.tonic is None:
            v.key = None
            return
        scale = self.p.find_scale(e.scale)
        if scale is None:
            self.err("no scale '%s'" % e.scale)
        v.key = (e.tonic % 12, scale)

    def do_Bar(self, v, e, depth):
        self.p.bar_line(v, 0, 0)

    def do_Loop(self, v, e, depth):
        if self.p.loop is None:
            self.p.loop = v.pos

    def do_Once(self, v, e, depth):
        self.p.tune.once = True

    def do_Comment(self, v, e, depth):
        pass

    # ---- structure
    def do_Repeat(self, v, e, depth):
        if not 1 <= e.count <= 99:
            self.err("a repeat count is 1 to 99")
        for _ in range(e.count):
            self.play(v, e.items, depth + 1)

    def do_Call(self, v, e, depth):
        clip = self.song.clips.get(e.name)
        if clip is None:
            self.err("no pattern '%s'" % e.name)
        if clip.raw is not None:
            self.err("pattern '%s' could not be read" % e.name)
        if depth > 8:
            self.err("patterns call each other too deeply")
        self.play(v, clip.items, depth + 1)

    # ---- DAC
    def do_Play(self, v, e, depth):
        smp = self.p.find_sample(e.sample)
        if smp is None:
            self.err("no sample '%s' (declare it under [sample]; the file goes in music/samples)" % e.sample)
        self.p.tune.dac[v.dac].append((v.pos, "play", smp, (v.vol, v.pan or 0)))
        v.pos += ticks_of(e.dur, v.deflen)

    def do_Stop(self, v, e, depth):
        self.p.tune.dac[v.dac].append((v.pos, "stop", None, None))

    def do_Level(self, v, e, depth):
        self.p.tune.dac[v.dac].append((v.pos, "level", e.level, (v.vol, v.pan or 0)))
