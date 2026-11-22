# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standard MIDI file export and import (PLAN-CHIPSYNTH.md, C7)."""
import struct
import tempfile
import unittest
from pathlib import Path

from chipsynth import midi, musicgen, package as pk
from chipsynth.song import Song
from chipsynth.__main__ import main


def _chord_file(pitches, length=96):
    """A one-track, one-channel MIDI file playing all `pitches` together for `length` ticks (PPQ 96)."""
    def vlq(n):
        out = [n & 0x7F]
        while n >> 7:
            n >>= 7
            out.append(0x80 | (n & 0x7F))
        return bytes(reversed(out))
    body = b"".join(b"\x00\x90" + bytes([p, 100]) for p in pitches)
    body += vlq(length) + b"\x80" + bytes([pitches[0], 0])
    body += b"".join(b"\x00\x80" + bytes([p, 0]) for p in pitches[1:])
    body += b"\x00\xff\x2f\x00"
    return b"MThd" + struct.pack(">IHHH", 6, 0, 1, 96) + b"MTrk" + struct.pack(">I", len(body)) + body


class ChordSplitTests(unittest.TestCase):
    def test_a_chord_on_one_channel_spreads_over_voices(self):
        song = midi.from_midi(_chord_file([60, 64, 67]))
        self.assertEqual(song.voices, 3)
        tune = song.compile()
        self.assertEqual(sorted(notes(tune, v)[0][2] for v in range(3)), [60, 64, 67])
        self.assertEqual(notes(tune, 0)[0][2], 67)          # the top note stays in the first voice

    def test_with_fewer_voices_the_extra_chord_notes_have_nowhere_to_go(self):
        song = midi.from_midi(_chord_file([60, 64, 67]), voices=2)
        self.assertEqual(song.voices, 2)
        self.assertTrue(any("left out" in w for w in song.warnings))


def notes(tune, v):
    return [(n.start, n.dur, n.pitch) for n in tune.voices[v]]


