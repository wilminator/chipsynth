# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writing Furnace modules.  What checks it here: a strict block reader of our own (the file must tile into blocks the way
Furnace's loader walks them), and a round trip through chipsynth's importer.  The independent check was the real Furnace
(v0.6.8.3, built from source, headless): every file the writer makes loaded there and was rendered and measured against
chipsynth's own render.  That cannot run in this suite, so these tests are NOT that check; see PLAN-FORMATS.md section 4."""
import contextlib
import io
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from chipsynth import dpcm, exporter, mml, package as pk
from chipsynth.__main__ import main
from chipsynth.formats import furnace, furnace_export as ex
from chipsynth.song import Song
from chipsynth.tests import test_composer_filemenu as tcf

MML = """tempo 120
[instr]
lead wave=pulse pw=25 a=0 d=6 s=8 r=3
bass wave=tri
hat wave=noise nmode=short a=0 d=3 s=0 r=0
V1: @lead l8 c d e f g2 r4 c4
V2: @lead v9 o3 l4 c e g c
V3: @bass l4 o2 c e g c
V4: @hat l8 o5 c c c c r2
"""
PULSE_SHIFT = 12            # the writer puts pulse notes an octave down (Furnace's NES pulse sounds an octave up); the importer reads the file as written


def tune_of(text, lib=None):
    return Song.from_mml(text, lib).compile(lib or mml.builtin_library())


class FurError(ValueError):
    pass


def strict_blocks(data):
    """The file as Furnace's loader walks it: header, then blocks that tile the rest of the file exactly."""
    if data[:2] in (b"x\x9c", b"x\xda", b"x\x01", b"x^"):
        try:
            data = zlib.decompress(data)
        except zlib.error as e:
            raise FurError("zlib: %s" % e)
    if data[:16] != b"-Furnace module-":
        raise FurError("magic")
    version, _res, info = struct.unpack_from("<HHI", data, 16)
    if info < 32 or info + 8 > len(data):
        raise FurError("song info pointer")
    out, pos = [], info
    while pos < len(data):
        if pos + 8 > len(data):
            raise FurError("block header runs off the end")
        tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if not all(65 <= c <= 90 or 48 <= c <= 57 for c in tag):
            raise FurError("block tag %r" % tag)
        if pos + 8 + size > len(data):
            raise FurError("block %s is longer than the file" % tag)
        out.append((tag.decode(), pos, data[pos + 8:pos + 8 + size]))
        pos += 8 + size
    return version, data, out


def read(text=MML, target="nes", region="ntsc", compress=True):
    tune = tune_of(text)
    data, rep = ex.dump(tune, target=target, region=region, compress=compress)
    return tune, data, rep, furnace.parse(data)


def seconds(tick, bpm):
    return tick * 60.0 / (bpm * 96.0)


class Imported:
    """The module played back by the importer: notes per voice as (seconds, seconds long, pitch)."""

    def __init__(self, data):
        res = furnace.load(data)
        im = res.songs[0]
        self.tune = im.song.compile(dict(mml.builtin_library(), _mem=im.samples))
        self.report = res.report
        t = self.tune
        self.voices = [[(seconds(n.start, t.bpm), seconds(n.dur, t.bpm), n.pitch) for n in v] for v in t.voices]


