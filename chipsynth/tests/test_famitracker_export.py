# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writing FamiTracker text.  The check is a reader of our own, written from the format and as strict as FamiStudio's
FamiTracker-text reader is about where on a line things sit (header text, `ROW xx`, `ORDER xx`, the field counts): what
is written must read back, note for note and frame for frame.  The reader is ours; what the real FamiStudio does with these files
(pitch, timing, envelopes, release, loop) was checked by running its command line on them, and the tests marked "played in
FamiStudio" pin what that showed.  Nothing here was opened in FamiTracker itself."""
import re
import tempfile
import unittest
from pathlib import Path

from chipsynth import dpcm, exporter, mml, package as pk
from chipsynth.__main__ import main
from chipsynth.formats import famitracker_export as ex
from chipsynth.formats.envelope import FRAME_HZ, adsr
from chipsynth.formats.famistudio_text import PAL_HZ, noise_pitch
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
NOTE_NUMBER = {"C-": 0, "C#": 1, "D-": 2, "D#": 3, "E-": 4, "F-": 5, "F#": 6, "G-": 7, "G#": 8, "A-": 9, "A#": 10, "B-": 11}


# ------------------------------------------------------------------ the reader
class FtError(ValueError):
    pass


def quoted(rest):
    """The fields of a line with "quoted names" kept whole (FamiStudio's SplitStringKeepQuotes)."""
    out = []
    for i, piece in enumerate(rest.split('"')):
        out += piece.split() if i % 2 == 0 else [piece]
    return out


