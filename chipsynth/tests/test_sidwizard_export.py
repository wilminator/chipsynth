# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writing SID-Wizard .swm files.  The reader (formats/sidwizard.py) is the check here: what is written must read back.

The real SID-Wizard 6502 player, SWMconvert and pysidwizard were also run against these files by hand (see PLAN-FORMATS.md); they
are not dependencies of the tests.  Nothing here uses a real song: every file is made from MML in this module."""
import contextlib
import io
import random
import struct
import tempfile
import unittest
from pathlib import Path

from chipsynth import __main__ as cli, exporter, mml, package as pk
from chipsynth.formats import sidwizard as sw, sidwizard_export as ex
from chipsynth.song import Song

BASE = """tempo 125
[instr]
lead wave=pulse pw=25 a=0 d=6 s=8 r=3
bass wave=tri a=1 d=4 s=10 r=2
V1: @lead l8 c d e f g2 r4 c4
V2: @lead o3 l4 c e g c
V3: @bass l4 o2 c e g c
"""


def tune_of(text):
    return Song.from_mml(text).compile()


def notes(tune, v):
    return [(n.start, n.dur, n.pitch) for n in tune.voices[v]]


def trip(text=BASE, **kw):
    """(the tune, the tune read back, the bytes, the export report, the import Result)"""
    tune = tune_of(text)
    data, rep = ex.dump(tune, **kw)
    res = sw.load(data, kw.get("region", "pal"))
    back = res.songs[0].song.compile(mml.builtin_library())
    return tune, back, data, rep, res


def frames(tune, v, limit=None):
    """[(video frame the note starts in (PAL), pitch)] of voice v, only the notes before `limit` frames."""
    out = [(round(tune.tick_to_sample(n.start) * 50.0 / 44100), n.pitch) for n in tune.voices[v]]
    return [x for x in out if limit is None or x[0] < limit]


def total_frames(tune):
    return round(tune.tick_to_sample(tune.length) * 50.0 / 44100)


def first_pass(a, b, v):
    """The notes of voice v in the tune and in what was read back, the read-back cut at the first pass: the importer may play a loop
    round more than once when the channels' tempo phases differ (see the report in PLAN-FORMATS.md)."""
    return frames(a, v), frames(b, v, total_frames(a))


def instr(res, n=0):
    return list(res.songs[0].song.instruments.values())[n]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main([str(a) for a in argv])
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def random_song(seed, once=False):
    rnd = random.Random(seed)
    lines = ["tempo %d" % rnd.choice([90, 110, 125, 140, 160])]
    if once:
        lines.append("once")
    lines.append("[instr]")
    names = []
    for i in range(rnd.randint(1, 4)):
        w = rnd.choice(["pulse", "saw", "tri", "noise"])
        extra = ""
        if w == "pulse" and rnd.random() < 0.5:
            extra += " pws=%d,%d,%d" % (rnd.randint(10, 40), rnd.randint(45, 80), rnd.randint(1, 3))
        if rnd.random() < 0.3:
            extra += " arp=0,%d,%d arprate=%d" % (rnd.choice([3, 4]), rnd.choice([7, 12]), rnd.choice([6, 8, 12]))
        if rnd.random() < 0.3 and w != "noise":
            extra += " vib=%d,%d" % (rnd.randint(2, 8), rnd.randint(2, 8))
        lines.append("i%d wave=%s pw=%d a=%d d=%d s=%d r=%d%s" % (i, w, rnd.randint(10, 90), rnd.randint(0, 4), rnd.randint(0, 9),
                                                                   rnd.randint(4, 15), rnd.randint(0, 9), extra))
        names.append("i%d" % i)
    bars = rnd.randint(2, 6)
    for v in range(3):
        toks = ["@" + rnd.choice(names), "o%d" % rnd.randint(2, 5)]
        left = bars * 16
        while left > 0:
            n = min(rnd.choice([1, 2, 2, 4, 4, 4, 8]), left)
            ln = {1: "16", 2: "8", 4: "4", 8: "2"}.get(n, "4")
            n = {"16": 1, "8": 2, "4": 4, "2": 8}[ln]
            toks.append(("r" if rnd.random() < 0.2 else rnd.choice("cdefgab")) + ln)
            left -= n
        lines.append("V%d: %s" % (v + 1, " ".join(toks)))
    return "\n".join(lines) + "\n"


