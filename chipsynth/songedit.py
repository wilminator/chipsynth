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
from .song import (Note, Degree, Rest, Extend, Octave, Select, Repeat, Call, Play, Tempo, Loop, Trace, Clip, TRACKS,
                   SongError, Volume, Tweak, Key)
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
# per-note effects: name -> (vibrato (rate, depth), arpeggio offsets, slide); the tracker cycles through these
EFFECTS = {"none": ((0, 0), (), 0), "vibrato": ((5, 3), (), 0), "slide": ((0, 0), (), 12),
           "arpeggio": ((0, 0), (0, 4, 7), 0), "arpeggio minor": ((0, 0), (0, 3, 7), 0)}
EFFECT_MARK = {"none": "", "vibrato": "~", "slide": "/", "arpeggio": "^", "arpeggio minor": "^m"}


def effect_of(note):
    """The name in EFFECTS that a compiled note plays with (slide, then arpeggio, then vibrato; else "none")."""
    i = note.inst
    if note.slide:
        return "slide"
    if i.arp:
        return "arpeggio minor" if tuple(i.arp) == EFFECTS["arpeggio minor"][1] else "arpeggio"
    if i.vib_rate and i.vib_depth:
        return "vibrato"
    return "none"


def _fx_sig(n):
    return (n.inst.vib_rate, n.inst.vib_depth, tuple(n.inst.arp), n.slide)


def _fx_events(have, want):
    """Tweaks that turn the effect state `have` into `want` (each is (vib rate, vib depth, arp, slide))."""
    out = []
    if have[:2] != want[:2]:
        out.append(Tweak("vib", "%d,%d" % want[:2] if any(want[:2]) else "off"))
    if have[2] != want[2]:
        out.append(Tweak("arp", ",".join(str(x) for x in want[2]) if want[2] else "off"))
    if have[3] != want[3]:
        out.append(Tweak("slide", str(want[3])))
    return out


def _lead(c, ev):
    """Where in `c` events that come before the note `ev` go (before the `<` that shifts its octave, if any)."""
    i = _index(c, ev)
    return i - 1 if i > 0 and _is_shift(c[i - 1]) else i
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
            return a.shift
    return 0


def _walk(items):
    for e in items:
        yield e
        body = getattr(e, "items", None) or getattr(e, "body", None)
        if isinstance(body, list):
            yield from _walk(body)


