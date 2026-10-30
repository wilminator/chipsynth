# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""FamiStudio text import: fixtures are written by hand from the format (tests/data/famistudio), never real songs."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from chipsynth import package as pk
from chipsynth.formats import envelope as env, famistudio_text as fam
from chipsynth.__main__ import main

DATA = Path(__file__).parent / "data" / "famistudio"
TINY = (DATA / "tiny.txt").read_text()


def notes(tune, v):
    return [(n.start, n.dur, n.pitch) for n in tune.voices[v]]


def load_tiny(**kw):
    r = fam.load(TINY, **kw)
    return r, r.songs[0]


class ParseTests(unittest.TestCase):
    def test_a_quote_inside_a_value_is_doubled(self):
        kw, attrs = fam.split_line('Project Name="A ""quoted"" name" Author="me"')
        self.assertEqual(kw, "Project")
        self.assertEqual(attrs, {"Name": 'A "quoted" name', "Author": "me"})

    def test_a_line_that_is_not_attributes_is_ignored(self):
        self.assertIsNone(fam.split_line('Project Name="x" and some words'))
        self.assertIsNone(fam.split_line("   "))

    def test_it_knows_a_famistudio_file_from_other_text(self):
        self.assertTrue(fam.looks_like(TINY))
        self.assertTrue(fam.looks_like("\ufeff\n" + TINY))
        self.assertFalse(fam.looks_like("hello\nworld\n"))

    def test_note_names(self):
        self.assertEqual(fam.note_value("C0"), 1)
        self.assertEqual(fam.note_value("A4"), 58)
        self.assertEqual(fam.note_value("Stop"), 0)
        self.assertEqual(fam.note_value("Release"), 0x80)
        with self.assertRaises(fam.FamiStudioError):
            fam.note_value("H4")

    def test_a_non_famistudio_file_and_empty_projects_are_refused(self):
        with self.assertRaises(fam.FamiStudioError):
            fam.load("just some text\n")
        with self.assertRaises(fam.FamiStudioError):
            fam.load('Project Name="x"\n')

    def test_bad_sample_data_is_an_error_not_a_crash(self):
        with self.assertRaises(fam.FamiStudioError):
            fam.load('Project Name="x"\n\tDPCMSample Name="s" Data="zz"\n')


class SongTests(unittest.TestCase):
    def setUp(self):
        self.result, self.imp = load_tiny()
        self.song = self.imp.song
        self.tune = self.song.compile(dict(__import__("chipsynth.mml", fromlist=["x"]).builtin_library(),
                                           _mem=self.imp.samples))

    def test_title_author_and_the_channels_land_in_the_right_voices(self):
        self.assertEqual((self.song.title, self.song.author), ("Main Theme", "Test Author"))
        self.assertEqual((self.song.voices, self.song.dacs), (4, 1))
        self.assertTrue(notes(self.tune, 0))
        self.assertFalse(notes(self.tune, 1))
        self.assertTrue(notes(self.tune, 2))
        self.assertTrue(notes(self.tune, 3))

    def test_c4_is_chipsynths_c4_and_times_are_rows(self):
        n = notes(self.tune, 0)
        self.assertEqual(n[0], (0, 48, 48))                  # C4 for two rows of 24 ticks
        self.assertEqual(n[1][2], 52)                         # E4
        self.assertEqual(n[1][0], 48)

    def test_tempo_is_what_the_rows_last_on_the_nes(self):
        # 4 rows a beat, 10 frames a row at 60.0988 Hz: 40 frames = 0.6655 s = 90.15 bpm
        self.assertEqual(self.song.bpm, 90)

    def test_a_bar_is_a_pattern_and_the_loop_point_is_its_start(self):
        self.assertEqual(self.song.time, (2, 4))
        self.assertEqual(self.tune.loop_tick, 192)
        self.assertEqual(self.tune.length, 384)

    def test_a_stop_leaves_a_rest(self):
        n = notes(self.tune, 0)
        self.assertEqual(n[2][0], 120)                        # the arpeggio note after the stop (5 rows in)

    def test_volume_effect_and_instrument_changes_come_through(self):
        from chipsynth import song as sg
        v1 = self.song.tracks["V1"]
        self.assertIn(sg.Volume(value=9), v1)
        self.assertIn(sg.Select(name="Arp"), v1)

    def test_instruments_get_their_duty_wave_and_arpeggio(self):
        lead, arp = self.song.instruments["Lead"], self.song.instruments["Arp"]
        self.assertEqual((lead.wave, lead.pw), ("pulse", 25))
        self.assertEqual(arp.arp, (0, 4, 7))
        self.assertEqual(self.song.instruments["Lead_triangle"].wave, "tri")
        self.assertEqual(self.song.instruments["Lead_noise"].wave, "noise")
        self.assertEqual(self.song.instruments["Lead_noise"].nmode, "short")      # duty 1 on the noise channel

    def test_the_dpcm_sample_and_its_rate(self):
        smp = self.imp.samples["Kick_Drum"]
        self.assertEqual(smp.tracks, [[bytes.fromhex("aaaaaa55aaaa55aa")]])
        self.assertEqual(smp.rate, 12)

    def test_the_report_names_what_was_left_out(self):
        text = "\n".join(self.result.report.lines())
        self.assertIn("VRC6Saw", text)
        self.assertIn("volume envelope", text)

    def test_the_song_survives_mml_and_the_package(self):
        from chipsynth import mml
        lib = dict(mml.builtin_library(), _mem=self.imp.samples)
        pkg = pk.Package(name="t")
        pkg.add_song("tiny", self.imp.song, lib)
        again = pk.loads(pk.dumps(pkg))
        t = again.tune("tiny")
        self.assertEqual(notes(t, 0)[0], (0, 48, 48))
        self.assertIn("Kick_Drum", again.samples)


