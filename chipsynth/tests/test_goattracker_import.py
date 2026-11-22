# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""GoatTracker .sng import: the files are built here from the format, byte by byte; no real songs are used."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from chipsynth import TunePlayer, __main__ as cli, package as pk, song as sg
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

    def test_a_note_with_no_key_off_lasts_to_the_end_of_the_song_and_not_a_frame_more(self):
        p = [(C4, 1, 0, 0), (gt.REST, 0, 0, 0), (gt.REST, 0, 0, 0)]
        data = sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], [(0x09, 0xA4, 1, 0, 0, 0, 0, 2, 0x41, "t")], [([0x41], [0x80]), ([], []), ([], []), ([], [])])
        notes = [e for e in self.load(data)[1].tracks["V1"] if e.__class__.__name__ == "Note"]
        self.assertEqual([n.dur for n in notes], [72])            # three rows of 24 ticks

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

    def test_what_is_not_carried_is_reported(self):
        p = [(C4, 1, 1, 0x20), (gt.KEYOFF, 0, 0, 0)]
        ins = [(0x09, 0xA4, 1, 0, 0, 1, 0, 2, 0x10, "x")]
        tbl = [([0x11, 0xF4, 0xFF], [0x80, 0x00, 0x01]), ([], []), ([], []), ([0x01], [0x02])]
        r, song = self.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], ins, tbl))
        text = "\n".join(r.report.lines())
        self.assertIn("wavetable program", text)          # a wavetable that runs commands
        self.assertIn("runs commands", text)
        self.assertIn("speed table", text)                # the portamento names a speed entry that is not there
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


SEMI = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def played(song, track="V1"):
    """The notes of a track as (pitch, ticks, instrument name, slide) in order; pitch is octave * 12 + semitone."""
    out, octave, name, slide = [], 4, None, 0
    for e in song.tracks[track]:
        kind = e.__class__.__name__
        if kind == "Octave":
            octave = e.set
        elif kind == "Select":
            name = e.name
        elif kind == "Tweak" and e.word == "slide":
            slide = int(e.arg)
        elif kind == "Note":
            out.append((octave * 12 + SEMI[e.letter] + e.acc, e.dur, name, slide))
            slide = 0
    return out


