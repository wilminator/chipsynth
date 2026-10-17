# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

import numpy as np

from chipsynth import mml
from chipsynth.player import TunePlayer
from chipsynth.synth import Instrument, RATE, Synth, note_freq


def play(inst, freq=440.0, seconds=1.0, chunks=None, **kw):
    s = Synth()
    s.schedule(0, "on", 0, freq=freq, inst=inst, vol=15, **kw)
    n = int(seconds * RATE)
    if chunks:
        parts, left = [], n
        while left > 0:
            k = min(left, chunks)
            parts.append(s.render(k))
            left -= k
        return np.concatenate(parts)[:, 0]
    return s.render(n)[:, 0]


def crossings(x):
    return int(np.sum((x[1:] > 0) != (x[:-1] > 0)))


def sine(**kw):
    return Instrument(wave="sine", a=0, d=0, s=15, r=0, vol=15, **kw)


class SynthFxTests(unittest.TestCase):
    def test_no_effect_is_unchanged(self):
        self.assertFalse(sine().has_fx)

    def test_vibrato_widens_the_pitch(self):
        steady = play(sine())
        vib = play(sine(vib_rate=8, vib_depth=15))
        # measure the length of each half cycle: vibrato spreads them
        def spread(x):
            idx = np.nonzero((x[1:] > 0) & (x[:-1] <= 0))[0]
            return np.diff(idx).std()
        self.assertGreater(spread(vib), spread(steady) * 3)

    def test_vibrato_stays_centred(self):
        steady = play(sine(), seconds=2.0)
        vib = play(sine(vib_rate=5, vib_depth=8), seconds=2.0)
        self.assertAlmostEqual(crossings(vib) / crossings(steady), 1.0, delta=0.01)

    def test_arpeggio_steps_through_the_offsets(self):
        beat = RATE // 4                              # one beat = 0.25 s; 4 steps a beat = 1/16 s each
        x = play(sine(arp=(0, 12), arprate=4), seconds=0.5, beat=beat)
        step = beat // 4
        a = crossings(x[:step // 2 * 2 - 200]) / (step - 200)
        b = crossings(x[step + 100:2 * step - 100]) / (step - 200)
        self.assertAlmostEqual(b / a, 2.0, delta=0.15)
        c = crossings(x[2 * step + 100:3 * step - 100]) / (step - 200)
        self.assertAlmostEqual(c / a, 1.0, delta=0.15)

    def test_slide_glides_from_the_old_note(self):
        x = play(sine(), freq=880.0, seconds=0.6, slide_from=440.0, slide_len=RATE // 2)
        early = crossings(x[:2000]) / 2000
        late = crossings(x[-2000:]) / 2000
        self.assertAlmostEqual(late / early, 2.0, delta=0.25)

    def test_slide_ends_on_pitch_and_stays_there(self):
        x = play(sine(), freq=880.0, seconds=1.0, slide_from=220.0, slide_len=RATE // 10)
        self.assertAlmostEqual(crossings(x[RATE // 2:]) / (len(x) - RATE // 2) * RATE / 2, 880.0, delta=15)

    def test_pulse_width_sweep_changes_the_duty(self):
        inst = Instrument(wave="pulse", a=0, d=0, s=15, r=0, pw=50, pws_from=10, pws_to=90, pws_rate=2)
        x = play(inst, freq=200.0, seconds=1.0)
        win = RATE // 10

        def duty(a):
            return float(np.mean(a[:win] > 0))
        duties = [duty(x[i:]) for i in range(0, RATE - win, win)]
        self.assertGreater(max(duties) - min(duties), .4)

    def test_pws_only_affects_pulse(self):
        self.assertFalse(Instrument(wave="saw", pws_from=10, pws_to=90, pws_rate=2).has_fx)
        self.assertTrue(Instrument(wave="pulse", pws_from=10, pws_to=90, pws_rate=2).has_fx)

    def test_chunking_never_changes_the_sound(self):
        for inst in (sine(vib_rate=6, vib_depth=9),
                     Instrument(wave="saw", a=0, d=0, s=15, r=0, arp=(0, 4, 7)),
                     Instrument(wave="pulse", a=0, d=0, s=15, r=0, pws_from=20, pws_to=80, pws_rate=5),
                     Instrument(wave="noise", a=0, d=0, s=15, r=0, arp=(0, 7))):
            whole = play(inst, seconds=0.5, beat=RATE // 3)
            cut = play(inst, seconds=0.5, chunks=777, beat=RATE // 3)
            self.assertTrue(np.array_equal(whole, cut), inst.wave)

    def test_slide_chunking(self):
        a = play(sine(), freq=660.0, seconds=0.5, slide_from=330.0, slide_len=RATE // 4)
        b = play(sine(), freq=660.0, seconds=0.5, chunks=500, slide_from=330.0, slide_len=RATE // 4)
        self.assertTrue(np.array_equal(a, b))


class ParseFxTests(unittest.TestCase):
    def notes(self, text):
        return mml.parse("tempo 120\n" + text).voices[0]

    def test_vibrato_is_sticky_until_off(self):
        n = self.notes("V1: vib 5,3 c8 d8 vib off e8")
        self.assertEqual([(x.inst.vib_rate, x.inst.vib_depth) for x in n], [(5, 3), (5, 3), (0, 0)])

    def test_slide_applies_to_the_next_note_only(self):
        n = self.notes("V1: c8 slide 12 d8 e8")
        self.assertEqual([x.slide for x in n], [0, 12, 0])

    def test_arp_and_arprate(self):
        n = self.notes("V1: arp 0,4,7 arprate 6 c4 arp off d4")
        self.assertEqual((n[0].inst.arp, n[0].inst.arprate), ((0, 4, 7), 6))
        self.assertEqual(n[1].inst.arp, ())

    def test_pws(self):
        n = self.notes("V1: wave pulse pws 25,75,6 c4")
        self.assertEqual((n[0].inst.pws_from, n[0].inst.pws_to, n[0].inst.pws_rate), (25, 75, 6))

    def test_instrument_keys(self):
        t = mml.parse("tempo 120\n[instr]\nwob wave=pulse vib=4,2 slide=6 arp=0,7 arprate=4 pws=20,60,3\nV1: @wob c4")
        i = t.voices[0][0].inst
        self.assertEqual((i.vib_rate, i.vib_depth, i.slide, i.arp, i.arprate, i.pws_rate),
                         (4, 2, 6, (0, 7), 4, 3))
        self.assertEqual(t.voices[0][0].slide, 6)             # an instrument default slide reaches the note

    def test_fx_sets(self):
        t = mml.parse("tempo 120\n[fx]\nwobble vib=5,3 pws=25,75,6\nV1: fx wobble c4 fx off_ c4" .replace("fx off_ c4", "c4"))
        self.assertEqual(t.voices[0][0].inst.vib_rate, 5)
        self.assertEqual(t.voices[0][1].inst.pws_to, 75)

    def test_fx_import_by_header_line_and_missing_set(self):
        with self.assertRaises(mml.MMLError):
            mml.parse("tempo 120\nV1: fx nothing c4")
        with self.assertRaises(mml.MMLError):
            mml.parse("tempo 120\nimport fx.nothing\nV1: c4")

    def test_range_errors_name_the_command(self):
        for bad, word in (("vib 16,1", "vib"), ("vib 5", "vib"), ("arp 0,30", "arp"), ("arp 1,2,3,4,5,6,7,8,9", "arp"),
                          ("pws 0,50,3", "pws"), ("slide 500", "slide"), ("arprate 0", "arprate")):
            with self.assertRaises(mml.MMLError, msg=bad) as cm:
                mml.parse("tempo 120\nV1: %s c4" % bad)
            self.assertIn(word, str(cm.exception))

    def test_pws_is_not_mistaken_for_pw(self):
        n = self.notes("V1: pw 30 pws 10,90,4 c4")
        self.assertEqual(n[0].inst.pw, 30)
        self.assertEqual(n[0].inst.pws_to, 90)

    def test_library_fx(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        (d / "fx.mml").write_text("[fx]\nshimmer vib=3,2\n")
        lib = mml.load_library(d)
        t = mml.parse("tempo 120\nV1: fx shimmer c4", lib)
        self.assertEqual(t.voices[0][0].inst.vib_rate, 3)


class PlayerFxTests(unittest.TestCase):
    def test_slide_reaches_the_synth_with_the_previous_pitch(self):
        t = mml.parse("tempo 120\nV1: @lead c4 slide 24 g4")
        p = TunePlayer(t)
        row = p._notes[0]
        self.assertIsNone(row[0][6])
        extra = row[1][6]
        self.assertAlmostEqual(extra["slide_from"], note_freq(48), places=3)
        # 24/48 of a quarter note at 120 bpm = 0.25 s
        self.assertAlmostEqual(extra["slide_len"] / RATE, .25, delta=.01)

    def test_a_first_note_has_nothing_to_glide_from(self):
        t = mml.parse("tempo 120\nV1: slide 24 c4")
        self.assertNotIn("slide_from", TunePlayer(t)._notes[0][0][6])

    def test_effects_render_and_differ_from_plain(self):
        plain = TunePlayer(mml.parse("tempo 120\nV1: @flute c1")).render(RATE)
        vib = TunePlayer(mml.parse("tempo 120\nV1: @flute vib 8,15 c1")).render(RATE)
        self.assertFalse(np.array_equal(plain, vib))
        self.assertTrue(np.isfinite(vib).all())


if __name__ == "__main__":
    unittest.main()