class TimingTests(unittest.TestCase):
    def test_famitracker_tempo_mode_uses_speed_and_tempo(self):
        text = ('Project TempoMode="FamiTracker"\n'
                '\tSong Name="s" Length="1" LoopPoint="-1" PatternLength="16" BeatLength="4" FamiTrackerTempo="150" FamiTrackerSpeed="6"\n'
                '\t\tChannel Type="Square1"\n\t\t\tPattern Name="p"\n\t\t\t\tNote Time="0" Value="A4" Duration="4"\n'
                '\t\t\tPatternInstance Time="0" Pattern="p"\n')
        song = fam.load(text).songs[0].song
        self.assertEqual(song.bpm, 150)                       # 6 frames a row at 60 Hz: a beat is 24 frames, 0.4 s
        from chipsynth import song as sg
        first = next(e for e in song.tracks["V1"] if isinstance(e, sg.Note))
        self.assertEqual(first.dur, 4 * 24)                   # four rows of 24 ticks

    def test_pal_changes_the_tempo(self):
        ntsc = fam.load(TINY).songs[0].song.bpm
        pal = fam.load(TINY, region="pal").songs[0].song.bpm
        self.assertLess(pal, ntsc)

    def test_a_groove_is_averaged_and_reported(self):
        text = TINY.replace('NoteLength="10" Groove="10"', 'NoteLength="5" Groove="6-5"')
        r = fam.load(text)
        self.assertTrue(any("groove" in m for m in r.report.approximated))

    def test_custom_pattern_length_moves_the_next_pattern(self):
        text = TINY.replace('\t\tChannel Type="Square1"', '\t\tPatternCustomSettings Time="0" Length="4" NoteLength="10" Groove="10" GroovePaddingMode="Beginning" BeatLength="4"\n\t\tChannel Type="Square1"', 1)
        song = fam.load(text).songs[0].song
        from chipsynth import mml
        t = song.compile(dict(mml.builtin_library(), _mem=fam.load(text).songs[0].samples))
        self.assertEqual(t.length, (4 + 8) * 24)

    def test_song_choice(self):
        with self.assertRaises(fam.FamiStudioError):
            fam.load(TINY, song="Nope")
        self.assertEqual(len(fam.load(TINY, song="1").songs), 1)
        self.assertEqual(len(fam.load(TINY, song="Main Theme").songs), 1)