class MidiTests(unittest.TestCase):
    TEXT = "tempo 140\ntime 3/4\nV1: o4 c4 d8 e8 r4 g4 >c2\nV2: o3 c1 g1\nV3: o2 c4 r2 c4\n"

    def test_round_trip_keeps_notes_tempo_and_time(self):
        t = Song.from_mml(self.TEXT).compile()
        back = midi.from_midi(midi.to_midi(t)).compile()
        for v in range(3):
            self.assertEqual(notes(back, v), notes(t, v))
        self.assertEqual((back.bpm, back.time), (140, (3, 4)))

    def test_tempo_changes_survive(self):
        t = Song.from_mml("tempo 100\nV1: o4 c1 d1 e1\n").compile()
        t.tempo_events = [(384, 150), (768, 90)]
        back = midi.from_midi(midi.to_midi(t)).compile()
        self.assertEqual(back.tempo_events, [(384, 150), (768, 90)])
        self.assertEqual(notes(back, 0), notes(t, 0))

    def test_generated_songs_round_trip_on_the_grid(self):
        for kind in ("town", "fight"):
            t = Song.from_mml(musicgen.generate(kind, 1)).compile()
            back = midi.from_midi(midi.to_midi(t)).compile()
            for v in range(3):
                self.assertEqual([n[2] for n in notes(back, v)][:20], [n[2] for n in notes(t, v)][:20])
            Song.from_mml(midi.from_midi(midi.to_midi(t)).to_mml())            # and what it writes parses

    def test_overlap_keeps_the_higher_note_and_cuts_the_lower(self):
        ev = lambda t, k, p, c: (t, k, p, 100, c)
        notes_ = [(0, 96, 60), (48, 144, 64)]
        out = midi._voice_events([(s, e, p) for s, e, p in notes_], lambda t: t, [])
        names = [type(e).__name__ for e in out]
        self.assertEqual(names.count("Note"), 2)
        song = Song()
        song.tracks["V1"] = out
        got = notes(song.compile(), 0)
        self.assertEqual(got, [(0, 48, 60), (48, 96, 64)])

    @staticmethod
    def file(channels, lens=None, start=None, drums=True):
        """A format-1 file with one track per channel: channel ch plays note 60+ch for lens[ch] quarters from start[ch]."""
        def trk(ev):
            body = b"".join(midi._varlen(d) + b for d, b in ev) + b"\x00\xff\x2f\x00"
            return midi._chunk(b"MTrk", body)
        tracks = []
        for ch in channels:
            a = (start or {}).get(ch, 0)
            n = (lens or {}).get(ch, 1)
            tracks.append(trk([(96 * a, bytes([0x90 | ch, 60 + ch, 90])), (96 * n, bytes([0x80 | ch, 60 + ch, 0]))]))
        return midi._chunk(b"MThd", struct.pack(">HHH", 1, len(tracks), 96)) + b"".join(tracks)

    def test_every_part_becomes_a_voice(self):
        song = midi.from_midi(self.file((0, 1, 2, 3, 4, 9)))
        self.assertEqual(song.voices, 5)
        self.assertEqual(sorted(k for k, v in song.tracks.items() if v), ["V1", "V2", "V3", "V4", "V5"])
        self.assertFalse(any("most important" in w for w in song.warnings))
        self.assertTrue(any("drum" in w for w in song.warnings))
        self.assertEqual(len(song.compile().voices), 5)

    def test_asking_for_four_voices_keeps_the_most_important_and_folds_the_rest_into_gaps(self):
        # parts 0-3 play a quarter note at bar 1; part 4 plays at beat 3 (a gap in every kept voice); part 5 collides
        data = self.file((0, 1, 2, 3, 4, 5), lens={0: 8, 1: 7, 2: 6, 3: 5, 4: 1, 5: 1}, start={4: 9, 5: 0})
        song = midi.from_midi(data, voices=4)
        self.assertEqual(song.voices, 4)
        w = [w for w in song.warnings if "most important" in w]
        self.assertEqual(len(w), 1)
        self.assertIn("1 of their 2 notes fit", w[0])
        tune = song.compile()
        self.assertEqual(sum(len(v) for v in tune.voices), 5)           # 4 kept parts + the one that fitted
        self.assertEqual({n.pitch for v in tune.voices for n in v}, {60, 61, 62, 63, 64})

    def test_the_longest_sounding_part_outranks_a_short_one(self):
        parts = [[(0, 96, 80)], [(0, 960, 40)], [(0, 480, 60)]]
        self.assertEqual(midi.rank_parts(parts), [1, 2, 0])

    def test_more_than_sixteen_parts_keep_sixteen(self):
        song = midi.from_midi(self.file(list(range(9)) + [10, 11, 12, 13, 14, 15] + [0, 1, 2]))
        self.assertEqual(song.voices, 16)
        self.assertTrue(any("most important" in w for w in song.warnings))

    def test_a_requested_count_larger_than_the_parts_is_just_the_parts(self):
        self.assertEqual(midi.from_midi(self.file((0, 1)), voices=8).voices, 2)
        self.assertEqual(midi.from_midi(self.file((0, 1, 2)), voices=0).voices, 1)

    def test_the_song_round_trips_through_mml_and_the_package(self):
        from chipsynth import package as pk
        song = midi.from_midi(self.file((0, 1, 2, 3, 4, 5)))
        again = Song.from_mml(song.to_mml())
        self.assertEqual(again.voices, 6)
        self.assertEqual(pk.decode_song(pk.encode_song(song)).voices, 6)

    def test_running_status_and_note_on_with_zero_velocity(self):
        body = (midi._varlen(0) + bytes([0x90, 60, 90]) + midi._varlen(96) + bytes([60, 0])
                + midi._varlen(0) + bytes([62, 90]) + midi._varlen(96) + bytes([62, 0]) + b"\x00\xff\x2f\x00")
        data = midi._chunk(b"MThd", struct.pack(">HHH", 0, 1, 96)) + midi._chunk(b"MTrk", body)
        got = notes(midi.from_midi(data).compile(), 0)
        self.assertEqual(got, [(0, 96, 60), (96, 96, 62)])

    def test_other_resolutions_are_scaled(self):
        body = midi._varlen(0) + bytes([0x90, 60, 90]) + midi._varlen(480) + bytes([0x80, 60, 0]) + b"\x00\xff\x2f\x00"
        data = midi._chunk(b"MThd", struct.pack(">HHH", 0, 1, 480)) + midi._chunk(b"MTrk", body)
        self.assertEqual(notes(midi.from_midi(data).compile(), 0), [(0, 96, 60)])

    def test_bad_files(self):
        for bad in (b"", b"RIFFxxxx", midi._chunk(b"MThd", struct.pack(">HHH", 0, 1, 0x8000 | 25)),
                    midi._chunk(b"MThd", struct.pack(">HHH", 0, 1, 96)) + b"MTrk\x00\x00\x00\x02\x00\x90"):
            with self.assertRaises(midi.MidiError):
                midi.from_midi(bad)
        empty = midi._chunk(b"MThd", struct.pack(">HHH", 0, 1, 96)) + midi._chunk(b"MTrk", b"\x00\xff\x2f\x00")
        with self.assertRaises(midi.MidiError):
            midi.from_midi(empty)

    def test_command_line_import_and_export(self):
        d = Path(tempfile.mkdtemp())
        (d / "tune.mml").write_text(self.TEXT)
        pkgfile = d / "p.csp"
        self.assertEqual(main(["import", str(pkgfile), str(d / "tune.mml")]), 0)
        self.assertEqual(main(["export", str(pkgfile), "-d", str(d / "out"), "--midi"]), 0)
        mid = d / "out" / "tune.mid"
        self.assertTrue(mid.is_file())
        self.assertEqual(main(["import", str(d / "q.csp"), str(mid)]), 0)
        self.assertEqual(notes(pk.load(d / "q.csp").tune("tune"), 0), notes(Song.from_mml(self.TEXT).compile(), 0))
        (d / "bad.mid").write_bytes(b"nope")
        self.assertEqual(main(["import", str(d / "r.csp"), str(d / "bad.mid")]), 1)


