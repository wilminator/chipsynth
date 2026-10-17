# Copyright (C) 2026 Michael Allen Wilmes
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from chipsynth import dpcm, mml, player as music
from chipsynth.synth import DPCM_RATES, RATE, Synth, dpcm_decode


def tone(seconds=0.5, hz=110, rate=44100):
    t = np.arange(int(seconds * rate)) / rate
    return np.sin(2 * np.pi * hz * t) * np.exp(-t * 4)


class ConvertTests(unittest.TestCase):
    def test_size_and_duration_follow_the_rate(self):
        s = dpcm.convert(tone(), 44100, rate=12)
        self.assertAlmostEqual(s.seconds, 0.5, delta=0.01)
        self.assertAlmostEqual(s.size, 0.5 * DPCM_RATES[12] / 8, delta=4)

    def test_fit_follows_the_signal_closely(self):
        s = dpcm.convert(np.sin(2 * np.pi * 220 * np.arange(44100) / 44100), 44100, rate=12, fit=True)
        d = dpcm.decode_preview(s)
        ref = dpcm.resample(np.sin(2 * np.pi * 220 * np.arange(44100) / 44100), 44100, DPCM_RATES[12])
        n = min(len(d), len(ref))
        self.assertGreater(np.corrcoef(d[:n], ref[:n])[0, 1], 0.95)

    def test_loud_signal_without_fit_overloads_but_stays_in_range(self):
        s = dpcm.convert(np.sign(np.sin(np.arange(20000) / 20.0)), 44100, rate=15)
        d = dpcm.decode_preview(s)
        self.assertTrue(np.all(np.abs(d) <= 1.0))

    def test_long_samples_are_cut_into_parts_of_at_most_4081_bytes(self):
        s = dpcm.convert(tone(2.0), 44100, rate=15)
        self.assertGreater(len(s.tracks[0]), 1)
        self.assertTrue(all(len(p) <= dpcm.MAX_PART for p in s.tracks[0]))
        self.assertEqual(sum(len(p) for p in s.tracks[0]), s.size)

    def test_stereo_split_makes_two_tracks_and_mix_makes_one(self):
        st = np.stack([tone(), -tone()], axis=1)
        self.assertEqual(len(dpcm.convert(st, 44100, rate=12, stereo=True).tracks), 2)
        self.assertEqual(len(dpcm.convert(st, 44100, rate=12, stereo=False).tracks), 1)

    def test_too_long_is_refused(self):
        with self.assertRaises(dpcm.DpcmError):
            dpcm.convert(tone(90.0), 44100, rate=15)

    def test_silence_is_a_flat_sample(self):
        s = dpcm.convert(np.zeros(4000), 44100, rate=12)
        d = dpcm.decode_preview(s)
        self.assertLess(float(np.abs(d).max()), 0.1)

    def test_bad_rate(self):
        with self.assertRaises(dpcm.DpcmError):
            dpcm.convert(tone(), 44100, rate=16)