class TimingTests(unittest.TestCase):
    def test_notes_and_timing_round_trip_exactly_at_the_native_tempo(self):
        a, b, *_ = trip()
        for v in range(3):
            self.assertEqual(notes(a, v), notes(b, v), v)

    def test_other_tempos_land_within_a_frame(self):
        for bpm in (90, 100, 133, 160):
            a, b, *_ = trip(BASE.replace("tempo 125", "tempo %d" % bpm))
            for v in range(3):
                x, y = first_pass(a, b, v)
                self.assertEqual([p for _, p in x], [p for _, p in y], (bpm, v))
                for (f, _), (g, _) in zip(x, y):
                    self.assertLessEqual(abs(f - g), 1, (bpm, v, x, y))

    def test_the_native_tempo_needs_no_tempo_changes_in_the_report(self):
        _, rep = ex.dump(tune_of(BASE))
        self.assertEqual(rep.approximated, [])

    def test_one_tempo_mode_is_a_valid_file_and_reports_the_error(self):
        _, _, _, rep, res = trip(BASE.replace("tempo 125", "tempo 133"), tempo="speed")
        self.assertTrue(any("tempo" in m for m in rep.approximated), rep.approximated)

    def test_tempo_changes_in_the_song_are_kept(self):
        a, b, *_ = trip("tempo 125\nV1: l4 c d t250 e f t125 g a\n")
        x, y = first_pass(a, b, 0)
        self.assertEqual([p for _, p in x], [p for _, p in y])
        # two quarters at 125 (24 frames each at 50 Hz), two at 250 (12), the rest at 125 again
        self.assertEqual([f for f, _ in y], [0, 24, 48, 60, 72, 96])
        self.assertEqual([f for f, _ in x], [0, 24, 48, 60, 72, 96])

    def test_a_song_too_fast_for_three_frame_rows_is_quantized_and_says_so(self):
        _, rep = ex.dump(tune_of("tempo 300\nV1: l16 c d e f g a b >c<\n"))
        self.assertTrue(any("nearest row" in m for m in rep.approximated), rep.approximated)

    def test_ntsc_is_timed_at_60_hz(self):
        a, b, *_ = trip(BASE.replace("tempo 125", "tempo 150"), region="ntsc")
        for v in range(3):
            self.assertEqual(notes(a, v), notes(b, v), v)


