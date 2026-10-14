# Copyright (C) 2026 Michael Allen Wilmes
"""Note editing on a Song, with undo/redo (PLAN-CHIPSYNTH.md, C2)."""
import random
import unittest

import numpy as np

from chipsynth import dpcm, mml, musicgen
from chipsynth.song import Song
from chipsynth.songedit import Editor, EditError


def notes(ed, v=0):
    return [(n.start, n.dur, n.pitch) for n in ed.tune.voices[v]]


def make(text):
    return Editor(Song.from_mml(text))


class AddEraseTests(unittest.TestCase):
    def test_add_past_end_and_into_rest(self):
        ed = make("V1: o4 c4 r2 e4")
        ed.add_note(0, 96 * 2, 96, 67)                 # into the rest
        self.assertEqual(notes(ed), [(0, 96, 48), (192, 96, 67), (288, 96, 52)])
        ed.add_note(0, 96 * 5, 96, 60)                 # past the end, with a gap
        self.assertEqual(notes(ed)[-1], (480, 96, 60))

    def test_no_room_changes_nothing(self):
        ed = make("V1: o4 c4 d4")
        before = ed.song.to_mml()
        with self.assertRaises(EditError):
            ed.add_note(0, 48, 96, 60)
        self.assertEqual(ed.song.to_mml(), before)
        self.assertFalse(ed.can_undo())

    def test_erase_keeps_timing(self):
        ed = make("V1: o4 c4 d4 e4")
        ed.erase(0, 1)
        self.assertEqual(notes(ed), [(0, 96, 48), (192, 96, 52)])
        self.assertEqual(ed.tune.length, 288)

    def test_pitch_outside_octave_shifts_only_that_note(self):
        ed = make("V1: o4 c4 d4 e4")
        ed.set_pitch(0, 1, 74)
        self.assertEqual([p for _, _, p in notes(ed)], [48, 74, 52])
        again = Song.from_mml(ed.song.to_mml()).compile()
        self.assertEqual(notes(ed), [(n.start, n.dur, n.pitch) for n in again.voices[0]])

    def test_octave_shifts_do_not_pile_up(self):
        ed = make("V1: o4 c4 r4 e4")
        ed.set_pitch(0, 0, 61)
        ed.set_pitch(0, 0, 49)
        ed.set_pitch(0, 0, 61)
        ed.move(0, 0, 48, 61)                          # the rest left behind joins its neighbour
        self.assertEqual(notes(ed)[0], (48, 96, 61))
        self.assertEqual(sum(type(e).__name__ == "Octave" for e in ed.song.tracks["V1"]), 3)

    def test_degrees_stay_degrees(self):
        ed = make("V1: key c major o4 1 2 3")
        ed.set_pitch(0, 1, 55)
        self.assertEqual([p for _, _, p in notes(ed)], [48, 55, 52])
        self.assertIn("5", ed.song.to_mml().split("V1:")[1])


class ResizeMoveTests(unittest.TestCase):
    def test_shrink_leaves_a_rest(self):
        ed = make("V1: o4 c2 d4")
        ed.resize(0, 0, 96)
        self.assertEqual(notes(ed), [(0, 96, 48), (192, 96, 50)])

    def test_grow_eats_rest_but_stops_at_next_note(self):
        ed = make("V1: o4 c4 r4 d4")
        self.assertEqual(ed.max_dur(0, 0), 192)
        ed.resize(0, 0, 192)
        self.assertEqual(notes(ed), [(0, 192, 48), (192, 96, 50)])
        with self.assertRaises(EditError):
            ed.resize(0, 0, 200)

    def test_grow_last_note_freely(self):
        ed = make("V1: o4 c4")
        ed.resize(0, 0, 384)
        self.assertEqual(notes(ed), [(0, 384, 48)])

    def test_resize_a_tied_note(self):
        ed = make("V1: o4 c4&4 d4")
        ed.resize(0, 0, 96)
        self.assertEqual(notes(ed), [(0, 96, 48), (192, 96, 50)])
        ed.undo()
        self.assertEqual(notes(ed), [(0, 192, 48), (192, 96, 50)])

    def test_move(self):
        ed = make("V1: o4 c4 r2 d4")
        ed.move(0, 0, 96 * 2, 55)
        self.assertEqual(notes(ed), [(192, 96, 55), (288, 96, 50)])
        with self.assertRaises(EditError):
            ed.move(0, 1, 192, 60)                     # onto the other note
        self.assertEqual(notes(ed), [(192, 96, 55), (288, 96, 50)])

    def test_unwritable_length_refused(self):
        ed = make("V1: o4 c2 r2")
        with self.assertRaises(EditError):
            ed.resize(0, 0, 5)

    def test_transpose_all_or_nothing(self):
        ed = make("V1: o4 c4 d4")
        ed.transpose([(0, 0), (0, 1)], 12)
        self.assertEqual([p for _, _, p in notes(ed)], [60, 62])
        with self.assertRaises(EditError):
            ed.transpose([(0, 0), (0, 1)], 100)
        self.assertEqual([p for _, _, p in notes(ed)], [60, 62])


