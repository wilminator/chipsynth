# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""SID Factory II .sf2 import.  The files are built here from the format, byte by byte (a driver-11-shaped memory image
with its descriptor blocks, tables, order lists and sequences); no real songs are used or stored."""
import contextlib
import io
import json
import random
import struct
import tempfile
import unittest
from pathlib import Path

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from chipsynth import __main__ as cli, package as pk
from chipsynth.composer import fileops as fo
from chipsynth.formats import sf2
from chipsynth.tests.test_composer_filemenu import Base, key

LOAD = 0x0D7E
A = dict(cmds=0x1200, instr=0x12C0, wave=0x1380, pulse=0x1580, filt=0x1880, arp=0x1B80, tempo=0x1C80, init=0x1D80,
         seqlo=0x1E00, seqhi=0x1E80, ollo=0x1F00, olhi=0x1F03, orders=0x2000, seqs=0x3000, aux=0x1100)
END = 0x3600
C4, E4, G4 = 0x30, 0x34, 0x37


def screen(name):
    """The editor's text: lower case letters as screen codes 1-26."""
    return bytes(ord(c) - 96 if c.islower() else ord(c) for c in name)


def ev(note, inst=None, cmd=None, dur=None, tie=False):
    """One sequence event: [command] [instrument] [duration] note."""
    out = bytearray()
    if cmd is not None:
        out.append(0xC0 | cmd)
    if inst is not None:
        out.append(0xA0 | inst)
    if dur is not None:
        out.append(0x80 | dur | (0x10 if tie else 0))
    out.append(note)
    return bytes(out)


class Img:
    """A driver 11 shaped .sf2 image.  Tables are column major, as the real driver 11 has them."""

    def __init__(self, name="Driver 11.00 - The Standard", version=(11, 0, 0), songs=1, tracks=3, tempo=(5,), pal=None,
                 song_names=None, order_size=0x100):
        self.mem = bytearray(0x10000)
        self.name, self.version, self.tracks, self.nsongs = name, version, tracks, songs
        self.order_size, self.pal, self.song_names = order_size, pal, song_names
        self.nseq = 0
        self.set_table("wave", 0, [(0x7F, 0)])
        self.set_table("pulse", 0, [(0x7F, 0, 0)])
        self.set_tempo(0, tempo)
        for s in range(songs):
            for t in range(tracks):
                self.order(s, t, [0xFE])

    TABLES = (("commands", 0x81, 0, "Commands", 3, 64, "cmds"), ("instruments", 0x80, 1, "Instruments", 6, 32, "instr"),
              ("wave", 0, 2, "Wave", 2, 256, "wave"), ("pulse", 0, 3, "Pulse", 3, 256, "pulse"),
              ("filter", 0, 4, "Filter", 3, 256, "filt"), ("arp", 0, 6, "Arp", 1, 256, "arp"),
              ("tempo", 0, 7, "Tempo", 1, 256, "tempo"), ("init", 0, 8, "Init", 2, 32, "init"))

    def set_table(self, name, row, rows):
        for key, _t, _i, _n, cols, nrows, addr in self.TABLES:
            if key == name:
                for k, values in enumerate(rows):
                    for c, v in enumerate(values):
                        self.mem[A[addr] + c * nrows + row + k] = v
                return
        raise KeyError(name)

    def set_tempo(self, row, values):
        self.set_table("tempo", row, [(v,) for v in values] + [(0x7F,)])

    def instrument(self, i, ad=0x09, sr=0xA4, flags=0, filt=0, pulse=0, wave=0):
        self.set_table("instruments", i, [(ad, sr, flags, filt, pulse, wave)])

    def command(self, i, ctype, p1=0, p2=0):
        self.set_table("commands", i, [(ctype, p1, p2)])

    def order(self, song, track, data):
        base = A["orders"] + (song * self.tracks + track) * self.order_size
        self.mem[base:base + len(data)] = bytes(data)

    def sequence(self, index, data):
        addr = A["seqs"] + index * 0x100
        self.mem[addr:addr + len(data)] = bytes(data) + b"\x7f"
        self.mem[A["seqlo"] + index], self.mem[A["seqhi"] + index] = addr & 255, addr >> 8
        self.nseq = max(self.nseq, index + 1)

    def block(self, bid, data):
        return bytes((bid, len(data))) + bytes(data)

    def build(self, tables=True, aux=True):
        desc = bytes((0,)) + struct.pack("<H", 1000) + screen(self.name) + b"\0" + struct.pack("<HH", 0x1000, 0x400) + bytes(self.version)
        tab = bytearray()
        for key, ttype, tid, tname, cols, rows, addr in self.TABLES:
            if key in ("arp", "init", "filter") and self.version[0] == 12:
                continue
            tab += bytes((ttype, tid, 0)) + screen(tname) + b"\0" + bytes((1, 0, 0, 0, 0)) + struct.pack("<HHH", A[addr], cols, rows) + bytes((16,))
        tab.append(0xFF)
        music = struct.pack("<BHHBHHHHHH", self.tracks, A["ollo"], A["olhi"], 128, A["seqlo"], A["seqhi"], self.order_size,
                            A["orders"], 0x100, A["seqs"])
        head = struct.pack("<H", 0x1337) + self.block(1, desc) + self.block(2, bytes(40)) + (self.block(3, tab) if tables else b"") \
            + self.block(4, b"\0") + self.block(5, music) + b"\xff"
        self.mem[LOAD:LOAD + len(head)] = head
        if aux:
            blocks = bytearray()
            if self.pal is not None:
                blocks += bytes((2,)) + struct.pack("<HH", 2, 5) + bytes((1, 0 if self.pal else 1, 0, 0, 0))
            if self.song_names is not None:
                names = b"".join(bytes((len(n),)) + n.encode() for n in self.song_names)
                blocks += bytes((5,)) + struct.pack("<HH", 2, 2 + len(names)) + bytes((len(self.song_names), 0)) + names
            blocks += bytes((0,)) + struct.pack("<HH", 0, 0)
            self.mem[A["aux"]:A["aux"] + len(blocks)] = blocks
            self.mem[0x0FFB], self.mem[0x0FFC] = A["aux"] & 255, A["aux"] >> 8
        return struct.pack("<H", LOAD) + bytes(self.mem[LOAD:END])


