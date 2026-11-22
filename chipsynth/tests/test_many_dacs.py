# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""A song with more than two DAC lanes (`dacs N`, up to 8; the MIDI drum import makes them): the composer's roll and
tracker, the audio and MIDI renderers and the FamiStudio exporter must not crash, and the NES-style exporter must keep
dropping the extra lanes with its report line.  (There is no GoatTracker or FamiTracker exporter: both are import only,
and an imported GoatTracker song has no DACs at all.)"""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import numpy as np
import pygame

from chipsynth import exporter, midi, package as pk
from chipsynth.composer import model, tracker
from chipsynth.composer.app import Composer
from chipsynth.composer.prefs import Prefs
from chipsynth.formats import famistudio_export as ex, famistudio_text as fam
from chipsynth.song import Song
from chipsynth.__main__ import main

KIT = ["kit_kick", "kit_snare", "kit_hat", "kit_crash", "kit_tom", "kit_tomlo", "kit_ohat", "kit_kick"]


def many(n, bars="r4 {s}4 r2"):
    """V1 holding a note and n DAC lanes, each playing a different kit sample on its own beat."""
    used = sorted(set(KIT[:n]))
    text = "dacs %d\n[sample]\n%s" % (n, "".join("%s = %s\n" % (k, k) for k in used))
    text += "V1: c1\n"
    for i in range(n):
        text += "D%d: %s\n" % (i + 1, " ".join(["r4"] * (i % 4) + ["v15 %s4" % KIT[i], "r4"]))
    return Song.from_mml(text)


class ModelTests(unittest.TestCase):
    def test_the_roll_the_tracker_and_the_mixdown_cover_every_lane(self):
        for n in (3, 5, 8):
            tune = many(n).compile()
            self.assertEqual(len(tune.dac), n)
            roll = model.Roll.from_tune(tune)
            self.assertEqual((roll.nv, roll.nd, roll.lanes), (4, n, 4 + n))
            self.assertEqual([len(roll.dac[i]) for i in range(n)], [1] * n)
            for col in range(roll.lanes):
                cells = tracker.column_cells(roll, col, 96, 0, 8)
                self.assertEqual(len(cells), 8)
            for i in range(n):
                self.assertTrue(any(kind == "start" for _, kind in tracker.column_cells(roll, 4 + i, 96, 0, 8)), i)
            self.assertEqual(model.lane_names(4, n)[-1], "D%d" % n)
            quiet = model.mixdown(tune, {4 + k for k in range(2, n)})              # mute D3 and up
            self.assertEqual([bool(d) for d in quiet.dac], [True, True] + [False] * (n - 2))
            self.assertEqual(len(model.tune_from(tune, 96).dac), n)


class RenderTests(unittest.TestCase):
    def test_the_audio_renderer_plays_the_extra_lanes(self):
        two, five = many(2).compile(), many(5).compile()
        a2, *_ = exporter.render_audio(two)
        a5, *_ = exporter.render_audio(five)
        self.assertEqual(a2.shape[1], 2)
        self.assertGreater(float(np.abs(a5).max()), 0.0)
        # D3-D5 hold the hat, crash and tom: the five-lane render is not the two-lane one
        n = min(len(a2), len(a5))
        self.assertFalse(np.allclose(a2[:n], a5[:n]))

    def test_wav_and_midi_export_through_the_package_route(self):
        d = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="m")
        pkg.add_song("m", many(8), {})
        self.assertEqual(exporter.export_song(pkg, "m", d / "m.wav").written, [d / "m.wav"])
        r = exporter.export_song(pkg, "m", d / "m.mid")
        self.assertEqual(r.notes, [])
        _, tracks = midi.parse((d / "m.mid").read_bytes())
        notes = sorted(a for t in tracks for _, kind, a, _, ch in t if kind == "on" and ch == 9)
        self.assertEqual(notes, sorted([36, 38, 42, 49, 47, 41, 46, 36]))             # D8 repeats the kick, a beat later

    def test_midi_reports_the_samples_without_a_gm_note_on_any_lane(self):
        text = "dacs 4\n[sample]\nkit_hey = kit_hey\nkit_kick = kit_kick\nV1: c1\nD4: kit_hey4 kit_kick4\n"
        hits, lines = midi.drum_hits(Song.from_mml(text).compile())
        self.assertEqual([h[1] for h in hits], [36])
        self.assertEqual(len(lines), 1)
        self.assertIn("kit_hey", lines[0])


class NesExportTests(unittest.TestCase):
    def test_famistudio_keeps_d1_and_drops_the_other_lanes_with_the_report_line(self):
        for n in (3, 8):
            tune = many(n).compile()
            text, rep = ex.dump(tune)
            self.assertEqual([m for m in rep.dropped if m.startswith("DAC tracks")],
                             ["DAC tracks D2 and up (FamiStudio has one DPCM channel)"], n)
            back = fam.load(text)                                                  # the file still reads
            self.assertEqual(len(back.songs), 1)

    def test_a_lane_above_d2_alone_is_still_reported(self):
        text = "dacs 3\n[sample]\nkit_kick = kit_kick\nV1: c1\nD3: kit_kick4\n"
        _, rep = ex.dump(Song.from_mml(text).compile())
        self.assertTrue(any(m.startswith("DAC tracks D2 and up") for m in rep.dropped))

    def test_two_lanes_report_the_same_line_as_before(self):
        _, rep = ex.dump(many(2).compile())
        self.assertTrue(any(m.startswith("DAC tracks D2 and up") for m in rep.dropped))
        _, rep = ex.dump(many(1).compile())
        self.assertFalse(any(m.startswith("DAC tracks") for m in rep.dropped))

    def test_the_export_command_line_names_the_dropped_lanes(self):
        d = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="m")
        pkg.add_song("m", many(5), {})
        pk.save(pkg, d / "m.csp")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["export-song", str(d / "m.csp"), "-o", str(d / "m.txt"), "-f", "famistudio"]), 0)
        self.assertTrue((d / "m.txt").exists())
        self.assertIn("DAC tracks D2 and up", err.getvalue())


class ComposerTests(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())

    def make(self, view, n):
        a = Composer(self.screen, Prefs(view=view), prefs_path=self.dir / "prefs.json", root=self.dir)
        a.frame()
        self.assertTrue(a.load_song(many(n), "Many"))
        a.frame()
        return a

    def test_the_roll_and_the_tracker_draw_and_pick_every_lane_without_crashing(self):
        for view in ("roll", "tracker"):
            for n in (3, 8):
                a = self.make(view, n)
                self.assertEqual(a.roll.nd, n)
                for k in range(pygame.K_1, pygame.K_9 + 1):                        # pick each lane in turn
                    a.frame([pygame.event.Event(pygame.KEYDOWN, key=k, mod=0, unicode=chr(k))])
                    a.frame()
                for key in (pygame.K_DOWN, pygame.K_RIGHT, pygame.K_TAB, pygame.K_END, pygame.K_HOME):
                    a.frame([pygame.event.Event(pygame.KEYDOWN, key=key, mod=0, unicode="")])
                a.play()
                a.frame()

    def test_exports_of_a_many_lane_song_from_the_composer(self):
        a = self.make("roll", 5)
        for fmt, name in (("wav", "o.wav"), ("mid", "o.mid"), ("famistudio", "o.txt"), ("mml", "o.mml")):
            self.assertTrue(a.export_as(fmt, self.dir / name), fmt)
        a.export_as("famistudio", self.dir / "again.txt")
        self.assertTrue(any("DAC tracks D2 and up" in x for x in a.last_report[1]), a.last_report)
        self.assertIn("1 left out", a.message)
        a.export_as("mid", self.dir / "again.mid")
        self.assertEqual(a.last_report[1], ["note: nothing was changed or left out"])


if __name__ == "__main__":
    unittest.main()
