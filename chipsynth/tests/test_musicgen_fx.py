# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The generator's effect options (echo, reverb, filter, vibrato, slide, sweep) and degrees."""
import contextlib
import io
import re
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np
import pygame

from chipsynth import __main__ as cli
from chipsynth import midi, mml, musicgen, package as pk
from chipsynth.composer.app import Composer, NullOut
from chipsynth.composer.prefs import Prefs
from chipsynth.formats import famistudio_export as fex, famistudio_text as fam
from chipsynth.player import render, render_wav
from chipsynth.song import Song

MOODS = musicgen.MOODS
ALL = musicgen.FX_WORDS

# crc32 of generate(kind, seed, mood, dac=..., dac_bass=..., voice=...) as the generator wrote it before the effect options
# existed: d = dac, b = dac_bass, dbv = all three (the plain outputs are pinned in test_musicgen_dac.BASELINE)
DAC_BASELINE = {
    ('title', 3, '', 'd'): '28afa28b',
    ('title', 3, '', 'b'): '8c4bd2ce',
    ('title', 3, '', 'dbv'): 'e5b88285',
    ('title', 21, 'calm', 'd'): '6563e8e6',
    ('title', 21, 'calm', 'b'): 'b7f46352',
    ('title', 21, 'calm', 'dbv'): '3bf8d536',
    ('title', 55, 'dark', 'd'): '741ab0cf',
    ('title', 55, 'dark', 'b'): '748b12be',
    ('title', 55, 'dark', 'dbv'): '273dce71',
    ('victory', 3, '', 'd'): '05cefd7c',
    ('victory', 3, '', 'b'): 'e67cff6a',
    ('victory', 3, '', 'dbv'): '3d2cf6e8',
    ('victory', 21, 'calm', 'd'): '216bd773',
    ('victory', 21, 'calm', 'b'): '1709e0e9',
    ('victory', 21, 'calm', 'dbv'): '4b55f2c5',
    ('victory', 55, 'dark', 'd'): 'fe9e5e4e',
    ('victory', 55, 'dark', 'b'): '869747b4',
    ('victory', 55, 'dark', 'dbv'): '50c80b95',
    ('gameover', 3, '', 'd'): 'a8bb9bd1',
    ('gameover', 3, '', 'b'): '8158c93c',
    ('gameover', 3, '', 'dbv'): 'a8bb9bd1',
    ('gameover', 21, 'calm', 'd'): 'b16032f1',
    ('gameover', 21, 'calm', 'b'): '42f91438',
    ('gameover', 21, 'calm', 'dbv'): 'b16032f1',
    ('gameover', 55, 'dark', 'd'): 'c51551da',
    ('gameover', 55, 'dark', 'b'): '14144899',
    ('gameover', 55, 'dark', 'dbv'): 'c51551da',
    ('fight', 3, '', 'd'): '78b3719c',
    ('fight', 3, '', 'b'): 'f8040c44',
    ('fight', 3, '', 'dbv'): '9f00acb5',
    ('fight', 21, 'calm', 'd'): '7693eb38',
    ('fight', 21, 'calm', 'b'): 'c8b86dd4',
    ('fight', 21, 'calm', 'dbv'): '6d0c06ca',
    ('fight', 55, 'dark', 'd'): '18920a04',
    ('fight', 55, 'dark', 'b'): '18c0a6d0',
    ('fight', 55, 'dark', 'dbv'): '7673db00',
    ('boss', 3, '', 'd'): '2a3375c7',
    ('boss', 3, '', 'b'): '0e80276d',
    ('boss', 3, '', 'dbv'): 'f7d5e626',
    ('boss', 21, 'calm', 'd'): 'e1e0578f',
    ('boss', 21, 'calm', 'b'): '2d8ba3cf',
    ('boss', 21, 'calm', 'dbv'): '6e6dfb9f',
    ('boss', 55, 'dark', 'd'): '16ca7541',
    ('boss', 55, 'dark', 'b'): '767ee941',
    ('boss', 55, 'dark', 'dbv'): 'f5a63158',
    ('world', 3, '', 'd'): '78884269',
    ('world', 3, '', 'b'): '500e14ab',
    ('world', 3, '', 'dbv'): 'bc7911cc',
    ('world', 21, 'calm', 'd'): 'c9a16c23',
    ('world', 21, 'calm', 'b'): '451c50f8',
    ('world', 21, 'calm', 'dbv'): '9df50cb7',
    ('world', 55, 'dark', 'd'): 'da08b62c',
    ('world', 55, 'dark', 'b'): 'f73bab19',
    ('world', 55, 'dark', 'dbv'): '7fa2afcf',
    ('maze', 3, '', 'd'): 'a43b4b20',
    ('maze', 3, '', 'b'): '5b0b0afc',
    ('maze', 3, '', 'dbv'): 'a43b4b20',
    ('maze', 21, 'calm', 'd'): 'f9a59462',
    ('maze', 21, 'calm', 'b'): 'e4a9e913',
    ('maze', 21, 'calm', 'dbv'): 'f9a59462',
    ('maze', 55, 'dark', 'd'): 'e0395b33',
    ('maze', 55, 'dark', 'b'): '241b6345',
    ('maze', 55, 'dark', 'dbv'): 'e0395b33',
    ('town', 3, '', 'd'): '3602f4df',
    ('town', 3, '', 'b'): '9cbdf07f',
    ('town', 3, '', 'dbv'): 'b1620765',
    ('town', 21, 'calm', 'd'): '228a80ef',
    ('town', 21, 'calm', 'b'): '38b7e51c',
    ('town', 21, 'calm', 'dbv'): 'be60b773',
    ('town', 55, 'dark', 'd'): '8ebe4750',
    ('town', 55, 'dark', 'b'): '5062ad8b',
    ('town', 55, 'dark', 'dbv'): '0d801042',
}
DAC_OPTS = {"d": dict(dac=True), "b": dict(dac_bass=True), "dbv": dict(dac=True, dac_bass=True, voice=True)}


