# Copyright (C) 2026 Michael Allen Wilmes
"""The Song model: MML -> Song -> Tune and Song -> MML round trips (PLAN-CHIPSYNTH.md, C0)."""
import ast
import dataclasses
import glob
import re
import unittest
from pathlib import Path

import numpy as np

from chipsynth import dpcm, mml, musicgen
from chipsynth.song import (Song, Clip, SongError, Note, Rest, Repeat, Call, Comment, Extend, Tie, Degree, Rel)
from chipsynth.songmml import length_pieces, instrument_text

ROOT = Path(__file__).resolve().parents[2]


class _AnySamples(dict):
    """A sample library where every name exists (so tests that mention samples still parse)."""
    def __init__(self):
        t = np.arange(2000) / 44100.0
        self.smp = dpcm.convert(np.sin(2 * np.pi * 200 * t), 44100, rate=12, name="x")

    def __contains__(self, k):
        return True

    def __getitem__(self, k):
        return self.smp


def lib():
    lb = mml.builtin_library()
    lb["_mem"] = _AnySamples()
    return lb


def norm(t):
    return dataclasses.replace(t, warnings=sorted(re.sub(r"^line \d+: ", "", w) for w in t.warnings))


def bare(t):
    return dataclasses.replace(t, warnings=[])


FEATURES = """\
// a header comment
title Everything
author Mike
comment all the features
tempo 100
time 4/4
tune -30
echo 240ms,40,30,wide
reverb 5
instr bell
fx bright

[instr]
lead2x wave=pulse pw=12 a=1 d=6 s=9 r=3 vol=12 pan=-2 vib=5,3 slide=6 arp=0,4,7 arprate=12 pws=25,75,6 filter=lp,8,4 fsweep=2,12,4 send=7
hat2 wave=noise nmode=short a=0 d=1 s=0 r=1 vol=9

[fx]
wobble vib=5,3 pws=25,75,6


[key]
blues = 0 3 5 6 7 10

[sample]
kick = kick rate=12
snare

[pat]
riff = c8 d e f g a b >c<
bassfig = l16 c c r c [d e]2 c8.
nest = !riff !riff [!riff c4]2
deg = 1 3 5 8:4 3:8. 5#:16

// the melody
V1: l8 v12 @lead2x t100 !riff | o5 e4 c4.& c8 | c4&16 d4.; r2.. | [c d e f]3 [d4 e4] fx wobble |
V1: pan -3 slide 12 vib 5,3 adsr 1,2,3,4 pw 30 wave tri nmode short send 4 filter bp,6,2 fsweep 1,2,3 arp 0,12
V1: o4g loop 5c 3d 4a- b+ > < <<  >> v+ v- v+3 v-2 v0 r16 r;  p4 l4. c l8
V2: key c major 1 3 5 8:4 &1 key off @bass c4 & c4 key a minor 3 4:8 6#
V2: @bell !deg
V3: !nest !bassfig once
D1: kick4 snare8 r8 level 64 stop v10 pan 3 kick8.; l8 snare
D2: [kick8 r8]2 !drumpat
"""


class Corpus(unittest.TestCase):
    def check(self, text, lb=None, label=""):
        lb = lb or lib()
        t0 = mml.parse(text, lb)
        s = Song.from_mml(text, lb)
        self.assertEqual(norm(t0), norm(s.compile(lb)), label + " Song.compile != parse")
        m = s.to_mml()
        s2 = Song.from_mml(m, lb)
        self.assertEqual(bare(t0), bare(s2.compile(lb)), label + " export did not keep the tune")
        self.assertEqual(s2.to_mml(), m, label + " export is not a fixpoint")
        self.assertEqual(s2, dataclasses.replace(s, warnings=s2.warnings), label + " song changed on re-import")
        return s


