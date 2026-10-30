# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""GoatTracker .sng import: the files are built here from the format, byte by byte; no real songs are used."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from chipsynth import __main__ as cli, package as pk
from chipsynth.formats import goattracker as gt

C4, E4, G4 = 0x60 + 48, 0x60 + 52, 0x60 + 55


def sng(orders, patterns, instruments=(), tables=None, subtunes=None, title="Tiny", ident=b"GTS5"):
    """orders: three lists of order bytes (without the restart byte's length: the last item is the restart position)."""
    out = bytearray(ident)
    for s in (title, "me", "(c) me"):
        out += s.encode().ljust(32, b"\0")
    subs = subtunes or [orders]
    out.append(len(subs))
    for chans in subs:
        for o in chans:
            out.append(len(o) - 1)
            out += bytes(o)
    out.append(len(instruments))
    for ad, sr, wp, pp, fp, sp, vd, gate, fw, name in instruments:
        out += bytes((ad, sr, wp, pp, fp, sp, vd, gate, fw)) + name.encode().ljust(16, b"\0")
    for left, right in (tables or [([], [])] * 4):
        out.append(len(left))
        out += bytes(left) + bytes(right)
    out.append(len(patterns))
    for rows in patterns:
        out.append(len(rows))
        for row in rows:
            out += bytes(row)
    return bytes(out)


def pat(*rows):
    return [tuple(r) for r in rows] + [(gt.ENDPATT, 0, 0, 0)]


def rows(*notes, inst=1):
    return [(n, inst if i == 0 else 0, 0, 0) for i, n in enumerate(notes)]


def simple():
    p = [(C4, 1, 0, 0), (gt.REST, 0, 0, 0), (E4, 0, 0, 0), (gt.KEYOFF, 0, 0, 0)]
    ins = [(0x09, 0xA4, 1, 0, 0, 0, 0, 2, 0x41, "lead")]
    tbl = [([0x41], [0x80]), ([], []), ([], []), ([], [])]
    return sng([[0, gt.LOOPSONG, 0], [gt.LOOPSONG, 0], [gt.LOOPSONG, 0]], [pat(*p)], ins, tbl)


class ParseTests(unittest.TestCase):
    def test_header_and_counts(self):
        t = gt.parse(simple())
        self.assertEqual((t.title, t.author, t.copyright, t.version), ("Tiny", "me", "(c) me", "GTS5"))
        self.assertEqual(len(t.patterns), 1)
        self.assertEqual(t.instruments[0].name, "lead")
        self.assertEqual(t.instruments[0].ad, 0x09)

    def test_older_idents_are_read(self):
        self.assertTrue(gt.looks_like(b"GTS3rest"))
        self.assertFalse(gt.looks_like(b"GTS!"))

    def test_garbage_and_truncation_are_errors_not_crashes(self):
        with self.assertRaises(gt.GoatTrackerError):
            gt.parse(b"not a song")
        data = simple()
        for cut in (5, 40, 110, len(data) - 3):
            with self.assertRaises(gt.GoatTrackerError):
                gt.parse(data[:cut])


