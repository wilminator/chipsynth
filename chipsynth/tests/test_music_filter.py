# Copyright (C) 1994, 2026 Michael Allen Wilmes
import unittest

import numpy as np

from chipsynth import mml, player as music
from chipsynth.synth import RATE, biquad, filter_cutoff_hz


def tune(text):
    return mml.parse("tempo 120\n" + text)


def render(text, n=RATE, sizes=None):
    p = music.TunePlayer(tune(text))
    if sizes:
        return np.concatenate([p.render(k) for k in sizes])
    return p.render(n)


def band_energy(x, lo, hi):
    sp = np.abs(np.fft.rfft(x[:, 0] * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1.0 / RATE)
    return float(sp[(f >= lo) & (f < hi)].sum())


class ResponseTests(unittest.TestCase):
    SAW = "V1: wave saw adsr 0,0,15,4 o3 c1 "

    def test_cutoff_scale(self):
        self.assertAlmostEqual(filter_cutoff_hz(0), 100.0)
        self.assertAlmostEqual(filter_cutoff_hz(15), 8000.0)
        self.assertLess(filter_cutoff_hz(5), filter_cutoff_hz(6))

    def test_lowpass_removes_highs(self):
        dry = render(self.SAW)[4000:]
        lp = render("V1: filter lp,5 wave saw adsr 0,0,15,4 o3 c1")[4000:]
        self.assertLess(band_energy(lp, 3000, 10000), band_energy(dry, 3000, 10000) * 0.05)
        self.assertGreater(band_energy(lp, 100, 400), band_energy(dry, 100, 400) * 0.5)

    def test_highpass_removes_lows(self):
        dry = render(self.SAW)[4000:]
        hp = render("V1: filter hp,10 wave saw adsr 0,0,15,4 o3 c1")[4000:]
        self.assertLess(band_energy(hp, 50, 400), band_energy(dry, 50, 400) * 0.05)
        self.assertGreater(band_energy(hp, 3000, 10000), band_energy(dry, 3000, 10000) * 0.5)

    def test_bandpass_keeps_the_middle(self):
        dry = render(self.SAW)[4000:]
        bp = render("V1: filter bp,8,6 wave saw adsr 0,0,15,4 o3 c1")[4000:]
        f = filter_cutoff_hz(8)
        self.assertLess(band_energy(bp, 50, f / 4), band_energy(dry, 50, f / 4) * 0.2)
        self.assertLess(band_energy(bp, f * 4, 12000), band_energy(dry, f * 4, 12000) * 0.2)

    def test_resonance_peaks_at_the_cutoff(self):
        f = filter_cutoff_hz(8)

        def gain(res):
            b, a = biquad("lp", 8, res)
            w = np.exp(-1j * 2 * np.pi * f / RATE * np.arange(3))
            return abs((b * w).sum() / (a * w).sum())
        self.assertGreater(gain(12), gain(0) * 3)

    def test_off_changes_nothing(self):
        a = render(self.SAW)
        b = render("V1: filter lp,3 filter off wave saw adsr 0,0,15,4 o3 c1")
        np.testing.assert_array_equal(a, b)

    def test_sweep_moves_the_cutoff(self):
        out = render("V1: filter lp,2 fsweep 2,13,2 wave saw adsr 0,0,15,4 o3 c1", n=RATE * 2)
        early = band_energy(out[2000:8000], 2000, 10000)
        late = band_energy(out[RATE // 2 - 3000:RATE // 2 + 3000], 2000, 10000)     # half a period in: cutoff fully open
        self.assertGreater(late, early * 3)

    def test_drums_with_a_filter_and_dry_voices_unaffected(self):
        both = render("V1: wave saw adsr 0,0,15,4 o3 c1\nV2: filter lp,2 wave saw adsr 0,0,15,4 o3 c1")
        solo = render("V1: wave saw adsr 0,0,15,4 o3 c1")
        self.assertFalse(np.allclose(both, solo * 2))


class ChunkTests(unittest.TestCase):
    def test_chunking_is_exact_with_a_sweep(self):
        text = "V1: filter lp,5,8 fsweep 2,12,4 wave saw o2 c1\nV2: filter hp,4 wave pulse o4 e1"
        a = render(text, sizes=[RATE * 2])
        b = render(text, sizes=[1, 31, 32, 33, 1000, 5000, 44100, RATE * 2 - 50197])
        np.testing.assert_allclose(a, b, atol=1e-6)

    def test_filter_state_resets_on_a_new_note(self):
        from chipsynth.synth import Instrument, _Voice
        v = _Voice()
        v.inst = Instrument(wave="saw", filt="lp", filt_cut=4, filt_res=10)
        v.gate_on()
        v.filtered(np.ones(500), 0)
        self.assertTrue(np.any(v.zi != 0))
        v.gate_on()
        self.assertTrue(np.all(v.zi == 0))


class ParseTests(unittest.TestCase):
    def test_command_instrument_key_and_fx(self):
        t = tune("[instr]\nsoft wave=saw filter=lp,6,3 fsweep=1,9,5\n[fx]\nwah filter=bp,7,9\n"
                 "V1: filter hp,4 c fx wah d\nV2: instr soft c")
        a, b = t.voices[0][0].inst, t.voices[0][1].inst
        self.assertEqual((a.filt, a.filt_cut, a.filt_res), ("hp", 4, 0))
        self.assertEqual((b.filt, b.filt_cut, b.filt_res), ("bp", 7, 9))
        c = t.voices[1][0].inst
        self.assertEqual((c.filt, c.filt_cut, c.filt_res, c.fs_from, c.fs_to, c.fs_rate), ("lp", 6, 3, 1, 9, 5))

    def test_off_and_defaults(self):
        t = tune("V1: c filter lp,3,3 d filter off e")
        self.assertEqual(t.voices[0][0].inst.filt, "")
        self.assertEqual(t.voices[0][1].inst.filt, "lp")
        self.assertEqual(t.voices[0][2].inst.filt, "")

    def test_errors(self):
        for bad in ("filter xx,3", "filter lp", "filter lp,16", "filter lp,3,16", "fsweep 1,2", "fsweep 1,2,16",
                    "fsweep 20,2,3"):
            with self.assertRaises(mml.MMLError, msg=bad):
                tune("V1: %s c" % bad)


class BusTests(unittest.TestCase):
    def test_the_send_is_filtered_too(self):
        text = "echo 100ms,0,100\nV1: %s wave saw adsr 0,0,15,4 o4 c8 r1"
        raw = render(text % "", n=RATE)
        filt = render(text % "filter lp,2", n=RATE)
        echo_part = slice(int(0.13 * RATE), int(0.2 * RATE))
        self.assertLess(band_energy(filt[echo_part], 2000, 10000), band_energy(raw[echo_part], 2000, 10000) * 0.1)


if __name__ == "__main__":
    unittest.main()