def only_variant(song):
    """The instrument made last (a variant of the first one)."""
    return list(song.instruments.values())[-1]


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

    def test_absolute_notes_are_played_as_what_they_are(self):
        # row 1 is C-1 (absolute 0x8C), row 2 four above the played C-4: they alternate, 40 semitones apart
        t = [([0x41, 0x41, 0xFF], [0x8C, 4, 1]), E, E, E]
        r, song = load(inst(wp=1), t)
        self.assertIn("more than two octaves", text(r))
        self.assertNotIn("wavetable program", text(r))
        self.assertEqual(played(song)[0][0], 12)                       # the note is the first step of the cycle
        self.assertEqual(only_variant(song).arp, (0, 24))              # limited to two octaves

    def test_rows_that_play_once_before_the_loop_are_notes_before_the_arpeggio(self):
        # a noise hit on the played note, then a pulse that alternates 4 and 7 above it
        t = [([0x81, 0x41, 0x41, 0xFF], [0, 4, 7, 2]), E, E, E]
        r, song = load(inst(wp=1, fw=0x81), t)
        notes = played(song)
        self.assertEqual([(n[0], n[1], n[2]) for n in notes], [(48, 4, "x"), (52, 20, "x_pulse_arp0_3r24")])
        self.assertEqual(song.instruments["x"].wave, "noise")
        self.assertEqual((song.instruments["x_pulse_arp0_3r24"].wave, song.instruments["x_pulse_arp0_3r24"].arp), ("pulse", (0, 3)))
        self.assertNotIn("wavetable program", text(r))
        self.assertIn("struck again", text(r))

    def test_a_wavetable_that_ends_leaves_the_last_note_playing(self):
        t = [([0x41, 0x41, 0xFF], [0, 7, 0]), E, E, E]              # a jump to 0 stops it: C, then G for the rest of the note
        r, song = load(inst(wp=1), t)
        self.assertEqual([(n[0], n[1]) for n in played(song)], [(48, 4), (55, 20)])
        self.assertNotIn("does not repeat", text(r))

    def test_a_noise_hit_that_becomes_a_tone_without_an_arpeggio(self):
        # an absolute noise note for one frame, then the pulse on the played note for the rest
        t = [([0x81, 0x41, 0xFF], [0x9C, 0, 2]), E, E, E]
        r, song = load(inst(wp=1, fw=0x81), t)
        notes = played(song)
        self.assertEqual([(n[0], n[1], n[2]) for n in notes], [(28, 4, "x"), (48, 20, "x_pulse")])
        self.assertEqual((song.instruments["x"].wave, song.instruments["x_pulse"].wave), ("noise", "pulse"))

    def test_delay_rows_set_how_long_each_part_lasts(self):
        # a delay of 2 is three frames of the state before it (it takes effect at the end), so the noise lasts 1 + 3 frames
        t = [([0x81, 0x02, 0x41, 0xFF], [0x9C, 0x80, 0, 3]), E, E, E]
        r, song = load(inst(wp=1, fw=0x81), t)
        self.assertEqual([(n[1], n[2]) for n in played(song)], [(16, "x"), (8, "x_pulse")])

    def test_a_silent_waveform_ends_the_note(self):
        t = [([0x41, 0x02, 0xE1, 0xFF], [0, 0x80, 0, 0]), E, E, E]
        r, song = load(inst(wp=1), t)
        self.assertEqual([n[1] for n in played(song)], [16])           # the tone, then the delay row, then silence
        self.assertIn("note ends there", text(r))

    def test_commands_in_a_wavetable_still_stop_the_import_of_it(self):
        t = [([0x41, 0xF4, 0xFF], [0, 1, 1]), E, E, E]
        r, song = load(inst(wp=1), t)
        self.assertIn("runs commands", text(r))

    def test_commands_in_the_wavetable_are_reported(self):
        t = [([0x41, 0xF4, 0xFF], [0, 1, 1]), E, E, E]
        self.assertIn("runs commands", text(load(inst(wp=1), t)[0]))

    def test_waveforms_that_alternate_for_ever_play_their_first_step(self):
        t = [([0x41, 0x81, 0xFF], [0, 0, 1]), E, E, E]             # a tone and a hit, again and again
        r, song = load(inst(wp=1), t)
        self.assertIn("changes waveform", text(r))
        self.assertEqual(only(song).wave, "pulse")

    def test_an_arpeggio_keeps_its_notes_when_the_waveform_changes(self):
        t = [([0x41, 0x81, 0x41, 0xFF], [0, 4, 7, 1]), E, E, E]
        r, song = load(inst(wp=1), t)
        self.assertEqual(only(song).arp, (0, 4, 7))
        self.assertIn("waveform changes are not carried", text(r))

    def test_a_wavetable_that_only_changes_waveform_once(self):
        # no pitch moves at all, so no arpeggio: noise first, then the tone, both on the played note
        t = [([0x81, 0x41, 0xFF], [0, 0, 2]), E, E, E]
        r, song = load(inst(wp=1, fw=0x81), t)
        self.assertEqual([(n[0], n[2]) for n in played(song)], [(48, "x"), (48, "x_pulse")])

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

    def test_a_zigzag_that_repeats_one_sweep_is_that_sweep(self):
        # two identical rises and falls in the table are one sweep, exactly: 8 frames a cycle
        t = [E, ([0x88, 4, 4, 4, 4, 0xFF], [0x00, 0x10, 0xF0, 0x10, 0xF0, 2]), E, E]
        r, song = load(inst(pp=1), t)
        i = only(song)
        self.assertEqual((i.pws_from, i.pws_to, i.pws_rate), (50, 52, 12))
        self.assertNotIn("approximated", text(r))
        self.assertNotIn("pulse sweep (", text(r))

    def test_an_uneven_zigzag_is_an_approximate_sweep(self):
        # a small rise and fall then a bigger one: no single sweep, so its range is swept at its average speed
        t = [E, ([0x88, 4, 8, 4, 8, 0xFF], [0x00, 0x10, 0xF8, 0x18, 0xF4, 2]), E, E]
        r, song = load(inst(pp=1), t)
        i = only(song)
        self.assertEqual((i.pws_from, i.pws_to), (50, 52))
        self.assertGreater(i.pws_rate, 0)
        self.assertIn("pulse zigzags 2 times", text(r))

    def test_a_wild_zigzag_is_not_a_sweep(self):
        left = [0x88] + [k for k in range(1, 10) for _ in (0, 1)] + [0xFF]          # rises and falls of 1, 2 ... 9 frames
        right = [0x00] + [v for _ in range(1, 10) for v in (0x10, 0xF0)] + [2]
        r, song = load(inst(pp=1), [E, (left, right), E, E])
        self.assertEqual(only(song).pws_rate, 0)
        self.assertIn("zigzags 9 times", text(r))

    def test_a_ramp_before_any_width_is_set_starts_from_half(self):
        t = [E, ([4, 4, 0xFF], [0x10, 0xF0, 1]), E, E]
        r, song = load(inst(pp=1), t)
        self.assertEqual((only(song).pws_from, only(song).pws_to), (50, 52))
        self.assertIn("before it sets one", text(r))
        self.assertNotIn("pulse sweep (", text(r))

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

    def test_a_tempo_command_on_the_note_row_does_not_stop_the_instruments_vibrato(self):
        r, song = load(inst(sp=1, vd=1), self.SPEED, cmd=gt.CMD_SETTEMPO, data=6)
        self.assertEqual((only(song).vib_rate, only(song).vib_depth), (3, 10))

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

    def test_a_vibrato_command_part_way_through_a_note_splits_the_note(self):
        p = [(C4, 1, 0, 0), (gt.REST, 0, 4, 1), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], inst(), self.SPEED))
        self.assertEqual([(n[1], n[2]) for n in played(r.songs[0].song)], [(24, "x"), (24, "x_vib3_10")])
        self.assertIn("part-way through", text(r))
        self.assertNotIn("left out", text(r))

    def test_a_vibrato_command_on_a_key_off_row_is_reported(self):
        p = [(C4, 1, 0, 0), (gt.KEYOFF, 0, 4, 1)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], inst(), self.SPEED))
        self.assertIn("without a new note", text(r))