class FamiTrackerText:
    """What a FamiTracker text export says, read line by line the way FamiStudio reads it."""

    def __init__(self, text):
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if lines[0].strip()[:26] != "# FamiTracker text export " or lines[0].strip()[26:] != "0.4.2":
            raise FtError("header")
        self.title = self.author = self.copyright = ""
        self.machine = self.expansion = 0
        self.macros = {}                  # (type, idx) -> (loop, release, setting, [values])
        self.samples = {}                 # idx -> (name, size, bytes)
        self.insts = {}                   # idx -> dict(vol, arp, pit, duty, name)
        self.keys = []                    # (inst, octave, semitone, sample, pitch, loop)
        self.tracks = []
        self.keywords = []
        track = None
        cur = None
        pattern = None
        for n, raw in enumerate(lines[1:], 2):
            line = raw.strip()
            where = "line %d: %s" % (n, line[:60])
            try:
                if not line or line.startswith("#"):
                    continue
                self.keywords.append(line.split()[0])
                if line.startswith("TITLE"):
                    self.title = line[5:].strip(' "')
                elif line.startswith("AUTHOR"):
                    self.author = line[6:].strip(' "')
                elif line.startswith("COPYRIGHT"):
                    self.copyright = line[9:].strip(' "')
                elif line.split()[0] in ("COMMENT", "FRAMERATE", "VIBRATO", "SPLIT"):
                    pass                                  # FamiStudio's reader has no branch for these either
                elif line.startswith("EXPANSION"):
                    self.expansion = int(line[9:])
                elif line.startswith("MACHINE"):
                    self.machine = int(line[8:])
                elif line.startswith("MACRO"):
                    if line.startswith("MACROVRC6") or line.startswith("MACRON163"):
                        raise FtError("an expansion macro")
                    head, tail = line[line.index(" "):].split(":")
                    p = [int(x) for x in head.split()]
                    if len(p) != 5:
                        raise FtError("macro fields")
                    self.macros[(p[0], p[1])] = (p[2], p[3], p[4], [int(x) for x in tail.split()])
                elif line.startswith("DPCMDEF"):
                    p = quoted(line[7:])
                    cur = [p[2], int(p[1]), bytearray()]
                    self.samples[int(p[0])] = cur
                elif line.startswith("DPCM"):
                    cur[2] += bytes(int(s, 16) for s in line[6:].split())
                elif line.startswith("KEYDPCM"):
                    p = [int(x) for x in line[7:].split()]
                    self.keys.append(tuple(p[:6]))
                elif line.startswith("INST2A03"):
                    p = quoted(line[line.index(" "):])
                    self.insts[int(p[0])] = dict(vol=int(p[1]), arp=int(p[2]), pit=int(p[3]), hipit=int(p[4]), duty=int(p[5]),
                                                 name=p[-1])
                elif line.startswith("INST"):
                    raise FtError("an expansion instrument")
                elif line.startswith("TRACK"):
                    p = quoted(line[5:])
                    track = dict(rows=int(p[0]), speed=int(p[1]), tempo=int(p[2]), name=p[3], columns=None, orders=[], patterns={})
                    self.tracks.append(track)
                elif line.startswith("COLUMNS"):
                    track["columns"] = [int(x) for x in re.split("[ :]+", line[7:]) if x]
                    if len(track["columns"]) != 5:
                        raise FtError("columns")
                elif line.startswith("ORDER"):
                    idx = int(line[6:8], 16)
                    values = line[5:].split(":")[1].split()
                    if idx != len(track["orders"]) or len(values) != 5:
                        raise FtError("order")
                    track["orders"].append([int(v, 16) for v in values])
                elif line.startswith("PATTERN"):
                    pattern = int(line[8:], 16)
                    track["patterns"][pattern] = []
                elif line.startswith("ROW"):
                    chans = [c for c in line.split(":") if c]
                    if int(chans[0][4:6], 16) != len(track["patterns"][pattern]):
                        raise FtError("row number")
                    if len(chans) != 6:
                        raise FtError("channels in a row")
                    row = []
                    for ci in range(5):
                        f = chans[ci + 1].split()
                        if len(f) != 3 + track["columns"][ci] or len(f[0]) != 3 or len(f[1]) != 2 or len(f[2]) != 1 \
                                or any(len(x) != 3 for x in f[3:]):
                            raise FtError("cell %r" % chans[ci + 1])
                        row.append(f)
                    track["patterns"][pattern].append(row)
                else:
                    raise FtError("unknown line")
            except FtError as e:
                raise FtError("%s (%s)" % (e, where))
            except (ValueError, IndexError, KeyError, TypeError, AttributeError) as e:
                raise FtError("%s: %r" % (where, e))
        for t in self.tracks:
            for k, pat in t["patterns"].items():
                if len(pat) != t["rows"]:
                    raise FtError("pattern %02X has %d rows of %d" % (k, len(pat), t["rows"]))

    # ---- playing
    def row_frames(self, track, nrows):
        """The frame each of the first `nrows` rows starts on: FamiStudio's copy of FamiTracker's tempo counter."""
        pal = self.machine == 1
        dec, rem = divmod(track["tempo"] * 24, track["speed"])
        reload = 60 * (50 if pal else 60)
        counter, f, out = 0, 0, []
        while len(out) < nrows:
            if counter <= 0:
                out.append(f)
                counter += reload - rem
            counter -= dec
            f += 1
        return out

    def play(self, track=0):
        """One pass: ({channel: [(frame, text, instrument, volume)]}, rows played, the jump row's effect, the order jumped to)."""
        t = self.tracks[track]
        rows, ev, jump = [], {c: [] for c in range(5)}, None
        for k, order in enumerate(t["orders"]):
            for r in range(t["rows"]):
                rows.append((k, r))
                fx = {t["patterns"][order[c]][r][c][3] for c in range(5)}
                hit = [f for f in fx if f[0] in "BC"]
                if hit:
                    jump = hit[0]
                    break
            if jump:
                break
        frames = self.row_frames(t, len(rows))
        for i, (k, r) in enumerate(rows):
            for c in range(5):
                cell = t["patterns"][t["orders"][k][c]][r][c]
                if cell[0] != "...":
                    ev[c].append((frames[i], cell[0], cell[1], cell[2]))
        return ev, len(rows), jump, rows

    def hz(self):
        return PAL_HZ if self.machine == 1 else FRAME_HZ


def pitch_of(text, voice=2):
    """chipsynth pitch of 'C-4' on a voice.  The triangle's C-4 is chipsynth's C4 (pitch 48); a pulse's is an octave higher
    (FamiTracker's A-3 is 440 Hz on the pulse channels, checked by playing the file in FamiStudio), so pitch 60."""
    return int(text[2]) * 12 + NOTE_NUMBER[text[:2]] + (12 if voice < 2 else 0)


def tune_of(text=MML, **kw):
    return Song.from_mml(text).compile()


def read(text=MML, **kw):
    tune = tune_of(text)
    out, rep = ex.dump(tune, **kw)
    return tune, FamiTrackerText(out), out, rep


