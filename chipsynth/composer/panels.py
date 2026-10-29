# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything the composer draws.  `draw(app)` paints one frame; the geometry it uses comes from layout.py."""
import math

import pygame

from chipsynth import musicgen
from chipsynth.mml import SCALES
from chipsynth.songedit import EFFECT_MARK
from chipsynth.synth import ATTACK_MS, DECAY_MS, WAVES

from . import layout as L, model as M, tracker as TR
from .ui import mix, rrect, text

VIEW_STACK, VIEW_ROLL, VIEW_TRACKER = "Tracks", "Piano roll", "Tracker"


def draw(app):
    surf, th = app.screen, app.th
    surf.fill(th["bg"])
    if app.layout.too_small:
        too_small(app)
        return
    lay = app.layout
    ui = app.ui
    held = (ui.click, ui.pos)
    if app.overlay:                                # a dialog is open: what is behind it ignores the mouse
        ui.click, ui.pos = None, (-1, -1)
    clip(surf, lay.top)
    topbar(app)
    app.ensure_layout()                            # the top bar's buttons may have changed the mode
    lay = app.layout
    if lay.left[2] > 0:
        clip(surf, lay.left)
        voices(app)
        app.ensure_layout()
        lay = app.layout
    clip(surf, lay.center)
    if app.mode == "tracker" and app.roll is not None:
        tracker(app)
    elif app.mode == "stack":
        stack(app)
    else:
        roll(app)
    clip(surf, lay.right)
    inspector(app)
    clip(surf, lay.bottom)
    bottom(app)
    surf.set_clip(None)
    splitters(app)
    if app.overlay:
        ui.click, ui.pos = held
        ui.tip = ""
    if app.overlay == "songs":
        songs_overlay(app)
    elif app.overlay == "help":
        help_overlay(app)
    elif app.overlay == "prompt":
        prompt_overlay(app)
    elif app.overlay == "new":
        new_overlay(app)


def clip(surf, rect):
    surf.set_clip(pygame.Rect(rect))


def panel_bg(app, rect, color=None):
    app.screen.fill(color or app.th["panel"], rect)


def splitters(app):
    surf, th, lay = app.screen, app.th, app.layout
    for name in ("split_left", "split_right", "split_bottom"):
        if name == "split_left" and app.mode == "stack":
            continue                                       # no left panel in the track view
        r = getattr(lay, name)
        hot = app.ui.hover(r) or (app.drag and app.drag["kind"] == name)
        surf.fill(th["accent"] if hot else th["line"], r)
        if hot:
            app.ui.tip = "Drag to resize the panel"
        # grip dots
        cx, cy = r[0] + r[2] // 2, r[1] + r[3] // 2
        for d in (-6, 0, 6):
            if r[2] > r[3]:
                pygame.draw.circle(surf, th["dim"], (cx + d, cy), 1)
            else:
                pygame.draw.circle(surf, th["dim"], (cx, cy + d), 1)


