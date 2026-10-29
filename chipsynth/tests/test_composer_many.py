# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The composer on a song with more voices and DACs than the default 4 + 2, headless (SDL dummy driver)."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from chipsynth import mml
from chipsynth.song import Song
from chipsynth.composer import layout as L, model as M, panels, tracker as TR
from chipsynth.composer.app import Composer
from chipsynth.composer.prefs import Prefs

DATA = Path(__file__).resolve().parent / "data" / "many_voices"


def key(k, mod=0):
    return pygame.event.Event(pygame.KEYDOWN, key=k, mod=mod, unicode="")


def lib():
    lb = mml.builtin_library()
    lb["_sampledir"] = DATA / "samples"
    return lb


class ModelTests(unittest.TestCase):
    def test_roll_follows_the_tune(self):
        t = mml.parse((DATA / "six_voices.mml").read_text(), lib())
        r = M.Roll.from_tune(t, lambda i: "x")
        self.assertEqual((r.nv, r.nd, r.lanes), (6, 3, 9))
        self.assertEqual(r.names, ("V1", "V2", "V3", "V4", "V5", "V6", "D1", "D2", "D3"))
        self.assertEqual([b.voice for d in r.dac for b in d], [6] * 4 + [7] * 2 + [8] * 2)
        self.assertEqual(len(r.dac_of(8)), 2)

    def test_mixdown_and_solo_use_the_lane_numbers(self):
        t = mml.parse((DATA / "six_voices.mml").read_text(), lib())
        out = M.mixdown(t, {0, 6, 8})
        self.assertEqual(len(out.voices[0]), 0)
        self.assertEqual(len(out.dac[0]), 0)
        self.assertEqual(len(out.dac[1]), 2)
        self.assertEqual(len(out.dac[2]), 0)
        self.assertEqual(M.solo_set({7}, 9), set(range(9)) - {7})

    def test_playing_from_the_middle_keeps_every_lane(self):
        t = mml.parse((DATA / "six_voices.mml").read_text(), lib())
        for fn in (lambda: M.tune_from(t, 96), lambda: M.tune_range(t, 48, 240)):
            out = fn()
            self.assertEqual((len(out.voices), len(out.dac)), (6, 3))

    def test_tracker_helpers_know_where_the_drums_start(self):
        t = mml.parse((DATA / "six_voices.mml").read_text(), lib())
        r = M.Roll.from_tune(t, lambda i: "x")
        self.assertIsNone(TR.note_at(r, 6, 0))
        self.assertIsNotNone(TR.hit_at(r, 6, 0, 48))
        self.assertIsNotNone(TR.note_at(r, 5, 0))
        self.assertEqual(len(TR.column_cells(r, 8, 48, 0, 4)), 4)

    def test_layout_has_one_strip_per_dac(self):
        lay = L.compute(1280, 760, L.Panels())
        for nd in (0, 2, 8):
            g = L.roll_rects(lay.center, 18, nd)
            self.assertEqual(sum(1 for k in g if k.startswith("dac")), nd)
            cx, cy, cw, ch = lay.center
            for name, rect in g.items():
                self.assertGreaterEqual(rect[1], cy, name)
                self.assertLessEqual(rect[1] + rect[3], cy + ch, name)
        g = L.roll_rects(lay.center, 18, 3)
        self.assertEqual(L.dac_at(g, g["dac2"][0] + 3, g["dac2"][1] + 3), 2)
        self.assertIsNone(L.dac_at(g, g["grid"][0] + 3, g["grid"][1] + 3))


