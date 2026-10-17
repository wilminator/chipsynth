# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""WAV import options (dpcm.ImportOptions, prepare, convert_wav, preview and the command line)."""
import contextlib
import io
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from chipsynth import dpcm
from chipsynth.synth import DPCM_RATES

SR = 22050


def sine(seconds=0.5, freq=110.0, rate=SR, amp=0.6):
    t = np.arange(int(seconds * rate)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def noise(seconds=0.3, rate=SR, seed=3):
    return np.random.default_rng(seed).uniform(-0.5, 0.5, int(seconds * rate))


def write_wav(path, x, rate=SR):
    x = np.asarray(x)
    ch = 1 if x.ndim == 1 else x.shape[1]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 30000).astype("<i2").reshape(-1).tobytes())


class DefaultsTests(unittest.TestCase):
    def test_default_options_are_byte_identical_to_convert(self):
        for x in (sine(), noise(), np.stack([sine(), sine(freq=220)], axis=1)):
            for rate in (0, 8, 12, 15):
                old = dpcm.convert(x, SR, rate, "n")
                new, notes = dpcm.convert_wav(x, SR, dpcm.ImportOptions(rate=rate), "n")
                self.assertEqual(dpcm.dumps(old), dpcm.dumps(new))
                self.assertEqual(notes, [])

    def test_default_stereo_and_fit_match(self):
        x = np.stack([sine(), sine(freq=220)], axis=1)
        old = dpcm.convert(x, SR, 12, "n", True, fit=True)
        new, _ = dpcm.convert_wav(x, SR, dpcm.ImportOptions(channel="stereo", fit=True), "n")
        self.assertEqual(dpcm.dumps(old), dpcm.dumps(new))

    def test_none_options_means_defaults(self):
        self.assertEqual(dpcm.dumps(dpcm.convert_wav(sine(), SR)[0]), dpcm.dumps(dpcm.convert(sine(), SR, 12)))