def crc(text):
    return "%08x" % (zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF)


def lane(text, label):
    return [l for l in text.split("\n") if l.startswith(label + ":")]


def melodic(tune, v):
    return [(n.start, n.dur, n.pitch) for n in tune.voices[v]]



class DefaultOutputTests(unittest.TestCase):
    def test_the_dac_options_write_what_they_always_wrote(self):
        for (kind, seed, mood, code), want in DAC_BASELINE.items():
            self.assertEqual(crc(musicgen.generate(kind, seed, mood, **DAC_OPTS[code])), want, (kind, seed, mood, code))

    def test_options_off_is_the_same_as_not_passing_them(self):
        for kind in musicgen.KINDS:
            for fx in ((), [], "", None, False):
                self.assertEqual(musicgen.generate(kind, 4, "tense", fx=fx, degrees=False), musicgen.generate(kind, 4, "tense"))

    def test_a_plain_tune_has_none_of_the_new_words(self):
        for kind in musicgen.KINDS:
            text = musicgen.generate(kind, 2)
            for word in ("echo", "reverb", "filter", "fsweep", "vib", "slide", "pws", "send", "key ", "fx"):
                self.assertNotIn(word, text, (kind, word))
            self.assertNotIn("fx", musicgen.meta(text))
            self.assertEqual(musicgen.fx_options(text), ((), False))


class WordTests(unittest.TestCase):
    def test_the_ways_to_ask(self):
        self.assertEqual(musicgen.fx_words("echo"), ("echo",))
        self.assertEqual(musicgen.fx_words(["sweep", "echo"]), ("echo", "sweep"))             # always in the canonical order
        self.assertEqual(musicgen.fx_words("echo,filter vibrato"), ("echo", "filter", "vibrato"))
        self.assertEqual(musicgen.fx_words("echo+reverb"), ("echo", "reverb"))
        for every in (True, "all", "fx", ["fx"], ("echo", "all")):
            self.assertEqual(musicgen.fx_words(every), ALL)

    def test_a_bad_word_is_an_error_naming_the_effects(self):
        with self.assertRaises(ValueError) as cm:
            musicgen.generate("town", 1, fx=["echo", "wobble"])
        self.assertIn("wobble", str(cm.exception))
        self.assertIn("reverb", str(cm.exception))

    def test_same_arguments_same_song(self):
        for kind in musicgen.KINDS:
            a = musicgen.generate(kind, 9, "tense", fx="all", degrees=True)
            self.assertEqual(a, musicgen.generate(kind, 9, "tense", fx="all", degrees=True))
            self.assertNotEqual(a, musicgen.generate(kind, 10, "tense", fx="all", degrees=True))


