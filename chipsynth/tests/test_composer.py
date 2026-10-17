# Copyright (C) 2026 Michael Allen Wilmes
"""The composer window, headless (SDL dummy driver): the model maths, the layout, and the window's behaviour."""
import os
import random
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import numpy as np
import pygame

from chipsynth import dpcm, mml, musicgen, package as pk
from chipsynth.packageset import PackageSet
from chipsynth.player import TunePlayer
from chipsynth.song import Song
from chipsynth.songedit import EditError
from chipsynth.composer import layout as L, model as M
from chipsynth.composer.app import Composer, NullOut
from chipsynth.composer.prefs import Prefs

TUNE = ("title Test\ntempo 120\nV1: @lead c4 d4 e4 f4 | loop g4 a4 b4 >c4 |\nV2: @bass <c1 | loop e1 |\n"
        "V3: @bell c8 r8 r4 r2 | r1\n")


def lib_with_samples():
    lb = mml.builtin_library()
    t = np.arange(3000) / 44100.0
    lb["_mem"] = {"kick": dpcm.convert(np.sin(2 * np.pi * 90 * t) * np.exp(-t * 30), 44100, rate=12, name="kick")}
    return lb


class RollTests(unittest.TestCase):
    def test_notes_and_range(self):
        t = mml.parse(TUNE)
        r = M.Roll.from_tune(t, lambda i: "x")
        self.assertEqual([len(v) for v in r.notes], [8, 2, 1, 0])
        self.assertEqual(r.loop_tick, 384)
        self.assertLessEqual(r.low, min(n.pitch for v in r.notes for n in v))
        self.assertGreaterEqual(r.high - r.low, 24)

    def test_dac_blocks_use_the_sample_length_and_stop_at_the_next_event(self):
        lb = lib_with_samples()
        t = mml.parse("tempo 120\nV1: c\n[sample]\nkick\nD1: kick8 r8 kick8 r2 stop\n", lb)
        r = M.Roll.from_tune(t)
        self.assertEqual([b.name for b in r.dac[0]], ["kick", "kick"])
        self.assertTrue(all(b.dur >= 1 for b in r.dac[0]))
        self.assertLessEqual(r.dac[0][0].start + r.dac[0][0].dur, r.dac[0][1].start)


class SeekTests(unittest.TestCase):
    def test_from_zero_is_the_tune_itself(self):
        t = mml.parse(TUNE)
        self.assertIs(M.tune_from(t, 0), t)

    def test_the_remainder_then_the_loop_section(self):
        t = mml.parse(TUNE)                                   # 2 bars, loop from bar 2 (tick 384)
        s = M.tune_from(t, 192)
        self.assertEqual(s.length, (768 - 192) + (768 - 384))
        self.assertEqual(s.loop_tick, 768 - 192)
        v1 = [(n.start, n.pitch) for n in s.voices[0]]
        # notes from tick 192 on (e f), then g a b c, then the loop section again (g a b c) after the first run
        self.assertEqual([p for _, p in v1], [52, 53, 55, 57, 59, 60, 55, 57, 59, 60])
        self.assertEqual(v1[0][0], 0)
        self.assertEqual(v1[6][0], 576)

    def test_a_note_held_across_the_seek_point_starts_again_with_what_is_left(self):
        t = mml.parse(TUNE)
        s = M.tune_from(t, 192)
        first = s.voices[1][0]                                # V2 c1 ran 0-384; 192 ticks left
        self.assertEqual((first.start, first.dur), (0, 192))

    def test_seeking_sounds_like_playing_from_there(self):
        t = mml.parse(TUNE)
        s = M.tune_from(t, 384)                               # bar 2 = the loop point: same as a looped pass
        a = TunePlayer(s, loop=True).render(60000)
        b = TunePlayer(t, loop=True)
        b.render(t.tick_to_sample(384))
        c = b.render(60000)
        n = 44100                  # the same music: compare spectra, because oscillator phase depends on elapsed time
        sa, sc = np.abs(np.fft.rfft(a[:n, 0])), np.abs(np.fft.rfft(c[:n, 0]))
        self.assertGreater(np.corrcoef(sa, sc)[0, 1], 0.9)
        other = np.abs(np.fft.rfft(TunePlayer(t, loop=False).render(n)[:, 0]))      # bar 1 is different music
        self.assertLess(np.corrcoef(sa, other)[0, 1], np.corrcoef(sa, sc)[0, 1])

    def test_once_only_tunes_do_not_loop(self):
        t = mml.parse("once\nV1: c4 d4 e4 f4\n")
        s = M.tune_from(t, 96)
        self.assertTrue(s.once)
        self.assertEqual(s.length, 288)

    def test_source_tick_maps_back_into_the_song(self):
        t = mml.parse(TUNE)
        self.assertEqual(M.source_tick(t, 0, 100), 100)
        self.assertEqual(M.source_tick(t, 192, 10), 202)
        self.assertEqual(M.source_tick(t, 192, 576), 384)               # the end of the first run is the loop point
        self.assertEqual(M.source_tick(t, 192, 576 + 384 + 5), 389)      # and the loop repeats
        self.assertEqual(M.source_tick(t, 99999, 0), 384)                # past the end seeks to the loop

    def test_mixdown_and_solo(self):
        t = mml.parse(TUNE)
        m = M.mixdown(t, {0, 2})
        self.assertEqual([len(v) for v in m.voices], [0, 2, 0, 0])
        self.assertEqual(M.solo_set({1}), {0, 2, 3, 4, 5})
        self.assertEqual(M.solo_set(set()), set())
        self.assertIs(M.mixdown(t, set()), t)


class ViewTests(unittest.TestCase):
    def test_zoom_keeps_the_tick_under_the_pointer(self):
        v = M.View(ppb=56, scroll_tick=200)
        before = v.tick_at(400, 100)
        v.zoom_h(1.5, 400, 100)
        self.assertAlmostEqual(v.tick_at(400, 100), before, places=6)
        self.assertGreater(v.ppb, 56)

    def test_horizontal_and_vertical_scale_are_independent(self):
        v = M.View(ppb=56, row=14)
        v.zoom_h(2.0)
        self.assertEqual(v.row, 14)
        v.zoom_v(1.5)
        self.assertEqual(v.ppb, 112)

    def test_limits(self):
        v = M.View()
        for _ in range(60):
            v.zoom_h(0.5)
            v.zoom_v(0.5)
        self.assertEqual((v.ppb, v.row), (v.MIN_PPB, v.MIN_ROW))

    def test_pixel_maths_round_trip(self):
        v = M.View(ppb=70, row=16, scroll_tick=96, top_pitch=70.0)
        self.assertAlmostEqual(v.tick_at(v.x_of(500, 30), 30), 500)
        self.assertEqual(v.pitch_at(v.y_of(60, 10) + 3, 10), 60)

    def test_clamp_keeps_the_view_in_the_song_and_the_keyboard(self):
        v = M.View(scroll_tick=-500, top_pitch=500)
        v.clamp(600, 300, 1000)
        self.assertEqual(v.scroll_tick, 0)
        self.assertLessEqual(v.top_pitch, M.HIGH + 1)
        v.scroll_tick, v.top_pitch = 99999, -50
        v.clamp(600, 300, 1000)
        self.assertLess(v.scroll_tick, 1500)
        self.assertGreaterEqual(v.top_pitch, 300 / v.row)


