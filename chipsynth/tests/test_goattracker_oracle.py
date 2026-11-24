# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The GoatTracker exporter against GoatTracker's own player (see gt_oracle.py).  Skipped unless `gt2reloc` and `py65` are there.

Each song is exported, packed and played by the real 6502 player; the notes that come out must start where chipsynth starts
them (after the player's fixed pre-roll), with the right pitch.  The songs are made here from MML."""
import math
import unittest

from chipsynth.formats import goattracker_export as ex
from chipsynth.song import Song
from chipsynth.tests import gt_oracle as O

SONGS = {
    "melody": "tempo 125\n[instr]\nl wave=pulse pw=25 a=0 d=6 s=8 r=3\nV1: @l l8 c d e f g2 r4 c4\n",
    "chord": "tempo 125\n[instr]\nl wave=saw a=0 d=6 s=8 r=3\nb wave=tri a=1 d=4 s=10 r=2\nV1: @l l4 c e g c\nV2: @l o3 l4 c e g c\nV3: @b o2 l4 c e g c\n",
    "rests": "tempo 125\n[instr]\nl wave=saw a=0 d=6 s=8 r=3\nV1: @l l8 c r d r4 e r8 f\n",
}


@unittest.skipUnless(O.available(), "needs GoatTracker's gt2reloc (CHIPSYNTH_GT2RELOC) and py65")
class RealPlayerTests(unittest.TestCase):
    def play(self, text, region, mult, seconds=4.0):
        tune = Song.from_mml(text).compile()
        data, _rep = ex.dump(tune, region=region, multispeed=mult)
        calls = int(seconds * O.HZ[region] * mult)
        return tune, O.trace(O.pack(data, region, mult), calls, mult)

    def test_onsets_and_pitches_match_chipsynths_own_timing(self):
        for name, text in SONGS.items():
            for region, mult in (("pal", 1), ("pal", 2), ("ntsc", 1)):
                tune, tr = self.play(text, region, mult)
                sharp = 12 * math.log2(O.CLOCK["ntsc"] / O.CLOCK["pal"]) if region == "ntsc" else 0     # the table is PAL-tuned
                for v in range(3):
                    want = O.planned(tune, region, mult, v)
                    got = O.gates(tr, v, region)
                    self.assertGreaterEqual(len(got), len(want), (name, region, mult, v))
                    for w, g in zip(want, got):
                        at = (name, region, mult, v, w)
                        self.assertAlmostEqual(g["on"] - w[0], O.PREROLL(mult), delta=1.0, msg=at)
                        self.assertAlmostEqual(g["pitch"], w[1] + sharp, delta=0.2, msg=at)

    def test_the_first_row_writes_a_frequency(self):
        # the headline bug: a first wavetable row of 0x80 kept the frequency, so every voice stayed at 0
        _, tr = self.play(SONGS["melody"], "pal", 1)
        self.assertTrue(any(r[0] | r[1] << 8 for r in tr))

    def test_the_loop_repeats_at_chipsynths_period(self):
        text = "tempo 125\n[instr]\nl wave=saw a=0 d=6 s=8 r=3\nV1: @l l8 c d loop e f g a\n"
        tune = Song.from_mml(text).compile()
        data, _rep = ex.dump(tune)
        tr = O.trace(O.pack(data), 500)
        ons = [g["on"] for g in O.gates(tr, 0)]
        n = len(tune.voices[0])
        first = [k for k, x in enumerate(tune.voices[0]) if x.start >= tune.loop_tick][0]
        self.assertGreater(len(ons), n + 1)
        period = (tune.tick_to_sample(tune.length) - tune.tick_to_sample(tune.loop_tick)) / 44100.0 * 50
        self.assertAlmostEqual(ons[n] - ons[first], period, delta=1.0)


if __name__ == "__main__":
    unittest.main()