class WindowTests(unittest.TestCase):
    def open(self, view):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        a = Composer(self.screen, Prefs(view=view), prefs_path=self.dir / "prefs.json", root=self.dir)
        a.frame()
        a.load_song(Song.from_mml((DATA / "six_voices.mml").read_text(), lib()), "Six", source=str(self.dir / "t.mml"))
        a.frame()
        self.a = a
        return a

    def test_every_view_draws_all_the_tracks(self):
        for view in ("stack", "roll", "tracker"):
            a = self.open(view)
            self.assertEqual(a.roll.lanes, 9)
            self.assertEqual(a.stack.count, 9)
            for dark in (True, False):
                a.prefs.dark = dark
                a.frame()
                self.assertGreater(int(pygame.surfarray.array3d(self.screen).std()), 5, view)

    def test_roll_view_shows_a_strip_per_dac(self):
        a = self.open("roll")
        self.assertEqual([k for k in a.geo if k.startswith("dac")], ["dac0", "dac1", "dac2"])

    def test_number_keys_pick_the_new_lanes_and_mute_works_on_them(self):
        a = self.open("roll")
        a.frame([key(pygame.K_6)])
        self.assertEqual(a.sel_voice, 5)
        a.sel_voice = 8
        a.frame([key(pygame.K_m)])
        self.assertIn(8, a.muted)
        self.assertEqual(a.muted_now(), {8})

    def test_the_tracker_scrolls_sideways_to_keep_the_cursor_in_view(self):
        a = self.open("tracker")
        a.set_mode("tracker")
        for _ in range(8):
            a.frame([key(pygame.K_RIGHT)])
        self.assertEqual(a.tr.col, 8)
        g = panels.tracker_geo(a)
        self.assertLessEqual(a.tr.left, a.tr.col)
        self.assertLess(a.tr.col, a.tr.left + g["cols"])
        a.frame([key(pygame.K_RIGHT)])                          # wraps to the first column and scrolls back
        self.assertEqual((a.tr.col, a.tr.left), (0, 0))

    def test_the_voices_panel_scrolls_when_the_cards_do_not_fit(self):
        a = self.open("roll")
        a.voices_scroll = 10 ** 6
        a.frame()
        self.assertGreaterEqual(a.voices_scroll, 0)
        self.assertEqual(len(a.voice_cards), 9)

    def test_a_default_song_still_has_six_lanes(self):
        a = self.open("stack")
        a.load_song(Song.from_mml("V1: c\nD1: r4\n"), "Plain", source=str(self.dir / "p.mml"))
        a.frame()
        self.assertEqual((a.roll.nv, a.roll.nd, a.stack.count), (4, 2, 6))


if __name__ == "__main__":
    unittest.main()


class StaleGeometryTests(unittest.TestCase):
    def test_opening_a_bigger_song_between_update_and_draw_does_not_crash(self):
        """A click inside a frame (the Songs list) loads a song after update() laid the window out for the old one."""
        pygame.init()
        screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        d = Path(tempfile.mkdtemp())
        for view in ("roll", "stack", "tracker"):
            a = Composer(screen, Prefs(view=view), prefs_path=d / "prefs.json", root=d)
            a.frame()
            a.load_song(Song.from_mml("V1: c\nD1: r4\n"), "Plain", source=str(d / "p.mml"))
            a.frame()
            a.load_song(Song.from_mml((DATA / "six_voices.mml").read_text(), lib()), "Six", source=str(d / "t.mml"))
            panels.draw(a)                                       # no update() in between
            a.ui.begin()
            panels.draw(a)
            a.ui.end()
            a.load_song(Song.from_mml("V1: c\n"), "Plain", source=str(d / "p.mml"))
            panels.draw(a)


class LayoutGuardTests(unittest.TestCase):
    def test_drawing_after_the_mode_or_song_changed_behind_update_does_not_crash(self):
        pygame.init()
        screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        d = Path(tempfile.mkdtemp())
        a = Composer(screen, Prefs(view="roll"), prefs_path=d / "prefs.json", root=d)
        a.frame()
        a.load_song(Song.from_mml((DATA / "six_voices.mml").read_text(), lib()), "Six", source=str(d / "t.mml"))
        a.frame()
        for mode in ("stack", "tracker", "roll", "stack"):
            a.mode = mode                                        # changed without going through set_mode
            panels.draw(a)
        a.roll = M.Roll.from_tune(mml.parse("V1: c"), None)      # and a song with fewer tracks, with no layout at all
        a.mode = "roll"
        panels.draw(a)
        a.set_mode("stack")
        self.assertIn("heads", a.geo)


