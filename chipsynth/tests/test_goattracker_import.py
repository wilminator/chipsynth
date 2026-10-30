# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""GoatTracker .sng import: the files are built here from the format, byte by byte; no real songs are used."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
