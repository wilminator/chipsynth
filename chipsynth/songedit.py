# Copyright (C) 2026 Michael Allen Wilmes
"""Editing a Song note by note, with undo and redo (what the composer's piano roll drives).

    ed = Editor(song, lib)
    ed.add_note(voice, start, dur, pitch)      # into a rest (or past the end of the voice)
    ed.set_pitch(voice, index, pitch)          # `index` is the note's place in ed.tune.voices[voice]
    ed.resize(voice, index, dur)               # later notes never move: the freed time becomes a rest
    ed.move(voice, index, start, pitch)
    ed.erase(voice, index)                     # the note becomes a rest of the same length
    ed.undo(); ed.redo()

Every edit changes the Song's events (so what you edit is what `to_mml()` writes) and keeps every other note where it
was.  A note that lives in a pattern or a repeat is edited in the pattern: every place that plays it changes with it.
An edit that cannot work (no room, an unwritable length, a note out of range) raises `EditError` and changes nothing.
"""
import copy
import dataclasses
import re

from . import mml
from .mml import MMLError, LETTER
from .song import (Note, Degree, Rest, Extend, Octave, Select, Repeat, Call, Play, Trace, TRACKS, SongError)
from .synth import WAVES, Instrument
from .songmml import length_pieces


class EditError(Exception):
    pass


NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*$")
# instrument field -> (low, high) for numbers, or the allowed words
FIELDS = {"pw": (1, 99), "a": (0, 15), "d": (0, 15), "s": (0, 15), "r": (0, 15), "vol": (0, 15), "pan": (-8, 8),
          "vib_rate": (0, 15), "vib_depth": (0, 15), "slide": (0, 192), "arprate": (1, 32),
          "filt_cut": (0, 15), "filt_res": (0, 15), "send": (0, 15), "pws_from": (0, 99), "pws_to": (0, 99),
          "pws_rate": (0, 15), "fs_from": (0, 15), "fs_to": (0, 15), "fs_rate": (0, 15),
          "wave": WAVES, "nmode": ("long", "short"), "filt": ("", "lp", "bp", "hp")}


MAX_PITCH = 8 * 12 + 11
GROW_CAP = 16 * mml.TICKS_WHOLE            # how far past the end a note may be pulled in one go


def writable(ticks):
    try:
        length_pieces(int(ticks))
        return True
    except SongError:
        return False


