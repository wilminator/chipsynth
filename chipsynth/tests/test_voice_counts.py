# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""A song may have 1-16 voices and 0-8 DACs (header lines `voices N` / `dacs N`); the default stays 4 and 2."""
import unittest
from pathlib import Path

import numpy as np

from chipsynth import mml, package as pk
from chipsynth.mml import MMLError
from chipsynth.player import TunePlayer
from chipsynth.song import Song, Trace, track_names
from chipsynth.songedit import Editor
from chipsynth.synth import Synth

DATA = Path(__file__).resolve().parent / "data" / "many_voices"


def lib():
    lb = mml.builtin_library()
    lb["_sampledir"] = DATA / "samples"
    return lb


def six():
    return (DATA / "six_voices.mml").read_text()


class SynthTests(unittest.TestCase):
    def test_default_is_four_and_two(self):
        s = Synth()
        self.assertEqual((len(s.voices), len(s.dacs)), (4, 2))

    def test_other_counts(self):
        s = Synth(9, 0)
        self.assertEqual((len(s.voices), len(s.dacs)), (9, 0))

    def test_counts_are_limited(self):
        for args in ((0, 2), (17, 2), (4, -1), (4, 9)):
            with self.assertRaises(ValueError, msg=args):
                Synth(*args)


class ParseTests(unittest.TestCase):
    def test_default_tune_has_four_voices_and_two_dacs(self):
        t = mml.parse("V1: c")
        self.assertEqual((len(t.voices), len(t.dac)), (4, 2))

    def test_header_sets_the_counts(self):
        t = mml.parse(six(), lib())
        self.assertEqual((len(t.voices), len(t.dac)), (6, 3))
        self.assertEqual([len(v) for v in t.voices], [7, 7, 7, 4, 4, 4])
        self.assertEqual([len(d) for d in t.dac], [4, 2, 2])
        self.assertEqual(t.warnings, [])

    def test_a_voice_over_the_count_is_an_error_that_names_the_fix(self):
        with self.assertRaises(MMLError) as cm:
            mml.parse("V5: c")
        self.assertIn("voices 5", str(cm.exception))

    def test_a_dac_over_the_count_is_an_error(self):
        with self.assertRaises(MMLError):
            mml.parse("dacs 1\nV1: c\nD2: r4")

    def test_two_digit_voice(self):
        t = mml.parse("voices 12\nV12: c")
        self.assertEqual(len(t.voices[11]), 1)

    def test_zero_dacs_is_allowed_and_zero_voices_is_not(self):
        self.assertEqual(len(mml.parse("dacs 0\nV1: c").dac), 0)
        with self.assertRaises(MMLError):
            mml.parse("voices 0\nV1: c")
        with self.assertRaises(MMLError):
            mml.parse("voices 17\nV1: c")
        with self.assertRaises(MMLError):
            mml.parse("dacs 9\nV1: c")

    def test_the_header_line_may_come_after_a_voice_line(self):
        t = mml.parse("V6: c\nvoices 6")
        self.assertEqual(len(t.voices[5]), 1)


class SongTests(unittest.TestCase):
    def test_track_names(self):
        self.assertEqual(track_names(), ("V1", "V2", "V3", "V4", "D1", "D2"))
        self.assertEqual(track_names(2, 1), ("V1", "V2", "D1"))

    def test_song_round_trips_through_mml_and_matches_parse(self):
        song = Song.from_mml(six(), lib())
        self.assertEqual((song.voices, song.dacs), (6, 3))
        self.assertEqual(song.names, track_names(6, 3))
        text = song.to_mml()
        self.assertIn("voices 6", text)
        self.assertIn("dacs 3", text)
        a = song.compile(lib())
        b = mml.parse(text, lib())
        self.assertEqual([[(n.start, n.pitch, n.dur) for n in v] for v in a.voices],
                         [[(n.start, n.pitch, n.dur) for n in v] for v in b.voices])
        self.assertEqual(a.dac, b.dac)

    def test_default_song_writes_no_count_lines(self):
        text = Song.from_mml("V1: c\nD1: r4\n").to_mml()
        self.assertNotIn("voices", text)
        self.assertNotIn("dacs", text)

    def test_package_round_trip(self):
        song = Song.from_mml(six(), lib())
        back = pk.decode_song(pk.encode_song(song))
        self.assertEqual((back.voices, back.dacs), (6, 3))
        self.assertEqual(back.to_mml(), song.to_mml())

    def test_a_default_song_encodes_exactly_as_before(self):
        data = pk.encode_song(Song.from_mml("V1: c\n"))
        self.assertEqual(data[0], 0)                       # minor revision 0: no counts stored
        back = pk.decode_song(data)
        self.assertEqual((back.voices, back.dacs), (4, 2))

    def test_package_rejects_impossible_counts(self):
        song = Song.from_mml("V1: c\n")
        song.voices = 17
        with self.assertRaises(pk.PackageError):
            pk.decode_song(pk.encode_song(song))

    def test_trace_covers_every_track(self):
        song = Song.from_mml(six(), lib())
        tr = Trace(song.voices + song.dacs)
        song.compile(lib(), tr)
        self.assertEqual(len(tr.pieces), 9)
        self.assertTrue(all(tr.ends[i] > 0 for i in range(9)))

    def test_editor_works_on_a_big_song(self):
        ed = Editor(Song.from_mml(six(), lib()), lib())
        self.assertEqual(len(ed.trace.pieces), 9)
        self.assertEqual(len(ed.tune.voices), 6)


class PlayTests(unittest.TestCase):
    def render(self, text):
        p = TunePlayer(mml.parse(text, lib()), loop=False)
        return np.concatenate([p.render(4410) for _ in range(30)])

    def test_the_sixth_voice_and_third_dac_make_sound(self):
        voice_only = self.render("voices 6\ndacs 3\nV6: l4 o5 c e g")
        self.assertGreater(np.abs(voice_only).max(), 0.01)
        dac_only = self.render("voices 1\ndacs 3\nV1: r1\n[sample]\nkick = kit_kick\nD3: kick4")
        self.assertGreater(np.abs(dac_only).max(), 0.01)

    def test_the_demo_song_plays(self):
        out = self.render(six())
        self.assertGreater(np.abs(out).max(), 0.05)

    def test_default_rendering_is_unchanged_by_the_count_machinery(self):
        a = self.render("V1: l4 c e g\nD1: r4")
        b = self.render("voices 4\ndacs 2\nV1: l4 c e g\nD1: r4")
        np.testing.assert_array_equal(a, b)


if __name__ == "__main__":
    unittest.main()
