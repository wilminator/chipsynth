# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""WAV import options for packages and the composer: gain, DAC rates, the report, `python -m chipsynth import FILE.wav`
and the File > Import > WAV dialog.  Every WAV is built here."""
import contextlib
import io
import os
import tempfile
import unittest
import wave
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import numpy as np
import pygame

from chipsynth import dpcm, package as pk
from chipsynth.__main__ import main
from chipsynth.composer.app import Composer
from chipsynth.composer.prefs import Prefs
from chipsynth.song import Song
from chipsynth.synth import DPCM_RATES

SR = 22050


def tone(seconds=0.4, freq=110.0, amp=0.6):
    t = np.arange(int(seconds * SR)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


def write_wav(path, x, rate=SR):
    x = np.asarray(x)
    ch = 1 if x.ndim == 1 else x.shape[1]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 30000).astype("<i2").reshape(-1).tobytes())


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(args))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class GainAndReportTests(unittest.TestCase):
    def test_default_gain_changes_nothing(self):
        x = tone()
        for fit in (False, True):
            old = dpcm.convert(x, SR, 12, "n", fit=fit)
            new, _ = dpcm.convert_wav(x, SR, dpcm.ImportOptions(fit=fit, gain_db=0.0), "n")
            self.assertEqual(dpcm.dumps(old), dpcm.dumps(new))

    def test_gain_scales_the_level_after_normalizing(self):
        st = {}
        dpcm.convert_wav(tone(), SR, dpcm.ImportOptions(gain_db=-6.0), "n", st)
        self.assertAlmostEqual(st["peak"], 0.98 * 10 ** (-6 / 20), delta=0.02)
        self.assertEqual(st["clipped"], 0)

    def test_a_boost_past_full_scale_is_counted_as_clipping(self):
        st = {}
        s, notes = dpcm.convert_wav(tone(), SR, dpcm.ImportOptions(gain_db=12.0), "n", st)
        self.assertGreater(st["clipped"], 0)
        self.assertIn("gain +12 dB", notes)
        lines = dpcm.report_lines(s, SR, 0.4, st, notes)
        self.assertTrue(any(x.startswith("warning: clipping") for x in lines), lines)

    def test_report_gives_rate_length_bytes_and_clean_clipping(self):
        st = {}
        s, notes = dpcm.convert_wav(tone(), SR, dpcm.ImportOptions(rate=7), "n", st)
        text = "\n".join(dpcm.report_lines(s, SR, 0.4, st, notes))
        self.assertIn("rate: 7 = %d Hz" % round(DPCM_RATES[7]), text)
        self.assertIn("bytes: %d" % s.size, text)
        self.assertIn("length: %.2f s" % s.seconds, text)
        self.assertIn("clipping: none", text)

    def test_rate_parse_takes_index_hz_and_auto(self):
        self.assertEqual(dpcm.parse_rate("5"), 5)
        self.assertEqual(dpcm.parse_rate("auto"), "auto")
        self.assertEqual(dpcm.parse_rate("8363"), 7)
        self.assertEqual(dpcm.parse_rate("%.2f" % DPCM_RATES[15]), 15)
        for bad in ("16", "8000", "x"):
            with self.assertRaises(dpcm.DpcmError):
                dpcm.parse_rate(bad)

    def test_every_rate_has_a_quality_note(self):
        for r in range(16):
            self.assertIn("%d Hz" % round(DPCM_RATES[r]), dpcm.rate_note(r))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.pkg = self.dir / "p.csp"
        self.stereo = self.dir / "st.wav"
        write_wav(self.stereo, np.stack([tone(freq=110), tone(freq=330, amp=0.3)], axis=1))
        self.mono = self.dir / "kick.wav"
        write_wav(self.mono, tone(freq=110))

    def sample(self, name):
        return pk.load(self.pkg).samples[name]

    def test_plain_import_is_what_the_composer_always_made(self):
        code, out, err = cli("import", str(self.pkg), str(self.stereo))
        self.assertEqual(code, 0, err)
        sr, x = dpcm.read_wav(self.stereo)
        expect = dpcm.convert(x, sr, rate=12, name="st", fit=True)
        self.assertEqual(self.sample("st").tracks, expect.tracks)
        self.assertEqual(self.sample("st").rate, 12)
        self.assertIn("bytes:", out)
        self.assertIn("clipping:", out)

    def test_channel_choices(self):
        sr, x = dpcm.read_wav(self.stereo)
        for ch, col in (("left", 0), ("right", 1)):
            code, _, err = cli("import", str(self.pkg), str(self.stereo), "--id", ch, "--channel", ch)
            self.assertEqual(code, 0, err)
            expect = dpcm.convert(x[:, col:col + 1], sr, rate=12, name=ch, fit=True)
            self.assertEqual(self.sample(ch).tracks, expect.tracks)
        self.assertNotEqual(self.sample("left").tracks, self.sample("right").tracks)

    def test_rate_by_index_hz_and_auto(self):
        cli("import", str(self.pkg), str(self.mono), "--id", "a", "--rate", "3")
        cli("import", str(self.pkg), str(self.mono), "--id", "b", "--rate", "5593")
        self.assertEqual(self.sample("a").rate, 3)
        self.assertEqual(self.sample("b").rate, 3)
        cli("import", str(self.pkg), str(self.mono), "--id", "c", "--rate", "auto")
        self.assertEqual(self.sample("c").rate, 15)                      # a short WAV fits at the top rate
        self.assertGreater(self.sample("c").size, self.sample("a").size)
        code, _, err = cli("import", str(self.pkg), str(self.mono), "--rate", "9000")
        self.assertNotEqual(code, 0)

    def test_trim_normalize_gain_and_no_fit(self):
        quiet = self.dir / "q.wav"
        write_wav(quiet, np.concatenate([np.zeros(SR // 4), tone(0.2, amp=0.1), np.zeros(SR // 4)]))
        cli("import", str(self.pkg), str(quiet), "--id", "full", "--no-fit")
        cli("import", str(self.pkg), str(quiet), "--id", "trim", "--no-fit", "--trim")
        cli("import", str(self.pkg), str(quiet), "--id", "raw", "--no-fit", "--trim", "--no-normalize")
        self.assertLess(self.sample("trim").size, self.sample("full").size)
        sr, x = dpcm.read_wav(quiet)
        expect, _ = dpcm.convert_wav(x, sr, dpcm.ImportOptions(trim_db=-48.0), "trim")
        self.assertEqual(self.sample("trim").tracks, expect.tracks)
        self.assertEqual(self.sample("trim").size, self.sample("raw").size)
        self.assertNotEqual(self.sample("trim").tracks, self.sample("raw").tracks)   # raw keeps the quiet level
        code, out, _ = cli("import", str(self.pkg), str(self.mono), "--id", "loud", "--no-fit", "--gain", "12")
        self.assertEqual(code, 0)
        self.assertIn("warning: clipping", out)

    def test_no_replace_refuses_a_sample_that_is_there(self):
        cli("import", str(self.pkg), str(self.mono))
        code, out, err = cli("import", str(self.pkg), str(self.mono), "--no-replace")
        self.assertNotEqual(code, 0)
        self.assertIn("already has a sample", out + err)

    def test_a_bad_wav_is_an_error(self):
        bad = self.dir / "bad.wav"
        bad.write_bytes(b"not a wav")
        code, out, err = cli("import", str(self.pkg), str(bad))
        self.assertNotEqual(code, 0)
        self.assertIn("bad.wav", out + err)

    def test_the_dpcm_command_takes_the_same_flags(self):
        game = self.dir / "game"
        self.assertEqual(dpcm.main(["convert", str(self.mono), str(game), "--rate", "5593", "--gain", "-3"]), 0)
        self.assertEqual(dpcm.load(game / "music" / "samples" / "kick.dpcm").rate, 3)


class ComposerTests(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.a = Composer(self.screen, Prefs(view="roll"), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.a.frame()
        self.assertTrue(self.a.load_song(Song.from_mml("title T\ntempo 120\nV1: c4 d4 e4 f4\n"), "T"))
        self.a.frame()
        self.wav = self.dir / "thump.wav"
        write_wav(self.wav, np.stack([tone(freq=90), tone(freq=300, amp=0.2)], axis=1))

    def key(self, k):
        return pygame.event.Event(pygame.KEYDOWN, key=k, mod=0, unicode="")

    def test_a_plain_import_keeps_its_bytes(self):
        self.assertTrue(self.a.import_wav(self.wav))
        sr, x = dpcm.read_wav(self.wav)
        self.assertEqual(dpcm.dumps(self.a.imported["thump"]), dpcm.dumps(dpcm.convert(x, sr, rate=12, name="thump", fit=True)))
        self.assertIsNone(self.a.overlay)
        self.assertIn("bytes: %d" % self.a.imported["thump"].size, self.a.last_report[1])

    def test_the_dialog_defaults_give_the_same_bytes_as_a_plain_import(self):
        a = self.a
        self.assertTrue(a.import_begin(self.wav))
        self.assertEqual(a.overlay, "fileopts")
        a.frame()
        self.assertEqual([r["key"] for r in a.fileopts["rows"]], ["rate", "channel", "trim", "normalize", "gain", "fit"])
        a.frame([self.key(pygame.K_RETURN)])
        self.assertIsNone(a.overlay)
        sr, x = dpcm.read_wav(self.wav)
        self.assertEqual(dpcm.dumps(a.imported["thump"]), dpcm.dumps(dpcm.convert(x, sr, rate=12, name="thump", fit=True)))

    def test_choices_in_the_dialog_reach_the_sample(self):
        a = self.a
        a.import_begin(self.wav)
        a.frame()
        rows = {r["key"]: i for i, r in enumerate(a.fileopts["rows"])}
        a.fileopts_step(rows["rate"], -9)                        # 12 -> 3
        a.fileopts_step(rows["channel"], 1)                      # left
        a.fileopts_step(rows["fit"], 1)                          # off
        self.assertEqual(a.fileopts_values()["rate"], 3)
        a.fileopts_finish(True)
        sr, x = dpcm.read_wav(self.wav)
        expect, _ = dpcm.convert_wav(x, sr, dpcm.ImportOptions(rate=3, channel="left"), "thump")
        self.assertEqual(a.imported["thump"].rate, 3)
        self.assertEqual(dpcm.dumps(a.imported["thump"]), dpcm.dumps(expect))

    def test_a_loud_gain_opens_the_report_with_the_clipping_warning(self):
        a = self.a
        a.import_begin(self.wav)
        a.frame()
        rows = {r["key"]: i for i, r in enumerate(a.fileopts["rows"])}
        a.fileopts_step(rows["gain"], 3)                         # +12 dB
        a.fileopts_step(rows["fit"], 1)
        a.fileopts_finish(True)
        self.assertEqual(a.overlay, "report")
        self.assertTrue(any(x.startswith("warning: clipping") for x in a.last_report[1]), a.last_report)
        self.assertIn("CLIPPING", a.message)

    def test_cancel_imports_nothing_and_a_drop_asks(self):
        a = self.a
        a.frame([pygame.event.Event(pygame.DROPFILE, file=str(self.wav))])
        self.assertEqual(a.overlay, "fileopts")
        a.frame([self.key(pygame.K_ESCAPE)])
        self.assertNotIn("thump", a.ed.sample_names())


if __name__ == "__main__":
    unittest.main()