def seconds_per_tick(tune):
    return 60.0 / (tune.bpm * 96)


class Timing(unittest.TestCase):
    def assert_notes(self, tune, ft, voices=(0, 1, 2)):
        """Every note of the voices starts on its frame (within one frame and the tempo's rounding) with its pitch, and
        ends where the song ends it."""
        ev, nrows, jump, rows = ft.play()
        hz, t = ft.hz(), ft.tracks[0]
        base = 125.0 if ft.machine == 1 else 150.0
        grid = [g for g in ex.GRIDS if all(x % g == 0 for x in [tune.bar_ticks, tune.loop_tick, tune.length]
                                           + [v for vv in tune.voices[:4] for n in vv for v in (n.start, n.dur)])][0]
        want_row = 60.0 / (tune.bpm * 96) * grid
        real_row = base * t["speed"] / t["tempo"] / hz
        drift = abs(real_row / want_row - 1)
        for v in voices:
            got = [(f, c) for f, c, _, _ in ev[v] if c not in ("---", "===")]
            self.assertEqual(len(got), len(tune.voices[v]), "voice %d" % (v + 1))
            for (f, c), n in zip(got, tune.voices[v]):
                self.assertEqual(pitch_of(c, v), n.pitch, "voice %d pitch" % (v + 1))
                sec = n.start * seconds_per_tick(tune)
                self.assertLessEqual(abs(f / hz - sec), 1.6 / hz + drift * sec, "voice %d at %.3fs" % (v + 1, sec))
        return ev, hz, drift


class RoundTripTests(Timing):
    def test_every_melodic_channel_keeps_its_notes_pitch_and_timing(self):
        tune, ft, *_ = read()
        self.assert_notes(tune, ft)

    def test_the_row_length_is_landed_closely_at_several_tempos(self):
        for bpm in (60, 90, 100, 120, 133, 150, 177, 200):
            for pal in (False, True):
                tune = tune_of("tempo %d\nV1: l8 c d e f g a b >c\nV2: l4 c e g c\n" % bpm)
                _, ft, _, rep = read("tempo %d\nV1: l8 c d e f g a b >c\nV2: l4 c e g c\n" % bpm, region="pal" if pal else "ntsc")
                self.assertEqual(ft.machine, int(pal))
                self.assert_notes(tune, ft, (0, 1))

    def test_a_note_cut_comes_where_the_note_ends_and_not_between_joined_notes(self):
        tune, ft, *_ = read("tempo 120\nV1: l4 c d r e\n")
        ev, hz, _ = self.assert_notes(tune, ft, (0,))
        cuts = [f for f, c, _, _ in ev[0] if c in ("---", "===")]     # the lead has a release: its tail is let go with `===`
        self.assertLessEqual(len(cuts), 2)                   # after d, where the rest is (c runs into d; e ends the song): the release, then the cut
        d = tune.voices[0][1]
        self.assertAlmostEqual(cuts[0] / hz, (d.start + d.dur) * seconds_per_tick(tune), delta=1.6 / hz)

    def test_a_note_longer_than_a_pattern_is_written_once(self):
        tune, ft, text, _ = read("tempo 120\ntime 4/4\nV1: o4 c1 c1 c1 c1\n")
        ev, hz, _ = self.assert_notes(tune, ft, (0,))
        self.assertEqual(len([e for e in ev[0] if e[1] not in ("---", "===")]), 4)

    def test_the_song_is_as_long_as_the_tune(self):
        tune, ft, *_ = read()
        _, nrows, jump, _ = ft.play()
        want = tune.length // 24
        self.assertEqual(nrows, want)
        self.assertIsNotNone(jump)

    def test_noise_keeps_its_order_of_pitches(self):
        text = "V4: l8 o2 c o4 c o6 c o8 c\n"
        tune, ft, *_ = read(text)
        ev, *_ = ft.play()
        hexes = [int(c[0], 16) for _, c, _, _ in ev[3] if c != "---"]
        self.assertEqual(len(set(hexes)), 4)
        self.assertEqual(hexes, sorted(hexes))
        for (f, c, _, _), n in zip([e for e in ev[3] if e[1] != "---"], tune.voices[3]):
            self.assertTrue(re.fullmatch(r"[0-9A-F]-#", c), c)
            self.assertEqual(noise_pitch(int(c[0], 16)), min((noise_pitch(k) for k in range(16)), key=lambda p: abs(p - n.pitch)))

    def test_note_volumes_are_written_on_every_note_and_the_triangle_has_none(self):
        text = "V1: l4 v15 c v7 d v3 e\nV3: l4 v9 c d e\n"
        tune, ft, *_ = read(text)
        ev, *_ = ft.play()
        self.assertEqual([int(v, 16) for _, c, _, v in ev[0] if c != "---"], [n.vol for n in tune.voices[0]])
        self.assertEqual({v for _, c, _, v in ev[2] if c != "---"}, {"."})

    def test_instruments_are_named_by_index_and_the_row_uses_them(self):
        tune, ft, *_ = read()
        ev, *_ = ft.play()
        names = {i: d["name"] for i, d in ft.insts.items()}
        self.assertEqual(names[int(ev[0][0][2], 16)], "lead")
        self.assertEqual(names[int(ev[2][0][2], 16)], "bass")
        self.assertEqual(names[int(ev[3][0][2], 16)], "hat")