def held(ins, tables, cmd=0, data=0, note=C4, rows=8, extra=None, **kw):
    """One note on voice 1 held for `rows` rows (6 frames each) and then released.  `extra` puts a command on a later row:
    {row: (command, data)}."""
    p = [(note, 1, cmd, data)] + [(gt.REST, 0, 0, 0)] * (rows - 1) + [(gt.KEYOFF, 0, 0, 0)]
    for i, (c, d) in (extra or {}).items():
        p[i] = (p[i][0], p[i][1], c, d)
    r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], ins, tables), **kw)
    return r, r.songs[0].song


def two(ins, tables, cmd, data, **kw):
    """C4 then (a row later) G4 carrying the command, then a key off.  Rows are 6 frames, so 24 ticks."""
    p = [(C4, 1, 0, 0), (gt.REST, 0, 0, 0), (G4, 0, cmd, data), (gt.REST, 0, 0, 0), (gt.KEYOFF, 0, 0, 0)]
    r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], ins, tables), **kw)
    return r, r.songs[0].song


def assert_valid(case, song):
    """The song compiles, and it still does after a trip through MML (names, slides and volumes are all legal there)."""
    song.compile()
    sg.Song.from_mml(song.to_mml()).compile()


SPEEDS = [E, E, E, ([0x00, 0x00], [0x40, 0x04])]          # entry 1: 64 SID units a frame, entry 2: 4