class NotesStayTests(unittest.TestCase):
    def test_no_option_moves_a_note(self):
        for kind in musicgen.KINDS:
            for mood in MOODS:
                plain = mml.parse(musicgen.generate(kind, 3, mood))
                for opts in ({"fx": "all"}, {"degrees": True}, {"fx": "all", "degrees": True}):
                    tune = mml.parse(musicgen.generate(kind, 3, mood, **opts))
                    for v in range(4):
                        self.assertEqual(melodic(tune, v), melodic(plain, v), (kind, mood, opts, v))
                    self.assertEqual((tune.bpm, tune.length, tune.once), (plain.bpm, plain.length, plain.once))

    def test_the_dac_lanes_are_untouched_too(self):
        dac = {"dac": True, "dac_bass": True, "voice": True}
        for kind in musicgen.KINDS:
            a = musicgen.generate(kind, 5, "dark", **dac)
            b = musicgen.generate(kind, 5, "dark", fx="all", degrees=True, **dac)
            for label in ("D1", "D2", "V4"):
                self.assertEqual(lane(a, label), lane(b, label))

    def test_each_effect_has_a_random_generator_of_its_own(self):
        """Switching another word on does not move the slides."""
        def slides(text):
            tune = mml.parse(text)
            return [(v, n.start, n.slide) for v in range(3) for n in tune.voices[v] if n.slide]
        a = slides(musicgen.generate("world", 8, fx="slide"))
        self.assertTrue(a)
        self.assertEqual(a, slides(musicgen.generate("world", 8, fx="all")))
        self.assertEqual(a, slides(musicgen.generate("world", 8, fx="slide", degrees=True)))


class CompileTests(unittest.TestCase):
    def test_every_kind_mood_and_option_set_parses_with_no_warnings_and_compiles(self):
        sets = ({"fx": "all"}, {"degrees": True}, {"fx": "all", "degrees": True},
                {"fx": "all", "degrees": True, "dac": True, "dac_bass": True, "voice": True})
        sets += tuple({"fx": w} for w in ALL)
        for kind in musicgen.KINDS:
            for mood in MOODS:
                for opts in sets:
                    for seed in (1, 6):
                        text = musicgen.generate(kind, seed, mood, **opts)
                        tune = mml.parse(text)
                        self.assertEqual(tune.warnings, [], (kind, mood, seed, opts))
                        self.assertEqual(tune.length % tune.bar_ticks, 0)
                        Song.from_mml(text).compile()

    def test_the_song_model_reads_and_writes_it_back(self):
        for kind in ("boss", "maze", "town", "victory"):
            text = musicgen.generate(kind, 4, "tense", fx="all", degrees=True)
            song = Song.from_mml(text)
            first = song.compile()
            again = Song.from_mml(song.to_mml()).compile()
            for v in range(4):
                self.assertEqual(melodic(again, v), melodic(first, v), (kind, v))
            self.assertEqual((again.echo, again.reverb), (first.echo, first.reverb))

    def test_every_number_is_in_range_for_every_mood_and_tempo(self):
        for kind in musicgen.KINDS:
            for mood in MOODS:
                for tempo in (30, 60, 300):
                    text = musicgen.generate(kind, 2, mood, tempo, fx="all")
                    self.assertEqual(mml.parse(text).warnings, [])
                    m = re.search(r"^echo (\d+)ms,(\d+),(\d+)", text, re.M)
                    self.assertTrue(20 <= int(m.group(1)) <= 1000)
                    self.assertTrue(0 <= int(m.group(2)) <= 70 and 0 <= int(m.group(3)) <= 45)


