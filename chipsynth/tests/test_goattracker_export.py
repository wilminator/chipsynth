# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writing GoatTracker 2 .sng files.  The reader (formats/goattracker.py) is the check: what is written must read back.

Nothing here uses a real song: every file is made from MML in this module."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from chipsynth import __main__ as cli, mml, package as pk
from chipsynth.formats import goattracker as gt, goattracker_export as ex
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
    res = gt.load(data, region=kw.get("region", "pal"), multispeed=kw.get("multispeed", 1))
    back = res.songs[0].song.compile(mml.builtin_library())
    return tune, back, data, rep, res


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main([str(a) for a in argv])
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class TimingTests(unittest.TestCase):
    def test_notes_and_timing_round_trip_exactly_at_the_native_tempo(self):
        for region, bpm in (("pal", 125), ("ntsc", 150)):
            for mult in (1, 2, 4):
                text = BASE.replace("tempo 125", "tempo %d" % bpm)
                a, b, *_ = trip(text, region=region, multispeed=mult)
                for v in range(3):
                    self.assertEqual(notes(a, v), notes(b, v), (region, mult, v))
                self.assertEqual((a.length, a.bpm), (b.length, b.bpm), (region, mult))

    def test_other_tempos_land_within_half_a_frame(self):
        for bpm in (97, 120, 133, 200):
            a, b, *_ = trip(BASE.replace("tempo 125", "tempo %d" % bpm))
            for v in range(3):
                got, want = notes(b, v), notes(a, v)
                self.assertEqual([p for _, _, p in got], [p for _, _, p in want])
                for (s1, _, _), (s2, _, _) in zip(got, want):
                    self.assertLessEqual(abs(s1 / 4 - a.tick_to_sample(s2) / 44100 * 50), 0.5 + 1e-3, (bpm, v))

    def test_a_tempo_that_is_not_whole_frames_changes_the_speed_on_the_rows_that_need_it(self):
        tune = tune_of(BASE.replace("tempo 125", "tempo 120"))
        data, rep = ex.dump(tune)
        t = gt.parse(data)
        speeds = [r[3] for p in t.patterns for r in p if r[2] == gt.CMD_SETTEMPO]
        self.assertIn(6, speeds)
        self.assertIn(7, speeds)
        self.assertTrue(any("tempo changes" in n for n in rep.notes))

    def test_one_speed_mode_writes_one_tempo_command_and_reports_the_error(self):
        tune = tune_of(BASE.replace("tempo 125", "tempo 120"))
        data, rep = ex.dump(tune, tempo="speed")
        t = gt.parse(data)
        self.assertEqual([r[3] for p in t.patterns for r in p if r[2] == gt.CMD_SETTEMPO], [6])
        self.assertTrue(any("one speed (6 frames a row)" in m and "fast" in m for m in rep.approximated), rep.approximated)

    def test_the_native_tempo_needs_one_tempo_command_and_nothing_in_the_report(self):
        tune = tune_of(BASE)
        data, rep = ex.dump(tune)
        t = gt.parse(data)
        self.assertEqual([r[3] for p in t.patterns for r in p if r[2] == gt.CMD_SETTEMPO], [6])
        self.assertEqual(rep.approximated, [])
        self.assertEqual(rep.dropped, [])

    def test_tempo_changes_in_the_song_are_kept(self):
        a, b, *_ = trip("tempo 125\nV1: l4 c d t250 e f t125 g a\n")
        self.assertEqual([p for _, _, p in notes(a, 0)], [p for _, _, p in notes(b, 0)])
        # frames: two quarters at 125 (24 frames each), two at 250 (12 frames each), the rest at 125 again
        self.assertEqual([s // 4 for s, _, _ in notes(b, 0)], [0, 24, 48, 60, 72, 96])

    def test_triplets_are_exact(self):
        a, b, *_ = trip("tempo 125\nV1: l8 c d e f g a b >c<\nV2: [c8 d e]3\n")
        self.assertEqual(notes(a, 1), notes(b, 1))

    def test_a_song_too_fast_for_three_frame_rows_is_quantized_and_says_so(self):
        _, b, _, rep, _ = trip("tempo 300\nV1: l16 c d e f g a b >c<\n")
        self.assertTrue(any("must last 3 frames" in m for m in rep.approximated), rep.approximated)
        self.assertTrue(notes(b, 0))
        _, b2, _, rep2, _ = trip("tempo 300\nV1: l16 c d e f g a b >c<\n", multispeed=2)   # twice the frames: rows fit
        self.assertFalse(any("must last 3 frames" in m for m in rep2.approximated))

    def test_a_very_slow_song_keeps_its_length(self):
        a, b, *_ = trip("tempo 30\nV1: l1 c d\n")
        self.assertEqual(notes(b, 0)[1][0] // 4, 400)


class StructureTests(unittest.TestCase):
    def test_the_header_and_limits_of_the_format_hold(self):
        data = trip()[2]
        self.assertEqual(data[:4], b"GTS5")
        t = gt.parse(data)
        self.assertEqual((t.title, t.version), ("", "GTS5"))
        self.assertEqual(len(t.orders), 1)
        for chan in t.orders[0]:
            self.assertEqual(chan[-2], gt.LOOPSONG)
            self.assertTrue(all(b < 208 for b in chan[:-2]))
        for p in t.patterns:
            self.assertLessEqual(len(p), 128)
            self.assertEqual(p[-1][0], gt.ENDPATT)
        for left, right in t.tables:
            self.assertLessEqual(len(left), 255)
            self.assertEqual(len(left), len(right))
        self.assertLessEqual(len(t.instruments), 63)

    def test_it_is_deterministic(self):
        self.assertEqual(ex.dump(tune_of(BASE))[0], ex.dump(tune_of(BASE))[0])

    def test_title_author_and_comment_are_written_and_a_long_title_is_cut(self):
        text = "title A very long title that does not fit in thirty-two bytes\nauthor Me\n" + BASE
        tune = tune_of(text)
        data, rep = ex.dump(tune)
        t = gt.parse(data)
        self.assertEqual(t.title, "A very long title that does not")
        self.assertEqual(t.author, "Me")
        self.assertTrue(any("title is cut" in m for m in rep.approximated))

    def test_same_bars_share_a_pattern(self):
        text = "tempo 125\nV1: " + "l4 c d e f " * 8 + "\n"
        t = gt.parse(ex.dump(tune_of(text))[0])
        self.assertLessEqual(len(t.patterns), 3)         # the bar, the empty bar for V2/V3 (shared), maybe the first with the tempo
        self.assertGreater(len(t.orders[0][0]), 8)

    def test_an_empty_song_is_a_valid_file(self):
        data, _ = ex.dump(tune_of("tempo 125\nV1: r1\n"))
        t = gt.parse(data)
        self.assertEqual(len(t.orders[0]), 3)

    def test_bad_options_are_errors(self):
        with self.assertRaises(ex.GoatTrackerExportError):
            ex.dump(tune_of(BASE), multispeed=3)
        with self.assertRaises(ex.GoatTrackerExportError):
            ex.dump(tune_of(BASE), region="secam")
        with self.assertRaises(ex.GoatTrackerExportError):
            ex.dump(tune_of(BASE), tempo="fast")


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

    def test_a_once_song_ends_in_silence_and_says_it_cannot_play_once(self):
        a, b, _, rep, _ = trip("tempo 125\nonce\nV1: l8 c d e f\nV2: l8 r r r c\n")
        for v in range(2):
            self.assertEqual(notes(a, v), notes(b, v))
        self.assertTrue(any("play once" in m for m in rep.approximated))

    def test_a_note_that_ends_with_the_song_does_not_ring_into_a_loop_that_starts_with_a_rest(self):
        text = "tempo 125\nV1: l4 c d e f c d e f\nV2: l1 r l4 loop r c d e\n"
        tune = tune_of(text)
        self.assertEqual(tune.loop_tick, 384)
        data, _ = ex.dump(tune)
        t = gt.parse(data)
        order = t.orders[0][1]
        second = t.patterns[order[1]]
        self.assertEqual(second[0][0], gt.KEYOFF)
        a, b, *_ = trip(text)
        self.assertEqual(notes(a, 1), notes(b, 1))


class NotesTests(unittest.TestCase):
    def test_rests_gaps_and_leading_rests_round_trip(self):
        a, b, *_ = trip("tempo 125\nV1: r8 l8 c r d e r r f g2 r2 c\nV2: r1 l4 c r e r\nV3: c8\n")
        for v in range(3):
            self.assertEqual(notes(a, v), notes(b, v), v)

    def test_a_long_song_in_many_patterns_round_trips(self):
        text = "tempo 125\nV1: " + " ".join("c d e f g a b >c<" for _ in range(30)) + "\nV2: " + "c1 " * 40 + "\n"
        a, b, *_ = trip(text)
        for v in range(3):
            self.assertEqual(notes(a, v), notes(b, v))

    def test_notes_outside_the_sids_range_move_by_octaves_and_are_reported(self):
        a, b, _, rep, _ = trip("tempo 125\nV1: o8 l4 c d\nV2: o0 c d\n")
        self.assertEqual([p % 12 for _, _, p in notes(a, 0)], [p % 12 for _, _, p in notes(b, 0)])
        self.assertTrue(all(0 <= p <= 92 for v in range(3) for _, _, p in notes(b, v)))
        self.assertTrue(any("outside the SID's range" in m for m in rep.approximated))

    def test_the_song_is_cut_when_it_will_not_fit_and_that_is_reported(self):
        bars = []
        for i in range(700):
            digits = [(i // (7 ** k)) % 7 for k in range(4)]
            bars.append("l4 " + " ".join("cdefgab"[d] for d in digits))
        text = "tempo 125\nV1: " + " ".join(bars) + "\nV2: " + " ".join(reversed(bars)) + "\nV3: " + " ".join(bars[::2] * 2) + "\n"
        tune = tune_of(text)
        data, rep = ex.dump(tune)
        t = gt.parse(data)
        self.assertLessEqual(len(t.patterns), 208)
        self.assertTrue(any("the last" in m for m in rep.dropped), rep.dropped)
        for chan in t.orders[0]:
            self.assertLessEqual(len(chan), 255)


class InstrumentTests(unittest.TestCase):
    def test_adsr_wave_and_pulse_width_round_trip(self):
        a, b, _, rep, res = trip()
        got = res.songs[0].song.instruments
        lead, bass = got["lead"], got["bass"]
        self.assertEqual((lead.wave, lead.pw, lead.a, lead.d, lead.s, lead.r), ("pulse", 25, 0, 6, 8, 3))
        self.assertEqual((bass.wave, bass.a, bass.d, bass.s, bass.r), ("tri", 1, 4, 10, 2))

    def test_every_wave(self):
        text = "[instr]\np wave=pulse\ns wave=saw\nt wave=tri\nn wave=noise\nV1: @p c @s c @t c @n c\n"
        _, _, _, rep, res = trip(text)
        got = {i.wave for i in res.songs[0].song.instruments.values()}
        self.assertEqual(got, {"pulse", "saw", "tri", "noise"})

    def test_every_pulse_width_reads_back(self):
        for pw in (1, 12, 25, 33, 50, 75, 99):
            _, _, _, _, res = trip("[instr]\nx wave=pulse pw=%d\nV1: @x c\n" % pw)
            self.assertEqual(list(res.songs[0].song.instruments.values())[0].pw, pw)

    def test_a_sine_is_a_triangle_and_the_short_noise_is_just_noise(self):
        _, _, _, rep, res = trip("[instr]\ns wave=sine\nh wave=noise nmode=short\nV1: @s c @h c\n")
        self.assertEqual({i.wave for i in res.songs[0].song.instruments.values()}, {"tri", "noise"})
        self.assertTrue(any("no sine" in m for m in rep.approximated))
        self.assertTrue(any("short noise" in m for m in rep.dropped))

    def test_an_arpeggio_becomes_a_wavetable_and_reads_back(self):
        for arp, rate in (("0,4,7", 8), ("0,3,7,12", 12), ("0,12", 24), ("0,5", 6)):
            _, b, _, rep, res = trip("[instr]\na wave=pulse arp=%s arprate=%d\nV1: @a c d\n" % (arp, rate))
            i = list(res.songs[0].song.instruments.values())[0]
            self.assertEqual((i.arp, i.arprate), (tuple(int(x) for x in arp.split(",")), rate), arp)

    def test_an_arpeggio_below_the_note_is_moved_up_and_the_sound_is_the_same(self):
        a, b, _, rep, res = trip("[instr]\na wave=pulse arp=-3,0,4 arprate=8\nV1: @a o4 c\n")
        i = list(res.songs[0].song.instruments.values())[0]
        self.assertEqual(i.arp, (0, 3, 7))
        self.assertEqual(notes(b, 0)[0][2], notes(a, 0)[0][2] - 3)
        self.assertTrue(any("below the note" in m for m in rep.approximated))

    def test_an_arpeggio_rate_that_is_not_whole_frames_is_reported(self):
        _, _, _, rep, _ = trip("[instr]\na wave=pulse arp=0,4 arprate=16\nV1: @a c\n")
        self.assertTrue(any("whole frames" in m for m in rep.approximated))

    def test_a_pulse_sweep_becomes_a_pulse_table_and_reads_back(self):
        _, _, _, rep, res = trip("[instr]\nw wave=pulse pws=30,40,2\nV1: @w c\n")
        i = list(res.songs[0].song.instruments.values())[0]
        self.assertEqual((i.pws_from, i.pws_to, i.pws_rate), (30, 40, 2))
        self.assertFalse(any("pulse sweep" in m for m in rep.approximated))

    def test_a_sweep_too_fast_for_the_player_is_narrowed_and_reported(self):
        _, _, _, rep, res = trip("[instr]\nw wave=pulse pws=20,70,6\nV1: @w c\n")
        i = list(res.songs[0].song.instruments.values())[0]
        self.assertEqual((i.pws_from, i.pws_rate), (20, 6))
        self.assertLess(i.pws_to, 70)
        self.assertTrue(any("faster than the player" in m for m in rep.approximated))

    def test_a_sweep_that_runs_downwards_reads_back_the_same_way(self):
        _, _, _, rep, res = trip("[instr]\nw wave=pulse pws=60,50,3\nV1: @w c\n")
        i = list(res.songs[0].song.instruments.values())[0]
        self.assertEqual((i.pws_from, i.pws_to, i.pws_rate), (60, 50, 3))

    def test_vibrato_becomes_a_speed_table_entry_and_reads_back(self):
        for rate, depth in ((6, 5), (8, 4), (4, 3), (3, 6), (2, 10), (1, 1)):
            _, _, _, rep, res = trip("[instr]\nv wave=saw vib=%d,%d\nV1: @v c\n" % (rate, depth))
            i = list(res.songs[0].song.instruments.values())[0]
            self.assertEqual((i.vib_rate, i.vib_depth), (rate, depth))
            self.assertTrue(any("triangle in the player" in m for m in rep.approximated))
            self.assertFalse(any("is written as" in m for m in rep.approximated))

    def test_a_vibrato_the_player_cannot_make_is_the_nearest_one_and_said(self):
        # the player's vibrato cycle is a whole number of frames, so at 50 Hz there is no rate 12 (and few depths at rate 10)
        _, _, _, rep, res = trip("[instr]\nv wave=saw vib=12,2\nV1: @v c\n")
        i = list(res.songs[0].song.instruments.values())[0]
        self.assertGreater(i.vib_rate, 0)
        self.assertTrue(any("vibrato rate 12 depth 2 is written as" in m for m in rep.approximated), rep.approximated)

    def test_vibrato_does_not_get_lost_on_the_row_that_sets_the_tempo(self):
        _, _, _, rep, res = trip("tempo 120\n[instr]\nv wave=saw vib=6,5\nV1: @v c d\n")
        self.assertTrue(any(i.vib_rate for i in res.songs[0].song.instruments.values()))

    def test_same_instrument_twice_is_written_once_and_different_ones_get_their_own(self):
        text = "[instr]\na wave=pulse\nb wave=pulse a=3\nV1: @a c @b c @a c\nV2: @a c\n"
        t = gt.parse(ex.dump(tune_of(text))[0])
        self.assertEqual([i.name for i in t.instruments], ["a", "b"])

    def test_shared_wavetables_stay_small(self):
        text = "[instr]\n" + "".join("i%d wave=pulse a=%d\n" % (i, i) for i in range(12)) + "V1: " + " ".join("@i%d c" % i for i in range(12)) + "\n"
        t = gt.parse(ex.dump(tune_of(text))[0])
        self.assertLessEqual(len(t.tables[gt.WTBL][0]), 2)

    def test_more_instruments_than_a_song_holds_are_folded_and_reported(self):
        text = "[instr]\n" + "".join("i%d wave=pulse a=%d d=%d s=%d r=0\n" % (i, i % 16, (i // 16) % 16, 5 + i // 64) for i in range(70)) + \
               "V1: " + " ".join("@i%d c" % i for i in range(70)) + "\n"
        data, rep = ex.dump(tune_of(text))
        t = gt.parse(data)
        self.assertEqual(len(t.instruments), 63)
        self.assertTrue(any("more than 63" in m for m in rep.approximated))

    def test_names_are_made_safe_and_unique(self):
        text = "[instr]\nlead_one wave=pulse\nlead_two wave=pulse a=2\nV1: @lead_one c @lead_two c\n"
        t = gt.parse(ex.dump(tune_of(text))[0])
        self.assertEqual([i.name for i in t.instruments], ["lead_one", "lead_two"])


class ReportTests(unittest.TestCase):
    def test_what_a_sid_cannot_do_is_left_out_and_said(self):
        text = ("voices 5\ntempo 125\n[instr]\nf wave=pulse filter=lp,5 slide=4\nV1: @f c d\nV2: v8 c d\nV3: c\n"
                "V4: l4 c d e f\nV5: c\n")
        _, rep = ex.dump(tune_of(text))
        dropped = " | ".join(rep.dropped)
        self.assertIn("V4, V5", dropped)
        self.assertIn("loudness", dropped)

    def test_unused_fourth_voice_and_dacs_are_not_reported(self):
        _, rep = ex.dump(tune_of(BASE))
        self.assertEqual(rep.dropped, [])

    def test_a_dac_track_is_left_out(self):
        text = "[sample]\nkick\n\nV1: c\nD1: kick\n"
        from chipsynth import dpcm
        smp = dpcm.Sample(name="kick", rate=12, tracks=[[bytes.fromhex("aa55aa55")]])
        lib = dict(mml.builtin_library(), _mem={"kick": smp})
        tune = Song.from_mml(text, lib).compile(lib)
        _, rep = ex.dump(tune)
        self.assertTrue(any("DAC" in m for m in rep.dropped))

    def test_the_report_says_how_it_was_written(self):
        _, rep = ex.dump(tune_of(BASE), region="ntsc", multispeed=2)
        self.assertTrue(any("NTSC" in n and "multispeed x2" in n for n in rep.notes))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="t")
        pkg.add_song("tune", Song.from_mml(BASE))
        pk.save(pkg, self.tmp / "t.csp")

    def test_export_song_writes_a_sng_that_imports_again(self):
        rc, out, err = run("export-song", self.tmp / "t.csp", "-o", self.tmp / "t.sng")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.tmp / "t.sng").read_bytes()[:4], b"GTS5")
        self.assertIn("PAL", err)
        self.assertEqual(run("import", self.tmp / "u.csp", self.tmp / "t.sng")[0], 0)
        got = pk.load(self.tmp / "u.csp").songs["t"].compile(mml.builtin_library())
        self.assertEqual(notes(got, 0), notes(tune_of(BASE), 0))

    def test_region_and_multispeed_are_options_and_the_format_can_be_named(self):
        out = self.tmp / "x.bin"
        rc, _, err = run("export-song", self.tmp / "t.csp", "-o", out, "-f", "goattracker", "--region", "ntsc", "--multispeed", "2")
        self.assertEqual(rc, 0, err)
        self.assertIn("NTSC", err)
        self.assertIn("multispeed x2", err)
        rc, _, err2 = run("export-song", self.tmp / "t.csp", "-o", self.tmp / "y.sng", "--gt-tempo", "speed")
        self.assertEqual(rc, 0, err2)

    def test_multispeed_3_is_refused(self):
        rc, _, err = run("export-song", self.tmp / "t.csp", "-o", self.tmp / "z.sng", "--multispeed", "3")
        self.assertEqual(rc, 2)

    def test_a_famistudio_export_is_still_ntsc_by_default(self):
        rc, _, err = run("export-song", self.tmp / "t.csp", "-o", self.tmp / "f.txt")
        self.assertEqual(rc, 0, err)
        self.assertIn("NTSC", err)


if __name__ == "__main__":
    unittest.main()
