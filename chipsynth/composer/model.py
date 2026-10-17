# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the composer window shows, with no pygame in it (so it is tested without a display).

    roll = Roll.from_tune(tune, names)       # notes per voice, DAC blocks, pitch range
    seek = tune_from(tune, tick)             # a Tune that starts at `tick` and then loops the loop section
    mix  = mixdown(tune, {1, 4})             # the tune with voice 2 and DAC 1 silenced
    view = View(); view.x_of(tick); view.tick_at(x); view.zoom_h(1.25, anchor_x)
"""
from dataclasses import dataclass, field, replace

from chipsynth.mml import Note, Tune, TICKS_WHOLE
from chipsynth.songedit import effect_of
from chipsynth.synth import RATE

BEAT = TICKS_WHOLE // 4
SNAPS = (("1/4", 96), ("1/8", 48), ("1/16", 24), ("1/32", 12), ("1/8T", 32), ("1/16T", 16))   # note-grid choices
VOICES = 6                                   # V1-V4, D1, D2
NAMES = ("V1", "V2", "V3", "V4", "D1", "D2")
LOW, HIGH = 0, 8 * 12 + 11                   # C0 .. B8
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def pitch_name(p):
    return "%s%d" % (NOTE_NAMES[p % 12], p // 12)


def is_black(p):
    return p % 12 in (1, 3, 6, 8, 10)


# ------------------------------------------------------------------ the roll
@dataclass
class RollNote:
    voice: int
    start: int
    dur: int
    pitch: int
    vol: int
    inst: str = ""
    fx: str = "none"           # one of songedit.EFFECTS


@dataclass
class DacBlock:
    voice: int                 # 4 or 5
    start: int
    dur: int
    name: str
    vol: int = 15


@dataclass
class Roll:
    notes: list = field(default_factory=lambda: [[] for _ in range(4)])
    dac: list = field(default_factory=lambda: [[], []])
    low: int = 48
    high: int = 72
    length: int = 0
    loop_tick: int = 0
    bar_ticks: int = TICKS_WHOLE

    @classmethod
    def from_tune(cls, tune, names=None):
        """`names(inst)` -> a display name for an Instrument (or None)."""
        r = cls(length=tune.length, loop_tick=tune.loop_tick, bar_ticks=tune.bar_ticks)
        for vi, voice in enumerate(tune.voices):
            for n in voice:
                r.notes[vi].append(RollNote(vi, n.start, n.dur, n.pitch, n.vol, names(n.inst) if names else "", effect_of(n)))
        for di, events in enumerate(tune.dac):
            plays = [e for e in events if e[1] == "play"]
            for k, (tick, kind, smp, extra) in enumerate(events):
                if kind != "play":
                    continue
                end = tune.sample_to_tick(tune.tick_to_sample(tick) + smp.seconds * RATE)
                for nxt in events[events.index((tick, kind, smp, extra)) + 1:]:
                    if nxt[0] > tick and nxt[1] in ("play", "stop"):
                        end = min(end, nxt[0])
                        break
                r.dac[di].append(DacBlock(4 + di, tick, max(1, int(round(end - tick))), smp.name or "sample",
                                          extra[0] if extra else 15))
        pitches = [n.pitch for v in r.notes for n in v]
        if pitches:
            r.low, r.high = max(LOW, min(pitches) - 3), min(HIGH, max(pitches) + 3)
            if r.high - r.low < 24:
                mid = (r.low + r.high) // 2
                r.low, r.high = max(LOW, mid - 12), min(HIGH, mid + 12)
        return r

    def counts(self):
        return [len(v) for v in self.notes] + [len(v) for v in self.dac]


# ------------------------------------------------------------------ playing from the middle, muting
def _shift_note(n, delta, start=None, dur=None):
    return replace(n, start=(n.start if start is None else start) + delta, dur=n.dur if dur is None else dur)


def tune_from(tune, tick):
    """A Tune that starts at `tick` of `tune`, runs to the end, then repeats the loop section forever -- so playing it
    from the start is the same sound as playing `tune` from `tick`, and the loop that follows is the real one.  A note
    held across `tick` starts again at the beginning.  Returns `tune` itself for tick 0."""
    tick = max(0, min(int(tick), tune.length))
    if tick >= tune.length:
        tick = tune.loop_tick
    if tick == 0:
        return tune
    length, loop = tune.length, tune.loop_tick
    a_len = length - tick
    gap = a_len - loop                           # add to a tick of the loop section to get its place after the first run
    voices = [[] for _ in tune.voices]
    for vi, voice in enumerate(tune.voices):
        for n in voice:
            if n.start + n.dur > tick:
                s = max(n.start, tick)
                voices[vi].append(replace(n, start=s - tick, dur=n.start + n.dur - s))
        if not tune.once:
            for n in voice:
                if n.start >= loop:
                    voices[vi].append(replace(n, start=n.start + gap))
    dac = [[], []]
    for di, events in enumerate(tune.dac):
        for e in events:
            if e[0] >= tick:
                dac[di].append((e[0] - tick,) + tuple(e[1:]))
        if not tune.once:
            for e in events:
                if e[0] >= loop:
                    dac[di].append((e[0] + gap,) + tuple(e[1:]))
        dac[di].sort(key=lambda e: e[0])
    tempos = [(t - tick, b) for t, b in tune.tempo_events if t > tick]
    if not tune.once:
        tempos.append((a_len, tune.bpm_at(loop)))
        tempos += [(t + gap, b) for t, b in tune.tempo_events if t > loop]
    out = replace(tune, bpm=round(tune.bpm_at(tick)), tempo_events=tempos, voices=voices, dac=dac,
                  length=a_len + (0 if tune.once else length - loop), loop_tick=0 if tune.once else a_len)
    return out


def tune_range(tune, a, b):
    """A Tune that is just ticks a..b of `tune`, repeating from its own start (for auditioning a stretch).  A note
    held across `a` starts again at the beginning and one running past `b` is cut."""
    a = max(0, int(a))
    b = min(int(b), tune.length)
    if b <= a:
        return tune
    voices = [[replace(n, start=max(n.start, a) - a, dur=min(n.start + n.dur, b) - max(n.start, a))
               for n in voice if n.start < b and n.start + n.dur > a] for voice in tune.voices]
    dac = [sorted(((e[0] - a,) + tuple(e[1:]) for e in events if a <= e[0] < b), key=lambda e: e[0])
           for events in tune.dac]
    tempos = [(t - a, bpm) for t, bpm in tune.tempo_events if a < t < b]
    return replace(tune, bpm=round(tune.bpm_at(a)), tempo_events=tempos, voices=voices, dac=dac, length=b - a,
                   loop_tick=0, once=False)


def source_tick(tune, seek_tick, ticks):
    """Where in the original tune a position of `tune_from(tune, seek_tick)` is."""
    if seek_tick >= tune.length:
        seek_tick = tune.loop_tick
    if seek_tick == 0:
        return ticks
    a_len = tune.length - seek_tick
    if ticks < a_len or tune.once:
        return seek_tick + min(ticks, a_len)
    seg = max(1, tune.length - tune.loop_tick)
    return tune.loop_tick + (ticks - a_len) % seg


def mixdown(tune, muted):
    """The tune with the voices in `muted` (0-3 melodic, 4-5 DAC) silenced."""
    if not muted:
        return tune
    return replace(tune, voices=[[] if i in muted else v for i, v in enumerate(tune.voices)],
                   dac=[[] if 4 + i in muted else d for i, d in enumerate(tune.dac)])


def solo_set(solos):
    """The muted set that leaves only `solos` sounding."""
    return set(range(VOICES)) - set(solos) if solos else set()


# ------------------------------------------------------------------ the view
@dataclass
class View:
    """Scale and scroll of the piano roll.  Horizontal and vertical scale are independent."""
    ppb: float = 56.0                # pixels per beat (horizontal scale)
    row: float = 14.0                # pixels per semitone (vertical scale)
    scroll_tick: float = 0.0         # the tick at the left edge
    top_pitch: float = 84.0          # the pitch at the top edge (fractional)
    MIN_PPB, MAX_PPB, MIN_ROW, MAX_ROW = 6.0, 400.0, 6.0, 48.0

    @property
    def ppt(self):
        return self.ppb / BEAT

    def x_of(self, tick, x0=0):
        return x0 + (tick - self.scroll_tick) * self.ppt

    def tick_at(self, x, x0=0):
        return (x - x0) / self.ppt + self.scroll_tick

    def y_of(self, pitch, y0=0):
        """The top edge of the row for `pitch`."""
        return y0 + (self.top_pitch - pitch - 1) * self.row

    def pitch_at(self, y, y0=0):
        return int(self.top_pitch - (y - y0) / self.row)

    def zoom_h(self, factor, anchor_x=0, x0=0):
        tick = self.tick_at(anchor_x, x0)
        self.ppb = max(self.MIN_PPB, min(self.MAX_PPB, self.ppb * factor))
        self.scroll_tick = tick - (anchor_x - x0) / self.ppt

    def zoom_v(self, factor, anchor_y=0, y0=0):
        pitch = self.top_pitch - (anchor_y - y0) / self.row
        self.row = max(self.MIN_ROW, min(self.MAX_ROW, self.row * factor))
        self.top_pitch = pitch + (anchor_y - y0) / self.row

    def clamp(self, width, height, length):
        """Keep the view inside the song (a little room after the end) and inside C0..B8."""
        span = width / self.ppt
        self.scroll_tick = max(0.0, min(self.scroll_tick, max(0.0, length + BEAT * 4 - span)))
        rows = height / self.row
        self.top_pitch = max(min(rows, HIGH + 1.0), min(self.top_pitch, HIGH + 1.0))

    def show(self, low, high, width, height):
        """Scroll so pitches low..high are in view and the song starts at the left."""
        self.scroll_tick = 0.0
        mid = (low + high) / 2.0
        self.top_pitch = mid + height / self.row / 2.0
