# Copyright (C) 2026 Michael Allen Wilmes
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

    def test_drums_and_extra_parts_are_reported_not_fatal(self):
        def trk(ev):
            body = b"".join(midi._varlen(d) + b for d, b in ev) + b"\x00\xff\x2f\x00"
            return midi._chunk(b"MTrk", body)
        parts = [trk([(0, bytes([0x90 | ch, 60 + ch, 90]) ), (96, bytes([0x80 | ch, 60 + ch, 0]))]) for ch in (0, 1, 2, 3, 4, 9)]
        data = midi._chunk(b"MThd", struct.pack(">HHH", 1, 6, 96)) + b"".join(parts)
        song = midi.from_midi(data)
        self.assertEqual(sorted(k for k, v in song.tracks.items() if v), ["V1", "V2", "V3", "V4"])
        self.assertTrue(any("first four" in w for w in song.warnings))
        self.assertTrue(any("drum" in w for w in song.warnings))

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