class LayoutTests(unittest.TestCase):
    def test_panels_tile_the_window(self):
        lay = L.compute(1280, 760, L.Panels())
        self.assertFalse(lay.too_small)
        self.assertEqual(lay.left[0] + lay.left[2], lay.split_left[0])
        self.assertEqual(lay.split_left[0] + lay.split_left[2], lay.center[0])
        self.assertEqual(lay.center[0] + lay.center[2], lay.split_right[0])
        self.assertEqual(lay.split_right[0] + lay.split_right[2], lay.right[0])
        self.assertEqual(lay.right[0] + lay.right[2], 1280)
        self.assertEqual(lay.center[1] + lay.center[3], lay.split_bottom[1])
        self.assertEqual(lay.split_bottom[1] + lay.split_bottom[3], lay.bottom[1])
        self.assertEqual(lay.bottom[1] + lay.bottom[3], 760)

    def test_too_small_notice(self):
        self.assertTrue(L.compute(959, 700, L.Panels()).too_small)
        self.assertTrue(L.compute(1200, 599, L.Panels()).too_small)
        self.assertFalse(L.compute(960, 600, L.Panels()).too_small)

    def test_the_centre_keeps_room_at_the_minimum_size_for_any_font_size(self):
        for scale in (0.8, 1.0, 1.4):
            lay = L.compute(960, 600, L.Panels(left=420, right=520, bottom=260), scale)
            self.assertFalse(lay.too_small)
            self.assertGreaterEqual(lay.center[2], int(320 * scale) - 2)
            self.assertGreater(lay.center[3], 150 * scale)
        self.assertTrue(L.compute(960, 600, L.Panels(), 1.9).too_small)       # huge text needs a bigger window
        self.assertFalse(L.compute(1920, 1080, L.Panels(), 1.9).too_small)

    def test_dragging_clamps(self):
        p = L.Panels()
        L.drag_left(p, 5, 1.0)
        self.assertEqual(p.left, p.LEFT[0])
        L.drag_left(p, 9000, 1.0)
        self.assertEqual(p.left, p.LEFT[1])
        L.drag_right(p, 0, 1280, 1.0)
        self.assertEqual(p.right, p.RIGHT[1])
        L.drag_bottom(p, 0, 760, 1.0)
        self.assertEqual(p.bottom, p.BOTTOM[1])

    def test_roll_rects_fit_inside_the_centre(self):
        lay = L.compute(1280, 760, L.Panels())
        g = L.roll_rects(lay.center, 18)
        cx, cy, cw, ch = lay.center
        for name, r in g.items():
            self.assertGreaterEqual(r[0], cx, name)
            self.assertLessEqual(r[0] + r[2], cx + cw, name)
            self.assertGreaterEqual(r[1], cy, name)
            self.assertLessEqual(r[1] + r[3], cy + ch, name)
        self.assertGreater(g["grid"][3], 100)


class PrefsTests(unittest.TestCase):
    def test_round_trip_and_garbage(self):
        d = Path(tempfile.mkdtemp())
        p = Prefs(dark=False, font=19, ppb=90.0, row=20.0, last="a:b")
        p.panels.left = 300
        self.assertTrue(p.save(d / "x" / "c.json"))
        q = Prefs.load(d / "x" / "c.json")
        self.assertEqual((q.dark, q.font, q.ppb, q.row, q.last, q.panels.left), (False, 19, 90.0, 20.0, "a:b", 300))
        (d / "bad.json").write_text("{not json")
        self.assertEqual(Prefs.load(d / "bad.json"), Prefs())
        (d / "odd.json").write_text('{"font": "huge", "ppb": 100000, "dark": 3, "panels": {"left": "x"}}')
        q = Prefs.load(d / "odd.json")
        self.assertEqual(q.font, 15)
        self.assertEqual(q.ppb, 400.0)
        self.assertEqual(q.panels.left, Prefs().panels.left)
        self.assertEqual(Prefs.load(d / "nothing.json"), Prefs())


def key(k, mod=0):
    return pygame.event.Event(pygame.KEYDOWN, key=k, mod=mod, unicode="")


def down(pos, button=1):
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=button)


def up(pos, button=1):
    return pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=button)


def move(pos):
    return pygame.event.Event(pygame.MOUSEMOTION, pos=pos, rel=(0, 0), buttons=(1, 0, 0))