def too_small(app):
    surf, th, f = app.screen, app.th, app.fonts
    w, h = surf.get_size()
    text(surf, f.bold, "This window is too small", (w // 2, h // 2 - f.line), th["text"], "mc")
    text(surf, f.ui, "Please make it larger (at least %d x %d)." % (L.MIN_W, L.MIN_H), (w // 2, h // 2 + f.line // 2),
         th["dim"], "mc", w - 20)
    text(surf, f.small, "now %d x %d" % (w, h), (w // 2, h // 2 + int(f.line * 1.8)), th["dim"], "mc")


# ------------------------------------------------------------------ top bar
def topbar(app):
    surf, th, f, ui, s = app.screen, app.th, app.fonts, app.ui, app.scale
    r = app.layout.top
    panel_bg(app, r)
    pygame.draw.line(surf, th["line"], (0, r[3] - 1), (r[2], r[3] - 1))
    pad = int(8 * s)
    bh = r[3] - 2 * pad
    y = r[1] + pad
    gap = int(6 * s)

    def width(label):
        return f.ui.size(label)[0] + int(24 * s)

    x = pad
    w = width("Songs")
    if ui.button(surf, th, f, (x, y, w, bh), "Songs", "Choose a song from the loaded packages and files"):
        app.overlay = "songs"
        app.overlay_scroll = 0
    x += w + gap
    w = int(bh * 1.5)
    playing = app.playing
    if ui.button(surf, th, f, (x, y, w, bh), "", "Stop (Space)" if playing else "Play (Space)", on=playing,
                 enabled=app.tune is not None, icon="stop" if playing else "play"):
        app.toggle_play()
    x += w + gap
    w = width("Loop")
    if ui.button(surf, th, f, (x, y, w, bh), "Loop", "Loop the song at its loop point (L)", on=app.loop_on):
        app.loop_on = not app.loop_on
        app.restart_here()
    x += w + gap
    w = width("Follow")
    if ui.button(surf, th, f, (x, y, w, bh), "Follow", "Scroll with the playhead (F)", on=app.follow):
        app.follow = not app.follow
    x += w + gap
    ed = app.ed
    for label, tip, ok, act in (
            ("New", "Start a new song from the generator (Ctrl+N)", True, app.new_from_generator_dialog),
            ("Undo", "Undo the last edit (Ctrl+Z)", bool(ed and ed.can_undo()), app.undo),
            ("Redo", "Redo (Ctrl+Y)", bool(ed and ed.can_redo()), app.redo),
            ("Save", "Save the song (Ctrl+S; Ctrl+Shift+S saves as)", app.song is not None, app.save)):
        w = width(label)
        if ui.button(surf, th, f, (x, y, w, bh), label, tip, enabled=ok, on=(label == "Save" and app.song_dirty)):
            act()
        x += w + gap
    w = width("Snap " + app.snap_name)
    if ui.button(surf, th, f, (x, y, w, bh), "Snap " + app.snap_name, "Grid for placing and sizing notes ([ and ] change it)"):
        app.set_snap(app.snap_i + 1 if app.snap_i + 1 < len(M.SNAPS) else 0)
    left_end = x + w + gap * 2

    # right group, laid out from the right edge; whatever does not fit is left off (least important last)
    right = r[2] - pad
    limit = left_end + int(190 * s)
    items = []
    sq = bh
    items.append(("view", [(VIEW_STACK, width(VIEW_STACK), app.mode == "stack"), (VIEW_ROLL, width(VIEW_ROLL), app.mode == "roll"),
                           (VIEW_TRACKER, width(VIEW_TRACKER), app.mode == "tracker")]))
    items.append(("theme", width("Light" if app.prefs.dark else "Dark")))
    items.append(("font", (sq, sq)))
    items.append(("hz", (sq, sq)))
    items.append(("vz", (sq, sq)))
    if app.mode == "stack":
        items.append(("tz", (sq, sq)))
    for kind, spec in items:
        if kind == "view":
            need = sum(wd for _, wd, _ in spec) + gap
            if right - need < limit:
                continue
            x2 = right - need + gap
            for name, wd, active in spec:
                rect = (x2, y, wd, bh)
                tip = {VIEW_STACK: "All the tracks, one lane each, scrolling down the page (Tab changes view)",
                       VIEW_ROLL: "One big piano roll for all the voices", VIEW_TRACKER: "Time down the page: the tracker grid"}[name]
                if ui.button(surf, th, f, rect, name, tip, on=active):
                    app.set_mode({VIEW_STACK: "stack", VIEW_ROLL: "roll", VIEW_TRACKER: "tracker"}[name])
                x2 += wd
            right -= need + gap
        elif kind == "theme":
            need = spec
            if right - need < limit:
                continue
            rect = (right - need, y, need, bh)
            if ui.button(surf, th, f, rect, "Light" if app.prefs.dark else "Dark", "Switch light / dark theme (T)"):
                app.prefs.dark = not app.prefs.dark
                app.dirty = True
            right -= need + gap
        else:
            label = {"font": "Aa", "hz": "H", "vz": "V", "tz": "T"}[kind]
            lw = f.small.size(label)[0] + 4
            need = lw + 2 * sq + gap
            if right - need < limit:
                continue
            x2 = right - need
            text(surf, f.small, label, (x2, y + bh // 2), th["dim"], "ml")
            x2 += lw
            tips = {"font": ("Smaller text (Ctrl -)", "Larger text (Ctrl +)"),
                    "hz": ("Zoom out horizontally (-)", "Zoom in horizontally (+)"),
                    "vz": ("Shrink the rows", "Taller rows (Ctrl+Shift+wheel)"),
                    "tz": ("Shorter tracks (Ctrl+Shift+wheel)", "Taller tracks (Ctrl+Shift+wheel)")}[kind]
            if ui.button(surf, th, f, (x2, y, sq, sq), "−", tips[0]):
                step(app, kind, -1)
            if ui.button(surf, th, f, (x2 + sq, y, sq, sq), "+", tips[1]):
                step(app, kind, 1)
            right -= need + gap * 2
    # title and facts in what is left
    if app.song is not None:
        tx = left_end
        tw = right - tx
        text(surf, f.bold, app.label + (" *" if app.song_dirty else ""), (tx, y + bh // 4), th["text"], "ml", tw)
        t = app.tune
        facts = "%d bpm   %d/%d   %d bars   %s" % (t.bpm, t.time[0], t.time[1], t.bars, clock(t.seconds))
        text(surf, f.small, facts, (tx, y + bh * 3 // 4), th["dim"], "ml", tw)


def step(app, kind, d):
    if kind == "font":
        app.set_font(app.prefs.font + d)
    elif kind == "hz" and app.roll:
        app.zoom_h_center(1.25 if d > 0 else 0.8)
    elif kind == "vz" and app.roll:
        app.zoom_v_center(1.2 if d > 0 else 1 / 1.2)
    elif kind == "tz":
        app.set_track_h(app.prefs.track_h + 12 * d)
    app.dirty = True


def clock(seconds):
    m, s = divmod(int(round(seconds)), 60)
    return "%d:%02d" % (m, s)


# ------------------------------------------------------------------ voices
VOICE_LABEL = ("V1", "V2", "V3", "V4", "D1", "D2")      # the default lane names; Roll.names for a song's own


def vcol(th, i):
    """The colour of lane i (the palette repeats for songs with more lanes than it has colours)."""
    pal = th["voices"]
    return pal[i % len(pal)]


def voices(app):
    surf, th, f, ui, s = app.screen, app.th, app.fonts, app.ui, app.scale
    r = app.layout.left
    panel_bg(app, r)
    x, y, w, h = r
    pad = int(10 * s)
    text(surf, f.small, "VOICES", (x + pad, y + pad), th["dim"])
    y += pad + f.line
    n = app.roll.lanes if app.roll else 6
    names = app.roll.names if app.roll else VOICE_LABEL
    area = h - (y - r[1]) - pad
    card_h = max(int(f.line * 1.7), min(int(f.line * 3.0), area // n - 4))        # tall enough to read; scrolls if not
    total = n * (card_h + 4)
    app.voices_scroll = max(0, min(app.voices_scroll, total - area))
    y -= app.voices_scroll
    clip(surf, (x, r[1] + pad + f.line, w, h - pad - f.line))
    app.voice_cards = []
    muted = app.muted_now() if app.tune else set()
    counts = app.roll.counts() if app.roll else [0] * 6
    nv = app.roll.nv if app.roll else 4
    for i in range(n):
        rect = (x + pad, y, w - 2 * pad - 2, card_h)
        app.voice_cards.append(rect)
        y += card_h + 4
        if rect[1] + rect[3] < r[1] + pad + f.line or rect[1] > r[1] + h:
            continue
        sel = i == app.sel_voice
        col = vcol(th, i)
        dead = i in muted or counts[i] == 0
        rrect(surf, th["panel2"] if not sel else mix(th["panel2"], th["accent"], 0.18), rect, 6,
              th["accent"] if sel else th["line"])
        pygame.draw.rect(surf, col if not dead else mix(col, th["panel2"], 0.65),
                         (rect[0] + 1, rect[1] + 6, max(4, int(5 * s)), rect[3] - 12), border_radius=3)
        tx = rect[0] + int(16 * s)
        text(surf, f.bold, names[i], (tx, rect[1] + int(6 * s)), th["text"] if not dead else th["dim"])
        # M / S
        bw = int(f.line * 1.25)
        bx = rect[0] + rect[2] - 2 * bw - int(10 * s)
        by = rect[1] + int(6 * s)
        if ui.button(surf, th, f, (bx, by, bw, bw), "M", "Mute %s (M)" % names[i], on=i in app.muted, font=f.small):
            app.toggle_mute(i)
        if ui.button(surf, th, f, (bx + bw + int(4 * s), by, bw, bw), "S", "Solo %s" % names[i], on=i in app.solos,
                     font=f.small):
            app.toggle_solo(i)
        if ui.clicked(rect) and not ui.hover((bx, by, 2 * bw + int(4 * s), bw)):
            app.sel_voice = i
        if app.roll and card_h >= int(f.line * 2.4):
            if i < nv:
                inst = voice_inst(app, i)
                sub = "%s   %d notes" % (inst, counts[i]) if counts[i] else "empty"
            else:
                sub = "%d samples" % counts[i] if counts[i] else "empty"
            text(surf, f.small, sub, (tx, rect[1] + rect[3] - int(7 * s)), th["dim"], "bl", rect[2] - int(24 * s))


def voice_inst(app, i):
    notes = app.roll.notes[i]
    return notes[0].inst if notes else ""


# ------------------------------------------------------------------ piano roll
def roll(app):
    surf, th, f = app.screen, app.th, app.fonts
    panel_bg(app, app.layout.center, th["bg"])
    if app.roll is None:
        c = app.layout.center
        text(surf, f.ui, "No song open.  Press Songs, or drop a .mml / .csp file here.",
             (c[0] + c[2] // 2, c[1] + c[3] // 2), th["dim"], "mc")
        return
    g = app.geo
    draw_grid(app)
    draw_notes(app)
    draw_ghost(app)
    draw_lanes(app)
    draw_keys(app)
    draw_ruler(app)
    draw_playhead(app)
    scrollbar(app, "h")
    scrollbar(app, "v")


def tracker_geo(app):
    x, y, w, h = app.layout.center
    line = app.fonts.line
    head = int(line * 1.7)
    rn = int(line * 3.6)
    sb = max(11, int(line * 0.8))
    row_h = int(line * 1.3)
    lanes = app.roll.lanes if app.roll else 6
    cols = max(1, min(lanes, (w - rn - sb) // int(line * 4.6)))       # columns that fit; the rest scroll sideways
    cw = max(10, (w - rn - sb) // cols)
    rows_y = y + head
    visible = max(1, (h - head) // row_h)
    return dict(x=x, y=y, w=w, h=h, head=head, rn=rn, sb=sb, row_h=row_h, cw=cw, rows_y=rows_y, visible=visible, cols=cols)


def tracker(app):
    surf, th, f, ui = app.screen, app.th, app.fonts, app.ui
    g = tracker_geo(app)
    panel_bg(app, app.layout.center, th["bg"])
    rt, rl, cur = app.snap_ticks, app.roll, app.tr
    n_rows = TR.total_rows(rl, rt)
    cur.row = max(0, min(n_rows - 1, cur.row))
    cur.top = max(0, min(max(0, n_rows - g["visible"]), cur.top))
    x0 = g["x"] + g["rn"]
    clip(surf, app.layout.center)
    # header
    pygame.draw.rect(surf, th["panel"], (g["x"], g["y"], g["w"], g["head"]))
    pygame.draw.line(surf, th["line"], (g["x"], g["y"] + g["head"] - 1), (g["x"] + g["w"], g["y"] + g["head"] - 1))
    text(surf, f.small, "%s rows" % M.SNAPS[app.snap_i][0], (g["x"] + 6, g["y"] + g["head"] // 2), th["dim"], "ml")
    left = cur.left
    shown = range(left, min(rl.lanes, left + g["cols"]))
    for c in shown:
        cx = x0 + (c - left) * g["cw"]
        sel = app.sel_voice == c
        text(surf, f.bold, rl.names[c], (cx + g["cw"] // 2, g["y"] + g["head"] // 2), vcol(th, c) if sel else mix(vcol(th, c), th["panel"], 0.35), "mc")
    # rows
    first, count = cur.top, g["visible"]
    cells = [TR.column_cells(rl, c, rt, first, count) for c in range(rl.lanes)]
    extras = [TR.row_notes(rl, c, rt, first, count) for c in range(rl.lanes)]
    tick = app.playhead()
    play_row = None if tick is None else int(tick // rt)
    bt = rl.bar_ticks
    for k in range(count):
        r = first + k
        if r >= n_rows:
            break
        ry = g["rows_y"] + k * g["row_h"]
        t = r * rt
        bg = th["white_row"] if (t // M.BEAT) % 2 == 0 else th["black_row"]
        pygame.draw.rect(surf, bg, (g["x"], ry, g["w"] - g["sb"], g["row_h"]))
        if t % bt == 0:
            pygame.draw.line(surf, th["bar"], (g["x"], ry), (g["x"] + g["w"] - g["sb"], ry))
        if r == play_row:
            shade = pygame.Surface((g["w"] - g["sb"], g["row_h"]), pygame.SRCALPHA)
            shade.fill(th["accent"] + (90,))
            surf.blit(shade, (g["x"], ry))
        text(surf, f.mono, TR.row_label(rl, r, rt), (g["x"] + g["rn"] - 8, ry + g["row_h"] // 2), th["dim"], "mr")
        for c in shown:
            cx = x0 + (c - left) * g["cw"]
            label, kind = cells[c][k]
            if c == cur.col and r == cur.row:
                pygame.draw.rect(surf, th["accent"], (cx + 1, ry, g["cw"] - 2, g["row_h"]), 2, border_radius=3)
            if kind == "empty":
                text(surf, f.mono, "...", (cx + g["cw"] // 2, ry + g["row_h"] // 2), mix(th["dim"], bg, 0.55), "mc")
            elif kind == "hold":
                text(surf, f.mono, "|", (cx + g["cw"] // 2, ry + g["row_h"] // 2), mix(vcol(th, c), bg, 0.5), "mc")
            else:
                col = vcol(th, c) if kind == "start" else mix(vcol(th, c), bg, 0.45)
                nt = extras[c][k]
                if nt is None:
                    text(surf, f.mono, label, (cx + g["cw"] // 2, ry + g["row_h"] // 2), col, "mc", g["cw"] - 6)
                else:                                   # note name, then its volume, then its effect mark
                    mark = EFFECT_MARK.get(nt.fx, "")
                    text(surf, f.mono, label, (cx + 6, ry + g["row_h"] // 2), col, "ml", g["cw"] // 3)
                    text(surf, f.mono, "v%d" % nt.vol, (cx + g["cw"] * 5 // 9, ry + g["row_h"] // 2),
                         mix(th["dim"], bg, 0.15 if nt.vol != 15 else 0.6), "ml", g["cw"] // 4)
                    if mark:
                        text(surf, f.mono, mark, (cx + g["cw"] - 6, ry + g["row_h"] // 2), th["accent"], "mr", g["cw"] // 5)
    for c in range(1, len(shown) + 1):
        pygame.draw.line(surf, th["line"], (x0 + c * g["cw"] - 1, g["rows_y"]), (x0 + c * g["cw"] - 1, g["y"] + g["h"]))
    # scroll bar
    bx = g["x"] + g["w"] - g["sb"]
    pygame.draw.rect(surf, th["panel"], (bx, g["rows_y"], g["sb"], g["h"] - g["head"]))
    frac = g["visible"] / max(1, n_rows)
    if frac < 1:
        bh = max(20, int((g["h"] - g["head"]) * frac))
        by = g["rows_y"] + int((g["h"] - g["head"] - bh) * cur.top / max(1, n_rows - g["visible"]))
        pygame.draw.rect(surf, mix(th["dim"], th["panel"], 0.5), (bx + 2, by, g["sb"] - 4, bh), border_radius=3)
    if ui.hover((g["x"], g["rows_y"], g["w"], g["h"] - g["head"])):
        ui.tip = ("Type notes on the keys z s x d c v ... (piano layout), - / + octave, Delete erases, Enter places a drum sample, "
                  ", / . volume (Shift: bigger steps), ; effect")
    surf.set_clip(None)


def draw_grid(app):
    surf, th, v, rl = app.screen, app.th, app.view, app.roll
    gx, gy, gw, gh = app.geo["grid"]
    clip(surf, app.geo["grid"])
    top = int(math.ceil(v.top_pitch)) - 1
    p = min(M.HIGH, top)
    while p >= M.LOW:
        y = v.y_of(p, gy)
        if y > gy + gh:
            break
        pygame.draw.rect(surf, th["black_row"] if M.is_black(p) else th["white_row"], (gx, y, gw, v.row + 1))
        if app.guide is not None:
            tonic, name = app.guide
            if (p - tonic) % 12 in SCALES.get(name, ()):
                shade = pygame.Surface((int(gw), v.row + 1), pygame.SRCALPHA)
                shade.fill(th["accent"] + (70 if (p - tonic) % 12 == 0 else 30,))
                surf.blit(shade, (gx, y))
        if p % 12 == 0:
            pygame.draw.line(surf, th["bar"], (gx, y + v.row), (gx + gw, y + v.row))
        p -= 1
    time_lines(app, (gx, gy, gw, gh))


def time_lines(app, rect):
    """Over a lane's rows: the shading past the end and over the loop, bar lines and beat lines."""
    surf, th, v, rl = app.screen, app.th, app.view, app.roll
    gx, gy, gw, gh = rect
    end_x = v.x_of(rl.length, gx)
    if end_x < gx + gw:
        surf.fill(th["past"], (max(gx, end_x), gy, gw - (max(gx, end_x) - gx), gh))
    if rl.loop_tick > 0:
        lx = max(gx, v.x_of(rl.loop_tick, gx))
        ex = min(gx + gw, end_x)
        if ex > lx:
            shade = pygame.Surface((int(ex - lx), gh), pygame.SRCALPHA)
            shade.fill(th["loop"] + (110,))
            surf.blit(shade, (lx, gy))
    # vertical lines
    bar_px = rl.bar_ticks * v.ppt
    every = 1
    while bar_px * every < 36:
        every *= 2
    first = int(max(0, v.scroll_tick) // rl.bar_ticks)
    last = int((v.scroll_tick + gw / v.ppt) // rl.bar_ticks) + 1
    for b in range(first, last + 1):
        x = v.x_of(b * rl.bar_ticks, gx)
        if b % every == 0:
            pygame.draw.line(surf, th["bar"], (x, gy), (x, gy + gh))
        if v.ppb >= 26 and every == 1:
            for k in range(1, rl.bar_ticks // M.BEAT):
                bx = x + k * M.BEAT * v.ppt
                pygame.draw.line(surf, th["grid"], (bx, gy), (bx, gy + gh))
                if v.ppb >= 120:
                    for q in (1, 2, 3):
                        qx = bx + q * M.BEAT * v.ppt / 4
                        pygame.draw.line(surf, mix(th["grid"], th["white_row"], 0.45), (qx, gy), (qx, gy + gh))


def note_color(app, voice, dim=False):
    th = app.th
    c = vcol(th, voice)
    if voice in (app.muted_now() if app.tune else ()):
        return mix(th["dim"], th["bg"], 0.55)
    return c if not dim else mix(c, th["white_row"], 0.58)


def draw_notes(app):
    surf, th, v, rl, f = app.screen, app.th, app.view, app.roll, app.fonts
    gx, gy, gw, gh = app.geo["grid"]
    clip(surf, app.geo["grid"])
    t0, t1 = v.scroll_tick, v.scroll_tick + gw / v.ppt
    nv = rl.nv
    order = [i for i in range(nv) if i != app.sel_voice] + ([app.sel_voice] if app.sel_voice < nv else [])
    for vi in order:
        dim = vi != app.sel_voice
        col = note_color(app, vi, dim)
        edge = mix(col, (0, 0, 0), 0.4)
        for idx, n in enumerate(rl.notes[vi]):
            if n.start + n.dur < t0 or n.start > t1:
                continue
            x = v.x_of(n.start, gx)
            w = max(2.0, n.dur * v.ppt - 1)
            y = v.y_of(n.pitch, gy) + 1
            h = v.row - 2
            if y + h < gy or y > gy + gh:
                continue
            rect = pygame.Rect(int(x), int(y), int(w), int(max(3, h)))
            pygame.draw.rect(surf, col, rect, border_radius=3)
            pygame.draw.rect(surf, edge, rect, 1, border_radius=3)
            if app.note_sel == (vi, idx):
                pygame.draw.rect(surf, th["text"], rect.inflate(2, 2), 2, border_radius=4)
            elif (vi, n.start) in app.multi:
                pygame.draw.rect(surf, th["accent"], rect.inflate(2, 2), 2, border_radius=4)
            elif app.note_hover == (vi, idx):
                pygame.draw.rect(surf, mix(col, (255, 255, 255), 0.6), rect.inflate(2, 2), 1, border_radius=4)
            if not dim and rect.w > f.small.size("C#4")[0] + 8 and v.row >= 13:
                text(surf, f.small, M.pitch_name(n.pitch), (rect.x + 4, rect.centery), th["accent_text"]
                     if sum(col) > 380 else (255, 255, 255), "ml")
            if n.fx != "none" and rect.w >= 8 and v.row >= 9:           # the note's effect, at its right end
                text(surf, f.small, EFFECT_MARK[n.fx], (rect.right - 3, rect.centery),
                     th["accent_text"] if sum(col) > 380 else (255, 255, 255), "mr")


def draw_marquee(app):
    d = app.drag
    if not d or d["kind"] != "marquee":
        return
    gx, gy, gw, gh = app.geo["grid"]
    clip(app.screen, app.geo["grid"])
    r = pygame.Rect(min(d["at"][0], d["to"][0]), min(d["at"][1], d["to"][1]), abs(d["to"][0] - d["at"][0]), abs(d["to"][1] - d["at"][1]))
    shade = pygame.Surface((max(1, r.w), max(1, r.h)), pygame.SRCALPHA)
    shade.fill(app.th["accent"] + (40,))
    app.screen.blit(shade, r.topleft)
    pygame.draw.rect(app.screen, app.th["accent"], r, 1)
    app.screen.set_clip(None)


def draw_ghost(app):
    """The note being placed, moved or sized by the mouse."""
    draw_marquee(app)
    d = app.drag
    if not d or d["kind"] not in ("move", "resize", "new") or not d.get("moved", True):
        return
    surf, th, v = app.screen, app.th, app.view
    gx, gy, gw, gh = app.geo["grid"]
    if d["kind"] == "new":
        start, dur, pitch = d["start"], d["dur"], d["pitch"]
    else:
        start, dur, pitch = d["new"]
    clip(surf, app.geo["grid"])
    rect = pygame.Rect(int(v.x_of(start, gx)), int(v.y_of(pitch, gy) + 1), int(max(3, dur * v.ppt - 1)), int(max(3, v.row - 2)))
    shade = pygame.Surface(rect.size, pygame.SRCALPHA)
    shade.fill(note_color(app, d["voice"]) + (120,))
    surf.blit(shade, rect.topleft)
    pygame.draw.rect(surf, th["text"], rect, 2, border_radius=3)
    surf.set_clip(None)


def draw_keys(app):
    surf, th, v, f = app.screen, app.th, app.view, app.fonts
    kx, ky, kw, kh = app.geo["keys"]
    clip(surf, app.geo["keys"])
    surf.fill(th["panel"], app.geo["keys"])
    p = min(M.HIGH, int(math.ceil(v.top_pitch)) - 1)
    while p >= M.LOW:
        y = v.y_of(p, ky)
        if y > ky + kh:
            break
        black = M.is_black(p)
        col = th["key_black"] if black else th["key_white"]
        w = kw * (0.62 if black else 1.0)
        pygame.draw.rect(surf, col, (kx, y + 0.5, w - 1, v.row - 1))
        if not black:
            pygame.draw.line(surf, th["line"], (kx, y + v.row), (kx + kw, y + v.row))
        if p % 12 == 0 and v.row >= 9:
            text(surf, f.small, M.pitch_name(p), (kx + kw - 4, y + v.row / 2), th["dim"] if not black else th["text"], "mr")
        p -= 1
    pygame.draw.line(surf, th["line"], (kx + kw - 1, ky), (kx + kw - 1, ky + kh))


def draw_ruler(app):
    surf, th, v, rl, f = app.screen, app.th, app.view, app.roll, app.fonts
    rx, ry, rw, rh = app.geo["ruler"]
    clip(surf, app.geo["corner"])
    surf.fill(th["panel"], app.geo["corner"])
    text(surf, f.small, "bar", (app.geo["corner"][0] + app.geo["corner"][2] - 6, ry + rh // 2), th["dim"], "mr")
    clip(surf, app.geo["ruler"])
    surf.fill(th["panel"], app.geo["ruler"])
    pygame.draw.line(surf, th["line"], (rx, ry + rh - 1), (rx + rw, ry + rh - 1))
    bar_px = rl.bar_ticks * v.ppt
    every = 1
    while bar_px * every < f.small.size("000")[0] + 14:
        every *= 2
    first = int(max(0, v.scroll_tick) // rl.bar_ticks)
    last = int((v.scroll_tick + rw / v.ppt) // rl.bar_ticks) + 1
    for b in range(first, last + 1):
        if b % every:
            continue
        x = v.x_of(b * rl.bar_ticks, rx)
        pygame.draw.line(surf, th["bar"], (x, ry + rh * 0.45), (x, ry + rh))
        text(surf, f.small, str(b + 1), (x + 4, ry + 3), th["dim"])
    if rl.loop_tick > 0:
        x = v.x_of(rl.loop_tick, rx)
        pygame.draw.polygon(surf, th["voices"][2], [(x, ry + rh - 1), (x, ry + rh * 0.45), (x + rh * 0.35, ry + rh * 0.62),
                                                    (x, ry + rh * 0.78)])
        app.ui.tip = app.ui.tip
    for tick, mb in app.ed.tempo_marks() if app.ed else ():
        x = v.x_of(tick, rx)
        if rx - 40 <= x <= rx + rw:
            pygame.draw.line(surf, th["voices"][1], (x, ry + rh * 0.3), (x, ry + rh))
            text(surf, f.small, "%d" % mb, (x + 3, ry + rh - 3), th["voices"][1], "bl")
    # where Play starts
    cx = v.x_of(app.cursor_tick, rx)
    if rx <= cx <= rx + rw and app.cursor_tick:
        pygame.draw.polygon(surf, th["accent"], [(cx - 5, ry + rh - 1), (cx + 5, ry + rh - 1), (cx, ry + rh - 9)])
    if app.ui.hover(app.geo["ruler"]):
        app.ui.tip = "Click to set where Play starts (and jump there while playing)"


def draw_playhead(app):
    tick = app.playhead()
    if tick is None:
        return
    surf, th, v = app.screen, app.th, app.view
    gx, gy, gw, gh = app.geo["grid"]
    x = v.x_of(tick, gx)
    if x < gx or x > gx + gw:
        return
    top = app.geo["ruler"][1]
    bottom = app.geo["vel"][1] + app.geo["vel"][3]
    surf.set_clip(pygame.Rect(gx, top, gw, bottom - top))
    pygame.draw.line(surf, th["accent"], (x, top), (x, bottom), 2)


def draw_lanes(app):
    surf, th, v, rl, f = app.screen, app.th, app.view, app.roll, app.fonts
    names = app.geo["lane_names"]
    clip(surf, names)
    surf.fill(th["panel"], names)
    nv = rl.nv
    for di in range(rl.nd):
        r = app.geo["dac%d" % di]
        text(surf, f.small, "D%d" % (di + 1), (names[0] + names[2] - 6, r[1] + r[3] // 2), vcol(th, nv + di), "mr")
    vr = app.geo["vel"]
    text(surf, f.small, "Volume", (names[0] + names[2] - 6, vr[1] + 4), th["dim"], "tr")
    sv = min(app.sel_voice, nv - 1)
    text(surf, f.small, "V%d" % (sv + 1), (names[0] + names[2] - 6, vr[1] + 4 + f.line), vcol(th, sv), "tr")
    t0, t1 = v.scroll_tick, v.scroll_tick + app.geo["grid"][2] / v.ppt
    for di in range(rl.nd):
        r = app.geo["dac%d" % di]
        clip(surf, r)
        surf.fill(th["white_row"] if di % 2 == 0 else th["black_row"], r)
        pygame.draw.line(surf, th["line"], (r[0], r[1]), (r[0] + r[2], r[1]))
        col = note_color(app, nv + di)
        for b in rl.dac[di]:
            if b.start + b.dur < t0 or b.start > t1:
                continue
            x = v.x_of(b.start, r[0])
            w = max(3.0, b.dur * v.ppt - 1)
            rect = pygame.Rect(int(x), r[1] + 3, int(w), r[3] - 6)
            pygame.draw.rect(surf, col, rect, border_radius=3)
            if app.hit_sel == (nv + di, b.start):
                pygame.draw.rect(surf, th["text"], rect.inflate(2, 2), 2, border_radius=4)
            if rect.w > 22:
                text(surf, f.small, b.name, (rect.x + 4, rect.centery), th["accent_text"] if sum(col) > 380 else (255, 255, 255),
                     "ml", rect.w - 6)
    d = app.drag
    if d and d["kind"] in ("hit_new", "hit_move") and d.get("moved"):
        r = app.geo["dac%d" % (d["lane"] - nv)]
        clip(surf, r)
        w = app.snap_ticks if d["kind"] == "hit_new" else next(
            (b.dur for b in rl.dac_of(d["lane"]) if b.start == d["start"]), app.snap_ticks)
        rect = pygame.Rect(int(v.x_of(d["new"], r[0])), r[1] + 3, int(max(3, w * v.ppt - 1)), r[3] - 6)
        pygame.draw.rect(surf, th["text"], rect, 2, border_radius=3)
    r = app.geo["vel"]
    clip(surf, r)
    surf.fill(th["panel"], r)
    pygame.draw.line(surf, th["line"], (r[0], r[1]), (r[0] + r[2], r[1]))
    for frac in (0.25, 0.5, 0.75):
        y = r[1] + r[3] - 4 - (r[3] - 8) * frac
        pygame.draw.line(surf, th["grid"], (r[0], y), (r[0] + r[2], y))
    for vi in [i for i in range(nv) if i != app.sel_voice] + [min(app.sel_voice, nv - 1)]:
        dim = vi != app.sel_voice
        col = note_color(app, vi, dim)
        for idx, n in enumerate(rl.notes[vi]):
            if n.start + n.dur < t0 or n.start > t1:
                continue
            x = int(v.x_of(n.start, r[0]))
            hh = max(2, int((r[3] - 8) * n.vol / 15.0))
            pygame.draw.rect(surf, col, (x, r[1] + r[3] - 4 - hh, max(2, int(min(6, n.dur * v.ppt))), hh), border_radius=1)
            if app.note_sel == (vi, idx):
                pygame.draw.rect(surf, th["text"], (x - 1, r[1] + r[3] - 5 - hh, 5, hh + 2), 1)


def scrollbar(app, axis):
    surf, th = app.screen, app.th
    tr, off, ln = app.thumb(axis)
    clip(surf, tr)
    surf.fill(th["panel"], tr)
    hot = app.drag and app.drag["kind"] == axis + "bar"
    if axis == "h":
        thumb = (tr[0] + off, tr[1] + 2, ln, tr[3] - 4)
    else:
        thumb = (tr[0] + 2, tr[1] + off, tr[2] - 4, ln)
    rrect(surf, th["accent"] if hot else mix(th["bar"], th["panel"], 0.2), thumb, 4)


# ------------------------------------------------------------------ inspector
def inspector(app):
    surf, th, f, s = app.screen, app.th, app.fonts, app.scale
    r = app.layout.right
    panel_bg(app, r)
    x, y0, w, h = r
    pad = int(12 * s)
    y = y0 + pad - app.insp_scroll
    inner = w - 2 * pad - 4

    def head(title):
        nonlocal y
        text(surf, f.small, title.upper(), (x + pad, y), th["dim"])
        y += f.line
        pygame.draw.line(surf, th["line"], (x + pad, y - 2), (x + w - pad - 4, y - 2))
        y += int(4 * s)

    def row(k, val, col=None):
        nonlocal y
        text(surf, f.small, k, (x + pad, y), th["dim"])
        text(surf, f.ui, str(val), (x + w - pad - 4, y), col or th["text"], "tr", inner - f.small.size(k)[0] - 8)
        y += int(f.line * 1.15)

    if app.song is None:
        text(surf, f.ui, "Nothing open", (x + pad, y), th["dim"])
        return
    t, rl = app.tune, app.roll
    head("Song")
    ed = app.ed
    sq = int(f.line * 1.12)

    def step_row(label, shown, minus, plus, lo=True, hi=True, wide=False):
        nonlocal y
        text(surf, f.small, label, (x + pad, y + sq // 2), th["dim"], "ml")
        px = x + w - pad - 4 - sq
        vw = int(inner * 0.5) if wide else int(f.small.size("00000000")[0] * 1.3)
        if app.ui.button(surf, th, f, (px, y, sq, sq), ">" if wide else "+", "", enabled=hi, font=f.small):
            plus()
        text(surf, f.ui, str(shown), (px - 4 - vw // 2, y + sq // 2), th["text"], "mc", vw)
        if app.ui.button(surf, th, f, (px - vw - 8 - sq, y, sq, sq), "<" if wide else "-", "", enabled=lo, font=f.small):
            minus()
        y += int(sq * 1.18)

    def text_row(label, value, key):
        nonlocal y
        text(surf, f.small, label, (x + pad, y + sq // 2), th["dim"], "ml")
        bw = f.small.size("Edit")[0] + int(16 * s)
        if app.ui.button(surf, th, f, (x + w - pad - 4 - bw, y, bw, sq), "Edit", "Change the " + label.lower(), font=f.small):
            app.ask(label, getattr(app.song, key) or "", lambda v: app.set_song(**{key: v}))
        text(surf, f.ui, value or "-", (x + w - pad - 4 - bw - 6, y + sq // 2), th["text"], "mr", inner - bw - f.small.size(label)[0] - 16)
        y += int(sq * 1.18)

    def btn_row(*items):
        nonlocal y
        gap = int(6 * s)
        bw = (inner - gap * (len(items) - 1)) // len(items)
        bh = int(f.line * 1.25)
        for k, (label, tip, ok, act) in enumerate(items):
            if app.ui.button(surf, th, f, (x + pad + k * (bw + gap), y, bw, bh), label, tip, enabled=ok, font=f.small):
                act()
        y += int(bh * 1.25)

    t, rl = app.tune, app.roll
    text_row("Title", app.song.title or app.label, "title")
    text_row("Author", app.song.author, "author")
    bpm = app.song.bpm
    step_row("Tempo", "%d bpm" % bpm, lambda: app.set_song(bpm=max(30, bpm - 5)), lambda: app.set_song(bpm=min(300, bpm + 5)),
             bpm > 30, bpm < 300)
    times = [(2, 4), (3, 4), (4, 4), (5, 4), (3, 8), (6, 8), (7, 8), (9, 8), (12, 8)]
    if tuple(t.time) not in times:
        times.append(tuple(t.time))
    ti = times.index(tuple(t.time))
    step_row("Time", "%d/%d" % t.time, lambda: app.set_song(time=times[(ti - 1) % len(times)]),
             lambda: app.set_song(time=times[(ti + 1) % len(times)]), wide=True)
    bar = max(1, t.bar_ticks)
    cur_bar = app.cursor_tick // bar + 1
    text(surf, f.small, "Loop", (x + pad, y + 2), th["dim"])
    text(surf, f.ui, "from bar %d" % (t.loop_tick // bar + 1) if t.loop_tick else "none (whole song)", (x + w - pad - 4, y), th["text"], "tr")
    y += int(f.line * 1.15)
    btn_row(("Loop from bar %d" % cur_bar, "Loop from the bar where Play starts (click the ruler to move it)", app.cursor_tick > 0,
             lambda: app.set_loop(app.cursor_tick)),
            ("No loop point", "Play from the start again each time", bool(t.loop_tick), lambda: app.set_loop(None)))
    btn_row(("Trim end rests", "Cut the song down to where its last note ends: erased notes leave rests that keep it long",
             ed.trailing_rest_ticks() > 0, app.trim_end))
    marks = ed.tempo_marks()
    text(surf, f.small, "Tempo changes", (x + pad, y + 2), th["dim"])
    y += int(f.line * 1.15)
    for tick, mb in marks:
        text(surf, f.ui, "bar %d: %d bpm" % (tick // bar + 1, mb), (x + pad + 6, y + sq // 2), th["text"], "ml")
        bw = sq
        if app.ui.button(surf, th, f, (x + w - pad - 4 - bw, y, bw, sq), "x", "Remove this tempo change", font=f.small):
            app.edit(lambda tk=tick: ed.remove_tempo(tk))
        if app.ui.button(surf, th, f, (x + w - pad - 8 - 2 * bw, y, bw, sq), "+", "Faster by 5", font=f.small):
            app.edit(lambda tk=tick, m=mb: ed.set_tempo_at(tk, min(300, m + 5)))
        if app.ui.button(surf, th, f, (x + w - pad - 12 - 3 * bw, y, bw, sq), "-", "Slower by 5", font=f.small):
            app.edit(lambda tk=tick, m=mb: ed.set_tempo_at(tk, max(30, m - 5)))
        y += int(sq * 1.18)
    btn_row(("Change tempo at bar %d" % cur_bar, "Add a tempo change where Play starts", app.cursor_tick > 0,
             lambda: app.add_tempo_here()))
    e = t.echo
    step_row("Echo", "%d ms" % round(e[0] * 1000) if e else "off",
             lambda: app.set_song(echo=None if e and e[0] <= 0.02 + 1e-9 else ((max(0.02, e[0] - 0.02),) + tuple(e[1:]) if e else None)),
             lambda: app.set_song(echo=((min(1.0, e[0] + 0.02),) + tuple(e[1:])) if e else (0.24, 0.4, 0.3, False)),
             bool(e), (not e) or e[0] < 1.0)
    if e:
        step_row("  feedback", "%d%%" % round(e[1] * 100), lambda: app.set_song(echo=(e[0], max(0.0, e[1] - 0.05), e[2], e[3])),
                 lambda: app.set_song(echo=(e[0], min(0.9, e[1] + 0.05), e[2], e[3])), e[1] > 0.0, e[1] < 0.9)
        step_row("  mix", "%d%%" % round(e[2] * 100), lambda: app.set_song(echo=(e[0], e[1], max(0.0, e[2] - 0.05), e[3])),
                 lambda: app.set_song(echo=(e[0], e[1], min(1.0, e[2] + 0.05), e[3])), e[2] > 0.0, e[2] < 1.0)
        step_row("  stereo", "wide" if e[3] else "plain", lambda: app.set_song(echo=(e[0], e[1], e[2], not e[3])),
                 lambda: app.set_song(echo=(e[0], e[1], e[2], not e[3])), wide=True)
    rv = t.reverb
    step_row("Reverb", rv if rv else "off", lambda: app.set_song(reverb=max(0, rv - 1)), lambda: app.set_song(reverb=min(15, rv + 1)),
             rv > 0, rv < 15)
    dt = t.detune
    step_row("Tuning", "%+d cents" % dt if dt else "0", lambda: app.set_song(detune=max(-1200, dt - 5)),
             lambda: app.set_song(detune=min(1200, dt + 5)), dt > -1200, dt < 1200)
    step_row("Plays", "once" if t.once else "looping", lambda: app.set_song(once=not t.once),
             lambda: app.set_song(once=not t.once), wide=True)
    # the scale guide shades the roll; it changes nothing in the song
    g = app.guide
    keys = ("off",) + M.NOTE_NAMES
    scales = list(SCALES)
    gi = 0 if g is None else 1 + g[0]
    step_row("Scale guide", keys[gi], lambda: app.set_guide(keys, scales, gi - 1, g), lambda: app.set_guide(keys, scales, gi + 1, g),
             wide=True)
    if g is not None:
        si = scales.index(g[1]) if g[1] in scales else 0
        step_row("  scale", g[1], lambda: app.set_guide(keys, scales, gi, g, scales[(si - 1) % len(scales)]),
                 lambda: app.set_guide(keys, scales, gi, g, scales[(si + 1) % len(scales)]), wide=True)
    if t.warnings:
        row("Notes", "%d warning%s" % (len(t.warnings), "" if len(t.warnings) == 1 else "s"), th["warn"])
        app.warning_rect = None
        for wmsg in t.warnings[:3]:
            lines = wrap(f.small, wmsg, inner)
            for ln in lines[:2]:
                text(surf, f.small, ln, (x + pad, y), th["warn"])
                y += f.line - 2
    uses = ed.clip_uses()
    if uses:
        y += int(6 * s)
        head("Patterns")
        for name, n in uses.items():
            text(surf, f.ui, name, (x + pad + 6, y + sq // 2), th["text"], "ml", inner // 2)
            text(surf, f.small, "%d use%s" % (n, "" if n == 1 else "s"), (x + w - pad - 4 - (sq + 4 if n == 0 else 0), y + sq // 2),
                 th["dim"], "mr")
            if n == 0 and app.ui.button(surf, th, f, (x + w - pad - 4 - sq, y, sq, sq), "x", "Delete this unused pattern", font=f.small):
                app.edit(lambda nm=name: ed.delete_clip(nm))
            y += int(sq * 1.18)
    y += int(10 * s)
    vi = app.sel_voice
    head("Voice  " + rl.names[vi])
    if vi < rl.nv:
        notes = rl.notes[vi]
        if not notes:
            text(surf, f.ui, "No notes", (x + pad, y), th["dim"])
            y += f.line
        else:
            inst = first_inst(app, vi)
            ed = app.ed
            iname = ed.voice_instrument(vi)
            names = ed.instrument_names()

            def ctl(k, shown, minus, plus, lo_ok=True, hi_ok=True, tip=""):
                """label, then  [<] value [>]  on the right."""
                nonlocal y
                sq = int(f.line * 1.12)
                text(surf, f.small, k, (x + pad, y + sq // 2), th["dim"], "ml")
                px = x + w - pad - 4 - sq
                ui = app.ui
                if ui.button(surf, th, f, (px, y, sq, sq), "+" if tip != "pick" else ">", "", enabled=hi_ok, font=f.small):
                    plus()
                vw = int(f.small.size("0000000")[0] * 1.1) if tip != "pick" else int(inner * 0.5)
                text(surf, f.ui, str(shown), (px - 4 - vw // 2, y + sq // 2), th["text"], "mc", vw)
                if ui.button(surf, th, f, (px - vw - 8 - sq, y, sq, sq), "-" if tip != "pick" else "<", "", enabled=lo_ok,
                             font=f.small):
                    minus()
                y += int(sq * 1.18)

            def num(label, field, lo, hi, step=1):
                cur = getattr(inst, field)
                ctl(label, cur, lambda: app.set_inst(vi, **{field: max(lo, cur - step)}),
                    lambda: app.set_inst(vi, **{field: min(hi, cur + step)}), cur > lo, cur < hi)

            def cycle(label, field, words, show=lambda w: w or "off"):
                cur = getattr(inst, field)
                i = words.index(cur) if cur in words else 0
                ctl(label, show(cur), lambda: app.set_inst(vi, **{field: words[(i - 1) % len(words)]}),
                    lambda: app.set_inst(vi, **{field: words[(i + 1) % len(words)]}))

            pos = names.index(iname) if iname in names else -1
            ctl("Instrument", iname or "(default)",
                lambda: app.pick_instrument(vi, names[(pos - 1) % len(names)]),
                lambda: app.pick_instrument(vi, names[(pos + 1) % len(names)]), tip="pick")
            users = [rl.names[k] for k in range(rl.nv) if k != vi and iname and ed.voice_instrument(k) == iname]
            bw = int(f.line * 1.25)
            if ui_button_row(app, x + pad, y, inner, bw, "Make my own copy" if iname else "Give this voice its own",
                             "Copy the instrument so changing it leaves the other voices alone"):
                app.copy_instrument(vi)
            y += int(bw * 1.25)
            if users:
                text(surf, f.small, "also played by " + ", ".join(users), (x + pad, y), th["warn"], "tl", inner)
                y += int(f.line * 1.1)
            cycle("Wave", "wave", WAVES)
            if inst.wave == "pulse":
                num("Pulse width %", "pw", 1, 99, 5)
            if inst.wave == "noise":
                cycle("Noise", "nmode", ("long", "short"))
            env(app, inst, (x + pad, y, inner, int(f.line * 3.4)))
            y += int(f.line * 3.4) + int(8 * s)
            num("Attack", "a", 0, 15)
            num("Decay", "d", 0, 15)
            num("Sustain", "s", 0, 15)
            num("Release", "r", 0, 15)
            num("Volume", "vol", 0, 15)
            num("Pan", "pan", -8, 8)
            num("Echo send", "send", 0, 15)
            cycle("Filter", "filt", ("", "lp", "bp", "hp"))
            if inst.filt:
                num("Cutoff", "filt_cut", 0, 15)
                num("Resonance", "filt_res", 0, 15)
            num("Vibrato rate", "vib_rate", 0, 15)
            num("Vibrato depth", "vib_depth", 0, 15)
            fx = effects(inst)
            if fx:
                y += int(2 * s)
                cx = x + pad
                for label in fx:
                    wd = f.small.size(label)[0] + 12
                    if cx + wd > x + w - pad:
                        cx = x + pad
                        y += int(f.line * 1.2)
                    rrect(surf, th["panel2"], (cx, y, wd, int(f.line * 1.05)), 8, th["line"])
                    text(surf, f.small, label, (cx + 6, y + int(f.line * 0.52)), th["text"], "ml")
                    cx += wd + 4
                y += int(f.line * 1.4)
            row("Notes", len(notes))
            row("Range", "%s - %s" % (M.pitch_name(min(n.pitch for n in notes)), M.pitch_name(max(n.pitch for n in notes))))
    else:
        blocks = rl.dac_of(vi)
        sq = int(f.line * 1.12)
        names = app.ed.sample_names()
        ui = app.ui
        text(surf, f.small, "Hit sample", (x + pad, y + sq // 2), th["dim"], "ml")
        px = x + w - pad - 4 - sq
        vw = int(inner * 0.5)
        if ui.button(surf, th, f, (px, y, sq, sq), ">", "Next sample", enabled=bool(names), font=f.small):
            app.pick_sample(1)
        text(surf, f.ui, app.sample_name or "(none)", (px - 4 - vw // 2, y + sq // 2), th["text"], "mc", vw)
        if ui.button(surf, th, f, (px - vw - 8 - sq, y, sq, sq), "<", "Previous sample", enabled=bool(names), font=f.small):
            app.pick_sample(-1)
        y += int(sq * 1.3)
        bw = (inner - int(6 * s)) // 2
        bh = int(f.line * 1.25)
        if ui.button(surf, th, f, (x + pad, y, bw, bh), "Preview", "Hear the picked sample", enabled=bool(app.sample_name), font=f.small):
            app.preview_sample()
        if ui.button(surf, th, f, (x + pad + bw + int(6 * s), y, bw, bh), "Import WAV...",
                     "Convert a WAV file to a drum sample (or drop one on the window)", font=f.small):
            app.import_dialog()
        y += int(bh * 1.3)
        text(surf, f.small, "Click this lane to place the sample, drag a hit to move it.", (x + pad, y), th["dim"], "tl", inner)
        y += int(f.line * 1.4)
        row("Samples played", len(blocks))
        seen = []
        for b in blocks:
            if b.name not in seen:
                seen.append(b.name)
        for name in seen[:10]:
            row("", name)
    y += int(10 * s)
    if app.note_sel:
        v, i = app.note_sel
        n = rl.notes[v][i]
        head("Selected note")
        bar, beat = divmod(n.start, max(1, t.bar_ticks))
        row("Pitch", M.pitch_name(n.pitch))
        row("Start", "bar %d, beat %.2f" % (bar + 1, beat / M.BEAT + 1))
        row("Length", "%.2f beats" % (n.dur / M.BEAT))
        row("Volume", n.vol)
        row("Instrument", n.inst)
        inside = app.ed.inside(v, i)
        if inside:
            what = ", ".join("%s %s" % (k, nm) for k, nm in inside)
            text(surf, f.small, "Inside " + what, (x + pad, y), th["warn"], "tl", inner)
            y += int(f.line * 1.15)
            bh = int(f.line * 1.25)
            if ui_button_row(app, x + pad, y, inner, bh, "Make unique", "Copy these events here so editing this note changes only this place"):
                app.make_unique_selected()
            y += int(bh * 1.25)
    app.insp_max = max(0, y + app.insp_scroll - (y0 + h) + pad)
    app.insp_scroll = min(app.insp_scroll, app.insp_max)


def ui_button_row(app, x, y, w, h, label, tip):
    return app.ui.button(app.screen, app.th, app.fonts, (x, y, w, h), label, tip, font=app.fonts.small)


def first_inst(app, vi):
    for n in app.tune.voices[vi]:
        return n.inst
    return None


def effects(inst):
    out = []
    if inst.vib_rate and inst.vib_depth:
        out.append("vib %d,%d" % (inst.vib_rate, inst.vib_depth))
    if inst.slide:
        out.append("slide %d" % inst.slide)
    if inst.arp:
        out.append("arp " + ",".join(str(x) for x in inst.arp))
    if inst.pws_rate:
        out.append("pws %d-%d" % (inst.pws_from, inst.pws_to))
    if inst.filt:
        out.append("%s %d,%d" % (inst.filt, inst.filt_cut, inst.filt_res))
    if inst.fs_rate:
        out.append("fsweep %d-%d" % (inst.fs_from, inst.fs_to))
    if inst.wave == "noise" and inst.nmode == "short":
        out.append("short noise")
    return out


def wrap(font, s, width):
    words, lines, cur = s.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.size(trial)[0] <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def env_points(inst, w, h):
    """The envelope drawn as points in a w x h box: attack, decay to sustain, hold, release."""
    a, d, r = ATTACK_MS[inst.a & 15], DECAY_MS[inst.d & 15], DECAY_MS[inst.r & 15]
    sus = (inst.s & 15) / 15.0
    seg = [math.sqrt(a), math.sqrt(min(d, 3000)), 7.0, math.sqrt(min(r, 3000))]
    total = sum(seg)
    xs = [0]
    for sgm in seg:
        xs.append(xs[-1] + sgm / total * w)
    pts = [(xs[0], h)]
    pts.append((xs[1], 0))
    n = 14
    for k in range(1, n + 1):
        t = k / n
        lvl = sus + (1 - sus) * math.exp(-6.9 * t)
        pts.append((xs[1] + (xs[2] - xs[1]) * t, h - lvl * h))
    pts.append((xs[3], h - sus * h))
    for k in range(1, n + 1):
        t = k / n
        pts.append((xs[3] + (xs[4] - xs[3]) * t, h - sus * math.exp(-6.9 * t) * h))
    return pts, xs


def env(app, inst, rect):
    surf, th, f = app.screen, app.th, app.fonts
    rrect(surf, th["panel2"], rect, 6, th["line"])
    x, y, w, h = rect
    ix, iy, iw, ih = x + 8, y + 8, w - 16, h - 16 - f.small.get_linesize() + 2
    pts, xs = env_points(inst, iw, ih)
    pts = [(ix + px, iy + py) for px, py in pts]
    fill = pts + [(pts[-1][0], iy + ih), (pts[0][0], iy + ih)]
    shade = pygame.Surface((w, h), pygame.SRCALPHA)
    pygame.draw.polygon(shade, app.th["accent"] + (50,), [(px - x, py - y) for px, py in fill])
    surf.blit(shade, (x, y))
    pygame.draw.lines(surf, th["accent"], False, pts, 2)
    for k, label in enumerate("ADSR"):
        lx = ix + (xs[k] + xs[k + 1]) / 2
        text(surf, f.small, label, (lx, y + h - 3), th["dim"], "bc")


# ------------------------------------------------------------------ bottom: overview and status
def overview_rect(app):
    b = app.layout.bottom
    return (b[0] + 8, b[1] + 6, max(0, b[2] - 16), max(0, b[3] - app.fonts.line - 14))


def bottom(app):
    surf, th, f, ui = app.screen, app.th, app.fonts, app.ui
    b = app.layout.bottom
    panel_bg(app, b)
    ov = overview_rect(app)
    rrect(surf, th["bg"], ov, 4, th["line"])
    rl = app.roll
    if rl and rl.length and ov[2] > 10 and ov[3] > 8:
        clip(surf, ov)
        total = float(rl.length)
        span = rl.high - rl.low + 1
        for vi in range(rl.nv):
            col = note_color(app, vi, vi != app.sel_voice)
            for n in rl.notes[vi]:
                x = ov[0] + n.start / total * ov[2]
                y = ov[1] + 3 + (rl.high - n.pitch) / span * (ov[3] - 8)
                pygame.draw.rect(surf, col, (x, y, max(1.0, n.dur / total * ov[2]), max(1.5, (ov[3] - 8) / span)))
        for di in range(rl.nd):
            col = note_color(app, rl.nv + di)
            for blk in rl.dac[di]:
                x = ov[0] + blk.start / total * ov[2]
                pygame.draw.rect(surf, col, (x, ov[1] + ov[3] - 4 - di * 3, max(1.0, blk.dur / total * ov[2]), 2))
        # the part of the song in view
        g = app.geo["grid"]
        vx = ov[0] + app.view.scroll_tick / total * ov[2]
        vw = (g[2] / app.view.ppt) / total * ov[2]
        shade = pygame.Surface((max(2, int(vw)), ov[3]), pygame.SRCALPHA)
        shade.fill(th["accent"] + (46,))
        surf.blit(shade, (vx, ov[1]))
        pygame.draw.rect(surf, th["accent"], (vx, ov[1], max(2, vw), ov[3]), 1)
        tick = app.playhead()
        if tick is not None:
            px = ov[0] + tick / total * ov[2]
            pygame.draw.line(surf, th["text"], (px, ov[1]), (px, ov[1] + ov[3]), 2)
        if ui.hover(ov):
            ui.tip = "Click or drag to move around the song"
    clip(surf, b)
    sy = b[1] + b[3] - f.line - 4
    pygame.draw.line(surf, th["line"], (b[0], sy - 2), (b[0] + b[2], sy - 2))
    import time
    if app.message and time.time() < app.message_until:
        msg, col = app.message, th["warn"]
    elif ui.tip:
        msg, col = ui.tip, th["text"]
    elif app.note_hover and app.roll:
        n = app.roll.notes[app.note_hover[0]][app.note_hover[1]]
        msg = "%s  %s  bar %d  %.2f beats  vol %d  %s%s" % (app.roll.names[n.voice], M.pitch_name(n.pitch),
                                                        n.start // max(1, rl.bar_ticks) + 1, n.dur / M.BEAT, n.vol, n.inst,
                                                        "" if n.fx == "none" else "  " + n.fx)
        col = th["text"]
    else:
        msg, col = "Space play/stop   Ctrl+wheel zoom   Shift+wheel scroll   F1 help", th["dim"]
    hint = "F1 help"
    hr = text(surf, f.small, hint, (b[0] + b[2] - 10, sy + 2), th["accent"], "tr")
    if ui.hover(hr):
        ui.tip = "Keys and mouse (F1)"
        pygame.draw.line(surf, th["accent"], (hr.x, hr.bottom), (hr.right, hr.bottom))
    if ui.clicked(hr):
        app.overlay = "help"
    text(surf, f.small, msg, (b[0] + 10, sy + 2), col, "tl", b[2] - 30 - hr.w)


# ------------------------------------------------------------------ overlays
def modal(app, w, h):
    surf, th = app.screen, app.th
    sw, sh = surf.get_size()
    shade = pygame.Surface((sw, sh), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 120))
    surf.blit(shade, (0, 0))
    r = pygame.Rect((sw - w) // 2, (sh - h) // 2, w, h)
    rrect(surf, th["panel"], r, 10, th["line"], 2)
    surf.set_clip(r)
    return r


def songs_overlay(app):
    surf, th, f, ui, s = app.screen, app.th, app.fonts, app.ui, app.scale
    sw, sh = surf.get_size()
    r = modal(app, min(sw - 80, int(620 * s)), min(sh - 80, int(520 * s)))
    pad = int(14 * s)
    text(surf, f.bold, "Songs", (r.x + pad, r.y + pad), th["text"])
    if ui.button(surf, th, f, (r.right - pad - int(70 * s), r.y + pad - 4, int(70 * s), int(f.line * 1.3)), "Close"):
        app.overlay = None
    y = r.y + pad * 2 + f.line - app.overlay_scroll * f.line
    refs = app.packages.songs()
    ui.tip = ""
    if not refs:
        text(surf, f.ui, "No songs found yet.", (r.x + pad, y), th["dim"])
        text(surf, f.small, "Drop a .mml or .csp file on the window, or press Ctrl+O.", (r.x + pad, y + f.line), th["dim"])
    body = pygame.Rect(r.x, r.y + pad * 2 + f.line, r.w, r.h - pad * 2 - f.line)
    surf.set_clip(body)
    last = None
    for ref in refs:
        if ref.package != last:
            last = ref.package
            text(surf, f.small, ref.package.upper() + "   " + ref.path.replace("\\", "/").split("/")[-1], (r.x + pad, y + 2), th["dim"])
            y += int(f.line * 1.2)
        row = pygame.Rect(r.x + pad, y, r.w - 2 * pad, int(f.line * 1.35))
        cur = ref.id == app.song_id
        hot = ui.hover(row) and body.collidepoint(ui.pos)
        if cur or hot:
            rrect(surf, th["accent"] if cur else th["hot"], row, 5)
        text(surf, f.ui, ref.title, (row.x + 10, row.centery), th["accent_text"] if cur else th["text"], "ml", row.w // 2)
        text(surf, f.small, ref.id, (row.right - 10, row.centery), th["accent_text"] if cur else th["dim"], "mr", row.w // 2 - 20)
        if ui.clicked(row) and body.collidepoint(ui.pos):
            app.overlay = None
            app.open_song_id(ref.id)
        y += int(f.line * 1.4)
    if app.packages.problems:
        y += 6
        for p in app.packages.problems[:5]:
            text(surf, f.small, p, (r.x + pad, y), th["warn"], "tl", r.w - 2 * pad)
            y += f.line - 2
    surf.set_clip(None)


def prompt_overlay(app):
    surf, th, f, ui, s = app.screen, app.th, app.fonts, app.ui, app.scale
    sw, sh = surf.get_size()
    r = modal(app, min(sw - 80, int(520 * s)), int(f.line * 7))
    pad = int(16 * s)
    p = app.prompt
    text(surf, f.bold, p["label"], (r.x + pad, r.y + pad), th["text"])
    box = pygame.Rect(r.x + pad, r.y + pad + int(f.line * 1.6), r.w - 2 * pad, int(f.line * 1.5))
    rrect(surf, th["bg"], box, 4, th["accent"])
    shown = p["text"] + ("|" if int(__import__("time").time() * 2) % 2 == 0 else " ")
    text(surf, f.ui, shown, (box.x + 8, box.centery), th["text"], "ml", box.w - 16)
    bw = int(90 * s)
    y = box.bottom + int(f.line * 0.6)
    if ui.button(surf, th, f, (r.right - pad - bw, y, bw, int(f.line * 1.3)), "OK", "Enter"):
        app.finish_prompt(True)
    if ui.button(surf, th, f, (r.right - pad - 2 * bw - 8, y, bw, int(f.line * 1.3)), "Cancel", "Esc"):
        app.finish_prompt(False)
    surf.set_clip(None)


MOOD_LABEL = {"": "any"}


def new_overlay(app):
    """The New-song dialog: kind and mood drop-downs, a seed box with - / + and Random, OK / Cancel."""
    surf, th, f, ui, s = app.screen, app.th, app.fonts, app.ui, app.scale
    sw, sh = surf.get_size()
    d = app.newdlg
    r = modal(app, min(sw - 40, int(460 * s)), int(f.line * 11.8))
    pad = int(16 * s)
    text(surf, f.bold, "New song from the generator", (r.x + pad, r.y + pad), th["text"])
    lw = int(70 * s)
    fx = r.x + pad + lw
    fw = r.w - 2 * pad - lw
    rh = int(f.line * 1.5)
    y = r.y + pad + int(f.line * 1.7)
    ring = th["accent"]
    # while a list is open the other widgets behind it must not see the mouse
    open_key = ui.dd_open
    held = (ui.click, ui.pos)
    kinds = list(musicgen.KINDS)
    moods = [MOOD_LABEL.get(m, m) for m in musicgen.MOODS]
    rows = (("Kind", "kind", kinds), ("Mood", "mood", moods))
    boxes = []
    for label, key, opts in rows:
        text(surf, f.ui, label, (r.x + pad, y + rh // 2), th["dim"], "ml")
        boxes.append((key, opts, pygame.Rect(fx, y, int(min(fw, 220 * s)), rh)))
        y += rh + int(f.line * 0.5)
    # the seed row
    text(surf, f.ui, "Seed", (r.x + pad, y + rh // 2), th["dim"], "ml")
    box = pygame.Rect(fx, y, int(100 * s), rh)
    if open_key is not None:
        ui.click, ui.pos = None, (-1, -1)
    rrect(surf, th["bg"], box, 4, ring if d["focus"] == "seed" else th["line"])
    caret = "|" if d["focus"] == "seed" and int(__import__("time").time() * 2) % 2 == 0 else ""
    text(surf, f.ui, d["seed"] + caret, (box.x + 8, box.centery), th["text"], "ml", box.w - 12)
    if ui.clicked(box):
        d["focus"] = "seed"
    sq = rh
    x = box.right + 6
    app.geo["new"] = dict(seed=box, minus=pygame.Rect(x, y, sq, sq), plus=pygame.Rect(x + sq + 4, y, sq, sq),
                          random=pygame.Rect(x + 2 * sq + 12, y, int(90 * s), sq),
                          ok=pygame.Rect(r.right - pad - int(90 * s), r.bottom - pad - int(f.line * 1.3), int(90 * s), int(f.line * 1.3)),
                          cancel=pygame.Rect(r.right - pad - 2 * int(90 * s) - 8, r.bottom - pad - int(f.line * 1.3), int(90 * s), int(f.line * 1.3)),
                          kind=boxes[0][2], mood=boxes[1][2])
    if ui.button(surf, th, f, (x, y, sq, sq), "−", "Previous seed"):
        app.new_seed_step(-1)
        d["focus"] = "seed"
    if ui.button(surf, th, f, (x + sq + 4, y, sq, sq), "+", "Next seed"):
        app.new_seed_step(1)
        d["focus"] = "seed"
    rw = int(90 * s)
    if ui.button(surf, th, f, (x + 2 * sq + 12, y, rw, sq), "Random", "Pick a random seed (Ctrl+R)"):
        app.new_seed_random()
        d["focus"] = "seed"
    y += rh + int(f.line * 0.5)
    text(surf, f.small, "The same kind, mood and seed always make the same tune.", (r.x + pad, y), th["dim"], "tl", r.w - 2 * pad)
    bw = int(90 * s)
    by = r.bottom - pad - int(f.line * 1.3)
    if ui.button(surf, th, f, (r.right - pad - bw, by, bw, int(f.line * 1.3)), "OK", "Enter"):
        app.finish_new(True)
    if ui.button(surf, th, f, (r.right - pad - 2 * bw - 8, by, bw, int(f.line * 1.3)), "Cancel", "Esc"):
        app.finish_new(False)
    if open_key is not None:
        ui.click, ui.pos = held
    # the drop-downs last-but-one so the open list lands on top of everything
    for key, opts, rect in boxes:
        if ui.dropdown(surf, th, f, rect, key, opts, d[key], focus=d["focus"] == key):
            d["focus"] = key
    pick = ui.finish_dropdowns(surf, th, f)
    if pick:
        app.new_dialog_set(*pick)
    surf.set_clip(None)


HELP = (
    ("Space", "play / stop"), ("Home", "back to the start"), ("L", "loop on / off"), ("F", "follow the playhead"),
    ("1 - 6", "pick a voice (V1-V4, D1, D2)"), ("M / S", "mute / solo the picked voice"),
    ("Wheel", "scroll up and down (Tracks: scroll the track list)"), ("Shift+wheel", "scroll along the song"),
    ("Ctrl+wheel", "zoom along the song"), ("Ctrl+Shift+wheel", "zoom the rows (Tracks: change the track height)"),
    ("+ / -", "zoom along the song"), ("Ctrl + / -", "larger / smaller text"),
    ("Arrows", "scroll (Shift for bigger steps); in Tracks, Up / Down pick the track above / below"), ("Middle drag", "pan"),
    ("Tracker keys", "z s x d c v ... and q 2 w 3 e ... are a piano; - / + octave"),
    ("Tracker , / .", "volume of the note at the cursor, down / up (Shift: by 4)"),
    ("Tracker ; / :", "effect of the note at the cursor: next / previous (~ vibrato, / slide, ^ arpeggio)"),
    ("Tab", "switch between Tracks, the piano roll and the tracker"),
    ("Click ruler", "set where Play starts"), ("Click a note", "select it (the inspector shows it)"),
    ("Click empty", "add a note (drag to set its length)"),
    ("Shift+click", "add a note to the selection"), ("Ctrl+drag", "box-select notes of the picked voice (Shift adds)"),
    ("Ctrl+A", "select the picked voice's notes (Shift: all voices)"),
    ("Ctrl+C / X / V", "copy / cut / paste at the Play-from marker"), ("Ctrl+D", "duplicate the selection after itself"),
    ("Ctrl+N", "new song from the generator (a dialog: kind, mood, seed)"),
    ("P", "make a pattern from the selected notes"), ("Shift+Space", "loop just the selected stretch"),
    ("Q", "snap the selected notes to the grid"), ("Ctrl+E", "export a MIDI file (drop a .mid to import one)"), ("Drag a note", "move it; drag its right end to resize"),
    ("Drum lanes", "click D1 / D2 to place the picked sample; drag to move"),
    ("Drop a .wav", "import it as a drum sample"),
    ("Delete", "erase the selected note or drum hit"), ("Arrows (note)", "with a note picked: nudge it (Shift = octave)"),
    ("K", "set the key (e.g. D minor): the scale guide, and the song's key lines keep their notes"),
    ("Alt+Up / Down", "move the selected notes one step up / down the guide's scale"),
    ("[ / ]", "finer / coarser snap grid"), ("Ctrl+Z / Y", "undo / redo"), ("Ctrl+S", "save (Ctrl+Shift+S: save as)"),
    ("T", "light / dark theme"), ("Ctrl+O", "open a file (or drop one on the window)"),
    ("Drag borders", "resize the panels"), ("Esc", "close this"))


def help_overlay(app):
    surf, th, f, ui, s = app.screen, app.th, app.fonts, app.ui, app.scale
    sw, sh = surf.get_size()
    cols = 2 if sw >= 1000 else 1
    per = -(-len(HELP) // cols)
    r = modal(app, min(sw - 60, int(cols * 560 * s)), min(sh - 40, int((per + 3) * f.line * 1.15 + 40)))
    pad = int(16 * s)
    text(surf, f.bold, "Keys and mouse", (r.x + pad, r.y + pad), th["text"])
    colw = (r.w - 2 * pad) // cols
    step = min(f.line * 1.12, (r.h - pad * 2 - f.line * 1.5) / per)
    for ci in range(cols):
        y = r.y + pad + int(f.line * 1.5)
        x = r.x + pad + ci * colw
        for k, d in HELP[ci * per:(ci + 1) * per]:
            text(surf, f.mono, k, (x, y), th["accent"], "tl", int(170 * s) - 8)
            text(surf, f.ui, d, (x + int(170 * s), y), th["text"], "tl", colw - int(190 * s))
            y += step
    surf.set_clip(None)


# ------------------------------------------------------------------ the track stack
def stack(app):
    """Every track as a lane of the same height, stacked and scrolled vertically."""
    surf, th, f = app.screen, app.th, app.fonts
    panel_bg(app, app.layout.center, th["bg"])
    if app.roll is None:
        c = app.layout.center
        text(surf, f.ui, "No song open.  Press Songs, or drop a .mml / .csp file here.",
             (c[0] + c[2] // 2, c[1] + c[3] // 2), th["dim"], "mc")
        return
    gx, gy, gw, gh = app.geo["grid"]
    for i, top in app.stack.visible(gh):
        stack_lane(app, i, gy + top)
    stack_ghost(app)
    stack_heads(app)
    draw_ruler(app)
    stack_playhead(app)
    scrollbar(app, "h")
    scrollbar(app, "v")


def stack_lane(app, i, y0):
    """One lane: its rows, the time lines, its notes (or drum hits) and a line under it.  `y0` is its top edge."""
    surf, th, v, rl, st = app.screen, app.th, app.view, app.roll, app.stack
    gx, gy, gw, gh = app.geo["grid"]
    h = st.height
    lane = pygame.Rect(gx, y0, gw, h).clip(pygame.Rect(app.geo["grid"]))
    if lane.h <= 0:
        return
    clip(surf, lane)
    if st.is_melodic(i):
        low, high = st.window(i, rl)
        row = st.row_px(i, rl)
        for p in range(high, low - 1, -1):
            y = y0 + (high - p) * row
            pygame.draw.rect(surf, th["black_row"] if M.is_black(p) else th["white_row"], (gx, y, gw, row + 1))
            if app.guide is not None:
                tonic, name = app.guide
                if (p - tonic) % 12 in SCALES.get(name, ()):
                    shade = pygame.Surface((int(gw), max(1, int(row) + 1)), pygame.SRCALPHA)
                    shade.fill(th["accent"] + (70 if (p - tonic) % 12 == 0 else 30,))
                    surf.blit(shade, (gx, y))
            if p % 12 == 0 and row >= 3:
                pygame.draw.line(surf, th["bar"], (gx, y + row), (gx + gw, y + row))
        time_lines(app, (gx, y0, gw, h))
        stack_notes(app, i, y0, row)
    else:
        surf.fill(th["white_row"] if i % 2 == 0 else th["black_row"], (gx, y0, gw, h))
        time_lines(app, (gx, y0, gw, h))
        stack_hits(app, i, y0)
    pygame.draw.line(surf, th["line"], (gx, y0 + h - 1), (gx + gw, y0 + h - 1))
    if i == app.sel_voice:
        pygame.draw.rect(surf, th["accent"], (gx, y0, gw, h), 1)


def stack_notes(app, i, y0, row):
    surf, th, v, rl, f = app.screen, app.th, app.view, app.roll, app.fonts
    gx, gy, gw, gh = app.geo["grid"]
    t0, t1 = v.scroll_tick, v.scroll_tick + gw / v.ppt
    low, high = app.stack.window(i, rl)
    col = note_color(app, i)
    edge = mix(col, (0, 0, 0), 0.4)
    for idx, n in enumerate(rl.notes[i]):
        if n.start + n.dur < t0 or n.start > t1 or not low <= n.pitch <= high:
            continue
        x = v.x_of(n.start, gx)
        w = max(2.0, n.dur * v.ppt - 1)
        y = y0 + (high - n.pitch) * row
        rect = pygame.Rect(int(x), int(y) + (1 if row >= 4 else 0), int(w), int(max(2, row - (2 if row >= 4 else 0))))
        pygame.draw.rect(surf, col, rect, border_radius=2 if row >= 4 else 0)
        if row >= 4:
            pygame.draw.rect(surf, edge, rect, 1, border_radius=2)
        if app.note_sel == (i, idx):
            pygame.draw.rect(surf, th["text"], rect.inflate(2, 2), 2, border_radius=3)
        elif (i, n.start) in app.multi:
            pygame.draw.rect(surf, th["accent"], rect.inflate(2, 2), 2, border_radius=3)
        elif app.note_hover == (i, idx):
            pygame.draw.rect(surf, mix(col, (255, 255, 255), 0.6), rect.inflate(2, 2), 1, border_radius=3)
        if rect.h >= 13 and rect.w > f.small.size("C#4")[0] + 8:
            text(surf, f.small, M.pitch_name(n.pitch), (rect.x + 4, rect.centery), th["accent_text"]
                 if sum(col) > 380 else (255, 255, 255), "ml")


def stack_hits(app, i, y0):
    """The drum hits of lane `i` (a DAC lane: nv + the DAC's index)."""
    surf, th, v, rl, f = app.screen, app.th, app.view, app.roll, app.fonts
    gx, gy, gw, gh = app.geo["grid"]
    t0, t1 = v.scroll_tick, v.scroll_tick + gw / v.ppt
    col = note_color(app, i)
    h = app.stack.height
    pad = max(2, h // 8)
    for b in rl.dac_of(i):
        if b.start + b.dur < t0 or b.start > t1:
            continue
        rect = pygame.Rect(int(v.x_of(b.start, gx)), int(y0 + pad), int(max(3.0, b.dur * v.ppt - 1)), int(h - 2 * pad))
        pygame.draw.rect(surf, col, rect, border_radius=3)
        if app.hit_sel == (i, b.start):
            pygame.draw.rect(surf, th["text"], rect.inflate(2, 2), 2, border_radius=4)
        if rect.w > 22:
            text(surf, f.small, b.name, (rect.x + 4, rect.centery), th["accent_text"] if sum(col) > 380 else (255, 255, 255),
                 "ml", rect.w - 6)


def stack_ghost(app):
    """The note or hit being placed, moved or sized by the mouse, in its own lane."""
    draw_marquee(app)
    d = app.drag
    if not d or not d.get("moved", True):
        return
    surf, th, v, st = app.screen, app.th, app.view, app.stack
    gx, gy, gw, gh = app.geo["grid"]
    clip(surf, app.geo["grid"])
    if d["kind"] in ("new", "move", "resize"):
        i = d["voice"]
        start, dur, pitch = (d["start"], d["dur"], d["pitch"]) if d["kind"] == "new" else d["new"]
        row = st.row_px(i, app.roll)
        low, high = st.window(i, app.roll)
        pitch = max(low, min(high, pitch))
        rect = pygame.Rect(int(v.x_of(start, gx)), int(gy + st.y_of(i, pitch, app.roll) + 1), int(max(3, dur * v.ppt - 1)),
                           int(max(3, row - 2)))
        col = note_color(app, i)
    elif d["kind"] in ("hit_new", "hit_move"):
        i = d["lane"]
        w = app.snap_ticks if d["kind"] == "hit_new" else next(
            (b.dur for b in app.roll.dac_of(i) if b.start == d["start"]), app.snap_ticks)
        pad = max(2, st.height // 8)
        rect = pygame.Rect(int(v.x_of(d["new"], gx)), int(gy + st.top_of(i) + pad), int(max(3, w * v.ppt - 1)),
                           int(st.height - 2 * pad))
        col = note_color(app, i)
    else:
        return
    shade = pygame.Surface(rect.size, pygame.SRCALPHA)
    shade.fill(col + (120,))
    surf.blit(shade, rect.topleft)
    pygame.draw.rect(surf, th["text"], rect, 2, border_radius=3)
    surf.set_clip(None)


def stack_heads(app):
    """The header column: one card per visible track with its name, instrument, note count and Mute / Solo."""
    surf, th, f, ui, s, st = app.screen, app.th, app.fonts, app.ui, app.scale, app.stack
    hx, hy, hw, hh = app.geo["heads"]
    clip(surf, app.geo["heads"])
    surf.fill(th["panel"], app.geo["heads"])
    muted = app.muted_now() if app.tune else set()
    counts = app.roll.counts()
    app.voice_cards = [None] * st.count
    names, nv = app.roll.names, app.roll.nv
    for i, top in st.visible(hh):
        rect = pygame.Rect(hx + 3, hy + top + 2, hw - 7, st.height - 4)
        app.voice_cards[i] = tuple(rect)
        sel = i == app.sel_voice
        col = vcol(th, i)
        dead = i in muted or counts[i] == 0
        rrect(surf, th["panel2"] if not sel else mix(th["panel2"], th["accent"], 0.18), tuple(rect), 6,
              th["accent"] if sel else th["line"])
        pygame.draw.rect(surf, col if not dead else mix(col, th["panel2"], 0.65),
                         (rect.x + 1, rect.y + 6, max(4, int(5 * s)), max(4, rect.h - 12)), border_radius=3)
        tx = rect.x + int(16 * s)
        text(surf, f.bold, names[i], (tx, rect.y + int(6 * s)), th["text"] if not dead else th["dim"])
        bw = int(f.line * 1.25)
        bx = rect.right - 2 * bw - int(10 * s)
        by = rect.y + int(6 * s)
        buttons_ok = by + bw <= hy + hh and by >= hy
        if buttons_ok:
            if ui.button(surf, th, f, (bx, by, bw, bw), "M", "Mute %s (M)" % names[i], on=i in app.muted, font=f.small):
                app.toggle_mute(i)
            if ui.button(surf, th, f, (bx + bw + int(4 * s), by, bw, bw), "S", "Solo %s" % names[i], on=i in app.solos,
                         font=f.small):
                app.toggle_solo(i)
        if ui.clicked(tuple(rect)) and not (buttons_ok and ui.hover((bx, by, 2 * bw + int(4 * s), bw))):
            app.sel_voice = i
        if st.height >= int(f.line * 2.6):
            if i < nv:
                inst = voice_inst(app, i)
                sub = "%s   %d notes" % (inst, counts[i]) if counts[i] else "empty"
            else:
                sub = "%d samples" % counts[i] if counts[i] else "empty"
            text(surf, f.small, sub, (tx, rect.y + int(6 * s) + f.line), th["dim"], "tl", rect.w - int(24 * s))
    pygame.draw.line(surf, th["line"], (hx + hw - 1, hy), (hx + hw - 1, hy + hh))


def stack_playhead(app):
    tick = app.playhead()
    if tick is None:
        return
    surf, th, v = app.screen, app.th, app.view
    gx, gy, gw, gh = app.geo["grid"]
    x = v.x_of(tick, gx)
    if x < gx or x > gx + gw:
        return
    top = app.geo["ruler"][1]
    surf.set_clip(pygame.Rect(gx, top, gw, gy + gh - top))
    pygame.draw.line(surf, th["accent"], (x, top), (x, gy + gh), 2)
    surf.set_clip(None)