class PortamentoTests(unittest.TestCase):
    def test_portamento_up_glides_to_the_pitch_it_reaches(self):
        # 64 units a frame for 47 frames from C4 (4455) is 9.1 semitones: A4, and the note says how long the glide takes
        r, song = held(inst(), SPEEDS, cmd=1, data=1)
        self.assertEqual(played(song), [(48, 4, "x", 0), (57, 188, "x", 94)])
        self.assertIn("nearest semitone", text(r))
        self.assertNotIn("left out", text(r))
        assert_valid(self, song)

    def test_portamento_down(self):
        r, song = held(inst(), SPEEDS, cmd=2, data=1)
        self.assertEqual(played(song), [(48, 4, "x", 0), (28, 188, "x", 94)])
        assert_valid(self, song)

    def test_a_slow_slide_is_a_semitone(self):
        r, song = held(inst(), SPEEDS, cmd=1, data=2)
        self.assertEqual([n[0] for n in played(song)], [48, 49])

    def test_multispeed_makes_the_slide_faster(self):
        # the same units a frame, twice the frames a second
        r, song = held(inst(), SPEEDS, cmd=1, data=1, multispeed=2)
        self.assertEqual(played(song), [(48, 2, "x", 0), (63, 190, "x", 95)])

    def test_a_long_slide_is_cut_into_notes_chipsynth_can_glide(self):
        r, song = held(inst(), SPEEDS, cmd=1, data=1, rows=80)
        notes = played(song)
        self.assertEqual(sum(n[1] for n in notes), 80 * 6 * 4)
        self.assertTrue(all(n[3] <= 192 for n in notes))
        self.assertGreater(len(notes), 3)
        r, song = held(inst(), SPEEDS, cmd=1, data=1, rows=40, multispeed=4)
        notes = played(song)
        self.assertEqual(sum(n[1] for n in notes), 40 * 24)
        self.assertTrue(all(n[3] <= 192 for n in notes))

    def test_a_slide_that_is_stopped_leaves_the_pitch_where_it_got_to(self):
        r, song = held(inst(), SPEEDS, cmd=1, data=1, extra={3: (1, 0)})
        notes = played(song)
        self.assertEqual([(n[0], n[1], n[3]) for n in notes], [(48, 4, 0), (52, 68, 34), (52, 120, 0)])

    def test_a_new_speed_part_way_changes_the_slide(self):
        r, song = held(inst(), SPEEDS, cmd=1, data=1, extra={4: (1, 2)})
        notes = played(song)
        self.assertEqual([n[0] for n in notes], [48, 53, 53])
        self.assertEqual(sum(n[1] for n in notes), 192)

    def test_a_missing_speed_entry_is_reported_and_the_note_plays_plain(self):
        r, song = held(inst(), SPEEDS, cmd=1, data=9)
        self.assertEqual(played(song), [(48, 192, "x", 0)])
        self.assertIn("speed table entry 9 does not exist", text(r))

    def test_a_slide_on_an_arpeggio_is_reported(self):
        t = [([0x41, 0x41, 0x41, 0xFF], [0, 4, 7, 1]), E, E, SPEEDS[3]]
        r, song = held(inst(wp=1), t, cmd=1, data=1)
        self.assertEqual(len(played(song)), 1)
        self.assertIn("portamento on a note whose wavetable plays an arpeggio", text(r))

    def test_a_command_with_no_note_sounding_is_reported(self):
        p = [(C4, 1, 0, 0), (gt.KEYOFF, 0, 0, 0), (gt.REST, 0, 1, 1), (gt.REST, 0, 3, 1)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], inst(), SPEEDS))
        self.assertIn("'portamento up' on a row with no note sounding", text(r))
        self.assertIn("tone portamento' on a row without a new note", text(r))

    def test_tone_portamento_glides_in_from_the_last_pitch(self):
        # C4 to G4: 2220 units at 64 a frame is 35 frames: 70 slide units
        r, song = two(inst(), SPEEDS, 3, 1)
        self.assertEqual(played(song), [(48, 48, "x", 0), (55, 48, "x", 70)])
        self.assertIn("tone portamento", text(r))
        assert_valid(self, song)

    def test_tone_portamento_with_no_speed_changes_pitch_at_once(self):
        r, song = two(inst(), SPEEDS, 3, 0)
        self.assertEqual([n[3] for n in played(song)], [0, 0])
        self.assertIn("tie note", text(r))

    def test_tone_portamento_to_the_same_note_or_from_nothing_does_not_slide(self):
        r, song = held(inst(), SPEEDS, cmd=3, data=1)
        self.assertEqual([n[3] for n in played(song)], [0])
        r, song = two(inst(), SPEEDS, 3, 9)
        self.assertEqual([n[3] for n in played(song)], [0, 0])
        self.assertIn("speed table entry 9 does not exist", text(r))

    def test_a_tone_portamento_does_not_start_the_instruments_wavetable(self):
        t = [([0x81, 0x41, 0xFF], [0, 0, 2]), E, E, SPEEDS[3]]
        r, song = two(inst(wp=1, fw=0x81), t, 3, 1)
        self.assertEqual([n[2] for n in played(song)[-1:]], ["x"])        # the slid note is the instrument as it is, not its noise hit