class OptionTests(unittest.TestCase):
    def test_rate_picks_the_table_entry(self):
        for r in (3, 15):
            s, _ = dpcm.convert_wav(sine(), SR, dpcm.ImportOptions(rate=r))
            self.assertEqual(s.rate, r)
            self.assertAlmostEqual(s.seconds, 0.5, delta=0.02)
        with self.assertRaises(dpcm.DpcmError):
            dpcm.convert_wav(sine(), SR, dpcm.ImportOptions(rate=16))

    def test_auto_rate_is_the_highest_that_fits(self):
        short, _ = dpcm.convert_wav(sine(0.5), SR, dpcm.ImportOptions(rate="auto"))
        self.assertEqual(short.rate, 15)
        x = sine(70.0, rate=8000)                      # too long for rate 15 (about 63 s), fits lower down
        s, notes = dpcm.convert_wav(x, 8000, dpcm.ImportOptions(rate=None))
        self.assertLess(s.rate, 15)
        self.assertLessEqual(len(s.tracks[0]), dpcm.MAX_PARTS)
        self.assertIn("auto rate %d" % s.rate, notes)
        self.assertEqual(dpcm.auto_rate(1.0), 15)
        self.assertLess(dpcm.auto_rate(10.0, max_bytes=1000), dpcm.auto_rate(10.0))

    def test_channel_pick(self):
        left, right = sine(freq=110), sine(freq=330, amp=0.3)
        st = np.stack([left, right], axis=1)

        def pick(ch):
            return dpcm.convert_wav(st, SR, dpcm.ImportOptions(channel=ch))[0]

        self.assertEqual(dpcm.dumps(pick("left")), dpcm.dumps(dpcm.convert(left, SR, 12)))
        self.assertEqual(dpcm.dumps(pick("right")), dpcm.dumps(dpcm.convert(right, SR, 12)))
        self.assertEqual(dpcm.dumps(pick("mix")), dpcm.dumps(dpcm.convert(st, SR, 12)))
        self.assertEqual(len(pick("stereo").tracks), 2)
        self.assertNotEqual(dpcm.dumps(pick("left")), dpcm.dumps(pick("right")))
        with self.assertRaises(dpcm.DpcmError):
            pick("both")

    def test_channel_on_a_mono_file_is_harmless(self):
        base = dpcm.dumps(dpcm.convert(sine(), SR, 12))
        for ch in ("mix", "left", "right", "stereo"):
            s, _ = dpcm.convert_wav(sine(), SR, dpcm.ImportOptions(channel=ch))
            self.assertEqual(dpcm.dumps(s), base)

    def test_normalize_off_keeps_the_level(self):
        quiet = sine(amp=0.1)
        on, _ = dpcm.convert_wav(quiet, SR, dpcm.ImportOptions())
        off, _ = dpcm.convert_wav(quiet, SR, dpcm.ImportOptions(normalize=False))
        self.assertGreater(float(np.abs(dpcm.decode_preview(on)).max()),
                           float(np.abs(dpcm.decode_preview(off)).max()) + 0.1)
        self.assertEqual(dpcm.dumps(off), dpcm.dumps(dpcm.convert(quiet, SR, 12, normalise=False)))

    def test_trim_silence(self):
        body = sine(0.2)
        padded = np.concatenate([np.zeros(SR // 4), body, np.zeros(SR // 2)])
        untrimmed, _ = dpcm.convert_wav(padded, SR, dpcm.ImportOptions())
        trimmed, notes = dpcm.convert_wav(padded, SR, dpcm.ImportOptions(trim_db=-48.0))
        self.assertAlmostEqual(untrimmed.seconds, 0.95, delta=0.02)
        self.assertAlmostEqual(trimmed.seconds, 0.2, delta=0.02)
        self.assertTrue(any("trimmed" in n for n in notes))

    def test_trim_threshold_and_pure_silence(self):
        x = np.concatenate([np.zeros(1000), sine(0.1, amp=0.05), np.zeros(1000)])        # about -26 dB
        self.assertAlmostEqual(len(dpcm.trim_silence(x.reshape(-1, 1), -48.0)), len(sine(0.1)), delta=20)
        self.assertEqual(len(dpcm.trim_silence(x.reshape(-1, 1), -20.0)), 1)
        s, _ = dpcm.convert_wav(np.zeros(5000), SR, dpcm.ImportOptions(trim_db=-48.0))
        self.assertGreaterEqual(s.size, 1)

    def test_fade_out_ends_quiet_and_leaves_the_start(self):
        x = np.sign(sine(0.5, 55.0)) * 0.8
        f = dpcm.fade_out(x.reshape(-1, 1), SR, 100)
        k = len(x) - SR // 10 - 5
        self.assertEqual(float(np.abs(f[:k]).max()), float(np.abs(x[:k]).max()))
        self.assertLess(float(np.abs(f[-50:]).max()), 0.05)
        plain, _ = dpcm.convert_wav(x, SR, dpcm.ImportOptions())
        faded, notes = dpcm.convert_wav(x, SR, dpcm.ImportOptions(fade_ms=100))
        self.assertEqual(plain.size, faded.size)
        self.assertNotEqual(dpcm.dumps(plain), dpcm.dumps(faded))

        def tail(s):
            return float(np.abs(dpcm.decode_preview(s)[-200:]).mean())

        self.assertLess(tail(faded), tail(plain))
        self.assertTrue(any("fade" in n for n in notes))
        self.assertEqual(dpcm.fade_out(x, SR, 0).shape, x.shape)          # 1-D input works too
        self.assertEqual(dpcm.fade_out(x, SR, 50).shape, x.shape)

    def test_max_seconds(self):
        s, notes = dpcm.convert_wav(sine(1.0), SR, dpcm.ImportOptions(max_seconds=0.25))
        self.assertAlmostEqual(s.seconds, 0.25, delta=0.02)
        self.assertTrue(any("cut" in n for n in notes))
        s, notes = dpcm.convert_wav(sine(0.2), SR, dpcm.ImportOptions(max_seconds=5))
        self.assertEqual(notes, [])

    def test_max_bytes(self):
        s, _ = dpcm.convert_wav(sine(2.0), SR, dpcm.ImportOptions(rate=12, max_bytes=500))
        self.assertLessEqual(s.size, 500)
        self.assertGreater(s.size, 480)

    def test_too_long_fails_unless_truncate(self):
        x = sine(70.0, rate=8000)
        with self.assertRaises(dpcm.DpcmError):
            dpcm.convert_wav(x, 8000, dpcm.ImportOptions(rate=15))
        s, notes = dpcm.convert_wav(x, 8000, dpcm.ImportOptions(rate=15, truncate=True))
        self.assertLessEqual(len(s.tracks[0]), dpcm.MAX_PARTS)
        self.assertTrue(any("cut to" in n for n in notes))

    def test_options_combine_in_order(self):
        x = np.concatenate([np.zeros(SR // 2), sine(1.0), np.zeros(SR // 2)])
        s, notes = dpcm.convert_wav(x, SR, dpcm.ImportOptions(trim_db=-48, max_seconds=0.5, fade_ms=50))
        self.assertAlmostEqual(s.seconds, 0.5, delta=0.02)
        self.assertEqual(len(notes), 3)


class PreviewTests(unittest.TestCase):
    def test_preview_matches_the_real_conversion(self):
        o = dpcm.ImportOptions(rate=9, max_seconds=0.3)
        info = dpcm.preview(sine(), SR, o, "p")
        s, _ = dpcm.convert_wav(sine(), SR, o, "p")
        self.assertEqual((info["size"], info["rate"], info["hz"], info["parts"], info["tracks"]),
                         (s.size, 9, DPCM_RATES[9], 1, 1))
        self.assertAlmostEqual(info["seconds"], s.seconds)
        self.assertIsNone(info["error"])
        text = dpcm.format_preview(info, SR, 0.5)
        self.assertIn("%d bytes" % s.size, text)
        self.assertIn("rate 9", text)

    def test_preview_reports_errors(self):
        info = dpcm.preview(sine(70.0, rate=8000), 8000, dpcm.ImportOptions(rate=15))
        self.assertIn("too long", info["error"])
        self.assertIn("cannot import", dpcm.format_preview(info))
        self.assertIn("rate is 0 to 15", dpcm.preview(sine(), SR, dpcm.ImportOptions(rate=40))["error"])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = dpcm.main(list(argv))
        return rc, out.getvalue()

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.root = Path(self._d.name)
        self.wav = self.root / "In Tone.wav"
        write_wav(self.wav, np.stack([sine(), sine(freq=330, amp=0.3)], axis=1))

    def tearDown(self):
        self._d.cleanup()

    def test_default_cli_output_is_unchanged(self):
        rc, out = self.run_cli("convert", str(self.wav), str(self.root))
        self.assertEqual(rc, 0)
        rate, x = dpcm.read_wav(self.wav)
        want = dpcm.convert(x, rate, 12, "InTone")
        self.assertEqual((self.root / "music" / "samples" / "InTone.dpcm").read_bytes(), dpcm.dumps(want))
        self.assertEqual(out, "music/samples/InTone.dpcm: %d bytes, %.1f s, rate 12, 1 part(s)\n"
                         % (want.size, want.seconds))

    def test_stereo_flag_and_channel_stereo_agree(self):
        self.run_cli("convert", str(self.wav), str(self.root), "--name", "a", "--stereo")
        self.run_cli("convert", str(self.wav), str(self.root), "--name", "b", "--channel", "stereo")
        d = self.root / "music" / "samples"
        self.assertEqual((d / "a.dpcm").read_bytes(), (d / "b.dpcm").read_bytes())

    def test_each_option_on_the_command_line(self):
        d = self.root / "music" / "samples"
        cases = {"l": ["--channel", "left"], "r": ["--rate", "5"], "n": ["--no-normalize"], "t": ["--trim"],
                 "f": ["--fade", "80"], "m": ["--max-seconds", "0.2"], "b": ["--max-bytes", "300"],
                 "a": ["--rate", "auto"]}
        for name, extra in cases.items():
            rc, _ = self.run_cli("convert", str(self.wav), str(self.root), "--name", name, *extra)
            self.assertEqual(rc, 0, name)
        s = {n: dpcm.load(d / (n + ".dpcm")) for n in cases}
        self.assertEqual(s["r"].rate, 5)
        self.assertEqual(s["a"].rate, 15)
        self.assertAlmostEqual(s["m"].seconds, 0.2, delta=0.02)
        self.assertLessEqual(s["b"].size, 300)
        self.assertNotEqual(dpcm.dumps(s["l"]), dpcm.dumps(s["n"]))

    def test_preview_writes_nothing(self):
        rc, out = self.run_cli("convert", str(self.wav), str(self.root), "--preview", "--rate", "auto",
                               "--max-seconds", "0.25")
        self.assertEqual(rc, 0)
        self.assertIn("22050 Hz 0.50 s WAV -> ", out)
        self.assertIn("rate 15", out)
        self.assertIn("cut to 0.250 s", out)
        self.assertFalse((self.root / "music").exists())

    def test_preview_of_a_failing_import_returns_1(self):
        write_wav(self.root / "long.wav", sine(70.0, rate=8000), 8000)
        rc, out = self.run_cli("convert", str(self.root / "long.wav"), str(self.root), "--preview", "--rate", "15")
        self.assertEqual(rc, 1)
        self.assertIn("cannot import", out)
        rc, out = self.run_cli("convert", str(self.root / "long.wav"), str(self.root), "--rate", "15", "--truncate")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
