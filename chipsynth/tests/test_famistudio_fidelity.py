# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""FamiStudio text import, fidelity: expansion channels on extra voices, and table-driven instruments played frame by frame.
Every FamiStudio file here is built by hand in the test (nothing is a real song)."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import numpy as np

from chipsynth import mml, package as pk, song as sg
from chipsynth.__main__ import main
from chipsynth.formats import envelope as env, famistudio_text as fam

# FamiTracker tempo 150 / speed 6: a row is 6 frames, so with a 4-row beat a frame is 4 ticks (3.99 here, NTSC).
HEAD = 'Project Version="4.5.3" TempoMode="FamiTracker" Name="T" Author="A"%s\n'
SONG = '\tSong Name="S" Length="1" LoopPoint="-1" PatternLength="16" BeatLength="4" FamiTrackerTempo="150" FamiTrackerSpeed="6"\n'


def chan(kind, *notes):
    """A channel with one pattern holding `notes` ('Time Value Duration [extra attributes]')."""
    lines = ['\t\tChannel Type="%s"' % kind, '\t\t\tPattern Name="P"']
    for n in notes:
        t, v, d, *rest = n.split(" ", 3)
        lines.append('\t\t\t\tNote Time="%s" Value="%s" Duration="%s" %s' % (t, v, d, rest[0] if rest else ""))
    lines.append('\t\t\tPatternInstance Time="0" Pattern="P"')
    return "\n".join(lines) + "\n"


def build(instruments="", channels="", expansions="", extra=""):
    head = HEAD % (' Expansions="%s"' % expansions if expansions else "") + extra
    return head + instruments + SONG + channels


def load(text, **kw):
    r = fam.load(text, **kw)
    return r, r.songs[0]


def tune_of(imp):
    return imp.song.compile(dict(mml.builtin_library(), _mem=imp.samples))


def notes(tune, v):
    return [(n.start, n.dur, n.pitch, n.vol) for n in tune.voices[v]]


def report_text(r):
    return "\n".join(r.report.lines())


WAVE64 = lambda f: ",".join(str(int(round(f(i / 64.0)))) for i in range(64))
SINE64 = WAVE64(lambda x: 31.5 + 31.5 * np.sin(2 * np.pi * x))
SAW64 = WAVE64(lambda x: 63 * x)
TRI64 = WAVE64(lambda x: 63 * (1 - abs(2 * x - 1)))