class EchoReverbTests(unittest.TestCase):
    def test_the_echo_follows_the_tempo(self):
        for kind in ("world", "fight"):
            ms = []
            for tempo in (80, 160):
                tune = mml.parse(musicgen.generate(kind, 2, "", tempo, fx="echo"))
                ms.append(tune.echo[0])
            self.assertAlmostEqual(ms[0], 2 * ms[1], delta=0.003)
        t = mml.parse(musicgen.generate("town", 2, "", 100, fx="echo"))
        self.assertAlmostEqual(t.echo[0], 0.75 * 0.6, delta=0.001)                            # a dotted eighth at 100 bpm
        self.assertFalse(t.echo[3])
        self.assertTrue(mml.parse(musicgen.generate("maze", 2, fx="echo")).echo[3])           # ping-pong in the roomy kinds

    def test_only_what_was_asked_for(self):
        echo = mml.parse(musicgen.generate("world", 2, fx="echo"))
        self.assertTrue(echo.echo and not echo.reverb)
        rev = mml.parse(musicgen.generate("world", 2, fx="reverb"))
        self.assertTrue(rev.reverb and not rev.echo)

    def test_the_bass_stays_dry_and_the_arpeggio_sits_back(self):
        for kind in musicgen.KINDS:
            tune = mml.parse(musicgen.generate(kind, 2, fx="reverb"))
            self.assertEqual(tune.voices[0][0].inst.send, 15)
            self.assertLess(tune.voices[1][0].inst.send, 15)
            self.assertLess(tune.voices[2][0].inst.send, 15)
        none = mml.parse(musicgen.generate("world", 2, fx="filter"))
        self.assertEqual({n.inst.send for v in range(3) for n in none.voices[v]}, {15})        # no bus, no sends

    def test_calm_is_wetter_than_bright(self):
        calm = mml.parse(musicgen.generate("town", 2, "calm", 100, fx="echo,reverb"))
        bright = mml.parse(musicgen.generate("town", 2, "bright", 100, fx="echo,reverb"))
        self.assertGreater(calm.echo[2], bright.echo[2])
        self.assertGreater(calm.reverb, bright.reverb)

    def test_the_tail_rings_past_the_end_of_a_jingle_and_the_wet_signal_is_there(self):
        dry = np.asarray(render(mml.parse(musicgen.generate("gameover", 2)), seconds=20))
        wet = np.asarray(render(mml.parse(musicgen.generate("gameover", 2, fx="echo,reverb")), seconds=20))
        self.assertFalse(np.allclose(dry, wet))
        self.assertTrue(np.isfinite(wet).all())


class FilterTests(unittest.TestCase):
    def inst(self, kind, v, **kw):
        return mml.parse(musicgen.generate(kind, 2, kw.pop("mood", ""), fx="filter")).voices[v][0].inst

    def test_a_boss_has_a_filter_sweep_on_the_lead(self):
        i = self.inst("boss", 0)
        self.assertEqual(i.filt, "lp")
        self.assertGreater(i.fs_rate, 0)
        self.assertNotEqual(i.fs_from, i.fs_to)

    def test_the_kinds_that_have_one(self):
        for kind, voice in (("fight", 0), ("world", 1), ("maze", 0), ("town", 1), ("title", 0), ("gameover", 2)):
            self.assertEqual(self.inst(kind, voice).filt, "lp", (kind, voice))

    def test_a_victory_jingle_is_too_short_for_one_so_it_adds_nothing(self):
        text = musicgen.generate("victory", 2, fx="filter")
        self.assertEqual(text, musicgen.generate("victory", 2))                              # not even a record of it
        self.assertEqual(musicgen.fx_options(text), ((), False))

    def test_the_drums_are_never_filtered(self):
        tune = mml.parse(musicgen.generate("boss", 2, fx="all"))
        self.assertEqual({n.inst.filt for n in tune.voices[3]}, {""})

    def test_dark_is_duller_and_tense_more_resonant_than_plain(self):
        plain, dark = self.inst("boss", 0), self.inst("boss", 0, mood="dark")
        tense = self.inst("boss", 0, mood="tense")
        self.assertLess(dark.filt_cut, plain.filt_cut)
        self.assertGreater(tense.filt_res, plain.filt_res)

    def test_a_pad_becomes_a_saw_so_there_is_something_to_filter(self):
        self.assertEqual(self.inst("maze", 2).wave, "saw")
        self.assertEqual(mml.parse(musicgen.generate("maze", 2, fx="filter,sweep")).voices[2][0].inst.wave, "pulse")

    def test_no_filter_on_a_voice_that_the_dac_bass_took_away(self):
        text = musicgen.generate("boss", 2, dac_bass=True, fx="filter")
        self.assertEqual(lane(text, "V3"), [])
        self.assertEqual(mml.parse(text).warnings, [])


