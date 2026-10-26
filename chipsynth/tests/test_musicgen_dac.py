# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The generator's DAC options (drums, bass, voice accent) and the built-in kit behind them."""
import contextlib
import io
import re
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

from chipsynth import __main__ as cli
from chipsynth import dpcm, kit, mml, musicgen, package as pk
from chipsynth.song import Song
from chipsynth.synth import DPCM_RATES

ALL = {"dac": True, "dac_bass": True, "voice": True}

# crc32 of generate(kind, seed, mood, tempo) exactly as the generator wrote it before the DAC options existed
BASELINE = {
    ("title", 1, "", None): "3d684596",
    ("title", 42, "tense", None): "c193953a",
    ("title", 7, "calm", None): "1c178fef",
    ("title", 1234, "dark", 140): "64defb71",
    ("title", 99, "bright", None): "9e11a8e8",
    ("victory", 1, "", None): "e4f7b704",
    ("victory", 42, "tense", None): "99854403",
    ("victory", 7, "calm", None): "6c5ab844",
    ("victory", 1234, "dark", 140): "d0deadf9",
    ("victory", 99, "bright", None): "56f6a699",
    ("gameover", 1, "", None): "1dfbbd3f",
    ("gameover", 42, "tense", None): "006323a0",
    ("gameover", 7, "calm", None): "9ddbf955",
    ("gameover", 1234, "dark", 140): "52a03ac1",
    ("gameover", 99, "bright", None): "be377a68",
    ("fight", 1, "", None): "a819e582",
    ("fight", 42, "tense", None): "e24e7f91",
    ("fight", 7, "calm", None): "52edcfbc",
    ("fight", 1234, "dark", 140): "db1425ed",
    ("fight", 99, "bright", None): "05c66526",
    ("boss", 1, "", None): "673d93a3",
    ("boss", 42, "tense", None): "302f8e56",
    ("boss", 7, "calm", None): "e454095a",
    ("boss", 1234, "dark", 140): "856409ea",
    ("boss", 99, "bright", None): "f2efe9ab",
    ("world", 1, "", None): "4a332895",
    ("world", 42, "tense", None): "5fba3f1a",
    ("world", 7, "calm", None): "15824d95",
    ("world", 1234, "dark", 140): "0c2a853a",
    ("world", 99, "bright", None): "d3940e29",
    ("maze", 1, "", None): "00a70dba",
    ("maze", 42, "tense", None): "c811f0eb",
    ("maze", 7, "calm", None): "ec806bc7",
    ("maze", 1234, "dark", 140): "61a5f0c6",
    ("maze", 99, "bright", None): "76f2c506",
    ("town", 1, "", None): "40c1b8cc",
    ("town", 42, "tense", None): "f20921fb",
    ("town", 7, "calm", None): "39708476",
    ("town", 1234, "dark", 140): "58623cc5",
    ("town", 99, "bright", None): "93ae4261",
}


def crc(text):
    return "%08x" % (zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF)


def lane(text, label):
    return [l for l in text.split("\n") if l.startswith(label + ":")]


def names_in(text, label):
    return set(re.findall(r"kit_[A-Za-z_]+", " ".join(lane(text, label))))


class DefaultOutputTests(unittest.TestCase):
    def test_default_output_is_byte_identical_to_the_recorded_checksums(self):
        for (kind, seed, mood, tempo), want in BASELINE.items():
            self.assertEqual(crc(musicgen.generate(kind, seed, mood, tempo)), want, (kind, seed, mood, tempo))

    def test_options_off_is_the_same_as_not_passing_them(self):
        for kind in musicgen.KINDS:
            self.assertEqual(musicgen.generate(kind, 4, "tense"),
                             musicgen.generate(kind, 4, "tense", None, dac=False, dac_bass=False, voice=False))

    def test_a_plain_tune_has_no_dac_lines_samples_or_dac_record(self):
        for kind in musicgen.KINDS:
            text = musicgen.generate(kind, 2)
            self.assertNotIn("D1:", text)
            self.assertNotIn("D2:", text)
            self.assertNotIn("[sample]", text)
            self.assertNotIn("kit_", text)
            self.assertNotIn("dac", musicgen.meta(text))
            self.assertEqual(musicgen.dac_options(text), (False, False, False))