class ExpansionTests(unittest.TestCase):
    def test_vrc6_pulses_and_saw_get_voices_after_the_four_with_the_right_duty(self):
        text = build(
            instruments='\tInstrument Name="Pu" Expansion="VRC6"\n\t\tEnvelope Type="DutyCycle" Length="1" Values="3"\n'
                        '\tInstrument Name="Sw" Expansion="VRC6"\n',
            channels=chan("VRC6Square1", '0 C4 4 Instrument="Pu"', '4 E4 4 DutyCycle="7"')
            + chan("VRC6Square2", '0 G4 4') + chan("VRC6Saw", '0 C3 8 Instrument="Sw"'),
            expansions="VRC6")
        r, imp = load(text)
        song = imp.song
        self.assertEqual((song.voices, song.dacs), (7, 1))
        t = tune_of(imp)
        self.assertEqual([n[2] for n in notes(t, 4)], [48, 52])                  # C4, E4 on V5
        self.assertEqual(notes(t, 5)[0][2], 55)                                  # G4 on V6
        self.assertEqual(notes(t, 6)[0][2], 36)                                      # C3 on V7
        self.assertEqual(t.voices[6][0].inst.wave, "saw")
        self.assertEqual((t.voices[4][0].inst.wave, t.voices[4][0].inst.pw), ("pulse", 25))      # VRC6 duty 3 = 4/16
        self.assertEqual(t.voices[4][1].inst.pw, 50)                              # note duty 7 = 8/16
        self.assertEqual(t.voices[5][0].inst.pw, 50)                              # no envelope: the 50% default
        self.assertEqual(song.tracks["V5"][0], sg.Comment(text="VRC6Square1"))
        self.assertIn("V5", report_text(r))
        self.assertFalse(r.report.dropped)

    def test_vrc6_duty_table_is_sixteenths(self):
        self.assertEqual(fam.VRC6_PW, tuple(round((n + 1) * 100 / 16) for n in range(8)))

    def test_mmc5_pulses_use_the_nes_duties_and_its_pcm_channel_is_left_out(self):
        text = build('\tInstrument Name="L"\n\t\tEnvelope Type="DutyCycle" Length="1" Values="1"\n',
                     chan("MMC5Square1", '0 C4 4 Instrument="L"') + chan("MMC5DPCM", "0 C4 4"), expansions="MMC5")
        r, imp = load(text)
        self.assertEqual(imp.song.voices, 5)
        self.assertEqual(tune_of(imp).voices[4][0].inst.pw, 25)
        self.assertTrue(any("MMC5DPCM" in m for m in r.report.dropped))

    def test_fds_and_n163_waves_are_fitted_and_reported_as_approximate(self):
        text = build(
            instruments='\tInstrument Name="F" Expansion="FDS" FdsModDepth="20" FdsModSpeed="50"\n'
                        '\t\tEnvelope Type="FDSWave" Length="64" Values="%s"\n' % SAW64
            + '\tInstrument Name="N" Expansion="N163" N163WaveSize="16"\n'
              '\t\tEnvelope Type="N163Wave" Length="16" Values="15,15,15,15,0,0,0,0,0,0,0,0,0,0,0,0"\n',
            channels=chan("FDS", '0 C3 4 Instrument="F"') + chan("N163Wave1", '0 C4 4 Instrument="N"'),
            expansions="FDS,N163")
        r, imp = load(text)
        t = tune_of(imp)
        self.assertEqual(t.voices[4][0].inst.wave, "saw")
        self.assertEqual((t.voices[5][0].inst.wave, t.voices[5][0].inst.pw), ("pulse", 25))     # 4 of 16 high
        text = report_text(r)
        self.assertIn("FDS wavetable): played as saw", text)
        self.assertIn("N163 wavetable): played as a 25% pulse", text)
        self.assertIn("frequency modulation", text)
        self.assertTrue(all(m.startswith("instrument") for m in r.report.approximated))

    def test_wave_fit_names_the_nearest_shape_and_its_error(self):
        self.assertEqual(env.fit_wave([float(x) for x in SINE64.split(",")])[0], "sine")
        self.assertEqual(env.fit_wave([float(x) for x in TRI64.split(",")])[0], "tri")
        w, pw, err = env.fit_wave([15] * 8 + [0] * 24)
        self.assertEqual((w, pw), ("pulse", 25))
        self.assertLess(err, 0.01)
        w, pw, err = env.fit_wave([0, 15, 0, 15, 3, 12, 8, 8])                    # nothing chipsynth has looks like this
        self.assertGreater(err, 0.5)

    def test_s5b_squares_are_plain_pulses_and_their_noise_mix_is_reported(self):
        text = build('\tInstrument Name="B" Expansion="S5B"\n\t\tEnvelope Type="MixerSettings" Length="1" Values="1"\n',
                     chan("S5BSquare1", '0 C4 4 Instrument="B"') + chan("S5BSquare2", "0 E4 4"), expansions="S5B")
        r, imp = load(text)
        self.assertEqual(imp.song.voices, 6)
        self.assertEqual(tune_of(imp).voices[4][0].inst.pw, 50)
        self.assertIn("noise, hardware envelope and mixer", report_text(r))

    def test_vrc7_and_epsm_fm_are_skipped_and_reported(self):
        text = build('\tInstrument Name="X" Expansion="VRC7"\n',
                     chan("VRC7FM1", '0 C4 4 Instrument="X"') + chan("EPSMFM1", "0 C4 4") + chan("EPSMSquare1", "0 C4 4"),
                     expansions="VRC7,EPSM")
        r, imp = load(text)
        self.assertEqual(imp.song.voices, 5)                                       # only the EPSM square
        dropped = "\n".join(r.report.dropped)
        for what in ("VRC7", "VRC7FM1", "EPSMFM1"):
            self.assertIn(what, dropped)
        self.assertNotIn("EPSMSquare1", dropped)

    def test_a_channel_of_a_chip_the_project_does_not_name_is_still_skipped(self):
        r, imp = load(build(channels=chan("VRC6Saw", "0 C4 4")))
        self.assertEqual(imp.song.voices, 4)
        self.assertIn("channel VRC6Saw (expansion audio)", r.report.dropped)

    def test_an_expansion_instrument_on_the_wrong_chip_gets_defaults_and_a_report(self):
        text = build('\tInstrument Name="W" Expansion="FDS"\n', chan("VRC6Saw", '0 C4 4 Instrument="W"'), expansions="VRC6,FDS")
        r, _ = load(text)
        self.assertIn("instrument 'W' uses expansion audio: plain defaults used", r.report.dropped)

    def test_more_channels_than_voices_are_reported_not_forgotten(self):
        chans = "".join(chan("N163Wave%d" % i, "0 C%d 4" % (i % 8)) for i in range(1, 9))
        chans += chan("VRC6Square1", "0 C4 4") + chan("VRC6Square2", "0 C4 4") + chan("VRC6Saw", "0 C4 4")
        chans += chan("MMC5Square1", "0 C4 4") + chan("MMC5Square2", "0 C4 4") + chan("FDS", "0 C4 4")
        r, imp = load(build(channels=chans, expansions="VRC6,MMC5,FDS,N163"))
        self.assertEqual(imp.song.voices, 16)
        tune_of(imp)                                                               # still compiles
        left = [m for m in r.report.dropped if "no voice left" in m]
        self.assertEqual(sorted(m.split()[1].rstrip(":") for m in left), ["N163Wave7", "N163Wave8"])
        # V5.. follow EXP_ORDER: VRC6 first, FDS and N163 after
        self.assertEqual([e.text for e in (imp.song.tracks["V%d" % n][0] for n in (5, 6, 7, 8, 9, 10, 11))],
                         ["VRC6Square1", "VRC6Square2", "VRC6Saw", "MMC5Square1", "MMC5Square2", "FDS", "N163Wave1"])

    def test_empty_expansion_channels_take_no_voice(self):
        r, imp = load(build(channels=chan("VRC6Saw", "0 C4 4") + '\t\tChannel Type="VRC6Square1"\n', expansions="VRC6"))
        self.assertEqual(imp.song.voices, 5)

    def test_a_file_without_expansions_or_tables_is_unchanged(self):
        text = build('\tInstrument Name="L"\n\t\tEnvelope Type="Volume" Length="1" Values="15"\n', chan("Square1", '0 C4 4 Instrument="L"'))
        r, imp = load(text)
        self.assertEqual((imp.song.voices, imp.song.dacs), (4, 1))
        self.assertEqual(list(imp.song.instruments), ["L"])
        self.assertTrue(r.report.clean)

    def test_the_song_survives_mml_and_the_package(self):
        text = build('\tInstrument Name="Sw" Expansion="VRC6"\n',
                     chan("VRC6Saw", '0 C3 8 Instrument="Sw"') + chan("Square1", "0 C4 4"), expansions="VRC6")
        _, imp = load(text)
        again = sg.Song.from_mml(imp.song.to_mml())
        self.assertEqual(again.voices, 5)
        a, b = tune_of(imp), again.compile(dict(mml.builtin_library()))
        self.assertEqual(notes(a, 4), notes(b, 4))
        self.assertEqual(b.voices[4][0].inst.wave, "saw")