if __name__ == "__main__":
    unittest.main()


class ManyVoiceFileTests(unittest.TestCase):
    def test_writing_a_big_song_never_uses_the_drum_channel(self):
        text = "voices 12\n" + "\n".join("V%d: c" % i for i in range(1, 13)) + "\n"
        data = midi.to_midi(Song.from_mml(text).compile())
        _, tracks = midi.parse(data)
        chans = {ev[4] for t in tracks for ev in t if ev[1] == "on"}
        self.assertEqual(len(chans), 12)
        self.assertNotIn(9, chans)
        self.assertEqual(midi.from_midi(data).voices, 12)


def drum_file(hits, melody=None, ppq=96):
    """A format-0 file built by hand: channel 10 drum hits [(tick, GM note, velocity)] (each a 12-tick note, ended by a
    note-on with velocity 0) and, when `melody` is given, [(tick, pitch, length)] on channel 1."""
    ev = []
    for tick, note, vel in hits:
        ev += [(tick, 1, bytes([0x99, note, vel])), (tick + 12, 0, bytes([0x99, note, 0]))]
    for tick, pitch, length in melody or []:
        ev += [(tick, 1, bytes([0x90, pitch, 90])), (tick + length, 0, bytes([0x80, pitch, 0]))]
    body, last = b"", 0
    for tick, _, data in sorted(ev, key=lambda e: (e[0], e[1])):
        body += midi._varlen(tick - last) + data
        last = tick
    body += b"\x00\xff\x2f\x00"
    return midi._chunk(b"MThd", struct.pack(">HHH", 0, 1, ppq)) + midi._chunk(b"MTrk", body)


def plays(tune):
    """[(lane, tick, sample name, volume)] of a compiled tune's DAC lanes."""
    return [(k + 1, t, smp.name, vv[0]) for k, lane in enumerate(tune.dac) for t, kind, smp, vv in lane if kind == "play"]