def spell(pitch, state):
    """Events that sound `pitch` at a voice whose (octave, boundary, key) is `state`: (before, note-event, after).
    The note is in the current octave when it can be, else the octave is shifted just for this note."""
    octave, b, key = state
    for k in (0, -1, 1, -2, 2, -3, 3):
        o = octave + k
        if not 0 <= o <= 8:
            continue
        for acc in (0, 1, -1):
            if key is None:
                for letter, pc in LETTER.items():
                    if o * 12 + b + ((pc - b) % 12) + acc == pitch:
                        return _wrap(k, Note(letter, acc))
            else:
                tonic, scale = key
                n = len(scale)
                for d in range(1, n + 3):
                    idx = d - 1
                    if o * 12 + tonic + scale[idx % n] + 12 * (idx // n) + acc == pitch:
                        return _wrap(k, Degree(d, acc))
    raise EditError("that pitch cannot be written here")


def _wrap(k, ev):
    return ([Octave(shift=k)] if k else []), ev, ([Octave(shift=-k)] if k else [])


def _index(container, event):
    for i, e in enumerate(container):
        if e is event:
            return i
    raise EditError("internal: event not found")


def _unwrap(container, event):
    """Drop a `<` just before `event` and the `>` just after it (or the reverse): together they change nothing, and
    they are what set_pitch adds around a note outside the voice's octave."""
    i = _index(container, event)
    if 0 < i < len(container) - 1:
        a, b = container[i - 1], container[i + 1]
        if (isinstance(a, Octave) and isinstance(b, Octave) and a.set is None and b.set is None
                and a.boundary is None and b.boundary is None and a.shift and a.shift == -b.shift):
            del container[i + 1]
            del container[i - 1]


def _set_ticks(ev, ticks):
    if isinstance(ev, Extend):
        ev.ticks = ticks
    else:
        ev.dur = ticks


class Editor:
    def __init__(self, song, lib=None, limit=300):
        self.song = song
        self.lib = lib or mml.builtin_library()
        self.limit = limit
        self.undo_stack = []            # (id, label, before, after)
        self.redo_stack = []
        self._next = 1
        self.saved_id = 0
        self.refresh()

    # ---- the derived view
    def refresh(self):
        self.trace = Trace()
        self.tune = self.song.compile(self.lib, self.trace)
        return self.tune

    @property
    def dirty(self):
        return (self.undo_stack[-1][0] if self.undo_stack else 0) != self.saved_id

    def mark_saved(self):
        self.saved_id = self.undo_stack[-1][0] if self.undo_stack else 0

    # ---- history
    def _snap(self):
        return copy.deepcopy({k: v for k, v in self.song.__dict__.items()})

    def _restore(self, snap):
        self.song.__dict__.clear()
        self.song.__dict__.update(copy.deepcopy(snap))

    def change(self, label, fn):
        """Run `fn()` (which edits self.song) as one undoable step.  Any failure puts the song back."""
        before = self._snap()
        try:
            result = fn()
            self.refresh()
        except (EditError, MMLError, SongError) as e:
            self._restore(before)
            self.refresh()
            raise e if isinstance(e, EditError) else EditError(str(e))
        self.undo_stack.append((self._next, label, before, self._snap()))
        self._next += 1
        del self.undo_stack[:-self.limit]
        self.redo_stack.clear()
        return result

    def can_undo(self):
        return bool(self.undo_stack)

    def can_redo(self):
        return bool(self.redo_stack)

    def undo(self):
        if not self.undo_stack:
            return None
        item = self.undo_stack.pop()
        self._restore(item[2])
        self.refresh()
        self.redo_stack.append(item)
        return item[1]

    def redo(self):
        if not self.redo_stack:
            return None
        item = self.redo_stack.pop()
        self._restore(item[3])
        self.refresh()
        self.undo_stack.append(item)
        return item[1]

    # ---- looking
    def group(self, voice, index):
        """The pieces (note + holds) of the compiled note `tune.voices[voice][index]`, in time order."""
        note = self.tune.voices[voice][index]
        g = self.trace.groups(voice).get(id(note))
        if not g:
            raise EditError("that note is not in the song's events")
        return g

    def find(self, voice, start, pitch=None):
        """Index of the note starting at `start` (optionally with `pitch`) in the compiled voice, or None."""
        for i, n in enumerate(self.tune.voices[voice]):
            if n.start == start and (pitch is None or n.pitch == pitch):
                return i
        return None

    def shared(self, voice, index):
        """How many places play this note's events (a pattern called twice -> 2)."""
        g = self.group(voice, index)
        ev = g[0].event
        return sum(1 for p in self.trace.pieces[voice] if p.event is ev)

    # ---- editing
    def add_note(self, voice, start, dur, pitch):
        return self.change("add note", lambda: self._add(voice, start, dur, pitch))

    def erase(self, voice, index):
        return self.change("erase note", lambda: self._erase(voice, index))

    def set_pitch(self, voice, index, pitch):
        return self.change("change pitch", lambda: self._set_pitch(voice, index, pitch))

    def resize(self, voice, index, dur):
        return self.change("resize note", lambda: self._resize(voice, index, dur))

    def move(self, voice, index, start, pitch):
        return self.change("move note", lambda: self._move(voice, index, start, pitch))

    def transpose(self, notes, semitones):
        """`notes` is [(voice, index)].  All or nothing."""
        def run():
            todo = []
            for v, i in notes:
                n = self.tune.voices[v][i]
                todo.append((v, n.start, n.pitch + semitones))
            for v, start, pitch in todo:
                if not 0 <= pitch <= MAX_PITCH:
                    raise EditError("transposed out of range")
            for v, start, pitch in todo:
                self._set_pitch(v, self.find(v, start), pitch)
                # the first pass changed only pitches, so every other note is still where we noted it
            return None
        return self.change("transpose", run)

    # ---- instruments
    def instrument_names(self):
        """Every instrument name a voice can use: the song's own, then the library's."""
        return sorted({*self.song.instruments, *self.lib["instr"]})

    def instrument(self, name):
        return self.song.instruments.get(name) or self.lib["instr"].get(name)

    def _first_select(self, items, depth=0):
        for e in items:
            if isinstance(e, Select):
                return e
            if isinstance(e, Repeat) and depth < 8:
                r = self._first_select(e.items, depth + 1)
                if r is not None:
                    return r
            if isinstance(e, Call) and e.name in self.song.clips and depth < 8:
                r = self._first_select(self.song.clips[e.name].items, depth + 1)
                if r is not None:
                    return r
            if isinstance(e, (Note, Degree, Rest, Play, Extend)):
                return None
        return None

    def voice_instrument(self, voice):
        """The name the voice selects before its first sound, or None (it plays the default instrument)."""
        e = self._first_select(self.song.tracks.get(TRACKS[voice], ()))
        return e.name if e else None

    def edit_instrument(self, name, **fields):
        """Change fields of instrument `name` (for this song: a library instrument becomes the song's own copy)."""
        return self.change("edit instrument", lambda: self._edit(name, fields))

    def _edit(self, name, fields):
        base = self.instrument(name)
        if base is None:
            raise EditError("no instrument '%s'" % name)
        for k, v in fields.items():
            rule = FIELDS.get(k)
            if rule is None:
                raise EditError("unknown instrument field '%s'" % k)
            if isinstance(rule[0], str):
                if v not in rule:
                    raise EditError("%s is one of %s" % (k, ", ".join(w or "off" for w in rule)))
            elif not rule[0] <= v <= rule[1]:
                raise EditError("%s is %d to %d" % (k, rule[0], rule[1]))
        self.song.instruments[name] = dataclasses.replace(base, **fields)

    def edit_voice_instrument(self, voice, **fields):
        """Edit what the voice plays; a voice that selects no instrument first gets its own named copy of the
        default one.  One undo step."""
        def run():
            name = self.voice_instrument(voice)
            if name is None:
                name = self._copy(voice, None)
            self._edit(name, fields)
            return name
        return self.change("edit instrument", run)

    def set_voice_instrument(self, voice, name):
        def run():
            if self.instrument(name) is None:
                raise EditError("no instrument '%s'" % name)
            e = self._first_select(self.song.tracks.get(TRACKS[voice], ()))
            if e is not None:
                e.name = name
            else:
                self._track(voice).insert(0, Select(name))
        return self.change("change instrument", run)

    def copy_instrument(self, voice, new=None):
        """Give the voice its own copy of the instrument it plays, so editing it leaves other voices alone."""
        return self.change("copy instrument", lambda: self._copy(voice, new))

    def _copy(self, voice, new):
        old = self.voice_instrument(voice)
        base = self.instrument(old) if old else None
        if base is None:
            notes = self.tune.voices[voice]
            base = notes[0].inst if notes else Instrument()
        stem = (new or old or "voice%d" % (voice + 1))
        if not NAME.match(stem):
            raise EditError("an instrument name is letters, digits, - and _")
        name, n = stem, 1
        while new is None and (name in self.song.instruments or name in self.lib["instr"]):
            n += 1
            name = "%s%d" % (stem, n)
        if name in self.song.instruments or name in self.lib["instr"]:
            raise EditError("there is already an instrument called '%s'" % name)
        self.song.instruments[name] = dataclasses.replace(base)
        e = self._first_select(self.song.tracks.get(TRACKS[voice], ()))
        if e is not None:
            e.name = name
        else:
            self._track(voice).insert(0, Select(name))
        return name

    # ---- drum / sample lanes (D1 = lane 4, D2 = lane 5)
    SAMPLE_NAME = re.compile(r"[A-Za-z_]+$")

    def sample_names(self):
        """Every sample a D line can play: declared in the song, in the library, held in memory or in the folder."""
        names = set(self.song.samples) | set(self.lib.get("sample", {})) | set(self.lib.get("_mem", {}))
        folder = self.lib.get("_sampledir")
        if folder is not None:
            from pathlib import Path
            names |= {p.stem for p in Path(folder).glob("*.dpcm") if self.SAMPLE_NAME.match(p.stem)}
        return sorted(names)

    def import_sample(self, name, sample):
        """Make `sample` (a dpcm.Sample) available to this song as `name`."""
        if not self.SAMPLE_NAME.match(name or ""):
            raise EditError("a sample name is letters and _ only")
        def run():
            self.lib.setdefault("_mem", {})[name] = dataclasses.replace(sample, name=name)
            self.song.samples[name] = (name, None)
        return self.change("import sample", run)

    def find_hit(self, lane, start):
        for p in self.trace.pieces[lane]:
            if p.kind == "play" and p.start == start:
                return p
        return None

    def add_hit(self, lane, start, sample, dur):
        return self.change("add hit", lambda: self._add_hit(lane, start, sample, dur))

    def erase_hit(self, lane, start):
        return self.change("erase hit", lambda: self._erase_hit(lane, start))

    def move_hit(self, lane, start, new_start):
        def run():
            p = self.find_hit(lane, start)
            if p is None:
                raise EditError("no hit there")
            sample, dur = p.event.sample, p.dur
            self._erase_hit(lane, start)
            self._add_hit(lane, new_start, sample, dur)
        return self.change("move hit", run)

    def set_hit_sample(self, lane, start, sample):
        def run():
            p = self.find_hit(lane, start)
            if p is None:
                raise EditError("no hit there")
            p.event.sample = sample
        return self.change("change sample", run)

    def _add_hit(self, lane, start, sample, dur):
        if dur < 1 or start < 0:
            raise EditError("bad hit")
        self._check(dur)
        end = start + dur
        ev = Play(sample, dur)
        if start >= self.trace.ends[lane]:
            gap = start - self.trace.ends[lane]
            self._check(gap)
            self._track(lane).extend(self._rests(gap, None) + [ev])
        else:
            run = self._rest_run(lane, start, end)
            if run is None:
                raise EditError("no room there")
            first = run[0]
            a, b = start - first.start, run[-1].start + run[-1].dur - end
            self._check(a, b)
            c = first.container
            i = _index(c, first.event)
            c[i:i + len(run)] = self._rests(a, None) + [ev] + self._rests(b, None)
        self.refresh()

    def _erase_hit(self, lane, start):
        p = self.find_hit(lane, start)
        if p is None:
            raise EditError("no hit there")
        self._check(p.dur)
        self._replace(p.container, p.event, [Rest(p.dur)])
        self._merge_rests([p.container])
        self.refresh()

    # ---- the pieces of an edit
    def _track(self, voice):
        return self.song.tracks.setdefault(TRACKS[voice], [])

    def _check(self, *ticks):
        for t in ticks:
            if t and not writable(t):
                raise EditError("a length of %d ticks cannot be written in MML" % t)

    def _rests(self, n, container):
        return [Rest(n)] if n else []

    def _replace(self, container, event, new):
        i = _index(container, event)
        container[i:i + 1] = new

    def _add(self, voice, start, dur, pitch):
        if not 0 <= pitch <= MAX_PITCH:
            raise EditError("pitch out of range")
        if dur < 1 or start < 0:
            raise EditError("bad note")
        self._check(dur)
        end = start + dur
        if start >= self.trace.ends[voice]:
            gap = start - self.trace.ends[voice]
            self._check(gap)
            before, ev, after = spell(pitch, self.trace.final[voice])
            ev.dur = dur
            self._track(voice).extend(self._rests(gap, None) + before + [ev] + after)
        else:
            run = self._rest_run(voice, start, end)
            if run is None:
                raise EditError("no room there")
            first = run[0]
            a, b = start - first.start, run[-1].start + run[-1].dur - end
            self._check(a, b)
            before, ev, after = spell(pitch, first.state)
            ev.dur = dur
            c = first.container
            i = _index(c, first.event)
            c[i:i + len(run)] = self._rests(a, None) + before + [ev] + after + self._rests(b, None)
        self.refresh()
        return start, pitch

    def _rest_run(self, voice, start, end):
        """Rest pieces, side by side in one list, that cover start..end (or None)."""
        first_of = {}
        for p in self.trace.pieces[voice]:
            if p.kind == "rest":
                first_of.setdefault(id(p.event), p)
        for p in self.trace.pieces[voice]:
            if p.kind != "rest" or p.start > start or p.start + p.dur <= start:
                continue
            run, c = [p], p.container
            i = _index(c, p.event)
            while run[-1].start + run[-1].dur < end and i + 1 < len(c) and id(c[i + 1]) in first_of:
                i += 1
                q = first_of[id(c[i])]
                if q.start != run[-1].start + run[-1].dur:
                    break
                run.append(q)
            if run[-1].start + run[-1].dur >= end:
                return run
        return None

    def _erase(self, voice, index):
        g = self.group(voice, index)
        for p in g:
            self._check(p.dur)
            if p.kind == "note":
                _unwrap(p.container, p.event)
            self._replace(p.container, p.event, [Rest(p.dur)])
        self._merge_rests({id(p.container): p.container for p in g}.values())
        self.refresh()

    def _merge_rests(self, containers):
        for c in containers:
            i = 0
            while i + 1 < len(c):
                a, b = c[i], c[i + 1]
                if isinstance(a, Rest) and isinstance(b, Rest) and isinstance(a.dur, int) and isinstance(b.dur, int) \
                        and writable(a.dur + b.dur):
                    a.dur += b.dur
                    del c[i + 1]
                else:
                    i += 1

    def _set_pitch(self, voice, index, pitch):
        if not 0 <= pitch <= MAX_PITCH:
            raise EditError("pitch out of range")
        g = self.group(voice, index)
        done = set()
        for p in g:
            if p.kind != "note" or id(p.event) in done:
                continue
            done.add(id(p.event))
            before, ev, after = spell(pitch, p.state)
            ev.dur = p.event.dur if isinstance(p.event.dur, int) else p.dur
            _unwrap(p.container, p.event)
            self._replace(p.container, p.event, before + [ev] + after)
        self.refresh()

    def _room(self, voice, g):
        """(rests that follow the note in its own list, can grow forever) for growing a note."""
        last = g[-1]
        c = last.container
        i = _index(c, last.event) + 1
        durs = {id(p.event): p.dur for p in self.trace.pieces[voice] if p.kind == "rest"}
        rests = []
        while i < len(c):
            e = c[i]
            if isinstance(e, Rest) and id(e) in durs:
                rests.append(e)
            elif type(e).__name__ in ("Bar", "Comment"):
                pass
            else:
                return rests, False
            i += 1
        return rests, c is self.song.tracks.get(TRACKS[voice])

    def max_dur(self, voice, index):
        g = self.group(voice, index)
        total = sum(p.dur for p in g)
        rests, forever = self._room(voice, g)
        if forever:
            return total + GROW_CAP
        durs = {id(p.event): p.dur for p in self.trace.pieces[voice] if p.kind == "rest"}
        return total + sum(durs[id(r)] for r in rests)

    def _resize(self, voice, index, dur):
        g = self.group(voice, index)
        total = sum(p.dur for p in g)
        dur = int(dur)
        if dur < 1:
            raise EditError("a note needs a length")
        self._check(dur)
        if dur == total:
            return total
        if dur < total:
            remaining = total - dur
            for k in range(len(g) - 1, -1, -1):
                if remaining <= 0:
                    break
                p = g[k]
                if k > 0 and p.dur <= remaining:
                    self._replace(p.container, p.event, [Rest(p.dur)])
                    remaining -= p.dur
                    continue
                cut = min(remaining, p.dur - 1) if k == 0 else remaining
                if cut:
                    self._check(cut, p.dur - cut)
                    _set_ticks(p.event, p.dur - cut)
                    self._replace(p.container, p.event, [p.event, Rest(cut)])
                    remaining -= cut
            self._merge_rests({id(p.container): p.container for p in g}.values())
        else:
            need = dur - total
            last = g[-1]
            rests, forever = self._room(voice, g)
            durs = {id(p.event): p.dur for p in self.trace.pieces[voice] if p.kind == "rest"}
            got = 0
            for r in rests:
                if got >= need:
                    break
                take = min(durs[id(r)], need - got)
                left = durs[id(r)] - take
                self._check(left)
                if left:
                    r.dur = left
                else:
                    last.container.remove(r)
                got += take
            if got < need:
                if not forever:
                    raise EditError("not enough room after the note")
                got = need
            _set_ticks(last.event, last.dur + got)
        self.refresh()
        return dur

    def _move(self, voice, index, start, pitch):
        n = self.tune.voices[voice][index]
        dur = n.dur
        self._erase(voice, index)
        self._add(voice, start, dur, pitch)
        return start, pitch
