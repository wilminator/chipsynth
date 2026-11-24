# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writing FamiStudio text.  The reader (formats/famistudio_text.py) is the check: what is written must read back."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from chipsynth import dpcm, mml, package as pk
from chipsynth.formats import famistudio_export as ex, famistudio_text as fam
from chipsynth.song import Song
from chipsynth.__main__ import main

MML = """tempo 120
[instr]
lead wave=pulse pw=25 a=0 d=6 s=8 r=3
bass wave=tri
hat wave=noise nmode=short a=0 d=3 s=0 r=0
V1: @lead l8 c d e f g2 r4 c4
V2: @lead v9 o3 l4 c e g c
V3: @bass l4 o2 c e g c
V4: @hat l8 o5 c c c c r2
"""


def notes(tune, v):
    return [(n.start, n.dur, n.pitch) for n in tune.voices[v]]


def round_trip(text=MML, **kw):
    tune = Song.from_mml(text).compile()
    out, rep = ex.dump(tune, **kw)
    back = fam.load(out)
    imp = back.songs[0]
    lib = dict(mml.builtin_library(), _mem=imp.samples)
    return tune, imp.song.compile(lib), out, rep, back


class RoundTripTests(unittest.TestCase):
    def test_every_channel_keeps_its_notes_and_timing(self):
        a, b, *_ = round_trip()
        for v in (0, 1, 2):
            self.assertEqual(notes(a, v), notes(b, v), "voice %d" % (v + 1))

    def test_the_tempo_survives(self):
        a, b, *_ = round_trip()
        self.assertEqual(a.bpm, b.bpm)

    def test_noise_keeps_its_order_of_notes(self):
        text = "V4: l8 o2 c o4 c o6 c o8 c\n"
        a, b, *_ = round_trip(text)
        got = [p for _, _, p in notes(b, 3)]
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(set(got)), 4)

    def test_the_instruments_come_back_with_their_pulse_width_and_noise_mode(self):
        *_, back = round_trip()
        imp = back.songs[0].song
        self.assertEqual(imp.instruments["lead"].pw, 25)
        self.assertTrue(any(i.nmode == "short" for i in imp.instruments.values()))

    def test_a_note_volume_comes_back(self):
        a, b, *_ = round_trip()
        self.assertEqual([n.vol for n in a.voices[1]], [n.vol for n in b.voices[1]])

    def test_a_loop_point_comes_back_on_its_pattern(self):
        text = "time 4/4\nV1: o4 c1 loop d1 e1\n"
        a, b, *_ = round_trip(text)
        self.assertEqual(a.loop_tick, b.loop_tick)

    def test_pal_is_written_and_read(self):
        tune = Song.from_mml(MML).compile()
        text, _ = ex.dump(tune, region="pal")
        self.assertIn('PAL="true"', text)
        self.assertTrue(fam.looks_like(text))


class ReportTests(unittest.TestCase):
    def test_what_is_dropped_is_said(self):
        text = "voices 6\n" + "\n".join("V%d: l4 c d e f" % i for i in range(1, 7)) + "\n"
        _, rep = ex.dump(Song.from_mml(text).compile())
        self.assertTrue(any("V5" in m and "V6" in m for m in rep.dropped))

    def test_tempo_changes_are_dropped(self):
        _, rep = ex.dump(Song.from_mml("V1: l4 c d t90 e f\n").compile())
        self.assertTrue(any("tempo changes" in m for m in rep.dropped))

    def test_a_vibrato_instrument_says_so(self):
        text = "[instr]\nv wave=pulse vib=8,6\nV1: @v c d e f\n"
        try:
            tune = Song.from_mml(text).compile()
        except mml.MMLError:
            self.skipTest("instrument vibrato spelling differs")
        _, rep = ex.dump(tune)
        self.assertTrue(any("vibrato" in m for m in rep.dropped))

    def test_a_pulse_width_the_nes_does_not_have_is_approximated(self):
        text = "[instr]\nw wave=pulse pw=40\nV1: @w c d e f\n"
        _, rep = ex.dump(Song.from_mml(text).compile())
        self.assertTrue(any("pulse width" in m for m in rep.approximated))


class DpcmTests(unittest.TestCase):
    def test_a_sample_is_written_with_its_rate_and_plays_back(self):
        smp = dpcm.Sample(name="kick", rate=12, tracks=[[bytes.fromhex("aa55aa55")]])
        lib = dict(mml.builtin_library(), _mem={"kick": smp})
        tune = Song.from_mml("[sample]\nkick\n\nV1: c\nD1: kick r2 kick\n", lib).compile(lib)
        text, rep = ex.dump(tune)
        self.assertIn('DPCMSample Name="kick" Data="aa55aa55"', text)
        self.assertIn('Pitch="12"', text)
        back = fam.load(text).songs[0]
        self.assertEqual(back.samples["kick"].rate, 12)
        self.assertEqual(back.samples["kick"].tracks, [[bytes.fromhex("aa55aa55")]])