class DrumImportTests(unittest.TestCase):
    def test_gm_notes_land_on_the_kit_samples_on_the_generators_lanes(self):
        hits = [(0, 36, 127), (0, 42, 64), (96, 38, 100), (192, 46, 100), (288, 49, 100), (288, 47, 100)]
        song = midi.from_midi(drum_file(hits, melody=[(0, 60, 96)]))
        self.assertEqual(song.warnings, [])
        self.assertEqual(sorted(plays(song.compile())), sorted([
            (1, 0, "kit_kick", 15), (2, 0, "kit_hat", 8), (1, 96, "kit_snare", 12), (2, 192, "kit_ohat", 12),
            (2, 288, "kit_crash", 12), (1, 288, "kit_tom", 12)]))
        self.assertEqual(song.voices, 1)
        self.assertEqual(set(song.samples), {"kit_kick", "kit_hat", "kit_snare", "kit_ohat", "kit_crash", "kit_tom"})

    def test_every_mapped_note_name(self):
        for note, key in midi.GM_DRUMS.items():
            song = midi.from_midi(drum_file([(0, note, 100)]))
            self.assertEqual([p[2] for p in plays(song.compile())], [musicgen.DAC_SAMPLE[key]], note)

    def test_velocity_is_volume(self):
        vols = [plays(midi.from_midi(drum_file([(0, 38, vel)])).compile())[0][3] for vel in (1, 20, 64, 100, 127)]
        self.assertEqual(vols, [1, 2, 8, 12, 15])

    def test_times_are_rounded_to_the_grid_for_any_division(self):
        song = midi.from_midi(drum_file([(0, 36, 100), (480, 38, 100), (500, 42, 100)], ppq=480))
        self.assertEqual(sorted(plays(song.compile())), [(1, 0, "kit_kick", 12), (1, 96, "kit_snare", 12), (2, 96, "kit_hat", 12)])

    def test_overlapping_hits_split_across_the_lanes_by_priority(self):
        # kick + snare together: the kick keeps D1 and the snare moves to D2; add a crash and, with only two lanes
        # (drum_lanes=2), the snare has no lane left (the default "auto" gives it D3: see DrumLaneTests)
        song = midi.from_midi(drum_file([(0, 36, 100), (0, 38, 100), (96, 36, 100), (96, 38, 100), (96, 49, 100)]), drum_lanes=2)
        self.assertEqual(sorted(plays(song.compile())),
                         [(1, 0, "kit_kick", 12), (1, 96, "kit_kick", 12), (2, 0, "kit_snare", 12), (2, 96, "kit_crash", 12)])
        self.assertEqual(len(song.warnings), 1)
        self.assertIn("did not fit", song.warnings[0])
        self.assertIn("snare x1", song.warnings[0])

    def test_the_same_drum_twice_at_once_plays_once_with_the_louder_velocity(self):
        song = midi.from_midi(drum_file([(0, 42, 50), (0, 44, 90), (96, 49, 80), (96, 57, 100)]))
        self.assertEqual(sorted(plays(song.compile())), [(2, 0, "kit_hat", 11), (2, 96, "kit_crash", 12)])
        self.assertEqual(song.warnings, [])

    def test_unmappable_percussion_is_reported_not_dropped_silently(self):
        song = midi.from_midi(drum_file([(0, 36, 100), (96, 56, 100), (192, 56, 100), (288, 69, 100), (384, 35, 100)]))
        self.assertEqual([p[2] for p in sorted(plays(song.compile()), key=lambda p: p[1])], ["kit_kick", "kit_kick"])
        self.assertEqual(len(song.warnings), 1)
        w = song.warnings[0]
        self.assertIn("drum", w)
        self.assertIn("Cowbell (note 56) x2", w)
        self.assertIn("Cabasa (note 69) x1", w)

    def test_a_file_of_only_unmappable_drums_is_refused_with_the_reason(self):
        with self.assertRaises(midi.MidiError) as cm:
            midi.from_midi(drum_file([(0, 56, 100)]))
        self.assertIn("drum kit", str(cm.exception))

    def test_a_drums_only_file_is_a_song_with_an_empty_voice(self):
        song = midi.from_midi(drum_file([(0, 36, 100), (96, 38, 100), (192, 42, 100)]))
        self.assertEqual(song.voices, 1)
        self.assertEqual(song.tracks["V1"], [])
        text = song.to_mml()
        self.assertIn("D1:", text)
        self.assertIn("D2:", text)
        self.assertIn("[sample]", text)
        again = Song.from_mml(text)                          # the MML it writes reads back to the same drums
        self.assertEqual(plays(again.compile()), plays(song.compile()))

    def test_note_offs_and_other_channels_do_not_make_hits(self):
        # a note-on with velocity 0 is an off, and a note on channel 1 with a drum number is a pitch
        song = midi.from_midi(drum_file([(0, 38, 100)], melody=[(0, 36, 96)]))
        self.assertEqual(len(plays(song.compile())), 1)
        self.assertEqual(notes(song.compile(), 0), [(0, 96, 36)])

    def test_files_without_percussion_come_out_as_before(self):
        song = midi.from_midi(MidiTests.file((0, 1, 2), lens={1: 2}, start={2: 1}))
        self.assertEqual(song.warnings, [])
        self.assertEqual((song.voices, song.dacs), (3, 2))
        self.assertEqual(song.samples, {})
        self.assertNotIn("D1:", song.to_mml())
        self.assertNotIn("[sample]", song.to_mml())

    def test_command_line_import_reports_and_keeps_the_drums(self):
        import contextlib
        import io
        d = Path(tempfile.mkdtemp())
        (d / "beat.mid").write_bytes(drum_file([(0, 36, 100), (0, 56, 100), (48, 42, 100)], melody=[(0, 60, 96)]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["import", str(d / "p.csp"), str(d / "beat.mid")]), 0)
        self.assertIn("warning:", out.getvalue())
        self.assertIn("Cowbell", out.getvalue())
        tune = pk.load(d / "p.csp").tune("beat")
        self.assertEqual(sorted(plays(tune)), [(1, 0, "kit_kick", 12), (2, 48, "kit_hat", 12)])