class FileTests(unittest.TestCase):
    def test_round_trip(self):
        s = dpcm.convert(np.stack([tone(1.5), tone(1.5, 220)], axis=1), 44100, rate=14, stereo=True, name="x")
        back = dpcm.loads(dpcm.dumps(s), "x")
        self.assertEqual((back.rate, back.tracks), (s.rate, s.tracks))

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            s = dpcm.convert(tone(), 44100, rate=10)
            dpcm.save(Path(d) / "sub" / "thump.dpcm", s)
            back = dpcm.load(Path(d) / "sub" / "thump.dpcm")
            self.assertEqual((back.name, back.rate, back.tracks), ("thump", 10, s.tracks))

    def test_bad_files(self):
        for bad in (b"", b"XXXX\x01\x0f\x01", b"BDPC\x09\x00\x01", dpcm.dumps(dpcm.convert(tone(), 44100))[:-3]):
            with self.assertRaises(dpcm.DpcmError):
                dpcm.loads(bad)

    def test_read_wav(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.wav"
            x = (tone(0.1) * 30000).astype("<i2")
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(x.tobytes())
            rate, y = dpcm.read_wav(path)
            self.assertEqual((rate, y.shape), (22050, (len(x), 1)))
            self.assertAlmostEqual(float(y[100, 0]), x[100] / 32768.0, places=4)
            with self.assertRaises(dpcm.DpcmError):
                dpcm.read_wav(Path(d) / "nope.wav")


class ChainTests(unittest.TestCase):
    def test_a_chain_plays_like_one_long_sample(self):
        long = dpcm.convert(tone(1.0), 44100, rate=15)
        parts = long.tracks[0]
        self.assertGreater(len(parts), 1)
        sy = Synth()
        sy.schedule(0, "dac_start", 0, data=parts, rate=15, vol=15, pan=0)
        chained = sy.render(RATE)
        # the same bits as one decode (a carried level makes the join seamless)
        whole = dpcm_decode(b"".join(parts))
        want = (whole - 64.0) / 64.0
        got = chained[:, 0] / (0.2 * 1.4142 * 0.7071)             # undo the mixer gains (centre pan)
        step = DPCM_RATES[15] / RATE
        idx = (np.arange(RATE) * step).astype(int)
        ok = idx < len(want)
        np.testing.assert_allclose(got[ok][:20000], want[idx[ok]][:20000], atol=0.04)

    def test_chunking_is_exact(self):
        parts = dpcm.convert(tone(1.0), 44100, rate=15).tracks[0]

        def go(sizes):
            sy = Synth()
            sy.schedule(0, "dac_start", 0, data=parts, rate=15, vol=15, pan=0)
            return np.concatenate([sy.render(n) for n in sizes])
        a = go([RATE * 2])
        b = go([1, 4095, 10000, 333, RATE * 2 - 14429])
        np.testing.assert_array_equal(a, b)

    def test_a_new_start_cuts_the_old_chain(self):
        parts = dpcm.convert(tone(1.0), 44100, rate=15).tracks[0]
        sy = Synth()
        sy.schedule(0, "dac_start", 0, data=parts, rate=15, vol=15, pan=0)
        sy.schedule(1000, "dac_start", 0, data=[b"\x55"], rate=15, vol=15, pan=0)
        out = sy.render(RATE)
        self.assertLess(float(np.abs(out[8000:]).max()), 0.01)


def library_with(samples):
    d = tempfile.mkdtemp()
    for name, s in samples.items():
        dpcm.save(Path(d) / "samples" / (name + ".dpcm"), s)
    (Path(d) / "lib").mkdir()
    return mml.load_library(Path(d) / "lib"), Path(d)


class MmlTests(unittest.TestCase):
    def setUp(self):
        self.kick = dpcm.convert(tone(0.2, 60), 44100, rate=12, name="kick")
        self.voice = dpcm.convert(tone(1.2), 44100, rate=15, name="voice")
        self.lib, self.dir = library_with({"kick": self.kick, "voice": self.voice})

    def parse(self, text):
        return mml.parse("tempo 120\n" + text, self.lib)

    def test_names_play_at_their_times(self):
        t = self.parse("D1: kick4 r4 kick8 kick8")
        self.assertEqual([(e[0], e[1]) for e in t.dac[0]], [(0, "play"), (192, "play"), (240, "play")])
        self.assertEqual(t.length, 288)

    def test_default_length_and_commands(self):
        t = self.parse("D2: l8 v9 pan -3 kick kick4 stop level 90 r1")
        ev = t.dac[1]
        self.assertEqual([e[1] for e in ev], ["play", "play", "stop", "level"])
        self.assertEqual(ev[0][3], (9, -3))
        self.assertEqual(ev[3][2], 90)
        self.assertEqual(ev[1][0], 48)

    def test_repeats_and_patterns(self):
        t = self.parse("[pat]\nbeat = kick4 r4\nD1: [kick8 r8]3 !beat")
        self.assertEqual(len(t.dac[0]), 4)

    def test_sample_section_alias_and_rate_override(self):
        t = self.parse("[sample]\nthump = kick rate=5\nboom kick\nD1: thump boom")
        a, b = t.dac[0][0][2], t.dac[0][1][2]
        self.assertEqual((a.rate, b.rate), (5, 12))
        self.assertEqual(a.tracks, self.kick.tracks)

    def test_import_and_unknown(self):
        self.parse("import sample.kick\nD1: kick")
        for bad in ("D1: snare", "import sample.snare", "D1: kick v16", "D1: pan 9 kick", "D1: level 128",
                    "[sample]\nx kick rate=16\nD1: x", "D1: l kick"):
            with self.assertRaises(mml.MMLError, msg=bad):
                self.parse(bad)

    def test_lengths_count_towards_the_tune_and_loop(self):
        t = self.parse("V1: c4\nD1: loop kick1 kick1")
        self.assertEqual(t.length, 768)
        self.assertEqual(t.loop_tick, 0)

    def test_library_file_declares_samples(self):
        (self.dir / "lib" / "drums.mml").write_text("[sample]\nbd = kick rate=9\n")
        lib = mml.load_library(self.dir / "lib")
        t = mml.parse("tempo 120\nD1: bd", lib)
        self.assertEqual(t.dac[0][0][2].rate, 9)

    def test_damaged_file_is_not_found(self):
        (self.dir / "samples" / "bad.dpcm").write_bytes(b"junk")
        with self.assertRaises(mml.MMLError):
            self.parse("D1: bad")


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.kick = dpcm.convert(tone(0.2, 60), 44100, rate=12, name="kick")
        self.lib, self.dir = library_with({"kick": self.kick})

    def render(self, text, sizes):
        p = music.TunePlayer(mml.parse("tempo 120\n" + text, self.lib))
        return np.concatenate([p.render(n) for n in sizes])

    def test_sample_is_heard_at_its_time(self):
        out = self.render("D1: r4 kick4 r2", [RATE * 2])
        self.assertLess(float(np.abs(out[:RATE // 2 - 100]).max()), 1e-6)
        self.assertGreater(float(np.abs(out[RATE // 2:RATE // 2 + 4000]).max()), 0.01)

    def test_chunking_is_exact(self):
        text = "D1: loop kick4 r4 kick8 kick8\nV1: c1"
        a = self.render(text, [RATE * 4])
        b = self.render(text, [1, 999, 4095, 20000, RATE * 4 - 25095])
        np.testing.assert_array_equal(a, b)

    def test_stereo_sample_splits_left_and_right(self):
        st = dpcm.convert(np.stack([tone(0.2, 60), np.zeros(8820)], axis=1), 44100, rate=12, stereo=True, name="st")
        dpcm.save(self.dir / "samples" / "st.dpcm", st)
        out = self.render("D1: st4 r2", [RATE])
        left, right = float(np.abs(out[:8000, 0]).max()), float(np.abs(out[:8000, 1]).max())
        self.assertGreater(left, right * 3)

    def test_direct_level_holds(self):
        out = self.render("D2: level 100 r2", [RATE // 4])
        self.assertGreater(float(out[1000:, 0].mean()), 0.01)

    def test_stop_cuts(self):
        a = self.render("D1: kick4 r4", [RATE])
        b = self.render("D1: kick8 stop r8 r4", [RATE])
        self.assertLess(float(np.abs(b[RATE // 4 + 2000:]).max()), 1e-6)
        self.assertGreater(float(np.abs(a[RATE // 8:RATE // 4]).max()), 0.01)

    def test_a_sample_rings_over_the_loop_restart(self):
        long = dpcm.convert(tone(1.0, 60), 44100, rate=15, name="long")
        dpcm.save(self.dir / "samples" / "long.dpcm", long)
        p = music.TunePlayer(mml.parse("tempo 120\nD1: loop r4 r4 r4 long4\nV1: r1\n", self.lib))
        n = p._pass_len
        out = p.render(n * 2)
        self.assertGreater(float(np.abs(out[n + 500:n + 6000]).max()), 0.01)


if __name__ == "__main__":
    unittest.main()


class CommandLineTests(unittest.TestCase):
    def test_runs_as_a_module_by_both_names(self):
        import subprocess
        import sys
        root = str(Path(__file__).resolve().parents[2])
        import importlib.util
        names = ["chipsynth.dpcm"] + (["blobs.dpcm"] if importlib.util.find_spec("blobs") else [])   # blobs: the game's shim
        for name in names:
            with tempfile.TemporaryDirectory() as d:
                out = subprocess.run([sys.executable, "-m", name, "demo", d], cwd=root, capture_output=True, text=True)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertTrue((Path(d) / "music" / "wav" / "kick.wav").is_file(), name)

    def test_demo_and_convert(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(dpcm.main(["demo", d]), 0)
            self.assertTrue((Path(d) / "music" / "wav" / "kick.wav").is_file())
            self.assertEqual(dpcm.main(["convert", str(Path(d) / "music" / "wav" / "snare.wav"), d, "--rate", "13",
                                        "--fit"]), 0)
            s = dpcm.load(Path(d) / "music" / "samples" / "snare.dpcm")
            self.assertEqual((s.name, s.rate), ("snare", 13))