class SimulationTests(unittest.TestCase):
    def test_resolve_note_jumps_to_the_release_point_when_the_note_ends(self):
        vals = [15, 12, 10, 8, 6, 4, 2, 0]
        self.assertEqual(env.resolve_note(vals, 0, 5, 8, 8), [15, 12, 10, 8, 6, 15, 12, 10])
        self.assertEqual(env.resolve_note(vals, 0, 5, 8, 8, released_at=2), [15, 12, 4, 2, 0, 0, 0, 0])
        self.assertEqual(env.resolve_note(vals, -1, -1, 4, 6), [15, 12, 10, 8, 8, 8])
        self.assertEqual(env.accumulate([3, -1], -1, 2, 5), [3, 2, 2, 2, 2])
        self.assertEqual(env.accumulate([2], 0, 1, 4), [2, 4, 6, 8])

    def test_pitch_units_are_period_steps_and_positive_is_higher(self):
        self.assertGreater(env.pitch_semitones(10, 48), 0)
        self.assertLess(env.pitch_semitones(-10, 48), 0)
        self.assertAlmostEqual(env.pitch_semitones(10, 48, 32.0), 2 * env.pitch_semitones(10, 48), delta=0.05)    # the triangle's register is half as big
        self.assertGreater(env.pitch_semitones(10, 72), env.pitch_semitones(10, 48))

    def test_a_volume_envelope_that_does_not_fit_is_played_a_frame_at_a_time(self):
        zig = ",".join(["15", "5"] * 4)
        text = build('\tInstrument Name="Z"\n\t\tEnvelope Type="Volume" Length="8" Loop="0" Values="%s"\n' % zig,
                     chan("Square1", '0 C4 2 Instrument="Z"'))
        r, imp = load(text)
        t = tune_of(imp)
        ns = notes(t, 0)
        self.assertEqual(sum(n[1] for n in ns), 48)                                # two rows of 24 ticks, no gap or overlap
        self.assertEqual([n[3] for n in ns], [15, 5] * (len(ns) // 2))
        self.assertTrue(all(n[1] in (3, 4, 5) for n in ns), ns)                    # a frame is 4 ticks, rounded to the tick
        self.assertEqual(ns[-1][0] + ns[-1][1], 48)
        self.assertTrue(all(n[0] == ns[i - 1][0] + ns[i - 1][1] for i, n in enumerate(ns) if i))
        inst = t.voices[0][0].inst
        self.assertEqual((inst.s, inst.vol, inst.r), (15, 15, 0))
        text = report_text(r)
        self.assertIn("played frame by frame as short notes", text)
        self.assertIn("volume envelope", text)
        self.assertIn("half a tick", text)

    def test_a_volume_envelope_that_fits_stays_one_note(self):
        text = build('\tInstrument Name="D"\n\t\tEnvelope Type="Volume" Length="6" Loop="5" Values="15,12,10,8,6,6"\n',
                     chan("Square1", '0 C4 4 Instrument="D"'))
        r, imp = load(text)
        self.assertEqual(len(notes(tune_of(imp), 0)), 1)
        self.assertNotIn("frame by frame", report_text(r))

    def test_the_note_volume_scales_the_frames(self):
        zig = ",".join(["15", "5"] * 4)
        text = build('\tInstrument Name="Z"\n\t\tEnvelope Type="Volume" Length="8" Loop="0" Values="%s"\n' % zig,
                     chan("Square1", '0 C4 1 Instrument="Z" Volume="8"'))
        ns = notes(tune_of(load(text)[1]), 0)
        self.assertEqual([n[3] for n in ns[:2]], [8, 3])                           # 15 * 8 / 15, 5 * 8 / 15 = 2.67

    def test_a_release_tail_plays_on_into_the_gap_but_not_into_the_next_note(self):
        vals = "15,5,15,5,12,9,6,3,0"
        inst = '\tInstrument Name="R"\n\t\tEnvelope Type="Volume" Length="9" Loop="0" Release="4" Values="%s"\n' % vals
        gap = build(inst, chan("Square1", '0 C4 1 Instrument="R"'))                 # one row (6 frames), then nothing
        ns = notes(tune_of(load(gap)[1]), 0)
        self.assertGreater(ns[-1][0] + ns[-1][1], 24)                              # the tail goes on after the row
        self.assertEqual([n[3] for n in ns][:1], [15])
        self.assertEqual([n[3] for n in ns][-4:], [12, 9, 6, 3])
        self.assertLessEqual(ns[-1][0] + ns[-1][1], 24 + 5 * 4 + 4)
        butted = build(inst, chan("Square1", '0 C4 1 Instrument="R"', '1 E4 1 Instrument="R"'))
        first = [n for n in notes(tune_of(load(butted)[1]), 0) if n[0] < 24]
        self.assertEqual(first[-1][0] + first[-1][1], 24)                          # cut by the next note

    def test_a_one_shot_arpeggio_is_played_step_by_step(self):
        text = build('\tInstrument Name="Z"\n\t\tEnvelope Type="Arpeggio" Length="4" Values="12,7,0,0"\n',
                     chan("Square1", '0 G4 2 Instrument="Z"'))
        r, imp = load(text)
        ns = notes(imp and tune_of(imp), 0)
        self.assertEqual([n[2] for n in ns], [67, 62, 55])                         # G5, D5, G4 held
        self.assertEqual([n[0] for n in ns], [0, 4, 8])
        self.assertIn("arpeggio envelope", report_text(r))

    def test_a_repeating_arpeggio_still_uses_the_arp_setting(self):
        text = build('\tInstrument Name="A"\n\t\tEnvelope Type="Arpeggio" Length="3" Loop="0" Values="0,4,7"\n', chan("Square1", '0 C4 2 Instrument="A"'))
        _, imp = load(text)
        self.assertEqual(imp.song.instruments["A"].arp, (0, 4, 7))
        self.assertEqual(len(notes(tune_of(imp), 0)), 1)

    def test_a_note_can_name_an_arpeggio(self):
        text = build('\tArpeggio Name="Maj" Length="3" Loop="0" Values="0,4,7"\n', chan("Triangle", '0 C3 1 Arpeggio="Maj"'))
        r, imp = load(text)
        ns = notes(tune_of(imp), 2)
        self.assertEqual([n[2] for n in ns], [36, 40, 43, 36, 40, 43][: len(ns)])
        self.assertTrue(all(n[3] == 15 for n in ns))                                # the triangle carries no volume: the default
        self.assertIn("arpeggio 'Maj'", report_text(r))
        self.assertEqual(sum(n[1] for n in ns), 24)

    def test_an_arpeggio_that_is_not_in_the_file_is_reported(self):
        r, _ = load(build(channels=chan("Square1", '0 C4 1 Arpeggio="Nope"')))
        self.assertTrue(any("'Nope'" in m for m in r.report.dropped))

    def test_a_big_pitch_envelope_moves_the_notes_and_the_rounding_is_reported(self):
        text = build('\tInstrument Name="K"\n\t\tEnvelope Type="Pitch" Length="4" Relative="true" Values="-20,-20,-20,-20"\n',
                     chan("Square1", '0 E4 2 Instrument="K"'))
        r, imp = load(text)
        ps = [n[2] for n in notes(tune_of(imp), 0)]
        self.assertEqual(ps[0], 51)                                                # the first -20 units is a semitone down from E4... of 52
        self.assertTrue(all(a > b for a, b in zip(ps, ps[1:])))
        self.assertIn("whole semitone", report_text(r))
        self.assertNotIn("pitch envelope", "\n".join(r.report.dropped))

    def test_a_small_looping_pitch_swing_is_vibrato_not_notes(self):
        text = build('\tInstrument Name="V"\n\t\tEnvelope Type="Pitch" Length="8" Loop="0" Values="2,4,2,0,-2,-4,-2,0"\n',
                     chan("Square1", '0 C4 4 Instrument="V"'))
        r, imp = load(text)
        inst = imp.song.instruments["V"]
        self.assertEqual((inst.vib_rate, inst.vib_depth), (9, 3))
        self.assertEqual(len(notes(tune_of(imp), 0)), 1)
        self.assertIn("played as vibrato", report_text(r))

    def test_a_pitch_envelope_on_noise_or_an_odd_chip_is_still_reported(self):
        inst = '\tInstrument Name="K"\n\t\tEnvelope Type="Pitch" Length="2" Values="9,9"\n'
        r, _ = load(build(inst, chan("Noise", '0 C1 1 Instrument="K"')))
        self.assertTrue(any("pitch envelope" in m for m in r.report.dropped))
        r, _ = load(build(inst.replace("Name", 'Expansion="N163" Name'), chan("N163Wave1", '0 C4 1 Instrument="K"'), expansions="N163"))
        self.assertTrue(any("pitch envelope" in m for m in r.report.dropped))

    def test_a_duty_envelope_that_changes_makes_a_note_per_duty(self):
        text = build('\tInstrument Name="D"\n\t\tEnvelope Type="DutyCycle" Length="4" Values="0,1,2,2"\n', chan("Square1", '0 C4 2 Instrument="D"'))
        _, imp = load(text)
        t = tune_of(imp)
        self.assertEqual([n.inst.pw for n in t.voices[0]], [12, 25, 50])
        self.assertEqual([n.dur for n in t.voices[0]], [4, 4, 40])

    def test_a_duty_sweep_still_becomes_a_pulse_width_sweep(self):
        text = build('\tInstrument Name="D"\n\t\tEnvelope Type="DutyCycle" Length="4" Loop="0" Values="0,0,2,2"\n', chan("Square1", '0 C4 2 Instrument="D"'))
        r, imp = load(text)
        self.assertGreater(imp.song.instruments["D"].pws_rate, 0)
        self.assertEqual(len(notes(tune_of(imp), 0)), 1)

    def test_the_noise_mode_envelope_steps_between_long_and_short(self):
        text = build('\tInstrument Name="N"\n\t\tEnvelope Type="DutyCycle" Length="3" Values="1,1,0"\n', chan("Noise", '0 C2 1 Instrument="N"'))
        _, imp = load(text)
        self.assertEqual([n.inst.nmode for n in tune_of(imp).voices[3]], ["short", "long"])

    def test_the_triangle_has_no_volume_events_when_simulated(self):
        text = build('\tInstrument Name="Z"\n\t\tEnvelope Type="Arpeggio" Length="2" Values="12,0"\n', chan("Triangle", '0 C3 1 Instrument="Z"'))
        _, imp = load(text)
        self.assertFalse([e for e in imp.song.tracks["V3"] if isinstance(e, sg.Volume)])

    def test_frames_shorter_than_a_tick_fall_back_to_the_fit_and_say_so(self):
        zig = ",".join(["15", "5"] * 4)
        text = build('\tInstrument Name="Z"\n\t\tEnvelope Type="Volume" Length="8" Loop="0" Values="%s"\n' % zig,
                     chan("Square1", '0 C4 2 Instrument="Z"')).replace('TempoMode="FamiTracker"', 'TempoMode="FamiStudio"')
        text = text.replace('FamiTrackerTempo="150" FamiTrackerSpeed="6"', 'NoteLength="30" Groove="30" GroovePaddingMode="Beginning"')
        r, imp = load(text)
        self.assertLess(imp.song.bpm, 40)
        self.assertEqual(len(imp.song.instruments), 1)
        self.assertIn("shorter than one tick", report_text(r))
        self.assertIn("does not fit an attack", report_text(r))

    def test_ntsc_and_pal_have_different_frame_lengths_in_ticks(self):
        zig = ",".join(["15", "5"] * 4)
        text = build('\tInstrument Name="Z"\n\t\tEnvelope Type="Volume" Length="8" Loop="0" Values="%s"\n' % zig, chan("Square1", '0 C4 1 Instrument="Z"'))
        ntsc = [n[1] for n in notes(tune_of(load(text, region="ntsc")[1]), 0)]
        pal = [n[1] for n in notes(tune_of(load(text, region="pal")[1]), 0)]
        self.assertEqual(sum(ntsc), 24)
        self.assertEqual(sum(pal), 24)
        self.assertNotEqual(len(ntsc), len(pal))                                   # fewer frames fit in the row on PAL

    def test_simulated_expansion_instruments_work_too(self):
        zig = ",".join(["15", "5"] * 4)
        text = build('\tInstrument Name="Sw" Expansion="VRC6"\n\t\tEnvelope Type="Volume" Length="8" Loop="0" Values="%s"\n' % zig,
                     chan("VRC6Saw", '0 C3 1 Instrument="Sw"'), expansions="VRC6")
        _, imp = load(text)
        t = tune_of(imp)
        self.assertTrue(len(t.voices[4]) > 4)
        self.assertTrue(all(n.inst.wave == "saw" for n in t.voices[4]))

    def test_the_simulated_song_renders_and_is_as_loud_as_the_fitted_one(self):
        # a decay the fit gets right, once as one note and once forced through the frame-by-frame path (a duty envelope that changes)
        decay = "15,14,13,12,11,10,9,8,7,6,5,4,3,2,1,0"
        fitted = build('\tInstrument Name="D"\n\t\tEnvelope Type="Volume" Length="16" Values="%s"\n' % decay, chan("Square1", '0 C4 4 Instrument="D"'))
        forced = fitted.replace('\t\tEnvelope Type="Volume"', '\t\tEnvelope Type="DutyCycle" Length="2" Values="2,1"\n\t\tEnvelope Type="Volume"')
        one, many = fam.load(fitted).songs[0], fam.load(forced).songs[0]
        self.assertEqual(len(notes(tune_of(one), 0)), 1)
        self.assertGreater(len(notes(tune_of(many), 0)), 10)
        loud_one, loud_many = np.abs(_render(one)).max(), np.abs(_render(many)).max()
        self.assertGreater(loud_one, 0.05)
        self.assertGreater(loud_many, 0.05)
        self.assertLess(abs(loud_one - loud_many), 0.25)


def _render(imp):
    from chipsynth.player import TunePlayer
    return TunePlayer(tune_of(imp)).render(8000)


class CliTests(unittest.TestCase):
    def test_import_writes_expansion_voices_into_the_package(self):
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "e.txt"
        src.write_text(build('\tInstrument Name="Sw" Expansion="VRC6"\n', chan("VRC6Saw", '0 C3 4 Instrument="Sw"'), expansions="VRC6"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["import", str(tmp / "p.csp"), str(src)])
        self.assertEqual(rc, 0, err.getvalue())
        pkg = pk.load(tmp / "p.csp")
        song = pkg.songs["e"]
        self.assertEqual(song.voices, 5)
        self.assertIn("V5", song.tracks)


if __name__ == "__main__":
    unittest.main()
