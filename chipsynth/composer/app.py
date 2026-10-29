# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The composer window: state, events and playback.  Drawing is in panels.py; the maths in model.py / layout.py."""
import dataclasses
import random
import time
from pathlib import Path

import pygame

import re

from chipsynth import dpcm, midi, mml, musicgen
from chipsynth.packageset import PackageSet
from chipsynth import package as pk
from chipsynth.package import PackageError
from chipsynth.songedit import Editor, EditError
from chipsynth.song import Song, SongError, Play, Key
from chipsynth.songedit import EFFECTS, effect_of
from chipsynth.mml import SCALES
from chipsynth.synth import RATE

from . import layout as L, model as M, panels, tracker as TR, ui as U
from .prefs import Prefs
from .tracks import Stack, clamp_height

LAG = 0.16                       # seconds of sound already queued ahead of what is audible
TITLE = "chipsynth composer"
SNAPS = M.SNAPS


class NullOut:
    """Stands in for MusicOut when there is no sound (tests, no audio device)."""
    def __init__(self):
        self.player = None
        self.tune = None
        self.loop = None
        self.volume = 1.0
        self.note = ""
        self._playing = False

    @property
    def playing(self):
        return self._playing

    def start(self, tune=None, loop=True, player=None, fade_in=0.0):
        from chipsynth.player import TunePlayer
        self.tune, self.loop = tune, loop
        self.player = TunePlayer(tune, loop=loop)
        self._playing = True
        return True

    def stop(self, fade=0.0):
        self._playing = False
        return self.player