def simple(**kw):
    """One song, three tracks: track 1 plays C-4 for two rows then a gate-off for two rows, then stops."""
    img = Img(**kw)
    img.instrument(1)
    img.sequence(0, ev(C4, inst=1, dur=1) + ev(0x00))
    img.order(0, 0, [0xA0, 0x00, 0xFE])
    return img


def notes(song, track="V1"):
    return [e for e in song.tracks[track] if e.__class__.__name__ == "Note"]


def kinds(song, track="V1"):
    return [e.__class__.__name__ for e in song.tracks[track]]


def text(r):
    return "\n".join(r.report.lines())


class ParseTests(unittest.TestCase):
    def test_the_header_is_read(self):
        f = sf2.parse(simple().build())
        self.assertEqual((f.driver_name, f.version, f.tracks, f.load), ("Driver 11.00 - The Standard", (11, 0, 0), 3, LOAD))
        self.assertEqual(sorted(f.tables), ["arp", "commands", "filter", "init", "instruments", "pulse", "tempo", "wave"])
        self.assertEqual((f.tables["instruments"].cols, f.tables["instruments"].rows), (6, 32))

    def test_looks_like(self):
        self.assertTrue(sf2.looks_like(simple().build()))
        self.assertFalse(sf2.looks_like(b"GTS5\0\0"))
        self.assertFalse(sf2.looks_like(b""))

    def test_garbage_and_truncation_are_errors_not_crashes(self):
        with self.assertRaises(sf2.Sf2Error):
            sf2.parse(b"not a song at all")
        with self.assertRaises(sf2.Sf2Error):
            sf2.load(simple().build(tables=False))
        data = simple().build()
        for cut in (3, 7, 20, 90, 200, 300):
            with self.assertRaises(sf2.Sf2Error):
                sf2.load(data[:cut])

    def test_random_damage_never_escapes_as_another_exception(self):
        data = simple().build()
        rng = random.Random(1234)
        for _ in range(400):
            b = bytearray(data)
            for _k in range(rng.randint(1, 8)):
                where = rng.choice((rng.randrange(2, 420), rng.randrange(0x1100 - LOAD, len(b))))
                b[where] = rng.randrange(256)
            try:
                sf2.load(bytes(b))
            except sf2.Sf2Error:
                pass

    def test_text_is_decoded_from_screen_codes(self):
        self.assertEqual(sf2._screen_text(screen("Instruments")), "Instruments")