class FeatureTests(Corpus):
    def lib(self):
        lb = lib()
        lb["pat"]["drumpat"] = ("kick8 r8", 1, 1)
        lb["fx"]["bright"] = [("vib", "4,2")]
        return lb

    def test_all_features_round_trip(self):
        self.check(FEATURES, self.lib(), "features")

    def test_clips_repeats_and_calls_stay_objects(self):
        s = Song.from_mml(FEATURES, self.lib())
        self.assertIn("riff", s.clips)
        self.assertIn("drumpat", s.clips)             # a library pattern is read into the song
        self.assertTrue(any(isinstance(e, Repeat) for e in s.tracks["V1"]))
        self.assertTrue(any(isinstance(e, Call) and e.name == "riff" for e in s.tracks["V1"]))
        self.assertTrue(any(isinstance(e, Call) for e in s.clips["nest"].items))
        text = s.to_mml()
        self.assertIn("[pat]", text)
        self.assertIn("!riff", text)
        self.assertIn("[c d e f]3", text)

    def test_own_line_comments_are_notes_and_inline_ones_are_dropped(self):
        s = Song.from_mml("// top\ntitle t\n// before v1\nV1: c d // inline\n// the end\n")
        self.assertEqual(s.notes, ["top"])
        self.assertEqual([e.text for e in s.tracks["V1"] if isinstance(e, Comment)], ["before v1"])
        self.assertEqual(s.footer, ["the end"])
        self.assertNotIn("inline", s.to_mml())

    def test_ties_and_extends_are_kept(self):
        s = Song.from_mml("V1: c4&16 d4 & d4 e4&8&16")
        kinds = [type(e).__name__ for e in s.tracks["V1"]]
        self.assertEqual(kinds, ["Note", "Extend", "Note", "Tie", "Note", "Note", "Extend", "Extend"])
        self.check("V1: c4&16 d4 & d4 e4&8&16")

    def test_relative_lengths_follow_the_default(self):
        s = Song.from_mml("V1: l8 c. c; l4 c.")
        self.assertEqual([e.dur for e in s.tracks["V1"] if isinstance(e, Note)], [Rel(1, False), Rel(0, True), Rel(1, False)])
        self.check("V1: l8 c. c; l4 c.")

    def test_degrees(self):
        s = Song.from_mml("V1: key c major 1 3:8 5#")
        self.assertTrue(any(isinstance(e, Degree) for e in s.tracks["V1"]))
        self.check("V1: key c major 1 3:8 5# 8:4 key off c")

    def test_pattern_follows_the_callers_octave(self):
        t = Song.from_mml("[pat]\np = c d\nV1: o3 !p\nV2: o5 !p\n").compile()
        self.assertEqual(t.voices[1][0].pitch - t.voices[0][0].pitch, 24)

    def test_unused_unreadable_pattern_is_kept_as_text(self):
        text = "[pat]\nodd = c d @nosuch ((\nV1: c\n"
        # parse is lazy about patterns nobody calls, so the song must be too
        mml.parse(text)
        s = Song.from_mml(text)
        self.assertIsNotNone(s.clips["odd"].raw)
        self.assertIn("odd = c d @nosuch ((", s.to_mml())

    def test_pattern_on_both_kinds_is_refused(self):
        with self.assertRaises(mml.MMLError):
            Song.from_mml("[sample]\nk = k\n[pat]\np = r8\nV1: !p\nD1: !p\n", lib())

    def test_mistakes_are_reported_like_parse(self):
        with self.assertRaises(mml.MMLError) as cm:
            Song.from_mml("V1: c d !nope")
        self.assertEqual(cm.exception.line, 1)

    def test_compiled_song_edits_take_effect(self):
        s = Song.from_mml("V1: c d")
        s.tracks["V1"].append(Note("e", 0, 96))
        self.assertEqual(len(s.compile().voices[0]), 3)
        s.bpm = 90
        self.assertEqual(s.compile().bpm, 90)

    def test_clip_edit_changes_every_call(self):
        s = Song.from_mml("[pat]\np = c\nV1: !p !p\n")
        s.clips["p"].items.append(Note("d", 0, None))
        self.assertEqual(len(s.compile().voices[0]), 4)


class LengthTests(unittest.TestCase):
    def test_pieces_add_up_for_every_writable_length(self):
        from chipsynth.song import explicit_ticks
        for t in range(2, 2500):
            try:
                pieces = length_pieces(t)
            except SongError:
                self.assertIn(t, (2, 3, 5, 7, 11))          # the only small gaps
                continue
            total = 0
            for p in pieces:
                m = re.fullmatch(r"(\d+)(\.*)(;?)", p)
                total += explicit_ticks(int(m.group(1)), len(m.group(2)), bool(m.group(3)))
            self.assertEqual(total, t)

    def test_common_lengths_are_one_token(self):
        self.assertEqual(length_pieces(96), ["4"])
        self.assertEqual(length_pieces(144), ["4."])
        self.assertEqual(length_pieces(64), ["4;"])

    def test_unwritable_length(self):
        with self.assertRaises(SongError):
            length_pieces(1)

    def test_a_tie_into_a_degree_cannot_be_written(self):
        s = Song()
        s.tracks["V1"] = [Tie(), Degree(3)]
        with self.assertRaises(SongError):
            s.to_mml()

    def test_instrument_text_is_minimal_and_never_empty(self):
        from chipsynth.synth import Instrument
        self.assertEqual(instrument_text("x", Instrument()), "x wave=pulse")
        self.assertIn("filter=lp,8,4", instrument_text("x", Instrument(filt="lp", filt_cut=8, filt_res=4)))


class ExampleTunes(Corpus):
    def test_demo_songs(self):
        files = sorted(glob.glob(str(ROOT / "demo_songs" / "*.mml")))
        self.assertTrue(files)
        for f in files:
            self.check(Path(f).read_text(), None, f)

    def test_generator_output(self):
        for kind in musicgen.KINDS:
            for mood in musicgen.MOODS:
                for seed in (1, 2, 3):
                    self.check(musicgen.generate(kind, seed, mood), None, "%s/%s/%d" % (kind, mood, seed))

    def test_every_mml_string_in_the_test_suites(self):
        """Any string literal in the tests that parses as MML must round-trip too."""
        found = 0
        for path in glob.glob(str(ROOT / "chipsynth" / "tests" / "test_music*.py")) + \
                glob.glob(str(ROOT / "tests" / "test_make_music.py")):
            tree = ast.parse(Path(path).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 6:
                    text = node.value
                    try:
                        mml.parse(text, lib())
                    except Exception:
                        continue
                    found += 1
                    self.check(text, None, "%s:%d" % (Path(path).name, node.lineno))
        self.assertGreater(found, 50)