class DrumExportTests(unittest.TestCase):
    @staticmethod
    def drums_of(data):
        _, tracks = midi.parse(data)
        return sorted((tick, a, b) for t in tracks for tick, kind, a, b, ch in t if kind == "on" and ch == 9)

    def test_d1_d2_hits_are_channel_10_notes(self):
        text = ("[sample]\nkit_kick = kit_kick\nkit_hat = kit_hat\nkit_tom = kit_tom\n"
                "V1: c1\nD1: v15 kit_kick4 v8 kit_tom4 r2\nD2: v15 r4 kit_hat4\n")
        data = midi.to_midi(Song.from_mml(text).compile())
        self.assertEqual(self.drums_of(data), [(0, 36, 127), (96, 42, 127), (96, 47, 68)])

    def test_no_drum_track_without_hits_so_plain_songs_are_unchanged(self):
        tune = Song.from_mml(MidiTests.TEXT).compile()
        _, tracks = midi.parse(midi.to_midi(tune))
        self.assertEqual(len(tracks), 4)                             # tempo track + 3 voices
        self.assertEqual(midi.drum_hits(tune), ([], []))

    def test_samples_without_a_gm_note_are_reported_on_export(self):
        text = "[sample]\nkit_kick = kit_kick\nkit_hey = kit_hey\nV1: c1\nD1: kit_kick4 kit_hey4\n"
        hits, lines = midi.drum_hits(Song.from_mml(text).compile())
        self.assertEqual([h[1] for h in hits], [36])
        self.assertEqual(len(lines), 1)
        self.assertIn("kit_hey", lines[0])

    def test_round_trip_keeps_a_generated_beat(self):
        for kind in ("fight", "town", "boss"):
            tune = Song.from_mml(musicgen.generate(kind, 7, "", None, dac=True)).compile()
            back = midi.from_midi(midi.to_midi(tune))
            self.assertEqual(back.warnings, [], kind)
            self.assertTrue(plays(tune))
            self.assertEqual(sorted(plays(back.compile())), sorted(plays(tune)), kind)

    def test_round_trip_reports_the_hey_it_cannot_write(self):
        tune = Song.from_mml(musicgen.generate("boss", 3, "", None, dac=True, voice=True)).compile()
        self.assertTrue(any("kit_hey" in x for x in midi.drum_hits(tune)[1]))
        back = midi.from_midi(midi.to_midi(tune))
        self.assertEqual(sorted(plays(back.compile())), sorted(p for p in plays(tune) if p[2] != "kit_hey"))

    def test_export_command_line_carries_the_drums_and_names_what_it_left_out(self):
        import contextlib
        import io
        d = Path(tempfile.mkdtemp())
        (d / "t.mml").write_text("[sample]\nkit_snare = kit_snare\nkit_hey = kit_hey\nV1: c1\nD1: kit_snare4 kit_hey4\n")
        self.assertEqual(main(["import", str(d / "p.csp"), str(d / "t.mml")]), 0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["export", str(d / "p.csp"), "-d", str(d / "out"), "--midi"]), 0)
        self.assertEqual([x[1] for x in self.drums_of((d / "out" / "t.mid").read_bytes())], [38])
        self.assertIn("kit_hey", err.getvalue())


