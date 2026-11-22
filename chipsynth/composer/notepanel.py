# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The note panel (E, or the Effects and key button): per-note effects and key changes, in the same small options dialog
the File menu uses (Up / Down pick a row, Left / Right or a click change it, Enter applies, Esc cancels, Delete clears the
effects).  The rows are built here from the Editor's own numbers; the edit itself is `Editor.edit_notes`, one undo step."""
import pygame

from chipsynth import mml

from . import model as M, tracker as TR

KEEP = "keep"                                   # a row's choice for "leave each note as it is" (notes that differ)

VIBRATO = [((0, 0), "off"), ((5, 2), "light  5,2"), ((5, 3), "medium  5,3"), ((6, 6), "deep  6,6"), ((9, 3), "fast  9,3"),
           ((4, 10), "slow and wide  4,10")]
ARPEGGIO = [((), "off"), ((0, 4, 7), "major  0,4,7"), ((0, 3, 7), "minor  0,3,7"), ((0, 12), "octaves  0,12"),
            ((0, 4, 7, 12), "major + octave  0,4,7,12"), ((0, 3, 7, 12), "minor + octave  0,3,7,12"),
            ((0, 4, 7, 10), "dominant 7th  0,4,7,10"), ((0, 7), "fifth  0,7"), ((0, 5, 7), "sus4  0,5,7")]
ARP_SPEEDS = [(n, "%d steps a beat" % n) for n in (2, 3, 4, 6, 8, 12, 16, 24, 32)]
SLIDES = [(0, "off"), (6, "short  6"), (12, "16th note  12"), (24, "8th note  24"), (48, "quarter note  48"),
          (96, "half note  96")]
SWEEPS = [((0, 0, 0), "off"), ((25, 75, 3), "25-75%  slow"), ((25, 75, 6), "25-75%  medium"), ((10, 90, 10), "10-90%  fast"),
          ((40, 60, 4), "40-60%  gentle"), ((50, 10, 6), "50-10%  medium")]

# the effect rows: (field, label, presets, how a value that is not a preset reads)
EFFECT_ROWS = (
    ("vib", "Vibrato", VIBRATO, lambda v: "custom  %d,%d" % v),
    ("arp", "Arpeggio", ARPEGGIO, lambda v: "custom  " + ",".join(str(x) for x in v)),
    ("arprate", "  speed", ARP_SPEEDS, lambda v: "%d steps a beat" % v),
    ("slide", "Slide", SLIDES, lambda v: "custom  %d" % v),
    ("pws", "Pulse sweep", SWEEPS, lambda v: "custom  %d-%d%%  rate %d" % v),
)
FX_FIELDS = tuple(r[0] for r in EFFECT_ROWS)


def choices_for(presets, current, text):
    """([(value, text)], start index) for a row whose notes have `current` values (a set).  Notes that differ start on KEEP;
    a value that is none of the presets is added as a custom one so it can be kept."""
    choices = list(presets)
    if len(current) != 1:
        return [(KEEP, "keep each note's own")] + choices, 0
    cur = next(iter(current))
    for i, (v, _) in enumerate(choices):
        if v == cur:
            return choices, i
    choices.append((cur, text(cur)))
    return choices, len(choices) - 1


def effect_rows(settings):
    """Dialog rows for the effects of notes whose `Editor.note_effects` are `settings`: [(key, label, choices, index)]."""
    rows = []
    for field, label, presets, text in EFFECT_ROWS:
        choices, i = choices_for(presets, {s[field] for s in settings}, text)
        rows.append((field, label, choices, i))
    return rows


def key_rows(mark_here, current, scales, voice_name, many):
    """Dialog rows for the key change at the panel's tick.  `mark_here`: a key event already starts at this tick;
    `current`: the KeyMark in force there (or None); `scales`: scale names; `voice_name`: the voice the panel is for."""
    modes = [("none", "leave as it is"), ("set", "set the key below")]
    if mark_here:
        modes.append(("remove", "remove the key change here"))
    modes.append(("off", "key off (letter notes)"))
    tonic = current.tonic if current is not None and current.tonic is not None else 0
    scale = current.scale if current is not None and current.scale in scales else "major"
    rows = [("keymode", "Key change", modes, 0),
            ("tonic", "  key", [(i, M.NOTE_NAMES[i]) for i in range(12)], tonic),
            ("scale", "  scale", [(s, s) for s in scales], scales.index(scale))]
    if many:
        rows.append(("voices", "  applies to", [("this", "%s only" % voice_name), ("all", "all voices")], 0))
    return rows


def effects_from(rows, values):
    """{field: value} of the effect rows the person changed (a row left where it started is not an edit)."""
    out = {}
    for key, label, choices, start in rows:
        if key in FX_FIELDS and values[key] != choices[start][0] and values[key] != KEEP:
            out[key] = values[key]
    return out