class PlayTests(unittest.TestCase):
    def test_notes_rests_and_gate_off_and_the_tempo(self):
        r = sf2.load(simple().build())
        song = r.songs[0].song
        # tempo 5 = 6 frames a row = 24 ticks: C-4 lasts two rows; the gate-off two more, then the song stops
        self.assertEqual([(n.letter, n.acc, n.dur) for n in notes(song)], [("c", 0, 48)])
        self.assertNotIn("Octave", kinds(song))                  # note 0x30 is C-4 and octave 4 is the default
        self.assertEqual(sum(getattr(e, "dur", 0) for e in song.tracks["V1"] if e.__class__.__name__ in ("Note", "Rest")), 96)
        self.assertEqual(song.voices, 3)
        self.assertNotIn("Loop", kinds(song))

    def test_pitch_is_the_note_number_from_c0(self):
        img = simple()
        img.sequence(0, ev(0x3F, inst=1, dur=0) + ev(0x00))      # 0x3f = 63 = D#5
        song = sf2.load(img.build()).songs[0].song
        self.assertEqual(kinds(song)[:3], ["Select", "Octave", "Note"])
        self.assertEqual(song.tracks["V1"][1].set, 5)
        self.assertEqual((notes(song)[0].letter, notes(song)[0].acc), ("d", 1))

    def test_the_bpm_follows_pal_or_ntsc_as_the_file_records(self):
        self.assertEqual(sf2.load(simple().build()).songs[0].song.bpm, 125)             # no record: PAL
        self.assertEqual(sf2.load(simple(pal=False).build()).songs[0].song.bpm, 150)
        self.assertEqual(sf2.load(simple(pal=True).build()).songs[0].song.bpm, 125)
        self.assertEqual(sf2.load(simple(pal=True).build(), region="ntsc").songs[0].song.bpm, 150)
        self.assertIn("does not record PAL or NTSC", text(sf2.load(simple().build())))
        with self.assertRaises(sf2.Sf2Error):
            sf2.load(simple().build(), region="secam")

    def test_the_tempo_table_cycles_and_wraps(self):
        img = Img(tempo=(3, 5))                                   # 4 and 6 frames a row, then the wrap
        img.instrument(1)
        img.sequence(0, b"".join(ev(C4, inst=1 if i == 0 else None) for i in range(4)) + ev(0x00))
        img.order(0, 0, [0xA0, 0x00, 0xFE])
        song = sf2.load(img.build()).songs[0].song
        self.assertEqual([n.dur for n in notes(song)], [16, 24, 16, 24])

    def test_the_init_table_picks_the_tempo_program_of_the_song(self):
        img = simple(songs=2, song_names=["a", "b"])
        img.set_tempo(4, (2,))                                    # 3 frames a row, at row 4
        img.set_table("init", 1, [(4, 15)])
        img.order(1, 0, [0xA0, 0x00, 0xFE])
        r = sf2.load(img.build())
        self.assertEqual(len(r.songs), 2)
        self.assertEqual(notes(r.songs[0].song)[0].dur, 48)
        self.assertEqual(notes(r.songs[1].song)[0].dur, 24)       # 2 rows of 12 ticks

    def test_a_tempo_command_changes_the_tempo_program(self):
        img = Img(tempo=(5,))
        img.set_tempo(8, (1,))                                    # 2 frames a row
        img.command(2, 0x0D, 0, 8)
        img.instrument(1)
        img.sequence(0, ev(C4, inst=1, cmd=2) + ev(C4) + ev(0x00))
        img.order(0, 0, [0xA0, 0x00, 0xFE])
        song = sf2.load(img.build()).songs[0].song
        self.assertEqual([n.dur for n in notes(song)], [24, 8])   # the row with the command is still 6 frames

    def test_order_list_transpose_and_loop(self):
        img = simple()
        img.order(0, 0, [0xA2, 0x00, 0xA0, 0x00, 0xFF, 0x00])    # +2, the sequence, +0, the sequence, loop to the start
        song = sf2.load(img.build()).songs[0].song
        self.assertEqual([n.letter for n in notes(song)[:2]], ["d", "c"])
        self.assertEqual(kinds(song).count("Loop"), 1)
        r = sf2.load(img.build())
        self.assertIn("loops back", text(r))

    def test_the_loop_point_can_be_inside_the_list(self):
        img = simple()
        img.sequence(1, ev(E4, inst=1, dur=1) + ev(0x00))
        img.order(0, 0, [0xA0, 0x00, 0x01, 0xFF, 0x02])           # loop to the byte at offset 2: the second sequence
        song = sf2.load(img.build()).songs[0].song
        ev_ = song.tracks["V1"]
        i = [e.__class__.__name__ for e in ev_].index("Loop")
        self.assertEqual([e.letter for e in ev_[:i] if e.__class__.__name__ == "Note"], ["c"])   # the first pass plays C before the loop mark
        self.assertEqual([e.letter for e in ev_[i:] if e.__class__.__name__ == "Note"], ["e"])

    def test_tracks_two_and_three_become_v2_and_v3(self):
        img = simple()
        img.sequence(1, ev(G4, inst=1, dur=1) + ev(0x00))
        img.order(0, 1, [0xA0, 0x01, 0xFE])
        img.order(0, 2, [0xA0 + 12, 0x01, 0xFE])                 # an octave up
        song = sf2.load(img.build()).songs[0].song
        self.assertEqual([(n.letter, n.dur) for n in notes(song, "V2")], [("g", 48)])
        self.assertEqual(song.tracks["V3"][1].set, 5)

    def test_several_songs_and_their_names(self):
        img = simple(songs=2, song_names=["Intro", ""])
        img.order(1, 0, [0xA0, 0x00, 0xFE])
        r = sf2.load(img.build())
        self.assertEqual([i.name for i in r.songs], ["Intro", "Song 2"])
        self.assertEqual(len(sf2.load(img.build(), song=2).songs), 1)
        with self.assertRaises(sf2.Sf2Error):
            sf2.load(img.build(), song=3)

    def test_a_missing_sequence_is_reported_and_skipped(self):
        img = simple()
        img.order(0, 0, [0xA0, 0x05, 0x00, 0xFE])                 # sequence 5 does not exist (its pointer is 0)
        r = sf2.load(img.build())
        self.assertIn("sequence 05", text(r))
        self.assertEqual(len(notes(r.songs[0].song)), 1)

    def test_extra_tracks_of_a_multi_sid_driver_are_reported(self):
        img = simple(tracks=6)
        r = sf2.load(img.build())
        self.assertIn("tracks 4-6", text(r))
        self.assertEqual(sorted(r.songs[0].song.tracks), ["V1", "V2", "V3"])


