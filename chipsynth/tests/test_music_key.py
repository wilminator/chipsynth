# Copyright (C) 1994, 2026 Michael Allen Wilmes
import tempfile
import unittest
from pathlib import Path

from chipsynth import mml


def notes(text, lib=None):
    t = mml.parse("tempo 120\n" + text, lib)
    return [(n.pitch, n.dur) for n in t.voices[0]]


def pitches(text, lib=None):
    return [p for p, _ in notes(text, lib)]


class DegreeTests(unittest.TestCase):
    def test_c_major_degrees_match_letters(self):
        self.assertEqual(pitches("V1: key c major 1 2 3 4 5 6 7 8 9"),
                         pitches("V1: c d e f g a b >c d"))

    def test_other_tonics_and_modes(self):
        self.assertEqual(pitches("V1: key a minor 1 3 5 8"), pitches("V1: a >c e a"))
        self.assertEqual(pitches("V1: key d dorian 1 2 3 4 5 6 7"), pitches("V1: d e f g a b >c"))
        self.assertEqual(pitches("V1: key f# lydian 1 4"), pitches("V1: f# b#"))

    def test_octave_commands_still_apply(self):
        self.assertEqual(pitches("V1: o5 key c major 1 > 1 < < 1"), [60, 72, 48])
        self.assertEqual(pitches("V1: key c major o3 5"), [43])

    def test_accidentals(self):
        self.assertEqual(pitches("V1: key c major 3- 4+ 1 1--"), [51, 54, 48, 46])

    def test_lengths_use_a_colon(self):
        self.assertEqual(notes("V1: key c major l8 1 3:4 5:16. 8:4;"),
                         [(48, 48), (52, 96), (55, 36), (60, 64)])

    def test_default_length_between_notes(self):
        self.assertEqual([d for _, d in notes("V1: key c major l4 1 2 l8 3")], [96, 96, 48])

    def test_key_off_returns_to_letters(self):
        self.assertEqual(pitches("V1: key c major 1 key off c e"), [48, 48, 52])

    def test_key_is_per_voice(self):
        t = mml.parse("tempo 120\nV1: key c major 1 2\nV2: c d")
        self.assertEqual([n.pitch for n in t.voices[1]], [48, 50])

    def test_rests_ties_and_repeats_work_in_key_mode(self):
        self.assertEqual(notes("V1: key c major l8 [1 r]2 3&8"), [(48, 48), (48, 48), (52, 96)])
        p = pitches("V1: key c major 1 2 | 3 4 |")
        self.assertEqual(p, [48, 50, 52, 53])

    def test_a_pattern_inherits_the_voice_key(self):
        self.assertEqual(pitches("[pat]\nup = 1 3 5\nV1: key c major !up"), [48, 52, 55])

    def test_pentatonic_octave_is_degree_six(self):
        self.assertEqual(pitches("V1: key c pentatonic 1 2 3 4 5 6 7"), [48, 50, 52, 55, 57, 60, 62])
        self.assertEqual(pitches("V1: key a minor-pentatonic 1 5 6"), [57, 67, 69])

    def test_errors(self):
        for bad in ("key c major 0", "key c major 10", "key c pentatonic 8", "key c major c4", "key c", "key c nonesuch 1",
                    "key"):
            with self.assertRaises(mml.MMLError, msg=bad):
                mml.parse("tempo 120\nV1: " + bad)

    def test_letter_error_says_what_to_do(self):
        with self.assertRaises(mml.MMLError) as cm:
            mml.parse("tempo 120\nV1: key c major c4")
        self.assertIn("key off", str(cm.exception))

    def test_out_of_range_degree_names_the_octave(self):
        with self.assertRaises(mml.MMLError):
            mml.parse("tempo 120\nV1: o8 key c major 8")

    def test_custom_scale_in_a_key_section(self):
        self.assertEqual(pitches("[key]\ndream = 0 2 3 7 9\nV1: key c dream 1 2 3 4 5 6"),
                         [48, 50, 51, 55, 57, 60])

    def test_bad_key_lines(self):
        for bad in ("[key]\nx = 1 2 3\nV1: c", "[key]\nx = 0 2 2\nV1: c", "[key]\nx = 0 14\nV1: c", "[key]\nx 0\nV1: c"):
            with self.assertRaises(mml.MMLError, msg=bad):
                mml.parse("tempo 120\n" + bad)

    def test_library_scales(self):
        d = Path(tempfile.mkdtemp())
        (d / "key.mml").write_text("[key]\nhirajoshi = 0 2 3 7 8\n")
        lib = mml.load_library(d)
        self.assertEqual(pitches("V1: key c hirajoshi 1 3 5", lib), [48, 51, 56])

    def test_import_key_in_the_header(self):
        d = Path(tempfile.mkdtemp())
        (d / "key.mml").write_text("[key]\nhirajoshi = 0 2 3 7 8\n")
        lib = mml.load_library(d)
        t = mml.parse("tempo 120\nimport key.hirajoshi\nV1: key c hirajoshi 2", lib)
        self.assertEqual(t.voices[0][0].pitch, 50)
        with self.assertRaises(mml.MMLError):
            mml.parse("tempo 120\nimport key.nothing\nV1: c")

    def test_existing_octave_prefix_still_works_without_a_key(self):
        self.assertEqual(pitches("V1: 5c8 d8"), [60, 62])

    def test_a_degree_tune_plays(self):
        from chipsynth.player import TunePlayer
        t = mml.parse("tempo 120\nV1: @lead key c major l8 1 3 5 8")
        self.assertTrue(TunePlayer(t).render(20000).any())


if __name__ == "__main__":
    unittest.main()
