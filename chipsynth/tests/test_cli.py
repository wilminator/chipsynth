# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The command line: import / list / export / export-song (components.py, exporter.py, __main__.py)."""
import contextlib
import io
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from chipsynth import __main__ as cli, mml, musicgen, package as pk, packagetext
from chipsynth.exporter import AudioOptions, ExportError, decibels, frames, gain, render_audio, split_source
from chipsynth.song import Song
from chipsynth.synth import RATE

DATA = Path(__file__).resolve().parent / "data" / "demo_songs"
LOOPING = "tempo 120\nV1: l8 c d e f loop g a b >c<\n"
ONCE = "tempo 120\nonce\nV1: l8 c d e f\n"


def run(*argv):
    """(exit code, stdout, stderr) of `python -m chipsynth ARGV`."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main([str(a) for a in argv])
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def read_wav(path):
    with wave.open(str(path), "rb") as f:
        assert (f.getnchannels(), f.getsampwidth(), f.getframerate()) == (2, 2, RATE)
        return np.frombuffer(f.readframes(f.getnframes()), "<i2").reshape(-1, 2).astype(np.float64) / 32767


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def mml(self, name, text):
        p = self.tmp / (name + ".mml")
        p.write_text(text)
        return p

    def package(self, **songs):
        """A package file of the given {id: MML text}."""
        files = [self.mml(k, v) for k, v in songs.items()]
        path = self.tmp / "p.csp"
        rc, out, err = run("import", path, *files)
        self.assertEqual((rc, err), (0, ""), out)
        return path


class ImportTests(Base):
    def test_creates_a_package_and_adds_to_an_existing_one(self):
        path = self.tmp / "p.csp"
        self.assertEqual(run("import", path, DATA / "town.mml")[0], 0)
        self.assertEqual(list(pk.load(path).songs), ["town"])
        rc, out, _ = run("import", path, DATA / "boss.mml", self.mml("tiny", ONCE))
        self.assertEqual(rc, 0)
        self.assertEqual(list(pk.load(path).songs), ["town", "boss", "tiny"])
        self.assertIn("p.csp: 3 song(s)", out)

    def test_the_song_id_defaults_to_the_file_name_and_id_overrides_it(self):
        path = self.tmp / "p.csp"
        run("import", path, DATA / "town.mml", "--id", "village")
        self.assertEqual(list(pk.load(path).songs), ["village"])
        rc, _, err = run("import", path, DATA / "town.mml", DATA / "boss.mml", "--id", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--id needs exactly one file", err)

    def test_a_song_id_already_there_is_replaced_unless_no_replace(self):
        path = self.package(a=ONCE)
        rc, out, _ = run("import", path, self.mml("a", LOOPING))
        self.assertIn("replaced song 'a'", out)
        self.assertFalse(pk.load(path).songs["a"].once)
        before = path.read_bytes()
        rc, _, err = run("import", path, self.mml("a", ONCE), "--no-replace")
        self.assertEqual(rc, 1)
        self.assertIn("already has a song 'a'", err)
        self.assertEqual(path.read_bytes(), before)

    def test_a_mistake_in_the_mml_is_an_error_and_nothing_is_written(self):
        path = self.tmp / "p.csp"
        rc, _, err = run("import", path, self.mml("bad", "V1: c d e @nosuchinstrument"))
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error:"), err)
        self.assertFalse(path.exists())

    def test_shared_adds_a_library_files_parts_to_the_package(self):
        lib = self.mml("lib", "[instr]\nzap wave=saw vol=9\n[fx]\nwob vib=4,2\n[key]\nodd = 0 1 5 7\n")
        path = self.package(a=ONCE)
        rc, _, _ = run("import", path, lib, "--shared")
        self.assertEqual(rc, 0)
        pkg = pk.load(path)
        self.assertEqual((list(pkg.instruments), list(pkg.fx), list(pkg.keys)), (["zap"], ["wob"], ["odd"]))
        self.assertEqual(list(pkg.songs), ["a"])


class ListTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.package(town=(DATA / "town.mml").read_text(), boss=(DATA / "boss.mml").read_text())

    def test_lists_every_type_by_default(self):
        rc, out, _ = run("list", self.path)
        self.assertEqual(rc, 0)
        self.assertIn("song       p:town", out)
        self.assertIn("song       p:boss", out)
        self.assertIn("instrument buzz", out)

    def test_type_filters_and_takes_commas_aliases_and_repeats(self):
        _, out, _ = run("list", self.path, "--type", "song")
        self.assertEqual([l.split()[0] for l in out.splitlines()[1:]], ["song", "song"])
        _, out, _ = run("list", self.path, "--type", "instr")
        self.assertTrue(all(l.split()[0] == "instrument" for l in out.splitlines()[1:]))
        _, out, _ = run("list", self.path, "--type", "song", "--type", "fx")
        self.assertEqual({l.split()[0] for l in out.splitlines()[1:]}, {"song"})      # these songs have no fx sets
        rc, _, err = run("list", self.path, "--type", "banana")
        self.assertEqual(rc, 1)
        self.assertIn("unknown component type 'banana'", err)

    def test_name_is_a_glob_and_may_name_a_type(self):
        _, out, _ = run("list", self.path, "--name", "bo*")
        self.assertIn("p:boss", out)
        self.assertNotIn("p:town", out)
        _, out, _ = run("list", self.path, "--name", "song:b*")
        self.assertEqual([l.split()[1] for l in out.splitlines()[1:]], ["p:boss"])
        _, out, _ = run("list", self.path, "--name", "zzz*")
        self.assertIn("(nothing matches)", out)

    def test_samples_are_listed_with_their_rate(self):
        gen, path = self.tmp / "dr.mml", self.tmp / "d.csp"
        run("generate", "town", "--seed", 3, "--dac", "-o", gen, "--samples", self.tmp / "gs")
        self.assertEqual(run("import", path, gen, "--samples", self.tmp / "gs")[0], 0)
        _, out, _ = run("list", path, "--type", "sample")
        self.assertRegex(out, r"sample +kit_kick +shared +rate \d+, \d+ byte\(s\), mono")

    def test_a_folder_and_a_loose_mml_file_are_listed_too(self):
        _, out, _ = run("list", self.tmp, "--type", "song")
        self.assertIn("p:town", out)
        self.assertIn("town:town", out)             # the loose town.mml


class ExportTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.package(town=(DATA / "town.mml").read_text(), boss=(DATA / "boss.mml").read_text())

    def test_every_song_by_default_one_file_each(self):
        rc, out, _ = run("export", self.path, "-d", self.tmp / "o")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in (self.tmp / "o").iterdir()), ["boss.mml", "town.mml"])

    def test_type_and_name_choose_what_is_written(self):
        run("export", self.path, "-d", self.tmp / "o", "--name", "town")
        self.assertEqual([p.name for p in (self.tmp / "o").iterdir()], ["town.mml"])
        rc, _, err = run("export", self.path, "-d", self.tmp / "o2", "--name", "nothing")
        self.assertEqual(rc, 1)
        self.assertIn("nothing matches", err)

    def test_instruments_go_in_one_library_file_that_reads_back(self):
        rc, out, _ = run("export", self.path, "-d", self.tmp / "o", "--type", "instrument", "--name", "buzz", "--name", "bass")
        self.assertEqual(rc, 0)
        lib = self.tmp / "o" / "lib" / "p.mml"
        self.assertEqual([p.name for p in (self.tmp / "o").rglob("*") if p.is_file()], ["p.mml"])
        mml.parse(lib.read_text())
        path = self.tmp / "q.csp"
        run("import", path, self.mml("a", ONCE))
        self.assertEqual(run("import", path, lib, "--shared")[0], 0)
        self.assertEqual(sorted(pk.load(path).instruments), ["bass", "buzz"])

    def test_dash_o_writes_one_thing_to_a_file_or_stdout(self):
        rc, out, _ = run("export", self.path, "--name", "town", "-o", "-")
        self.assertEqual(rc, 0)
        self.assertIn("V1:", out)
        mml.parse(out)
        run("export", self.path, "--name", "boss", "-o", self.tmp / "b.mml")
        self.assertIn("title Boss", (self.tmp / "b.mml").read_text())
        rc, _, err = run("export", self.path, "-o", self.tmp / "both.mml")
        self.assertEqual(rc, 1)
        self.assertIn("-o writes one thing", err)

    def test_midi_flag_writes_mid_files(self):
        run("export", self.path, "-d", self.tmp / "o", "--midi", "--name", "town")
        self.assertEqual((self.tmp / "o" / "town.mid").read_bytes()[:4], b"MThd")

    def test_a_song_with_samples_comes_back_the_same_through_mml(self):
        gen = self.tmp / "dr.mml"
        run("generate", "town", "--seed", 3, "--dac", "-o", gen, "--samples", self.tmp / "gs")
        a, b = self.tmp / "a.csp", self.tmp / "b.csp"
        run("import", a, gen, "--samples", self.tmp / "gs")
        self.assertEqual(run("export", a, "-d", self.tmp / "o", "--type", "song")[0], 0)
        self.assertTrue(list((self.tmp / "o" / "samples").glob("kit_kick.dpcm")))
        self.assertEqual(run("import", b, self.tmp / "o" / "dr.mml", "--samples", self.tmp / "o" / "samples")[0], 0)
        same = lambda p: packagetext.dump_text(pk.load(p)).replace("name: " + p.stem, "")
        self.assertEqual(same(a), same(b))

    def test_exporting_the_songs_keeps_their_dpcm_samples_beside_them(self):
        gen = self.tmp / "dr.mml"
        run("generate", "town", "--seed", 3, "--dac", "-o", gen, "--samples", self.tmp / "gs")
        a = self.tmp / "a.csp"
        run("import", a, gen, "--samples", self.tmp / "gs")
        rc, out, _ = run("export-song", a, "-o", self.tmp / "x" / "song.mml")
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / "x" / "samples" / "kit_kick.dpcm").is_file())


class TimeAndLevelTests(unittest.TestCase):
    def test_times(self):
        self.assertEqual(frames("1"), RATE)
        self.assertEqual(frames("1.5s"), int(1.5 * RATE))
        self.assertEqual(frames("250ms"), RATE // 4)
        self.assertEqual(frames("1:05.5"), int(65.5 * RATE))
        self.assertEqual(frames("88200f"), 88200)
        self.assertEqual(frames("10frames"), 10)
        for bad in ("abc", "-1", "1.5f", "x:y", ""):
            with self.assertRaises(ExportError, msg=bad):
                frames(bad)

    def test_volume_and_peak(self):
        self.assertAlmostEqual(gain("0.5"), 0.5)
        self.assertAlmostEqual(gain("50%"), 0.5)
        self.assertAlmostEqual(gain("-6dB"), 0.5012, places=3)
        self.assertAlmostEqual(gain("0dB"), 1.0)
        self.assertEqual(decibels("-3"), -3.0)
        self.assertEqual(decibels("-3dB"), -3.0)
        for bad in ("loud", "-1"):
            with self.assertRaises(ExportError, msg=bad):
                gain(bad)
        with self.assertRaises(ExportError):
            decibels("2")

    def test_split_source(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.csp"
            f.write_text("")
            self.assertEqual(split_source(str(f) + ":dawn"), (str(f), "dawn"))
            self.assertEqual(split_source(str(f)), (str(f), None))
            self.assertEqual(split_source("nofile.csp:dawn"), ("nofile.csp:dawn", None))


def has_soundfile():
    try:
        import soundfile  # noqa: F401
        return True
    except ImportError:
        return False


def decode(path):
    """Stereo float samples of an mp3 / ogg file (needs soundfile)."""
    import soundfile
    x, rate = soundfile.read(str(path))
    assert rate == RATE
    return x


class EncodeTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.package(loop=LOOPING, once=ONCE)

    @unittest.skipUnless(has_soundfile(), "soundfile is not installed")
    def test_mp3_and_ogg_hold_the_same_audio_as_the_wav(self):
        out = {}
        for fmt in ("wav", "mp3", "ogg"):
            rc, text, err = run("export-song", self.path, "once", "-o", self.tmp / ("o." + fmt), "--end", "1")
            self.assertEqual((rc, err), (0, ""), text)
            self.assertIn("(1.00s, peak", text)
            out[fmt] = read_wav(self.tmp / "o.wav") if fmt == "wav" else decode(self.tmp / ("o." + fmt))
        for fmt in ("mp3", "ogg"):
            self.assertGreater((self.tmp / ("o." + fmt)).stat().st_size, 1000)
            self.assertLess((self.tmp / ("o." + fmt)).stat().st_size, (self.tmp / "o.wav").stat().st_size / 4)
            # lossy, so compare the envelope: the same loudness in every 50 ms window, within 2 dB
            w, m = out["wav"], out[fmt][:len(out["wav"])]
            n = RATE // 20
            for k in range(0, len(w) - n, n):
                a, b = np.sqrt((w[k:k + n] ** 2).mean()), np.sqrt((m[k:k + n] ** 2).mean())
                if a > 0.01:
                    self.assertLess(abs(20 * np.log10(b / a)), 2.0, (fmt, k))

    def test_without_soundfile_the_error_says_what_to_install(self):
        import sys
        from unittest import mock
        with mock.patch.dict(sys.modules, {"soundfile": None}):
            rc, _, err = run("export-song", self.path, "once", "-o", self.tmp / "o.mp3")
        self.assertEqual(rc, 1)
        self.assertIn("soundfile", err)
        self.assertFalse((self.tmp / "o.mp3").exists())

    def test_format_option_overrides_the_ending(self):
        rc, _, err = run("export-song", self.path, "once", "-o", self.tmp / "o.bin", "-f", "wav")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual((self.tmp / "o.bin").read_bytes()[:4], b"RIFF")


class TrimTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.package(once=ONCE)

    def test_trim_cuts_the_silence_at_the_end(self):
        whole = self.wav()
        trimmed = self.wav("--trim")
        self.assertLess(len(trimmed), len(whole) - RATE)                 # the 2 s tail is mostly silence
        self.assertGreater(len(trimmed), RATE)                           # the four notes themselves stay
        np.testing.assert_array_equal(trimmed, whole[:len(trimmed)])     # a cut, nothing else
        pad = int(0.02 * RATE)
        self.assertTrue((np.abs(whole[len(trimmed):]) <= np.abs(whole).max() * 10 ** (-60 / 20)).all())   # only silence went
        self.assertGreater(np.abs(trimmed[:-pad]).max(), np.abs(whole).max() * 10 ** (-60 / 20))         # up to the last sound

    def test_trim_level_is_relative_to_the_peak(self):
        quiet = self.wav("--trim", "--volume", "0.1")
        loud = self.wav("--trim")
        self.assertEqual(len(quiet), len(loud))

    def test_trim_takes_a_level(self):
        self.assertLessEqual(len(self.wav("--trim", "-20")), len(self.wav("--trim", "-80")))

    def wav(self, *opts):
        out = self.tmp / "o.wav"
        rc, text, err = run("export-song", self.path, "once", "-o", out, *opts)
        self.assertEqual((rc, err), (0, ""), text)
        return read_wav(out)


class SfxTests(Base):
    def setUp(self):
        super().setUp()
        self.mml("blip", "tempo 240\nonce\nV1: l32 o5 c e\n")
        self.mml("thud", "tempo 240\nonce\n[instr]\nn wave=noise d=4 s=0 r=3\nV4: @n l16 o3 c\n")
        pkg = pk.Package(name="p")
        pkg.add_song("a", Song.from_mml(ONCE))
        pkg.add_song("b", Song.from_mml(LOOPING))
        pk.save(pkg, self.tmp / "p.csp")

    def test_a_folder_renders_every_cue_trimmed_and_normalized(self):
        rc, out, err = run("sfx", self.tmp, "-d", self.tmp / "cues")
        self.assertEqual((rc, err), (0, ""), out)
        names = sorted(p.name for p in (self.tmp / "cues").iterdir())
        self.assertEqual(names, ["a.wav", "b.wav", "blip.wav", "thud.wav"])     # .mml by file name, .csp by song id
        self.assertIn("4 effect(s)", out)
        blip = read_wav(self.tmp / "cues" / "blip.wav")
        self.assertLess(len(blip), RATE)                                       # trimmed: two 32nd notes and a short ring
        self.assertAlmostEqual(20 * np.log10(np.abs(blip).max()), -1.0, places=1)
        b = read_wav(self.tmp / "cues" / "b.wav")
        self.assertLess(len(b), 6 * RATE)                                      # a looping song is still rendered once

    def test_one_file_and_the_options(self):
        rc, out, _ = run("sfx", self.tmp / "blip.mml", "-d", self.tmp / "c2", "--no-normalize", "--trim", "off", "--tail", "0.5")
        self.assertEqual(rc, 0, out)
        blip = read_wav(self.tmp / "c2" / "blip.wav")
        self.assertLess(20 * np.log10(np.abs(blip).max()), -1.5)               # not normalized
        from chipsynth.player import TunePlayer
        tune = mml.parse((self.tmp / "blip.mml").read_text())
        self.assertEqual(len(blip), TunePlayer(tune).frames(0, 0.5))          # not trimmed: the notes and the 0.5 s tail
        rc, _, err = run("sfx", self.tmp / "nothing", "-d", self.tmp / "c3")
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)

    def test_two_cues_with_one_name_are_refused_before_anything_is_written(self):
        self.mml("a", ONCE)                                    # a.mml beside p.csp, which holds a song 'a' too
        rc, _, err = run("sfx", self.tmp, "-d", self.tmp / "c5")
        self.assertEqual(rc, 1)
        self.assertIn("two cues would be written as a.wav", err)
        self.assertFalse((self.tmp / "c5").exists())

    @unittest.skipUnless(has_soundfile(), "soundfile is not installed")
    def test_mp3_output(self):
        rc, out, _ = run("sfx", self.tmp / "blip.mml", "-d", self.tmp / "c4", "-f", "mp3")
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.tmp / "c4" / "blip.mp3").is_file())


class ExportSongTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.package(loop=LOOPING, once=ONCE, town=(DATA / "town.mml").read_text())

    def wav(self, *opts, song="loop"):
        out = self.tmp / "o.wav"
        rc, text, err = run("export-song", self.path, song, "-o", out, *opts)
        self.assertEqual((rc, err), (0, ""), text)
        return read_wav(out)

    def test_defaults_match_render_wav(self):
        from chipsynth.player import render
        tune = pk.load(self.path).tune("loop")
        self.assertEqual(len(self.wav()), len(render(tune)))

    def test_start_and_end_in_seconds_and_in_frames(self):
        self.assertEqual(len(self.wav("--end", "1")), RATE)
        self.assertEqual(len(self.wav("--end", "44100f")), RATE)
        self.assertEqual(len(self.wav("--start", "250ms", "--end", "1.25")), RATE)
        self.assertEqual(len(self.wav("--start", "1000f", "--end", "3000f")), 2000)

    def test_a_cut_is_the_same_samples_as_the_whole(self):
        whole = self.wav("--end", "2")
        cut = self.wav("--start", "0.5", "--end", "1.5")
        np.testing.assert_allclose(cut, whole[RATE // 2:RATE + RATE // 2], atol=1 / 32767)

    def test_start_must_be_before_end_and_inside_the_audio(self):
        for opts in (("--start", "2", "--end", "1"), ("--start", "1", "--end", "1"), ("--start", "999")):
            rc, _, err = run("export-song", self.path, "loop", "-o", self.tmp / "o.wav", *opts)
            self.assertEqual(rc, 1, opts)
            self.assertIn("error:", err)

    def test_loops_lengthen_a_looping_song_by_the_loop_section(self):
        n0, n1, n2 = (len(self.wav("--loops", str(k))) for k in (0, 1, 2))
        seg = n1 - n0
        self.assertGreater(seg, RATE // 2)
        self.assertEqual(n2 - n1, seg)

    def test_loops_on_a_song_that_plays_once_say_so(self):
        out = self.tmp / "o.wav"
        rc, _, err = run("export-song", self.path, "once", "-o", out, "--loops", "3")
        self.assertEqual(rc, 0)
        self.assertIn("marked once", err)
        self.assertEqual(len(read_wav(out)), len(self.wav(song="once")))

    def test_tail_sets_the_ring_out(self):
        self.assertEqual(len(self.wav("--tail", "0")) + 2 * RATE, len(self.wav()))

    def test_volume_scales_the_signal(self):
        full, half = self.wav("--end", "1"), self.wav("--end", "1", "--volume", "50%")
        np.testing.assert_allclose(half, full * 0.5, atol=2 / 32767)
        quiet = self.wav("--end", "1", "--volume", "-6dB")
        self.assertAlmostEqual(np.abs(quiet).max() / np.abs(full).max(), gain("-6dB"), places=2)
        self.assertEqual(np.abs(self.wav("--end", "1", "--volume", "0")).max(), 0)

    def test_normalize_aims_for_the_peak_and_volume_comes_after_it(self):
        peak = lambda x: 20 * np.log10(np.abs(x).max())
        self.assertAlmostEqual(peak(self.wav("--end", "2", "--normalize")), -1.0, places=1)
        self.assertAlmostEqual(peak(self.wav("--end", "2", "--normalize", "--peak", "-6")), -6.0, places=1)
        self.assertAlmostEqual(peak(self.wav("--end", "2", "--normalize", "--peak=-6dB", "--volume", "-3dB")), -9.0, places=1)

    def test_normalize_covers_only_the_part_kept(self):
        tune = pk.load(self.path).tune("town")
        a, *_ = render_audio(tune, AudioOptions(normalize=True, peak_db=-2, start=0, end=RATE))
        b, *_ = render_audio(tune, AudioOptions(normalize=True, peak_db=-2, start=5 * RATE, end=6 * RATE))
        for x in (a, b):
            self.assertAlmostEqual(20 * np.log10(np.abs(x).max()), -2.0, places=3)

    def test_too_loud_is_clipped_and_reported(self):
        out = self.tmp / "o.wav"
        rc, text, err = run("export-song", self.path, "loop", "-o", out, "--volume", "20", "--end", "1")
        self.assertEqual(rc, 0)
        self.assertIn("clipped", err)
        self.assertIn("peak 0.0 dBFS", text)
        self.assertEqual(np.abs(read_wav(out)).max(), 1.0)

    def test_silence_cannot_be_normalized_but_is_written(self):
        tune = mml.parse("tempo 120\nV1: r1\n")
        audio, peak, clipped, notes = render_audio(tune, AudioOptions(normalize=True))
        self.assertIsNone(peak)
        self.assertIn("silent", notes[0])
        self.assertEqual(np.abs(audio).max(), 0)

    def test_other_formats_from_the_ending_or_format(self):
        for name, magic in (("o.mid", b"MThd"), ("o.csp", b"CSPK")):
            self.assertEqual(run("export-song", self.path, "town", "-o", self.tmp / name)[0], 0)
            self.assertEqual((self.tmp / name).read_bytes()[:4], magic)
        one = pk.load(self.tmp / "o.csp")
        self.assertEqual(list(one.songs), ["town"])
        self.assertEqual(one.tune("town").length, pk.load(self.path).tune("town").length)
        self.assertEqual(run("export-song", self.path, "town", "-o", self.tmp / "song.dat", "-f", "mml")[0], 0)
        mml.parse((self.tmp / "song.dat").read_text())

    def test_audio_options_are_refused_for_other_formats(self):
        for opt in (("--volume", "0.5"), ("--normalize",), ("--start", "1"), ("--end", "2"), ("--loops", "1")):
            rc, _, err = run("export-song", self.path, "town", "-o", self.tmp / "o.mid", *opt)
            self.assertEqual(rc, 1, opt)
            self.assertIn("only applies to audio", err)
            self.assertFalse((self.tmp / "o.mid").exists())

    def test_the_song_is_named_after_a_colon_or_as_an_argument_or_is_the_only_one(self):
        out = self.tmp / "o.wav"
        self.assertEqual(run("export-song", "%s:once" % self.path, "-o", out)[0], 0)
        rc, _, err = run("export-song", self.path, "-o", out)
        self.assertEqual(rc, 1)
        self.assertIn("has 3 songs; say which: loop, once, town", err)
        rc, _, err = run("export-song", self.path, "nope", "-o", out)
        self.assertIn("no song 'nope'", err)
        rc, _, err = run("export-song", "%s:once" % self.path, "loop", "-o", out)
        self.assertEqual(rc, 2)

    def test_a_loose_mml_file_is_a_one_song_source(self):
        src = self.mml("solo", ONCE)
        out = self.tmp / "o.wav"
        self.assertEqual(run("export-song", src, "-o", out)[0], 0)
        self.assertGreater(len(read_wav(out)), RATE)

    def test_an_unknown_ending_needs_format(self):
        rc, _, err = run("export-song", self.path, "town", "-o", self.tmp / "o.xyz")
        self.assertEqual(rc, 1)
        self.assertIn("--format", err)


if __name__ == "__main__":
    unittest.main()