class VibratoSlideTests(unittest.TestCase):
    def test_vibrato_is_only_on_the_held_notes_of_the_lead(self):
        for kind in musicgen.KINDS:
            tune = mml.parse(musicgen.generate(kind, 4, fx="vibrato"))
            lead = tune.voices[0]
            on = [n for n in lead if n.inst.vib_rate]
            off = [n for n in lead if not n.inst.vib_rate]
            self.assertTrue(on, kind)
            shortest = musicgen.VIBRATO[kind][2] * mml.TICKS_WHOLE // 16
            self.assertTrue(all(n.dur >= shortest for n in on), kind)
            self.assertTrue(all(n.dur < shortest for n in off), kind)
            for v in (1, 2, 3):
                self.assertFalse(any(n.inst.vib_rate for n in tune.voices[v]), (kind, v))

    def test_a_tense_vibrato_is_faster_and_a_calm_one_gentler(self):
        def vib(mood):
            n = next(n for n in mml.parse(musicgen.generate("world", 4, mood, fx="vibrato")).voices[0] if n.inst.vib_rate)
            return n.inst.vib_rate, n.inst.vib_depth
        self.assertGreater(vib("tense")[0], vib("calm")[0])
        self.assertGreater(vib("tense")[1], vib("calm")[1])

    def test_slides_only_join_notes_that_touch_and_leap(self):
        for kind in ("fight", "boss", "world", "gameover"):
            tune = mml.parse(musicgen.generate(kind, 4, fx="slide"))
            found = 0
            for v in range(3):
                notes = tune.voices[v]
                for i, n in enumerate(notes):
                    if not n.slide:
                        continue
                    found += 1
                    prev = notes[i - 1]
                    self.assertEqual(prev.start + prev.dur, n.start, (kind, v, n.start))
                    self.assertGreaterEqual(abs(n.pitch - prev.pitch), musicgen.SLIDE[kind][v + 1][2])
                    self.assertLessEqual(n.slide, 12 * n.dur // (mml.TICKS_WHOLE // 16))
            self.assertTrue(found, kind)

    def test_a_game_over_wails_and_a_town_has_only_the_lead_sliding(self):
        n = lambda kind: sum(1 for v in range(3) for x in mml.parse(musicgen.generate(kind, 4, fx="slide")).voices[v] if x.slide)
        self.assertGreater(n("gameover"), 0)
        town = mml.parse(musicgen.generate("town", 4, fx="slide"))
        self.assertFalse(any(x.slide for v in (1, 2) for x in town.voices[v]))


class SweepTests(unittest.TestCase):
    def sweeps(self, kind, mood="", **kw):
        tune = mml.parse(musicgen.generate(kind, 4, mood, fx="sweep", **kw))
        return {v: tune.voices[v][0].inst for v in range(3) if tune.voices[v] and tune.voices[v][0].inst.pws_rate}

    def test_a_pulse_arpeggio_sweeps(self):
        for kind in ("fight", "boss", "world", "town", "title", "victory"):
            self.assertIn(1, self.sweeps(kind), kind)
            self.assertEqual(self.sweeps(kind)[1].wave, "pulse")

    def test_a_pulse_lead_sweeps_but_a_flute_or_a_saw_does_not(self):
        self.assertIn(0, self.sweeps("world"))
        self.assertNotIn(0, self.sweeps("boss"))                  # the buzz is a saw
        self.assertNotIn(0, self.sweeps("world", "calm"))         # calm trades the lead for a flute
        self.assertNotIn(0, self.sweeps("town"))

    def test_a_pad_becomes_a_pulse_pad(self):
        for kind in ("maze", "gameover"):
            pads = self.sweeps(kind)
            self.assertEqual(list(pads), [2], kind)
            self.assertEqual(pads[2].wave, "pulse")
            self.assertLess(pads[2].pws_from, pads[2].pws_to)

    def test_the_numbers_are_in_range(self):
        for kind in musicgen.KINDS:
            for mood in MOODS:
                for i in self.sweeps(kind, mood).values():
                    self.assertTrue(1 <= i.pws_from <= 99 and 1 <= i.pws_to <= 99 and 1 <= i.pws_rate <= 15)


class DegreeTests(unittest.TestCase):
    def test_the_voice_lines_are_degrees_under_a_key_and_the_drums_keep_letters(self):
        text = musicgen.generate("fight", 3, degrees=True)
        for v in (1, 2, 3):
            first = lane(text, "V%d" % v)[0]
            self.assertRegex(first, r" key [a-g]#? (minor|phrygian|dorian) ")
            for l in lane(text, "V%d" % v):
                notes = re.sub(r"^V\d: .*? key \S+ \S+ ", "", l)
                self.assertNotRegex(re.sub(r"@\w+", "", notes), r"[a-g]\d|[a-g]#", (v, l))
        self.assertNotIn("key", lane(text, "V4")[0])

    def test_the_modes_the_generator_uses_are_all_known_scales(self):
        seen = set()
        for kind in musicgen.KINDS:
            for mood in MOODS:
                for seed in range(1, 6):
                    text = musicgen.generate(kind, seed, mood, degrees=True)
                    seen.add(re.search(r" key \S+ (\S+) ", text).group(1))
        self.assertEqual(seen, set(musicgen.SCALES))

    def test_changing_the_key_line_transposes_the_song(self):
        text = musicgen.generate("world", 3, degrees=True)
        key = re.search(r"key ([a-g]#?) (\w+)", text)
        tonic = mml.NOTE_NAMES.index(key.group(1))
        up = text.replace("key %s " % key.group(1), "key %s " % mml.NOTE_NAMES[(tonic + 2) % 12])
        a, b = mml.parse(text), mml.parse(up)
        for v in range(3):
            if tonic + 2 < 12:
                self.assertEqual([n.pitch + 2 for n in a.voices[v]], [n.pitch for n in b.voices[v]])

    def test_the_comment_and_record_say_so(self):
        text = musicgen.generate("town", 3, degrees=True)
        self.assertIn("degrees", re.search(r"^comment .*", text, re.M).group(0))
        self.assertEqual(musicgen.fx_options(text), ((), True))
        self.assertEqual(musicgen.meta(text)["fx"], "degrees")


class HeaderTests(unittest.TestCase):
    def test_the_record_lists_what_was_written_and_nothing_else(self):
        text = musicgen.generate("boss", 7, "tense", fx="all", degrees=True, dac=True)
        self.assertEqual(musicgen.fx_options(text), (ALL, True))
        self.assertEqual(musicgen.dac_options(text), (True, False, False))
        self.assertEqual(musicgen.meta(text)["fx"], "+".join(ALL + ("degrees",)))
        victory = musicgen.generate("victory", 7, fx="all")
        self.assertNotIn("filter", musicgen.fx_options(victory)[0])
        self.assertEqual(musicgen.fx_options(musicgen.generate("boss", 7, fx="echo,sweep"))[0], ("echo", "sweep"))

    def test_the_record_line_stays_first_and_is_edited_still_works(self):
        for opts in ({"fx": "all"}, {"degrees": True}, {"fx": "echo", "dac": True}):
            text = musicgen.generate("fight", 5, **opts)
            self.assertTrue(text.startswith("// generated: kind=fight"))
            self.assertFalse(musicgen.is_edited(text))
            self.assertTrue(musicgen.is_edited(text.replace("tempo ", "tempo 1", 1)))
        self.assertTrue(musicgen.is_edited(musicgen.generate("fight", 5, fx="echo").replace("\necho ", "\necho 1", 1)))

    def test_old_records_still_read(self):
        old = "// generated: kind=town mood=- seed=3 tempo=95 sum=70add299\nV1: c"
        self.assertEqual(musicgen.meta(old), {"kind": "town", "mood": "", "seed": 3, "tempo": 95, "sum": "70add299"})
        self.assertEqual(musicgen.fx_options(old), ((), False))
        self.assertEqual(musicgen.fx_options("title x\nV1: c"), ((), False))

    def test_the_comment_names_the_options(self):
        text = musicgen.generate("boss", 7, fx="echo,filter", degrees=True)
        self.assertIn("fx echo+filter, degrees", text)


class LevelTests(unittest.TestCase):
    def test_every_kind_still_plays_and_stays_close_to_full_scale(self):
        for kind in musicgen.KINDS:
            for mood in ("", "bright"):
                tune = mml.parse(musicgen.generate(kind, 5, mood, fx="all", degrees=True))
                audio = np.asarray(render(tune, seconds=8))
                self.assertTrue(np.isfinite(audio).all())
                self.assertGreater(float(np.abs(audio).max()), 0.1)
                self.assertLess(float(np.abs(audio).max()), 1.1, (kind, mood))


class ExportTests(unittest.TestCase):
    def test_wav_midi_mml_and_package_all_work(self):
        text = musicgen.generate("boss", 5, "dark", fx="all", degrees=True, dac=True)
        tune = mml.parse(text)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "boss.wav"
            render_wav(tune, str(out))
            self.assertGreater(out.stat().st_size, 10000)
        data = midi.to_midi(Song.from_mml(text).compile())
        back = midi.from_midi(data)
        self.assertTrue(any(len(v) for v in back.compile().voices))
        pkg = pk.Package(name="gen")
        pkg.add_song("boss", Song.from_mml(text))
        again = pk.loads(pk.dumps(pkg)).songs["boss"].compile(pk.loads(pk.dumps(pkg)).library())
        self.assertEqual(melodic(again, 0), melodic(tune, 0))
        mml.parse(Song.from_mml(text).to_mml())

    def test_famistudio_says_what_it_leaves_out(self):
        tune = Song.from_mml(musicgen.generate("boss", 5, fx="all")).compile()
        out, rep = fex.dump(tune)
        left = " | ".join(rep.dropped)
        for what in ("echo and reverb", "slide into a note", "vibrato", "filter", "filter sweep", "pulse-width sweep"):
            self.assertIn(what, left)
        fam.load(out)                                                                           # and what is written reads back

    def test_equal_instruments_are_written_once(self):
        """A tweak in the middle of a voice makes a fresh instrument object each time; FamiStudio gets one per distinct sound."""
        tune = Song.from_mml(musicgen.generate("boss", 5, fx="all")).compile()
        out, _ = fex.dump(tune)
        names = re.findall(r'Instrument Name="([^"]+)"', out)
        self.assertEqual(len(names), len(set(names)))
        self.assertLess(len(names), 12)

    def test_a_plain_tune_loses_nothing_new(self):
        tune = Song.from_mml(musicgen.generate("boss", 5)).compile()
        _, rep = fex.dump(tune)
        left = " ".join(rep.dropped)
        for what in ("echo", "vibrato", "filter", "slide", "sweep"):
            self.assertNotIn(what, left)


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_each_flag_is_its_word(self):
        for w in ALL:
            rc, out, _ = self.run_cli("generate", "world", "--seed", "4", "--" + w)
            self.assertEqual((rc, out), (0, musicgen.generate("world", 4, fx=w)), w)

    def test_fx_means_all_six_and_degrees_is_its_own_flag(self):
        rc, out, _ = self.run_cli("generate", "boss", "--seed", "4", "--mood", "dark", "--fx", "--degrees", "--dac")
        self.assertEqual(out, musicgen.generate("boss", 4, "dark", fx="all", degrees=True, dac=True))
        rc, out, _ = self.run_cli("generate", "boss", "--seed", "4", "--echo", "--sweep")
        self.assertEqual(out, musicgen.generate("boss", 4, fx=("echo", "sweep")))

    def test_the_default_is_unchanged_and_a_file_and_wav_come_out(self):
        rc, out, _ = self.run_cli("generate", "town", "--seed", "3")
        self.assertEqual(out, musicgen.generate("town", 3))
        with tempfile.TemporaryDirectory() as d:
            f, w = Path(d) / "t.mml", Path(d) / "t.wav"
            rc, _, _ = self.run_cli("generate", "maze", "--seed", "2", "--fx", "-o", str(f), "--wav", str(w))
            self.assertEqual(rc, 0)
            self.assertEqual(f.read_text(encoding="utf-8"), musicgen.generate("maze", 2, fx="all"))
            self.assertGreater(w.stat().st_size, 10000)

    def test_a_flag_is_not_mistaken_for_the_kind(self):
        self.assertEqual(self.run_cli("generate", "--echo")[0], 2)


def key(k, mod=0):
    return pygame.event.Event(pygame.KEYDOWN, key=k, mod=mod, unicode="")


def down(pos):
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1)