class InstrumentTests(unittest.TestCase):
    def inst(self, img, name="i01"):
        return sf2.load(img.build()).songs[0].song.instruments[name]

    def wave_img(self, rows, ad=0x2A, sr=0x8F, **kw):
        img = simple()
        img.instrument(1, ad=ad, sr=sr, wave=1, **kw)
        img.set_table("wave", 1, rows)
        return img

    def test_adsr_nibbles_and_waveform(self):
        i = self.inst(self.wave_img([(0x41, 0), (0x7F, 1)]))
        self.assertEqual((i.a, i.d, i.s, i.r, i.wave), (2, 10, 8, 15, "pulse"))
        self.assertEqual(self.inst(self.wave_img([(0x81, 0), (0x7F, 1)])).wave, "noise")
        self.assertEqual(self.inst(self.wave_img([(0x21, 0), (0x7F, 1)])).wave, "saw")
        self.assertEqual(self.inst(self.wave_img([(0x11, 0), (0x7F, 1)])).wave, "tri")

    def test_a_combined_waveform_is_approximated_and_ring_sync_reported(self):
        r = sf2.load(self.wave_img([(0x57, 0), (0x7F, 1)]).build())
        self.assertIn("combines waves", text(r))
        self.assertIn("ring modulation / hard sync", text(r))

    def test_a_relative_semitone_offset_shifts_the_pitch(self):
        song = sf2.load(self.wave_img([(0x11, 12), (0x7F, 1)]).build()).songs[0].song     # "11 0c": triangle, an octave up
        self.assertEqual(song.tracks["V1"][1].set, 5)
        self.assertEqual(notes(song)[0].letter, "c")

    def test_an_absolute_note_is_a_fixed_pitch(self):
        img = self.wave_img([(0x81, 0x80 + 0x40), (0x7F, 1)])
        song = sf2.load(img.build()).songs[0].song
        self.assertEqual(song.tracks["V1"][1].set, 5)             # 0x40 = 64 = E-5
        self.assertEqual(notes(song)[0].letter, "e")

    def test_a_cycling_wave_table_is_an_arpeggio(self):
        r = sf2.load(self.wave_img([(0x41, 0), (0x41, 4), (0x41, 7), (0x7F, 1)]).build())
        i = r.songs[0].song.instruments["i01"]
        self.assertEqual(i.arp, (0, 4, 7))
        self.assertEqual(i.arprate, 24)                           # a frame a step: 24 steps to the beat
        self.assertIn("arpeggio of 0,4,7", text(r))

    def test_a_wave_table_with_a_click_first_keeps_the_longer_waveform(self):
        r = sf2.load(self.wave_img([(0x81, 0), (0x41, 0), (0x7F, 2)]).build())
        self.assertEqual(r.songs[0].song.instruments["i01"].wave, "pulse")
        self.assertIn("changes waveform", text(r))

    def test_pulse_set_row_gives_the_start_width(self):
        img = self.wave_img([(0x41, 0), (0x7F, 1)], pulse=1)
        img.set_table("pulse", 1, [(0x88, 0x00, 0), (0x7F, 0, 1)])      # width 0x800 = 50%... then holds
        self.assertEqual(self.inst(img).pw, 50)
        img.set_table("pulse", 1, [(0x84, 0x00, 0), (0x7F, 0, 1)])      # 0x400 = 25%
        self.assertEqual(self.inst(img).pw, 25)

    def test_a_pulse_ramp_up_and_down_is_a_sweep(self):
        img = self.wave_img([(0x41, 0), (0x7F, 1)], pulse=1)
        img.set_table("pulse", 1, [(0x84, 0x00, 0), (0x00, 0x10, 16), (0x0F, 0xF0, 16), (0x7F, 0, 2)])
        i = self.inst(img)
        self.assertGreater(i.pws_rate, 0)
        self.assertLess(i.pws_from, i.pws_to)
        self.assertEqual(i.pws_from, 25)

    def test_a_pulse_sweep_on_a_non_pulse_wave_is_ignored(self):
        img = self.wave_img([(0x21, 0), (0x7F, 1)], pulse=1)
        img.set_table("pulse", 1, [(0x84, 0x00, 0), (0x00, 0x10, 16), (0x0F, 0xF0, 16), (0x7F, 0, 2)])
        self.assertEqual(self.inst(img).pws_rate, 0)

    def test_filter_program_is_left_out(self):
        r = sf2.load(self.wave_img([(0x41, 0), (0x7F, 1)], flags=0x40, filt=3).build())
        self.assertIn("filter program", text(r))

    def test_local_and_persistent_adsr_commands(self):
        img = simple()
        img.instrument(1)
        img.instrument(2, ad=0x11, sr=0x22)
        img.command(1, 0x08, 0x12, 0x34)                          # local ADSR
        img.command(2, 0x09, 0x56, 0x78)                          # persistent ADSR
        img.sequence(0, ev(C4, inst=1, cmd=1) + ev(C4, cmd=2) + ev(C4) + ev(C4, inst=2) + ev(0x00))
        img.order(0, 0, [0xA0, 0x00, 0xFE])
        song = sf2.load(img.build()).songs[0].song
        names = [e.name for e in song.tracks["V1"] if e.__class__.__name__ == "Select"]
        self.assertEqual(names, ["i01_adsr1234", "i01_adsr5678", "i02"])        # a different instrument ends the persistent ADSR
        self.assertEqual((song.instruments["i01_adsr1234"].a, song.instruments["i01_adsr1234"].d), (1, 2))
        self.assertEqual((song.instruments["i01_adsr5678"].s, song.instruments["i01_adsr5678"].r), (7, 8))

    def test_an_arpeggio_command_is_a_chord_and_says_how_it_guessed(self):
        img = simple()
        img.instrument(1)
        img.set_table("arp", 0, [(12,), (7,), (3,), (0,), (0x71,)])
        img.command(1, 0x03, 1, 0)
        img.sequence(0, ev(C4, inst=1, cmd=1, dur=3) + ev(0x00))
        img.order(0, 0, [0xA0, 0x00, 0xFE])
        r = sf2.load(img.build())
        i = r.songs[0].song.instruments["i01_arp0001"]
        self.assertEqual(i.arp, (7, 3, 0))
        self.assertEqual(i.arprate, 12)                           # 2 frames a step
        self.assertIn("lead-in", text(r))
        self.assertIn("not verified", text(r))

    def test_unsupported_commands_are_reported(self):
        img = simple()
        img.instrument(1)
        img.command(1, 0x00, 0, 8)                                # slide
        img.command(2, 0x01, 4, 4)                                # vibrato
        img.sequence(0, ev(C4, inst=1, cmd=1) + ev(C4, cmd=2) + ev(0x00))
        img.order(0, 0, [0xA0, 0x00, 0xFE])
        r = sf2.load(img.build())
        self.assertIn("command 'slide'", text(r))
        self.assertIn("command 'vibrato'", text(r))

    def test_tied_notes_are_played_as_new_notes_and_reported(self):
        img = simple()
        img.sequence(0, ev(C4, inst=1) + ev(E4, dur=0, tie=True) + ev(0x00))
        r = sf2.load(img.build())
        self.assertEqual(len(notes(r.songs[0].song)), 2)
        self.assertIn("tied notes (1)", text(r))

    def test_a_driver_that_is_not_one_of_the_standard_ones_keeps_the_notes_and_says_so(self):
        img = simple(name="NP20.G4.00.00", version=(4, 0, 0))
        r = sf2.load(img.build())
        self.assertEqual(len(notes(r.songs[0].song)), 1)
        self.assertIn("is not one of the standard drivers", text(r))

    def test_the_simple_drivers_have_a_waveform_and_a_pulse_width_in_the_instrument(self):
        img = Img(name="Driver 12.00 - The Barber", version=(12, 0, 0))
        img.set_table("instruments", 1, [(0x09, 0xA4, 0x21, 0x04, 0, 0)])
        img.sequence(0, ev(C4, inst=1, dur=1) + ev(0x00))
        img.order(0, 0, [0xA0, 0x00, 0xFE])
        i = sf2.load(img.build()).songs[0].song.instruments["i01"]
        self.assertEqual((i.wave, i.pw), ("saw", 25))             # 0x04: the top 4 bits of the width, so 0x400