class LoopTests(Timing):
    def test_a_loop_jumps_to_the_pattern_the_loop_point_is_on(self):
        text = "time 4/4\ntempo 120\nV1: o4 c1 d1 loop e1 f1 g1\n"
        tune, ft, *_ = read(text)
        _, nrows, jump, rows = ft.play()
        self.assertEqual(jump[0], "B")
        order = int(jump[1:], 16)
        self.assertEqual(order * ft.tracks[0]["rows"] * 24, tune.loop_tick)
        self.assertEqual(rows[-1], (len(ft.tracks[0]["orders"]) - 1, (tune.length // 24 - 1) % ft.tracks[0]["rows"]))

    def test_a_song_with_no_loop_section_loops_from_the_start(self):
        _, ft, *_ = read("V1: l4 c d e f\n")
        self.assertEqual(ft.play()[2], "B00")

    def test_a_song_marked_once_halts(self):
        _, ft, *_ = read("once\nV1: l4 c d e f\n")
        self.assertEqual(ft.play()[2], "C00")

    def test_a_loop_point_in_a_short_bar_still_lands_on_the_first_row_of_a_pattern(self):
        tune, ft, _, rep = read("time 2/4\ntempo 120\nV1: l4 c d e f loop g a b >c c d e f\n")
        self.assertEqual(tune.loop_tick, 2 * 192)
        t = ft.tracks[0]
        _, _, jump, _ = ft.play()
        self.assertEqual(jump[0], "B")
        self.assertEqual(int(jump[1:], 16) * t["rows"] * 24, tune.loop_tick)
        self.assertFalse(any("loop point" in m for m in rep.approximated), rep.approximated)

    def test_repeated_music_shares_patterns(self):
        text = "time 4/4\nV1: o4 [c1 d1]4\nV3: o2 [c1 c1]4\n"
        tune, ft, *_ = read(text)
        t = ft.tracks[0]
        self.assertGreater(len(t["orders"]), len(t["patterns"]) - 0)          # more orders than distinct patterns
        self.assertLessEqual(len(t["patterns"]), 4)
        self.assert_notes(tune, ft, (0, 2))

    def test_the_song_is_cut_at_the_limit_of_128_patterns_and_says_so(self):
        text = "time 4/4\nV1: " + " ".join("o%d %s1" % (3 + i % 3, "cdefgab"[i % 7]) for i in range(140)) + "\n"
        tune = tune_of(text)
        out, rep = ex.dump(tune)
        ft = FamiTrackerText(out)
        self.assertLessEqual(len(ft.tracks[0]["orders"]), 128)
        self.assertLessEqual(len(ft.tracks[0]["patterns"]), 128)
        self.assertEqual(ft.play()[2][0], "B")
        self.assertTrue(any("128 patterns" in m for m in rep.dropped) or len(ft.tracks[0]["orders"]) * ft.tracks[0]["rows"] >= tune.length // 24,
                        rep.dropped)


class InstrumentTests(unittest.TestCase):
    def inst(self, ft, name):
        return next(d for d in ft.insts.values() if d["name"] == name)

    def test_the_volume_macro_is_the_adsr_sampled_a_frame_at_a_time(self):
        _, ft, *_ = read()
        d = self.inst(ft, "lead")
        loop, rel, setting, vals = ft.macros[(0, d["vol"])]
        import numpy as np
        gate = vals[:rel + 1]                                          # up to the sustain: what a held note plays
        t = (np.arange(len(gate)) + 0.5) / FRAME_HZ
        want = [int(round(x)) for x in adsr(0, 6, 8, 3, None, t) * 15]
        self.assertEqual(gate, want)
        self.assertEqual(gate[-1], 8)                                  # it ends on the sustain level
        self.assertEqual((loop, setting), (-1, 0))                     # no loop: with a release point FamiTracker holds the sustain value

    def test_the_release_is_a_tail_after_the_sustain_that_decays_like_chipsynths(self):
        _, ft, *_ = read()
        loop, rel, setting, vals = ft.macros[(0, self.inst(ft, "lead")["vol"])]
        tail = vals[rel + 1:]
        self.assertGreaterEqual(len(tail), 2)
        self.assertEqual(tail[-1], 0)
        self.assertEqual(tail, sorted(tail, reverse=True))

    def test_a_note_followed_by_a_rest_is_released_then_cut_and_a_legato_note_is_not(self):
        """Played in FamiStudio: a `===` cell plays the volume sequence's release part; the cut after it comes where the tail is silent."""
        _, ft, *_ = read("tempo 120\n[instr]\nl wave=pulse a=0 d=6 s=8 r=5\nV1: @l l4 c d r2\n")
        ev, _, _, _ = ft.play()
        cells = [c for _, c, _, _ in ev[0]]
        self.assertEqual(cells[0][:2], "C-")
        self.assertEqual(cells[1][:2], "D-")
        self.assertEqual(cells[2:], ["===", "---"])                   # c runs into d: no release between them

    def test_a_flat_full_volume_has_no_macro_and_the_triangle_never_has_one(self):
        _, ft, *_ = read("[instr]\nflat wave=pulse a=0 d=0 s=15 r=0\nV1: @flat c\nV3: c\n")
        self.assertEqual(self.inst(ft, "flat")["vol"], -1)
        tri = [d for d in ft.insts.values() if d["name"] != "flat"][0]
        self.assertEqual(tri["vol"], -1)

    def test_duty_comes_from_the_pulse_width_and_noise_mode(self):
        _, ft, *_ = read()
        self.assertEqual(ft.macros[(4, self.inst(ft, "lead")["duty"])][3], [1])     # 25%
        self.assertEqual(ft.macros[(4, self.inst(ft, "hat")["duty"])][3], [1])      # the short (metallic) noise
        _, ft, *_ = read("[instr]\nn wave=noise nmode=long\nV4: @n c\n")
        self.assertEqual(ft.macros[(4, self.inst(ft, "n")["duty"])][3], [0])

    def test_a_pulse_width_the_nes_does_not_have_is_approximated(self):
        _, _, _, rep = read("[instr]\nw wave=pulse pw=40\nV1: @w c d e f\n")
        self.assertTrue(any("pulse width" in m for m in rep.approximated))

    def test_the_arpeggio_is_a_looping_macro_of_semitone_offsets(self):
        _, ft, *_ = read("tempo 120\n[instr]\na wave=pulse arp=0,4,7 arprate=8\nV1: @a c1\n")
        d = self.inst(ft, "a")
        loop, rel, setting, vals = ft.macros[(1, d["arp"])]
        self.assertEqual((loop, setting), (0, 0))
        self.assertEqual(sorted(set(vals)), [0, 4, 7])
        steps = [vals[0]] + [b for a, b in zip(vals, vals[1:]) if a != b]
        self.assertEqual(steps[:3], [0, 4, 7])
        frames_per_step = vals.index(4)
        self.assertAlmostEqual(frames_per_step / FRAME_HZ, 60.0 / 120 / 8, delta=1.0 / FRAME_HZ)

    def test_a_pulse_width_sweep_becomes_a_duty_macro(self):
        _, ft, _, rep = read("[instr]\ns wave=pulse pws=12,75,4\nV1: @s c1\n")
        loop, _, _, vals = ft.macros[(4, self.inst(ft, "s")["duty"])]
        self.assertEqual(loop, 0)
        self.assertGreater(len(set(vals)), 1)
        self.assertTrue(all(0 <= v <= 3 for v in vals))
        self.assertTrue(any("pulse-width sweep" in m for m in rep.approximated))

    def test_instruments_that_sound_alike_are_one_instrument(self):
        _, ft, *_ = read("[instr]\na wave=pulse pw=50\nb wave=pulse pw=50\nV1: @a c d\nV2: @b c d\n")
        self.assertEqual(len(ft.insts), 1)

    def test_the_same_instrument_on_pulse_and_noise_is_two_instruments(self):
        _, ft, *_ = read("[instr]\nx wave=pulse\nV1: @x c\nV4: @x c\n")
        self.assertEqual(len(ft.insts), 2)

    def test_what_is_dropped_is_said(self):
        text = "[instr]\nv wave=pulse vib=8,6 slide=4 filt=lp\nV1: @v c d e f\n"
        try:
            tune = tune_of(text)
        except mml.MMLError:
            self.skipTest("instrument spelling differs")
        _, rep = ex.dump(tune)
        self.assertTrue(any("vibrato" in m for m in rep.dropped), rep.dropped)

    def test_the_release_is_said_once_for_all_instruments(self):
        _, _, _, rep = read()
        self.assertEqual(len([m for m in rep.approximated if "release" in m]), 1)


class ReportTests(unittest.TestCase):
    def test_voices_past_the_fourth_are_left_out(self):
        text = "voices 6\n" + "\n".join("V%d: l4 c d e f" % i for i in range(1, 7)) + "\n"
        _, rep = ex.dump(tune_of(text))
        self.assertTrue(any("V5" in m and "V6" in m for m in rep.dropped))

    def test_tempo_changes_are_dropped(self):
        _, rep = ex.dump(tune_of("V1: l4 c d t90 e f\n"))
        self.assertTrue(any("tempo changes" in m for m in rep.dropped))

    def test_echo_and_reverb_are_dropped(self):
        _, rep = ex.dump(tune_of("echo 240ms,40,30\nV1: l4 c d e f\n"))
        self.assertTrue(any("echo" in m for m in rep.dropped))

    def test_a_wave_the_channel_cannot_make_is_said(self):
        _, rep = ex.dump(tune_of("[instr]\ns wave=saw\nV1: @s c\n"))
        self.assertTrue(any("saw" in m for m in rep.approximated), rep.approximated)

    def test_the_header_says_what_was_written(self):
        _, _, _, rep = read(region="pal")
        self.assertTrue(any("PAL" in m and "speed" in m for m in rep.notes))

    def test_a_clean_song_has_nothing_left_out(self):
        _, ft, _, rep = read("[instr]\nx wave=pulse a=0 d=0 s=15 r=0\nV1: @x l4 c d e f\n")
        self.assertEqual(rep.dropped, [])


class SpeedTempoTests(unittest.TestCase):
    def test_a_row_of_a_whole_number_of_frames_is_exact(self):
        for frames in (1, 3, 6, 7.5, 12):
            s, t, real = ex.speed_and_tempo(frames, False)
            self.assertAlmostEqual(real, frames, places=6, msg=frames)
            self.assertTrue(1 <= s <= 31 and 32 <= t <= 255)

    def test_pal_uses_its_own_base_tempo(self):
        s, t, real = ex.speed_and_tempo(6, True)
        self.assertAlmostEqual(real, 6)
        self.assertEqual(125 * s / t, 6)

    def test_the_error_is_small_over_the_useful_range(self):
        for tenth in range(10, 400):
            frames = tenth / 10
            _, _, real = ex.speed_and_tempo(frames, False)
            self.assertLess(abs(real - frames) / frames, 0.01, frames)

    def test_a_row_shorter_than_a_frame_is_played_at_a_frame(self):
        _, t, real = ex.speed_and_tempo(1.0, False)
        self.assertEqual(real, 1.0)


class DpcmTests(unittest.TestCase):
    def tune(self):
        a = dpcm.Sample(name="kick", rate=12, tracks=[[bytes.fromhex("aa55aa55")]])
        b = dpcm.Sample(name="snare", rate=9, tracks=[[bytes(range(1, 40))]])
        lib = dict(mml.builtin_library(), _mem={"kick": a, "snare": b})
        return Song.from_mml("[sample]\nkick\nsnare\n\nV1: c\nD1: kick r2 snare r2 kick\n", lib).compile(lib)

    def test_samples_keys_and_notes_read_back(self):
        tune = self.tune()
        text, rep = ex.dump(tune)
        ft = FamiTrackerText(text)
        self.assertEqual({k: (v[0], v[1], bytes(v[2])) for k, v in ft.samples.items()},
                         {0: ("kick", 4, bytes.fromhex("aa55aa55")), 1: ("snare", 39, bytes(range(1, 40)))})
        keys = {(o * 12 + s): (smp, pit, loop) for _, o, s, smp, pit, loop in ft.keys}
        self.assertEqual(keys, {12: (0, 12, 0), 13: (1, 9, 0)})          # C-1 and C#-1 (key numbers count from C-0)
        dp = ft.insts[ft.keys[0][0]]
        self.assertEqual((dp["name"], dp["vol"], dp["duty"]), ("DPCM", -1, -1))
        ev, hz, = ft.play()[0][4], ft.hz()
        got = [(round(f / hz, 3), c, i) for f, c, i, _ in ev]
        self.assertEqual([c for _, c, _ in got], ["C-1", "C#1", "C-1"])
        self.assertEqual({i for _, _, i in got}, {"%02X" % ft.keys[0][0]})
        for (sec, _, _), e in zip(got, [e for e in tune.dac[0] if e[1] == "play"]):
            self.assertAlmostEqual(sec, e[0] * seconds_per_tick(tune), delta=1.6 / hz)

    def test_a_long_sample_is_split_over_lines_and_still_adds_up(self):
        smp = dpcm.Sample(name="big", rate=15, tracks=[[bytes(i % 256 for i in range(200))]])
        lib = dict(mml.builtin_library(), _mem={"big": smp})
        tune = Song.from_mml("[sample]\nbig\n\nV1: c\nD1: big\n", lib).compile(lib)
        text, _ = ex.dump(tune)
        self.assertGreater(len([x for x in text.split("\n") if x.startswith("DPCM :")]), 1)
        self.assertEqual(bytes(FamiTrackerText(text).samples[0][2]), bytes(i % 256 for i in range(200)))

    def test_a_second_dac_is_left_out(self):
        smp = dpcm.Sample(name="k", rate=15, tracks=[[bytes(16)]])
        lib = dict(mml.builtin_library(), _mem={"k": smp})
        tune = Song.from_mml("[sample]\nk\n\nV1: c\nD1: k\nD2: k\n", lib).compile(lib)
        _, rep = ex.dump(tune)
        self.assertTrue(any("D2" in m for m in rep.dropped))

    def test_no_samples_means_no_dpcm_section_or_instrument(self):
        text, _ = ex.dump(tune_of())
        self.assertNotIn("DPCMDEF", text)
        self.assertNotIn("KEYDPCM", text)


class FileTests(unittest.TestCase):
    def test_the_file_is_what_famitracker_and_famistudio_expect_of_a_text_export(self):
        text, _ = ex.dump(tune_of(), name="song")
        lines = text.split("\n")
        self.assertEqual(lines[0], "# FamiTracker text export 0.4.2")
        self.assertTrue(text.endswith("# End of export\n"))
        self.assertNotIn("\r", text)
        ft = FamiTrackerText(text)
        self.assertEqual(ft.expansion, 0)
        self.assertEqual(len(ft.tracks), 1)
        # every keyword the FamiStudio reader and FamiTracker know, and no other
        known = {"TITLE", "AUTHOR", "COPYRIGHT", "COMMENT", "MACHINE", "FRAMERATE", "EXPANSION", "VIBRATO", "SPLIT", "MACRO",
                 "INST2A03", "TRACK", "COLUMNS", "ORDER", "PATTERN", "ROW"}
        self.assertLessEqual(set(ft.keywords), known)

    def test_names_cannot_break_a_line(self):
        tune = tune_of('title He said "hi"\nauthor A\\nB\nV1: c d\n')
        text, _ = ex.dump(tune)
        ft = FamiTrackerText(text)
        self.assertNotIn('"', ft.title)
        self.assertEqual(len(ft.tracks), 1)

    def test_titles_are_cut_to_famitrackers_32_characters(self):
        text, _ = ex.dump(tune_of("title " + "x" * 50 + "\nV1: c d\n"))
        self.assertEqual(len(FamiTrackerText(text).title), 32)

    def test_pal_sets_the_machine(self):
        text, _ = ex.dump(tune_of(), region="pal")
        self.assertEqual(FamiTrackerText(text).machine, 1)

    def test_an_empty_song_is_a_valid_file(self):
        text, _ = ex.dump(tune_of("tempo 120\nV1: r1\n"))
        ft = FamiTrackerText(text)
        self.assertEqual(ft.play()[2][0], "B")

    def test_the_reader_is_strict_about_the_things_famistudio_is(self):
        text, _ = ex.dump(tune_of())
        for bad in (text.replace("0.4.2", "0.4.3", 1), text.replace("ROW 01", "ROW 02", 1), text.replace("ORDER 00", "ORDER 01", 1),
                    text.replace("COLUMNS : 1 1 1 1 1", "COLUMNS : 1 1 1 1"), text.replace("INST2A03", "INSTVRC7", 1)):
            with self.assertRaises(FtError):
                FamiTrackerText(bad)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="t")
        pkg.add_song("tune", Song.from_mml(MML))
        pk.save(pkg, self.tmp / "t.csp")

    def run_cli(self, *args):
        return main(["export-song", str(self.tmp / "t.csp"), *args])

    def test_the_format_flag_writes_famitracker_text(self):
        self.assertEqual(self.run_cli("-o", str(self.tmp / "a.txt"), "-f", "famitracker"), 0)
        FamiTrackerText((self.tmp / "a.txt").read_text(encoding="utf-8"))

    def test_the_ending_ftm_txt_picks_famitracker_and_a_plain_txt_stays_famistudio(self):
        self.assertEqual(exporter.format_of("x.ftm.txt"), "famitracker")
        self.assertEqual(exporter.format_of("X.FTM.TXT"), "famitracker")
        self.assertEqual(exporter.format_of("x.txt"), "famistudio")
        self.assertEqual(self.run_cli("-o", str(self.tmp / "b.ftm.txt")), 0)
        self.assertTrue((self.tmp / "b.ftm.txt").read_text(encoding="utf-8").startswith("# FamiTracker text export 0.4.2"))
        self.assertEqual(self.run_cli("-o", str(self.tmp / "c.txt")), 0)
        self.assertTrue((self.tmp / "c.txt").read_text(encoding="utf-8").startswith("Project"))

    def test_region_pal_reaches_the_file_and_the_report_is_printed(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_cli("-o", str(self.tmp / "p.ftm.txt"), "--region", "pal"), 0)
        self.assertEqual(FamiTrackerText((self.tmp / "p.ftm.txt").read_text(encoding="utf-8")).machine, 1)
        self.assertIn("PAL", err.getvalue())

    def test_audio_options_do_not_apply(self):
        self.assertEqual(self.run_cli("-o", str(self.tmp / "d.ftm.txt"), "--volume", "0.5"), 1)

    def test_a_song_with_no_title_is_named_by_its_id(self):
        self.run_cli("-o", str(self.tmp / "e.ftm.txt"))
        self.assertEqual(FamiTrackerText((self.tmp / "e.ftm.txt").read_text(encoding="utf-8")).title, "tune")


class ComposerTests(tcf.Base):
    def test_export_famitracker_text_from_the_file_menu_code(self):
        a = self.a
        self.assertTrue(a.export_as("famitracker", self.dir / "o.ftm.txt", region="pal"))
        ft = FamiTrackerText((self.dir / "o.ftm.txt").read_text(encoding="utf-8"))
        self.assertEqual(ft.machine, 1)
        self.assertTrue(any(x.startswith("left out:") and "echo" in x for x in a.last_report[1]), a.last_report[1])
        self.assertIn("PAL timing", " ".join(a.last_report[1]))
        self.assertFalse(any(x.startswith("warning:") for x in a.last_report[1]))

    def test_the_menu_lists_it(self):
        labels = [i["label"] for i in self.a.submenu_items("export")]
        self.assertTrue(any(x.startswith("FamiTracker text") for x in labels), labels)

    def test_a_name_without_an_ending_gets_ftm_txt(self):
        a = self.a
        self.assertTrue(a.export_as("famitracker", self.dir / "noending"))
        self.assertTrue((self.dir / "noending.ftm.txt").exists())


if __name__ == "__main__":
    unittest.main()