def _is_shift(e):
    return isinstance(e, Octave) and e.set is None and e.boundary is None and e.shift != 0


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

    # ---- several notes at once (a selection is [(voice, start)], so it survives the notes being renumbered)
    def selection_notes(self, selection):
        """[(voice, start, dur, pitch)] of the selected notes that exist, in order."""
        out = []
        for v, start in sorted(selection):
            i = self.find(v, start)
            if i is not None:
                n = self.tune.voices[v][i]
                out.append((v, n.start, n.dur, n.pitch))
        return out

    def _erase_at(self, voice, start):
        i = self.find(voice, start)
        if i is None:
            raise EditError("a selected note is gone")
        self._erase(voice, i)

    def _clear(self, voice, t0, t1, keep=()):
        """Make room: remove what sounds in [t0, t1) of a voice.  A note that began earlier is cut at t0; one that runs
        past t1 keeps its tail."""
        for n in [n for n in self.tune.voices[voice] if n.start < t1 and n.start + n.dur > t0
                  and (voice, n.start) not in keep]:
            start, dur, pitch = n.start, n.dur, n.pitch
            self._erase_at(voice, start)
            if start < t0:
                self._add(voice, start, t0 - start, pitch)
            if start + dur > t1:
                self._add(voice, t1, start + dur - t1, pitch)

    def move_notes(self, selection, dtick=0, dpitch=0):
        """Move the selected notes together.  All or nothing.  Returns the new selection."""
        def run():
            notes = self.selection_notes(selection)
            if not notes:
                raise EditError("nothing selected")
            for v, start, dur, pitch in notes:
                if start + dtick < 0 or not 0 <= pitch + dpitch <= MAX_PITCH:
                    raise EditError("that would move a note out of range")
            for v, start, dur, pitch in notes:
                self._erase_at(v, start)
            for v, start, dur, pitch in notes:
                self._clear(v, start + dtick, start + dtick + dur)
                self._add(v, start + dtick, dur, pitch + dpitch)
            return [(v, start + dtick) for v, start, dur, pitch in notes]
        return self.change("move notes", run)

    def erase_notes(self, selection):
        def run():
            notes = self.selection_notes(selection)
            if not notes:
                raise EditError("nothing selected")
            for v, start, dur, pitch in notes:
                self._erase_at(v, start)
        return self.change("erase notes", run)

    def quantize(self, selection, grid):
        """Snap the starts (and lengths) of the selected notes to `grid` ticks.  Returns the new selection."""
        def run():
            notes = self.selection_notes(selection)
            if not notes:
                raise EditError("nothing selected")
            todo = [(v, max(0, int(round(start / grid)) * grid), max(grid, int(round(dur / grid)) * grid), pitch, start)
                    for v, start, dur, pitch in notes]
            for v, ns, nd, pitch, old in todo:
                self._erase_at(v, old)
            for v, ns, nd, pitch, old in todo:
                self._clear(v, ns, ns + nd)
                self._add(v, ns, nd, pitch)
            return [(v, ns) for v, ns, nd, pitch, old in todo]
        return self.change("quantize", run)

    def paste_notes(self, clip, tick, voice_shift=0):
        """`clip` is [(voice, start, dur, pitch)] (from selection_notes).  The first note lands on `tick`; whatever
        was sounding underneath is replaced.  Returns the new selection."""
        def run():
            if not clip:
                raise EditError("nothing to paste")
            base = min(c[1] for c in clip)
            out = []
            for v, start, dur, pitch in clip:
                v2 = v + voice_shift
                if not 0 <= v2 <= 3:
                    raise EditError("there is no such voice")
                t = tick + start - base
                self._clear(v2, t, t + dur)
                self._add(v2, t, dur, pitch)
                out.append((v2, t))
            return out
        return self.change("paste", run)

    def put_note(self, voice, tick, pitch, dur):
        """Tracker-style entry: sound `pitch` at `tick` for up to `dur` ticks.  A note that starts there changes
        pitch (keeping its length); a note running through `tick` is cut short there; the new note stops where the
        next one starts.  Returns the length used."""
        def run():
            notes = self.tune.voices[voice]
            hit = next((i for i, n in enumerate(notes) if n.start == tick), None)
            if hit is not None:
                self._set_pitch(voice, hit, pitch)
                return notes[hit].dur
            cover = next((i for i, n in enumerate(notes) if n.start < tick < n.start + n.dur), None)
            if cover is not None:
                self._resize(voice, cover, tick - notes[cover].start)
            nxt = min((n.start for n in self.tune.voices[voice] if n.start > tick), default=None)
            use = dur if nxt is None else min(dur, nxt - tick)
            if use < 1:
                raise EditError("no room there")
            while use > 1 and not writable(use):
                use -= 1
            self._add(voice, tick, use, pitch)
            return use
        return self.change("enter note", run)

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

    # ---- per-note volume and effects
    def set_volume(self, voice, index, vol):
        """Give one note its own volume (0-15).  A `v` is written just before the note and the next note whose volume
        would have changed is given its old volume back, so only this note differs."""
        return self.change("note volume", lambda: self._set_volume(voice, index, vol))

    def set_effect(self, voice, index, effect):
        """Give one note an effect from EFFECTS ("none" turns off whatever the instrument would add)."""
        if effect not in EFFECTS:
            raise EditError("no such effect: %s" % effect)
        return self.change("note effect", lambda: self._set_effect(voice, index, effect))

    def _pin(self, voice, before, sig, fixer, skip):
        """Insert fixes until every note not in `skip` has the state it had in `before` ({start: state})."""
        for _ in range(32):
            bad = None
            for i, n in enumerate(self.tune.voices[voice]):
                if n.start in before and sig(n) != before[n.start]:
                    g = self.group(voice, i)
                    if id(g[0].event) not in skip:
                        bad = (g, n)
                        break
            if bad is None:
                return
            g, n = bad
            c, ev = g[0].container, g[0].event
            i = _lead(c, ev)
            c[i:i] = fixer(n, before[n.start])
            self.refresh()
        raise EditError("could not keep the other notes as they were")

    def _target_events(self, voice, index):
        g = self.group(voice, index)
        out, seen = [], set()
        for p in g:
            if p.kind == "note" and id(p.event) not in seen:
                seen.add(id(p.event))
                out.append(p)
        return g, out, {id(p.event) for p in g}

    def _set_volume(self, voice, index, vol):
        if not 0 <= vol <= 15:
            raise EditError("volume is 0 to 15")
        if self.tune.voices[voice][index].vol == vol:
            return vol
        before = {n.start: n.vol for n in self.tune.voices[voice]}
        g, todo, skip = self._target_events(voice, index)
        for p in todo:
            c = p.container
            i = _lead(c, p.event)
            if i > 0 and isinstance(c[i - 1], Volume) and c[i - 1].value is not None:
                c[i - 1].value = vol
            else:
                c.insert(i, Volume(value=vol))
        self.refresh()
        self._pin(voice, before, lambda n: n.vol, lambda n, want: [Volume(value=want)], skip)
        return vol

    def _set_effect(self, voice, index, effect):
        vib, arp, slide = EFFECTS[effect]
        want = (vib[0], vib[1], arp, slide)
        if _fx_sig(self.tune.voices[voice][index]) == want:
            return effect
        before = {n.start: _fx_sig(n) for n in self.tune.voices[voice]}
        have = before[self.tune.voices[voice][index].start]
        g, todo, skip = self._target_events(voice, index)
        for p in todo:
            c = p.container
            i = _lead(c, p.event)
            new = _fx_events(have, want)
            words = {t.word for t in new}
            while i > 0 and isinstance(c[i - 1], Tweak) and c[i - 1].word in words:       # replace our own earlier ones
                del c[i - 1]
                i -= 1
            c[i:i] = new
        self.refresh()
        self._pin(voice, before, _fx_sig, lambda n, w: _fx_events(_fx_sig(n), w), skip)
        return effect

    # ---- the key
    def transpose_in_key(self, notes, steps, tonic, scale):
        """Move `notes` ([(voice, index)]) by `steps` degrees of the key `tonic` (0-11) `scale` (a name in mml.SCALES);
        a note outside the scale keeps its distance above the degree below it.  All or nothing."""
        steps_of = mml.SCALES.get(scale)
        if steps_of is None:
            raise EditError("no such scale: %s" % scale)
        n = len(steps_of)

        def move(p):
            octv, pc = divmod(p - tonic, 12)
            d = max(k for k in range(n) if steps_of[k] <= pc)
            k = d + steps
            return tonic + 12 * (octv + k // n) + steps_of[k % n] + (pc - steps_of[d])

        def run():
            todo = [(v, self.tune.voices[v][i].start, move(self.tune.voices[v][i].pitch)) for v, i in notes]
            for v, start, pitch in todo:
                if not 0 <= pitch <= MAX_PITCH:
                    raise EditError("transposed out of range")
            for v, start, pitch in todo:
                self._set_pitch(v, self.find(v, start), pitch)
        return self.change("transpose in key", run)

    def key_events(self):
        """Every `key` (not `key off`) event in the song's tracks and clips."""
        lists = list(self.song.tracks.values()) + [c.items for c in self.song.clips.values()]
        return [e for items in lists for e in _walk(items) if isinstance(e, Key) and e.tonic is not None]

    def set_key(self, tonic, scale, keep_pitch=True):
        """Change the song's `key` lines to `tonic` `scale`.  The notes are written as degrees of the key, so with
        keep_pitch they are re-spelled to sound exactly as before; without it they move with the key (a modulation).
        Returns how many key lines changed (0: the song writes letter notes, nothing to do)."""
        if scale not in mml.SCALES and scale not in self.song.keys:
            raise EditError("no such scale: %s" % scale)

        def run():
            keys = self.key_events()
            if not keys:
                return 0
            before = [[(n.start, n.pitch) for n in v] for v in self.tune.voices]
            for k in keys:
                k.tonic, k.scale = tonic % 12, scale
            self.refresh()
            if keep_pitch:
                for v in range(4):
                    for start, pitch in before[v]:
                        i = self.find(v, start)
                        if i is not None and self.tune.voices[v][i].pitch != pitch:
                            self._set_pitch(v, i, pitch)
                for v in range(4):
                    if [(n.start, n.pitch) for n in self.tune.voices[v]] != before[v]:
                        raise EditError("some notes cannot be kept at their pitch in that key")
            return len(keys)
        return self.change("change key", run)

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

    # ---- song settings, tempo map, loop point
    def set_song(self, **fields):
        """title, author, comment, bpm (30-300), time ((n, d)), detune (cents), once, echo ((secs, fb, mix, wide) or
        None) and reverb (0-15)."""
        def run():
            for k, v in fields.items():
                if k in ("title", "author", "comment"):
                    if not isinstance(v, str) or "\n" in v:
                        raise EditError("%s is one line of text" % k)
                elif k == "bpm":
                    if not 30 <= v <= 300:
                        raise EditError("tempo is 30 to 300")
                elif k == "time":
                    n, d = v
                    if not (1 <= n <= 32 and d in (1, 2, 4, 8, 16, 32)):
                        raise EditError("time must look like 4/4 or 6/8")
                    v = (int(n), int(d))
                elif k == "detune":
                    if not -1200 <= v <= 1200:
                        raise EditError("tuning is -1200 to 1200 cents")
                elif k == "once":
                    v = bool(v)
                elif k == "reverb":
                    if not 0 <= v <= 15:
                        raise EditError("reverb is 0 to 15")
                elif k == "echo":
                    if v is not None:
                        secs, fb, mix, wide = round(v[0], 3), round(v[1], 2), round(v[2], 2), v[3]
                        if not (0.02 <= secs <= 1.0 and 0 <= fb <= 0.9 and 0 <= mix <= 1.0):
                            raise EditError("echo is 20-1000 ms, feedback 0-90 %, mix 0-100 %")
                        v = (secs, fb, mix, bool(wide))
                else:
                    raise EditError("unknown song setting '%s'" % k)
                setattr(self.song, k, v)
        return self.change("song settings", run)

    def tempo_marks(self):
        """[(tick, bpm)] of the tempo changes in the song (the starting tempo is song.bpm)."""
        seen = {}
        for lane in range(6):
            for p in self.trace.pieces[lane]:
                if p.kind == "tempo":
                    seen.setdefault(p.start, p.event.bpm)
        return sorted(seen.items())

    def loop_at(self):
        """The tick of the loop marker the compiler used, or None."""
        for lane in range(6):
            for p in self.trace.pieces[lane]:
                if p.kind == "loop":
                    return p.start
        return None

    def set_tempo_at(self, tick, bpm):
        if not 30 <= bpm <= 300:
            raise EditError("tempo is 30 to 300")
        def run():
            if tick <= 0:
                self.song.bpm = bpm
                return
            for lane in range(6):
                for p in self.trace.pieces[lane]:
                    if p.kind == "tempo" and p.start == tick:
                        p.event.bpm = bpm
                        return
            self._insert_at(0, tick, Tempo(bpm))
        return self.change("tempo", run)

    def remove_tempo(self, tick):
        def run():
            hit = False
            for lane in range(6):
                for p in list(self.trace.pieces[lane]):
                    if p.kind == "tempo" and p.start == tick and p.event in p.container:
                        p.container.remove(p.event)
                        hit = True
            if not hit:
                raise EditError("no tempo change there")
        return self.change("remove tempo", run)

    def set_loop(self, tick):
        """Loop from `tick` (the start of a bar; the tune moves it back to one otherwise).  None removes the loop."""
        def run():
            self._remove_events(Loop)
            if tick:
                self.refresh()
                self._insert_at(0, tick, Loop())
        return self.change("loop point", run)

    def _remove_events(self, cls):
        def strip(items):
            items[:] = [e for e in items if not isinstance(e, cls)]
            for e in items:
                if isinstance(e, Repeat):
                    strip(e.items)
        for items in self.song.tracks.values():
            strip(items)
        for clip in self.song.clips.values():
            if clip.raw is None:
                strip(clip.items)

    def _insert_at(self, lane, tick, event):
        """Put a zero-length event (tempo, loop marker) at `tick` of a voice's own list, splitting a rest if needed."""
        top = self._track(lane)
        if tick >= self.trace.ends[lane]:
            gap = tick - self.trace.ends[lane]
            self._check(gap)
            top.extend(self._rests(gap, None) + [event])
            return
        for p in self.trace.pieces[lane]:
            if p.container is top and p.dur > 0 and p.start == tick:
                top.insert(_index(top, p.event), event)
                return
        firsts = {}
        for p in self.trace.pieces[lane]:
            if p.stack and p.stack[0][0] is top and p.dur > 0:
                ev = p.stack[0][1]
                firsts[id(ev)] = (min(p.start, firsts.get(id(ev), (p.start, ev))[0]), ev)
        for start, ev in firsts.values():
            if start == tick:                        # between a pattern call / repeat and what came before it
                top.insert(_index(top, ev), event)
                return
        for p in self.trace.pieces[lane]:
            if p.container is top and p.kind == "rest" and p.start < tick < p.start + p.dur:
                a, b = tick - p.start, p.start + p.dur - tick
                self._check(a, b)
                self._replace(top, p.event, [Rest(a), event, Rest(b)])
                return
        raise EditError("that spot is inside a note or a pattern: put the marker between notes (or use Make unique first)")

    # ---- patterns (clips)
    def clip_uses(self):
        """{clip name: number of places that call it}."""
        uses = {name: 0 for name in self.song.clips}
        def scan(items):
            for e in items:
                if isinstance(e, Call) and e.name in uses:
                    uses[e.name] += 1
                elif isinstance(e, Repeat):
                    scan(e.items)
        for items in self.song.tracks.values():
            scan(items)
        for clip in self.song.clips.values():
            if clip.raw is None:
                scan(clip.items)
        return uses

    def delete_clip(self, name):
        def run():
            if self.clip_uses().get(name, 1):
                raise EditError("pattern '%s' is still used" % name)
            del self.song.clips[name]
        return self.change("delete pattern", run)

    def rename_clip(self, old, new):
        def run():
            if not NAME.match(new or ""):
                raise EditError("a pattern name is letters, digits, - and _")
            if old not in self.song.clips or new in self.song.clips:
                raise EditError("there is already a pattern called '%s'" % new if new in self.song.clips else "no pattern '%s'" % old)
            clip = self.song.clips.pop(old)
            clip.name = new
            self.song.clips[new] = clip
            def fix(items):
                for e in items:
                    if isinstance(e, Call) and e.name == old:
                        e.name = new
                    elif isinstance(e, Repeat):
                        fix(e.items)
            for items in self.song.tracks.values():
                fix(items)
            for c in self.song.clips.values():
                if c.raw is None:
                    fix(c.items)
        return self.change("rename pattern", run)

    def inside(self, voice, index):
        """What the note is part of, innermost first: a list of ('pattern', name) / ('repeat', count)."""
        out = []
        for container, ev in reversed(self.group(voice, index)[0].stack):
            out.append(("pattern", ev.name) if isinstance(ev, Call) else ("repeat", ev.count))
        return out

    def make_unique(self, voice, index):
        """Replace the pattern call (or repeat) this note is inside with its own copy of the events, so editing the
        note changes only this place."""
        def run():
            stack = self.group(voice, index)[0].stack
            if not stack:
                raise EditError("this note is not in a pattern or a repeat")
            container, ev = stack[-1]
            if isinstance(ev, Call):
                clip = self.song.clips.get(ev.name)
                new = copy.deepcopy(clip.items)
            else:
                new = [copy.deepcopy(x) for _ in range(ev.count) for x in ev.items]
            self._replace(container, ev, new)
        return self.change("make unique", run)

    def make_pattern(self, selection, name):
        """Move the selected notes of one voice (and everything between the first and the last) into a new pattern
        `name`, leaving a call to it where they were.  The song must sound exactly the same afterwards, or nothing
        changes.  Returns the name."""
        def run():
            if not NAME.match(name or ""):
                raise EditError("a pattern name is letters, digits, - and _")
            if name in self.song.clips:
                raise EditError("there is already a pattern called '%s'" % name)
            notes = self.selection_notes(selection)
            if not notes:
                raise EditError("nothing selected")
            if len({n[0] for n in notes}) > 1:
                raise EditError("select notes of one voice to make a pattern")
            voice = notes[0][0]
            if voice > 3:
                raise EditError("patterns are made from the melodic voices")
            t0, t1 = min(n[1] for n in notes), max(n[1] + n[2] for n in notes)
            pieces = [p for p in self.trace.pieces[voice] if t0 <= p.start < t1]
            if any(p.stack for p in pieces):
                raise EditError("part of this is already inside a pattern or a repeat")
            if any(p.kind not in ("note", "extend", "rest") for p in pieces):
                raise EditError("a tempo or loop marker is in the way")
            if len({id(p.container) for p in pieces}) != 1:
                raise EditError("those notes are not in one place in the song")
            c = pieces[0].container
            lo, hi = _index(c, pieces[0].event), _index(c, pieces[-1].event)
            while (lo > 0 and hi < len(c) - 1 and _is_shift(c[lo - 1]) and _is_shift(c[hi + 1])
                   and c[lo - 1].shift == -c[hi + 1].shift):
                lo, hi = lo - 1, hi + 1
            before = [[(n.start, n.dur, n.pitch) for n in v] for v in self.tune.voices]
            self.song.clips[name] = Clip(name, c[lo:hi + 1])
            c[lo:hi + 1] = [Call(name)]
            self.refresh()
            if [[(n.start, n.dur, n.pitch) for n in v] for v in self.tune.voices] != before:
                raise EditError("that stretch cannot become a pattern without changing the sound")
            return name
        return self.change("make pattern", run)

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
            a, b = start - first.start, max(0, run[-1].start + run[-1].dur - end)
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
            a, b = start - first.start, max(0, run[-1].start + run[-1].dur - end)
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
            last = run[-1]
            at_end = (last.start + last.dur == self.trace.ends[voice] and c is self.song.tracks.get(TRACKS[voice])
                      and c and c[-1] is last.event)
            if last.start + last.dur >= end or at_end:      # a rest that ends the voice can be written over past its end
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
            ev_dur = p.event.dur if isinstance(p.event.dur, int) else p.dur
            shift = _unwrap(p.container, p.event)             # the state we were given includes that shift
            before, ev, after = spell(pitch, (p.state[0] - shift, p.state[1], p.state[2]))
            ev.dur = ev_dur
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