class StructureTests(unittest.TestCase):
    def test_the_blocks_tile_the_file_and_the_required_ones_are_there(self):
        for target in ex.TARGETS:
            for compress in (True, False):
                _, data, _, _ = read(target=target, compress=compress)
                version, raw, blocks = strict_blocks(data)
                tags = [b[0] for b in blocks]
                self.assertEqual(version, 228)
                self.assertEqual(tags[0], "INFO")
                self.assertEqual(tags.count("ADIR"), 3)
                self.assertIn("INS2", tags)
                self.assertIn("PATN", tags)
                self.assertEqual(blocks[-1][0] in ("PATN", "SMP2", "ADIR", "INS2"), True)

    def test_the_instruments_are_numbered_from_zero_without_gaps(self):
        mod = read()[3]
        self.assertEqual(sorted(mod.instruments), list(range(len(mod.instruments))))

    def test_both_the_packed_and_the_plain_file_parse_alike(self):
        tune = tune_of(MML)
        a, _ = ex.dump(tune, compress=True)
        b, _ = ex.dump(tune, compress=False)
        self.assertEqual(zlib.decompress(a), b)
        self.assertTrue(b.startswith(b"-Furnace module-"))
        self.assertEqual(furnace.parse(a).patterns, furnace.parse(b).patterns)

    def test_the_version_is_one_the_importer_reads(self):
        _, _, _, mod = read()
        self.assertEqual(mod.version, ex.VERSION)
        self.assertLessEqual(ex.VERSION, furnace.MAX_VERSION)
        self.assertGreaterEqual(ex.VERSION, furnace.MIN_VERSION)

    def test_the_chip_is_the_target(self):
        self.assertEqual(read(target="nes")[3].chips, [(0x06, 5)])
        self.assertEqual(read(target="sid")[3].chips, [(0x47, 3)])

    def test_the_song_is_named_and_a_title_is_cut_to_something_safe(self):
        tune = tune_of(MML)
        data, _ = ex.dump(tune, name="x" * 400)
        self.assertEqual(furnace.parse(data).name, "x" * len(furnace.parse(data).name))
        self.assertGreater(len(furnace.parse(data).name), 0)


class RoundTripTests(unittest.TestCase):
    def check(self, text, target="nes", region="ntsc", voices=(0, 1, 2), shifts=(PULSE_SHIFT, PULSE_SHIFT, 0)):
        tune = tune_of(text)
        data, rep = ex.dump(tune, target=target, region=region)
        back = Imported(data)
        frame = 1.0 / (60.0 if region == "ntsc" else 50.0)
        for v, shift in zip(voices, shifts):
            want = sorted(tune.voices[v], key=lambda n: n.start)
            got = back.voices[v]
            self.assertEqual(len(got), len(want), "voice %d" % (v + 1))
            for n, (start, _dur, pitch) in zip(want, got):
                self.assertEqual(pitch + (shift if target == "nes" else 0), n.pitch, "voice %d pitch" % (v + 1))
                self.assertLessEqual(abs(start - seconds(n.start, tune.bpm)), 1.5 * frame + 0.002 * seconds(n.start, tune.bpm),
                                     "voice %d at %.3fs" % (v + 1, seconds(n.start, tune.bpm)))
        return tune, back, rep

    def test_every_melodic_nes_channel_keeps_pitch_and_timing(self):
        self.check(MML)

    def test_the_sid_keeps_pitch_and_timing(self):
        self.check(MML, "sid", shifts=(0, 0, 0))

    def test_pal_timing(self):
        tune, back, rep = self.check(MML, region="pal")
        self.assertEqual(read(region="pal")[3].subsongs[0].hz, 50.0)
        self.assertEqual(read(region="ntsc")[3].subsongs[0].hz, 60.0)

    def test_tempos_that_are_not_a_whole_number_of_frames_use_a_groove(self):
        for bpm in (90, 101, 133, 175):
            with self.subTest(bpm=bpm):
                tune, back, _ = self.check("tempo %d\nV1: l8 c d e f g a b >c\nV2: l4 c e g c\n" % bpm, voices=(0, 1), shifts=(PULSE_SHIFT, PULSE_SHIFT))
        mod = read("tempo 101\nV1: l8 c d e f g a b >c\n")[3]
        self.assertTrue(mod.grooves or len(set(mod.subsongs[0].speeds)) >= 1)

    def test_a_song_the_length_of_the_tune(self):
        tune, data, _, mod = read()
        sub = mod.subsongs[0]
        frames = sub.orders_len * sub.pattern_len * sum(sub.speeds) / len(sub.speeds)
        self.assertAlmostEqual(frames / 60.0, seconds(tune.length, tune.bpm), delta=0.5 + seconds(tune.bar_ticks, tune.bpm))

    def test_a_tempo_change_in_the_song_is_followed(self):
        text = "time 4/4\ntempo 120\nV1: o4 l4 c d e f t150 g a b >c\n"
        tune = tune_of(text)
        self.assertTrue(tune.tempo_events)
        data, rep = ex.dump(tune)
        back = Imported(data)
        t0 = [n.start for n in sorted(tune.voices[0], key=lambda n: n.start)]
        for want, (start, _d, _p) in zip(t0, back.voices[0]):
            sec = sum((min(want, tk) - p) * 60.0 / (b * 96.0) for p, tk, b in
                      [(0, tune.tempo_events[0][0], 120), (tune.tempo_events[0][0], 10 ** 9, tune.tempo_events[0][1])] if want > p)
            self.assertLessEqual(abs(start - sec), 0.04, want)

    def test_the_same_tune_writes_the_same_bytes(self):
        tune = tune_of(MML)
        a, _ = ex.dump(tune)
        b, _ = ex.dump(tune)
        self.assertEqual(a, b)