class CommandTests(unittest.TestCase):
    def test_set_attack_decay_and_sustain_release_change_the_note_only(self):
        r, song = two(inst(), SPEEDS, 5, 0x08)
        self.assertEqual([n[2] for n in played(song)], ["x", "x_ad08"])
        i = song.instruments["x_ad08"]
        self.assertEqual((i.a, i.d, i.s, i.r), (0, 8, 10, 4))
        r, song = two(inst(), SPEEDS, 6, 0xF2)
        i = song.instruments["x_srF2"]
        self.assertEqual((i.a, i.d, i.s, i.r), (0, 9, 15, 2))
        assert_valid(self, song)

    def test_the_next_note_goes_back_to_the_instrument(self):
        p = [(C4, 1, 5, 0x08), (E4, 0, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], inst()))
        self.assertEqual([n[2] for n in played(r.songs[0].song)], ["x_ad08", "x"])

    def test_an_envelope_change_part_way_through_a_note_is_reported(self):
        r, song = held(inst(), SPEEDS, extra={3: (5, 0x00)})
        self.assertEqual(len(played(song)), 1)
        self.assertIn("'set attack/decay' on a row without a new note", text(r))

    def test_set_waveform(self):
        r, song = two(inst(fw=0x41), SPEEDS, 7, 0x81)
        self.assertEqual([(n[2], song.instruments[n[2]].wave) for n in played(song)], [("x", "pulse"), ("x_noise", "noise")])

    def test_set_waveform_does_nothing_while_a_wavetable_is_running(self):
        t = [([0x41, 0x41, 0x41, 0xFF], [0, 4, 7, 1]), E, E, E]
        r, song = two(inst(wp=1), t, 7, 0x81)
        self.assertEqual([n[2] for n in played(song)], ["x", "x"])
        self.assertIn("does nothing while the instrument's wavetable is running", text(r))

    def test_set_waveform_works_once_the_wavetable_has_ended(self):
        r, song = two(inst(wp=1, fw=0x41), [([0x41], [0]), E, E, E], 7, 0x81)
        self.assertEqual([n[2] for n in played(song)], ["x", "x_noise"])

    def test_a_waveform_change_part_way_splits_the_note(self):
        r, song = held(inst(fw=0x41), SPEEDS, extra={3: (7, 0x81)})
        self.assertEqual([(n[1], n[2]) for n in played(song)], [(72, "x"), (120, "x_noise")])
        self.assertIn("part-way through", text(r))
        assert_valid(self, song)

    def test_a_silent_waveform_command_ends_the_note(self):
        r, song = held(inst(fw=0x41), SPEEDS, extra={3: (7, 0x08)})
        self.assertEqual([n[1] for n in played(song)], [72])

    def test_set_wavetable_pointer_starts_another_wavetable(self):
        t = [([0x41, 0x41, 0x41, 0xFF], [0, 4, 7, 1]), E, E, E]
        r, song = held(inst(), t, cmd=8, data=1)
        self.assertEqual([(n[0], n[2]) for n in played(song)], [(48, "x_arp0_4_7r24")])
        self.assertEqual(song.instruments["x_arp0_4_7r24"].arp, (0, 4, 7))
        r, song = held(inst(wp=1), t, cmd=8, data=0)                   # 0 stops it: the arpeggio goes
        self.assertEqual([n[2] for n in played(song)], ["x_arpoff"])
        self.assertEqual(song.instruments["x_arpoff"].arp, ())
        r, song = held(inst(wp=1), t, cmd=8, data=9)                   # a pointer outside the table plays nothing
        self.assertEqual([n[2] for n in played(song)], ["x_arpoff"])

    def test_set_pulsetable_pointer_starts_another_pulse_sweep(self):
        tri = [E, ([0x88, 25, 25, 0xFF], [0x00, 0x28, 0xD8, 2]), E, E]
        r, song = held(inst(), tri, cmd=9, data=1)
        i = song.instruments["x_pw50_50_74_2"]
        self.assertEqual((i.pw, i.pws_from, i.pws_to, i.pws_rate), (50, 50, 74, 2))
        r, song = held(inst(pp=1), tri, cmd=9, data=0)                  # 0 stops it
        self.assertEqual(song.instruments["x_pw50_0_0_0"].pws_rate, 0)

    def test_master_volume_is_a_volume_change_on_every_voice(self):
        r, song = two(inst(), SPEEDS, 13, 0x08)
        for v in ("V1", "V2", "V3"):
            kinds = [e.__class__.__name__ for e in song.tracks[v]]
            self.assertEqual(kinds.count("Volume"), 1)
            self.assertEqual([e.value for e in song.tracks[v] if e.__class__.__name__ == "Volume"], [8])
            self.assertLess(kinds.index("Note"), kinds.index("Volume"))      # after the first note, before the second
        self.assertEqual(sum(e.__class__.__name__ == "Note" for e in song.tracks["V1"]), 2)
        self.assertIn("master volume changes apply", text(r))
        notes = song.compile().voices[0]
        self.assertEqual([n.vol for n in notes], [15, 8])
        assert_valid(self, song)

    def test_a_timing_mark_is_not_a_volume(self):
        r, song = two(inst(), SPEEDS, 13, 0x48)
        self.assertFalse(any(e.__class__.__name__ == "Volume" for e in song.tracks["V1"]))
        self.assertIn("timing mark", text(r))

    def test_funk_tempo_alternates_the_row_lengths(self):
        # entry 1 is 2 and 4 frames: the rows last 2, 4, 2 frames
        p = [(C4, 1, gt.CMD_FUNKTEMPO, 1), (E4, 0, 0, 0), (G4, 0, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0], [1, gt.LOOPSONG, 0], [1, gt.LOOPSONG, 0]], [pat(*p), pat((gt.REST, 0, 0, 0))],
                        inst(), [E, E, E, ([2], [4])]))
        self.assertEqual([n[1] for n in played(r.songs[0].song)], [8, 16, 8])
        self.assertIn("funk tempo alternates 2 and 4", text(r))

    def test_a_tempo_command_ends_the_funk(self):
        p = [(C4, 1, gt.CMD_FUNKTEMPO, 1), (E4, 0, gt.CMD_SETTEMPO, 3), (G4, 0, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0], [1, gt.LOOPSONG, 0], [1, gt.LOOPSONG, 0]], [pat(*p), pat((gt.REST, 0, 0, 0))],
                        inst(), [E, E, E, ([2], [4])]))
        self.assertEqual([n[1] for n in played(r.songs[0].song)], [8, 12, 12])

    def test_funk_tempo_recall_resumes_the_alternation(self):
        p = [(C4, 1, gt.CMD_FUNKTEMPO, 1), (E4, 0, gt.CMD_SETTEMPO, 3), (G4, 0, gt.CMD_SETTEMPO, 0), (C4, 0, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0], [1, gt.LOOPSONG, 0], [1, gt.LOOPSONG, 0]], [pat(*p), pat((gt.REST, 0, 0, 0))],
                        inst(), [E, E, E, ([2], [4])]))
        self.assertEqual([n[1] for n in played(r.songs[0].song)], [8, 12, 8, 16])

    def test_a_loop_after_a_funk_tempo_is_where_the_funk_puts_it(self):
        # order: pattern 0 (funk: 2 + 4 + 2 + 4 frames), then pattern 1, restarting at order position 1
        p0 = [(C4, 1, gt.CMD_FUNKTEMPO, 1), (E4, 0, 0, 0), (G4, 0, 0, 0), (E4, 0, 0, 0)]
        p1 = [(C4, 1, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        order = [0, 1, gt.LOOPSONG, 1]
        r = gt.load(sng([order, [1, gt.LOOPSONG, 0], [1, gt.LOOPSONG, 0]], [pat(*p0), pat(*p1)], inst(), [E, E, E, ([2], [4])]))
        ticks = 0
        for e in r.songs[0].song.tracks["V1"]:
            if e.__class__.__name__ == "Loop":
                break
            ticks += getattr(e, "dur", 0) or 0
        self.assertEqual(ticks, 12 * 4)                          # not the 24 frames a constant speed of 6 would give

    def test_a_funk_tempo_with_no_speed_entry_is_reported(self):
        p = [(C4, 1, gt.CMD_FUNKTEMPO, 5), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], inst(), [E, E, E, E]))
        self.assertIn("funk tempo: speed table entry 5", text(r))