class OptionTests(unittest.TestCase):
    def test_same_arguments_same_song(self):
        for opts in ({"dac": True}, {"dac_bass": True}, {"voice": True}, ALL):
            a = musicgen.generate("boss", 11, "dark", **opts)
            self.assertEqual(a, musicgen.generate("boss", 11, "dark", **opts))

    def test_the_other_voices_do_not_change_when_the_dac_is_switched_on(self):
        for kind in musicgen.KINDS:
            plain = musicgen.generate(kind, 6, "bright")
            for opts in ({"dac": True}, {"dac_bass": True}, ALL):
                text = musicgen.generate(kind, 6, "bright", **opts)
                for v in ("V1", "V2"):
                    self.assertEqual(lane(text, v), lane(plain, v), (kind, v, opts))

    def test_dac_drums_replace_the_noise_voice(self):
        text = musicgen.generate("fight", 3, dac=True)
        self.assertEqual(lane(text, "V4"), [])
        self.assertTrue(lane(text, "V3"))
        self.assertTrue(lane(text, "D1") and lane(text, "D2"))
        self.assertNotIn("@kick", text)

    def test_drums_split_between_the_lanes_kick_and_snare_on_d1_hats_and_crash_on_d2(self):
        text = musicgen.generate("boss", 3, dac=True)
        d1, d2 = names_in(text, "D1"), names_in(text, "D2")
        self.assertTrue({"kit_kick", "kit_snare"} <= d1)
        self.assertTrue(d1 <= {"kit_kick", "kit_snare", "kit_tom", "kit_tomlo"})
        self.assertTrue({"kit_hat", "kit_crash"} <= d2)
        self.assertTrue(d2 <= {"kit_hat", "kit_ohat", "kit_crash"})

    def test_dac_bass_replaces_the_bass_voice_and_uses_d2(self):
        text = musicgen.generate("world", 3, dac_bass=True)
        self.assertEqual(lane(text, "V3"), [])
        self.assertTrue(lane(text, "V4"))                          # the noise drums stay
        self.assertEqual(lane(text, "D1"), [])
        self.assertTrue(all(n.startswith("kit_bass_") for n in names_in(text, "D2")))

    def test_drums_and_bass_together_share_d1_and_the_bass_has_d2(self):
        text = musicgen.generate("fight", 3, dac=True, dac_bass=True)
        self.assertEqual((lane(text, "V3"), lane(text, "V4")), ([], []))
        self.assertIn("kit_hat", names_in(text, "D1"))
        self.assertTrue(all(n.startswith("kit_bass_") for n in names_in(text, "D2")))

    def test_dac_bass_is_ignored_for_the_pad_bass_kinds(self):
        for kind in ("maze", "gameover"):
            text = musicgen.generate(kind, 3, dac_bass=True)
            self.assertEqual(text, musicgen.generate(kind, 3))
            self.assertEqual(musicgen.dac_options(text), (False, False, False))

    def test_quiet_kinds_get_a_quiet_dac_beat(self):
        for kind in ("town", "gameover", "maze"):
            text = musicgen.generate(kind, 3, dac=True)
            self.assertTrue(names_in(text, "D1") | names_in(text, "D2"), kind)
        self.assertNotIn("kit_snare", musicgen.generate("town", 3, dac=True))

    def test_calm_turns_a_busy_kit_down_to_a_soft_one(self):
        busy = musicgen.generate("fight", 3, "", dac=True)
        calm = musicgen.generate("fight", 3, "calm", dac=True)
        self.assertIn("kit_snare", busy)
        self.assertNotIn("kit_snare", calm)

    def test_tempo_changes_the_hats(self):
        fast = musicgen.generate("fight", 3, "", 175, dac=True)
        slow = musicgen.generate("fight", 3, "", 70, dac=True)
        self.assertGreater(len(re.findall(r"kit_hat", fast)), 1.5 * len(re.findall(r"kit_hat", slow)))   # eighths vs quarters; fills thin the fast ones

    def test_jingles_end_on_a_kick_and_crash(self):
        text = musicgen.generate("victory", 3, dac=True)
        self.assertIn("kit_kick", lane(text, "D1")[-1].split("|")[-2])
        self.assertIn("kit_crash", lane(text, "D2")[-1].split("|")[-2])