class PatternTests(unittest.TestCase):
    TEXT = "[pat]\nriff = c4 d4 e4 r4\nV1: o4 !riff !riff\nV2: o3 !riff\n"

    def test_editing_a_pattern_changes_every_use(self):
        ed = make(self.TEXT)
        self.assertEqual(ed.shared(0, 0), 2)
        ed.set_pitch(0, 1, 55)
        self.assertEqual([p for _, _, p in notes(ed)][:4], [48, 55, 52, 48])
        self.assertEqual([p for _, _, p in notes(ed)][4], 55)
        self.assertEqual(ed.tune.voices[1][1].pitch, 55 - 12)       # the pattern follows each caller's octave


class HistoryTests(unittest.TestCase):
    def test_undo_redo_and_dirty(self):
        ed = make("V1: o4 c4 d4")
        base = ed.song.to_mml()
        self.assertFalse(ed.dirty)
        ed.add_note(0, 192, 96, 60)
        ed.erase(0, 0)
        self.assertTrue(ed.dirty)
        self.assertEqual(ed.undo(), "erase note")
        self.assertEqual(ed.undo(), "add note")
        self.assertEqual(ed.song.to_mml(), base)
        self.assertFalse(ed.dirty)
        self.assertEqual(ed.redo(), "add note")
        self.assertTrue(ed.dirty)
        ed.mark_saved()
        self.assertFalse(ed.dirty)
        ed.erase(0, 0)
        self.assertTrue(ed.dirty)
        ed.redo()
        self.assertFalse(ed.redo())

    def test_new_edit_clears_redo(self):
        ed = make("V1: o4 c4")
        ed.add_note(0, 96, 96, 60)
        ed.undo()
        self.assertTrue(ed.can_redo())
        ed.add_note(0, 96, 96, 62)
        self.assertFalse(ed.can_redo())


class InstrumentTests(unittest.TestCase):
    TEXT = "V1: @lead o4 c4 d4\nV2: @lead o3 c4\nV3: o3 c4\n"

    def test_edit_changes_every_voice_using_it_and_survives_mml(self):
        ed = make(self.TEXT)
        self.assertEqual(ed.voice_instrument(0), "lead")
        self.assertIsNone(ed.voice_instrument(2))
        ed.edit_instrument("lead", a=3, filt="lp", filt_cut=5, wave="saw")
        for v in (0, 1):
            i = ed.tune.voices[v][0].inst
            self.assertEqual((i.a, i.filt, i.filt_cut, i.wave), (3, "lp", 5, "saw"))
        back = Song.from_mml(ed.song.to_mml()).compile()
        self.assertEqual(back.voices[1][0].inst, ed.tune.voices[1][0].inst)

    def test_bad_values_are_refused_and_change_nothing(self):
        ed = make(self.TEXT)
        for bad in ({"vol": 16}, {"wave": "zip"}, {"nope": 1}, {"pan": -9}):
            with self.assertRaises(EditError):
                ed.edit_instrument("lead", **bad)
        with self.assertRaises(EditError):
            ed.edit_instrument("nosuch", vol=1)
        self.assertFalse(ed.can_undo())

    def test_own_copy_leaves_the_other_voice_alone(self):
        ed = make(self.TEXT)
        name = ed.copy_instrument(1)
        self.assertEqual(ed.voice_instrument(1), name)
        ed.edit_instrument(name, vol=3)
        self.assertEqual(ed.tune.voices[1][0].inst.vol, 3)
        self.assertNotEqual(ed.tune.voices[0][0].inst.vol, 3)

    def test_editing_a_voice_with_no_instrument_makes_its_own_in_one_step(self):
        ed = make(self.TEXT)
        ed.edit_voice_instrument(2, vol=7, wave="tri")
        self.assertEqual(ed.tune.voices[2][0].inst.vol, 7)
        self.assertEqual(ed.voice_instrument(2), "voice3")
        self.assertEqual(ed.undo(), "edit instrument")
        self.assertIsNone(ed.voice_instrument(2))
        self.assertFalse(ed.can_undo())

    def test_pick_another_instrument(self):
        ed = make(self.TEXT)
        ed.set_voice_instrument(2, "bass")
        self.assertEqual(ed.voice_instrument(2), "bass")
        ed.set_voice_instrument(0, "bell")
        self.assertEqual(ed.voice_instrument(0), "bell")
        self.assertEqual(ed.voice_instrument(1), "lead")
        with self.assertRaises(EditError):
            ed.set_voice_instrument(0, "nosuch")
        ed.undo(); ed.undo()
        self.assertIsNone(ed.voice_instrument(2))