class RunTests(unittest.TestCase):
    """The songs compile and play through the package, so what the importer builds is a valid chipsynth song."""

    def test_an_imported_song_renders(self):
        r = sf2.load(simple().build())
        pkg = pk.Package(name="x")
        pkg.add_song("s", r.songs[0].song, {})
        tune = pkg.tune("s", {})
        self.assertEqual(tune.nvoices, 3)


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

    def two(self):
        img = simple(songs=2, song_names=["a", "b"])
        img.order(1, 0, [0xA0, 0x00, 0xFE])
        return img.build()

    def test_import_names_the_song_after_the_file(self):
        rc, out, err = run("import", self.pkg, self.write("my tune.sf2", simple().build()))
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("added song 'my_tune'", out)
        self.assertEqual(pk.load(self.pkg).songs["my_tune"].bpm, 125)
        self.assertIn("note: Driver 11.00", out)

    def test_region_overrides_the_file(self):
        rc, out, err = run("import", self.pkg, self.write("t.sf2", simple(pal=True).build()), "--region", "ntsc")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(pk.load(self.pkg).songs["t"].bpm, 150)

    def test_songs_and_song_picks_one(self):
        f = self.write("multi.sf2", self.two())
        rc, out, err = run("import", self.pkg, f)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(sorted(pk.load(self.pkg).songs), ["multi_1", "multi_2"])
        pkg2 = self.tmp / "q.csp"
        rc, out, err = run("import", pkg2, f, "--song", "2")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(sorted(pk.load(pkg2).songs), ["multi"])
        self.assertEqual(run("import", pkg2, f, "--song", "3")[0], 1)
        self.assertEqual(run("import", pkg2, f, "--song", "lead")[0], 1)

    def test_id_no_replace_and_the_report_file(self):
        f = self.write("t.sf2", simple().build())
        report = self.tmp / "r.json"
        rc, out, err = run("import", self.pkg, f, "--id", "town", "--report", report)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(sorted(json.loads(report.read_text())), ["approximated", "dropped", "notes"])
        self.assertEqual(run("import", self.pkg, f, "--id", "town", "--no-replace")[0], 1)
        self.assertEqual(run("import", self.pkg, self.write("m.sf2", self.two()), "--id", "x")[0], 1)

    def test_a_song_is_found_by_its_start_when_the_name_has_no_sf2(self):
        rc, out, err = run("import", self.pkg, self.write("tune.dat", simple().build()))
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("tune", pk.load(self.pkg).songs)

    def test_a_broken_sf2_is_an_error_not_a_crash(self):
        rc, out, err = run("import", self.pkg, self.write("bad.sf2", b"not a song at all"))
        self.assertEqual(rc, 1)
        self.assertIn("not an SID Factory II song", err)
        rc, out, err = run("import", self.pkg, self.write("cut.sf2", simple().build()[:90]))
        self.assertEqual(rc, 1)
        self.assertIn("cut.sf2", err)