class FilterTests(unittest.TestCase):
    # row 1: low pass, resonance 2, voice 1; row 2: cutoff 0x40
    STATIC = [E, E, ([0x90, 0x00], [0x21, 0x40]), E]

    def test_a_filter_table_filters_the_voices_it_names(self):
        r, song = load(inst(fp=1), self.STATIC)
        self.assertEqual(sorted(song.instruments), ["x", "x_flp12_2"])
        i = song.instruments["x_flp12_2"]
        self.assertEqual((i.filt, i.filt_cut, i.filt_res, i.fs_rate), ("lp", 12, 2, 0))
        self.assertEqual([n[2] for n in played(song, "V1")], ["x_flp12_2"])
        self.assertEqual([n[2] for n in played(song, "V2")], ["x"])               # voice 2 is not routed through it
        self.assertNotIn("filter table", text(r))
        self.assertIn("one shared filter", text(r))
        assert_valid(self, song)

    def test_every_voice_in_the_mask_gets_the_filter(self):
        r, song = load(inst(fp=1), [E, E, ([0x90, 0x00], [0x27, 0x40]), E])
        self.assertEqual({played(song, v)[0][2] for v in ("V1", "V2", "V3")}, {"x_flp12_2"})
        self.assertEqual(sorted(song.instruments), ["x_flp12_2"])

    def test_filter_types(self):
        for left, mode in ((0x90, "lp"), (0xA0, "bp"), (0xC0, "hp")):
            r, song = load(inst(fp=1), [E, E, ([left, 0x00], [0x21, 0x40]), E])
            self.assertEqual(only_variant_filter(song), mode)
        r, song = load(inst(fp=1), [E, E, ([0xB0, 0x00], [0x21, 0x40]), E])           # low and band pass together
        self.assertEqual(only_variant_filter(song), "lp")
        self.assertIn("lp+bp together are played as lp", text(r))

    def test_a_cutoff_that_moves_is_a_sweep(self):
        # row 3 raises the cutoff 8 a frame for 10 frames and then keeps doing it (to the top)
        t = [E, E, ([0x90, 0x00, 10, 0xFF], [0x21, 0x10, 0x08, 3]), E]
        r, song = held(inst(fp=1), t)
        i = song.instruments["x_flp7_2_fs7_15_3"]
        self.assertEqual((i.filt_cut, i.fs_from, i.fs_to, i.fs_rate), (7, 7, 15, 3))
        self.assertIn("cutoff swing back and forth", text(r))
        assert_valid(self, song)

    def test_a_cutoff_that_falls_sweeps_from_where_it_starts(self):
        t = [E, E, ([0x90, 0x00, 10, 0xFF], [0x21, 0xF0, 0xF0, 3]), E]            # starts at 240, -16 a frame
        r, song = held(inst(fp=1), t)
        i = [v for v in song.instruments.values() if v.fs_rate][0]
        self.assertGreater(i.fs_from, i.fs_to)

    def test_commands_a_b_and_c_steer_the_filter(self):
        # A starts a table that sets low pass and a cutoff for no voice; B then routes voice 1 and sets the resonance; C sets the cutoff
        t = [E, E, ([0x90, 0x00], [0x00, 0x20]), E]
        p = [(C4, 1, gt.CMD_FILTERPTR, 1), (C4, 0, gt.CMD_FILTERCTRL, 0x21), (C4, 0, gt.CMD_FILTERCUT, 0x40), (C4, 0, gt.CMD_FILTERCTRL, 0),
             (C4, 0, 0, 0), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], inst(), t))
        song = r.songs[0].song
        self.assertEqual([n[2] for n in played(song)], ["x", "x_flp9_2", "x_flp12_2", "x", "x"])
        # (B 00 on the fourth row switches the filter off before that note sounds)
        assert_valid(self, song)

    def test_a_filter_table_pointer_outside_the_table_is_reported(self):
        r, song = load(inst(fp=1), [E, E, E, E])
        self.assertIn("filter table pointer 1 is outside the filter table", text(r))

    def test_resonance_and_voices_without_a_filter_type_do_nothing(self):
        r, song = load(inst(), [E, E, E, E], cmd=gt.CMD_FILTERCTRL, data=0x41)
        self.assertEqual(list(song.instruments), ["x"])
        self.assertIn("no filter type was set", text(r))