def up(pos):
    return pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1)


class ComposerTests(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.app = Composer(self.screen, Prefs(view="roll"), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.app.frame()

    def click(self, r):
        self.app.frame([down(r.center), up(r.center)])
        self.app.frame()

    def test_the_words_turn_the_options_on(self):
        a = self.app
        self.assertTrue(a.new_from_generator("boss 3 echo reverb filter vibrato slide sweep degrees"))
        text = musicgen.generate("boss", 3, fx="all", degrees=True)
        self.assertEqual(melodic(a.tune, 0), melodic(mml.parse(text), 0))
        self.assertEqual(a.tune.echo, mml.parse(text).echo)
        self.assertTrue(a.new_from_generator("town 3 calm fx dac"))
        self.assertTrue(a.tune.echo)
        self.assertFalse(a.new_from_generator("town 3 wobble"))

    def test_the_dialog_has_a_toggle_for_every_word_and_ok_uses_them(self):
        a = self.app
        a.frame([key(pygame.K_n, pygame.KMOD_CTRL)])
        a.frame()
        opts = a.geo["new"]["opts"]
        self.assertEqual(list(opts), list(musicgen.FX_WORDS) + ["degrees", "dac", "dac-bass", "voice"])
        a.newdlg.update(kind=musicgen.KINDS.index("maze"), mood=0, seed="9")
        for w in ("echo", "filter", "degrees"):
            self.click(a.geo["new"]["opts"][w])
        self.assertEqual(a.newdlg["opts"], {"echo", "filter", "degrees"})
        self.assertEqual(a.new_dialog_text(), "maze 9  echo filter degrees")
        self.click(a.geo["new"]["opts"]["echo"])                                                 # a second click turns it off
        self.assertEqual(a.newdlg["opts"], {"filter", "degrees"})
        a.frame([key(pygame.K_RETURN)])
        self.assertIsNone(a.overlay)
        want = mml.parse(musicgen.generate("maze", 9, fx="filter", degrees=True))
        self.assertEqual(melodic(a.tune, 0), melodic(want, 0))
        self.assertEqual(a.tune.voices[0][0].inst.filt, "lp")
        self.assertFalse(a.tune.echo)

    def test_the_dialog_without_toggles_makes_the_plain_tune(self):
        a = self.app
        a.frame([key(pygame.K_n, pygame.KMOD_CTRL)])
        a.frame()
        a.newdlg.update(kind=musicgen.KINDS.index("boss"), mood=musicgen.MOODS.index("dark"), seed="12", opts=set())
        self.assertEqual(a.new_dialog_text(), "boss 12 dark")


if __name__ == "__main__":
    unittest.main()