class NoiseTests(unittest.TestCase):
    def test_a_higher_noise_note_is_a_higher_noise_pitch_and_all_are_playable(self):
        ps = [fam.noise_pitch(k) for k in range(16)]
        self.assertEqual(ps, sorted(ps))
        self.assertEqual(len(set(ps)), 16)
        self.assertTrue(all(0 <= p <= 107 for p in ps))


class EnvelopeTests(unittest.TestCase):
    def test_resolve_loops_and_holds(self):
        self.assertEqual(env.resolve([5, 4, 3], 1, -1, 3, 7), [5, 4, 3, 4, 3, 4, 3])
        self.assertEqual(env.resolve([5, 4, 3], -1, -1, 3, 6), [5, 4, 3, 3, 3, 3])

    def test_release_waits_for_the_release_point(self):
        held = env.resolve([15, 10, 8, 4, 0], 2, 3, 5, 8)
        self.assertEqual(held, [15, 10, 8, 8, 8, 8, 8, 8])
        freed = env.resolve([15, 10, 8, 4, 0], 2, 3, 5, 6, released_at=4)
        self.assertEqual(freed[:6], [15, 10, 8, 8, 8, 4])

    def test_a_decay_made_from_an_adsr_is_fitted_back_closely(self):
        t = (np.arange(120) + 0.5) / env.FRAME_HZ
        vals = [int(round(15 * v)) for v in env.adsr(0, 9, 8, 0, None, t)]
        fields, err = env.fit_volume(vals, len(vals) - 1, -1, len(vals))
        self.assertLess(err, 0.04)
        self.assertGreaterEqual(fields["vol"], 14)
        self.assertAlmostEqual(fields["s"], 8, delta=1)

    def test_a_flat_envelope_is_a_flat_instrument(self):
        fields, err = env.fit_volume([15], 0, -1, 1)
        self.assertLess(err, 0.02)
        self.assertEqual(fields["s"], 15)

    def test_a_repeating_arpeggio_is_exact(self):
        arp, rate, exact = env.fit_arpeggio([0, 4, 7], 0, 40)
        self.assertEqual((arp, exact), ((0, 4, 7), True))
        self.assertEqual(rate, 40)

    def test_a_one_shot_arpeggio_is_reported_inexact(self):
        _, _, exact = env.fit_arpeggio([12, 0, 0, 0], -1, 40)
        self.assertFalse(exact)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def run_cli(self, *args):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main([str(a) for a in args])
            except SystemExit as e:
                rc = e.code
            except pk.PackageError as e:
                rc = 1
                err.write(str(e))
        return rc, out.getvalue(), err.getvalue()

    def test_import_adds_the_song_its_samples_and_prints_the_report(self):
        src = self.tmp / "My Tiny (v2).txt"
        src.write_text(TINY)
        path = self.tmp / "m.csp"
        rc, out, err = self.run_cli("import", path, src, "--report", self.tmp / "r.json")
        self.assertEqual(rc, 0, err)
        pkg = pk.load(path)
        self.assertIn("My_Tiny__v2_", pkg.songs)
        self.assertIn("Kick_Drum", pkg.samples)
        self.assertIn("left out: channel VRC6Saw", out)
        self.assertIn("dropped", json.loads((self.tmp / "r.json").read_text()))

    def test_several_songs_get_their_names_in_the_id(self):
        two = TINY + TINY[TINY.index("\tSong "):].replace("Main Theme", "Second Song")
        src = self.tmp / "game.txt"
        src.write_text(two)
        path = self.tmp / "g.csp"
        rc, out, err = self.run_cli("import", path, src)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(pk.load(path).songs), ["game_Main_Theme", "game_Second_Song"])
        rc, out, err = self.run_cli("import", self.tmp / "h.csp", src, "--song", "2", "--id", "second")
        self.assertEqual(rc, 0, err)
        self.assertEqual(list(pk.load(self.tmp / "h.csp").songs), ["second"])

    def test_a_text_file_that_is_not_famistudio_is_refused(self):
        src = self.tmp / "notes.txt"
        src.write_text("buy milk\n")
        rc, out, err = self.run_cli("import", self.tmp / "x.csp", src)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