def only_variant_filter(song):
    return song.instruments[played(song, "V1")[0][2]].filt


class RoundTripTests(unittest.TestCase):
    def test_a_song_with_everything_compiles_and_survives_mml(self):
        tri = [([0x81, 0x41, 0xFF], [0x9C, 0, 2]), ([0x88, 25, 25, 0xFF], [0x00, 0x28, 0xD8, 2]), ([0x90, 0x00], [0x21, 0x40]), ([0x00], [0x40])]
        ins = [(0x09, 0xA4, 1, 1, 1, 0, 0, 2, 0x81, "drum"), (0x0A, 0xF4, 0, 0, 0, 0, 0, 2, 0x41, "lead")]
        p = [(C4, 1, 0, 0), (gt.REST, 0, 1, 1), (E4, 2, 3, 1), (gt.REST, 0, 0, 0), (C4, 0, 5, 0x08), (gt.REST, 0, 13, 8), (gt.KEYOFF, 0, 0, 0)]
        r = gt.load(sng([[0, gt.LOOPSONG, 0]] * 3, [pat(*p)], ins, tri))
        assert_valid(self, r.songs[0].song)
        for v in ("V1", "V2", "V3"):
            self.assertTrue(played(r.songs[0].song, v))
        block = TunePlayer(r.songs[0].song.compile()).render(8192)         # the voices accept every instrument made here
        self.assertEqual(block.shape, (8192, 2))

    def test_a_song_that_uses_none_of_this_makes_only_its_own_instruments(self):
        r = gt.load(simple())
        self.assertEqual(list(r.songs[0].song.instruments), ["lead"])
        self.assertEqual(r.report.dropped, [])


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