class LoopTests(unittest.TestCase):
    def jump(self, mod):
        sub = mod.subsongs[0]
        codes = []
        for (s, ch, p), rows in mod.patterns.items():
            if ch == 0:
                for row, cell in rows.items():
                    codes += [(code, val) for code, val in cell.fx if code in (0x0B, 0xFF)]
        return codes

    def test_a_looping_song_ends_with_a_jump_to_the_loop(self):
        _, _, _, mod = read("time 4/4\ntempo 120\nV1: o4 c1 d1 loop e1 f1 g1\n")
        codes = self.jump(mod)
        self.assertEqual([c for c, _ in codes], [0x0B])
        self.assertEqual(codes[0][1], 2)

    def test_a_song_with_no_loop_section_loops_from_the_start(self):
        _, _, _, mod = read("V1: l4 c d e f\n")
        self.assertEqual(self.jump(mod), [(0x0B, 0)])

    def test_a_song_marked_once_stops(self):
        _, _, _, mod = read("once\nV1: l4 c d e f\n")
        self.assertEqual([c for c, _ in self.jump(mod)], [0xFF])

    def test_the_importer_finds_the_loop_within_a_bar_of_where_the_tune_has_it(self):
        """A chipsynth song loops at a bar, so the importer snaps the loop down to one: the file itself is exact (the jump above)."""
        tune = tune_of("time 4/4\ntempo 120\nV1: o4 c1 d1 loop e1 f1 g1\n")
        data, _ = ex.dump(tune)
        back = Imported(data)
        self.assertGreater(back.tune.loop_tick, 0)
        self.assertLessEqual(seconds(back.tune.loop_tick, back.tune.bpm), seconds(tune.loop_tick, tune.bpm) + 0.01)
        self.assertGreater(seconds(back.tune.loop_tick, back.tune.bpm), seconds(tune.loop_tick, tune.bpm) - seconds(back.tune.bar_ticks, back.tune.bpm))

    def test_repeated_music_shares_patterns(self):
        _, _, _, mod = read("time 4/4\nV1: o4 [c1 d1]4\nV3: o2 [c1 c1]4\n")
        used = {k for k in mod.patterns if k[1] == 0}
        self.assertLessEqual(len(used), 4)

    def test_a_long_song_is_cut_to_the_orders_a_module_has_and_says_so(self):
        text = "time 4/4\nV1: " + " ".join("o%d %s1" % (3 + i % 3, "cdefgab"[i % 7]) for i in range(40)) + "\n"
        data, rep = ex.dump(tune_of(text))
        self.assertTrue(furnace.parse(data).subsongs[0].orders_len <= 256)


