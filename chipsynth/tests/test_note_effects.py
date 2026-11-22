# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-note effects (vibrato, arpeggio, slide, pulse sweep) and `key` changes as Editor operations, and that they come
back unchanged through MML export and import (the composer's note panel is built on these)."""
import random
import unittest
from pathlib import Path

from chipsynth import musicgen
from chipsynth.song import Song
from chipsynth.songedit import Editor, EditError, FX_OFF, KeyMark, fx_marks, _fx_norm, _fx_raw


def make(text):
    return Editor(Song.from_mml(text))


def fx(ed, v=0):
    """What every note of voice v plays with, in order (the sounding effects only)."""
    return [_fx_norm(_fx_raw(n)) for n in ed.tune.voices[v]]


def pitches(ed, v=0):
    return [(n.start, n.dur, n.pitch) for n in ed.tune.voices[v]]


def same_after_round_trip(test, ed):
    """Export the song as MML, read it back, and compile: the notes and every note's effects must be the same."""
    back = Song.from_mml(ed.song.to_mml()).compile()
    for v in range(ed.song.voices):
        test.assertEqual([(n.start, n.dur, n.pitch) for n in back.voices[v]], pitches(ed, v))
        test.assertEqual([_fx_norm(_fx_raw(n)) for n in back.voices[v]], fx(ed, v))


TEXT = "V1: o4 l8 c d e f g a b >c\nV2: o3 l4 c e g c\n"
OFF = _fx_norm((0, 0, (), 8, 0, 0, 0, 0))


class EffectTests(unittest.TestCase):
    def test_one_note_gets_an_effect_and_its_neighbours_do_not(self):
        ed = make(TEXT)
        self.assertEqual(ed.set_note_effects([(0, 96)], vib=(5, 3)), 1)
        got = fx(ed)
        self.assertEqual([i for i, x in enumerate(got) if x != OFF], [2])
        self.assertEqual(got[2][:2], (5, 3))
        self.assertEqual(ed.note_effects(0, 2)["vib"], (5, 3))
        self.assertEqual(ed.note_effects(0, 3)["vib"], (0, 0))
        self.assertEqual(fx(ed, 1), [OFF] * 4)                     # another voice is not touched

    def test_every_effect_on_its_own_and_together(self):
        for changes, want in ((dict(arp=(0, 4, 7)), "arp"), (dict(arp=(0, 3, 7), arprate=4), "arp"), (dict(slide=12), "slide"),
                              (dict(pws=(25, 75, 6)), "pws"), (dict(vib=(5, 3), arp=(0, 12), slide=6, pws=(10, 90, 3)), "all")):
            ed = make(TEXT)
            self.assertEqual(ed.set_note_effects([(0, 48)], **changes), 1, changes)
            note = ed.note_effects(0, 1)
            for k, v in changes.items():
                self.assertEqual(note[k], v, (changes, k))
            self.assertEqual(sum(1 for x in fx(ed) if x != OFF), 1, changes)       # only that note
            same_after_round_trip(self, ed)
            self.assertEqual(ed.undo(), "note effects")
            self.assertEqual(ed.song.to_mml(), make(TEXT).song.to_mml())

    def test_the_marker_says_what_a_note_plays_with(self):
        ed = make(TEXT)
        ed.set_note_effects([(0, 0)], vib=(5, 3), slide=12, arp=(0, 4, 7), pws=(25, 75, 6))
        self.assertEqual(fx_marks(ed.tune.voices[0][0]), "~/^w")
        self.assertEqual(fx_marks(ed.tune.voices[0][1]), "")
        ed.set_note_effects([(0, 0)], arp=(0, 3, 7))
        self.assertEqual(fx_marks(ed.tune.voices[0][0]), "~/^mw")
        # a sweep makes no sound on a wave that is not a pulse, so it has no marker either
        ed2 = make("V1: o4 wave tri c4 d4")
        ed2.set_note_effects([(0, 0)], pws=(25, 75, 6))
        self.assertNotIn("w", fx_marks(ed2.tune.voices[0][0]))

    def test_a_run_of_notes_is_set_once_not_once_each(self):
        ed = make(TEXT)
        self.assertEqual(ed.set_note_effects([(0, 48), (0, 96), (0, 144)], vib=(5, 3)), 3)
        mml = ed.song.to_mml()
        self.assertEqual(mml.count("vib 5,3"), 1)                   # one `vib` before the run, one `vib off` after it
        self.assertEqual(mml.count("vib off"), 1)
        self.assertEqual([i for i, x in enumerate(fx(ed)) if x != OFF], [1, 2, 3])

    def test_a_gap_in_the_selection_is_kept(self):
        ed = make(TEXT)
        ed.set_note_effects([(0, 0), (0, 96)], vib=(5, 3))
        self.assertEqual([i for i, x in enumerate(fx(ed)) if x != OFF], [0, 2])
        same_after_round_trip(self, ed)

    def test_off_on_one_note_of_an_instrument_that_has_the_effect(self):
        ed = make("[instr]\nwob = wave=pulse vib=5,3\nV1: @wob o4 c4 d4 e4\n")
        self.assertEqual({x[:2] for x in fx(ed)}, {(5, 3)})
        ed.set_note_effects([(0, 96)], vib=FX_OFF["vib"])
        self.assertEqual([x[:2] for x in fx(ed)], [(5, 3), (0, 0), (5, 3)])
        same_after_round_trip(self, ed)

    def test_changing_one_part_leaves_the_rest_of_the_note_alone(self):
        ed = make(TEXT)
        ed.set_note_effects([(0, 48)], vib=(5, 3), arp=(0, 4, 7))
        ed.set_note_effects([(0, 48)], arp=FX_OFF["arp"])
        note = ed.note_effects(0, 1)
        self.assertEqual((note["vib"], note["arp"]), ((5, 3), ()))
        ed.set_note_effects([(0, 48)], slide=12)
        self.assertEqual(ed.note_effects(0, 1)["vib"], (5, 3))
        self.assertEqual(ed.note_effects(0, 1)["slide"], 12)
        self.assertEqual(ed.note_effects(0, 2)["slide"], 0)           # a slide is for the next note only

    def test_setting_what_is_already_there_changes_nothing(self):
        ed = make(TEXT)
        ed.set_note_effects([(0, 0)], vib=(5, 3))
        before, depth = ed.song.to_mml(), len(ed.undo_stack)
        self.assertEqual(ed.set_note_effects([(0, 0)], vib=(5, 3)), 0)
        self.assertEqual(ed.song.to_mml(), before)

    def test_refusals_change_nothing_and_leave_no_undo_step(self):
        ed = make(TEXT)
        before = ed.song.to_mml()
        bad = [dict(vib=(16, 3)), dict(vib=(5,)), dict(arp=tuple(range(9))), dict(arp=(0, 30)), dict(arprate=0), dict(slide=193),
               dict(pws=(0, 50, 3)), dict(pws=(25, 75, 16)), dict(sparkle=1), dict()]
        for changes in bad:
            with self.assertRaises(EditError, msg=changes):
                ed.set_note_effects([(0, 0)], **changes)
        for sel in ([], [(0, 7)], [(4, 0)]):                          # nothing picked, no note there, a drum lane
            with self.assertRaises(EditError):
                ed.set_note_effects(sel, vib=(5, 3))
        with self.assertRaises(EditError):                            # one good note and one that is gone: neither changes
            ed.set_note_effects([(0, 0), (0, 5)], vib=(5, 3))
        self.assertEqual(ed.song.to_mml(), before)
        self.assertFalse(ed.can_undo())

    def test_a_note_in_a_pattern_changes_in_every_use_and_nothing_else(self):
        ed = make("[pat]\nriff = c4 d4 e4 r4\nV1: o4 !riff !riff\nV2: o3 c1\n")
        ed.set_note_effects([(0, 96)], vib=(5, 3))
        self.assertEqual([i for i, x in enumerate(fx(ed)) if x != OFF], [1, 4])    # d4 in both calls
        same_after_round_trip(self, ed)

    def test_a_held_note_and_a_note_in_a_repeat(self):
        ed = make("V1: o4 c4&4 d4 [e8 f8]2 g4\n")
        ed.set_note_effects([(0, 0)], slide=12)
        self.assertEqual(ed.tune.voices[0][0].slide, 12)
        self.assertEqual(ed.tune.voices[0][0].dur, 192)
        ed.set_note_effects([(0, 288)], arp=(0, 4, 7))
        self.assertEqual([i for i, x in enumerate(fx(ed)) if x[2]], [2, 4])        # the repeat plays it twice
        same_after_round_trip(self, ed)

    def test_the_other_notes_never_change_whatever_is_picked(self):
        text = ("[pat]\nriff = c8 d8 e8 f8\n[instr]\nwob = wave=pulse vib=5,3 pws=25,75,6\n"
                "V1: o4 !riff @wob !riff [g8 a8]2 c4&4 d4 vib 3,2 e4 r4 >c4\nV2: o3 l8 c d e f g a b >c\n")
        rnd = random.Random(7)
        for _ in range(25):
            ed = make(text)
            v = rnd.choice((0, 1))
            notes = [(v, n.start) for n in ed.tune.voices[v]]
            picked = rnd.sample(notes, rnd.randint(1, min(4, len(notes))))
            before = [fx(ed, k) for k in range(2)]
            changes = rnd.choice((dict(vib=(6, 6)), dict(arp=(0, 4, 7)), dict(slide=24), dict(pws=(30, 70, 5)),
                                  dict(vib=(0, 0), pws=(0, 0, 0)), dict(arp=(0, 12), arprate=4)))
            try:
                ed.set_note_effects(picked, **changes)
            except EditError:
                self.assertEqual([fx(ed, k) for k in range(2)], before)
                continue
            after = [fx(ed, k) for k in range(2)]
            # notes outside the pick that do not share events with a pick keep exactly what they had
            shared = {id(p.event) for k, s in picked for p in ed.group(k, ed.find(k, s))}
            for k in range(2):
                for i, n in enumerate(ed.tune.voices[k]):
                    if (k, n.start) in picked:
                        continue
                    if not any(id(p.event) in shared for p in ed.group(k, i)):
                        self.assertEqual(after[k][i], before[k][i], (picked, changes, k, i))
            for k, s in picked:
                note = ed.note_effects(k, ed.find(k, s))
                for name, want in changes.items():
                    if name == "arprate" and not note["arp"]:
                        continue
                    self.assertEqual(note[name], want if name != "pws" or any(want) else (0, 0, 0), (picked, changes))
            same_after_round_trip(self, ed)
            while ed.can_undo():
                ed.undo()
            self.assertEqual(ed.song.to_mml(), make(text).song.to_mml())


class KeyTests(unittest.TestCase):
    TEXT = "V1: o4 l8 c d e f g a b >c | c d e f g a b <c\nV2: o3 l4 c e g c c e g c\n"

    def test_a_key_change_in_the_middle_keeps_every_note_where_and_how_it_sounds(self):
        ed = make(self.TEXT)
        before = [pitches(ed, 0), pitches(ed, 1)]
        self.assertEqual(ed.set_key_at(192, 2, "minor", voices=[0]), 1)
        self.assertEqual(ed.key_marks(), [KeyMark(0, 192, 2, "minor")])
        self.assertEqual([pitches(ed, 0), pitches(ed, 1)], before)
        mml = ed.song.to_mml()
        self.assertIn("key d minor", mml)
        self.assertEqual(ed.key_at(0, 100), None)
        self.assertEqual(ed.key_at(0, 192), KeyMark(0, 192, 2, "minor"))
        self.assertEqual(ed.key_at(1, 500), None)                     # keys belong to a voice
        same_after_round_trip(self, ed)
        self.assertEqual(ed.undo(), "key change")
        self.assertEqual(ed.song.to_mml(), make(self.TEXT).song.to_mml())
        self.assertEqual(ed.key_marks(), [])

    def test_all_voices_and_the_first_key_at_the_start(self):
        ed = make(self.TEXT)
        before = [pitches(ed, 0), pitches(ed, 1)]
        ed.set_key_at(0, 9, "minor", voices=[0, 1])
        self.assertEqual([pitches(ed, 0), pitches(ed, 1)], before)
        self.assertEqual({(m.voice, m.tick) for m in ed.key_marks()}, {(0, 0), (1, 0)})
        self.assertNotIn("c d e", ed.song.to_mml().split("V2")[0])       # voice 1 is written in degrees now
        same_after_round_trip(self, ed)

    def test_changing_a_key_in_place_keeps_the_pitches_or_modulates(self):
        ed = make(self.TEXT)
        ed.set_key_at(0, 0, "major", voices=[0])
        before = pitches(ed)
        ed.set_key_at(0, 7, "major", voices=[0])                          # the same event is edited, not a second one
        self.assertEqual(len(ed.key_marks()), 1)
        self.assertEqual(pitches(ed), before)
        same_after_round_trip(self, ed)
        ed.set_key_at(0, 9, "major", voices=[0], keep_pitch=False)        # a modulation (G major to A major): the degrees stay
        self.assertEqual(len(ed.key_marks()), 1)
        self.assertEqual([p - q for (_, _, p), (_, _, q) in zip(pitches(ed), before)], [2] * len(before))
        same_after_round_trip(self, ed)

    def test_removing_a_key_change_gives_the_letters_back(self):
        ed = make(self.TEXT)
        original = ed.song.to_mml()
        ed.set_key_at(192, 2, "minor", voices=[0, 1])
        before = [pitches(ed, 0), pitches(ed, 1)]
        self.assertEqual(ed.remove_key_at(192, voices=[0, 1]), 2)
        self.assertEqual([pitches(ed, 0), pitches(ed, 1)], before)
        self.assertEqual(ed.key_marks(), [])
        self.assertEqual(ed.song.to_mml(), original)                      # letters again, the very same text
        ed.undo()
        self.assertEqual(len(ed.key_marks()), 2)

    def test_key_off_puts_the_notes_back_to_letters(self):
        ed = make("V1: key c major o4 1 2 3 4 5 6 7 8\n")
        before = pitches(ed)
        ed.set_key_at(192, None, voices=[0])
        self.assertEqual(ed.key_marks()[-1], KeyMark(0, 192, None, None))
        self.assertEqual(pitches(ed), before)
        self.assertIn("key off", ed.song.to_mml())
        same_after_round_trip(self, ed)

    def test_a_key_change_between_pattern_calls_when_the_pattern_can_agree(self):
        ed = make("[pat]\nriff = c4 d4 e4 r4\nV1: o4 !riff !riff\nV2: o3 c1\n")
        before = pitches(ed)
        with self.assertRaises(EditError) as r:                           # the pattern would have to be letters and degrees
            ed.set_key_at(384, 0, "major", voices=[0])
        self.assertIn("Make unique", str(r.exception))
        self.assertEqual(pitches(ed), before)
        self.assertEqual(ed.key_marks(), [])
        self.assertFalse(ed.can_undo())
        ed.make_unique(0, 4)                                              # the second call gets its own copy
        ed.set_key_at(384, 0, "major", voices=[0])
        self.assertEqual(pitches(ed), before)
        same_after_round_trip(self, ed)

    def test_a_key_at_the_start_reaches_a_pattern_every_time_it_is_played(self):
        ed = make("[pat]\nriff = c4 d4 e4 r4\nV1: o4 !riff >> !riff\nV2: o3 c1\n")
        before = pitches(ed)
        ed.set_key_at(0, 0, "major", voices=[0])
        self.assertEqual(pitches(ed), before)
        same_after_round_trip(self, ed)

    def test_refusals_change_nothing(self):
        ed = make(self.TEXT)
        before = ed.song.to_mml()
        for args, kwargs in (((100, 2, "minor"), dict(voices=[0])),        # inside a note
                             ((9999, 2, "minor"), dict(voices=[0])),       # past the end of the voice
                             ((192, 2, "no-such-scale"), dict(voices=[0])),
                             ((192, 2, "minor"), dict(voices=[])), ((192, 2, "minor"), dict(voices=[4])),
                             ((-1, 2, "minor"), dict(voices=[0]))):
            with self.assertRaises(EditError, msg=(args, kwargs)):
                ed.set_key_at(*args, **kwargs)
        with self.assertRaises(EditError):
            ed.remove_key_at(192, voices=[0])                              # there is none
        self.assertEqual(ed.song.to_mml(), before)
        self.assertFalse(ed.can_undo())

    def test_two_voices_one_of_them_refused_changes_neither(self):
        ed = make("V1: o4 c1 c1\nV2: o3 l4 c e g c c4\n")                  # V2 ends before tick 384+
        before = ed.song.to_mml()
        with self.assertRaises(EditError):
            ed.set_key_at(960, 0, "major", voices=[0, 1])
        self.assertEqual(ed.song.to_mml(), before)

    def test_a_scale_the_song_defines_is_allowed(self):
        ed = make("[key]\nblues = 0 3 5 6 7 10\nV1: o4 c4 d4 e4 f4\n")
        before = pitches(ed)
        ed.set_key_at(0, 0, "blues", voices=[0])
        self.assertEqual(pitches(ed), before)
        same_after_round_trip(self, ed)


class PanelEditTests(unittest.TestCase):
    """`Editor.edit_notes`: the panel's effects and key change as one undo step."""

    def test_both_at_once_are_one_undo_step(self):
        ed = make(KeyTests.TEXT)
        original = ed.song.to_mml()
        n, k = ed.edit_notes([(0, 48)], dict(vib=(5, 3)), dict(tick=192, tonic=2, scale="minor", voices=[0]))
        self.assertEqual((n, k), (1, 1))
        self.assertEqual(len(ed.key_marks()), 1)
        self.assertEqual(ed.note_effects(0, 1)["vib"], (5, 3))
        same_after_round_trip(self, ed)
        self.assertEqual(len(ed.undo_stack), 1)
        self.assertEqual(ed.undo(), "note effects and key")
        self.assertEqual(ed.song.to_mml(), original)

    def test_if_either_half_is_refused_neither_happens(self):
        ed = make(KeyTests.TEXT)
        before = ed.song.to_mml()
        with self.assertRaises(EditError):                                 # the key is fine, the effect is not
            ed.edit_notes([(0, 48)], dict(vib=(99, 3)), dict(tick=192, tonic=2, scale="minor", voices=[0]))
        with self.assertRaises(EditError):                                 # the effect is fine, the key is not
            ed.edit_notes([(0, 48)], dict(vib=(5, 3)), dict(tick=100, tonic=2, scale="minor", voices=[0]))
        with self.assertRaises(EditError):
            ed.edit_notes([(0, 48)])
        self.assertEqual(ed.song.to_mml(), before)
        self.assertFalse(ed.can_undo())

    def test_key_only_and_remove(self):
        ed = make(KeyTests.TEXT)
        ed.edit_notes((), key=dict(tick=192, tonic=0, scale="major", voices=[0]))
        self.assertEqual(len(ed.key_marks()), 1)
        ed.edit_notes((), key=dict(tick=192, remove=True, voices=[0]))
        self.assertEqual(ed.key_marks(), [])

    def test_effects_on_notes_that_are_in_a_key(self):
        ed = make("V1: key d minor o4 1 2 3 4 5 6 7 8\n")
        ed.set_note_effects([(0, 96)], arp=(0, 3, 7))
        self.assertEqual([i for i, x in enumerate(fx(ed)) if x[2]], [2])
        same_after_round_trip(self, ed)


class FuzzTests(unittest.TestCase):
    """Random effects and key edits on the demo songs and generated tunes: each either works (the notes still sound the same, the
    MML round trip agrees, undo gets the song back) or is refused and changes nothing."""

    def songs(self):
        data = Path(__file__).parent / "data" / "demo_songs"
        for p in sorted(data.glob("*.mml"))[:4]:
            yield p.name, p.read_text()
        for kind, seed in (("town", 3), ("boss", 5), ("forest", 9)):
            try:
                yield "%s-%d" % (kind, seed), musicgen.generate(kind, seed)
            except Exception:                                            # a kind this build does not have
                continue

    def test_random_edits(self):
        rnd = random.Random(11)
        done = 0
        for name, text in self.songs():
            ed = Editor(Song.from_mml(text))
            start_mml = ed.song.to_mml()
            for _ in range(14):
                before_mml = ed.song.to_mml()
                before = [pitches(ed, v) for v in range(ed.song.voices)]
                before_fx = [fx(ed, v) for v in range(ed.song.voices)]
                v = rnd.randrange(ed.song.voices)
                notes = ed.tune.voices[v]
                if not notes:
                    continue
                try:
                    if rnd.random() < 0.6:
                        picked = [(v, n.start) for n in rnd.sample(notes, min(len(notes), rnd.randint(1, 3)))]
                        ed.set_note_effects(picked, **rnd.choice((dict(vib=(5, 3)), dict(arp=(0, 4, 7), arprate=6), dict(slide=12),
                                                                    dict(pws=(25, 75, 6)), dict(vib=(0, 0)), dict(arp=()))))
                        # the notes that were not picked (and share nothing with one that was) sound as they did
                        self.assertEqual([pitches(ed, k) for k in range(ed.song.voices)], before, name)
                    else:
                        tick = rnd.choice([n.start for n in notes])
                        if rnd.random() < 0.3 and ed.key_marks():
                            m = rnd.choice(ed.key_marks())
                            ed.remove_key_at(m.tick, [m.voice])
                        else:
                            ed.set_key_at(tick, rnd.randrange(12), rnd.choice(("major", "minor", "dorian")), voices=[v])
                        self.assertEqual([pitches(ed, k) for k in range(ed.song.voices)], before, name)
                        self.assertEqual([fx(ed, k) for k in range(ed.song.voices)], before_fx, name)
                    done += 1
                    same_after_round_trip(self, ed)
                except EditError:
                    self.assertEqual(ed.song.to_mml(), before_mml, name)
                    continue
            while ed.can_undo():
                ed.undo()
            self.assertEqual(ed.song.to_mml(), start_mml, name)
        self.assertGreater(done, 10)


class ParserTests(unittest.TestCase):
    def test_effect_words_that_start_with_a_note_letter_work_in_a_key(self):
        """`arp`, `adsr`, `filter`, `fsweep` and `fx` begin with a letter that is a note name; under a `key` they used to be
        refused as letter notes, which made a per-note arpeggio in a key unreadable."""
        for word in ("arp 0,4,7", "arprate 4", "adsr 0,8,10,4", "filter lp,8,2", "fsweep 2,12,4"):
            tune = Song.from_mml("V1: key c major o4 1 %s 2 3\n" % word).compile()
            self.assertEqual(len(tune.voices[0]), 3, word)


if __name__ == "__main__":
    unittest.main()
