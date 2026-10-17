# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

import numpy as np

from chipsynth import mml, player as music
from chipsynth.synth import RATE
from chipsynth.synthfx import Bus, Echo, Reverb


def tune(text):
    return mml.parse("tempo 120\n" + text)


def impulse(n=RATE):
    x = np.zeros((n, 2), dtype=np.float32)
    x[0] = 1.0
    return x


class EchoTests(unittest.TestCase):
    def test_repeats_at_delay_and_decays_by_feedback(self):
        e = Echo(0.1, 0.5, 1.0)
        y = e.process(impulse())
        d = e.d
        self.assertAlmostEqual(float(y[d, 0]), 1.0 * 1.0, places=5)
        self.assertAlmostEqual(float(y[2 * d, 0]), 0.5, places=5)
        self.assertAlmostEqual(float(y[3 * d, 0]), 0.25, places=5)
        self.assertEqual(float(y[0, 0]), 0.0)                      # the dry signal is not in the wet output

    def test_mix_scales(self):
        a = Echo(0.1, 0.5, 1.0).process(impulse())
        b = Echo(0.1, 0.5, 0.25).process(impulse())
        np.testing.assert_allclose(b, a * 0.25, atol=1e-6)

    def test_wide_ping_pongs(self):
        x = np.zeros((RATE, 2), dtype=np.float32)
        x[0, 0] = 1.0
        y = Echo(0.1, 0.5, 1.0, wide=True).process(x)
        d = int(round(0.1 * RATE))
        self.assertAlmostEqual(float(y[d, 1]), 1.0, places=5)
        self.assertAlmostEqual(float(y[2 * d, 0]), 0.5, places=5)
        self.assertEqual(float(y[d, 0]), 0.0)

    def test_chunking_is_exact(self):
        rng = np.random.default_rng(1)
        x = rng.standard_normal((30000, 2)).astype(np.float32)
        whole = Echo(0.02, 0.6, 0.5).process(x)
        e = Echo(0.02, 0.6, 0.5)
        parts = np.concatenate([e.process(x[a:b]) for a, b in ((0, 7), (7, 900), (900, 901), (901, 20000), (20000, 30000))])
        np.testing.assert_allclose(parts, whole, atol=1e-6)


class ReverbTests(unittest.TestCase):
    def test_has_a_tail_and_bigger_rings_longer(self):
        x = impulse(RATE * 2)
        small = Reverb(2).process(x)
        big = Reverb(15).process(x)
        self.assertGreater(float(np.abs(small[2000:]).max()), 0)
        late = slice(RATE, RATE * 2)
        self.assertGreater(float(np.abs(big[late]).sum()), float(np.abs(small[late]).sum()))

    def test_chunking_is_exact(self):
        rng = np.random.default_rng(2)
        x = rng.standard_normal((20000, 2)).astype(np.float32)
        whole = Reverb(8).process(x)
        r = Reverb(8)
        parts = np.concatenate([r.process(x[a:b]) for a, b in ((0, 5), (5, 1117), (1117, 9000), (9000, 20000))])
        np.testing.assert_allclose(parts, whole, atol=1e-5)

    def test_stereo_is_decorrelated(self):
        y = Reverb(8).process(impulse(RATE))
        self.assertFalse(np.allclose(y[:, 0], y[:, 1]))

    def test_bus_with_nothing_is_silent(self):
        np.testing.assert_array_equal(Bus().process(impulse(100)), np.zeros((100, 2), dtype=np.float32))


class ParseTests(unittest.TestCase):
    def test_echo_and_reverb_header(self):
        t = tune("echo 240ms,40,30\nreverb 6\nV1: c")
        self.assertEqual(t.echo, (0.24, 0.4, 0.3, False))
        self.assertEqual(t.reverb, 6)

    def test_echo_wide_and_off(self):
        self.assertTrue(tune("echo 300,50,20,wide\nV1: c").echo[3])
        self.assertIsNone(tune("echo off\nV1: c").echo)
        self.assertEqual(tune("reverb off\nV1: c").reverb, 0)

    def test_defaults_are_dry(self):
        t = tune("V1: c")
        self.assertIsNone(t.echo)
        self.assertEqual(t.reverb, 0)

    def test_range_errors(self):
        for bad in ("echo 10ms,40,30", "echo 2000ms,40,30", "echo 240ms,95,30", "echo 240ms,40,130", "echo 240ms,40",
                    "reverb 16", "reverb x"):
            with self.assertRaises(mml.MMLError, msg=bad):
                tune(bad + "\nV1: c")

    def test_send_defaults_and_drums(self):
        t = tune("V1: c\nV2: instr kick c")
        self.assertEqual(t.voices[0][0].inst.send, 15)
        self.assertEqual(t.voices[1][0].inst.send, 0)

    def test_send_command_and_instrument_key_and_fx(self):
        t = tune("[instr]\nsoft wave=sine send=4\n[fx]\nwet send=9\nV1: send 7 c fx wet d\nV2: instr soft c")
        self.assertEqual(t.voices[0][0].inst.send, 7)
        self.assertEqual(t.voices[0][1].inst.send, 9)
        self.assertEqual(t.voices[1][0].inst.send, 4)

    def test_send_range(self):
        with self.assertRaises(mml.MMLError):
            tune("V1: send 16 c")


class PlayerTests(unittest.TestCase):
    TEXT = "echo 200ms,50,50\nreverb 6\nV1: l8 c e g c r2 r1\n"

    def render(self, text, sizes):
        p = music.TunePlayer(tune(text))
        return np.concatenate([p.render(n) for n in sizes])

    def test_chunking_is_exact(self):
        a = self.render(self.TEXT, [RATE * 3])
        b = self.render(self.TEXT, [1000, 333, 5000, 44100, RATE * 3 - 50433])
        np.testing.assert_allclose(a, b, atol=1e-6)

    def test_bus_adds_a_tail_after_the_notes(self):
        dry = self.render("V1: c8 r1 r1\n", [RATE * 3])
        wet = self.render("echo 200ms,50,50\nV1: c8 r1 r1\n", [RATE * 3])
        late = slice(RATE * 2, RATE * 3)
        self.assertLess(float(np.abs(dry[late]).max()), 1e-3)
        self.assertGreater(float(np.abs(wet[1000:RATE]).max()), float(np.abs(dry[1000:RATE]).max()) * 0)
        self.assertFalse(np.allclose(wet[:RATE], dry[:RATE]))

    def test_send_zero_is_dry(self):
        dry = self.render("V1: c8 r1\n", [RATE * 2])
        zero = self.render("echo 200ms,50,50\nreverb 8\nV1: send 0 c8 r1\n", [RATE * 2])
        np.testing.assert_allclose(dry, zero, atol=1e-6)

    def test_tail_rings_over_the_loop_restart(self):
        def second_pass(text):
            p = music.TunePlayer(tune(text))
            n = p._pass_len
            return p.render(n * 2)[n + RATE // 2:n + RATE]
        # the note is the last thing in the pass; the second pass opens with rests, so only the tail can be heard
        wet = second_pass("echo 400ms,70,80\nV1: loop r4 r4 r4 c4\n")
        dry = second_pass("V1: loop r4 r4 r4 c4\n")
        self.assertLess(float(np.abs(dry).max()), 1e-4)
        self.assertGreater(float(np.abs(wet).max()), 1e-3)

    def test_no_bus_when_unset(self):
        self.assertIsNone(music.TunePlayer(tune("V1: c")).synth.bus)


if __name__ == "__main__":
    unittest.main()