def kick():
    t = np.arange(3000) / 44100.0
    return dpcm.convert(np.sin(2 * np.pi * 90 * t) * np.exp(-t * 30), 44100, rate=12, name="kick")


def hits(ed, lane=0):
    return [e[0] for e in ed.tune.dac[lane] if e[1] == "play"]


class DacTests(unittest.TestCase):
    def make(self, text="V1: c4 d4 e4 f4\n"):
        lib = mml.builtin_library()
        lib["_mem"] = {"kick": kick()}
        return Editor(Song.from_mml(text, lib), lib)

    def test_import_names_and_lists_the_sample(self):
        ed = self.make()
        self.assertIn("kick", ed.sample_names())
        ed.import_sample("snare", kick())
        self.assertIn("snare", ed.sample_names())
        self.assertIn("snare", ed.song.samples)
        with self.assertRaises(EditError):
            ed.import_sample("bad name!", kick())

    def test_add_move_erase_hits(self):
        ed = self.make()
        ed.add_hit(4, 0, "kick", 96)
        ed.add_hit(4, 192, "kick", 96)
        self.assertEqual(hits(ed), [0, 192])
        ed.move_hit(4, 192, 96)
        self.assertEqual(hits(ed), [0, 96])
        with self.assertRaises(EditError):
            ed.move_hit(4, 96, 0)                      # onto the other hit
        ed.erase_hit(4, 0)
        self.assertEqual(hits(ed), [96])
        back = Song.from_mml(ed.song.to_mml(), ed.lib).compile(ed.lib)
        self.assertEqual([e[0] for e in back.dac[0] if e[1] == "play"], [96])

    def test_a_second_lane_and_undo(self):
        ed = self.make()
        ed.add_hit(5, 96 * 3, "kick", 48)
        self.assertEqual(hits(ed, 1), [288])
        ed.undo()
        self.assertEqual(hits(ed, 1), [])

    def test_hits_in_a_pattern_and_unknown_sample(self):
        ed = self.make("[pat]\np = kick4 r4\nD1: !p !p\nV1: c4\n[sample]\nkick\n")
        self.assertEqual(hits(ed), [0, 192])
        ed.set_hit_sample(4, 0, "kick")
        with self.assertRaises(EditError):
            ed.set_hit_sample(4, 0, "nosuch")
        self.assertEqual(hits(ed), [0, 192])


class FuzzTests(unittest.TestCase):
    def run_fuzz(self, text, seed, steps=40):
        rnd = random.Random(seed)
        ed = make(text)
        for _ in range(steps):
            v = rnd.randrange(4)
            n = len(ed.tune.voices[v])
            op = rnd.choice(("add", "erase", "pitch", "resize", "move", "undo"))
            before = [list(x) for x in ed.tune.voices]
            try:
                if op == "undo":
                    ed.undo()
                    continue
                grid = rnd.choice((24, 48, 96))
                if op == "add":
                    ed.add_note(v, rnd.randrange(0, 40) * grid, grid, rnd.randrange(30, 90))
                elif n:
                    i = rnd.randrange(n)
                    if op == "erase":
                        ed.erase(v, i)
                    elif op == "pitch":
                        ed.set_pitch(v, i, rnd.randrange(12, 100))
                    elif op == "resize":
                        ed.resize(v, i, rnd.randrange(1, 12) * 24)
                    else:
                        ed.move(v, i, rnd.randrange(0, 40) * grid, rnd.randrange(30, 90))
            except EditError:
                self.assertEqual([list(x) for x in ed.tune.voices], before)      # a refused edit changes nothing
                continue
            # what the song says is what it plays, and it survives a trip through MML
            back = Song.from_mml(ed.song.to_mml()).compile()
            for k in range(4):
                self.assertEqual([(a.start, a.dur, a.pitch) for a in back.voices[k]],
                                 [(a.start, a.dur, a.pitch) for a in ed.tune.voices[k]], (seed, op))
        while ed.can_undo():
            ed.undo()
        self.assertEqual(ed.song.to_mml(), make(text).song.to_mml())

    def test_fuzz_hand_written(self):
        text = "tempo 120\n[pat]\nx = c4 d4&8 e8\nV1: o4 l8 c d e f g4 r4 o5 !x\nV2: o3 c2 !x r4\nV3: key c major o4 1 2 3 4\n"
        for seed in range(12):
            self.run_fuzz(text, seed)

    def test_fuzz_generated(self):
        for seed in range(3):
            for kind in ("town", "fight", "boss"):
                self.run_fuzz(musicgen.generate(kind, seed), seed, steps=25)


if __name__ == "__main__":
    unittest.main()