# ------------------------------------------------------------------ more than two drum lanes
GOLDEN = {                    # sha256[:16] of (MML + warnings) the two-lane importer made, taken before drum_lanes existed
    "kit": "acca80258652596c", "ppq480": "97d8f4b653bc12bc", "split": "21704a0cbc3a8476", "dupes": "e5d7f453f87c58ac",
    "cowbell": "47c873799cd81335", "only": "fe61e38bd98a6a4b", "cli": "6b0c3799e1907d81", "overfull": "d291868640039aeb"}
GOLDEN_FILES = {
    "kit": ([(0, 36, 127), (0, 42, 64), (96, 38, 100), (192, 46, 100), (288, 49, 100), (288, 47, 100)], [(0, 60, 96)], 96),
    "ppq480": ([(0, 36, 100), (480, 38, 100), (500, 42, 100)], None, 480),
    "split": ([(0, 36, 100), (0, 38, 100), (96, 36, 100), (96, 38, 100)], None, 96),
    "dupes": ([(0, 42, 50), (0, 44, 90), (96, 49, 80), (96, 57, 100)], None, 96),
    "cowbell": ([(0, 36, 100), (96, 56, 100), (192, 56, 100), (288, 69, 100), (384, 35, 100)], None, 96),
    "only": ([(0, 36, 100), (96, 38, 100), (192, 42, 100)], None, 96),
    "cli": ([(0, 36, 100), (0, 56, 100), (48, 42, 100)], [(0, 60, 96)], 96),
    "overfull": ([(0, 36, 100), (0, 38, 100), (96, 36, 100), (96, 38, 100), (96, 49, 100)], None, 96)}
GOLDEN_BEATS = {"fight": ("020a5fe1df0f2a19", "3c91ea3478c25576"), "town": ("08858ca049cb5f37", "2d9859849b5c1184"),
                "boss": ("76d3dc1940d41332", "1274a5caf60d3d10")}
# five drums at one moment: crash, kick, snare, tom, hat (the priority order)
FIVE = [(0, 36, 100), (0, 38, 90), (0, 42, 80), (0, 49, 127), (0, 47, 70)]


def digest(song):
    import hashlib
    return hashlib.sha256((song.to_mml() + "\n".join(song.warnings)).encode()).hexdigest()[:16]