class StructureTests(unittest.TestCase):
    def test_the_header_and_limits_of_the_format_hold(self):
        data, _ = ex.dump(tune_of(BASE))
        self.assertEqual(struct.unpack("<H", data[:2])[0], ex.LOAD_ADDRESS)
        h = data[2:66]
        self.assertEqual(h[:4], b"SWM1")
        self.assertEqual(h[12], 3)                                  # three sequences
        self.assertEqual(h[14], 2)                                  # two instruments
        self.assertLessEqual(h[13], ex.MAX_PATTERNS)
        self.assertEqual(h[15:17], bytes([0, 0]))                   # no chords, no tempo table
        self.assertTrue(sw.looks_like(data))

    def test_it_is_deterministic(self):
        self.assertEqual(ex.dump(tune_of(BASE))[0], ex.dump(tune_of(BASE))[0])

    def test_title_and_author_are_written_in_capitals_and_cut(self):
        data, _ = ex.dump(tune_of("tempo 125\nV1: c\n"), name="a very long song name that goes on and on and on and on")
        text = data[2 + 24:2 + 64]
        self.assertEqual(len(text), 40)
        self.assertEqual(text, text.upper())

    def test_same_bars_share_a_pattern(self):
        tune = tune_of("tempo 125\nV1: " + "l4 c d e f " * 8 + "\nV2: l1 c c c c c c c c\n")
        data, _ = ex.dump(tune)
        self.assertLess(data[2 + 13], 8 * 3)

    def test_an_empty_song_is_a_valid_file(self):
        data, rep = ex.dump(tune_of("tempo 125\nV1: r1\n"))
        sw.load(data, "pal")

    def test_bad_options_are_errors(self):
        tune = tune_of(BASE)
        with self.assertRaises(ex.SidWizardExportError):
            ex.dump(tune, region="secam")
        with self.assertRaises(ex.SidWizardExportError):
            ex.dump(tune, multispeed=3)
        with self.assertRaises(ex.SidWizardExportError):
            ex.dump(tune, tempo="fast")

    def test_multispeed_sets_the_frame_speed_byte_and_keeps_the_rows_in_video_frames(self):
        d1, _ = ex.dump(tune_of(BASE), multispeed=1)
        d2, rep = ex.dump(tune_of(BASE), multispeed=2)
        d4, _ = ex.dump(tune_of(BASE), multispeed=4)
        self.assertEqual([d[2 + 4] for d in (d1, d2, d4)], [1, 2, 4])
        self.assertTrue(any("frame speed" in n for n in rep.notes), rep.notes)
        # the sequencer runs once a video frame whatever the speed, so the tempos are the same
        self.assertEqual(d1[-3:], d2[-3:])


class LoopTests(unittest.TestCase):
    def test_the_loop_point_comes_back(self):
        a, b, *_ = trip("tempo 125\nV1: l4 c d e f loop g a b >c<\nV2: l1 c d\n")
        self.assertEqual(a.loop_tick, b.loop_tick)
        self.assertEqual(notes(a, 0), notes(b, 0))

    def test_a_loop_in_the_middle_of_a_bar_gets_its_own_pattern(self):
        a, b, *_ = trip("tempo 125\nV1: l4 c d loop e f g a b >c<\n")
        self.assertEqual(a.loop_tick, b.loop_tick)
        self.assertEqual(notes(a, 0), notes(b, 0))

    def test_no_loop_marker_loops_from_the_start(self):
        a, b, *_ = trip()
        self.assertEqual((a.loop_tick, b.loop_tick), (0, 0))

    def test_a_once_song_ends_in_the_orderlist_and_the_last_note_survives(self):
        a, b, data, rep, _ = trip("tempo 125\nonce\nV1: l8 c d e f\nV2: l8 r r r c\n")
        for v in range(2):
            self.assertEqual(notes(a, v), notes(b, v))
        for seq in sw.parse(data).sequences[:3]:
            self.assertEqual(seq[-1], ex.SEQ_END)
            self.assertNotIn(ex.SEQ_LOOP, seq)
        self.assertTrue(any("plays once" in n for n in rep.notes), rep.notes)

    def test_a_looping_song_ends_every_orderlist_with_a_jump(self):
        _, _, data, _, _ = trip()
        for seq in sw.parse(data).sequences[:3]:
            self.assertEqual(seq[-2], ex.SEQ_LOOP)