def key_from(values, tick, voice, all_voices):
    """The `key=` dict for Editor.edit_notes from the dialog's values, or None when the key is to be left alone.
    `all_voices` are the voices an "all voices" choice means (the ones that play at `tick`)."""
    mode = values.get("keymode", "none")
    if mode == "none":
        return None
    voices = list(all_voices) if values.get("voices") == "all" else [voice]
    if mode == "remove":
        return dict(tick=tick, voices=voices, remove=True)
    if mode == "off":
        return dict(tick=tick, tonic=None, scale=None, voices=voices)
    return dict(tick=tick, tonic=values["tonic"], scale=values["scale"], voices=voices)


def describe_key(mark):
    if mark is None:
        return "none"
    if mark.tonic is None:
        return "off"
    return "%s %s" % (M.NOTE_NAMES[mark.tonic], mark.scale)


class NotePanel:
    """Mixed into the Composer."""

    def panel_targets(self):
        """The notes the panel's effects go on: the selection, or in the tracker the note at the cursor.  [(voice, start)]."""
        if self.roll is None:
            return []
        sel = {(v, s) for v, s in self.selection() if v < self.song.voices}
        if not sel and self.mode == "tracker" and self.tr.col < self.roll.nv:
            i = TR.note_at(self.roll, self.tr.col, self.tr.row * self.snap_ticks)
            if i is not None:
                sel.add((self.tr.col, self.roll.notes[self.tr.col][i].start))
        return sorted(sel, key=lambda x: (x[1], x[0]))

    def open_note_panel(self):
        """E / the Effects and key button: open the dialog for the selected notes (or just the key change at the Play-from
        marker when nothing is selected)."""
        if self.ed is None or self.roll is None:
            return False
        targets = self.panel_targets()
        ed = self.ed
        if targets:
            voice, tick = targets[0][0], targets[0][1]
        else:
            voice = self.sel_voice if self.sel_voice < self.song.voices else 0
            tick = self.cursor_tick
        rows = []
        settings = [ed.note_effects(v, ed.find(v, s)) for v, s in targets if ed.find(v, s) is not None]
        if settings:
            rows += effect_rows(settings)
        scales = list(mml.SCALES) + [k for k in self.song.keys if k not in mml.SCALES]
        here = any(m.voice == voice and m.tick == tick for m in ed.key_marks())
        current = ed.key_at(voice, tick)
        rows += key_rows(here, current, scales, self.roll.names[voice], self.song.voices > 1)
        bt = max(1, self.tune.bar_ticks)
        bar, beat = divmod(tick, bt)
        note = ("%d note%s picked: the effects go on %s only. " % (len(targets), "" if len(targets) == 1 else "s",
                                                                  "it" if len(targets) == 1 else "them") if targets
                else "No notes picked, so only the key can change (click a note first for effects). ")
        note += "Key at bar %d, beat %.2f of %s (now %s)." % (bar + 1, beat / M.BEAT + 1, self.roll.names[voice], describe_key(current))
        if targets:
            note += " Pulse sweeps sound on the pulse wave. Delete clears the effects."
        ctx = dict(rows=rows, voice=voice, tick=tick, targets=targets, note=note)
        self.panel_ctx = ctx
        self.show_options("Notes: effects and key", rows, lambda values: self.panel_apply(values, ctx), note=note, ok="Apply",
                          keys={pygame.K_DELETE: self.panel_clear, pygame.K_BACKSPACE: self.panel_clear})
        return True

    def panel_clear(self):
        """Delete in the dialog: every effect row to its "off" choice."""
        d = self.fileopts
        if d is None:
            return
        for row in d["rows"]:
            if row["key"] in FX_FIELDS and row["key"] != "arprate":
                for i, (v, _) in enumerate(row["choices"]):
                    if v in ((0, 0), (), 0, (0, 0, 0)):
                        row["i"] = i
                        break

    def panel_apply(self, values, ctx):
        """OK in the dialog: the effects and the key change as one edit.  If the edit is refused the dialog comes back, as
        it was left, with the reason in the status line."""
        effects = effects_from(ctx["rows"], values)
        voice, tick = ctx["voice"], ctx["tick"]
        ed = self.ed
        playing = [v for v in range(self.song.voices) if tick == 0 or tick < ed.trace.ends[v]]
        key = key_from(values, tick, voice, playing)
        if not effects and not key:
            self.say("Nothing to change.", 2.5)
            return True
        targets = ctx["targets"]
        if self.edit(lambda: ed.edit_notes(targets, effects or None, key), None):
            n, k = self.last_result
            parts = []
            if effects:
                parts.append("effects set on %d note%s" % (n, "" if n == 1 else "s") if n else "the notes already had those effects")
            if key:
                parts.append("key change removed" if key.get("remove") else
                             "key %s from bar %d" % (describe_key(ed.key_at(voice, tick)), tick // max(1, self.tune.bar_ticks) + 1))
            text = "; ".join(parts)
            self.say(text[0].upper() + text[1:] + ".", 4.0)
            return True
        rows = [(k, lab, ch, next((i for i, (v, _) in enumerate(ch) if v == values[k]), st)) for k, lab, ch, st in ctx["rows"]]
        self.show_options("Notes: effects and key", rows, lambda vals: self.panel_apply(vals, ctx), note=ctx["note"],
                          ok="Apply", keys={pygame.K_DELETE: self.panel_clear, pygame.K_BACKSPACE: self.panel_clear})
        return False
