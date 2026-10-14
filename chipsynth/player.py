# Copyright (C) 2026 Michael Allen Wilmes
"""Plays a Tune (see mml.py) through the mixer core (synth.py).  Needs no audio device: it renders samples.

    p = TunePlayer(tune)            # loops from the loop marker; tune.once plays a single time
    chunk = p.render(4096)          # float32 (4096, 2); call again for the next chunk, forever
    render_wav(tune, "song.wav")    # the first pass plus `loops` repeats of the loop section

(output.py puts a TunePlayer on a pygame mixer channel for live playback.)
"""
import wave

import numpy as np

from .synth import RATE, Instrument, Synth, note_freq, to_pcm16

MASTER = 1.5
LOOKAHEAD = 2 * RATE            # schedule the next pass this far ahead of the play position
TAIL = 3 * RATE                 # a once-only tune is finished this long after its last note at the latest


class TunePlayer:
    def __init__(self, tune, loop=True, volume=1.0):
        self.tune = tune
        self.loop = loop and not tune.once
        self.volume = volume
        self.synth = Synth()
        if tune.echo or tune.reverb:
            from .synthfx import Bus, Echo, Reverb
            self.synth.bus = Bus(Echo(*tune.echo) if tune.echo else None, Reverb(tune.reverb) if tune.reverb else None)
        self.passes = 0                      # passes scheduled so far
        self.end = 0                         # sample at which the scheduled passes end
        self._cents = 2.0 ** (tune.detune / 1200.0)
        self._pass_len = tune.tick_to_sample(tune.length)
        self._loop_s = tune.tick_to_sample(tune.loop_tick)
        self._seg = self._pass_len - self._loop_s
        # per voice: (on sample, off sample, freq, inst, vol, pan) in time order
        self._notes = []
        for v in tune.voices:
            row, prev = [], None
            for n in v:
                freq = note_freq(n.pitch) * self._cents
                on = tune.tick_to_sample(n.start)
                extra = None
                if n.inst.has_fx or n.slide:
                    extra = {"beat": tune.tick_to_sample(n.start + 96) - on}      # a quarter note, in samples
                    if n.slide and prev:
                        extra["slide_from"] = prev
                        extra["slide_len"] = max(1, int(extra["beat"] * n.slide / 48))
                row.append((on, tune.tick_to_sample(n.start + n.dur), freq, n.inst, n.vol, n.pan, extra))
                prev = freq
            self._notes.append(row)
        self._starts = [[n[0] for n in v] for v in self._notes]
        # per DAC: (sample, kind, a, b) in time order, as absolute samples
        self._dac = [[(tune.tick_to_sample(e[0]),) + tuple(e[1:]) for e in ev] for ev in tune.dac]
        self.done = not tune.length
        self.looped = 0                      # completed trips round the loop

    def _schedule_pass(self):
        k = self.passes
        if k == 0:
            first, offset = 0, 0
        else:
            first, offset = self._loop_s, k * self._seg
        sy = self.synth
        for vi, notes in enumerate(self._notes):
            if k == 0:
                chunk = notes
            else:
                import bisect
                chunk = notes[bisect.bisect_left(self._starts[vi], first):]
            for on, off, freq, inst, vol, pan, extra in chunk:
                kw = {"freq": freq, "inst": inst, "vol": vol}
                if pan is not None:
                    kw["pan"] = pan
                if extra:
                    kw.update(extra)
                sy.schedule(offset + on, "on", vi, **kw)
                sy.schedule(offset + off, "off", vi)
        for di, events in enumerate(self._dac):
            for at, kind, a, b in events:
                if k and at < first:
                    continue
                self._schedule_dac(offset + at, di, kind, a, b)
        self.passes += 1
        self.end = self._pass_len if k == 0 else self._pass_len + k * self._seg

    def _schedule_dac(self, at, di, kind, a, b):
        sy = self.synth
        if kind == "play":
            vol, pan = b
            if a.stereo:                                  # left on DAC 1, right on DAC 2, started together
                sy.schedule(at, "dac_start", 0, data=a.tracks[0], rate=a.rate, vol=vol, pan=-8)
                sy.schedule(at, "dac_start", 1, data=a.tracks[1], rate=a.rate, vol=vol, pan=8)
            else:
                sy.schedule(at, "dac_start", di, data=a.tracks[0], rate=a.rate, vol=vol, pan=pan)
        elif kind == "stop":
            sy.schedule(at, "dac_stop", di)
        elif kind == "level":
            vol, pan = b
            sy.schedule(at, "dac_level", di, level=a, vol=vol, pan=pan)

    def render(self, n):
        """The next n samples (float32, shape (n, 2)); silence once a once-only tune has finished."""
        if not self.done:
            while self.end < self.synth.t + n + LOOKAHEAD and (self.passes == 0 or (self.loop and self._seg > 0)):
                self._schedule_pass()
        out = self.synth.render(n)
        if not self.done and self.synth.t > self.end and not (self.loop and self._seg > 0):
            quiet = (all(v.stage == "off" for v in self.synth.voices)
                     and all(d.levels is None for d in self.synth.dacs))
            if quiet or self.synth.t > self.end + TAIL:
                self.done = True
        out *= MASTER * self.volume
        return out

    def next_bar_sample(self):
        """The absolute sample at which the next bar line falls (a loop marker sits on a bar line)."""
        t = self.synth.t
        tune = self.tune
        bt = tune.bar_ticks
        if not bt or not tune.length:
            return t
        bars = [tune.tick_to_sample(k * bt) for k in range(-(-tune.length // bt) + 1)]
        bars[-1] = min(bars[-1], self._pass_len)
        if t < self._pass_len or self._seg <= 0:
            for b in bars:
                if b > t:
                    return b
            return t
        ls = self._loop_s + ((t - self._loop_s) % self._seg)
        for b in bars:
            if b > ls and b > self._loop_s - 1:
                return t + (b - ls)
        return t + (self._pass_len - ls)

    def skip_to_bar(self):
        """Run the player on, silently, to the next bar line (a tune resumed after a fight starts on the
        beat).  Returns the seconds skipped."""
        start = self.synth.t
        target = self.next_bar_sample()
        vol = self.volume
        self.volume = 0.0
        left = target - start
        while left > 0:
            n = min(left, RATE)
            self.render(n)
            left -= n
        self.volume = vol
        return (self.synth.t - start) / RATE

    @property
    def seconds(self):
        return self.synth.t / RATE


def render(tune, seconds=None, loops=0):
    """Samples of the tune: `seconds` of it, or the first pass plus its tail, plus `loops` round the loop."""
    p = TunePlayer(tune, loop=loops > 0)
    if seconds is None:
        n = p._pass_len + loops * p._seg + 2 * RATE
    else:
        n = int(seconds * RATE)
    return p.render(n)


def render_wav(tune, path, seconds=None, loops=0):
    pcm = to_pcm16(render(tune, seconds, loops))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm)
    return path
