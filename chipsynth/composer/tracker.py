# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tracker view's maths, with no pygame in it: which text goes in which cell, and the keyboard piano.

A tracker shows time going down the page: one row per `rt` ticks (the snap length), one column per voice
(V1-V4, D1, D2).  A note shows on the row where it starts; the rows it keeps sounding through show a hold mark.
"""
from bisect import bisect_right

from .model import pitch_name

COLUMNS = ("V1", "V2", "V3", "V4", "D1", "D2")
# the keys of a computer keyboard laid out like a piano, two octaves: semitones above C of the octave
PIANO = {c: i for i, c in enumerate("zsxdcvgbhnjm")}
PIANO.update({c: 12 + i for i, c in enumerate("q2w3er5t6y7u")})
EXTRA = 5                                      # empty bars after the end, so a song can be written past its end


class Cursor:
    def __init__(self):
        self.row, self.col, self.top, self.octave = 0, 0, 0, 4
        self.left = 0                       # first column on screen (songs with more tracks than fit)

    def tick(self, rt):
        return self.row * rt


def key_char(key, unicode=""):
    """The lower-case character a key press types (tests and some keyboards give no `unicode`)."""
    if unicode:
        return unicode.lower()
    return chr(key).lower() if 32 <= key < 127 else ""


def piano_pitch(char, octave):
    """The pitch for a piano key in `octave` (None if the key is not a piano key or the note is out of range)."""
    k = PIANO.get(char)
    if k is None:
        return None
    p = octave * 12 + k
    return p if 0 <= p <= 8 * 12 + 11 else None


def total_rows(roll, rt):
    bars = max(1, -(-roll.length // roll.bar_ticks)) + EXTRA
    return max(1, bars * roll.bar_ticks // rt)


def column_cells(roll, col, rt, first, count):
    """[(text, kind)] for rows first .. first+count-1 of one column.  kind: "start", "late" (starts off the row grid),
    "hold" or "empty"."""
    out = []
    if col < roll.nv:
        notes = sorted(roll.notes[col], key=lambda n: n.start)
        starts = [n.start for n in notes]
        for r in range(first, first + count):
            t0, t1 = r * rt, (r + 1) * rt
            i = bisect_right(starts, t0 - 1)                       # first note starting at t0 or later
            if i < len(notes) and notes[i].start < t1:
                n = notes[i]
                out.append((pitch_name(n.pitch), "start" if n.start == t0 else "late"))
            elif i > 0 and notes[i - 1].start + notes[i - 1].dur > t0:
                out.append(("|", "hold"))
            else:
                out.append(("", "empty"))
    else:
        blocks = sorted(roll.dac_of(col), key=lambda b: b.start)
        starts = [b.start for b in blocks]
        for r in range(first, first + count):
            t0, t1 = r * rt, (r + 1) * rt
            i = bisect_right(starts, t0 - 1)
            if i < len(blocks) and blocks[i].start < t1:
                out.append((blocks[i].name, "start" if blocks[i].start == t0 else "late"))
            else:
                out.append(("", "empty"))
    return out


def row_notes(roll, col, rt, first, count):
    """[RollNote or None] for rows first .. first+count-1 of a voice column: the note that starts within the row
    (what the volume and effect of the row belong to).  Drum columns give None."""
    out = []
    if col >= roll.nv:
        return [None] * count
    notes = sorted(roll.notes[col], key=lambda n: n.start)
    starts = [n.start for n in notes]
    for r in range(first, first + count):
        t0, t1 = r * rt, (r + 1) * rt
        i = bisect_right(starts, t0 - 1)
        out.append(notes[i] if i < len(notes) and notes[i].start < t1 else None)
    return out


def row_label(roll, row, rt):
    """'3.2' on a beat, '' otherwise (bar.beat, counted from 1)."""
    t = row * rt
    beat = 96
    if t % beat:
        return ""
    bar, rest = divmod(t, roll.bar_ticks)
    return "%d.%d" % (bar + 1, rest // beat + 1)


def note_at(roll, col, tick):
    """Index of the note of voice `col` that is sounding at `tick` (or starts there), else None."""
    if col >= roll.nv:
        return None
    for i, n in enumerate(roll.notes[col]):
        if n.start <= tick < n.start + n.dur:
            return i
    return None


def hit_at(roll, col, tick, rt):
    """The DacBlock of lane `col` that starts within the row at `tick`, else None."""
    if col < roll.nv:
        return None
    for b in roll.dac_of(col):
        if tick <= b.start < tick + rt:
            return b
    return None