class TrimEndWindowTests(unittest.TestCase):
    def test_trim_end_button_action_shortens_the_song(self):
        pygame.init()
        screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        d = Path(tempfile.mkdtemp())
        a = Composer(screen, Prefs(view="roll"), prefs_path=d / "prefs.json", root=d)
        a.frame()
        a.load_song(Song.from_mml("V1: l4 c d e f | g a b >c |\n"), "Two bars", source=str(d / "t.mml"))
        a.frame()
        for n in range(7, 3, -1):
            a.ed.erase(0, n)
        a.sync()
        self.assertEqual(a.tune.length, 768)
        a.trim_end()
        a.frame()
        self.assertEqual(a.tune.length, 384)
        a.trim_end()                                     # nothing left to trim: says so, changes nothing
        self.assertEqual(a.tune.length, 384)


class ExtrasTests(unittest.TestCase):
    def open(self, text, view="tracker", size=(960, 600), lb=None):
        pygame.init()
        screen = pygame.display.set_mode(size, pygame.RESIZABLE)
        d = Path(tempfile.mkdtemp())
        a = Composer(screen, Prefs(view=view), prefs_path=d / "prefs.json", root=d)
        a.frame()
        a.load_song(Song.from_mml(text, lb), "Song", source=str(d / "t.mml"))
        a.frame()
        return a

    def wide(self):
        return "voices 12\ndacs 4\n" + "\n".join("V%d: c d e f" % i for i in range(1, 13)) + "\n"

    def test_the_tracker_shows_a_bar_when_columns_do_not_fit_and_it_scrolls(self):
        a = self.open(self.wide())
        g = panels.tracker_geo(a)
        self.assertLess(g["cols"], 16)
        self.assertIsNotNone(g["hbar"])
        hb = g["hbar"]
        a.frame([pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(hb[0] + hb[2] - 3, hb[1] + 3), button=1)])
        self.assertEqual(a.tr.left, 16 - g["cols"])
        a.frame([pygame.event.Event(pygame.MOUSEBUTTONUP, pos=(hb[0] + hb[2] - 3, hb[1] + 3), button=1)])
        a.frame([pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(hb[0] + 2, hb[1] + 3), button=1)])
        self.assertEqual(a.tr.left, 0)
        self.assertEqual(a.tr.col, 0)

    def test_no_bar_when_everything_fits(self):
        a = self.open("V1: c d e f\n", size=(1280, 760))
        self.assertIsNone(panels.tracker_geo(a)["hbar"])

    def test_shift_wheel_scrolls_the_columns(self):
        a = self.open(self.wide())
        a.ui.pos = (a.layout.center[0] + 100, a.layout.center[1] + 100)
        pygame.key.set_mods(pygame.KMOD_SHIFT)
        try:
            a.frame([pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=-1, precise_x=0.0, precise_y=-1.0)])
        finally:
            pygame.key.set_mods(0)
        self.assertEqual(a.tr.left, 1)

    def test_the_octave_follows_the_keys(self):
        a = self.open("V1: c d e f\n", size=(1280, 760))
        self.assertEqual(a.tr.octave, 4)
        a.frame([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_UP, mod=pygame.KMOD_SHIFT, unicode="")])
        self.assertEqual(a.tr.octave, 5)
        a.frame([pygame.event.Event(pygame.KEYDOWN, key=pygame.K_MINUS, mod=0, unicode="-")])
        self.assertEqual(a.tr.octave, 4)

    def test_end_the_song_at_the_cursor(self):
        a = self.open("V1: l4 c d e f | g a b >c |\n", view="roll", size=(1280, 760))
        a.cursor_tick = 480
        a.cut_here()
        self.assertEqual(a.tune.length, 480)
        a.cursor_tick = 9999
        a.cut_here()                                    # past the end: says so and changes nothing
        self.assertEqual(a.tune.length, 480)
