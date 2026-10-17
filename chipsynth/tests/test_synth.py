# Copyright (C) 2026 Michael Allen Wilmes
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from chipsynth import synth
from chipsynth.synth import Synth, Instrument, RATE, note_freq, dpcm_decode, to_pcm16


def play(inst, freq, secs=1.0, vol=15, off_at=None, pan=None):
    s = Synth()
    kw = dict(freq=freq, inst=inst, vol=vol)
    if pan is not None:
        kw["pan"] = pan
    s.schedule(0, "on", 0, **kw)
    if off_at is not None:
        s.schedule(int(off_at * RATE), "off", 0)
    return s.render(int(secs * RATE))


def spectrum(x):
    x = x[:, 0] if x.ndim == 2 else x
    w = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return w, np.fft.rfftfreq(len(x), 1.0 / RATE)


def level_at(w, f, freqs):
    return w[np.argmin(abs(freqs - f))]


SUSTAINED = dict(a=0, d=0, s=15, r=0)


class WaveTests(unittest.TestCase):
    def test_note_numbers(self):
        self.assertAlmostEqual(note_freq(57), 440.0)
        self.assertAlmostEqual(note_freq(69), 880.0)
        self.assertAlmostEqual(note_freq(0), 16.3516, places=3)

    def test_sine_is_one_clean_peak(self):
        w, f = spectrum(play(Instrument(wave="sine", **SUSTAINED), 440.0)[RATE // 4:])
        self.assertAlmostEqual(f[np.argmax(w)], 440.0, delta=5)
        self.assertLess(np.sort(w)[-20], w.max() * 0.01)

    def test_square_has_odd_harmonics_only_and_quarter_pulse_has_even(self):
        sq = play(Instrument(wave="pulse", pw=50, **SUSTAINED), 440.0)[RATE // 4:]
        w, f = spectrum(sq)
        self.assertLess(level_at(w, 880, f), level_at(w, 440, f) * 0.03)       # no 2nd harmonic
        self.assertGreater(level_at(w, 1320, f), level_at(w, 440, f) * 0.25)    # a 3rd
        q = play(Instrument(wave="pulse", pw=25, **SUSTAINED), 440.0)[RATE // 4:]
        w, f = spectrum(q)
        self.assertGreater(level_at(w, 880, f), level_at(w, 440, f) * 0.2)      # a 2nd harmonic

    def test_saw_harmonics_fall_off(self):
        w, f = spectrum(play(Instrument(wave="saw", **SUSTAINED), 440.0)[RATE // 4:])
        h1, h2, h3 = (level_at(w, 440 * k, f) for k in (1, 2, 3))
        self.assertAlmostEqual(h2 / h1, 0.5, delta=0.1)
        self.assertAlmostEqual(h3 / h1, 1 / 3, delta=0.08)

    def test_band_limiting_keeps_aliases_down(self):
        # 5 kHz saw: harmonics above 22 kHz fold back; with PolyBLEP the folded energy is small
        w, f = spectrum(play(Instrument(wave="saw", **SUSTAINED), 5000.0)[RATE // 4:])
        harm = [5000 * k for k in range(1, 5)]
        peak = max(level_at(w, h, f) for h in harm)
        stray = [level_at(w, x, f) for x in (3300, 7700, 11100, 13300, 16600)]
        self.assertLess(max(stray), peak * 0.08)

    def test_triangle_is_soft_and_noise_follows_pitch(self):
        tri = play(Instrument(wave="tri", **SUSTAINED), 440.0)[RATE // 4:]
        w, f = spectrum(tri)
        self.assertLess(level_at(w, 1320, f), level_at(w, 440, f) * 0.15)

        def centroid(freq):
            w, f = spectrum(play(Instrument(wave="noise", **SUSTAINED), freq)[RATE // 4:])
            return float((w * w * f).sum() / (w * w).sum())
        self.assertGreater(centroid(2000.0), centroid(100.0) * 2)

    def test_noise_is_repeatable_and_modes_differ(self):
        a = play(Instrument(wave="noise", **SUSTAINED), 500.0)
        b = play(Instrument(wave="noise", **SUSTAINED), 500.0)
        self.assertTrue(np.array_equal(a, b))
        c = play(Instrument(wave="noise", nmode="short", **SUSTAINED), 500.0)
        self.assertFalse(np.array_equal(a, c))
        self.assertEqual(len(synth.noise_table("long")), 32767)
        self.assertEqual(len(synth.noise_table("short")), 93)


class EnvelopeTests(unittest.TestCase):
    def peak(self, x, a, b):
        return float(np.abs(x[int(a * RATE):int(b * RATE)]).max())

    def test_sustain_level_and_volume(self):
        full = play(Instrument(wave="sine", a=0, d=0, s=15, r=0), 440.0)
        half = play(Instrument(wave="sine", a=0, d=0, s=8, r=0), 440.0)
        self.assertAlmostEqual(self.peak(half, .5, .9) / self.peak(full, .5, .9), 8 / 15, delta=0.03)
        vol = play(Instrument(wave="sine", vol=7, **SUSTAINED), 440.0)
        self.assertAlmostEqual(self.peak(vol, .5, .9) / self.peak(full, .5, .9), 7 / 15, delta=0.03)
        note = play(Instrument(wave="sine", **SUSTAINED), 440.0, vol=5)
        self.assertAlmostEqual(self.peak(note, .5, .9) / self.peak(full, .5, .9), 5 / 15, delta=0.03)

    def test_slow_attack_ramps_up(self):
        x = play(Instrument(wave="sine", a=12, d=0, s=15, r=0), 440.0, secs=1.5)    # attack 1 s
        early, late = self.peak(x, 0.0, 0.1), self.peak(x, 0.9, 1.0)
        self.assertLess(early, late * 0.25)
        self.assertGreater(self.peak(x, 1.2, 1.5), late * 0.9)

    def test_decay_to_sustain_level(self):
        x = play(Instrument(wave="sine", a=0, d=9, s=5, r=0), 440.0, secs=2.0)      # decay 0.75 s
        self.assertGreater(self.peak(x, 0.0, 0.05), 0.15)
        settled = self.peak(x, 1.5, 2.0) / self.peak(x, 0.0, 0.02)
        self.assertAlmostEqual(settled, 5 / 15, delta=0.06)

    def test_release_dies_away_and_off_before_on_is_silent(self):
        x = play(Instrument(wave="sine", a=0, d=0, s=15, r=9), 440.0, secs=2.0, off_at=0.5)   # release 0.75 s
        self.assertGreater(self.peak(x, 0.4, 0.5), 0.15)
        self.assertGreater(self.peak(x, 0.55, 0.6), self.peak(x, 1.1, 1.2) * 2)
        self.assertLess(self.peak(x, 1.4, 2.0), 0.002)
        self.assertEqual(float(np.abs(Synth().render(1000)).max()), 0.0)

    def test_event_timing(self):
        s = Synth()
        s.schedule(1000, "on", 1, freq=440.0, inst=Instrument(wave="sine", **SUSTAINED))
        x = s.render(3000)
        self.assertEqual(float(np.abs(x[:1000]).max()), 0.0)
        self.assertGreater(float(np.abs(x[1000:1200]).max()), 0.0)


class StreamTests(unittest.TestCase):
    def test_chunked_render_equals_one_render(self):
        def build():
            s = Synth()
            s.schedule(0, "on", 0, freq=330.0, inst=Instrument(wave="pulse", pw=30, a=3, d=5, s=9, r=6))
            s.schedule(500, "on", 1, freq=1200.0, inst=Instrument(wave="noise", **SUSTAINED))
            s.schedule(7000, "off", 0)
            s.schedule(9000, "freq", 1, freq=700.0)
            s.schedule(3000, "dac_start", 0, data=bytes([0xFF, 0x00, 0xAA] * 20), rate=9)
            return s
        whole = build().render(20000)
        s, parts = build(), []
        for n in (1, 332, 1000, 4999, 1, 13667):
            parts.append(s.render(n))
        self.assertTrue(np.allclose(whole, np.concatenate(parts), atol=1e-6))


class PanTests(unittest.TestCase):
    def test_pan_positions(self):
        inst = Instrument(wave="sine", **SUSTAINED)
        left = play(inst, 440.0, 0.2, pan=-8)
        right = play(inst, 440.0, 0.2, pan=8)
        mid = play(inst, 440.0, 0.2, pan=0)
        self.assertLess(float(np.abs(left[:, 1]).max()), 1e-4)
        self.assertLess(float(np.abs(right[:, 0]).max()), 1e-4)
        self.assertAlmostEqual(float(np.abs(mid[:, 0]).max()), float(np.abs(mid[:, 1]).max()), places=4)
        quarter = play(inst, 440.0, 0.2, pan=4)
        self.assertGreater(float(np.abs(quarter[:, 1]).max()), float(np.abs(quarter[:, 0]).max()))

    def test_instrument_pan_is_used_by_default_and_power_is_constant(self):
        x = play(Instrument(wave="sine", pan=-8, **SUSTAINED), 440.0, 0.2)
        self.assertLess(float(np.abs(x[:, 1]).max()), 1e-4)
        p = [synth.pan_gains(k) for k in range(-8, 9)]
        for l, r in p:
            self.assertAlmostEqual(l * l + r * r, 1.0, places=6)


class DacTests(unittest.TestCase):
    def test_delta_decoding_climbs_falls_and_clamps(self):
        up = dpcm_decode(bytes([0xFF] * 40))
        self.assertEqual(list(up[:3]), [66, 68, 70])
        self.assertEqual(float(up.max()), 127.0)
        down = dpcm_decode(bytes([0x00] * 40))
        self.assertEqual(list(down[:2]), [62, 60])
        self.assertEqual(float(down.min()), 0.0)
        self.assertEqual(list(dpcm_decode(bytes([0b00000101]))[:3]), [66, 64, 66])     # least significant bit first

    def test_dac_plays_then_stops_and_holds_a_direct_level(self):
        s = Synth()
        s.schedule(0, "dac_start", 0, data=bytes([0xFF] * 8), rate=15)          # 64 samples at 33.1 kHz
        x = s.render(2000)
        self.assertGreater(float(np.abs(x[:60]).max()), 0.0)
        self.assertEqual(float(np.abs(x[100:]).max()), 0.0)
        s.schedule(s.t, "dac_level", 1, level=100)
        y = s.render(100)
        self.assertAlmostEqual(float(y[50, 0]), (100 - 64) / 64.0 * synth.VOICE_GAIN, places=3)
        s.schedule(s.t, "dac_stop", 1)
        self.assertEqual(float(np.abs(s.render(100)).max()), 0.0)

    def test_two_dacs_pan_apart(self):
        s = Synth()
        s.schedule(0, "dac_start", 0, data=bytes([0xFF] * 30), rate=0, pan=-8)
        s.schedule(0, "dac_start", 1, data=bytes([0xFF] * 30), rate=0, pan=8)
        x = s.render(500)
        self.assertGreater(float(np.abs(x[:, 0]).max()), 0.0)
        self.assertGreater(float(np.abs(x[:, 1]).max()), 0.0)
        s2 = Synth()
        s2.schedule(0, "dac_start", 0, data=bytes([0xFF] * 30), rate=0, pan=-8)
        self.assertLess(float(np.abs(s2.render(500)[:, 1]).max()), 1e-4)


class OutputTests(unittest.TestCase):
    def test_four_loud_voices_do_not_clip_and_pcm_size(self):
        s = Synth()
        for v in range(4):
            s.schedule(0, "on", v, freq=200.0 * (v + 1), inst=Instrument(wave="pulse", pan=0, **SUSTAINED))
        x = s.render(RATE // 2)
        self.assertLess(float(np.abs(x).max()), 1.0)
        self.assertEqual(len(to_pcm16(x)), x.shape[0] * 4)
        self.assertEqual(len(to_pcm16(np.full((3, 2), 5.0, np.float32))), 12)


if __name__ == "__main__":
    unittest.main()
