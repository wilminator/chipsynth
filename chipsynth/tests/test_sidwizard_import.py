# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""SID-Wizard .swm import: the files are built here from the format, byte by byte; no real songs are used."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from chipsynth import __main__ as cli, package as pk
from chipsynth.formats import sidwizard as sw

C4, E4, G4 = 49, 53, 56          # SWM note 1 is C-0, so C-4 is 49
OFF = 0x7E


def row(note=0, inst=0, fx=0, val=0):
    if fx:
        return bytes([note | 0x80, inst | 0x80, fx]) + (bytes([val]) if fx < 0x20 else b"")
    if inst:
        return bytes([note | 0x80, inst])
    return bytes([note])


def pack(rows):
    """Rows (bytes each) -> the packed body: a run of empty rows is a $00 and then $70-$77 markers for 2-9 more."""
    out, i = bytearray(), 0
    while i < len(rows):
        if rows[i] != b"\0":
            out += rows[i]
            i += 1
            continue
        n = 0
        while i + n < len(rows) and rows[i + n] == b"\0":
            n += 1
        out.append(0)
        left = n - 1
        while left >= 2:
            k = min(left, 9)
            out.append(0x70 + k - 2)
            left -= k
        if left == 1:
            out.append(0)
        i += n
    return bytes(out)


def pattern(*rows, length=None):
    """rows are tuples for row() (or bytes); returns (packed body, size, length)."""
    rb = [r if isinstance(r, bytes) else row(*r) for r in rows]
    unpacked = sum(len(r) for r in rb)
    return pack(rb), unpacked + 1, length or len(rb)


def padded(rows, n):
    return list(rows) + [(0,)] * (n - len(rows))


def instrument(ad=0x09, sr=0xA4, wf=((0x41, 0, 0),), pw=(), filt=(), arpspeed=0, octave=0, vib=0, name="lead", ctrl=0x0A, firstwave=0x09):
    flat = lambda t: b"".join(bytes(r) for r in t) + b"\xff"
    wfb, pwb, fb = flat(wf), flat(pw), flat(filt)
    pwp = 16 + len(wfb)
    fp = pwp + len(pwb)
    head = bytes([ctrl, 0x0F, 0xF0, ad, sr, vib, 0, arpspeed, 1, octave & 0xFF, pwp, fp, 0, 0, 0, firstwave])
    return head + wfb + pwb + fb, name


def swm(seqs, patterns, instruments=(), tempos=((0x86, 0x86),), chords=b"", tempotable=b"", frame_speed=1, author="me",
        wrap="prg", defaultlen=32, driver=3, tuning=0, header_patch=None):
    """seqs: orderlist byte lists (three per subtune); patterns: pattern() results; instruments: instrument() results."""
    h = bytearray(64)
    h[0:4] = b"SWM1"
    h[4] = frame_speed
    h[11] = defaultlen
    h[12], h[13], h[14], h[15], h[16] = len(seqs), len(patterns), len(instruments), len(chords), len(tempotable)
    h[19], h[20] = driver, tuning
    h[24:64] = author.encode().ljust(40, b" ")[:40]
    if header_patch:
        for k, v in header_patch.items():
            h[k] = v
    body = bytearray(h)
    for s in seqs:
        body += bytes(s) + bytes([len(s)])
    for packed, size, length in patterns:
        body += packed + bytes([size, length])
    for data, name in instruments:
        body += data[:-1] + bytes([len(data) - 1]) + name.encode().ljust(8, b" ")[:8]
    body += chords + tempotable
    for left, right in tempos:
        body += bytes([left, right])
    if wrap == "prg":
        return b"\x00\x1f" + bytes(body)          # load address $1F00 ...
    if wrap == "p00":
        return b"C64File\0" + b"TEST".ljust(16, b"\0") + b"\0\0" + b"\xf8\x1f" + bytes(body)
    return bytes(body)


def one(rows, ins=None, seq=(1, 0xFF, 0), **kw):
    """A song of one pattern on channel 1 and nothing on the others."""
    ins = ins or [instrument()]
    return swm([list(seq), [0xFE], [0xFE]], [pattern(*rows)], ins, **kw)


def notes_of(song, track="V1"):
    return [e for e in song.tracks[track] if e.__class__.__name__ == "Note"]


def load(data, **kw):
    r = sw.load(data, **kw)
    return r, r.songs[0].song