class VoiceAccentTests(unittest.TestCase):
    def test_off_by_default(self):
        for kind in musicgen.KINDS:
            self.assertNotIn("kit_hey", musicgen.generate(kind, 1, dac=True, dac_bass=True))

    def test_rare_and_only_on_the_kinds_that_have_a_spot(self):
        for kind in musicgen.KINDS:
            for seed in range(1, 8):
                text = musicgen.generate(kind, seed, voice=True)
                hits = len(re.findall(r"kit_hey(?![A-Za-z_])", " ".join(lane(text, "D1"))))
                self.assertLessEqual(hits, 2, (kind, seed))
                if kind not in musicgen.VOICE_SPOTS:
                    self.assertEqual(hits, 0, kind)

    def test_a_boss_has_its_intro_and_its_turnaround(self):
        text = musicgen.generate("boss", 5, voice=True)
        d1 = lane(text, "D1")
        self.assertIn("kit_hey", d1[0].split("|")[0])
        self.assertIn("kit_hey", d1[-1])
        self.assertEqual(musicgen.dac_options(text), (False, False, True))

    def test_record_says_voice_only_when_one_was_placed(self):
        for seed in range(1, 12):
            text = musicgen.generate("fight", seed, voice=True)
            self.assertEqual(musicgen.dac_options(text)[2], "kit_hey" in text)

    def test_the_voice_does_not_move_the_drums(self):
        a = musicgen.generate("boss", 5, dac=True)
        b = musicgen.generate("boss", 5, dac=True, voice=True)
        self.assertEqual(lane(a, "D2"), lane(b, "D2"))