class DrumLaneTests(unittest.TestCase):
    def test_files_that_need_two_lanes_or_fewer_come_out_byte_identical_to_the_two_lane_importer(self):
        for name, (hits, mel, ppq) in GOLDEN_FILES.items():
            data = drum_file(hits, melody=mel, ppq=ppq)
            two = midi.from_midi(data, drum_lanes=2)
            self.assertEqual(digest(two), GOLDEN[name], name)
            if name != "overfull":                                   # that one needs a third lane: see the next test
                auto = midi.from_midi(data)
                self.assertEqual(digest(auto), GOLDEN[name], name)
                self.assertEqual(auto.to_mml(), two.to_mml(), name)
                self.assertEqual(auto.dacs, 2, name)
                self.assertNotIn("dacs", auto.to_mml(), name)

    def test_generated_beats_round_trip_to_the_same_bytes_and_song_as_before(self):
        import hashlib
        for kind, (file_hash, song_hash) in GOLDEN_BEATS.items():
            tune = Song.from_mml(musicgen.generate(kind, 7, "", None, dac=True)).compile()
            data = midi.to_midi(tune)
            self.assertEqual(hashlib.sha256(data).hexdigest()[:16], file_hash, kind)
            self.assertEqual(digest(midi.from_midi(data)), song_hash, kind)

    def test_simultaneous_hits_spill_to_d3_and_up_in_priority_order(self):
        info = []
        song = midi.from_midi(drum_file(FIVE), info=info)
        self.assertEqual(song.warnings, [])
        self.assertEqual(song.dacs, 5)
        self.assertIn("dacs 5", song.to_mml())
        got = sorted(plays(song.compile()))
        # crash D2 (its lane), kick D1 (its lane), snare finds both taken -> D3, tom -> D4, hat -> D5
        self.assertEqual([(g[0], g[2]) for g in got],
                         [(1, "kit_kick"), (2, "kit_crash"), (3, "kit_snare"), (4, "kit_tom"), (5, "kit_hat")])
        self.assertEqual(len(song.compile().dac), 5)
        self.assertEqual(len(info), 1)
        self.assertIn("5 hits on 5 lanes (D1, D2, D3, D4, D5)", info[0])
        self.assertEqual(digest(midi.from_midi(drum_file(FIVE))), digest(song))            # deterministic
        again = Song.from_mml(song.to_mml())                                              # and the MML it writes reads back
        self.assertEqual(plays(again.compile()), plays(song.compile()))

    def test_auto_uses_only_the_lanes_the_busiest_moment_needs(self):
        hits = [(0, 36, 100), (0, 38, 100), (0, 42, 100)] + [(96 * k, 36, 100) for k in range(1, 5)]
        song = midi.from_midi(drum_file(hits))
        self.assertEqual(song.dacs, 3)
        self.assertEqual(sorted({p[0] for p in plays(song.compile())}), [1, 2, 3])

    def test_seven_kit_drums_at_once_use_seven_lanes(self):
        hits = [(0, n, 100) for n in (36, 38, 42, 46, 49, 47, 41)] + [(96, 36, 100)]
        song = midi.from_midi(drum_file(hits))
        self.assertEqual((song.dacs, song.warnings), (7, []))
        self.assertEqual(len({p[2] for p in plays(song.compile())}), 7)

    def test_a_fixed_number_is_a_ceiling_and_the_rest_is_reported(self):
        song = midi.from_midi(drum_file(FIVE), drum_lanes=3)
        self.assertEqual(song.dacs, 3)
        self.assertEqual(len(plays(song.compile())), 3)
        self.assertEqual(len(song.warnings), 1)
        self.assertIn("2 drum hits did not fit on the 3 drum lanes (D1-D3)", song.warnings[0])
        self.assertIn("hat x1", song.warnings[0])
        self.assertIn("tom x1", song.warnings[0])
        two = midi.from_midi(drum_file(FIVE), drum_lanes="2")
        self.assertEqual(two.dacs, 2)
        self.assertIn("3 drum hits did not fit on the two drum lanes (D1, D2)", two.warnings[0])

    def test_a_ceiling_above_the_need_changes_nothing(self):
        data = drum_file(*GOLDEN_FILES["kit"][:2])
        self.assertEqual(midi.from_midi(data, drum_lanes=8).to_mml(), midi.from_midi(data).to_mml())

    def test_bad_lane_counts_are_refused(self):
        for bad in (0, 1, 9, "nine", "", -3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                midi.from_midi(drum_file(FIVE), drum_lanes=bad)

    def test_the_extra_lanes_are_written_with_a_pan_and_the_hits_volumes(self):
        song = midi.from_midi(drum_file(FIVE))
        text = song.to_mml()
        for k in (3, 4, 5):
            self.assertIn("D%d:" % k, text)
        self.assertEqual(sorted(p[3] for p in plays(song.compile())), sorted([11, 8, 12, 15, 9]))

    def test_eight_lanes_of_hits_are_all_channel_10_notes_at_one_tick(self):
        names = ["kit_kick", "kit_snare", "kit_tomlo", "kit_tom", "kit_hat", "kit_ohat", "kit_crash", "kit_kick"]
        text = "dacs 8\n[sample]\n" + "".join("%s = %s\n" % (n, n) for n in sorted(set(names))) + "V1: c1\n" + "".join(
            "D%d: v15 %s4 r4\n" % (i, n) for i, n in enumerate(names, 1))
        tune = Song.from_mml(text).compile()
        self.assertEqual(len(tune.dac), 8)
        hits, lines = midi.drum_hits(tune)
        self.assertEqual(lines, [])
        want = sorted([36, 38, 41, 47, 42, 46, 49])
        self.assertEqual(sorted(h[1] for h in hits), want)                     # D1 and D8 both play the kick: one hit
        drums = DrumExportTests.drums_of(midi.to_midi(tune))
        self.assertEqual([(t, n) for t, n, v in drums], [(0, n) for n in want])
        back = midi.from_midi(midi.to_midi(tune))
        self.assertEqual(back.warnings, [])
        self.assertEqual(sorted(p[2] for p in plays(back.compile())), sorted(set(names)))

    def test_hits_on_d4_follow_each_other_in_order(self):
        text = "dacs 4\n[sample]\nkit_hat = kit_hat\nkit_snare = kit_snare\nV1: c1\nD4: kit_hat4 kit_snare4 kit_hat4\n"
        drums = DrumExportTests.drums_of(midi.to_midi(Song.from_mml(text).compile()))
        self.assertEqual(drums, [(0, 42, 127), (96, 38, 127), (192, 42, 127)])

    def test_round_trip_of_a_beat_with_five_simultaneous_hits(self):
        song = midi.from_midi(drum_file(FIVE + [(96, 36, 100), (96, 49, 100)]))
        back = midi.from_midi(midi.to_midi(song.compile()))
        key = lambda tune: sorted((p[1], p[2], p[3]) for p in plays(tune))
        self.assertEqual(key(back.compile()), key(song.compile()))
        self.assertEqual(back.dacs, 5)

    def test_command_line_drum_lanes_option(self):
        import contextlib
        import io
        d = Path(tempfile.mkdtemp())
        (d / "five.mid").write_bytes(drum_file(FIVE, melody=[(0, 60, 96)]))
        for opt, dacs, warned in (("auto", 5, False), ("4", 4, True), ("2", 2, True)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["import", str(d / "p.csp"), str(d / "five.mid"), "--id", "f" + opt, "--drum-lanes", opt]), 0)
            self.assertEqual(len(pk.load(d / "p.csp").tune("f" + opt).dac), dacs, opt)
            self.assertEqual("warning:" in out.getvalue(), warned, opt)
            self.assertIn("drums:", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["import", str(d / "p.csp"), str(d / "five.mid"), "--id", "dflt"]), 0)     # the default is auto
        self.assertEqual(len(pk.load(d / "p.csp").tune("dflt").dac), 5)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertNotEqual(main(["import", str(d / "p.csp"), str(d / "five.mid"), "--drum-lanes", "12"]), 0)
        self.assertIn("drum lanes", err.getvalue())

    def test_command_line_export_of_a_five_lane_song_writes_all_five_drums(self):
        d = Path(tempfile.mkdtemp())
        (d / "five.mid").write_bytes(drum_file(FIVE, melody=[(0, 60, 96)]))
        self.assertEqual(main(["import", str(d / "p.csp"), str(d / "five.mid")]), 0)
        self.assertEqual(main(["export", str(d / "p.csp"), "-d", str(d / "out"), "--midi"]), 0)
        self.assertEqual([n for _, n, _ in DrumExportTests.drums_of((d / "out" / "five.mid").read_bytes())], [36, 38, 42, 47, 49])