class Composer:
    def __init__(self, screen, prefs=None, packages=None, lib=None, out=None, prefs_path=None, root=None):
        self.screen = screen
        self.prefs = prefs or Prefs()
        self.prefs_path = prefs_path
        self.root = Path(root) if root else None          # where backups/ goes when saving
        self.lib = lib or mml.builtin_library()
        self.packages = packages if packages is not None else PackageSet(self.lib)
        self.out = out if out is not None else NullOut()
        self.ui = U.Ui()
        self.view = M.View(ppb=self.prefs.ppb, row=self.prefs.row)
        self.set_font(self.prefs.font)
        self.layout = L.compute(*screen.get_size(), self.prefs.panels, self.scale)
        self.geo = {}
        self.song = self.tune = self.roll = None
        self.song_id = ""
        self.label = ""
        self.source = ""
        self.sel_voice = 0
        self.voices_scroll = 0                      # pixels the Voices panel is scrolled (songs with many tracks)
        self.muted, self.solos = set(), set()
        self.loop_on, self.follow = True, True
        self.playing = False
        self.seek_tick = 0
        self.play_range = None               # (a, b) ticks when only a stretch is being auditioned
        self.play_tune = None
        self.cursor_tick = 0                 # where Play starts (set by clicking the ruler)
        self.note_sel = None                 # (voice, index into roll.notes[voice])
        self.note_hover = None
        self.drag = None
        self.overlay = None                  # None | "songs" | "help" | "prompt" | "new"
        self.overlay_scroll = 0
        self.insp_scroll = 0
        self.message, self.message_until = "", 0.0
        self.running = True
        self.dirty = False
        self.last_save = time.time()
        self.names = {}
        self.inst_names = {}
        self.cursor_kind = None
        self.ed = None                       # the Editor (the Song with undo/redo)
        self.snap_i = 1
        self.quit_warned = False
        self.sample_name = ""               # what a click in a drum lane places
        self.hit_sel = None                  # (lane, start tick) of the picked drum hit
        self.multi = set()                   # several picked notes: {(voice, start tick)}
        self.clip = []                       # copied notes
        self.last_result = None
        self.mode = self.prefs.view if self.prefs.view in ("stack", "roll", "tracker") else "roll"   # "stack", "roll" or "tracker"
        self.stack = Stack(height=int(round(self.prefs.track_h * self.scale)))
        self._stack_sel = -1                 # the voice the stack last scrolled to
        self.tr = TR.Cursor()
        self.prompt = None                   # a one-line text box: {label, text, apply}
        self.newdlg = dict(kind=musicgen.KINDS.index("town"), mood=0, seed="1", focus="kind")   # the New dialog
        self.guide = None                    # (tonic, scale name) shaded in the roll, or None
        self.imported = {}                   # samples brought in from WAV files this session

    # ------------------------------------------------------------ settings
    @property
    def th(self):
        return U.DARK if self.prefs.dark else U.LIGHT

    @property
    def scale(self):
        return self.prefs.font / 15.0

    def set_font(self, px):
        self.prefs.font = max(11, min(28, int(px)))
        self.fonts = U.Fonts(self.prefs.font)

    def say(self, s, secs=5.0):
        self.message, self.message_until = s, time.time() + secs

    def save_prefs(self):
        self.prefs.ppb, self.prefs.row = self.view.ppb, self.view.row
        self.prefs.width, self.prefs.height = self.screen.get_size()
        self.prefs.save(self.prefs_path)
        self.dirty = False
        self.last_save = time.time()

    # ------------------------------------------------------------ songs
    def load_song(self, song, label="", source="", song_id=""):
        """Show and (re)play-ready a Song.  Returns False (and says why) if it does not compile."""
        try:
            ed = Editor(song, self.lib_for(song_id))
        except mml.MMLError as e:
            self.say("Can't open %s: %s" % (label or "song", e))
            return False
        tune = ed.tune
        self.stop()
        self.ed = ed
        self.song, self.tune, self.song_id, self.source = song, tune, song_id, source
        self.quit_warned = False
        self.label = label or song.title or "Untitled"
        self.inst_names = self.build_names(tune)
        self.roll = M.Roll.from_tune(tune, self.inst_name)
        self.stack.names = self.roll.names
        self.stack.pinned = {k: v for k, v in self.stack.pinned.items() if k < self.roll.nv}
        self.relayout()                      # opened from a click while drawing: the lanes below must match at once
        self.muted, self.solos = set(), set()
        self.note_sel = self.note_hover = None
        self.hit_sel = None
        self.multi = set()
        self.guide = None
        for items in song.tracks.values():
            k = next((e for e in items if isinstance(e, Key) and e.tonic is not None), None)
            if k and (k.scale or "").lower() in SCALES:
                self.guide = (k.tonic % 12, k.scale.lower())
                break
        names = ed.sample_names()
        used = [e[2].name for lane in tune.dac for e in lane if e[1] == "play" and e[2] is not None and e[2].name in names]
        self.sample_name = used[0] if used else (names[0] if names else "")
        self.cursor_tick = 0
        self.sel_voice = next((i for i, c in enumerate(self.roll.counts()) if c), 0)
        self.fit_view()
        pygame.display.set_caption("%s - %s" % (self.label, TITLE)) if pygame.display.get_surface() else None
        self.prefs.last = song_id or source
        self.dirty = True
        return True

    def lib_for(self, song_id):
        if song_id and ":" in song_id and self.packages.has(song_id):
            pkg, _ = self.packages.split(song_id)
            return self.packages.packages[pkg].library(self.lib)
        return self.lib

    def build_names(self, tune):
        table = {}
        for name, inst in {**self.lib["instr"], **tune.instruments}.items():
            table[dataclasses.astuple(inst)] = name
        return table

    def inst_name(self, inst):
        return self.inst_names.get(dataclasses.astuple(inst), "custom")

    def open_song_id(self, song_id):
        try:
            song = self.packages.get(song_id)
        except PackageError as e:
            self.say(str(e))
            return False
        ref = next((r for r in self.packages.songs() if r.id == song_id), None)
        return self.load_song(song, ref.title if ref else song_id, ref.path if ref else "", song_id)

    def open_path(self, path):
        p = Path(path)
        try:
            if p.suffix.lower() == ".csp":
                name = None
                for n, src in self.packages.paths.items():
                    if Path(src) == p:
                        name = n
                if name is None:
                    name = self.packages.add(p).name
                ids = [r.id for r in self.packages.songs() if r.package == name]
                return bool(ids) and self.open_song_id(ids[0])
            if p.suffix.lower() in (".mid", ".midi"):
                song = midi.from_midi(p.read_bytes())
                self.say("Imported %s: the first four parts with notes. Save to keep it." % p.name, 6.0)
                return self.load_song(song, p.stem, "")
            text = p.read_text(encoding="utf-8", errors="replace")
            song = Song.from_mml(text, self.lib)
            return self.load_song(song, song.title or p.stem, str(p))
        except (PackageError, mml.MMLError, midi.MidiError, OSError) as e:
            self.say("Can't open %s: %s" % (p.name, e))
            return False

    def fit_view(self):
        g = self.geo.get("grid") or L.roll_rects(self.layout.center, self.fonts.line)["grid"]
        low, high = self.roll.low, self.roll.high
        if (high - low + 1) * self.view.row > g[3]:             # everything does not fit: show the picked voice
            own = [n.pitch for n in self.roll.notes[self.sel_voice]] if self.sel_voice < self.roll.nv else []
            if own:
                low, high = min(own), max(own)
        self.view.show(low, high, g[2], g[3])
        self.clamp_view()

    def clamp_view(self):
        g = self.geo.get("grid")
        if g and self.roll:
            self.view.clamp(g[2], g[3], self.roll.length)

    # ------------------------------------------------------------ playback
    def muted_now(self):
        return M.solo_set(self.solos, self.roll.lanes) | self.muted

    def play(self, from_tick=None):
        if self.tune is None:
            return
        tick = self.cursor_tick if from_tick is None else from_tick
        self.seek_tick = max(0, int(tick))
        if self.play_range:
            a, b = self.play_range
            seek = M.tune_range(self.tune, a, b)
            self.seek_tick = a
        else:
            seek = M.tune_from(self.tune, self.seek_tick)
        self.play_tune = M.mixdown(seek, self.muted_now())
        if not self.play_tune.length:
            self.say("Nothing to play.")
            return
        self.out.start(self.play_tune, loop=self.play_range is not None or (self.loop_on and not self.tune.once))
        self.playing = True
        if self.out.note and not getattr(self.out, "mixer", True):
            self.say(self.out.note)

    def stop(self):
        if self.playing:
            self.out.stop()
        self.playing = False

    def toggle_play(self):
        if self.playing:
            self.stop()
        else:
            self.play_range = None
            self.play()

    def selection_range(self):
        """(start, end) ticks covering the selected notes, or None."""
        notes = self.ed.selection_notes(sorted(self.selection())) if self.ed else []
        return (min(n[1] for n in notes), max(n[1] + n[2] for n in notes)) if notes else None

    def play_selection(self):
        """Shift+Space: loop just the stretch the selected notes cover (Space stops it, then plays the whole song)."""
        if self.playing:
            self.stop()
            return
        r = self.selection_range()
        if r is None:
            self.say("Select some notes first (click, or drag a box).", 3.0)
            return
        self.play_range = r
        self.play(r[0])
        if self.playing:
            self.say("Looping the selection. Space stops.", 3.0)

    def restart_here(self):
        """Mute / solo / loop changed while playing: carry on from the playhead."""
        tick = self.playhead()
        if self.playing and tick is not None:
            self.play(int(tick))

    def playhead(self):
        """The tick being heard, in the song's own timeline, or None when stopped."""
        if not self.playing or self.play_tune is None or self.out.player is None:
            return None
        if self.out.player is None:
            return None
        pos = self.out.player.position(LAG)
        ticks = self.play_tune.sample_to_tick(pos)
        if self.play_range:
            a, b = self.play_range
            return a + ticks % max(1, b - a)
        return M.source_tick(self.tune, self.seek_tick, ticks)

    # ------------------------------------------------------------ frame
    def geo_key(self):
        """What the rectangles depend on besides the panel sizes: the window, the mode and the song's track count."""
        return (self.screen.get_size(), self.mode, self.roll.lanes if self.roll else 0, self.roll.nd if self.roll else 0)

    def ensure_layout(self):
        """Lay out again if the mode, the song or the window changed since the last layout (a click during a frame
        can change them after update() ran), so drawing never uses rectangles made for something else."""
        if getattr(self, "_geo_key", None) != self.geo_key():
            self.relayout()

    def relayout(self):
        """Panel and lane rectangles for the window, the mode and the song's track count."""
        size = self.screen.get_size()
        self._geo_key = self.geo_key()
        self.layout = L.compute(*size, self.prefs.panels, self.scale, hide_left=self.mode == "stack")
        if self.mode == "stack":
            self.geo = L.stack_rects(self.layout.center, self.fonts.line)
            self.sync_stack()
        else:
            self.geo = L.roll_rects(self.layout.center, self.fonts.line, self.roll.nd if self.roll else 2)

    def update(self):
        if self.playing and not self.out.playing:
            self.playing = False                       # a once-only tune (or no loop) ran out
        self.relayout()
        if self.roll:
            self.clamp_view()
            tick = self.playhead()
            if tick is not None and self.mode == "tracker" and self.follow:
                row = int(tick // self.snap_ticks)
                g = panels.tracker_geo(self)
                if not self.tr.top <= row < self.tr.top + g["visible"] - 1:
                    self.tr.top = max(0, row - 2)
            elif tick is not None and self.follow and not self.drag:
                g = self.geo["grid"]
                x = self.view.x_of(tick, g[0])
                if x < g[0] or x > g[0] + g[2] * 0.92:
                    self.view.scroll_tick = max(0.0, tick - self.view.tick_at(g[0] + g[2] * 0.08, g[0]) + self.view.scroll_tick)
                    self.clamp_view()
        if self.dirty and time.time() - self.last_save > 5:
            self.save_prefs()

    def frame(self, events=()):
        for e in events:
            self.event(e)
        self.update()
        self.ui.begin()
        panels.draw(self)
        self.ui.end()
        self.set_cursor()

    def run(self):
        clock = pygame.time.Clock()
        while self.running:
            self.frame(pygame.event.get())
            pygame.display.flip()
            clock.tick(60)
        self.stop()
        self.save_prefs()

    def set_cursor(self):
        kind = None
        if self.drag and self.drag["kind"].startswith("split"):
            kind = self.drag["kind"]
        else:
            for name in ("split_left", "split_right", "split_bottom"):
                if name == "split_left" and self.mode == "stack":
                    continue
                if self.ui.hover(getattr(self.layout, name)):
                    kind = name
        if kind != self.cursor_kind:
            self.cursor_kind = kind
            try:
                pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_SIZENS if kind == "split_bottom" else
                                        pygame.SYSTEM_CURSOR_SIZEWE if kind else pygame.SYSTEM_CURSOR_ARROW)
            except Exception:
                pass

    # ------------------------------------------------------------ events
    def event(self, e):
        t = e.type
        if t == pygame.QUIT:
            self.request_quit()
        elif t == pygame.MOUSEMOTION:
            self.ui.pos = e.pos
            self.on_motion(e.pos)
        elif t == pygame.MOUSEBUTTONDOWN:
            self.ui.pos = e.pos
            if e.button == 1:
                self.ui.press = e.pos
                self.on_down(e.pos)
            elif e.button in (2, 3) and self.in_grid(e.pos):
                self.drag = dict(kind="pan", at=e.pos, tick=self.view.scroll_tick, top=self.view.top_pitch,
                                 scroll=self.stack.scroll)
        elif t == pygame.MOUSEBUTTONUP:
            self.ui.pos = e.pos
            if e.button == 1:
                self.ui.click = e.pos
                self.on_up(e.pos)
            self.drag = None
        elif t == pygame.MOUSEWHEEL:
            self.on_wheel(e)
        elif t == pygame.KEYDOWN:
            self.on_key(e)
        elif t == getattr(pygame, "DROPFILE", -1):
            if str(e.file).lower().endswith(".wav"):
                self.import_wav(e.file)
            else:
                self.open_path(e.file)

    def in_grid(self, pos):
        return self.roll is not None and L.contains(self.geo["grid"], *pos)

    def on_down(self, pos):
        x, y = pos
        if self.overlay:
            return
        lay = self.layout
        if lay.too_small:
            return
        for name in ("split_left", "split_right", "split_bottom"):
            if name == "split_left" and self.mode == "stack":
                continue                                       # the left panel is hidden in the track view
            if L.contains(getattr(lay, name), x, y):
                self.drag = dict(kind=name)
                return
        if self.roll is None:
            return
        if self.mode == "tracker":
            self.tracker_down(pos)
            return
        if self.mode == "stack" and self.stack_down(pos):
            return
        g = self.geo
        if L.contains(g["hbar"], x, y):
            self.drag = dict(kind="hbar", off=self.thumb_grab("h", x))
        elif L.contains(g["vbar"], x, y):
            self.drag = dict(kind="vbar", off=self.thumb_grab("v", y))
        elif L.contains(g["ruler"], x, y):
            self.cursor_tick = self.snap_bar(self.view.tick_at(x, g["ruler"][0]))
            if self.playing:
                self.play(self.cursor_tick)
            self.drag = dict(kind="ruler")
        elif L.contains(g["grid"], x, y) or L.contains(g["vel"], x, y):
            self.grid_down(pos)
        elif L.dac_at(g, x, y) is not None:
            self.lane_down(pos)
        elif L.contains(panels.overview_rect(self), x, y):
            self.drag = dict(kind="overview")
            self.overview_to(x)

    def grid_down(self, pos):
        """A click on the note grid (or the volume strip): pick, extend the selection, start a marquee, a drag or a new note."""
        x, y = pos
        g = self.geo
        hit = self.note_at(pos)
        mods = pygame.key.get_mods()
        if mods & pygame.KMOD_CTRL and L.contains(g["grid"], x, y):
            self.drag = dict(kind="marquee", at=pos, to=pos, add=bool(mods & pygame.KMOD_SHIFT))
            return
        if mods & pygame.KMOD_SHIFT and hit:
            if self.note_sel and self.note_sel != hit:
                prev = self.roll.notes[self.note_sel[0]][self.note_sel[1]]
                self.multi.add((prev.voice, prev.start))
            n = self.roll.notes[hit[0]][hit[1]]
            self.multi ^= {(n.voice, n.start)}
            self.note_sel = hit
            self.hit_sel = None
            return
        self.multi = set()
        self.note_sel = hit
        self.hit_sel = None
        if hit:
            self.sel_voice = hit[0]
            self.begin_note_drag(hit, pos)
        elif L.contains(g["grid"], x, y) and self.sel_voice < self.roll.nv:
            self.begin_new_note(pos)

    def stack_down(self, pos):
        """A press in the track stack's area.  True when it was handled here."""
        g = self.geo
        x, y = pos
        if L.contains(g["hbar"], x, y):
            self.drag = dict(kind="hbar", off=self.thumb_grab("h", x))
        elif L.contains(g["vbar"], x, y):
            self.drag = dict(kind="vbar", off=self.thumb_grab("v", y))
        elif L.contains(g["ruler"], x, y):
            self.cursor_tick = self.snap_bar(self.view.tick_at(x, g["ruler"][0]))
            if self.playing:
                self.play(self.cursor_tick)
            self.drag = dict(kind="ruler")
        elif L.contains(g["grid"], x, y):
            lane = self.stack_lane(pos)
            if lane is None:
                return True
            self.sel_voice = lane
            if lane >= self.roll.nv:
                self.lane_down(pos, lane)
            else:
                self.grid_down(pos)
        else:
            return False
        return True

    def snap_bar(self, tick):
        bt = self.roll.bar_ticks
        return max(0, int(round(tick / bt)) * bt)

    def on_motion(self, pos):
        d = self.drag
        x, y = pos
        if d is None:
            self.note_hover = self.note_at(pos) if self.roll and not self.overlay else None
            return
        k = d["kind"]
        if k == "split_left":
            L.drag_left(self.prefs.panels, x, self.scale)
        elif k == "split_right":
            L.drag_right(self.prefs.panels, x, self.layout.width, self.scale)
        elif k == "split_bottom":
            L.drag_bottom(self.prefs.panels, y, self.layout.height, self.scale)
        elif k == "pan" and self.roll:
            self.view.scroll_tick = d["tick"] - (x - d["at"][0]) / self.view.ppt
            if self.mode == "stack":
                self.stack.scroll = d["scroll"] - (y - d["at"][1])
                self.stack.clamp(self.stack_area_h())
            else:
                self.view.top_pitch = d["top"] + (y - d["at"][1]) / self.view.row
        elif k == "hbar":
            self.drag_thumb("h", x - d["off"])
        elif k == "vbar":
            self.drag_thumb("v", y - d["off"])
        elif k == "ruler":
            g = self.geo["ruler"]
            self.cursor_tick = self.snap_bar(self.view.tick_at(x, g[0]))
        elif k == "overview":
            self.overview_to(x)
        elif k == "marquee":
            d["to"] = pos
        elif k in ("move", "resize", "new", "hit_move", "hit_new"):
            self.drag_note(pos)
        self.dirty = True

    def on_wheel(self, e):
        pos = self.ui.pos
        mods = pygame.key.get_mods()
        ctrl, shift = mods & pygame.KMOD_CTRL, mods & pygame.KMOD_SHIFT
        dy = getattr(e, "precise_y", e.y)
        dx = getattr(e, "precise_x", e.x)
        if self.overlay:
            self.overlay_scroll = max(0, self.overlay_scroll - int(dy * 3))
            return
        if L.contains(self.layout.right, *pos):
            self.insp_scroll = max(0, self.insp_scroll - int(dy * 30))
            return
        if L.contains(self.layout.left, *pos):
            self.voices_scroll = max(0, self.voices_scroll - int(dy * 40))
            self.dirty = True
            return
        if self.roll is None or not (L.contains(self.layout.center, *pos)):
            return
        if self.mode == "tracker":
            self.tr.top = max(0, self.tr.top - int(dy * 3))
            return
        g = self.geo["grid"]
        if self.mode == "stack":
            if ctrl and shift:
                self.set_track_h(round(self.prefs.track_h * 1.1 ** dy) + (1 if dy > 0 else -1 if dy < 0 else 0))
            elif ctrl:
                self.view.zoom_h(1.15 ** dy, pos[0], g[0])
            elif shift or dx:
                self.view.scroll_tick -= (dy or dx) * 90 / self.view.ppt
            else:
                self.stack.wheel(dy, g[3])
            self.clamp_view()
            self.dirty = True
            return
        if ctrl and shift:
            self.view.zoom_v(1.12 ** dy, pos[1], g[1])
        elif ctrl:
            self.view.zoom_h(1.15 ** dy, pos[0], g[0])
        elif shift or dx:
            self.view.scroll_tick -= (dy or dx) * 90 / self.view.ppt * 0.5 * 2
        else:
            self.view.top_pitch += dy * 3 * (14.0 / self.view.row) * 1.0
        self.clamp_view()
        self.dirty = True

    def on_key(self, e):
        k, mods = e.key, e.mod
        ctrl = mods & pygame.KMOD_CTRL
        if self.overlay == "new":
            self.new_dialog_key(e)
            return
        if self.prompt:
            if k == pygame.K_RETURN or k == pygame.K_KP_ENTER:
                self.finish_prompt(True)
            elif k == pygame.K_ESCAPE:
                self.finish_prompt(False)
            elif k == pygame.K_BACKSPACE:
                self.prompt["text"] = self.prompt["text"][:-1]
            elif getattr(e, "unicode", "") and e.unicode.isprintable() and len(self.prompt["text"]) < 120:
                self.prompt["text"] += e.unicode
            return
        if k == pygame.K_ESCAPE:
            if self.overlay:
                self.overlay = None
            else:
                self.note_sel = self.hit_sel = None
                self.drag = None
            return
        if self.overlay == "songs" and k == pygame.K_RETURN:
            return
        if k == pygame.K_TAB and self.roll is not None:
            self.set_mode({"stack": "roll", "roll": "tracker", "tracker": "stack"}[self.mode])
            return
        if self.mode == "tracker" and self.roll is not None and not ctrl and self.tracker_key(e):
            return
        if ctrl and k in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
            self.set_font(self.prefs.font + 1)
        elif ctrl and k in (pygame.K_MINUS, pygame.K_KP_MINUS):
            self.set_font(self.prefs.font - 1)
        elif ctrl and k == pygame.K_o:
            self.open_dialog()
        elif ctrl and k == pygame.K_a and self.roll is not None:
            self.select_all(all_voices=bool(mods & pygame.KMOD_SHIFT))
        elif ctrl and k in (pygame.K_c, pygame.K_x):
            self.copy_selection(cut=k == pygame.K_x)
        elif ctrl and k == pygame.K_v:
            self.paste()
        elif ctrl and k == pygame.K_d:
            self.duplicate()
        elif ctrl and k == pygame.K_n:
            self.new_from_generator_dialog()
        elif ctrl and k == pygame.K_e:
            self.export_midi_dialog()
        elif k == pygame.K_q and self.selection():
            self.quantize_selection()
        elif ctrl and k == pygame.K_s:
            self.save_as() if mods & pygame.KMOD_SHIFT else self.save()
        elif ctrl and k == pygame.K_z:
            self.redo() if mods & pygame.KMOD_SHIFT else self.undo()
        elif ctrl and k == pygame.K_y:
            self.redo()
        elif mods & pygame.KMOD_ALT and k in (pygame.K_UP, pygame.K_DOWN) and self.selection():
            self.transpose_in_key(1 if k == pygame.K_UP else -1)
        elif k == pygame.K_k and self.roll is not None and self.ed is not None:
            g = self.guide
            self.ask("Key (e.g. D minor, F# dorian, off)", "%s %s" % (M.NOTE_NAMES[g[0]], g[1]) if g else "C major", self.set_key_text)
        elif k in (pygame.K_DELETE, pygame.K_BACKSPACE) and len(self.selection()) > 1:
            self.edit(lambda: self.ed.erase_notes(sorted(self.selection())), None)
            self.multi = set()
        elif len(self.selection()) > 1 and k in (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT):
            sign = (1, -1)[k in (pygame.K_DOWN, pygame.K_LEFT)]
            if k in (pygame.K_UP, pygame.K_DOWN):
                self.move_selection(0, sign * (12 if mods & pygame.KMOD_SHIFT else 1))
            else:
                self.move_selection(sign * self.snap_ticks, 0)
        elif k in (pygame.K_DELETE, pygame.K_BACKSPACE) and self.hit_sel:
            lane, start = self.hit_sel
            self.edit(lambda: self.ed.erase_hit(lane, start))
        elif k in (pygame.K_DELETE, pygame.K_BACKSPACE) and self.note_sel:
            self.delete_selected()
        elif self.hit_sel and k in (pygame.K_LEFT, pygame.K_RIGHT):
            lane, start = self.hit_sel
            new = max(0, start + (1 if k == pygame.K_RIGHT else -1) * self.snap_ticks)
            self.edit(lambda: self.ed.move_hit(lane, start, new))
            if self.ed.find_hit(lane, new):
                self.hit_sel = (lane, new)
        elif k == pygame.K_LEFTBRACKET:
            self.set_snap(self.snap_i - 1)
        elif k == pygame.K_RIGHTBRACKET:
            self.set_snap(self.snap_i + 1)
        elif self.note_sel and k in (pygame.K_UP, pygame.K_DOWN, pygame.K_LEFT, pygame.K_RIGHT):
            step = (1, -1)[k in (pygame.K_DOWN, pygame.K_LEFT)]
            if k in (pygame.K_UP, pygame.K_DOWN):
                self.nudge_selected(0, step * (12 if mods & pygame.KMOD_SHIFT else 1))
            else:
                self.nudge_selected(step * self.snap_ticks, 0)
        elif ctrl and k == pygame.K_q:
            self.running = False
        elif k == pygame.K_SPACE and mods & pygame.KMOD_SHIFT:
            self.play_selection()
        elif k == pygame.K_SPACE:
            self.toggle_play()
        elif k == pygame.K_p and self.selection():
            self.make_pattern_selected()
        elif k == pygame.K_HOME:
            self.cursor_tick = 0
            self.view.scroll_tick = 0
            if self.playing:
                self.play(0)
        elif k == pygame.K_F1 or (k == pygame.K_SLASH and mods & pygame.KMOD_SHIFT):
            self.overlay = None if self.overlay == "help" else "help"
        elif k == pygame.K_t:
            self.prefs.dark = not self.prefs.dark
            self.dirty = True
        elif k == pygame.K_l:
            self.loop_on = not self.loop_on
            self.restart_here()
        elif k == pygame.K_f:
            self.follow = not self.follow
        elif k == pygame.K_m:
            self.toggle_mute(self.sel_voice)
        elif k == pygame.K_s:
            self.toggle_solo(self.sel_voice)
        elif pygame.K_1 <= k <= pygame.K_9 and k - pygame.K_1 < self.roll.lanes:
            self.sel_voice = k - pygame.K_1
        elif k in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS) and self.roll:
            self.zoom_h_center(1.25)
        elif k in (pygame.K_MINUS, pygame.K_KP_MINUS) and self.roll:
            self.zoom_h_center(0.8)
        elif self.roll and k in (pygame.K_LEFT, pygame.K_RIGHT):
            self.view.scroll_tick += (1 if k == pygame.K_RIGHT else -1) * M.BEAT * (4 if mods & pygame.KMOD_SHIFT else 1)
            self.clamp_view()
        elif self.roll and self.mode == "stack" and k in (pygame.K_UP, pygame.K_DOWN):
            self.sel_voice = max(0, min(self.stack.count - 1, self.sel_voice + (1 if k == pygame.K_DOWN else -1)))
        elif self.roll and k in (pygame.K_UP, pygame.K_DOWN):
            self.view.top_pitch += (1 if k == pygame.K_UP else -1) * (12 if mods & pygame.KMOD_SHIFT else 3)
            self.clamp_view()

    def zoom_h_center(self, f):
        g = self.geo["grid"]
        self.view.zoom_h(f, g[0] + g[2] / 2, g[0])
        self.clamp_view()
        self.dirty = True

    def zoom_v_center(self, f):
        g = self.geo["grid"]
        self.view.zoom_v(f, g[1] + g[3] / 2, g[1])
        self.clamp_view()
        self.dirty = True

    def toggle_mute(self, v):
        self.muted ^= {v}
        self.restart_here()

    def toggle_solo(self, v):
        self.solos ^= {v}
        self.restart_here()

    def open_dialog(self):
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            path = filedialog.askopenfilename(title="Open a song", filetypes=[("Songs", "*.mml *.csp *.mid *.midi"), ("All files", "*.*")])
            root.destroy()
            if path:
                self.open_path(path)
        except Exception:
            self.say("No file dialog here: drag a .mml or .csp file onto the window, or pick from Songs.")

    # ------------------------------------------------------------ editing
    @property
    def snap_ticks(self):
        return SNAPS[self.snap_i][1]

    @property
    def snap_name(self):
        return SNAPS[self.snap_i][0]

    def set_snap(self, i):
        tick = self.tr.row * self.snap_ticks
        self.snap_i = max(0, min(len(SNAPS) - 1, i))
        self.tr.row = int(tick // self.snap_ticks)
        if self.mode == "tracker" and self.roll:
            self.tracker_visible()
        self.say("Snap to %s" % self.snap_name, 2.0)

    @property
    def song_dirty(self):
        return bool(self.ed and self.ed.dirty)

    def sync(self, select=None):
        """The Song changed: rebuild what is shown from the editor's tune; `select` is (voice, start, pitch)."""
        self.tune = self.ed.tune
        self.roll = M.Roll.from_tune(self.tune, self.inst_name)
        self.stack.names = self.roll.names
        self.relayout()                      # a click while drawing may have changed the song's track count
        self.note_hover = None
        self.note_sel = None
        hs, self.hit_sel = self.hit_sel, None
        if hs and self.ed.find_hit(*hs):
            self.hit_sel = hs
        self.inst_names = self.build_names(self.tune)
        if select:
            v, start, pitch = select
            i = self.ed.find(v, start, pitch)
            if i is not None:
                self.note_sel = (v, i)
        self.multi = {m for m in self.multi if self.ed.find(*m) is not None}
        self.quit_warned = False
        self.clamp_view()
        if self.playing:
            self.restart_here()

    def edit(self, fn, select=None):
        """Run an Editor call; show why it was refused.  Returns True if it changed the song."""
        if self.ed is None:
            return False
        if select is None:
            n = self.selected_note()
            select = (n.voice, n.start, n.pitch) if n else None
        try:
            self.last_result = fn()
        except EditError as e:
            self.say(str(e))
            return False
        self.sync(select)
        return True

    def undo(self):
        what = self.ed.undo() if self.ed else None
        if what:
            self.sync()
            self.say("Undid: %s" % what, 2.0)
        else:
            self.say("Nothing to undo.", 2.0)

    def redo(self):
        what = self.ed.redo() if self.ed else None
        if what:
            self.sync()
            self.say("Redid: %s" % what, 2.0)
        else:
            self.say("Nothing to redo.", 2.0)

    def set_inst(self, voice, **fields):
        """Change fields of the instrument the voice plays."""
        self.edit(lambda: self.ed.edit_voice_instrument(voice, **fields))

    def pick_instrument(self, voice, name):
        self.edit(lambda: self.ed.set_voice_instrument(voice, name))

    def copy_instrument(self, voice):
        if self.edit(lambda: self.ed.copy_instrument(voice)):
            self.say("%s now has its own instrument, %s." % (panels.VOICE_LABEL[voice], self.ed.voice_instrument(voice)), 4.0)

    # ------------------------------------------------------------ tracker
    # ------------------------------------------------------------ the track stack
    def stack_area_h(self):
        return self.geo["grid"][3] if self.geo.get("grid") else 0

    def sync_stack(self):
        """Keep the stack's height in step with the setting and the UI scale, and the picked track in view."""
        px = int(round(self.prefs.track_h * self.scale))
        area = self.stack_area_h()
        if self.stack.height != px:
            self.stack.set_height(px, area, limit=False)
        self.stack.clamp(area)
        if self.sel_voice != self._stack_sel:
            self._stack_sel = self.sel_voice
            self.stack.scroll_to(min(self.sel_voice, self.stack.count - 1), area)

    def set_track_h(self, h):
        """The track height setting (pixels at UI scale 1): kept in the preferences, applied by the next update."""
        h = clamp_height(h)
        if h != self.prefs.track_h:
            self.prefs.track_h = h
            self.dirty = True
        if self.geo.get("grid"):
            self.sync_stack()

    def stack_lane(self, pos):
        """The track under `pos` in the stack's lanes, or None."""
        g = self.geo.get("grid")
        if not g or not L.contains(g, *pos):
            return None
        return self.stack.track_at(pos[1] - g[1], g[3])

    def set_mode(self, mode):
        if mode == self.mode:
            return
        before = self.mode
        self.mode = mode
        self.prefs.view = mode
        self.dirty = True
        self.drag = None
        self.relayout()
        if mode == "tracker":
            self.tr.col = min(self.sel_voice, self.roll.lanes - 1)
            self.tr.row = int(self.cursor_tick // self.snap_ticks)
            self.tracker_visible()
        elif before == "tracker":
            self.sel_voice = self.tr.col
        self._stack_sel = -1

    def tracker_visible(self):
        g = panels.tracker_geo(self)
        n = TR.total_rows(self.roll, self.snap_ticks) if self.roll else 1
        c = self.tr
        c.row = max(0, min(n - 1, c.row))
        if c.row < c.top:
            c.top = c.row
        elif c.row >= c.top + g["visible"]:
            c.top = c.row - g["visible"] + 1
        c.top = max(0, c.top)
        c.col = max(0, min(self.roll.lanes - 1, c.col)) if self.roll else 0
        if c.col < c.left:
            c.left = c.col
        elif c.col >= c.left + g["cols"]:
            c.left = c.col - g["cols"] + 1
        c.left = max(0, min(c.left, max(0, (self.roll.lanes if self.roll else 1) - g["cols"])))

    def tracker_down(self, pos):
        g = panels.tracker_geo(self)
        x, y = pos
        if not L.contains((g["x"], g["rows_y"], g["w"] - g["sb"], g["h"] - g["head"]), x, y):
            return
        row = self.tr.top + (y - g["rows_y"]) // g["row_h"]
        col = self.tr.left + (x - g["x"] - g["rn"]) // g["cw"]
        self.tr.row = max(0, row)
        if 0 <= col < self.roll.lanes:
            self.tr.col = col
            self.sel_voice = col
        self.note_sel = self.hit_sel = None
        self.tracker_visible()

    def audition(self, voice, pitch):
        """Play one short note on the voice's instrument (what you hear when you type it in the tracker)."""
        if voice >= self.roll.nv or self.ed is None:
            return
        from chipsynth.song import Octave
        from chipsynth.songedit import spell
        inst = None
        for n in self.tune.voices[voice]:
            inst = n.inst
            break
        song = Song()
        if inst is not None:
            song.instruments = {"_audition": inst}
        from chipsynth.song import Select
        before, ev, after = spell(pitch, (4, 0, None))
        ev.dur = 48
        song.tracks["V1"] = ([Select("_audition")] if inst is not None else []) + [Octave(set=4)] + before + [ev]
        try:
            tune = song.compile(self.ed.lib)
        except mml.MMLError:
            return
        self.stop()
        self.out.start(tune, loop=False)

    def tracker_key(self, e):
        """Handle a key in the tracker.  Returns True if it was used."""
        k, mods = e.key, e.mod
        c, rt = self.tr, self.snap_ticks
        n = TR.total_rows(self.roll, rt)
        page = max(1, self.roll.bar_ticks // rt)
        moved = True
        if k == pygame.K_UP and mods & pygame.KMOD_SHIFT:
            c.octave = min(8, c.octave + 1)
            self.say("Octave %d" % c.octave, 1.5)
            return True
        if k == pygame.K_DOWN and mods & pygame.KMOD_SHIFT:
            c.octave = max(0, c.octave - 1)
            self.say("Octave %d" % c.octave, 1.5)
            return True
        if k == pygame.K_UP:
            c.row -= 1
        elif k == pygame.K_DOWN:
            c.row += 1
        elif k == pygame.K_PAGEUP:
            c.row -= page
        elif k == pygame.K_PAGEDOWN:
            c.row += page
        elif k == pygame.K_HOME:
            c.row = 0
        elif k == pygame.K_END:
            c.row = n - 1
        elif k == pygame.K_LEFT:
            c.col = (c.col - 1) % self.roll.lanes
        elif k == pygame.K_RIGHT:
            c.col = (c.col + 1) % self.roll.lanes
        elif k in (pygame.K_MINUS, pygame.K_KP_MINUS, pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
            c.octave = max(0, min(8, c.octave + (-1 if k in (pygame.K_MINUS, pygame.K_KP_MINUS) else 1)))
            self.say("Octave %d" % c.octave, 1.5)
            return True
        elif k in (pygame.K_DELETE, pygame.K_BACKSPACE):
            self.tracker_erase()
            c.row += 1 if k == pygame.K_BACKSPACE else 0
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER) and c.col >= self.roll.nv:
            self.tracker_hit()
            c.row += 1
        elif k in (pygame.K_COMMA, pygame.K_PERIOD) and not mods & pygame.KMOD_CTRL:
            self.tracker_volume((1 if k == pygame.K_PERIOD else -1) * (4 if mods & pygame.KMOD_SHIFT else 1))
            return True
        elif k == pygame.K_SEMICOLON:
            self.tracker_effect(-1 if mods & pygame.KMOD_SHIFT else 1)
            return True
        elif k == pygame.K_SPACE:
            if not self.playing:
                self.cursor_tick = c.row * rt
            return False                                    # the normal play / stop
        elif c.col < self.roll.nv and not mods & (pygame.KMOD_ALT | pygame.KMOD_META):
            ch = TR.key_char(k, getattr(e, "unicode", ""))
            pitch = TR.piano_pitch(ch, c.octave)
            if pitch is None:
                return ch in TR.PIANO
            tick = c.row * rt
            if self.edit(lambda: self.ed.put_note(c.col, tick, pitch, rt), None):
                self.audition(c.col, pitch)
                c.row += 1
            return True
        else:
            moved = False
        if moved:
            c.row = max(0, min(n - 1, c.row))
            self.sel_voice = c.col
            self.tracker_visible()
        return moved

    def tracker_note(self):
        """(voice, index) of the note at the tracker cursor, or None (and says so)."""
        c = self.tr
        i = TR.note_at(self.roll, c.col, c.row * self.snap_ticks) if c.col < self.roll.nv else None
        if i is None:
            self.say("No note here.", 1.5)
            return None
        return c.col, self.roll.notes[c.col][i].start

    def tracker_volume(self, delta):
        at = self.tracker_note()
        if at is None:
            return
        v, start = at
        i = self.ed.find(v, start)
        vol = max(0, min(15, self.tune.voices[v][i].vol + delta))
        if vol == self.tune.voices[v][i].vol:
            return
        if self.edit(lambda: self.ed.set_volume(v, self.ed.find(v, start), vol), None):
            self.say("Volume %d" % vol, 1.5)

    def tracker_effect(self, step):
        at = self.tracker_note()
        if at is None:
            return
        v, start = at
        names = list(EFFECTS)
        cur = effect_of(self.tune.voices[v][self.ed.find(v, start)])
        new = names[(names.index(cur) + step) % len(names)]
        if self.edit(lambda: self.ed.set_effect(v, self.ed.find(v, start), new), None):
            self.say("Effect: %s" % new, 1.5)

    def tracker_erase(self):
        c, rt = self.tr, self.snap_ticks
        tick = c.row * rt
        if c.col < self.roll.nv:
            i = TR.note_at(self.roll, c.col, tick)
            if i is not None:
                self.edit(lambda: self.ed.erase(c.col, i), None)
        else:
            b = TR.hit_at(self.roll, c.col, tick, rt)
            if b is not None:
                self.edit(lambda: self.ed.erase_hit(c.col, b.start), None)

    def tracker_hit(self):
        c, rt = self.tr, self.snap_ticks
        tick = c.row * rt
        if not self.sample_name:
            self.say("Pick a sample first (the inspector's Hit sample), or import a WAV.")
            return
        b = TR.hit_at(self.roll, c.col, tick, rt)
        if b is not None:
            self.edit(lambda: self.ed.set_hit_sample(c.col, b.start, self.sample_name), None)
        else:
            name = self.sample_name
            self.edit(lambda: self.ed.add_hit(c.col, tick, name, rt), None)

    # ------------------------------------------------------------ several notes
    def selection(self):
        sel = set(self.multi)
        n = self.selected_note()
        if n:
            sel.add((n.voice, n.start))
        return sel

    def set_selection(self, sel):
        sel = {tuple(x) for x in sel}
        self.multi = sel if len(sel) > 1 else set()
        self.note_sel = None
        if len(sel) == 1:
            v, start = next(iter(sel))
            i = self.ed.find(v, start)
            self.note_sel = (v, i) if i is not None else None

    def select_all(self, all_voices=False):
        nv = self.roll.nv
        voices = range(nv) if all_voices else [min(self.sel_voice, nv - 1)]
        self.set_selection({(v, n.start) for v in voices for n in self.roll.notes[v]})
        self.say("%d notes selected" % len(self.selection()), 2.0)

    def finish_marquee(self, d):
        g = self.geo["grid"]
        (x0, y0), (x1, y1) = d["at"], d["to"]
        t0, t1 = sorted((self.view.tick_at(x0, g[0]), self.view.tick_at(x1, g[0])))
        v = min(self.sel_voice, self.roll.nv - 1)
        if self.mode == "stack":                              # pitches as the picked lane shows them
            p0, p1 = sorted((self.stack.pitch_at(v, y0 - g[1], self.roll), self.stack.pitch_at(v, y1 - g[1], self.roll)))
        else:
            p0, p1 = sorted((self.view.pitch_at(y0, g[1]), self.view.pitch_at(y1, g[1])))
        got = {(v, n.start) for n in self.roll.notes[v] if n.start < t1 and n.start + n.dur > t0 and p0 <= n.pitch <= p1}
        self.set_selection((self.selection() | got) if d["add"] else got)
        if got:
            self.say("%d note%s selected" % (len(got), "" if len(got) == 1 else "s"), 2.0)

    def move_selection(self, dtick, dpitch):
        sel = sorted(self.selection())
        if self.edit(lambda: self.ed.move_notes(sel, dtick, dpitch), None):
            self.set_selection(self.last_result)

    def copy_selection(self, cut=False):
        sel = self.selection()
        if not sel:
            self.say("Select notes first (click, Shift+click, Ctrl+drag, Ctrl+A).", 3.0)
            return
        self.clip = self.ed.selection_notes(sorted(sel))
        self.say("%s %d note%s." % ("Cut" if cut else "Copied", len(self.clip), "" if len(self.clip) == 1 else "s"), 2.5)
        if cut:
            self.edit(lambda: self.ed.erase_notes(sorted(sel)), None)
            self.multi = set()

    def paste(self, tick=None):
        if not self.clip:
            self.say("Nothing copied yet.", 2.5)
            return
        tick = self.cursor_tick if tick is None else tick
        clip = self.clip
        if self.edit(lambda: self.ed.paste_notes(clip, tick), None):
            self.set_selection(self.last_result)

    def duplicate(self):
        sel = self.selection()
        if not sel:
            return
        clip = self.ed.selection_notes(sorted(sel))
        if clip:
            end = max(s + d for _, s, d, _ in clip)
            if self.edit(lambda: self.ed.paste_notes(clip, end), None):
                self.set_selection(self.last_result)

    def quantize_selection(self):
        sel = sorted(self.selection())
        sn = self.snap_ticks
        if self.edit(lambda: self.ed.quantize(sel, sn), None):
            self.set_selection(self.last_result)
            self.say("Notes snapped to %s." % self.snap_name, 2.5)

    def export_midi_dialog(self):
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            path = filedialog.asksaveasfilename(title="Export MIDI", defaultextension=".mid",
                                                initialfile=Path(self.source).stem if self.source else (self.label or "song"),
                                                filetypes=[("MIDI file", "*.mid")])
            root.destroy()
        except Exception:
            self.say("No file dialog here.")
            return False
        return bool(path) and self.export_midi(path)

    def export_midi(self, path):
        """Write the song as a standard MIDI file (voices only; drums are left out)."""
        if self.song is None:
            return False
        path = Path(path)
        try:
            from .files import save_with_backup
            save_with_backup(self.root or path.parent, path, midi.to_midi(self.ed.tune))
        except OSError as e:
            self.say("Can't write %s: %s" % (path.name, e), 8.0)
            return False
        self.say("Exported %s (the four voices; drums are not included)." % path.name, 5.0)
        return True

    def ask(self, label, text, apply):
        self.prompt = dict(label=label, text=text, apply=apply)
        self.overlay = "prompt"

    def finish_prompt(self, ok):
        p, self.prompt, self.overlay = self.prompt, None, None
        if ok and p:
            p["apply"](p["text"].strip())

    def set_song(self, **fields):
        self.edit(lambda: self.ed.set_song(**fields))

    def set_loop(self, tick):
        if self.edit(lambda: self.ed.set_loop(tick)):
            self.say("Loops from bar %d." % (self.ed.tune.loop_tick // max(1, self.tune.bar_ticks) + 1)
                     if self.ed.tune.loop_tick else "No loop point: plays from the start.", 3.0)

    def trim_end(self):
        before = self.tune.length
        if self.edit(lambda: self.ed.trim_end()):
            self.say("Trimmed the song from %d to %d bars." % (-(-before // self.tune.bar_ticks),
                                                                -(-self.ed.tune.length // self.tune.bar_ticks)), 4.0)

    def add_tempo_here(self):
        tick = self.cursor_tick
        self.edit(lambda: self.ed.set_tempo_at(tick, round(self.tune.bpm_at(tick))))

    def new_from_generator_dialog(self):
        """Ctrl+N / New: open the New-song dialog (kind, mood, seed); OK opens the generated tune as a new, unsaved song."""
        if self.song_dirty and not getattr(self, "new_warned", False):
            self.new_warned = True
            self.say("This song has unsaved edits. Save first, or choose New again to leave them behind.", 5.0)
            return
        self.new_warned = False
        self.newdlg["seed"] = str(random.randint(1, 9999))
        self.newdlg["focus"] = "kind"
        self.ui.dd_open = None
        self.overlay = "new"

    SEED_MAX = 999999

    def new_seed(self):
        """The dialog's seed as a number (a blank box counts as 1)."""
        t = self.newdlg["seed"]
        return int(t) if t.isdigit() else 1

    def new_seed_step(self, d):
        self.newdlg["seed"] = str(max(1, min(self.SEED_MAX, self.new_seed() + d)))

    def new_seed_random(self):
        self.newdlg["seed"] = str(random.randint(1, 9999))

    def new_dialog_text(self):
        d = self.newdlg
        return "%s %d %s" % (musicgen.KINDS[d["kind"]], self.new_seed(), musicgen.MOODS[d["mood"]])

    def finish_new(self, ok):
        if not ok:
            self.overlay = None
            self.ui.dd_open = None
            return
        text = self.new_dialog_text()
        self.overlay = None
        self.ui.dd_open = None
        if not self.new_from_generator(text):
            self.overlay = "new"

    def new_dialog_set(self, key, i):
        """A drop-down of the New dialog picked row i."""
        self.newdlg[key] = i
        self.newdlg["focus"] = key

    NEW_FOCUS = ("kind", "mood", "seed")

    def new_dialog_key(self, e):
        k, d, ui = e.key, self.newdlg, self.ui
        r = ui.dropdown_key(k)
        if r is not None:                                  # a list is open: it takes the keys
            if r[0] == "pick":
                self.new_dialog_set(r[1], r[2])
            return
        mods = e.mod
        if k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.finish_new(True)
        elif k == pygame.K_ESCAPE:
            self.finish_new(False)
        elif k == pygame.K_TAB:
            i = self.NEW_FOCUS.index(d["focus"]) + (-1 if mods & pygame.KMOD_SHIFT else 1)
            d["focus"] = self.NEW_FOCUS[i % 3]
        elif k == pygame.K_F5 or (k == pygame.K_r and mods & pygame.KMOD_CTRL):
            self.new_seed_random()
        elif d["focus"] == "seed":
            if k == pygame.K_UP:
                self.new_seed_step(1)
            elif k == pygame.K_DOWN:
                self.new_seed_step(-1)
            elif k == pygame.K_BACKSPACE:
                d["seed"] = d["seed"][:-1]
            elif getattr(e, "unicode", "") and e.unicode.isdigit() and len(d["seed"]) < len(str(self.SEED_MAX)):
                d["seed"] += e.unicode
        else:
            n = len(musicgen.KINDS) if d["focus"] == "kind" else len(musicgen.MOODS)
            if k == pygame.K_UP:
                d[d["focus"]] = (d[d["focus"]] - 1) % n
            elif k == pygame.K_DOWN and mods & pygame.KMOD_ALT or k == pygame.K_SPACE:
                ui.open_dropdown(d["focus"], d[d["focus"]])
            elif k == pygame.K_DOWN:
                d[d["focus"]] = (d[d["focus"]] + 1) % n

    def new_from_generator(self, text):
        words = text.lower().split()
        if not words or words[0] not in musicgen.KINDS:
            self.say("Kinds are: %s." % ", ".join(musicgen.KINDS), 5.0)
            return False
        seed = None
        mood = ""
        dac = {"dac": False, "dac-bass": False, "voice": False}       # words that turn the DAC parts on
        for w in words[1:]:
            if w.isdigit():
                seed = int(w)
            elif w in musicgen.MOODS and w:
                mood = w
            elif w in dac:
                dac[w] = True
            else:
                self.say("'%s' is neither a seed number, a mood (%s) nor dac / dac-bass / voice." % (
                    w, ", ".join(m for m in musicgen.MOODS if m)), 5.0)
                return False
        if seed is None:
            seed = random.randint(1, 9999)
        try:
            song = Song.from_mml(musicgen.generate(words[0], seed, mood, dac=dac["dac"], dac_bass=dac["dac-bass"], voice=dac["voice"]),
                                 self.lib)
        except (mml.MMLError, ValueError) as e:
            self.say("The generator failed: %s" % e)
            return False
        if self.load_song(song, song.title or words[0], ""):
            self.say("Generated %s, seed %d%s. Save to keep it." % (words[0], seed, ", " + mood if mood else ""), 5.0)
            return True
        return False

    def make_pattern_selected(self):
        sel = sorted(self.selection())
        names = self.song.clips if self.song else {}
        n = 1
        while "part%d" % n in names:
            n += 1

        def apply(name):
            if self.edit(lambda: self.ed.make_pattern(sel, name), None):
                self.say("Made pattern '%s': it plays in place of those notes, and edits to it change every use." % name, 5.0)
        self.ask("Name the new pattern", "part%d" % n, apply)

    def make_unique_selected(self):
        n = self.selected_note()
        if n:
            self.edit(lambda: self.ed.make_unique(n.voice, self.note_sel[1]))

    def set_key_text(self, text):
        """Apply what was typed at the K prompt: 'D minor', 'f# dorian', or 'off'."""
        words = text.lower().split()
        if words == ["off"]:
            self.guide = None
            self.say("Scale guide off.", 2.0)
            return
        m = re.fullmatch(r"([a-g])([#b+\-]*)", words[0]) if len(words) == 2 else None
        if not m or words[1] not in SCALES:
            self.say("Type a key such as D minor (scales: %s) or off." % ", ".join(SCALES), 6.0)
            return
        acc = sum({"#": 1, "+": 1, "b": -1, "-": -1}[x] for x in m.group(2))
        tonic = (mml.LETTER[m.group(1)] + acc) % 12
        n = 0
        if self.ed.key_events():
            if not self.edit(lambda: self.ed.set_key(tonic, words[1], keep_pitch=True), None):
                return
            n = self.last_result
        self.guide = (tonic, words[1])
        self.say("Key %s %s%s" % (M.NOTE_NAMES[tonic], words[1], " (the song's key lines changed, notes kept)" if n else ""), 4.0)

    def transpose_in_key(self, steps):
        """Move the selected notes `steps` degrees along the scale guide (the key set with K)."""
        if self.guide is None:
            self.say("Set a key first (K) so there is a scale to move along.", 4.0)
            return
        sel = sorted(self.selection())
        notes = [(v, self.ed.find(v, start)) for v, start in sel]
        notes = [x for x in notes if x[1] is not None]
        tonic, scale = self.guide
        if self.edit(lambda: self.ed.transpose_in_key(notes, steps, tonic, scale), None):
            self.set_selection(sel)

    def set_guide(self, keys, scales, i, g, scale=None):
        i %= len(keys)
        self.guide = None if i == 0 else (i - 1, scale or (g[1] if g else "major"))

    def selected_note(self):
        if not self.note_sel or not self.roll:
            return None
        v, i = self.note_sel
        return self.roll.notes[v][i] if i < len(self.roll.notes[v]) else None

    def delete_selected(self):
        n = self.selected_note()
        if n:
            self.edit(lambda: self.ed.erase(n.voice, self.note_sel[1]))

    def nudge_selected(self, dtick, dpitch):
        n = self.selected_note()
        if not n:
            return
        v, i = self.note_sel
        start, pitch = max(0, n.start + dtick), n.pitch + dpitch
        if dtick:
            self.edit(lambda: self.ed.move(v, i, start, pitch), (v, start, pitch))
        else:
            self.edit(lambda: self.ed.set_pitch(v, i, pitch), (v, start, pitch))

    def snapped(self, tick):
        sn = self.snap_ticks
        return max(0, int(tick // sn) * sn)

    def grid_point(self, pos):
        g = self.geo["grid"]
        if self.mode == "stack":                         # the pitch comes from the lane being worked in
            lane = self.drag["voice"] if self.drag and "voice" in self.drag else self.stack_lane(pos)
            if lane is None or lane >= self.roll.nv:
                lane = min(self.sel_voice, self.roll.nv - 1)
            return self.view.tick_at(pos[0], g[0]), self.stack.pitch_at(lane, pos[1] - g[1], self.roll)
        return self.view.tick_at(pos[0], g[0]), self.view.pitch_at(pos[1], g[1])

    def begin_new_note(self, pos):
        tick, pitch = self.grid_point(pos)
        if not 0 <= pitch <= M.HIGH:
            return
        start = self.snapped(tick)
        self.drag = dict(kind="new", voice=self.sel_voice, start=start, pitch=pitch, dur=self.snap_ticks)

    def begin_note_drag(self, hit, pos):
        n = self.roll.notes[hit[0]][hit[1]]
        tick, pitch = self.grid_point(pos)
        g = self.geo["grid"]
        right = self.view.x_of(n.start + n.dur, g[0])
        edge = min(8 * self.scale, (n.dur * self.view.ppt) / 3)
        mode = "resize" if pos[0] >= right - edge else "move"
        self.drag = dict(kind=mode, voice=n.voice, index=hit[1], start=n.start, dur=n.dur, pitch=n.pitch,
                         tick0=tick, pitch0=pitch, new=(n.start, n.dur, n.pitch), moved=False)

    def drag_note(self, pos):
        d = self.drag
        tick, pitch = self.grid_point(pos)
        sn = self.snap_ticks
        if d["kind"] in ("hit_new", "hit_move"):
            if d["kind"] == "hit_new":
                d["new"] = max(0, int(tick // sn) * sn)
            else:
                d["new"] = max(0, d["start"] + int(round((tick - d["tick0"]) / sn)) * sn)
            d["moved"] = True
            return
        if d["kind"] == "new":
            d["dur"] = max(sn, (int(round((tick - d["start"]) / sn)) * sn) or sn)
        elif d["kind"] == "resize":
            dur = max(sn, int(round((tick - d["start"]) / sn)) * sn)
            d["new"] = (d["start"], dur, d["pitch"])
        else:
            dt = int(round((tick - d["tick0"]) / sn)) * sn
            d["new"] = (max(0, d["start"] + dt), d["dur"], max(0, min(M.HIGH, d["pitch"] + pitch - d["pitch0"])))
        d["moved"] = True

    def on_up(self, pos):
        d = self.drag
        if d and d["kind"] == "marquee" and self.roll is not None:
            self.finish_marquee(d)
            return
        if d and d["kind"] in ("hit_new", "hit_move") and self.ed is not None:
            lane, new = d["lane"], d.get("new", d["start"])
            if d["kind"] == "hit_new":
                name, dur = self.sample_name, self.snap_ticks
                if self.edit(lambda: self.ed.add_hit(lane, new, name, dur)):
                    self.hit_sel = (lane, new) if self.ed.find_hit(lane, new) else None
            elif d["moved"] and new != d["start"]:
                start = d["start"]
                if self.edit(lambda: self.ed.move_hit(lane, start, new)):
                    self.hit_sel = (lane, new) if self.ed.find_hit(lane, new) else None
            return
        if not d or d["kind"] not in ("move", "resize", "new") or self.ed is None:
            return
        if d["kind"] == "new":
            v, start, dur, pitch = d["voice"], d["start"], d["dur"], d["pitch"]
            self.edit(lambda: self.ed.add_note(v, start, dur, pitch), (v, start, pitch))
            return
        if not d["moved"] or d["new"] == (d["start"], d["dur"], d["pitch"]):
            return
        v, i = d["voice"], d["index"]
        start, dur, pitch = d["new"]
        if d["kind"] == "resize":
            self.edit(lambda: self.ed.resize(v, i, dur), (v, start, pitch))
        elif start == d["start"]:
            self.edit(lambda: self.ed.set_pitch(v, i, pitch), (v, start, pitch))
        else:
            self.edit(lambda: self.ed.move(v, i, start, pitch), (v, start, pitch))

    # ------------------------------------------------------------ drum lanes and samples
    def lane_down(self, pos, lane=None):
        g = self.geo
        if lane is None:
            lane = self.roll.nv + (L.dac_at(g, *pos) or 0)
        self.sel_voice, self.note_sel = lane, None
        tick = self.view.tick_at(pos[0], g["grid"][0])
        for b in self.roll.dac_of(lane):
            if b.start <= tick < b.start + b.dur:
                self.hit_sel = (lane, b.start)
                self.drag = dict(kind="hit_move", lane=lane, start=b.start, tick0=tick, moved=False, new=b.start)
                return
        self.hit_sel = None
        if not self.sample_name:
            self.say("Pick a sample first (select D1 or D2, then use the inspector), or import a WAV.")
            return
        sn = self.snap_ticks
        self.drag = dict(kind="hit_new", lane=lane, start=max(0, int(tick // sn) * sn), moved=True,
                         new=max(0, int(tick // sn) * sn))

    def pick_sample(self, delta):
        names = self.ed.sample_names() if self.ed else []
        if not names:
            self.say("No samples yet: import a WAV (Import WAV in the inspector).")
            return
        i = names.index(self.sample_name) if self.sample_name in names else -1
        self.sample_name = names[(i + delta) % len(names)]
        if self.hit_sel:
            lane, start = self.hit_sel
            self.edit(lambda: self.ed.set_hit_sample(lane, start, self.sample_name))

    def preview_sample(self, name=None):
        name = name or self.sample_name
        if not name or self.ed is None:
            return
        song = Song()
        song.samples = dict(self.song.samples)
        song.tracks["D1"] = [Play(name)]
        try:
            tune = song.compile(self.ed.lib)
        except mml.MMLError as e:
            self.say("Can't play %s: %s" % (name, e))
            return
        self.stop()
        self.out.start(tune, loop=False)

    def import_wav(self, path, name=None, rate=12):
        """Convert a WAV file to a drum sample and make it available as `name` (default: the file name)."""
        if self.ed is None:
            return False
        path = Path(path)
        try:
            sr, x = dpcm.read_wav(path)
            name = name or re.sub(r"[^A-Za-z_]", "_", path.stem) or "sample"
            smp = dpcm.convert(x, sr, rate=rate, name=name, fit=True)
        except dpcm.DpcmError as e:
            self.say("Can't import %s: %s" % (path.name, e), 8.0)
            return False
        base, n = name, 0
        while name in self.ed.sample_names():
            n += 1
            name = "%s_%s" % (base, chr(96 + n))
        if not self.edit(lambda: self.ed.import_sample(name, smp)):
            return False
        self.imported[name] = smp
        self.sample_name = name
        self.sel_voice = self.roll.nv if self.sel_voice < self.roll.nv and self.roll.nd else self.sel_voice
        self.say("Imported %s: %.2f s, %d bytes. Click a drum lane to place it." % (name, smp.seconds, smp.size), 6.0)
        return True

    def import_dialog(self):
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            path = filedialog.askopenfilename(title="Import a sample", filetypes=[("WAV files", "*.wav"), ("All files", "*.*")])
            root.destroy()
            if path:
                self.import_wav(path)
        except Exception:
            self.say("No file dialog here: drag a .wav file onto the window.")

    # ------------------------------------------------------------ saving
    def request_quit(self):
        if self.song_dirty and not self.quit_warned:
            self.quit_warned = True
            self.say("Unsaved changes: Ctrl+S saves, or close the window again to discard them.", 8.0)
            return
        self.running = False

    def package_for(self, path):
        """The Package to write to `path`: the loaded one when this song came from it, else a one-song package."""
        if self.song_id and self.song_id in {r.id for r in self.packages.songs()}:
            name, sid = self.packages.split(self.song_id)
            if Path(self.packages.paths[name]) == Path(path) and self.packages.packages[name].songs[sid] is self.song:
                pkg = self.packages.packages[name]
                for n, smp in self.imported.items():
                    pkg.samples.setdefault(n, smp)
                return pkg
        import re
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(path).stem) or "song"
        pkg = pk.Package(name=stem, author=self.song.author)
        pkg.add_song(stem, self.song, self.ed.lib)
        return pkg

    def save(self, path=None):
        """Save to `path` (or where the song came from).  .csp writes a package, anything else MML.  The file it
        replaces is copied to <root>/backups first."""
        if self.song is None:
            return False
        path = path or self.source
        if not path:
            return self.save_as()
        path = Path(path)
        if path.suffix.lower() in (".mid", ".midi"):
            return self.export_midi(path)
        try:
            if path.suffix.lower() == ".csp":
                data = pk.dumps(self.package_for(path))
            else:
                data = self.song.to_mml().encode("utf-8")
                self.write_sample_files()
            from .files import save_with_backup
            changed = save_with_backup(self.root or path.parent, path, data)
        except (SongError, PackageError, OSError, mml.MMLError) as e:
            self.say("Can't save %s: %s" % (path.name, e), 8.0)
            return False
        self.source = str(path)
        self.ed.mark_saved()
        self.quit_warned = False
        self.say("Saved %s" % path.name if changed else "%s is already up to date." % path.name, 4.0)
        return True

    def write_sample_files(self):
        """A .mml names its samples, so the ones imported this session go beside the game's other samples."""
        todo = {n: s for n, s in self.imported.items() if n in self.song.samples}
        if not todo:
            return
        folder = self.ed.lib.get("_sampledir")
        if folder is None:
            raise OSError("imported samples need a game folder (open the composer with a game folder), or save as .csp")
        for n, smp in todo.items():
            dpcm.save(Path(folder) / (n + ".dpcm"), smp)

    def save_as(self):
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            start = Path(self.source) if self.source else None
            path = filedialog.asksaveasfilename(
                title="Save the song", defaultextension=".csp", initialdir=str(start.parent) if start else None,
                initialfile=(start.stem if start else (self.song.title or "song")),
                filetypes=[("Song package", "*.csp"), ("MML text", "*.mml"), ("MIDI file (export)", "*.mid")])
            root.destroy()
        except Exception:
            self.say("No file dialog here: Ctrl+S saves where the song came from.")
            return False
        return bool(path) and self.save(path)

    # ------------------------------------------------------------ scrolling helpers
    def h_range(self):
        """(scroll position, largest scroll position, visible span) in ticks."""
        span = self.geo["grid"][2] / self.view.ppt
        return self.view.scroll_tick, max(0.0, self.roll.length + M.BEAT * 4 - span), span

    def v_range(self):
        """(distance scrolled down from the top, largest distance, visible rows) in pitches (the stack: in pixels)."""
        if self.mode == "stack":
            area = self.stack_area_h()
            return float(self.stack.scroll), float(self.stack.max_scroll(area)), float(area)
        rows = self.geo["grid"][3] / self.view.row
        lo = min(rows, M.HIGH + 1.0)
        return (M.HIGH + 1.0) - self.view.top_pitch, (M.HIGH + 1.0) - lo, rows

    def thumb(self, axis):
        """(track rect, thumb offset, thumb length) of a scroll bar, in pixels along the bar."""
        pos, top, vis = self.h_range() if axis == "h" else self.v_range()
        tr = self.geo["hbar"] if axis == "h" else self.geo["vbar"]
        n = tr[2] if axis == "h" else tr[3]
        total = top + vis
        ln = max(24, int(n * min(1.0, vis / total))) if total > 0 else n
        off = int((n - ln) * min(1.0, max(0.0, pos / top))) if top > 0 else 0
        return tr, off, ln

    def thumb_grab(self, axis, p):
        tr, off, ln = self.thumb(axis)
        base = tr[0] if axis == "h" else tr[1]
        return p - (base + off) if base + off <= p <= base + off + ln else ln // 2

    def drag_thumb(self, axis, p):
        """Move the view so the thumb's start is at pixel p."""
        tr, _, ln = self.thumb(axis)
        pos, top, vis = self.h_range() if axis == "h" else self.v_range()
        base, n = (tr[0], tr[2]) if axis == "h" else (tr[1], tr[3])
        frac = max(0.0, min(1.0, (p - base) / max(1, n - ln)))
        if axis == "h":
            self.view.scroll_tick = frac * top
        elif self.mode == "stack":
            self.stack.scroll = int(frac * top)
            self.stack.clamp(self.stack_area_h())
        else:
            self.view.top_pitch = (M.HIGH + 1.0) - frac * top
        self.clamp_view()

    def overview_to(self, x):
        r = panels.overview_rect(self)
        if not self.roll or r[2] <= 0:
            return
        total = max(1, self.roll.length)
        tick = (x - r[0]) / r[2] * total
        span = self.geo["grid"][2] / self.view.ppt
        self.view.scroll_tick = tick - span / 2
        self.clamp_view()

    # ------------------------------------------------------------ notes
    def note_at(self, pos):
        """(voice, index) of the note under `pos` in the grid (the selected voice first), else None."""
        if not self.roll or not L.contains(self.geo["grid"], *pos):
            return None
        g = self.geo["grid"]
        tick = self.view.tick_at(pos[0], g[0])
        pitch = self.view.pitch_at(pos[1], g[1])
        order = [self.sel_voice] + [v for v in range(self.roll.nv) if v != self.sel_voice]
        if self.mode == "stack":                         # only the lane under the pointer, at that lane's pitches
            lane = self.stack_lane(pos)
            if lane is None or lane >= self.roll.nv:
                return None
            pitch = self.stack.pitch_at(lane, pos[1] - g[1], self.roll)
            order = [lane]
        for v in order:
            if v >= self.roll.nv:
                continue
            for i in range(len(self.roll.notes[v]) - 1, -1, -1):
                n = self.roll.notes[v][i]
                if n.pitch == pitch and n.start <= tick < n.start + n.dur:
                    return (v, i)
        return None