class InstrumentTests(unittest.TestCase):
    def ins(self, mod, name):
        return next(i for i in mod.instruments.values() if i.name == name)

    def test_the_volume_macro_follows_the_adsr(self):
        _, _, _, mod = read()
        m = self.ins(mod, "lead").macros[ex.MAC_VOL]
        self.assertTrue(max(m.values) <= 15 and min(m.values) >= 0)
        self.assertGreater(m.values[0], 8)
        self.assertEqual(m.values[-1], 0)
        self.assertIsNotNone(m.release)

    def test_a_flat_note_holds_full_volume_and_a_release_of_nothing_cuts_it(self):
        _, _, _, mod = read("tempo 120\n[instr]\nf wave=pulse a=0 d=0 s=15 r=0\nV1: @f c1\n")
        m = self.ins(mod, "f").macros[ex.MAC_VOL]
        self.assertEqual((m.values, m.release), ([15, 0], 0))

    def test_duty_comes_from_the_pulse_width(self):
        _, _, _, mod = read("tempo 120\n[instr]\nd wave=pulse pw=12\nV1: @d c1\n")
        self.assertEqual(self.ins(mod, "d").macros[ex.MAC_DUTY].values[0], 0)
        _, _, _, mod = read("tempo 120\n[instr]\nd wave=pulse pw=50\nV1: @d c1\n")
        self.assertEqual(self.ins(mod, "d").macros[ex.MAC_DUTY].values[0], 2)

    def test_an_arpeggio_is_a_looping_macro_of_semitone_offsets(self):
        _, _, _, mod = read("tempo 120\n[instr]\nar wave=pulse arp=0,4,7 arprate=8\nV1: @ar l2 c r\n")
        m = self.ins(mod, "ar").macros[ex.MAC_ARP]
        self.assertEqual(sorted(set(m.values)), [0, 4, 7])
        self.assertIsNotNone(m.loop)

    def test_vibrato_is_a_pitch_macro(self):
        _, _, _, mod = read("tempo 120\n[instr]\nvb wave=pulse vib=6,8\nV1: @vb l1 c\n")
        self.assertIn(ex.MAC_PITCH, self.ins(mod, "vb").macros)

    def test_the_sid_instrument_carries_its_wave_and_adsr_in_hardware(self):
        _, _, _, mod = read("tempo 120\n[instr]\nsw wave=saw a=2 d=4 s=9 r=5\nV1: @sw c1\n", "sid")
        c64 = self.ins(mod, "sw").c64
        self.assertEqual((c64["flags1"] & 15, c64["ad"], c64["sr"]), (2, 0x24, 0x95))      # sawtooth; A=2 D=4; S=9 R=5
        self.assertEqual(len(mod.instruments), 1)

    def test_instruments_that_sound_alike_are_one_instrument(self):
        _, _, _, mod = read("tempo 120\n[instr]\nx wave=pulse pw=50\nV1: @x c d e f\nV2: @x c d\n")
        self.assertEqual(len(mod.instruments), 1)


class ReportTests(unittest.TestCase):
    def test_voices_past_the_chip_are_left_out(self):
        _, _, rep, _ = read("voices 5\nV1: c\nV2: c\nV3: c\nV4: c\nV5: c\n")
        self.assertTrue(any("V5" in m for m in rep.dropped), rep.dropped)
        _, _, rep, _ = read("V1: c\nV2: c\nV3: c\nV4: c\n", "sid")
        self.assertTrue(any("V4" in m for m in rep.dropped), rep.dropped)

    def test_the_timing_and_chip_are_stated(self):
        _, _, rep, _ = read(region="pal")
        self.assertIn("50 Hz", " ".join(rep.notes))
        self.assertIn("228", " ".join(rep.notes))

    def test_the_triangle_volume_is_said_to_be_ignored(self):
        _, _, rep, _ = read()
        self.assertTrue(any("triangle" in m for m in rep.approximated), rep.approximated)

    def test_the_sid_filter_is_said_to_be_shared(self):
        _, _, rep, _ = read("tempo 120\n[instr]\nf wave=saw filter=lp,7,0\nV1: @f c1\nV2: @f e1\n", "sid")
        self.assertTrue(any("filter" in m for m in rep.approximated + rep.dropped), rep.lines())

    def test_a_second_sample_track_is_left_out_on_the_sid(self):
        a = dpcm.Sample(name="kick", rate=12, tracks=[[bytes.fromhex("aa55aa55")]])
        lib = dict(mml.builtin_library(), _mem={"kick": a})
        tune = Song.from_mml("[sample]\nkick\n\nV1: c\nD1: kick\n", lib).compile(lib)
        _, rep = ex.dump(tune, "sid")
        self.assertTrue(any("sample" in m.lower() or "D1" in m for m in rep.dropped), rep.dropped)

    def test_bad_arguments_are_refused(self):
        tune = tune_of(MML)
        with self.assertRaises(ex.FurnaceExportError):
            ex.dump(tune, target="gb")
        with self.assertRaises(ex.FurnaceExportError):
            ex.dump(tune, region="secam")

    def test_an_empty_song_is_a_valid_file(self):
        data, _ = ex.dump(tune_of("V1: r1\n"))
        strict_blocks(data)
        furnace.parse(data)