class ParseTests(unittest.TestCase):
    def test_header_counts_and_instrument(self):
        t = sw.parse(one([(C4, 1), (0,), (OFF,)], tuning=0))
        self.assertEqual((t.magic, t.frame_speed, t.default_pattern_len, t.driver, t.author), ("SWM1", 1, 32, 3, "me"))
        self.assertEqual((len(t.sequences), len(t.patterns), len(t.instruments)), (3, 1, 1))
        self.assertEqual(t.instruments[0].name, "lead")
        self.assertEqual((t.instruments[0].ad, t.instruments[0].sr), (0x09, 0xA4))
        self.assertEqual(t.subtune_tempos, [(0x86, 0x86)])

    def test_bare_prg_and_p00_files_all_read(self):
        for wrap in ("bare", "prg", "p00"):
            data = one([(C4, 1)], wrap=wrap)
            self.assertTrue(sw.looks_like(data))
            self.assertEqual(sw.parse(data).patterns[0].rows[0].note, C4)
        self.assertEqual(sw.parse(one([(C4, 1)], wrap="p00")).name, "TEST")

    def test_packed_empty_rows_expand(self):
        rows = [(C4, 1)] + [(0,)] * 14 + [(E4,)]
        t = sw.parse(one(rows))
        self.assertEqual(len(t.patterns[0].rows), 16)
        self.assertEqual([i for i, r in enumerate(t.patterns[0].rows) if r.note], [0, 15])
        packed = pattern(*rows)[0]
        self.assertLess(len(packed), 10)                      # really packed: 14 empties are a $00 and two markers

    def test_columns_and_effects(self):
        t = sw.parse(one([(C4, 2, 0x10, 5), (0, 0x3F), (E4, 1, 0x30)]))
        r = t.patterns[0].rows
        self.assertEqual((r[0].note, r[0].inst, r[0].fx, r[0].val), (C4, 2, 0x10, 5))
        self.assertEqual((r[1].inst, r[1].fx), (0x3F, 0))
        self.assertEqual((r[2].note, r[2].inst, r[2].fx), (E4, 1, 0x30))

    def test_subtune_count_follows_the_sequences(self):
        data = swm([[1, 0xFF, 0], [0xFE], [0xFE]] * 3, [pattern((C4, 1))], [instrument()], tempos=[(0x86, 0x86)] * 3)
        self.assertEqual(sw.parse(data).subtunes, 3)

    def test_garbage_and_truncation_are_errors_not_crashes(self):
        with self.assertRaises(sw.SidWizardError):
            sw.parse(b"not a song")
        data = one([(C4, 1), (0,), (OFF,)])
        for cut in (2, 5, 40, 70, len(data) - 3):
            with self.assertRaises(sw.SidWizardError):
                sw.parse(data[:cut])
        with self.assertRaises(sw.SidWizardError):
            sw.parse(data + b"\0\0\0")                          # data that does not add up to the header's counts

    def test_a_bad_table_pointer_is_an_error(self):
        data, name = instrument()
        bad = data[:10] + bytes([0x80]) + data[11:]
        with self.assertRaises(sw.SidWizardError):
            sw.parse(swm([[1, 0xFF, 0], [0xFE], [0xFE]], [pattern((C4, 1))], [(bad, name)]))


