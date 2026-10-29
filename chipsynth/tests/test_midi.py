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