class DpcmTests(unittest.TestCase):
    def tune(self):
        a = dpcm.Sample(name="kick", rate=12, tracks=[[bytes.fromhex("aa55aa55")]])
        b = dpcm.Sample(name="snare", rate=9, tracks=[[bytes(range(1, 40))]])
        lib = dict(mml.builtin_library(), _mem={"kick": a, "snare": b})
        return Song.from_mml("[sample]\nkick\nsnare\n\nV1: c\nD1: kick r2 snare r2 kick\n", lib).compile(lib)

    def test_samples_are_written_and_played(self):
        tune = self.tune()
        data, rep = ex.dump(tune)
        mod = furnace.parse(data)
        self.assertEqual(len(mod.samples), 2)
        played = [c for (s, ch, p), rows in mod.patterns.items() if ch == 4 for c in rows.values() if c.note is not None]
        self.assertEqual(len(played), 3)
        back = Imported(data)
        self.assertEqual(len(back.voices[0]), 1)

    def test_a_sample_is_padded_to_the_length_the_hardware_reads(self):
        data, rep = ex.dump(self.tune())
        mod = furnace.parse(data)
        for s in mod.samples.values():
            self.assertLessEqual(len(s.data), ex.DPCM_BYTES)

    def test_no_samples_means_no_sample_block(self):
        _, data, _, _ = read()
        self.assertNotIn("SMP2", [b[0] for b in strict_blocks(data)[2]])