class AppTests(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.size = (1280, 760)
        self.screen = pygame.display.set_mode(self.size, pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.prefs_path = self.dir / "prefs.json"
        self.app = Composer(self.screen, Prefs(), prefs_path=self.prefs_path)
        self.app.frame()
        self.app.load_song(Song.from_mml(TUNE), "Test")
        self.app.frame()

    def click(self, pos):
        self.app.frame([down(pos), up(pos)])

    def drag(self, a, b):
        self.app.frame([down(a)])
        self.app.frame([move(b)])
        self.app.frame([up(b)])

    def center(self, rect):
        return (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)

    def test_it_draws_at_every_size_and_both_themes(self):
        for size in ((960, 600), (1280, 760), (1920, 1080), (900, 500), (1500, 640)):
            self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)
            self.app.screen = self.screen
            for dark in (True, False):
                self.app.prefs.dark = dark
                self.app.frame()
                px = pygame.surfarray.array3d(self.screen)
                self.assertGreater(int(px.std()), 5, "blank window at %s" % (size,))
                self.assertEqual(self.app.layout.too_small, size[0] < 960 or size[1] < 600)

    def test_too_small_window_shows_the_notice_and_ignores_the_mouse(self):
        self.screen = pygame.display.set_mode((800, 500), pygame.RESIZABLE)
        self.app.screen = self.screen
        self.app.frame()
        self.assertTrue(self.app.layout.too_small)
        before = self.app.prefs.panels.left
        self.drag((self.app.layout.split_left[0] + 2, 200), (400, 200))
        self.assertEqual(self.app.prefs.panels.left, before)

    def test_every_font_size_draws(self):
        for px in (11, 15, 20, 28):
            self.app.set_font(px)
            self.app.frame()
            self.app.frame()
        self.assertEqual(self.app.prefs.font, 28)
        self.app.set_font(500)
        self.assertEqual(self.app.prefs.font, 28)

    def test_dragging_the_splitters_resizes_and_is_remembered(self):
        a = self.app
        lay = a.layout
        self.drag(self.center(lay.split_left), (lay.split_left[0] + 90, 300))
        self.assertGreater(a.prefs.panels.left, 250)
        w_before = a.prefs.panels.right
        lay = a.layout
        self.drag(self.center(lay.split_right), (lay.split_right[0] - 60, 300))
        self.assertGreater(a.prefs.panels.right, w_before)
        h_before = a.prefs.panels.bottom
        lay = a.layout
        self.drag(self.center(lay.split_bottom), (400, lay.split_bottom[1] - 50))
        self.assertGreater(a.prefs.panels.bottom, h_before)
        a.save_prefs()
        self.assertEqual(Prefs.load(self.prefs_path).panels.left, a.prefs.panels.left)

    def test_space_plays_and_stops(self):
        a = self.app
        a.frame([key(pygame.K_SPACE)])
        self.assertTrue(a.playing)
        self.assertTrue(a.out.playing)
        a.frame([key(pygame.K_SPACE)])
        self.assertFalse(a.playing)
        self.assertFalse(a.out.playing)

    def test_the_playhead_follows_the_player_and_folds_into_the_loop(self):
        a = self.app
        a.frame([key(pygame.K_SPACE)])
        self.assertAlmostEqual(a.playhead(), 0, delta=1)
        a.out.player.render(44100 * 1)                    # one second at 120 bpm = 2 beats = 192 ticks (minus lag)
        self.assertAlmostEqual(a.playhead(), 192 - 0.16 * 2 * 96, delta=3)
        a.out.player.render(44100 * 6)                    # well past the 4 s song: inside the loop section
        self.assertGreaterEqual(a.playhead(), a.tune.loop_tick)
        self.assertLess(a.playhead(), a.tune.length)

    def test_clicking_the_ruler_sets_and_jumps_the_start(self):
        a = self.app
        r = a.geo["ruler"]
        x = int(a.view.x_of(384, r[0]))
        self.click((x + 3, r[1] + 10))
        self.assertEqual(a.cursor_tick, 384)
        a.frame([key(pygame.K_SPACE)])
        self.assertEqual(a.seek_tick, 384)
        self.assertLess(abs(a.playhead() - 384), 2)
        self.click((int(a.view.x_of(0, r[0])) + 3, r[1] + 10))               # while playing: jump
        self.assertEqual(a.seek_tick, 0)
        self.assertTrue(a.playing)

    def test_mute_and_solo_change_what_plays(self):
        a = self.app
        a.frame([key(pygame.K_SPACE)])
        a.frame([key(pygame.K_m)])
        self.assertEqual(a.muted, {0})
        self.assertEqual([len(v) for v in a.out.tune.voices][0], 0)
        a.frame([key(pygame.K_2), key(pygame.K_s)])
        self.assertEqual(a.solos, {1})
        self.assertEqual([len(v) > 0 for v in a.out.tune.voices], [False, True, False, False])
        a.frame([key(pygame.K_s)])
        self.assertEqual(a.solos, set())
        self.assertTrue(a.playing)

    def test_voice_keys_and_cards_pick_the_voice(self):
        a = self.app
        a.frame([key(pygame.K_3)])
        self.assertEqual(a.sel_voice, 2)
        self.click(self.center(a.voice_cards[1]))
        self.assertEqual(a.sel_voice, 1)

    def test_loop_follow_theme_and_font_keys(self):
        a = self.app
        a.frame([key(pygame.K_l)])
        self.assertFalse(a.loop_on)
        a.frame([key(pygame.K_f)])
        self.assertFalse(a.follow)
        dark = a.prefs.dark
        a.frame([key(pygame.K_t)])
        self.assertNotEqual(a.prefs.dark, dark)
        px = a.prefs.font
        a.frame([key(pygame.K_EQUALS, pygame.KMOD_CTRL)])
        self.assertEqual(a.prefs.font, px + 1)
        a.frame([key(pygame.K_MINUS, pygame.KMOD_CTRL)])
        self.assertEqual(a.prefs.font, px)

    def test_without_looping_it_stops_at_the_end(self):
        a = self.app
        a.frame([key(pygame.K_l)])
        a.frame([key(pygame.K_SPACE)])
        self.assertFalse(a.out.loop)

    def test_wheel_zooms_and_scrolls(self):
        a = self.app
        g = a.geo["grid"]
        pos = (g[0] + 200, g[1] + 100)
        a.frame([pygame.event.Event(pygame.MOUSEMOTION, pos=pos, rel=(0, 0), buttons=(0, 0, 0))])
        ppb, row = a.view.ppb, a.view.row
        mods = {"ctrl": pygame.KMOD_CTRL, "both": pygame.KMOD_CTRL | pygame.KMOD_SHIFT}
        real = pygame.key.get_mods
        try:
            pygame.key.get_mods = lambda: mods["ctrl"]
            a.frame([pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=2, precise_x=0.0, precise_y=2.0)])
            self.assertGreater(a.view.ppb, ppb)
            self.assertEqual(a.view.row, row)
            pygame.key.get_mods = lambda: mods["both"]
            a.frame([pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=2, precise_x=0.0, precise_y=2.0)])
            self.assertGreater(a.view.row, row)
            pygame.key.get_mods = lambda: 0
            top = a.view.top_pitch
            a.frame([pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=-1, precise_x=0.0, precise_y=-1.0)])
            self.assertLess(a.view.top_pitch, top)
        finally:
            pygame.key.get_mods = real

    def test_scroll_bars_drag_the_view(self):
        a = self.app
        a.view.ppb = 200.0                                    # zoom in so there is something to scroll
        a.frame()
        tr, off, ln = a.thumb("h")
        start = (tr[0] + off + ln // 2, tr[1] + tr[3] // 2)
        self.drag(start, (start[0] + 60, start[1]))
        self.assertGreater(a.view.scroll_tick, 0)
        tr, off, ln = a.thumb("v")
        start = (tr[0] + tr[2] // 2, tr[1] + off + ln // 2)
        top = a.view.top_pitch
        self.drag(start, (start[0], start[1] + 40))
        self.assertNotEqual(a.view.top_pitch, top)

    def test_clicking_a_note_selects_it_and_its_voice(self):
        a = self.app
        a.view.scroll_tick = 0
        a.frame()
        n = a.roll.notes[1][0]                                # the bass note
        g = a.geo["grid"]
        a.view.top_pitch = n.pitch + 6
        a.frame()
        pos = (int(a.view.x_of(n.start + n.dur / 2, g[0])), int(a.view.y_of(n.pitch, g[1]) + a.view.row / 2))
        self.click(pos)
        self.assertEqual(a.note_sel, (1, 0))
        self.assertEqual(a.sel_voice, 1)
        self.click((g[0] + 5, g[1] + g[3] - 5))               # empty space
        self.assertIsNone(a.note_sel)

    def test_overview_click_moves_the_view(self):
        a = self.app
        a.view.ppb = 200.0
        a.frame()
        ov = (a.layout.bottom[0] + 8, a.layout.bottom[1] + 6, a.layout.bottom[2] - 16, 40)
        self.click((ov[0] + ov[2] - 10, ov[1] + 10))
        self.assertGreater(a.view.scroll_tick, 100)

    def test_songs_overlay_lists_and_opens_a_song(self):
        a = self.app
        d = self.dir / "music"
        d.mkdir()
        (d / "lute.mml").write_text("title Lute\nV1: c d e\n")
        pkg = pk.Package(name="forest")
        pkg.add_song("dawn", Song.from_mml("title Dawn\nV1: c2 d2\n"))
        pk.save(pkg, d / "forest.csp")
        a.packages.add_folder(d)
        a.overlay = "songs"
        a.frame()
        ids = [r.id for r in a.packages.songs()]
        self.assertEqual(sorted(ids), ["forest:dawn", "lute:lute"])
        self.assertTrue(a.open_song_id("forest:dawn"))
        self.assertEqual(a.label, "Dawn")
        self.assertEqual(a.song_id, "forest:dawn")
        a.frame([key(pygame.K_ESCAPE)])
        self.assertIsNone(a.overlay)

    def test_clicks_behind_a_dialog_do_nothing(self):
        a = self.app
        a.overlay = "help"
        a.frame()
        r = a.layout.top
        x, y = 100, 20
        self.click((x, y))
        self.assertEqual(a.overlay, "help")               # the Songs button under the pointer was not pressed

    def test_open_files_and_drop(self):
        a = self.app
        p = self.dir / "new.mml"
        p.write_text("title Dropped\nV1: c d e f\n")
        a.frame([pygame.event.Event(pygame.DROPFILE, file=str(p))])
        self.assertEqual(a.label, "Dropped")
        bad = self.dir / "bad.mml"
        bad.write_text("V1: zzz")
        a.frame([pygame.event.Event(pygame.DROPFILE, file=str(bad))])
        self.assertEqual(a.label, "Dropped")                  # still the old song
        self.assertIn("bad.mml", a.message)
        pkg = pk.Package(name="p")
        pkg.add_song("one", Song.from_mml("title One\nV1: c\n"))
        pk.save(pkg, self.dir / "p.csp")
        self.assertTrue(a.open_path(self.dir / "p.csp"))
        self.assertEqual(a.song_id, "p:one")
        self.assertFalse(a.open_path(self.dir / "missing.mml"))

    def test_a_song_with_a_missing_sample_is_refused_with_a_reason(self):
        a = self.app
        song = Song.from_mml("V1: c\nD1: kick8\n[sample]\nkick\n", lib_with_samples())
        self.assertFalse(a.load_song(song, "Drums"))
        self.assertIn("kick", a.message)

    def test_empty_window_and_dac_only_song_draw(self):
        screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        blank = Composer(screen, Prefs(), prefs_path=self.prefs_path)
        blank.frame()
        blank.frame([key(pygame.K_SPACE), key(pygame.K_m), key(pygame.K_HOME)])
        lb = lib_with_samples()
        drums = Composer(screen, Prefs(), lib=lb, prefs_path=self.prefs_path)
        drums.frame()
        self.assertTrue(drums.load_song(Song.from_mml("D1: kick8 r8 kick8 r4\n[sample]\nkick\n", lb), "Drums only"))
        drums.frame()
        self.assertEqual(drums.sel_voice, 4)

    def test_quit_saves_the_settings(self):
        a = self.app
        a.prefs.panels.left = 333
        a.frame([pygame.event.Event(pygame.QUIT)])
        self.assertFalse(a.running)
        a.save_prefs()
        self.assertEqual(Prefs.load(self.prefs_path).panels.left, 333)


class EditTests(unittest.TestCase):
    """Editing notes with the mouse and keys, undo/redo, and saving."""
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()
        self.song = Song.from_mml("title T\ntempo 120\nV1: o4 c4 r4 e4 r4\n")
        self.app.load_song(self.song, "T", source=str(self.dir / "t.mml"))
        self.app.frame()
        self.app.view.show(40, 70, *self.app.geo["grid"][2:])
        self.app.frame()

    def at(self, tick, pitch):
        g = self.app.geo["grid"]
        v = self.app.view
        return (int(v.x_of(tick, g[0]) + 3), int(v.y_of(pitch, g[1]) + v.row // 2))

    def notes(self):
        return [(n.start, n.dur, n.pitch) for n in self.app.tune.voices[0]]

    def test_click_empty_adds_a_note_of_the_snap_length(self):
        a = self.app
        p = self.at(96, 55)                                  # inside the rest after the first note
        a.frame([down(p), up(p)])
        self.assertIn((96, 48, 55), self.notes())
        self.assertEqual(a.selected_note().pitch, 55)
        self.assertTrue(a.song_dirty)

    def test_drag_sets_the_length_of_a_new_note(self):
        a = self.app
        p, q = self.at(96, 55), self.at(96 + 96, 55)
        a.frame([down(p)])
        a.frame([move(q)])
        a.frame([up(q)])
        self.assertIn((96, 96, 55), self.notes())

    def test_drag_a_note_moves_it_and_changes_pitch(self):
        a = self.app
        a.snap_i = 0
        p, q = self.at(0, 48), self.at(96, 50)
        a.frame([down(p)])
        a.frame([move(q)])
        a.frame([up(q)])
        self.assertEqual(self.notes()[0], (96, 96, 50))

    def test_drag_the_right_end_resizes(self):
        a = self.app
        g = a.geo["grid"]
        right = int(a.view.x_of(96, g[0])) - 2
        y = self.at(0, 48)[1]
        a.frame([down((right, y))])
        a.frame([move((right + int(96 * a.view.ppt), y))])
        a.frame([up((right + int(96 * a.view.ppt), y))])
        self.assertEqual(self.notes()[0], (0, 192, 48))

    def test_refused_edit_says_why_and_changes_nothing(self):
        a = self.app
        before = self.notes()
        a.snap_i = 0
        a.sel_voice = 0
        n = a.roll.notes[0][0]
        a.note_sel = (0, 0)
        a.drag = dict(kind="resize", voice=0, index=0, start=0, dur=96, pitch=48, tick0=0, pitch0=48, new=(0, 96 * 5, 48), moved=True)
        a.on_up((0, 0))
        self.assertEqual(self.notes(), before)
        self.assertIn("room", a.message)

    def test_keys_delete_nudge_undo_redo(self):
        a = self.app
        a.note_sel = (0, 0)
        a.frame([key(pygame.K_UP)])
        self.assertEqual(self.notes()[0][2], 49)
        a.frame([key(pygame.K_UP, pygame.KMOD_SHIFT)])
        self.assertEqual(self.notes()[0][2], 61)
        a.frame([key(pygame.K_RIGHT)])
        self.assertEqual(self.notes()[0][0], 48)
        a.frame([key(pygame.K_DELETE)])
        self.assertEqual(len(self.notes()), 1)
        for _ in range(4):
            a.frame([key(pygame.K_z, pygame.KMOD_CTRL)])
        self.assertEqual(self.notes(), [(0, 96, 48), (192, 96, 52)])
        self.assertFalse(a.song_dirty)
        a.frame([key(pygame.K_y, pygame.KMOD_CTRL)])
        self.assertEqual(self.notes()[0][2], 49)
        a.frame([key(pygame.K_z, pygame.KMOD_CTRL | pygame.KMOD_SHIFT)])      # redo too

    def test_snap_keys(self):
        a = self.app
        a.frame([key(pygame.K_RIGHTBRACKET)])
        self.assertEqual(a.snap_name, "1/16")
        for _ in range(9):
            a.frame([key(pygame.K_LEFTBRACKET)])
        self.assertEqual(a.snap_name, "1/4")

    def test_save_writes_mml_with_a_backup_and_clears_the_star(self):
        a = self.app
        path = self.dir / "t.mml"
        path.write_text("old\n")
        a.frame([down(self.at(96, 55)), up(self.at(96, 55))])
        self.assertTrue(a.save())
        self.assertFalse(a.song_dirty)
        self.assertEqual(Song.from_mml(path.read_text()).compile().voices[0][1].pitch, 55)
        self.assertTrue(list((self.dir / "backups").glob("t.mml.*")))

    def test_save_as_package_and_reopen(self):
        a = self.app
        a.frame([down(self.at(96, 55)), up(self.at(96, 55))])
        path = self.dir / "pack.csp"
        self.assertTrue(a.save(path))
        back = pk.load(path)
        self.assertEqual([n.pitch for n in back.tune("pack").voices[0]], [48, 55, 52])
        b = Composer(self.screen, Prefs(), prefs_path=self.dir / "p2.json")
        self.assertTrue(b.open_path(path))
        self.assertEqual(len(b.tune.voices[0]), 3)

    def test_saving_a_package_song_keeps_its_other_songs(self):
        pkg = pk.Package(name="two")
        pkg.songs["a"] = Song.from_mml("V1: c4 d4\n")
        pkg.songs["b"] = Song.from_mml("V1: e4 f4\n")
        path = self.dir / "two.csp"
        pk.save(pkg, path)
        ps = PackageSet()
        ps.add(path)
        a = Composer(self.screen, Prefs(), packages=ps, prefs_path=self.dir / "p3.json", root=self.dir)
        a.frame()
        a.open_song_id("two:b")
        a.frame()
        a.view.show(40, 70, *a.geo["grid"][2:])
        a.frame()
        a.note_sel = (0, 0)
        a.frame([key(pygame.K_UP)])
        self.assertTrue(a.save())
        back = pk.load(path)
        self.assertEqual(sorted(back.songs), ["a", "b"])
        self.assertEqual(back.tune("a").voices[0][0].pitch, 48)
        self.assertEqual(back.tune("b").voices[0][0].pitch, 53)           # e4 is 52 -> 53

    def test_instrument_controls_edit_undo_and_draw(self):
        a = self.app
        a.set_inst(0, a=7, wave="pulse", filt="lp")
        self.assertEqual(a.tune.voices[0][0].inst.a, 7)
        self.assertEqual(a.tune.voices[0][0].inst.filt, "lp")
        self.assertTrue(a.song_dirty)
        a.frame()                                              # the inspector draws pulse width, filter rows ...
        a.set_inst(0, wave="noise")
        a.frame()
        a.pick_instrument(0, "bell")
        self.assertEqual(a.ed.voice_instrument(0), "bell")
        a.copy_instrument(0)
        self.assertTrue(a.ed.voice_instrument(0).startswith("bell"))
        a.frame()
        for _ in range(5):
            a.undo()
        self.assertFalse(a.song_dirty)
        self.assertIsNone(a.ed.voice_instrument(0))                # back to a voice with no @instr

    def test_a_refused_instrument_edit_says_why(self):
        a = self.app
        a.set_inst(0, vol=99)
        self.assertIn("vol", a.message)
        self.assertFalse(a.song_dirty)

    def test_editing_an_instrument_keeps_the_selected_note(self):
        a = self.app
        a.note_sel = (0, 0)
        a.set_inst(0, vol=3)
        self.assertEqual(a.note_sel, (0, 0))

    def test_closing_with_unsaved_changes_asks_first(self):
        a = self.app
        a.frame([down(self.at(96, 55)), up(self.at(96, 55))])
        a.frame([pygame.event.Event(pygame.QUIT)])
        self.assertTrue(a.running)
        self.assertIn("Unsaved", a.message)
        a.frame([pygame.event.Event(pygame.QUIT)])
        self.assertFalse(a.running)

    def test_it_still_draws_with_a_ghost_and_the_buttons(self):
        a = self.app
        a.frame([down(self.at(96, 55))])
        a.frame([move(self.at(192, 55))])
        px = pygame.surfarray.array3d(self.screen)
        self.assertGreater(int(px.std()), 5)
        a.frame([up(self.at(192, 55))])


class DrumTests(unittest.TestCase):
    """Drum lanes: importing WAVs, placing, moving and erasing hits, saving the samples."""
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()
        self.app.load_song(Song.from_mml("title D\ntempo 120\nV1: o4 c4 d4 e4 f4\n"), "D", source=str(self.dir / "d.mml"))
        self.app.frame()
        self.wav = self.dir / "thump.wav"
        import wave
        t = np.arange(4000) / 22050.0
        with wave.open(str(self.wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes((np.sin(2 * np.pi * 80 * t) * np.exp(-t * 20) * 30000).astype("<i2").tobytes())

    def lane_pt(self, lane, tick):
        g, v = self.app.geo["grid"], self.app.view
        r = self.app.geo["dac%d" % (lane - 4)]
        return (int(v.x_of(tick, g[0]) + 3), r[1] + r[3] // 2)

    def hits(self, d=0):
        return [e[0] for e in self.app.tune.dac[d] if e[1] == "play"]

    def test_import_a_wav_then_place_move_erase(self):
        a = self.app
        self.assertTrue(a.import_wav(self.wav))
        self.assertEqual(a.sample_name, "thump")
        p = self.lane_pt(4, 96)
        a.frame([down(p), up(p)])
        self.assertEqual(self.hits(), [96])
        self.assertEqual(a.hit_sel, (4, 96))
        q = self.lane_pt(4, 192)
        a.frame([down(self.lane_pt(4, 96))])
        a.frame([move(q)])
        a.frame([up(q)])
        self.assertEqual(self.hits(), [192])
        a.frame([key(pygame.K_LEFT)])
        self.assertEqual(self.hits(), [144])
        a.frame([key(pygame.K_DELETE)])
        self.assertEqual(self.hits(), [])
        a.frame([key(pygame.K_z, pygame.KMOD_CTRL)])
        self.assertEqual(self.hits(), [144])

    def test_clicking_a_lane_with_no_sample_says_what_to_do(self):
        a = self.app
        a.sample_name = ""
        p = self.lane_pt(5, 0)
        a.frame([down(p), up(p)])
        self.assertIn("sample", a.message)
        self.assertEqual(self.hits(1), [])

    def test_a_bad_wav_is_refused(self):
        bad = self.dir / "bad.wav"
        bad.write_bytes(b"not a wav")
        self.assertFalse(self.app.import_wav(bad))
        self.assertIn("bad.wav", self.app.message)

    def test_imported_samples_survive_save_as_package(self):
        a = self.app
        a.import_wav(self.wav)
        p = self.lane_pt(4, 0)
        a.frame([down(p), up(p)])
        path = self.dir / "drums.csp"
        self.assertTrue(a.save(path))
        b = Composer(self.screen, Prefs(), prefs_path=self.dir / "p2.json")
        self.assertTrue(b.open_path(path))
        self.assertEqual([e[0] for e in b.tune.dac[0] if e[1] == "play"], [0])

    def test_mml_save_needs_a_sample_folder_or_writes_the_files(self):
        a = self.app
        a.import_wav(self.wav)
        p = self.lane_pt(4, 0)
        a.frame([down(p), up(p)])
        self.assertFalse(a.save())                              # no game folder: nowhere to put thump.dpcm
        self.assertIn("game folder", a.message)
        folder = self.dir / "samples"
        folder.mkdir()
        a.ed.lib["_sampledir"] = folder
        self.assertTrue(a.save())
        self.assertTrue((folder / "thump.dpcm").is_file())

    def test_drum_inspector_and_ghost_draw(self):
        a = self.app
        a.import_wav(self.wav)
        a.sel_voice = 4
        a.frame([down(self.lane_pt(4, 96))])
        a.frame([move(self.lane_pt(4, 150))])
        px = pygame.surfarray.array3d(self.screen)
        self.assertGreater(int(px.std()), 5)
        a.frame([up(self.lane_pt(4, 150))])
        a.preview_sample()
        a.frame()


class SongSettingsTests(unittest.TestCase):
    """Title / tempo / time / loop / tempo changes / echo / patterns, from the inspector's controls."""
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 900), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()
        text = "title S\n[pat]\nriff = c4 d4 e4 r4\nV1: o4 !riff !riff !riff\nV2: o3 c1 c1 c1\n"
        self.app.load_song(Song.from_mml(text), "S")
        self.app.frame()

    def test_title_prompt_types_and_applies(self):
        a = self.app
        a.ask("Title", "S", lambda v: a.set_song(title=v))
        self.assertEqual(a.overlay, "prompt")
        a.frame([key(pygame.K_BACKSPACE)])
        for ch in "New":
            a.frame([pygame.event.Event(pygame.KEYDOWN, key=ord(ch.lower()), mod=0, unicode=ch)])
        self.assertEqual(a.prompt["text"], "New")
        a.frame([key(pygame.K_RETURN)])
        self.assertIsNone(a.overlay)
        self.assertEqual(a.song.title, "New")
        a.ask("Title", "x", lambda v: a.set_song(title=v))
        a.frame([key(pygame.K_ESCAPE)])
        self.assertEqual(a.song.title, "New")

    def test_settings_edit_and_undo(self):
        a = self.app
        a.set_song(bpm=140, time=(3, 4), reverb=5, echo=(0.24, 0.4, 0.3, False))
        self.assertEqual((a.tune.bpm, a.tune.time, a.tune.reverb), (140, (3, 4), 5))
        a.frame()
        a.undo()
        self.assertEqual(a.tune.bpm, 120)
        a.set_song(bpm=999)
        self.assertIn("tempo", a.message)

    def test_loop_and_tempo_at_the_cursor(self):
        a = self.app
        a.cursor_tick = a.tune.bar_ticks * 2
        a.make_unique_selected() if a.selected_note() else None
        a.set_loop(a.cursor_tick)
        self.assertEqual(a.tune.loop_tick, 768)
        self.assertIn("bar 3", a.message)
        a.set_loop(None)
        self.assertEqual(a.tune.loop_tick, 0)
        a.add_tempo_here()                                       # on a bar line between two !riff calls
        self.assertEqual(a.ed.tempo_marks(), [(768, 120)])
        a.frame()

    def test_scale_guide_and_pattern_unique(self):
        a = self.app
        a.set_guide(M.NOTE_NAMES, ["major"], 1, None)
        self.assertEqual(a.guide, (0, "major"))
        a.frame()
        a.set_guide(M.NOTE_NAMES, ["major"], 0, a.guide)
        self.assertIsNone(a.guide)
        a.note_sel = (0, 0)
        a.frame()                                                # the inspector shows "Inside pattern riff"
        a.make_unique_selected()
        self.assertEqual(a.ed.clip_uses()["riff"], 2)
        a.frame()

    def test_a_song_with_a_key_starts_with_its_guide(self):
        a = self.app
        a.load_song(Song.from_mml("V1: key d dorian o4 1 2 3\n"), "K")
        self.assertEqual(a.guide, (2, "dorian"))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()
        self.app.load_song(Song.from_mml("title T\ntempo 120\nV1: o4 c4 r4 e4 r4\nV2: o3 c2\n"), "T")
        self.app.frame()
        self.app.frame([key(pygame.K_TAB)])
        self.app.frame()

    def typed(self, ch):
        return pygame.event.Event(pygame.KEYDOWN, key=ord(ch), mod=0, unicode=ch)

    def notes(self, v=0):
        return [(n.start, n.dur, n.pitch) for n in self.app.tune.voices[v]]

    def test_tab_switches_views_and_it_draws(self):
        a = self.app
        self.assertEqual(a.mode, "tracker")
        px = pygame.surfarray.array3d(self.screen)
        self.assertGreater(int(px.std()), 5)
        a.frame([key(pygame.K_TAB)])
        self.assertEqual(a.mode, "roll")

    def test_cells_show_starts_holds_and_empty(self):
        from chipsynth.composer import tracker as TR
        a = self.app
        cells = TR.column_cells(a.roll, 0, 24, 0, 6)
        self.assertEqual([k for _, k in cells], ["start", "hold", "hold", "hold", "empty", "empty"])
        self.assertEqual(cells[0][0], "C4")
        self.assertEqual(TR.column_cells(a.roll, 1, 24, 0, 2)[0], ("C3", "start"))
        self.assertEqual(TR.row_label(a.roll, 4, 24), "1.2")

    def test_typing_on_an_existing_note_changes_its_pitch_and_keeps_its_length(self):
        a = self.app
        a.snap_i = 2                                           # 1/16 rows
        a.tr.row, a.tr.col = 8, 0                              # tick 192: where e4 starts
        a.frame([self.typed("v")])                             # v = F in octave 4
        self.assertIn((192, 96, 53), self.notes())
        self.assertEqual(a.tr.row, 9)

    def test_entry_erase_octave_and_undo(self):
        a = self.app
        a.snap_i = 2
        a.tr.row, a.tr.col = 4, 0                              # tick 96: inside the rest
        a.frame([self.typed("z")])
        self.assertIn((96, 24, 48), self.notes())
        self.assertEqual(a.tr.row, 5)
        a.frame([key(pygame.K_EQUALS)])                        # octave 5
        a.frame([self.typed("x")])
        self.assertIn((120, 24, 62), self.notes())
        a.tr.row = 4
        a.frame([key(pygame.K_DELETE)])
        self.assertNotIn((96, 24, 48), self.notes())
        a.frame([key(pygame.K_z, pygame.KMOD_CTRL)])
        self.assertIn((96, 24, 48), self.notes())

    def test_arrows_columns_and_mouse(self):
        a = self.app
        a.frame([key(pygame.K_DOWN), key(pygame.K_DOWN), key(pygame.K_RIGHT)])
        self.assertEqual((a.tr.row, a.tr.col), (2, 1))
        g = __import__("chipsynth.composer.panels", fromlist=["x"]).tracker_geo(a)
        p = (g["x"] + g["rn"] + 3 * g["cw"] + 5, g["rows_y"] + 5 * g["row_h"] + 3)
        a.frame([down(p), up(p)])
        self.assertEqual((a.tr.row, a.tr.col), (5, 3))
        a.frame([key(pygame.K_PAGEDOWN)])
        self.assertGreater(a.tr.row, 5)

    def test_drum_column_enter_places_and_replaces_a_sample(self):
        import wave
        a = self.app
        t = np.arange(3000) / 22050.0
        wav = self.dir / "k.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes((np.sin(2 * np.pi * 80 * t) * 20000).astype("<i2").tobytes())
        self.assertTrue(a.import_wav(wav))
        a.tr.col, a.tr.row = 4, 4
        a.snap_i = 2
        a.frame([key(pygame.K_RETURN)])
        self.assertEqual([e[0] for e in a.tune.dac[0] if e[1] == "play"], [96])
        a.tr.row = 4
        a.frame([key(pygame.K_DELETE)])
        self.assertEqual([e for e in a.tune.dac[0] if e[1] == "play"], [])

    def test_space_plays_from_the_cursor_row_and_the_playing_row_draws(self):
        a = self.app
        a.snap_i = 2
        a.tr.row = 8
        a.frame([key(pygame.K_SPACE)])
        self.assertTrue(a.playing)
        self.assertEqual(a.seek_tick, 192)
        a.frame()
        a.frame([key(pygame.K_SPACE)])

    def test_changing_the_snap_keeps_the_cursor_tick(self):
        a = self.app
        a.snap_i = 1                                           # 1/8 = 48 ticks
        a.tr.row = 4                                           # tick 192
        a.frame([self.typed("]")])
        self.assertEqual(a.tr.row * a.snap_ticks, 192)


class MultiSelectTests(unittest.TestCase):
    """Selecting several notes, copy / paste / duplicate / quantize, MIDI in and out."""
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()
        self.app.load_song(Song.from_mml("tempo 120\nV1: o4 c4 d4 e4 f4 g4 a4 b4\n"), "M")
        self.app.frame()
        self.app.view.show(40, 70, *self.app.geo["grid"][2:])
        self.app.frame()

    def at(self, tick, pitch):
        g, v = self.app.geo["grid"], self.app.view
        return (int(v.x_of(tick, g[0]) + 3), int(v.y_of(pitch, g[1]) + v.row // 2))

    def notes(self):
        return [(n.start, n.dur, n.pitch) for n in self.app.tune.voices[0]]

    def ctrl(self, k, shift=False):
        return key(k, pygame.KMOD_CTRL | (pygame.KMOD_SHIFT if shift else 0))

    def test_select_all_then_move_and_transpose_together(self):
        a = self.app
        a.frame([self.ctrl(pygame.K_a)])
        self.assertEqual(len(a.selection()), 7)
        a.frame([key(pygame.K_UP, pygame.KMOD_SHIFT)])
        self.assertEqual([p for _, _, p in self.notes()], [60, 62, 64, 65, 67, 69, 71])
        self.assertEqual(len(a.selection()), 7)                       # still selected
        a.frame([key(pygame.K_RIGHT)])
        self.assertEqual(self.notes()[0][0], 48)
        a.frame([self.ctrl(pygame.K_z)])
        a.frame([self.ctrl(pygame.K_z)])
        self.assertEqual([p for _, _, p in self.notes()], [48, 50, 52, 53, 55, 57, 59])

    def test_shift_click_and_marquee(self):
        a = self.app
        a.frame([down(self.at(0, 48)), up(self.at(0, 48))])
        a.frame([down(self.at(96, 50)), up(self.at(96, 50))])
        self.assertEqual(len(a.selection()), 1)
        pygame.key.set_mods(pygame.KMOD_SHIFT)
        a.frame([down(self.at(96, 50)), up(self.at(96, 50))])
        a.frame([down(self.at(192, 52)), up(self.at(192, 52))])
        pygame.key.set_mods(0)
        self.assertEqual(a.selection(), {(0, 96), (0, 192)})
        pygame.key.set_mods(pygame.KMOD_CTRL)
        a.frame([down(self.at(0, 47)), move(self.at(300, 54)), up(self.at(300, 54))])
        pygame.key.set_mods(0)
        self.assertEqual(a.selection(), {(0, 0), (0, 96), (0, 192), (0, 288)})
        a.frame([key(pygame.K_DELETE)])
        self.assertEqual([p for _, _, p in self.notes()], [55, 57, 59])

    def test_copy_paste_duplicate_quantize(self):
        a = self.app
        a.set_selection({(0, 0), (0, 96)})
        a.frame([self.ctrl(pygame.K_c)])
        self.assertEqual(len(a.clip), 2)
        a.cursor_tick = 384 + 384
        a.frame([self.ctrl(pygame.K_v)])
        self.assertIn((768, 96, 48), self.notes())
        self.assertEqual(a.selection(), {(0, 768), (0, 864)})
        a.frame([self.ctrl(pygame.K_d)])
        self.assertIn((960, 96, 48), self.notes())
        a.set_selection({(0, 768)})
        a.edit(lambda: a.ed.move_notes([(0, 768)], 20, 0), None)
        a.set_selection(a.last_result)
        a.snap_i = 0
        a.frame([key(pygame.K_q)])
        self.assertIn((768, 96, 48), self.notes())
        a.set_selection({(0, 0), (0, 96)})
        a.frame([self.ctrl(pygame.K_x)])
        self.assertNotIn((0, 96, 48), self.notes())

    def test_nothing_selected_says_so(self):
        a = self.app
        a.frame([self.ctrl(pygame.K_c)])
        self.assertIn("Select", a.message)
        a.frame([self.ctrl(pygame.K_v)])
        self.assertIn("Nothing copied", a.message)

    def test_midi_export_and_open(self):
        a = self.app
        path = self.dir / "m.mid"
        self.assertTrue(a.save(path))                                  # a .mid is an export, not a save
        self.assertTrue(path.is_file())
        self.assertFalse(a.song_dirty)
        b = Composer(self.screen, Prefs(), prefs_path=self.dir / "p2.json")
        self.assertTrue(b.open_path(path))
        self.assertEqual([(n.start, n.dur, n.pitch) for n in b.tune.voices[0]], self.notes())
        self.assertEqual(b.source, "")
        bad = self.dir / "bad.mid"
        bad.write_bytes(b"nope")
        self.assertFalse(b.open_path(bad))
        self.assertIn("bad.mid", b.message)

    def test_help_overlay_fits_at_every_size(self):
        a = self.app
        for size in ((960, 600), (1280, 760), (1920, 1080)):
            self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)
            a.screen = self.screen
            a.overlay = "help"
            a.frame()
            a.frame()
            self.assertGreater(int(pygame.surfarray.array3d(self.screen).std()), 5)
        a.overlay = None


if __name__ == "__main__":
    unittest.main()


class PatternAndRangeTests(unittest.TestCase):
    """P makes a pattern from the selection; Shift+Space loops just that stretch."""
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()
        self.app.load_song(Song.from_mml("tempo 120\nV1: o4 c4 d4 e4 f4 g4 a4 b4\n"), "M")
        self.app.frame()

    def test_tune_range_is_just_that_stretch(self):
        t = M.tune_range(self.app.tune, 96, 288)
        self.assertEqual(t.length, 192)
        self.assertEqual([(n.start, n.dur, n.pitch) for n in t.voices[0]], [(0, 96, 50), (96, 96, 52)])
        cut = M.tune_range(self.app.tune, 48, 144)                  # a note held across each end is trimmed
        self.assertEqual([(n.start, n.dur) for n in cut.voices[0]], [(0, 48), (48, 48)])

    def test_p_asks_for_a_name_then_makes_the_pattern(self):
        a = self.app
        a.set_selection([(0, 96), (0, 192)])
        a.frame([key(pygame.K_p)])
        self.assertEqual(a.overlay, "prompt")
        self.assertEqual(a.prompt["text"], "part1")
        before = [(n.start, n.dur, n.pitch) for n in a.tune.voices[0]]
        a.frame([key(pygame.K_RETURN)])
        self.assertIn("part1", a.song.clips)
        self.assertEqual([(n.start, n.dur, n.pitch) for n in a.tune.voices[0]], before)
        self.assertEqual(a.selection(), {(0, 96), (0, 192)})

    def test_shift_space_plays_only_the_selected_stretch(self):
        a = self.app
        a.frame([key(pygame.K_SPACE, pygame.KMOD_SHIFT)])
        self.assertFalse(a.playing)                               # nothing selected
        a.set_selection([(0, 96), (0, 192)])
        a.frame([key(pygame.K_SPACE, pygame.KMOD_SHIFT)])
        self.assertEqual(a.play_range, (96, 288))
        self.assertEqual(a.play_tune.length, 192)
        a.stop()
        a.frame([key(pygame.K_SPACE)])                             # plain Space plays everything again
        self.assertIsNone(a.play_range)
        self.assertEqual(a.play_tune.length, a.tune.length)
        a.stop()


class NewFromGeneratorTests(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()

    def test_ctrl_n_opens_the_dialog_then_enter_opens_the_generated_tune(self):
        a = self.app
        a.frame([key(pygame.K_n, pygame.KMOD_CTRL)])
        self.assertEqual(a.overlay, "new")
        a.newdlg.update(kind=musicgen.KINDS.index("boss"), mood=musicgen.MOODS.index("dark"), seed="12")
        a.frame([key(pygame.K_RETURN)])
        self.assertIsNone(a.overlay)
        self.assertIn("Boss 12", a.label)
        self.assertGreater(sum(len(v) for v in a.tune.voices), 20)
        self.assertEqual(a.source, "")

    def test_bad_words_are_explained_and_change_nothing(self):
        a = self.app
        self.assertFalse(a.new_from_generator("lullaby"))
        self.assertFalse(a.new_from_generator("town 3 spooky"))
        self.assertIsNone(a.song)

    def test_dac_words_turn_on_the_drum_kit_and_bass(self):
        a = self.app
        self.assertTrue(a.new_from_generator("fight 3"))
        plain = [len(v) for v in a.tune.voices]
        self.assertTrue(a.new_from_generator("fight 3 dac dac-bass voice"))
        self.assertNotEqual([len(v) for v in a.tune.voices], plain)           # drums and bass moved onto other lanes
        self.assertTrue(a.new_from_generator("town 3"))                      # still works with none
        self.assertFalse(a.new_from_generator("town 3 dac spooky"))

    def test_unsaved_edits_need_a_second_press(self):
        a = self.app
        a.new_from_generator("town 1")
        a.ed.add_note(0, 96 * 400, 96, 60)
        a.sync()
        a.new_from_generator_dialog()
        self.assertIsNone(a.overlay)                                 # warned instead
        a.new_from_generator_dialog()
        self.assertEqual(a.overlay, "new")

    # ---- the dialog itself
    def open_dialog(self):
        a = self.app
        a.frame([key(pygame.K_n, pygame.KMOD_CTRL)])
        a.frame()
        self.assertEqual(a.overlay, "new")
        return a.geo["new"]

    def click(self, r):
        self.app.frame([down(r.center), up(r.center)])
        self.app.frame()

    def pick_row(self, box, i):
        """Open the drop-down at `box` and click its i-th row."""
        a = self.app
        key = next(k for k, (r, _, _) in a.ui._dd.items() if r == box)
        self.click(box)
        self.assertEqual(a.ui.dd_open, key)
        self.click(a.ui.row_rect(key, i, a.fonts, a.screen.get_height()))

    def test_dialog_lists_every_kind_and_mood(self):
        a = self.app
        g = self.open_dialog()
        self.click(g["kind"])
        self.assertEqual(a.ui._dd["kind"][1], list(musicgen.KINDS))
        self.click(g["kind"])                                    # a second click on the box closes it
        self.assertIsNone(a.ui.dd_open)
        self.click(g["mood"])
        self.assertEqual(a.ui._dd["mood"][1][0], "any")          # the blank mood is shown as "any"
        self.assertEqual(len(a.ui._dd["mood"][1]), len(musicgen.MOODS))

    def test_changing_kind_and_mood_with_the_mouse(self):
        a = self.app
        g = self.open_dialog()
        self.pick_row(g["kind"], musicgen.KINDS.index("fight"))
        self.assertEqual(a.newdlg["kind"], musicgen.KINDS.index("fight"))
        self.assertIsNone(a.ui.dd_open)
        self.pick_row(g["mood"], musicgen.MOODS.index("tense"))
        self.assertEqual(a.newdlg["mood"], musicgen.MOODS.index("tense"))
        self.assertTrue(a.new_dialog_text().startswith("fight "))
        self.assertTrue(a.new_dialog_text().endswith(" tense"))

    def test_clicking_away_closes_the_list_without_picking_or_pressing_what_is_beneath(self):
        a = self.app
        g = self.open_dialog()
        before = {k: a.newdlg[k] for k in ("kind", "mood", "seed")}
        self.click(g["mood"])
        self.assertEqual(a.ui.dd_open, "mood")
        away = pygame.Rect(g["mood"].x - 60, g["mood"].y, 20, 10)   # the label column: outside every list and button
        self.click(away)                                          # the click is spent closing the list
        self.assertIsNone(a.ui.dd_open)
        self.assertEqual(a.overlay, "new")
        self.assertEqual({k: a.newdlg[k] for k in ("kind", "mood", "seed")}, before)

    def test_drop_down_keys(self):
        a = self.app
        self.open_dialog()
        a.frame([key(pygame.K_DOWN)])                            # focus is on Kind: Down picks the next kind
        self.assertEqual(a.newdlg["kind"], musicgen.KINDS.index("town") + 1 - len(musicgen.KINDS))
        a.frame([key(pygame.K_TAB), key(pygame.K_SPACE)])        # to Mood, open its list
        self.assertEqual(a.ui.dd_open, "mood")
        a.frame([key(pygame.K_DOWN), key(pygame.K_DOWN)])
        a.frame([key(pygame.K_RETURN)])                          # picks the row, does not press OK
        self.assertEqual(a.newdlg["mood"], 2)
        self.assertEqual(a.overlay, "new")
        a.frame([key(pygame.K_TAB), key(pygame.K_TAB), key(pygame.K_SPACE)])
        a.frame([key(pygame.K_ESCAPE)])                          # closes the list only
        self.assertEqual(a.overlay, "new")

    def test_seed_box_takes_digits_only(self):
        a = self.app
        self.open_dialog()
        a.newdlg["seed"] = ""
        a.frame([key(pygame.K_TAB), key(pygame.K_TAB)])
        for ch in "4x2 7":
            a.frame([pygame.event.Event(pygame.KEYDOWN, key=ord(ch), mod=0, unicode=ch)])
        self.assertEqual(a.newdlg["seed"], "427")
        a.frame([key(pygame.K_BACKSPACE)])
        self.assertEqual(a.newdlg["seed"], "42")

    def test_spinner_and_random(self):
        a = self.app
        g = self.open_dialog()
        a.newdlg["seed"] = "10"
        self.click(g["plus"])
        self.assertEqual(a.newdlg["seed"], "11")
        self.click(g["minus"])
        self.click(g["minus"])
        self.assertEqual(a.newdlg["seed"], "9")
        a.newdlg["seed"] = "1"
        self.click(g["minus"])
        self.assertEqual(a.newdlg["seed"], "1")                  # never below 1
        random.seed(5)
        self.click(g["random"])
        first = a.newdlg["seed"]
        self.assertTrue(first.isdigit() and 1 <= int(first) <= 9999)
        seen = {first}
        for _ in range(6):
            self.click(g["random"])
            seen.add(a.newdlg["seed"])
        self.assertGreater(len(seen), 1)

    def test_ok_creates_the_song_from_the_dialog_choices(self):
        a = self.app
        g = self.open_dialog()
        self.pick_row(g["kind"], musicgen.KINDS.index("victory"))
        a.newdlg["seed"] = "77"
        self.click(g["ok"])
        self.assertIsNone(a.overlay)
        self.assertIsNotNone(a.song)
        self.assertIn("77", a.label)
        again = Song.from_mml(musicgen.generate("victory", 77, ""), a.lib)
        self.assertEqual(a.song.title, again.title)

    def test_cancel_and_escape_change_nothing(self):
        a = self.app
        g = self.open_dialog()
        self.click(g["cancel"])
        self.assertIsNone(a.overlay)
        self.assertIsNone(a.song)
        self.open_dialog()
        a.frame([key(pygame.K_ESCAPE)])
        self.assertIsNone(a.overlay)
        self.assertIsNone(a.song)

    def test_toolbar_new_button_opens_the_dialog(self):
        a = self.app
        a.frame()
        x, y = self.new_button_center()
        self.click(pygame.Rect(x, y, 0, 0))
        self.assertEqual(a.overlay, "new")

    def new_button_center(self):
        # the New button is the only toolbar button whose tip mentions Ctrl+N: find it by hovering across the bar
        a = self.app
        for x in range(10, 700, 4):
            a.frame([move((x, 20))])
            if "Ctrl+N" in a.ui.tip:
                return (x, 20)
        self.fail("no New button in the toolbar")

    def test_f1_hint_is_always_in_the_status_strip(self):
        for size in ((960, 600), (1280, 760)):
            screen = pygame.display.set_mode(size, pygame.RESIZABLE)
            a = Composer(screen, Prefs(), prefs_path=self.dir / ("p%d.json" % size[0]), root=self.dir)
            a.frame()
            b = a.layout.bottom
            strip = screen.subsurface((b[0] + b[2] - 80, b[1] + b[3] - 24, 70, 20))
            base = a.th["panel"]
            self.assertTrue(any(tuple(strip.get_at((x, y)))[:3] != tuple(base) for x in range(70) for y in range(20)))
            a.say("A long message " * 20)
            a.frame()                                            # a message cannot push the hint off
            strip = screen.subsurface((b[0] + b[2] - 80, b[1] + b[3] - 24, 70, 20))
            self.assertTrue(any(tuple(strip.get_at((x, y)))[:3] != tuple(base) for x in range(70) for y in range(20)))

    def test_help_mentions_the_dialog(self):
        from chipsynth.composer import panels
        self.assertTrue(any(k == "Ctrl+N" and "dialog" in d for k, d in panels.HELP))


class NoteVolumeEffectKeyTests(unittest.TestCase):
    """Per-note volume and effects (tracker, shown in the roll) and key editing from the roll."""
    SONG = "title T\ntempo 120\nV1: o4 v10 c4 d4 e4 f4\nV2: o3 c2\n"

    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()
        self.app.load_song(Song.from_mml(self.SONG), "T")
        self.app.snap_i = 0                                         # quarter-note rows: row n is note n
        self.app.frame()

    def vols(self, v=0):
        return [n.vol for n in self.app.ed.tune.voices[v]]

    def tracker(self):
        self.app.frame([key(pygame.K_TAB)])
        self.assertEqual(self.app.mode, "tracker")

    # ---- 1. volume
    def test_editor_volume_changes_only_that_note_and_undoes(self):
        ed = self.app.ed
        ed.set_volume(0, 1, 4)
        self.assertEqual(self.vols(), [10, 4, 10, 10])
        self.assertEqual(Song.from_mml(ed.song.to_mml()).compile().voices[0][1].vol, 4)      # it is in the MML
        ed.set_volume(0, 0, 15)
        self.assertEqual(self.vols(), [15, 4, 10, 10])
        ed.set_volume(0, 1, 15)
        self.assertEqual(self.vols(), [15, 15, 10, 10])
        with self.assertRaises(EditError):
            ed.set_volume(0, 0, 16)
        for _ in range(3):
            ed.undo()
        self.assertEqual(self.vols(), [10, 10, 10, 10])
        self.assertEqual(self.vols(1), [15])

    def test_tracker_shows_the_volume_and_keys_change_it(self):
        from chipsynth.composer import tracker as TR
        a = self.app
        self.tracker()
        a.tr.row, a.tr.col = 1, 0
        a.frame([key(pygame.K_COMMA)])
        self.assertEqual(self.vols(), [10, 9, 10, 10])
        a.frame([key(pygame.K_PERIOD, pygame.KMOD_SHIFT)])
        self.assertEqual(self.vols(), [10, 13, 10, 10])
        a.frame([key(pygame.K_PERIOD, pygame.KMOD_SHIFT)] * 2)
        self.assertEqual(self.vols()[1], 15)                      # clamped
        notes = TR.row_notes(a.roll, 0, a.snap_ticks, 0, 3)
        self.assertEqual([n.vol for n in notes], [10, 15, 10])
        a.frame()                                                  # draws with the volume column
        a.frame([key(pygame.K_z, pygame.KMOD_CTRL)])
        self.assertEqual(self.vols()[1], 13)                       # undid the step to 15 (clamped presses add none)
        a.tr.row = 10
        a.frame([key(pygame.K_COMMA)])                             # no note here: refused, nothing changes
        self.assertIn("No note", a.message)

    # ---- 2. effects
    def test_editor_effects(self):
        from chipsynth.songedit import effect_of
        ed = self.app.ed
        fx = lambda: [effect_of(n) for n in ed.tune.voices[0]]
        ed.set_effect(0, 1, "vibrato")
        self.assertEqual(fx(), ["none", "vibrato", "none", "none"])
        ed.set_effect(0, 2, "slide")
        ed.set_effect(0, 3, "arpeggio minor")
        self.assertEqual(fx(), ["none", "vibrato", "slide", "arpeggio minor"])
        self.assertEqual(Song.from_mml(ed.song.to_mml()).compile().voices[0][3].inst.arp, (0, 3, 7))
        ed.set_effect(0, 1, "none")
        self.assertEqual(fx(), ["none", "none", "slide", "arpeggio minor"])
        with self.assertRaises(EditError):
            ed.set_effect(0, 0, "wobble")
        for _ in range(4):
            ed.undo()
        self.assertEqual(fx(), ["none"] * 4)

    def test_an_effect_in_the_instrument_can_be_turned_off_for_one_note(self):
        from chipsynth.songedit import effect_of
        a = self.app
        a.load_song(Song.from_mml("tempo 120\nV1: vib 5,3 c4 d4 e4\n"), "V")
        fx = lambda: [effect_of(n) for n in a.ed.tune.voices[0]]
        self.assertEqual(fx(), ["vibrato"] * 3)
        a.ed.set_effect(0, 1, "none")
        self.assertEqual(fx(), ["vibrato", "none", "vibrato"])

    def test_tracker_effect_key_cycles_and_the_roll_shows_it(self):
        a = self.app
        self.tracker()
        a.tr.row, a.tr.col = 2, 0
        a.frame([key(pygame.K_SEMICOLON)])
        self.assertEqual(a.roll.notes[0][2].fx, "vibrato")
        a.frame([key(pygame.K_SEMICOLON)])
        self.assertEqual(a.roll.notes[0][2].fx, "slide")
        a.frame([key(pygame.K_SEMICOLON, pygame.KMOD_SHIFT)])
        self.assertEqual(a.roll.notes[0][2].fx, "vibrato")
        a.frame()
        a.frame([key(pygame.K_TAB)])                               # the roll draws the mark
        a.note_hover = (0, 2)
        a.frame()
        self.assertEqual([n.fx for n in a.roll.notes[0]], ["none", "none", "vibrato", "none"])

    # ---- 3. key
    def test_set_key_respells_a_song_with_key_lines_and_keeps_its_pitches(self):
        a = self.app
        a.load_song(Song.from_mml("tempo 120\nV1: key c major o4 1:4 2:4 3:4 5:4\nV2: key c major o3 1:2\n"), "K")
        before = [[n.pitch for n in v] for v in a.tune.voices]
        a.set_key_text("D minor")
        self.assertEqual([[n.pitch for n in v] for v in a.tune.voices], before)
        self.assertEqual(a.guide, (2, "minor"))
        self.assertIn("key d minor", a.ed.song.to_mml())
        a.undo()
        self.assertIn("key c major", a.ed.song.to_mml())

    def test_set_key_without_keep_pitch_modulates(self):
        ed = self.app.ed
        ed.song = Song.from_mml("tempo 120\nV1: key c major o4 1:4 3:4\n")
        ed.refresh()
        self.assertEqual(ed.set_key(7, "major", keep_pitch=False), 1)
        self.assertEqual([n.pitch for n in ed.tune.voices[0]], [55, 59])

    def test_key_on_a_letter_note_song_only_sets_the_guide(self):
        a = self.app
        a.set_key_text("f# dorian")
        self.assertEqual(a.guide, (6, "dorian"))
        self.assertNotIn("key", a.ed.song.to_mml())
        a.set_key_text("off")
        self.assertIsNone(a.guide)
        a.set_key_text("h minor")
        self.assertIn("Type a key", a.message)

    def test_transpose_in_key_moves_by_scale_steps(self):
        a = self.app                                                # C D E F in C major
        a.set_key_text("C major")
        a.set_selection({(0, 0), (0, 96), (0, 192)})
        a.frame([key(pygame.K_UP, pygame.KMOD_ALT)])
        self.assertEqual([n.pitch for n in a.tune.voices[0]], [50, 52, 53, 53])
        a.frame([key(pygame.K_DOWN, pygame.KMOD_ALT)] * 2)
        self.assertEqual([n.pitch for n in a.tune.voices[0]], [47, 48, 50, 53])
        self.assertEqual(a.selection(), {(0, 0), (0, 96), (0, 192)})
        a.guide = None
        a.frame([key(pygame.K_UP, pygame.KMOD_ALT)])
        self.assertIn("Set a key", a.message)

    def test_transpose_in_key_keeps_sharps_and_refuses_out_of_range(self):
        ed = self.app.ed
        ed.song = Song.from_mml("tempo 120\nV1: o4 c#4 b4\n")
        ed.refresh()
        ed.transpose_in_key([(0, 0), (0, 1)], 1, 0, "major")        # C# is a sharpened C: it becomes D#
        self.assertEqual([n.pitch for n in ed.tune.voices[0]], [51, 60])
        with self.assertRaises(EditError):
            ed.transpose_in_key([(0, 0)], 100, 0, "major")

    def test_k_key_opens_the_prompt_and_help_lists_the_new_keys(self):
        from chipsynth.composer import panels
        a = self.app
        a.frame([key(pygame.K_k)])
        self.assertEqual(a.overlay, "prompt")
        a.prompt["text"] = "A minor"
        a.frame([key(pygame.K_RETURN)])
        self.assertEqual(a.guide, (9, "minor"))
        keys = " ".join(k for k, d in panels.HELP)
        for want in ("K", "Alt+Up / Down", "Tracker , / .", "Tracker ; / :"):
            self.assertIn(want, keys)
