# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The track stack: every track is a lane of the same height, stacked top to bottom and scrolled vertically.

No pygame here, so it is tested without a display.  All y values are pixels relative to the top of the stack's
area (the part of the window the lanes are drawn in); `scroll` is how far the stack is moved up.

    st = Stack(names=("V1", "V2", "V3", "V4", "D1", "D2"), height=96)
    st.visible(area_h)             # [(track, top_y)] for the lanes that show, top_y may be above 0 for a cut lane
    st.track_at(y, area_h)         # which track is under y (or None)
    st.window(i, roll)             # (low, high) pitches a melodic track's lane shows, fitted to its notes
    st.y_of(i, pitch, roll) / st.pitch_at(i, y, roll)
"""
from dataclasses import dataclass, field

from .model import LOW, HIGH

MIN_H, MAX_H, DEFAULT_H = 56, 400, 96          # track height limits and default, in pixels at UI scale 1
MIN_SPAN = 24                                  # a lane never shows fewer than two octaves


def clamp_height(h):
    return max(MIN_H, min(MAX_H, int(h)))


@dataclass
class Stack:
    names: tuple = ("V1", "V2", "V3", "V4", "D1", "D2")
    height: int = DEFAULT_H                    # one track's height (the user's setting)
    scroll: int = 0                            # pixels the stack is scrolled up
    pinned: dict = field(default_factory=dict)  # track index -> (low, high): a pitch window the user fixed

    # ------------------------------------------------------------ size and scroll
    @property
    def count(self):
        return len(self.names)

    def total(self):
        return self.count * self.height

    def max_scroll(self, area_h):
        return max(0, self.total() - area_h)

    def clamp(self, area_h):
        self.scroll = max(0, min(int(self.scroll), self.max_scroll(area_h)))

    def set_height(self, h, area_h, keep_track=None):
        """Change the track height; keep `keep_track` (default: the one at the top edge) where it was."""
        top = self.scroll // self.height if keep_track is None else keep_track
        offset = self.scroll - top * self.height if keep_track is None else 0
        self.height = clamp_height(h)
        if keep_track is None:
            self.scroll = top * self.height + min(offset, self.height - 1)
        else:
            self.scroll_to(keep_track, area_h)
        self.clamp(area_h)

    def wheel(self, notches, area_h):
        """Mouse wheel: a notch moves a third of a track."""
        self.scroll += int(-notches * self.height / 3)
        self.clamp(area_h)

    def scroll_to(self, i, area_h):
        """Scroll the least that shows track `i` completely (or its top, if the area is shorter than a track)."""
        top, bottom = i * self.height, (i + 1) * self.height
        if top < self.scroll:
            self.scroll = top
        elif bottom > self.scroll + area_h:
            self.scroll = bottom - area_h if area_h >= self.height else top
        self.clamp(area_h)

    # ------------------------------------------------------------ what shows, what is under a point
    def visible(self, area_h):
        """[(track, top_y)] for every lane with any part in view, in order."""
        out = []
        for i in range(self.count):
            y = i * self.height - self.scroll
            if y + self.height > 0 and y < area_h:
                out.append((i, y))
        return out

    def top_of(self, i):
        return i * self.height - self.scroll

    def track_at(self, y, area_h):
        if y < 0 or y >= area_h:
            return None
        i = int((y + self.scroll) // self.height)
        return i if 0 <= i < self.count else None

    # ------------------------------------------------------------ the pitch window of a melodic track
    def is_melodic(self, i):
        return not self.names[i].startswith("D")

    def window(self, i, roll):
        """(low, high), both included, of the pitches lane `i` shows: the user's pinned window, else the track's own
        notes with three semitones of room, at least MIN_SPAN wide and kept inside C0..B8."""
        if i in self.pinned:
            return self.pinned[i]
        notes = roll.notes[i] if i < len(roll.notes) else []
        if notes:
            low, high = min(n.pitch for n in notes) - 3, max(n.pitch for n in notes) + 3
        else:
            low, high = roll.low, roll.high
        if high - low + 1 < MIN_SPAN:
            mid = (low + high) // 2
            low, high = mid - MIN_SPAN // 2, mid + MIN_SPAN // 2 - 1
        low = max(LOW, low)
        high = min(HIGH, max(high, low + MIN_SPAN - 1))
        low = max(LOW, min(low, high - MIN_SPAN + 1))
        return low, high

    def row_px(self, i, roll):
        low, high = self.window(i, roll)
        return self.height / float(high - low + 1)

    def y_of(self, i, pitch, roll):
        """Top edge (in stack-area pixels) of the row for `pitch` in lane `i`."""
        low, high = self.window(i, roll)
        return self.top_of(i) + (high - pitch) * self.row_px(i, roll)

    def pitch_at(self, i, y, roll):
        """The pitch under stack-area y inside lane `i`, clamped to the lane's window."""
        low, high = self.window(i, roll)
        p = high - int((y - self.top_of(i)) / self.row_px(i, roll))
        return max(low, min(high, p))

    def pin(self, i, low, high):
        """Fix lane `i`'s window (the focus view's pitch scrolling would do this)."""
        low = max(LOW, min(int(low), HIGH - MIN_SPAN + 1))
        high = min(HIGH, max(int(high), low + MIN_SPAN - 1))
        self.pinned[i] = (low, high)

    def unpin(self, i=None):
        if i is None:
            self.pinned.clear()
        else:
            self.pinned.pop(i, None)