class PlayTests(unittest.TestCase):
    def test_notes_rests_and_gate_off(self):
        # tempo 6: a row is 6 frames = 24 ticks; C lasts through the empty row (48), E until the gate-off (24)
        r, song = load(one([(C4, 1), (0,), (E4,), (OFF,), (0,)]))
        self.assertEqual([(n.letter, n.dur) for n in notes_of(song)], [("c", 48), ("e", 24)])
        self.assertEqual(song.voices, 3)
        self.assertEqual(song.title, "SID-Wizard song")

    def test_bpm_follows_the_region_and_timing_notes_say_so(self):
        self.assertEqual(load(one([(C4, 1)]))[1].bpm, 125)
        self.assertEqual(load(one([(C4, 1)]), region="ntsc")[1].bpm, 150)
        self.assertTrue(any("PAL" in n for n in load(one([(C4, 1)]))[0].report.notes))

    def test_pitch_is_note_minus_one_semitones_from_c0(self):
        r, song = load(one([(1, 1), (0,), (49 + 9, 0), (OFF,)]))
        self.assertEqual([(n.letter) for n in notes_of(song)], ["c", "a"])
        octaves = [e.set for e in song.tracks["V1"] if e.__class__.__name__ == "Octave"]
        self.assertEqual(octaves, [0, 4])                       # C-0, then the A in C-4's octave

    def test_instrument_adsr_wave_and_name(self):
        r, song = load(one([(C4, 1), (OFF,)], ins=[instrument(ad=0x2B, sr=0xC5, wf=((0x41, 0, 0),), pw=((0x88, 0x00, 0),))]))
        inst = list(song.instruments.values())[0]
        self.assertEqual((inst.a, inst.d, inst.s, inst.r), (2, 11, 12, 5))
        self.assertEqual(inst.wave, "pulse")
        self.assertEqual(list(song.instruments)[0], "lead")

    def test_pulse_width_value(self):
        r, song = load(one([(C4, 1), (OFF,)], ins=[instrument(pw=((0x88, 0x00, 0),))]))
        self.assertEqual(list(song.instruments.values())[0].pw, 50)        # $800 / $1000

    def test_waveforms(self):
        for bits, wave in ((0x11, "tri"), (0x21, "saw"), (0x41, "pulse"), (0x81, "noise")):
            r, song = load(one([(C4, 1)], ins=[instrument(wf=((bits, 0, 0),))]))
            self.assertEqual(list(song.instruments.values())[0].wave, wave)
        r, song = load(one([(C4, 1)], ins=[instrument(wf=((0x61, 0, 0),))]))
        self.assertEqual(list(song.instruments.values())[0].wave, "pulse")
        self.assertTrue(any("combines waves" in a for a in r.report.approximated))

    def test_unnamed_instrument_gets_a_name(self):
        r, song = load(one([(C4, 1)], ins=[instrument(name="........")]))
        self.assertEqual(list(song.instruments), ["instr1"])

    def test_tempo_from_the_subtune(self):
        r, song = load(one([(C4, 1), (E4,), (OFF,)], tempos=[(0x83, 0x83)]))
        self.assertEqual([n.dur for n in notes_of(song)], [12, 12])          # 3 frames x 4 ticks

    def test_funk_tempo_alternates(self):
        r, song = load(one([(C4, 1), (E4,), (G4,), (OFF,)], tempos=[(0x08, 0x84)]))      # 8, 4, 8, 4 ...
        self.assertEqual([n.dur for n in notes_of(song)], [32, 16, 32])

    def test_tempo_pattern_effect_and_orderlist_tempo(self):
        r, song = load(one([(C4, 1, 0x10, 3), (E4,), (OFF,)]))
        self.assertEqual([n.dur for n in notes_of(song)], [12, 12])
        r, song = load(one([(C4, 1), (E4,), (OFF,)], seq=(0xB0 + 2, 1, 0xFF, 1)))
        self.assertEqual([n.dur for n in notes_of(song)], [8, 8])             # 2 frames a row
        r, song = load(one([(C4, 1, 0x11, 0x42), (E4,), (G4,), (OFF,)]))
        self.assertEqual([n.dur for n in notes_of(song)], [16, 8, 16])        # funk 4, 2

    def test_main_tempo_changes_every_channel(self):
        data = swm([[1, 0xFF, 0], [2, 0xFF, 0], [0xFE]], [pattern((C4, 1, 0x10, 3), (E4,), (OFF,)), pattern((G4, 1), (G4,), (OFF,))],
                   [instrument()])
        r, song = load(data)
        self.assertEqual([n.dur for n in notes_of(song, "V2")][:2], [12, 12])

    def test_track_tempo_changes_one_channel(self):
        data = swm([[1, 0xFF, 0], [2, 0xFF, 0], [0xFE]], [pattern((C4, 1, 0x13, 3), (E4,), (OFF,)), pattern((G4, 1), (G4,), (OFF,))],
                   [instrument()])
        r, song = load(data)
        self.assertEqual([n.dur for n in notes_of(song, "V1")][:2], [12, 12])
        self.assertEqual([n.dur for n in notes_of(song, "V2")][:2], [24, 24])

    def test_orderlist_transpose_and_patterns(self):
        seq = (0x90 + 2, 1, 0x90 - 1, 1, 0xFF, 0)
        r, song = load(one([(C4, 1), (OFF,)], seq=seq))
        self.assertEqual([n.letter for n in notes_of(song)][:2], ["d", "b"])      # up two, then down one
        self.assertEqual(sum(1 for e in song.tracks["V1"] if e.__class__.__name__ == "Loop"), 1)

    def test_instrument_octave_shift(self):
        r, song = load(one([(C4, 1), (OFF,)], ins=[instrument(octave=12)]))
        self.assertEqual([e.set for e in song.tracks["V1"] if e.__class__.__name__ == "Octave"], [5])
        r, song = load(one([(C4, 1), (OFF,)], ins=[instrument(octave=-1)]))
        self.assertEqual([n.letter for n in notes_of(song)], ["b"])

    def test_loop_point_and_unrolling_of_channels_with_different_lengths(self):
        # channel 1 plays pattern 1 (2 rows) and loops to the start; channel 2 plays pattern 2 (3 rows) and loops
        data = swm([[1, 0xFF, 0], [2, 0xFF, 0], [0xFE]], [pattern((C4, 1), (OFF,)), pattern((G4, 1), (0,), (OFF,))], [instrument()])
        r, song = load(data)
        # a shared loop of lcm(48, 72) = 144 ticks: V1 plays three times, V2 twice
        self.assertEqual(len(notes_of(song, "V1")), 3)
        self.assertEqual(len(notes_of(song, "V2")), 2)
        self.assertTrue(any("different places or lengths" in a for a in r.report.approximated))

    def test_loop_to_a_later_position(self):
        seq = (1, 2, 0xFF, 1)                               # intro (pattern 1), then pattern 2 for ever
        data = swm([list(seq), [0xFE], [0xFE]], [pattern((C4, 1), (OFF,)), pattern((E4, 1), (OFF,))], [instrument()])
        r, song = load(data)
        ev = song.tracks["V1"]
        loops = [i for i, e in enumerate(ev) if e.__class__.__name__ == "Loop"]
        self.assertEqual(len(loops), 1)
        before = [e for e in ev[:loops[0]] if e.__class__.__name__ == "Note"]
        self.assertEqual([n.letter for n in before], ["c"])
        self.assertEqual([n.letter for n in notes_of(song)], ["c", "e"])

    def test_no_loop_means_no_loop_marker(self):
        r, song = load(one([(C4, 1), (OFF,)], seq=(1, 0xFE)))
        self.assertEqual(sum(1 for e in song.tracks["V1"] if e.__class__.__name__ == "Loop"), 0)

    def test_frame_speed_two_halves_the_tick_length(self):
        r, song = load(one([(C4, 1), (E4,), (OFF,)], frame_speed=2))
        self.assertEqual([n.dur for n in notes_of(song)], [12, 12])           # 6 calls a row at 2 per frame = 12 ticks

    def test_odd_frame_speed_is_rounded_and_said(self):
        r, song = load(one([(C4, 1), (E4,), (OFF,)], frame_speed=3))
        self.assertEqual([n.dur for n in notes_of(song)], [8, 8])               # 6 calls at 3 a frame = 8 ticks
        self.assertTrue(any("frame speed x3" in a for a in r.report.approximated))

    def test_several_subtunes_become_several_songs(self):
        data = swm([[1, 0xFF, 0], [0xFE], [0xFE]] * 2, [pattern((C4, 1), (OFF,))], [instrument()], tempos=[(0x86, 0x86), (0x83, 0x83)])
        r = sw.load(data)
        self.assertEqual(len(r.songs), 2)
        self.assertEqual([n.dur for n in notes_of(r.songs[1].song)], [12])
        self.assertEqual(len(sw.load(data, subtune=2).songs), 1)
        with self.assertRaises(sw.SidWizardError):
            sw.load(data, subtune=3)

    def test_title_and_author(self):
        r, song = load(one([(C4, 1)], author="Hermit"), name="Tune")
        self.assertEqual((song.title, song.author), ("Tune", "Hermit"))
        self.assertEqual(load(one([(C4, 1)], wrap="p00"))[1].title, "TEST")

    def test_orderlist_with_an_endless_loop_does_not_hang(self):
        data = swm([[0x91, 0xFF, 0], [0xFE], [0xFE]], [pattern((C4, 1))], [instrument()])
        r, song = load(data)
        self.assertTrue(any("plays nothing" in n for n in r.report.notes))


