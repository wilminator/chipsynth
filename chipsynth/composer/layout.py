# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where things go in the window.  Panel sizes are the user's (dragged splitters); the rest follows the window and
the font size.  No pygame: rectangles are (x, y, w, h) tuples."""
from dataclasses import dataclass

MIN_W, MIN_H = 960, 600                  # below this the window shows a "too small" notice instead of the tool
SPLIT = 6                                # splitter thickness in pixels (at UI scale 1)


def contains(r, x, y):
    return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]


@dataclass
class Panels:
    """The user-set sizes (pixels at UI scale 1; they are multiplied by the scale when laid out)."""
    left: int = 210
    right: int = 280
    bottom: int = 96

    LEFT = (150, 420)
    RIGHT = (200, 520)
    BOTTOM = (56, 260)


@dataclass
class Layout:
    width: int
    height: int
    top: tuple
    left: tuple
    center: tuple
    right: tuple
    bottom: tuple
    split_left: tuple                    # the draggable strips between panels
    split_right: tuple
    split_bottom: tuple
    scale: float
    too_small: bool = False


def compute(width, height, panels, scale=1.0, bar=None):
    """The rectangles for a window of width x height.  `scale` is the font-size scale (1.0 = 15 px text);
    `bar` the top bar height in pixels (default from the scale)."""
    bar = bar or int(round(46 * scale))
    sp = max(4, int(round(SPLIT * scale)))
    # clamp the user's panel sizes so the centre keeps room
    left = int(round(max(panels.LEFT[0], min(panels.left, panels.LEFT[1])) * scale))
    right = int(round(max(panels.RIGHT[0], min(panels.right, panels.RIGHT[1])) * scale))
    bottom = int(round(max(panels.BOTTOM[0], min(panels.bottom, panels.BOTTOM[1])) * scale))
    min_center = int(round(320 * scale))
    over = left + right + 2 * sp + min_center - width
    if over > 0:
        cut = min(over, max(0, left - int(150 * scale)))
        left -= cut
        over -= cut
        right -= min(over, max(0, right - int(200 * scale)))
    body_y, body_h = bar, max(0, height - bar)
    bottom = min(bottom, max(0, body_h - int(160 * scale)))
    mid_h = body_h - bottom - sp
    top = (0, 0, width, bar)
    lefts = (0, body_y, left, body_h)
    rights = (width - right, body_y, right, body_h)
    cx = left + sp
    cw = max(0, width - right - sp - cx)
    center = (cx, body_y, cw, max(0, mid_h))
    bot = (cx, body_y + mid_h + sp, cw, bottom)
    return Layout(width, height, top, lefts, center, rights, bot,
                  (left, body_y, sp, body_h), (width - right - sp, body_y, sp, body_h),
                  (cx, body_y + mid_h, cw, sp), scale,
                  too_small=width < MIN_W or height < MIN_H or cw < int(0.9 * min_center) or mid_h < int(150 * scale))


def drag_left(panels, x, scale):
    panels.left = int(round(max(panels.LEFT[0], min(x / scale, panels.LEFT[1]))))


def drag_right(panels, x, width, scale):
    panels.right = int(round(max(panels.RIGHT[0], min((width - x) / scale, panels.RIGHT[1]))))


def drag_bottom(panels, y, height, scale):
    panels.bottom = int(round(max(panels.BOTTOM[0], min((height - y) / scale, panels.BOTTOM[1]))))


def roll_rects(center, line):
    """The pieces of the piano-roll area: ruler, keys, grid, scroll bars and the lanes under the grid."""
    x, y, w, h = center
    ruler_h = int(line * 1.7)
    keys_w = int(line * 3.8)
    sb = max(11, int(line * 0.8))
    dac = int(line * 1.4)
    vel = int(line * 2.9)
    lanes_h = 2 * dac + vel
    grid_x, grid_w = x + keys_w, max(0, w - keys_w - sb)
    grid_y = y + ruler_h
    lanes_y = y + h - sb - lanes_h
    grid_h = max(0, lanes_y - grid_y)
    return dict(
        ruler=(grid_x, y, grid_w, ruler_h), corner=(x, y, keys_w, ruler_h),
        keys=(x, grid_y, keys_w, grid_h), grid=(grid_x, grid_y, grid_w, grid_h),
        vbar=(x + w - sb, grid_y, sb, grid_h),
        dac0=(grid_x, lanes_y, grid_w, dac), dac1=(grid_x, lanes_y + dac, grid_w, dac),
        vel=(grid_x, lanes_y + 2 * dac, grid_w, vel), lane_names=(x, lanes_y, keys_w, lanes_h),
        hbar=(grid_x, y + h - sb, grid_w, sb))