class NotesTests(unittest.TestCase):
    def test_rests_gaps_and_leading_rests_round_trip(self):
        a, b, *_ = trip("tempo 125\nV1: r4 c8 r8 d4 r2 e4\nV2: l4 c r d r\nV3: r1 c1\n")
        for v in range(3):
            self.assertEqual(notes(a, v), notes(b, v), v)

    def test_a_long_song_in_many_patterns_round_trips(self):
        text = "tempo 125\nV1: " + " ".join("l4 %s" % x for x in "cdefgab" * 12) + "\nV2: l1 c d e f g a b c d e f\n"
        a, b, *_ = trip(text)
        self.assertEqual(notes(a, 0), notes(b, 0))
        self.assertEqual(notes(a, 1), notes(b, 1))

    def test_notes_outside_the_sids_range_move_by_octaves_and_are_reported(self):
        _, rep = ex.dump(tune_of("tempo 125\n[instr]\nx wave=saw\nV1: @x o8 c o0 c\n"))
        self.assertTrue(rep.approximated, rep.approximated)

    def test_the_lowest_and_highest_notes_are_kept(self):
        a, b, _, rep, _ = trip("tempo 125\nV1: o0 c4 o7 a4\n")
        self.assertEqual(notes(a, 0), notes(b, 0))
        self.assertEqual(rep.approximated, [])
        a, b, _, rep, _ = trip("tempo 125\nV1: o7 b4\n")          # B-7 is one above the player's last note
        self.assertEqual(notes(b, 0)[0][2], notes(a, 0)[0][2] - 12)
        self.assertTrue(rep.approximated)

    def test_a_song_too_long_to_fit_is_cut_and_that_is_reported(self):
        rnd = random.Random(3)
        text = "tempo 125\nV1: " + " ".join("o%d l16 %s" % (rnd.randint(2, 6), rnd.choice("cdefgab")) for _ in range(30000)) + "\n"
        data, rep = ex.dump(tune_of(text))
        sw.load(data, "pal")
        self.assertTrue(any("the last" in m and "patterns" in m for m in rep.dropped), rep.dropped)


class InstrumentTests(unittest.TestCase):
    def test_adsr_wave_and_pulse_width_round_trip(self):
        _, _, _, _, res = trip()
        i = instr(res, 0)
        self.assertEqual((i.wave, i.pw, i.a, i.d, i.s, i.r), ("pulse", 25, 0, 6, 8, 3))
        j = instr(res, 1)
        self.assertEqual((j.wave, j.a, j.d, j.s, j.r), ("tri", 1, 4, 10, 2))

    def test_every_wave(self):
        for w, want in (("pulse", "pulse"), ("saw", "saw"), ("tri", "tri"), ("noise", "noise")):
            _, _, _, _, res = trip("[instr]\nx wave=%s\nV1: @x c\n" % w)
            self.assertEqual(instr(res).wave, want)

    def test_a_sine_is_a_triangle_and_the_short_noise_is_just_noise(self):
        _, _, _, rep, res = trip("[instr]\ns wave=sine\nh wave=noise nmode=short\nV1: @s c @h c\n")
        self.assertEqual({i.wave for i in res.songs[0].song.instruments.values()}, {"tri", "noise"})
        self.assertTrue(any("sine" in m for m in rep.approximated), rep.approximated)

    def test_an_arpeggio_becomes_a_wavetable_and_reads_back(self):
        for arp, rate in (("0,4,7", 8), ("0,3,7,12", 12), ("0,12", 24)):
            _, _, _, rep, res = trip("[instr]\na wave=pulse arp=%s arprate=%d\nV1: @a c d\n" % (arp, rate))
            i = instr(res)
            self.assertEqual(tuple(i.arp), tuple(int(x) for x in arp.split(",")), arp)

    def test_a_pulse_sweep_becomes_a_pulse_table_and_reads_back(self):
        _, _, _, rep, res = trip("[instr]\nw wave=pulse pws=30,40,2\nV1: @w c\n")
        i = instr(res)
        self.assertGreater(i.pws_rate, 0)
        self.assertLess(i.pws_from, i.pws_to)

    def test_a_sweep_that_runs_downwards_reads_back_the_same_way(self):
        _, _, _, rep, res = trip("[instr]\nw wave=pulse pws=60,50,3\nV1: @w c\n")
        i = instr(res)
        self.assertGreater(i.pws_from, i.pws_to)

    def test_vibrato_becomes_the_instrument_byte_and_reads_back(self):
        # chipsynth's importer drops the player's calculated vibrato, so the byte is read off the file itself (the real player's
        # swing was measured by hand against ex.vibrato_swing)
        _, _, data, rep, _ = trip("[instr]\nv wave=saw vib=6,5\nV1: @v c\n")
        v = sw.parse(data).instruments[0].vibrato
        self.assertGreater(v >> 4, 0)
        self.assertGreater(v & 15, 0)
        self.assertTrue(any("vibrato" in m for m in rep.approximated), rep.approximated)
        _, _, data, rep, _ = trip("[instr]\nv wave=saw\nV1: @v c\n")
        self.assertEqual(sw.parse(data).instruments[0].vibrato, 0)

    def test_the_vibrato_model_matches_a_triangle_of_the_players_steps(self):
        peak, cycle = ex.vibrato_swing(49, 4, 3)
        self.assertEqual(cycle, 8)
        self.assertGreater(peak, 0)
        bigger, _ = ex.vibrato_swing(49, 4, 6)
        self.assertGreater(bigger, peak)

    def test_the_frequency_table_is_the_equal_tempered_one(self):
        self.assertEqual(len(ex.FREQ), 96)
        for i in range(1, 96):
            self.assertAlmostEqual(ex.FREQ[i], 440.0 * 2 ** ((i - 1 - 57) / 12.0) * (1 << 24) / 985248.0, delta=1.0)

    def test_same_instrument_twice_is_written_once_and_different_ones_get_their_own(self):
        _, _, _, _, res = trip("[instr]\na wave=pulse\nb wave=pulse\nc wave=saw\nV1: @a c @b d @c e\n")
        self.assertEqual(len(res.songs[0].song.instruments), 2)

    def test_more_instruments_than_a_song_holds_are_folded_and_reported(self):
        lines = ["tempo 125", "[instr]"] + ["i%d wave=pulse pw=%d a=%d" % (i, 10 + i, i % 16) for i in range(40)]
        lines.append("V1: " + " ".join("@i%d c8" % i for i in range(40)))
        data, rep = ex.dump(tune_of("\n".join(lines) + "\n"))
        self.assertLessEqual(data[2 + 14], ex.MAX_INSTRUMENTS)
        self.assertTrue(any("instrument" in m for m in rep.approximated), rep.approximated)

    def test_names_are_made_safe_and_unique(self):
        data, _ = ex.dump(tune_of("[instr]\nlead-one wave=pulse\nlead-two wave=saw\nV1: @lead-one c @lead-two d\n"))
        sw.load(data, "pal")