class UseTests(unittest.TestCase):
    def three_voice_song(self):
        wf = ((0x41, 0, 0), (0x41, 4, 0), (0x41, 7, 0), (0xFE, 16, 0))
        ins = [instrument(wf=wf, pw=((0x84, 0, 0),), name="lead"), instrument(wf=((0x21, 0, 0),), name="bass")]
        pats = [pattern((C4, 1), (0,), (E4,), (OFF,)), pattern((C4 - 12, 2), (0,), (0,), (G4 - 12,)), pattern((G4, 1), (E4,), (C4,))]
        return swm([[1, 0xFF, 0], [2, 0xFF, 0], [3, 0xFF, 0]], pats, ins)

    def test_the_song_compiles_and_renders(self):
        from chipsynth import TunePlayer
        r, song = load(self.three_voice_song())
        tune = song.compile()
        block = TunePlayer(tune).render(22050)
        self.assertEqual(block.shape, (22050, 2))
        self.assertGreater(float(abs(block).max()), 0.01)

    def test_the_song_survives_a_round_trip_through_mml(self):
        from chipsynth.song import Song
        r, song = load(self.three_voice_song())
        again = Song.from_mml(song.to_mml())
        for t in ("V1", "V2", "V3"):
            self.assertEqual(len(notes_of(again, t)), len(notes_of(song, t)))
        self.assertEqual(again.instruments["lead"].arp, (0, 4, 7))


