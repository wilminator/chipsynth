# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The track stack's maths (no display): sizes, scrolling, what is visible, hit testing and pitch windows."""
import tempfile
import unittest
from pathlib import Path

from chipsynth import mml
from chipsynth.composer import model as M
from chipsynth.composer.prefs import Prefs
from chipsynth.composer.tracks import Stack, MIN_H, MAX_H, MIN_SPAN

TUNE = "tempo 120\nV1: @lead c4 d4 e4 f4\nV2: @bass <c1 e1\nV3: @bell c8 r8\n"


def roll():
    return M.Roll.from_tune(mml.parse(TUNE), lambda i: "x")


class SizeAndScroll(unittest.TestCase):
    def test_total_is_tracks_times_height(self):
        st = Stack(height=100)
        self.assertEqual(st.total(), 600)
        self.assertEqual(st.max_scroll(250), 350)
        self.assertEqual(st.max_scroll(900), 0)

    def test_clamp_keeps_the_scroll_inside_the_stack(self):
        st = Stack(height=100, scroll=9999)
        st.clamp(250)
        self.assertEqual(st.scroll, 350)
        st.scroll = -40
        st.clamp(250)
        self.assertEqual(st.scroll, 0)

    def test_wheel_moves_a_third_of_a_track_and_stops_at_the_ends(self):
        st = Stack(height=90)
        st.wheel(-1, 200)                       # a notch down the list (negative is towards the end)
        self.assertEqual(st.scroll, 30)
        st.wheel(+5, 200)
        self.assertEqual(st.scroll, 0)
        st.wheel(-100, 200)
        self.assertEqual(st.scroll, st.max_scroll(200))

    def test_scroll_to_shows_a_whole_track_with_the_least_movement(self):
        st = Stack(height=100)
        st.scroll_to(3, 250)                    # track 3 spans 300..400; the area shows 250 px
        self.assertEqual(st.scroll, 150)
        st.scroll_to(0, 250)
        self.assertEqual(st.scroll, 0)
        st.scroll_to(1, 250)                    # already in view from 0..250? no: 100..200 is
        self.assertEqual(st.scroll, 0)

    def test_scroll_to_in_an_area_shorter_than_a_track_shows_its_top(self):
        st = Stack(height=100)
        st.scroll_to(2, 60)
        self.assertEqual(st.scroll, 200)

    def test_changing_the_height_keeps_the_top_track_at_the_top(self):
        st = Stack(height=100, scroll=250)      # track 2, 50 px into it
        st.set_height(60, 100)
        self.assertEqual(st.height, 60)
        self.assertEqual(st.scroll // 60, 2)
        st.set_height(10_000, 100)
        self.assertEqual(st.height, MAX_H)
        st.set_height(1, 100)
        self.assertEqual(st.height, MIN_H)

    def test_a_height_change_keeps_a_named_track_in_view(self):
        st = Stack(height=60, scroll=0)
        st.set_height(200, 250, keep_track=4)
        self.assertGreaterEqual(4 * 200, st.scroll)
        self.assertLessEqual(5 * 200, st.scroll + 250 + 200)


class HitTesting(unittest.TestCase):
    def test_visible_lists_cut_lanes_at_both_ends(self):
        st = Stack(height=100, scroll=50)
        self.assertEqual(st.visible(200), [(0, -50), (1, 50), (2, 150)])

    def test_track_at(self):
        st = Stack(height=100, scroll=50)
        self.assertEqual(st.track_at(0, 200), 0)       # the lower half of track 0
        self.assertEqual(st.track_at(49, 200), 0)
        self.assertEqual(st.track_at(50, 200), 1)
        self.assertIsNone(st.track_at(-1, 200))
        self.assertIsNone(st.track_at(200, 200))

    def test_track_at_past_the_last_track_is_none(self):
        st = Stack(height=100)
        self.assertIsNone(st.track_at(650, 700))


class PitchWindow(unittest.TestCase):
    def test_a_lane_fits_its_own_notes(self):
        st, r = Stack(height=96), roll()
        low, high = st.window(0, r)
        pitches = [n.pitch for n in r.notes[0]]
        self.assertLessEqual(low, min(pitches))
        self.assertGreaterEqual(high, max(pitches))
        self.assertGreaterEqual(high - low + 1, MIN_SPAN)

    def test_two_tracks_have_their_own_windows(self):
        st, r = Stack(), roll()
        self.assertGreater(st.window(0, r)[0], st.window(1, r)[1] - MIN_SPAN * 3)
        self.assertNotEqual(st.window(0, r), st.window(1, r))

    def test_an_empty_track_uses_the_songs_range(self):
        st, r = Stack(), roll()
        low, high = st.window(3, r)
        self.assertGreaterEqual(high - low + 1, MIN_SPAN)

    def test_the_window_stays_inside_the_pitch_range(self):
        st, r = Stack(), M.Roll()
        r.notes[0] = [M.RollNote(0, 0, 24, M.HIGH, 15)]
        low, high = st.window(0, r)
        self.assertLessEqual(high, M.HIGH)
        self.assertGreaterEqual(low, M.LOW)
        self.assertGreaterEqual(high - low + 1, MIN_SPAN)

    def test_y_and_pitch_round_trip_in_the_middle_of_a_row(self):
        st, r = Stack(height=120, scroll=40), roll()
        low, high = st.window(1, r)
        for p in range(low, high + 1):
            y = st.y_of(1, p, r) + st.row_px(1, r) / 2
            self.assertEqual(st.pitch_at(1, y, r), p)

    def test_pitch_at_clamps_to_the_window(self):
        st, r = Stack(height=100), roll()
        low, high = st.window(0, r)
        self.assertEqual(st.pitch_at(0, -500, r), high)
        self.assertEqual(st.pitch_at(0, 5000, r), low)

    def test_a_pinned_window_wins_and_unpin_restores(self):
        st, r = Stack(), roll()
        auto = st.window(0, r)
        st.pin(0, 30, 60)
        self.assertEqual(st.window(0, r), (30, 60))
        st.unpin(0)
        self.assertEqual(st.window(0, r), auto)

    def test_a_pinned_window_is_never_narrower_than_two_octaves(self):
        st = Stack()
        st.pin(0, 40, 41)
        low, high = st.pinned[0]
        self.assertGreaterEqual(high - low + 1, MIN_SPAN)

    def test_drum_tracks_have_no_pitch(self):
        st = Stack()
        self.assertTrue(st.is_melodic(0))
        self.assertFalse(st.is_melodic(4))


class PrefsHeight(unittest.TestCase):
    def test_track_height_is_remembered_and_clamped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            pr = Prefs()
            pr.track_h = 150
            pr.save(p)
            self.assertEqual(Prefs.load(p).track_h, 150)
            p.write_text('{"track_h": 5}')
            self.assertEqual(Prefs.load(p).track_h, MIN_H)
            p.write_text('{"track_h": 99999}')
            self.assertEqual(Prefs.load(p).track_h, MAX_H)
            p.write_text('{"track_h": "tall"}')
            self.assertEqual(Prefs.load(p).track_h, 96)

    def test_the_centre_view_is_remembered_and_a_bad_one_falls_back_to_the_stack(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            self.assertEqual(Prefs.load(p).view, "stack")            # no file: the track view
            pr = Prefs()
            pr.view = "tracker"
            pr.save(p)
            self.assertEqual(Prefs.load(p).view, "tracker")
            p.write_text('{"view": "wide"}')
            self.assertEqual(Prefs.load(p).view, "stack")


if __name__ == "__main__":
    unittest.main()