class PlayTests(unittest.TestCase):
    def load(self, data, **kw):
        r = gt.load(data, **kw)
        return r, r.songs[0].song

    def test_notes_rests_and_key_off(self):
        r, song = self.load(simple())
        # speed 6 = 24 ticks a row: C lasts through the rest (48 ticks), E until key off (24 ticks)
        ev = song.tracks["V1"]
        notes = [e for e in ev if e.__class__.__name__ == "Note"]
        self.assertEqual([(n.letter, n.dur) for n in notes], [("c", 48), ("e", 24)])

    def test_bpm_is_set_from_the_region(self):
        self.assertEqual(self.load(simple())[1].bpm, 125)
        self.assertEqual(self.load(simple(), region="ntsc")[1].bpm, 150)

    def test_instrument_adsr_wave_and_pulse(self):
        r, song = self.load(simple())
        inst = list(song.instruments.values())[0]
        self.assertEqual((inst.a, inst.d, inst.s, inst.r), (0, 9, 10, 4))
        self.assertEqual(inst.wave, "pulse")
        self.assertEqual(inst.pw, 50)

    def test_tempo_command_changes_row_length(self):
        p = [(C4, 1, gt.CMD_SETTEMPO, 3), (E4, 0, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        data = sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], [(0x09, 0xA4, 0, 0, 0, 0, 0, 2, 0x10, "t")])
        r, song = self.load(data)
        notes = [e for e in song.tracks["V1"] if e.__class__.__name__ == "Note"]
        self.assertEqual([n.dur for n in notes], [12, 12])      # 3 frames x 4 ticks

    def test_orderlist_repeat_and_transpose(self):
        p = [(C4, 1, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        order = [gt.TRANSUP + 2, gt.REPEAT + 1, 0, gt.LOOPSONG, 0]
        data = sng([order, [gt.LOOPSONG, 0], [gt.LOOPSONG, 0]], [pat(*p)], [(0, 0xF0, 0, 0, 0, 0, 0, 2, 0x10, "s")])
        r, song = self.load(data)
        notes = [e for e in song.tracks["V1"] if e.__class__.__name__ == "Note"]
        self.assertEqual(len(notes), 2)                          # repeat 1 plays the pattern twice
        self.assertTrue(all(n.letter == "d" for n in notes))     # transposed up two semitones

    def test_loop_point_is_marked_on_voice_one(self):
        p = [(C4, 1, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        data = sng([[0, 0, gt.LOOPSONG, 1], [0, 0, gt.LOOPSONG, 1], [0, 0, gt.LOOPSONG, 1]], [pat(*p)],
                   [(0, 0xF0, 0, 0, 0, 0, 0, 2, 0x10, "s")])
        r, song = self.load(data)
        self.assertEqual(sum(e.__class__.__name__ == "Loop" for e in song.tracks["V1"]), 1)

    def test_unsupported_things_are_reported(self):
        p = [(C4, 1, 1, 0x20), (gt.KEYOFF, 0, 0, 0)]
        ins = [(0x09, 0xA4, 1, 0, 1, 1, 0, 2, 0x10, "x")]
        tbl = [([0x11, 0x13, 0xFF], [0x80, 0x00, 0x01]), ([], []), ([0x80], [0x10]), ([0x01], [0x02])]
        r, song = self.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], ins, tbl))
        text = "\n".join(r.report.lines())
        self.assertIn("wavetable program", text)
        self.assertIn("filter table", text)
        self.assertIn("speed table", text)
        self.assertIn("portamento up", text)

    def test_several_subtunes_become_several_songs(self):
        p = [(C4, 1, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        one = [[0, gt.LOOPSONG, 0]] * 3
        r = gt.load(sng(one, [pat(*p)], [(0, 0xF0, 0, 0, 0, 0, 0, 2, 0x10, "s")], subtunes=[one, one]))
        self.assertEqual(len(r.songs), 2)
        self.assertEqual(len(gt.load(sng(one, [pat(*p)], [(0, 0xF0, 0, 0, 0, 0, 0, 2, 0x10, "s")], subtunes=[one, one]), subtune=2).songs), 1)

    def test_bad_multispeed_is_an_error(self):
        with self.assertRaises(gt.GoatTrackerError):
            gt.load(simple(), multispeed=3)

    def test_bpm_follows_the_video_rate_not_the_multispeed(self):
        # a multispeed frame is shorter, but it is also fewer ticks: a row of the same frames lasts the same time
        for mult in (1, 2, 4):
            self.assertEqual(self.load(simple(), multispeed=mult)[1].bpm, 125)
            self.assertEqual(self.load(simple(), multispeed=mult, region="ntsc")[1].bpm, 150)


E = ([], [])


def inst(wp=0, pp=0, fp=0, sp=0, vd=0, fw=0x41, name="x"):
    return [(0x09, 0xA4, wp, pp, fp, sp, vd, 2, fw, name)]


def one_note(ins, tables, cmd=0, data=0, note=C4):
    """A tune of one note on voice 1 (command `cmd`, `data` on its row), with the tables (wave, pulse, filter, speed)."""
    p = [(note, 1, cmd, data), (gt.KEYOFF, 0, 0, 0)]
    return sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], ins, tables)


def load(ins, tables, cmd=0, data=0, note=C4, **kw):
    r = gt.load(one_note(ins, tables, cmd, data, note), **kw)
    return r, r.songs[0].song


def only(song):
    return list(song.instruments.values())[0]


def text(r):
    return "\n".join(r.report.lines())


class WavetableTests(unittest.TestCase):
    def test_a_looping_relative_wavetable_is_an_arpeggio(self):
        t = [([0x41, 0x41, 0x41, 0xFF], [0, 4, 7, 1]), E, E, E]
        r, song = load(inst(wp=1), t)
        i = only(song)
        self.assertEqual((i.arp, i.arprate), ((0, 4, 7), 24))       # a step a frame, 24 frames a beat
        self.assertNotIn("wavetable program", text(r))
        self.assertIn("arpeggio of 0,4,7", text(r))

    def test_delay_rows_set_the_step_length(self):
        # each row holds two frames: the pitch changes at the end of the delay, so the cycle is 6 frames
        t = [([0x01, 0x01, 0x01, 0xFF], [0, 4, 7, 1]), E, E, E]
        r, song = load(inst(wp=1, fw=0x41), t)
        self.assertEqual((only(song).arp, only(song).arprate), ((0, 4, 7), 12))

    def test_multispeed_and_a_jump_back_to_a_later_row(self):
        t = [([0x41, 0x41, 0xFF], [0, 12, 1]), E, E, E]
        r, song = load(inst(wp=1), t, multispeed=2)
        i = only(song)
        self.assertEqual(i.arp, (0, 12))
        self.assertEqual(i.arprate, 32)                             # 48 frames a beat is more than chipsynth takes
        self.assertIn("approximated", text(r))

    def test_no_change_rows_hold_the_pitch(self):
        t = [([0x41, 0x41, 0x41, 0xFF], [0, 0x80, 7, 1]), E, E, E]
        r, song = load(inst(wp=1), t)
        self.assertEqual((only(song).arp, only(song).arprate), ((0, 0, 7), 24))     # the held step is a frame of 0 again

    def test_absolute_notes_are_not_relative_and_are_reported(self):
        t = [([0x41, 0x41, 0xFF], [0x90, 4, 1]), E, E, E]
        r, song = load(inst(wp=1), t)
        self.assertEqual(only(song).arp, ())
        self.assertIn("absolute notes", text(r))
        self.assertIn("left out: instrument 'x': its wavetable program", text(r))

    def test_a_wavetable_that_plays_once_is_reported(self):
        t = [([0x81, 0x41, 0x41, 0xFF], [0, 4, 7, 2]), E, E, E]
        r, song = load(inst(wp=1, fw=0x81), t)
        self.assertEqual(only(song).arp, ())
        self.assertIn("plays some rows once before it loops", text(r))
        t = [([0x41, 0x41, 0xFF], [0, 7, 0]), E, E, E]             # ends: a chord that stays on its last note
        self.assertIn("does not repeat", text(load(inst(wp=1), t)[0]))

    def test_commands_in_the_wavetable_are_reported(self):
        t = [([0x41, 0xF4, 0xFF], [0, 1, 1]), E, E, E]
        self.assertIn("runs commands", text(load(inst(wp=1), t)[0]))

    def test_waveform_changes_are_reported_and_an_arpeggio_keeps_its_notes(self):
        t = [([0x41, 0x81, 0xFF], [0, 0, 1]), E, E, E]             # a hit then a tone: no pitch, two waves
        self.assertIn("changes waveform", text(load(inst(wp=1), t)[0]))
        t = [([0x41, 0x81, 0x41, 0xFF], [0, 4, 7, 1]), E, E, E]
        r, song = load(inst(wp=1), t)
        self.assertEqual(only(song).arp, (0, 4, 7))
        self.assertIn("waveform changes are not carried", text(r))

    def test_a_plain_one_row_table_is_not_a_complaint(self):
        t = [([0x41], [0]), E, E, E]
        self.assertNotIn("wavetable program", text(load(inst(wp=1), t)[0]))


class PulseTableTests(unittest.TestCase):
    # set 50%, then 25 frames up by 0x28 (to 74%), 25 frames down, repeat from the up row
    TRI = [E, ([0x88, 25, 25, 0xFF], [0x00, 0x28, 0xD8, 2]), E, E]

    def test_a_ramp_up_and_down_is_a_pulse_sweep(self):
        r, song = load(inst(pp=1), self.TRI)
        i = only(song)
        self.assertEqual((i.pw, i.pws_from, i.pws_to, i.pws_rate), (50, 50, 74, 2))   # 50 frames a cycle = 1 Hz
        self.assertNotIn("pulse sweep (", text(r))
        self.assertNotIn("approximated", text(r))

    def test_a_sweep_that_starts_high_starts_there(self):
        t = [E, ([0x8C, 25, 25, 0xFF], [0x00, 0xD8, 0x28, 2]), E, E]
        i = only(load(inst(pp=1), t)[1])
        self.assertEqual((i.pws_from, i.pws_to), (75, 51))

    def test_a_ramp_that_jumps_back_is_played_as_a_sweep(self):
        t = [E, ([0x88, 0x20, 0xFF], [0x00, 0x10, 1]), E, E]
        r, song = load(inst(pp=1), t)
        i = only(song)
        self.assertEqual((i.pws_from, i.pws_to), (50, 62))
        self.assertIn("jumps back", text(r))

    def test_a_one_way_ramp_is_an_approximate_sweep(self):
        t = [E, ([0x88, 0x20], [0x00, 0x10]), E, E]
        r, song = load(inst(pp=1), t)
        i = only(song)
        self.assertEqual((i.pws_from, i.pws_to), (50, 62))
        self.assertIn("one-way ramp", text(r))

    def test_a_constant_width_is_just_the_width(self):
        t = [E, ([0x84, 0xFF], [0x00, 1]), E, E]
        r, song = load(inst(pp=1), t)
        self.assertEqual((only(song).pw, only(song).pws_rate), (25, 0))
        self.assertNotIn("pulse sweep", text(r))

    def test_a_zigzag_is_reported_not_guessed(self):
        t = [E, ([0x88, 4, 4, 4, 4, 0xFF], [0x00, 0x10, 0xF0, 0x10, 0xF0, 2]), E, E]
        r, song = load(inst(pp=1), t)
        self.assertEqual(only(song).pws_rate, 0)
        self.assertIn("pulse sweep (it is not a single rise and fall)", text(r))

    def test_a_sweep_on_a_wave_without_pulse_is_ignored_with_a_note(self):
        r, song = load(inst(pp=1, fw=0x21), self.TRI)
        self.assertEqual((only(song).wave, only(song).pws_rate), ("saw", 0))
        self.assertIn("pulse sweep is ignored", text(r))


class VibratoTests(unittest.TestCase):
    # left 8: the swing turns every 5 frames (a 20 frame cycle); right 0x20: 32 register units a frame
    SPEED = [E, E, E, ([8], [0x20])]

    def test_pattern_command_4_makes_a_vibrato_instrument(self):
        r, song = load(inst(), self.SPEED, cmd=4, data=1)
        self.assertEqual(list(song.instruments), ["x_vib3_10"])      # 2.5 Hz (rate 3); 5 steps of 32 units on C4: 0.6 semitone
        i = only(song)
        self.assertEqual((i.vib_rate, i.vib_depth), (3, 10))
        self.assertEqual(sum(e.__class__.__name__ == "Select" for e in song.tracks["V1"]), 1)
        self.assertIn("approximated: vibrato is a triangle", text(r))
        self.assertNotIn("left out", text(r))

    def test_the_instruments_own_speed_entry_is_vibrato_after_its_delay(self):
        r, song = load(inst(sp=1, vd=1), self.SPEED)
        self.assertEqual((only(song).vib_rate, only(song).vib_depth), (3, 10))
        r, song = load(inst(sp=1, vd=4), self.SPEED)
        self.assertEqual(only(song).vib_rate, 3)
        self.assertIn("vibrato delay 4 is not kept", text(r))

    def test_without_a_delay_the_instruments_entry_does_nothing(self):
        r, song = load(inst(sp=1, vd=0), self.SPEED)
        self.assertEqual((only(song).vib_rate, list(song.instruments)), (0, ["x"]))
        self.assertIn("speed table pointer does nothing", text(r))

    def test_command_4_with_data_0_switches_the_instruments_vibrato_off(self):
        r, song = load(inst(sp=1, vd=1), self.SPEED, cmd=4, data=0)
        self.assertEqual(list(song.instruments), ["x"])

    def test_the_same_entry_is_deeper_on_a_low_note(self):
        # a fixed register step is a bigger part of a low note's frequency
        low = only(load(inst(), self.SPEED, 4, 1, note=0x60 + 24)[1]).vib_depth
        high = only(load(inst(), self.SPEED, 4, 1, note=0x60 + 60)[1]).vib_depth
        self.assertGreater(low, high)

    def test_a_relative_entry_scales_with_the_note(self):
        t = [E, E, E, ([0x88], [4])]                              # a sixteenth of a semitone a frame, 5 frames out
        lo = only(load(inst(), t, 4, 1, note=0x60 + 24)[1])
        hi = only(load(inst(), t, 4, 1, note=0x60 + 60)[1])
        self.assertEqual((lo.vib_depth, hi.vib_depth), (5, 5))

    def test_multispeed_doubles_the_rate(self):
        r, song = load(inst(), self.SPEED, 4, 1, multispeed=2)
        self.assertEqual(only(song).vib_rate, 6)

    def test_what_cannot_be_vibrato_is_reported(self):
        r, song = load(inst(), self.SPEED, cmd=4, data=9)
        self.assertIn("speed table entry 9 does not exist", text(r))
        r, song = load(inst(), [E, E, E, ([8], [0])], cmd=4, data=1)
        self.assertIn("has speed 0", text(r))
        r, song = load(inst(), [E, E, E, ([8], [1])], cmd=4, data=1)
        self.assertIn("too shallow", text(r))
        self.assertEqual(list(song.instruments), ["x"])

    def test_a_vibrato_command_part_way_through_a_note_is_reported(self):
        p = [(C4, 1, 0, 0), (gt.REST, 0, 4, 1), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], inst(), self.SPEED))
        self.assertIn("without a new note", text(r))


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main([str(a) for a in argv])
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pkg = self.tmp / "p.csp"

    def write(self, name, data):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def two_subtunes(self):
        one = [[0, gt.LOOPSONG, 0]] * 3
        p = [(C4, 1, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        return sng(one, [pat(*p)], inst(), subtunes=[one, one])

    def test_import_a_sng_names_the_song_after_the_file(self):
        f = self.write("my tune.sng", simple())
        rc, out, err = run("import", self.pkg, f)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("added song 'my_tune'", out)
        song = pk.load(self.pkg).songs["my_tune"]
        self.assertEqual(song.bpm, 125)       # pal by default
        self.assertIn("note: PAL timing", out)

    def test_region_and_multispeed(self):
        f = self.write("t.sng", simple())
        rc, out, err = run("import", self.pkg, f, "--region", "ntsc", "--multispeed", "2")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("NTSC timing (60 Hz)", out)
        self.assertIn("multispeed x2", out)
        self.assertEqual(run("import", self.pkg, f, "--multispeed", "3")[0], 2)

    def test_subtunes_are_songs_and_song_picks_one(self):
        f = self.write("multi.sng", self.two_subtunes())
        rc, out, err = run("import", self.pkg, f)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(sorted(pk.load(self.pkg).songs), ["multi_1", "multi_2"])
        pkg2 = self.tmp / "q.csp"
        rc, out, err = run("import", pkg2, f, "--song", "2")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(sorted(pk.load(pkg2).songs), ["multi"])
        self.assertEqual(run("import", pkg2, f, "--song", "3")[0], 1)         # there is no subtune 3
        self.assertEqual(run("import", pkg2, f, "--song", "lead")[0], 1)      # it is a number

    def test_id_no_replace_and_the_report_file(self):
        f = self.write("t.sng", simple())
        report = self.tmp / "r.json"
        rc, out, err = run("import", self.pkg, f, "--id", "town", "--report", report)
        self.assertEqual((rc, err), (0, ""), out)
        data = json.loads(report.read_text())
        self.assertEqual(sorted(data), ["approximated", "dropped", "notes"])
        rc, out, err = run("import", self.pkg, f, "--id", "town", "--no-replace")
        self.assertEqual(rc, 1)
        self.assertIn("already has a song 'town'", err)
        f2 = self.write("m.sng", self.two_subtunes())
        self.assertEqual(run("import", self.pkg, f2, "--id", "x")[0], 1)       # two songs, one id

    def test_a_song_is_found_by_its_start_when_the_name_has_no_sng(self):
        f = self.write("tune.dat", simple())
        rc, out, err = run("import", self.pkg, f)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("tune", pk.load(self.pkg).songs)

    def test_a_broken_sng_is_an_error_not_a_crash(self):
        rc, out, err = run("import", self.pkg, self.write("bad.sng", b"not a song at all"))
        self.assertEqual(rc, 1)
        self.assertIn("not a GoatTracker song", err)
        rc, out, err = run("import", self.pkg, self.write("cut.sng", simple()[:60]))
        self.assertEqual(rc, 1)
        self.assertIn("truncated", err)

    def test_tables_carry_through_to_the_package(self):
        t = [([0x41, 0x41, 0x41, 0xFF], [0, 4, 7, 1]), E, E, E]
        f = self.write("arp.sng", one_note(inst(wp=1), t))
        rc, out, err = run("import", self.pkg, f)
        self.assertEqual((rc, err), (0, ""), out)
        song = pk.load(self.pkg).songs["arp"]
        self.assertEqual(only(song).arp, (0, 4, 7))


if __name__ == "__main__":
    unittest.main()