class TableTests(unittest.TestCase):
    def inst_of(self, **kw):
        r, song = load(one([(C4, 1), (OFF,)], ins=[instrument(**kw)]))
        return r, list(song.instruments.values())[0]

    def test_a_wave_table_arpeggio_becomes_arp(self):
        wf = ((0x41, 0, 0), (0x41, 4, 0), (0x41, 7, 0), (0xFE, 16, 0))
        r, inst = self.inst_of(wf=wf)
        self.assertEqual(inst.arp, (0, 4, 7))
        self.assertTrue(any("arpeggio" in n for n in r.report.notes))
        self.assertEqual(r.report.dropped, [])

    def test_arpeggio_speed_sets_the_step_rate(self):
        wf = ((0x41, 0, 0), (0x41, 12, 0), (0xFE, 16, 0))
        _, fast = self.inst_of(wf=wf, arpspeed=0)
        _, slow = self.inst_of(wf=wf, arpspeed=1)
        self.assertEqual(fast.arp, (0, 12))
        self.assertEqual(slow.arp, (0, 12))
        self.assertEqual(slow.arprate * 2, fast.arprate)        # twice as long a step is half as many a beat

    def test_a_repeat_row_holds_the_step(self):
        wf = ((0x41, 0, 0), (0x01, 7, 0), (0xFE, 16, 0))        # $01: the row lasts one call more (two in all)
        r, inst = self.inst_of(wf=wf)
        self.assertEqual(inst.arp, (0, 7, 7))

    def test_a_one_shot_pitch_drop_is_reported_not_carried(self):
        wf = ((0x81, 12, 0), (0x41, 0, 0))
        r, inst = self.inst_of(wf=wf)
        self.assertEqual(inst.arp, ())
        self.assertTrue(any("wave table program" in d for d in r.report.dropped))
        self.assertEqual(inst.wave, "noise")                     # the first waveform is kept

    def test_chords_absolute_notes_and_detune_are_reported(self):
        for wf, what in ((((0x41, 0x7F, 0),), "chord"), (((0x41, 0x90, 0),), "absolute"), (((0x41, 0, 5),), "detunes")):
            r, inst = self.inst_of(wf=wf)
            self.assertTrue(any(what in d for d in r.report.dropped), (what, r.report.dropped))

    def test_a_pulse_sweep_becomes_pws(self):
        # set $400, sweep up by $20 a call for 0x20 calls, set $c00, sweep down, jump back
        pw = ((0x84, 0x00, 0), (0x20, 0x20, 0), (0x8C, 0x00, 0), (0x20, 0xE0, 0), (0xFE, 0x14, 0))
        r, inst = self.inst_of(pw=pw)
        self.assertEqual(inst.pw, 25)
        self.assertEqual(inst.wave, "pulse")
        self.assertGreater(inst.pws_rate, 0)
        self.assertEqual((inst.pws_from, inst.pws_to), (25, 75))
        self.assertTrue(any("pulse table" in n for n in r.report.notes))

    def test_pulse_table_is_not_run_for_other_waveforms(self):
        pw = ((0x84, 0x00, 0), (0x20, 0x20, 0), (0xFE, 0x14, 0))
        r, inst = self.inst_of(pw=pw, wf=((0x21, 0, 0),))
        self.assertEqual((inst.wave, inst.pws_rate), ("saw", 0))
        self.assertFalse(any("pulse" in d for d in r.report.dropped))

    def test_filter_and_vibrato_are_reported(self):
        r, inst = self.inst_of(filt=((0x9F, 0x03, 0x01),), vib=0x25)
        text = "\n".join(r.report.lines())
        self.assertIn("filter table", text)
        self.assertIn("vibrato", text)

    def test_pulse_table_that_sweeps_before_setting_is_reported(self):
        r, inst = self.inst_of(pw=((0x10, 0x20, 0), (0xFE, 0x14, 0)))
        self.assertTrue(any("before setting one" in d for d in r.report.dropped))