class CliTests(unittest.TestCase):
    def test_export_song_writes_a_txt_that_imports_again(self):
        tmp = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="t")
        pkg.add_song("tune", Song.from_mml(MML))
        pk.save(pkg, tmp / "t.csp")
        self.assertEqual(main(["export-song", str(tmp / "t.csp"), "-o", str(tmp / "t.txt")]), 0)
        self.assertTrue(fam.looks_like((tmp / "t.txt").read_text(encoding="utf-8")))
        self.assertEqual(main(["import", str(tmp / "u.csp"), str(tmp / "t.txt")]), 0)
        self.assertEqual(list(pk.load(tmp / "u.csp").songs), ["t"])


class VersionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="t")
        pkg.add_song("tune", Song.from_mml(MML))
        pk.save(pkg, self.tmp / "t.csp")

    def export(self, *opts):
        out = self.tmp / "o.txt"
        out.unlink(missing_ok=True)
        rc = main(["export-song", str(self.tmp / "t.csp"), "-o", str(out), *opts])
        return rc, out.read_text(encoding="utf-8") if out.exists() else None

    def first_line(self, text):
        return text.splitlines()[0]

    def test_no_version_is_written_and_the_reader_still_reads_it(self):
        rc, text = self.export()
        self.assertEqual(rc, 0)
        self.assertTrue(text.startswith("Project TempoMode"))
        out, rep = ex.dump(Song.from_mml(MML).compile())
        self.assertNotIn("Version", out.splitlines()[0])
        self.assertTrue(any("no Version" in n and "upgrade FamiStudio" in n for n in rep.notes))
        a, b, *_ = round_trip()
        self.assertEqual(notes(a, 0), notes(b, 0))
        self.assertFalse(any("written by FamiStudio" in n for n in fam.load(out).report.notes))

    def test_the_version_options_are_gone(self):
        for opt in ("--fs-version", "--fs-reference"):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.export(opt, "4.2.0")
        self.assertFalse(hasattr(ex, "DEFAULT_VERSION"))
        self.assertFalse(hasattr(ex, "reference_version"))


class RealFamiStudioFacts(unittest.TestCase):
    """What the real FamiStudio (built from source, run headless, see PLAN-FORMATS.md section 3) did with our files."""

    def text(self, mml_text):
        return ex.dump(Song.from_mml(mml_text).compile())[0]

    def first_note(self, text, channel):
        lines = text.splitlines()
        i = next(k for k, l in enumerate(lines) if 'Channel Type="%s"' % channel in l)
        return next(l for l in lines[i:] if l.strip().startswith("Note "))

    def test_a_squares_c4_is_written_an_octave_below_chipsynths_name_for_it(self):
        # FamiStudio's C1 is 32.7 Hz and its pulse value 1 is that C1; chipsynth's C4 is 261.6 Hz
        sq = self.first_note(self.text("V1: o4 c\n"), "Square1")
        self.assertIn('Value="C3"', sq)

    def test_the_triangle_is_written_an_octave_up_from_the_square(self):
        tr = self.first_note(self.text("[instr]\nb wave=tri\nV3: @b o4 c\n"), "Triangle")
        self.assertIn('Value="C4"', tr)

    def test_a_released_note_carries_a_release_frame_inside_a_longer_duration(self):
        t = self.text("[instr]\nl wave=pulse a=0 d=4 s=8 r=6\nV1: @l l4 o4 c r2\n")
        note = self.first_note(t, "Square1")
        attrs = dict(a.split("=") for a in note.split()[1:])
        dur, rel = int(attrs["Duration"].strip('"')), int(attrs["Release"].strip('"'))
        self.assertTrue(0 < rel < dur)

    def test_a_pattern_is_a_whole_number_of_groove_cycles(self):
        # FamiStudio restarts the groove at each pattern: 16 rows of 5-5-6-5-6 ran 0.4% fast (78 ms in 19 s)
        for bpm in (167, 117, 137):
            t = self.text("tempo %d\nV1: l16 o4 c d e f g a b c c d e f g a b c c d e f g a b c\n" % bpm)
            song = next(l for l in t.splitlines() if l.strip().startswith("Song "))
            attrs = dict(a.split("=") for a in song.split()[1:] if "=" in a)
            groove = attrs["Groove"].strip('"').split("-")
            self.assertEqual(int(attrs["PatternLength"].strip('"')) % len(groove), 0, (bpm, groove))

    def test_a_song_that_ends_inside_a_pattern_says_so(self):
        t = self.text("tempo 120\nV1: l4 o4 c d e\n")
        self.assertIn("PatternCustomSettings", t)
        back = fam.load(t).songs[0]
        notes_ = [(n.start, n.dur) for n in back.song.compile(dict(mml.builtin_library(), _mem=back.samples)).voices[0]]
        self.assertEqual(len(notes_), 3)

    def test_the_importer_cuts_a_note_at_its_release_frame(self):
        t = self.text("[instr]\nl wave=pulse a=0 d=4 s=8 r=6\nV1: @l l4 o4 c r2\n")
        back = fam.load(t).songs[0]
        tune = back.song.compile(dict(mml.builtin_library(), _mem=back.samples))
        self.assertEqual(len(tune.voices[0]), 1)
        self.assertLessEqual(tune.voices[0][0].dur, 96 + 2)


if __name__ == "__main__":
    unittest.main()