class CompileTests(unittest.TestCase):
    def test_every_kind_mood_and_option_set_parses_with_no_warnings_and_plays_samples(self):
        sets = ({"dac": True}, {"dac_bass": True}, {"voice": True}, ALL)
        for kind in musicgen.KINDS:
            for mood in musicgen.MOODS:
                for opts in sets:
                    text = musicgen.generate(kind, 3, mood, **opts)
                    tune = mml.parse(text)
                    self.assertEqual(tune.warnings, [], (kind, mood, opts))
                    plays = [e for events in tune.dac for e in events if e[1] == "play"]
                    if any(musicgen.dac_options(text)):
                        self.assertTrue(plays, (kind, mood, opts))

    def test_dac_events_sit_on_the_sixteenth_grid_and_the_tune_is_whole_bars(self):
        tune = mml.parse(musicgen.generate("fight", 3, **ALL))
        self.assertEqual(tune.length % tune.bar_ticks, 0)
        for events in tune.dac:
            for tick, _, _, _ in events:
                self.assertEqual(tick % (mml.TICKS_WHOLE // 16), 0)

    def test_the_song_model_reads_and_writes_it_back(self):
        text = musicgen.generate("boss", 9, "dark", **ALL)
        song = Song.from_mml(text)
        self.assertTrue(song.samples)
        song.compile()
        Song.from_mml(song.to_mml()).compile()

    def test_the_declared_samples_are_exactly_the_ones_played(self):
        text = musicgen.generate("fight", 3, **ALL)
        declared = set(re.findall(r"^(kit_\w+) = kit_\w+$", text, re.M))
        self.assertEqual(declared, names_in(text, "D1") | names_in(text, "D2"))

    def test_every_dac_line_is_whole_bars(self):
        text = musicgen.generate("world", 2, **ALL)
        for label in ("D1", "D2"):
            for l in lane(text, label):
                self.assertTrue(l.rstrip().endswith("|"))
                self.assertEqual(l.count("|"), 4)


class HeaderTests(unittest.TestCase):
    def test_meta_reads_the_dac_record_and_old_records_still_read(self):
        text = musicgen.generate("boss", 7, "tense", None, dac=True, dac_bass=True, voice=True)
        m = musicgen.meta(text)
        self.assertEqual((m["kind"], m["mood"], m["seed"]), ("boss", "tense", 7))
        self.assertEqual(m["dac"], "dbv")
        self.assertEqual(musicgen.dac_options(text), (True, True, True))
        old = "// generated: kind=town mood=- seed=3 tempo=95 sum=70add299\nV1: c"
        self.assertEqual(musicgen.meta(old), {"kind": "town", "mood": "", "seed": 3, "tempo": 95, "sum": "70add299"})

    def test_each_option_has_its_letter(self):
        self.assertEqual(musicgen.meta(musicgen.generate("fight", 1, dac=True))["dac"], "d")
        self.assertEqual(musicgen.meta(musicgen.generate("fight", 1, dac_bass=True))["dac"], "b")
        self.assertEqual(musicgen.meta(musicgen.generate("boss", 1, voice=True))["dac"], "v")

    def test_is_edited_still_works_with_the_dac_lanes(self):
        for opts in ({"dac": True}, {"dac_bass": True}, ALL):
            text = musicgen.generate("fight", 5, **opts)
            self.assertFalse(musicgen.is_edited(text))
            self.assertTrue(musicgen.is_edited(text.replace("D2: ", "D2: r4 ", 1)))
            self.assertTrue(musicgen.is_edited(text.replace("tempo ", "tempo 1", 1)))

    def test_the_record_line_stays_first(self):
        self.assertTrue(musicgen.generate("fight", 5, **ALL).startswith("// generated: kind=fight"))


class KitTests(unittest.TestCase):
    def test_every_kit_sample_decodes_to_a_real_sound(self):
        for name in kit.NAMES + (kit.bass_name(28), kit.bass_name(45)):
            smp = kit.builtin(name)
            self.assertIsNotNone(smp, name)
            self.assertEqual(smp.name, name)
            self.assertTrue(all(0 < len(p) <= dpcm.MAX_PART for p in smp.tracks[0]), name)
            d = dpcm.decode_preview(smp)
            self.assertTrue(np.isfinite(d).all())
            self.assertGreater(float(np.sqrt((d ** 2).mean())), 0.04, name)
            self.assertLess(float(np.abs(d[-50:]).max()), 0.3, name)              # it ends near silence: no click
            self.assertLess(smp.seconds, 1.2)
            back = dpcm.loads(dpcm.dumps(smp), name)
            self.assertEqual((back.rate, back.tracks), (smp.rate, smp.tracks))

    def test_every_sample_a_song_declares_is_in_the_kit(self):
        for kind in musicgen.KINDS:
            text = musicgen.generate(kind, 4, "dark", **ALL)
            for name in re.findall(r"^(kit_\w+) = ", text, re.M):
                self.assertIsNotNone(kit.builtin(name), name)

    def test_unknown_names_are_not_in_the_kit(self):
        for name in ("kick", "kit_nothing", "kit_bass_h_two", "kit_bass_c_nine", "", None):
            self.assertIsNone(kit.builtin(name), name)

    def test_building_it_twice_is_the_same_and_a_copy_is_safe_to_change(self):
        a, b = kit.builtin("kit_snare"), kit.builtin("kit_snare")
        self.assertEqual(a.tracks, b.tracks)
        a.tracks[0].clear()
        self.assertTrue(kit.builtin("kit_snare").tracks[0])

    def test_bass_names_round_trip_and_the_tone_is_at_the_right_pitch(self):
        for pitch in (24, 28, 35, 40, 46):
            self.assertEqual(kit.bass_pitch(kit.bass_name(pitch)), pitch)
            d = dpcm.decode_preview(kit.builtin(kit.bass_name(pitch)))[: int(0.6 * DPCM_RATES[15])]
            spectrum = np.abs(np.fft.rfft(d * np.hanning(len(d))))
            freq = np.fft.rfftfreq(len(d), 1.0 / DPCM_RATES[15])[int(spectrum.argmax())]
            want = 440.0 * 2.0 ** ((pitch - 57) / 12.0)
            self.assertAlmostEqual(freq / want, 1.0, delta=0.04, msg=str((pitch, freq, want)))


class LoaderTests(unittest.TestCase):
    def test_a_generated_song_plays_from_a_library_with_no_sample_folder_or_files(self):
        lib = mml.builtin_library()
        self.assertIsNone(lib["_sampledir"])
        tune = mml.parse(musicgen.generate("fight", 3, **ALL), lib)
        self.assertTrue(tune.dac[0] and tune.dac[1])

    def test_a_file_in_the_sample_folder_replaces_the_kit_sample(self):
        mine = dpcm.convert(np.sin(2 * np.pi * 80 * np.arange(8000) / 44100), 44100, rate=10, name="kit_kick")
        with tempfile.TemporaryDirectory() as d:
            dpcm.save(Path(d) / "samples" / "kit_kick.dpcm", mine)
            lib = mml.load_library(Path(d) / "lib")
            tune = mml.parse("tempo 120\nD1: kit_kick4 kit_snare4", lib)
            plays = [e[2] for e in tune.dac[0]]
            self.assertEqual(plays[0].tracks, mine.tracks)
            self.assertEqual(plays[1].tracks, kit.builtin("kit_snare").tracks)

    def test_kit_files_written_to_a_folder_are_what_the_parser_loads(self):
        with tempfile.TemporaryDirectory() as d:
            paths = kit.write_files(Path(d) / "samples", ["kit_kick", "kit_hey", "nothing"])
            self.assertEqual(sorted(p.name for p in paths), ["kit_hey.dpcm", "kit_kick.dpcm"])
            for p in paths:
                self.assertEqual(dpcm.load(p).tracks, kit.builtin(p.stem).tracks)

    def test_a_package_embeds_the_kit_and_plays_without_it(self):
        text = musicgen.generate("boss", 9, "dark", **ALL)
        pkg = pk.Package(name="gen")
        pkg.add_song("boss", Song.from_mml(text))
        used = set(re.findall(r"kit_[A-Za-z_]+", text))
        self.assertTrue(used <= set(pkg.samples), used - set(pkg.samples))
        back = pk.loads(pk.dumps(pkg))
        tune = back.songs["boss"].compile(back.library())
        self.assertTrue(tune.dac[0] and tune.dac[1])
        self.assertTrue(used <= set(back.samples))


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_generate_prints_the_same_text_as_the_function(self):
        rc, out, _ = self.run_cli("generate", "fight", "--seed", "42", "--mood", "tense", "--dac", "--dac-bass", "--voice")
        self.assertEqual(rc, 0)
        self.assertEqual(out, musicgen.generate("fight", 42, "tense", None, dac=True, dac_bass=True, voice=True))
        rc, out, _ = self.run_cli("generate", "town", "--seed", "3", "--tempo", "100")
        self.assertEqual(out, musicgen.generate("town", 3, "", 100))

    def test_generate_writes_a_file_samples_and_a_wav(self):
        with tempfile.TemporaryDirectory() as d:
            f, s, w = Path(d) / "f.mml", Path(d) / "smp", Path(d) / "f.wav"
            rc, _, _ = self.run_cli("generate", "world", "--seed", "2", "--dac", "-o", str(f), "--samples", str(s),
                                    "--wav", str(w))
            self.assertEqual(rc, 0)
            text = f.read_text(encoding="utf-8")
            self.assertEqual(text, musicgen.generate("world", 2, dac=True))
            for name in re.findall(r"^(kit_\w+) = ", text, re.M):
                self.assertTrue((s / (name + ".dpcm")).is_file(), name)
            self.assertGreater(w.stat().st_size, 10000)

    def test_bad_arguments(self):
        for args in (("generate",), ("generate", "lullaby"), ("generate", "fight", "--mood", "spooky"),
                     ("generate", "fight", "--seed", "x")):
            self.assertEqual(self.run_cli(*args)[0], 2, args)


if __name__ == "__main__":
    unittest.main()