class ReportTests(unittest.TestCase):
    def test_what_a_sid_cannot_do_is_left_out_and_said(self):
        text = ("voices 5\ntempo 125\n[instr]\nf wave=pulse filter=lp,5 slide=4\nV1: @f c d\nV2: v8 c d\nV3: c\n"
                "V4: l4 c d e f\nV5: c\n")
        _, rep = ex.dump(tune_of(text))
        dropped = " | ".join(rep.dropped)
        self.assertIn("V4, V5", dropped)

    def test_unused_fourth_voice_and_dacs_are_not_reported(self):
        _, rep = ex.dump(tune_of(BASE))
        self.assertEqual(rep.dropped, [])

    def test_a_dac_track_is_left_out(self):
        from chipsynth import dpcm
        smp = dpcm.Sample(name="kick", rate=12, tracks=[[bytes.fromhex("aa55aa55")]])
        lib = dict(mml.builtin_library(), _mem={"kick": smp})
        tune = Song.from_mml("[sample]\nkick\n\nV1: c\nD1: kick\n", lib).compile(lib)
        _, rep = ex.dump(tune)
        self.assertTrue(any("DAC" in m for m in rep.dropped))

    def test_the_report_says_how_it_was_written(self):
        _, rep = ex.dump(tune_of(BASE), region="ntsc", multispeed=2)
        self.assertTrue(any("NTSC" in n and "frame speed x2" in n for n in rep.notes), rep.notes)
        self.assertTrue(any("two video frames" in n for n in rep.notes), rep.notes)