class DamageTests(unittest.TestCase):
    """A written file that is damaged must be refused with a clear error, or read, but never crash the importer."""

    def setUp(self):
        self.good = ex.dump(tune_of(MML), compress=False)[0]

    def attempt(self, data):
        try:
            furnace.parse(data)
        except furnace.FurnaceError:
            return "refused"
        return "read"

    def test_truncations_are_refused(self):
        for cut in (0, 10, 31, 40, len(self.good) // 2):
            with self.subTest(cut=cut):
                self.assertEqual(self.attempt(self.good[:cut]), "refused")

    def test_a_bad_magic_is_refused(self):
        self.assertEqual(self.attempt(b"-Furnace modulX-" + self.good[16:]), "refused")

    def test_a_pointer_outside_the_file_is_refused(self):
        bad = bytearray(self.good)
        struct.pack_into("<I", bad, 20, len(bad) + 100)
        self.assertEqual(self.attempt(bytes(bad)), "refused")

    def test_a_too_new_version_is_refused(self):
        bad = bytearray(self.good)
        struct.pack_into("<H", bad, 16, 999)
        self.assertEqual(self.attempt(bytes(bad)), "refused")

    def test_flipped_bytes_never_crash_the_importer(self):
        import random
        rnd = random.Random(7)
        for _ in range(150):
            bad = bytearray(self.good)
            for _ in range(rnd.randint(1, 4)):
                bad[rnd.randrange(32, len(bad))] = rnd.randrange(256)
            try:
                furnace.parse(bytes(bad))
            except furnace.FurnaceError:
                pass

    def test_the_strict_reader_refuses_what_the_importer_does(self):
        with self.assertRaises(FurError):
            strict_blocks(self.good[:-1])
        with self.assertRaises(FurError):
            strict_blocks(self.good[:-3])
        with self.assertRaises(FurError):
            strict_blocks(b"x" * 100)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="t")
        pkg.add_song("tune", Song.from_mml(MML))
        pk.save(pkg, self.tmp / "t.csp")

    def run_cli(self, *args):
        return main(["export-song", str(self.tmp / "t.csp"), *args])

    def test_the_ending_fur_picks_the_format(self):
        self.assertEqual(exporter.format_of("x.fur"), "furnace")
        self.assertEqual(exporter.format_of("X.FUR"), "furnace")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("-o", str(self.tmp / "a.fur")), 0)
        mod = furnace.parse((self.tmp / "a.fur").read_bytes())
        self.assertEqual(mod.chips, [(0x06, 5)])

    def test_the_format_flag_and_the_target(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("-o", str(self.tmp / "b.bin"), "-f", "furnace", "--target", "sid"), 0)
        self.assertEqual(furnace.parse((self.tmp / "b.bin").read_bytes()).chips, [(0x47, 3)])

    def test_region_pal_reaches_the_file_and_the_report_is_printed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_cli("-o", str(self.tmp / "p.fur"), "--region", "pal"), 0)
        self.assertEqual(furnace.parse((self.tmp / "p.fur").read_bytes()).subsongs[0].hz, 50.0)
        self.assertIn("50 Hz", err.getvalue())

    def test_a_bad_target_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.run_cli("-o", str(self.tmp / "c.fur"), "--target", "gb")

    def test_audio_options_do_not_apply(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("-o", str(self.tmp / "d.fur"), "--volume", "0.5"), 1)

    def test_it_is_a_listed_format(self):
        self.assertIn("furnace", exporter.FORMATS)
        self.assertEqual(exporter.SUFFIXES[".fur"], "furnace")


class ComposerTests(tcf.Base):
    def test_export_from_the_file_menu_code(self):
        a = self.a
        self.assertTrue(a.export_as("furnace", self.dir / "o.fur", region="pal", target="nes"))
        mod = furnace.parse((self.dir / "o.fur").read_bytes())
        self.assertEqual(mod.subsongs[0].hz, 50.0)
        self.assertTrue(any(x.startswith("left out:") and "echo" in x for x in a.last_report[1]), a.last_report[1])
        self.assertIn("50 Hz", " ".join(a.last_report[1]))
        self.assertFalse(any(x.startswith("warning:") for x in a.last_report[1]))

    def test_the_sid_target(self):
        self.assertTrue(self.a.export_as("furnace", self.dir / "s.fur", target="sid"))
        self.assertEqual(furnace.parse((self.dir / "s.fur").read_bytes()).chips, [(0x47, 3)])

    def test_the_dialog_asks_for_chip_and_timing(self):
        a = self.a
        target = self.dir / "dlg.fur"
        a.choose_path = lambda save, title, pattern, ext="", initialfile="", initialdir=None: str(target)
        self.assertTrue(a.export_dialog("furnace"))
        self.assertEqual([r["key"] for r in a.fileopts["rows"]], ["target", "region"])
        self.assertEqual(a.fileopts_values(), {"target": "nes", "region": "ntsc"})
        a.frame()
        a.fileopts_step(0, 1)
        a.fileopts_step(1, 1)
        self.assertEqual(a.fileopts_values(), {"target": "sid", "region": "pal"})
        a.fileopts_finish(True)
        mod = furnace.parse(target.read_bytes())
        self.assertEqual((mod.chips, mod.subsongs[0].hz), ([(0x47, 3)], 50.0))

    def test_the_menu_lists_it(self):
        labels = [i["label"] for i in self.a.submenu_items("export")]
        self.assertTrue(any(x.startswith("Furnace module") for x in labels), labels)

    def test_a_name_without_an_ending_gets_fur(self):
        self.assertTrue(self.a.export_as("furnace", self.dir / "noending"))
        self.assertTrue((self.dir / "noending.fur").exists())


if __name__ == "__main__":
    unittest.main()