class ReportTests(unittest.TestCase):
    def test_unsupported_things_are_reported_by_name(self):
        rows = [(C4, 1, 0x08, 0x35), (0x65,), (0x78,), (E4, 0x3F), (G4, 0x52), (0, 0, 0x2A), (OFF,)]
        r, song = load(one(rows))
        text = "\n".join(r.report.lines())
        for word in ("set vibrato", "note-column vibrato", "auto-portamento", "legato", "set sustain", "set attack"):
            self.assertIn(word, text)

    def test_a_clean_song_has_nothing_dropped(self):
        r, song = load(one([(C4, 1), (E4,), (OFF,)]))
        self.assertEqual(r.report.dropped, [])
        self.assertEqual(r.report.approximated, [])

    def test_tuning_other_than_440_is_said(self):
        r, song = load(one([(C4, 1)], tuning=1))
        self.assertTrue(any("432" in a for a in r.report.approximated))

    def test_missing_instrument_and_pattern_are_reported(self):
        data = swm([[1, 5, 0xFF, 0], [0xFE], [0xFE]], [pattern((C4, 9), (OFF,))], [instrument()])
        r, song = load(data)
        text = "\n".join(r.report.lines())
        self.assertIn("instrument 9", text)
        self.assertIn("pattern 5", text)

    def test_orderlist_main_volume_is_reported(self):
        r, song = load(one([(C4, 1)], seq=(0xAF, 1, 0xFF, 1)))
        self.assertTrue(any("main volume" in d for d in r.report.dropped))


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(args))
        return code, out.getvalue()

    def test_import_adds_a_song_to_a_package_and_writes_the_report(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "my tune.swm"
            src.write_bytes(one([(C4, 1, 0x08, 0x35), (OFF,)]))
            pkg, rep = Path(d) / "p.csp", Path(d) / "rep.json"
            code, out = self.run_cli("import", str(pkg), str(src), "--report", str(rep))
            self.assertEqual(code, 0)
            self.assertIn("my_tune", out)
            self.assertIn("set vibrato", out)
            self.assertIn("my_tune", pk.load(pkg).songs)
            self.assertIn("set vibrato", json.dumps(json.loads(rep.read_text())))

    def test_import_is_found_by_content_when_the_name_has_no_swm_ending(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "tune.prg"
            src.write_bytes(one([(C4, 1), (OFF,)]))
            code, out = self.run_cli("import", str(Path(d) / "p.csp"), str(src))
            self.assertEqual(code, 0)

    def test_subtune_and_region_options(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "two.swm"
            src.write_bytes(swm([[1, 0xFF, 0], [0xFE], [0xFE]] * 2, [pattern((C4, 1), (OFF,))], [instrument()], tempos=[(0x86, 0x86)] * 2))
            pkg = Path(d) / "p.csp"
            self.run_cli("import", str(pkg), str(src), "--song", "2", "--region", "ntsc")
            songs = pk.load(pkg).songs
            self.assertEqual(list(songs), ["two"])                    # one song keeps the plain file name, as for GoatTracker
            self.assertEqual(songs["two"].bpm, 150)

    def test_a_bad_file_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "bad.swm"
            src.write_bytes(b"\0\0nope")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = self.run_cli("import", str(Path(d) / "p.csp"), str(src))
            self.assertEqual(code, 1)
            self.assertIn("SID-Wizard", err.getvalue())


if __name__ == "__main__":
    unittest.main()