class ComposerTests(Base):
    """File > Import > SID Factory II, through the methods that take a path (no file dialog)."""

    def test_the_import_menu_lists_it_and_the_suffix_is_recognised(self):
        self.open_menu()
        self.click(self.row("Import"))
        self.assertTrue(any("SID Factory II" in i["label"] for _, i in self.a.menu_geo["sub"]))
        self.assertEqual(fo.kind_of("x/Tune.SF2"), "sf2")

    def test_an_sf2_song_opens_unsaved_with_its_report(self):
        a = self.a
        self.assertTrue(a.import_file(self.write("tune.sf2", simple().build())))
        self.assertEqual((a.source, a.song_id), ("", ""))
        self.assertEqual(a.tune.nvoices, 3)
        self.assertTrue(any("Driver 11.00" in x for x in a.last_report[1]))
        self.assertIn("Imported", a.message)

    def test_it_asks_for_timing_and_song_and_applies_them(self):
        a = self.a
        self.assertTrue(a.import_begin(self.write("o.sf2", simple(pal=True).build())))
        self.assertEqual(a.overlay, "fileopts")
        a.frame()
        self.assertEqual([r["key"] for r in a.fileopts["rows"]], ["region", "song"])
        self.assertIsNone(a.fileopts_values()["region"])               # "as the file says"
        a.frame([key(pygame.K_RIGHT)])                                 # -> NTSC
        self.assertEqual(a.fileopts_values()["region"], "ntsc")
        self.click(a.menu_geo["opts"]["ok"])
        self.assertNotEqual(a.overlay, "fileopts")                    # the report overlay may follow
        self.assertEqual(a.song.bpm, 150)

    def test_several_songs_go_into_a_package_beside_the_file(self):
        a = self.a
        img = simple(songs=2, song_names=["one", "two"])
        img.order(1, 0, [0xA0, 0x00, 0xFE])
        p = self.write("multi.sf2", img.build())
        self.assertEqual(fo.song_names("sf2", p), ["one", "two"])
        self.assertTrue(a.import_file(p))
        self.assertTrue((self.dir / "multi.csp").exists())
        self.assertEqual(sorted(pk.load(self.dir / "multi.csp").songs), ["multi_1", "multi_2"])

    def test_a_bad_file_is_said_not_raised(self):
        a = self.a
        self.assertFalse(a.import_file(self.write("bad.sf2", b"nope nope nope")))
        self.assertIn("Can't import", a.message)


if __name__ == "__main__":
    unittest.main()