class DamageTests(unittest.TestCase):
    """The reader must meet any damaged file of ours with a SidWizardError, never another exception or a hang."""

    def setUp(self):
        self.files = [ex.dump(tune_of(random_song(s, once=s % 4 == 3)))[0] for s in range(6)] + [ex.dump(tune_of(BASE))[0]]

    def attempt(self, data):
        try:
            sw.load(data, "pal")
        except sw.SidWizardError:
            pass

    def test_every_truncation_is_an_error_or_a_song(self):
        data = self.files[-1]
        for n in range(0, len(data), 3):
            self.attempt(data[:n])

    def test_random_byte_damage_is_an_error_or_a_song(self):
        rnd = random.Random(7)
        for data in self.files:
            for _ in range(60):
                bad = bytearray(data)
                for _ in range(rnd.randint(1, 6)):
                    bad[rnd.randrange(len(bad))] = rnd.randrange(256)
                self.attempt(bytes(bad))

    def test_random_songs_export_and_read_back(self):
        for seed in range(25):
            tune = tune_of(random_song(seed, once=seed % 5 == 3))
            data, rep = ex.dump(tune)
            back = sw.load(data, "pal").songs[0].song.compile(mml.builtin_library())
            for v in range(3):
                x, y = first_pass(tune, back, v)
                self.assertEqual([p for _, p in x], [p for _, p in y], (seed, v))
                for (f, _), (g, _) in zip(x, y):
                    self.assertLessEqual(abs(f - g), 1, (seed, v))

    def test_random_songs_export_in_every_mode(self):
        for seed in range(8):
            tune = tune_of(random_song(seed))
            for region in ("pal", "ntsc"):
                for mult in (1, 2, 4):
                    for tempo in ("exact", "speed"):
                        data, _ = ex.dump(tune, region, mult, tempo=tempo)
                        self.assertTrue(sw.looks_like(data))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="t")
        pkg.add_song("tune", Song.from_mml(BASE))
        pk.save(pkg, self.tmp / "t.csp")

    def test_export_song_writes_a_swm_that_imports_again(self):
        rc, out, err = run("export-song", self.tmp / "t.csp", "-o", self.tmp / "t.swm")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.tmp / "t.swm").read_bytes()[2:6], b"SWM1")
        self.assertIn("PAL", err)
        self.assertEqual(run("import", self.tmp / "u.csp", self.tmp / "t.swm")[0], 0)
        got = pk.load(self.tmp / "u.csp").songs["t"].compile(mml.builtin_library())
        self.assertEqual(notes(got, 0), notes(tune_of(BASE), 0))

    def test_region_and_multispeed_are_options_and_the_format_can_be_named(self):
        out = self.tmp / "x.bin"
        rc, _, err = run("export-song", self.tmp / "t.csp", "-o", out, "-f", "sidwizard", "--region", "ntsc", "--multispeed", "2")
        self.assertEqual(rc, 0, err)
        self.assertIn("NTSC", err)
        self.assertEqual(out.read_bytes()[2 + 4], 2)
        rc, _, err2 = run("export-song", self.tmp / "t.csp", "-o", self.tmp / "y.swm", "--gt-tempo", "speed")
        self.assertEqual(rc, 0, err2)

    def test_multispeed_3_is_refused(self):
        rc, _, _ = run("export-song", self.tmp / "t.csp", "-o", self.tmp / "z.swm", "--multispeed", "3")
        self.assertEqual(rc, 2)

    def test_the_suffix_picks_the_format_and_goattracker_is_unchanged(self):
        self.assertEqual(exporter.format_of(Path("a.swm")), "sidwizard")
        self.assertIn("sidwizard", exporter.FORMATS)
        rc, _, err = run("export-song", self.tmp / "t.csp", "-o", self.tmp / "g.sng")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.tmp / "g.sng").read_bytes()[:4], b"GTS5")


if __name__ == "__main__":
    unittest.main()
