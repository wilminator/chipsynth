# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Furnace .fur import.  The modules are built here, byte by byte, from the format document (papers/format.md and
papers/newIns.md); no real songs are used or committed."""
import contextlib
import io
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from chipsynth import __main__ as cli, mml, package as pk, song as sg
from chipsynth.formats import furnace as fur

NES, SID6581, SID8580, SID_PCM, GB, YM = 0x06, 0x47, 0x07, 0xE2, 0x04, 0x83
CHANNELS = {0x06: 5, 0x47: 3, 0x07: 3, 0xE2: 4, 0x04: 4, 0x83: 6, 0x03: 4}
C4, D4, E4, G4 = (4 + 5) * 12, (4 + 5) * 12 + 2, (4 + 5) * 12 + 4, (4 + 5) * 12 + 7     # pattern note values (C-4 is 108)
OFF = 180


# ------------------------------------------------------------------ building a module from the format document
def cstr(text):
    return text.encode() + b"\0"


def block(tag, body):
    return tag + struct.pack("<I", len(body)) + body


def pattern_data(cells):
    """{row: (note, ins, vol, [(effect, value)])} -> the PATN row stream (None for a field that is absent)."""
    out, row = bytearray(), 0
    for r in sorted(cells):
        gap = r - row
        while gap > 0:
            if gap == 1:
                out.append(0x00)
                gap = 0
            else:
                n = min(gap, 128)
                out.append(0x80 | (n - 2))
                gap -= n
        note, ins, vol, fx = cells[r]
        mask = (1 if note is not None else 0) | (2 if ins is not None else 0) | (4 if vol is not None else 0)
        extra = bytearray()
        if len(fx) == 1:
            mask |= 0x18                       # one effect with its value: the short form
        elif fx:
            low = high = 0
            for k, _ in enumerate(fx[:4]):
                low |= 3 << (2 * k)
            for k, _ in enumerate(fx[4:8]):
                high |= 3 << (2 * k)
            mask |= 0x20 | (0x40 if high else 0)
            extra.append(low)
            if high:
                extra.append(high)
        out.append(mask)
        out += extra
        if note is not None:
            out.append(note)
        if ins is not None:
            out.append(ins)
        if vol is not None:
            out.append(vol)
        for code, val in fx:
            out += bytes((code, val))
        row = r + 1
    out.append(0xFF)
    return bytes(out)


def macro_feature(macros):
    """macros: [(code, values, loop, release, speed[, wide])] -> the body of an MA feature (8-byte headers; 8-bit signed
    values, or 16-bit signed ones when `wide`)."""
    body = struct.pack("<H", 8)
    for code, values, loop, rel, speed, *wide in macros:
        word = 2 if wide and wide[0] else 1
        body += bytes((code, len(values), 255 if loop is None else loop, 255 if rel is None else rel, 0, word << 6, 0, speed))
        body += struct.pack("<%d%s" % (len(values), "h" if word == 2 else "b"), *values)
    return body + bytes((255,))


def feature(code, body):
    return code.encode() + struct.pack("<H", len(body)) + body


def instrument(version, typ, name="", macros=(), c64=None, sm=None, ne=None, gb=None):
    body = struct.pack("<HH", version, typ)
    if name:
        body += feature("NA", cstr(name))
    if macros:
        body += feature("MA", macro_feature(macros))
    if c64:
        flags1, flags2, ad, sr, duty, cutoff, res = c64
        body += feature("64", struct.pack("<BBBBHH", flags1, flags2, ad, sr, duty, (res << 12) | cutoff))
    if gb:
        body += feature("GB", bytes(gb) + b"\0")
    if sm:
        init, flags, mapping = sm
        data = struct.pack("<HBB", init, flags, 0)
        if flags & 1:
            entries = [(0, 0xFFFF)] * (180 if version >= 246 else 120)
            for note, (play, smp) in mapping.items():
                idx = note if version >= 246 else note - 60
                entries[idx] = (play, smp)
            data += b"".join(struct.pack("<HH", a, b) for a, b in entries)
        body += feature("SM", data)
    if ne:
        data = b"\1"
        entries = [(255, 255)] * (180 if version >= 246 else 120)
        for note, (pitch, delta) in ne.items():
            entries[note if version >= 246 else note - 60] = (pitch, delta)
        body += feature("NE", data + b"".join(bytes(e) for e in entries))
    return body + feature("EN", b"")


def sample(name, depth, data, rate=8363, length=None):
    return (cstr(name) + struct.pack("<IIIBBBBii", len(data) if length is None else length, rate, rate, depth, 0, 0, 0, -1, -1)
            + bytes(16) + data)


class Sub:
    def __init__(self, name="", speeds=(6,), hz=60.0, patlen=16, orders=None, timebase=0, hla=4, hlb=16):
        self.name, self.speeds, self.hz, self.patlen, self.timebase, self.hla, self.hlb = name, list(speeds), hz, patlen, timebase, hla, hlb
        self.orders = orders or [[0]]


def build(version=228, chips=(NES,), subs=None, patterns=None, instruments=(), samples=(), name="Test Song", author="Tester",
          comment="", grooves=(), compress=False, tuning=440.0, wide=None):
    """patterns: {(subsong, channel, pattern index): {row: (note, ins, vol, [(effect, value)])}}.  `wide`: a 16-bit channel
    number in the pattern blocks (default: from version 252 on)."""
    wide = version > 251 if wide is None else wide
    subs = subs or [Sub()]
    patterns = patterns or {}
    nch = sum(CHANNELS[c] for c in chips)
    pat_keys = sorted(patterns)
    parts = []                                          # blocks after INFO / INF2, in order
    ins_blocks = [block(b"INS2", i) for i in instruments]
    smp_blocks = [block(b"SMP2", s) for s in samples]
    pat_blocks = []
    for sub, ch, idx in pat_keys:
        head = bytes((sub,)) + (struct.pack("<H", ch) if wide else bytes((ch,))) + struct.pack("<H", idx) + cstr("")
        pat_blocks.append(block(b"PATN", head + pattern_data(patterns[(sub, ch, idx)])))
    new = version >= 240

    def old_sub_block(s):
        body = bytes((s.timebase, s.speeds[0], s.speeds[-1], 0)) + struct.pack("<f", s.hz) + struct.pack("<HH", s.patlen, len(s.orders[0]))
        body += bytes((s.hla, s.hlb)) + struct.pack("<HH", 150, 150) + cstr(s.name) + cstr("")
        for c in range(nch):
            body += bytes(s.orders[c] if c < len(s.orders) else [0] * len(s.orders[0]))
        body += bytes(nch * 3) + b"".join(cstr("") for _ in range(nch * 2))
        return block(b"SONG", body + bytes((len(s.speeds),)) + bytes(list(s.speeds) + [0] * (16 - len(s.speeds))))

    def new_sub_block(s):
        body = struct.pack("<f", s.hz) + bytes((1, 1)) + struct.pack("<HH", s.patlen, len(s.orders[0])) + bytes((s.hla, s.hlb))
        body += struct.pack("<HH", 150, 150) + bytes((len(s.speeds),)) + struct.pack("<16H", *(list(s.speeds) + [0] * (16 - len(s.speeds))))
        body += cstr(s.name) + cstr("")
        for c in range(nch):
            body += bytes(s.orders[c] if c < len(s.orders) else [0] * len(s.orders[0]))
        body += bytes(nch * 3) + b"".join(cstr("") for _ in range(nch * 2)) + bytes(4 * nch)
        return block(b"SNG2", body)

    groove_blocks = [block(b"GROV", bytes((len(g),)) + struct.pack("<16H", *(list(g) + [0] * (16 - len(g))))) for g in grooves]

    def assemble(ptrs):
        if not new:
            first = subs[0]
            b = bytes((first.timebase, first.speeds[0], first.speeds[-1], 0)) + struct.pack("<f", first.hz)
            b += struct.pack("<HH", first.patlen, len(first.orders[0])) + bytes((first.hla, first.hlb))
            b += struct.pack("<HHHI", len(instruments), 0, len(samples), len(pat_blocks))
            b += bytes(chips) + bytes(32 - len(chips)) + bytes(32 + 32) + bytes(128)
            b += cstr(name) + cstr(author) + struct.pack("<f", tuning) + bytes(20)
            for p in ptrs["ins"] + ptrs["smp"] + ptrs["pat"]:
                b += struct.pack("<I", p)
            for c in range(nch):
                b += bytes(first.orders[c] if c < len(first.orders) else [0] * len(first.orders[0]))
            b += bytes(nch * 3) + b"".join(cstr("") for _ in range(nch * 2))
            b += cstr(comment) + struct.pack("<f", 1.0) + bytes(28) + struct.pack("<HH", 150, 150)
            b += cstr(first.name) + cstr("") + bytes((len(subs) - 1,)) + bytes(3)
            for p in ptrs["sub"]:
                b += struct.pack("<I", p)
            b += b"".join(cstr("") for _ in range(6))
            b += bytes(12 * len(chips))
            b += struct.pack("<I", 0) + bytes(1) + bytes(8)
            b += bytes((len(first.speeds),)) + bytes(list(first.speeds) + [0] * (16 - len(first.speeds)))
            b += bytes((len(grooves),))
            for g in grooves:
                b += bytes((len(g),)) + bytes(list(g) + [0] * (16 - len(g)))
            b += bytes(12)
            return block(b"INFO", b)
        b = cstr(name) + cstr(author) + b"".join(cstr("") for _ in range(6)) + struct.pack("<f", tuning) + bytes(1)
        b += struct.pack("<f", 1.0) + struct.pack("<HH", nch, len(chips))
        for c in chips:
            b += struct.pack("<HHfff", c, CHANNELS[c], 1.0, 0.0, 0.0)
        b += struct.pack("<I", 0) + bytes(1)
        for kind, key in ((1, "sub"), (4, "ins"), (6, "smp"), (7, "pat"), (10, "gro")):
            if ptrs[key]:
                b += bytes((kind,)) + struct.pack("<I", len(ptrs[key])) + b"".join(struct.pack("<I", p) for p in ptrs[key])
        b += bytes((0,))
        return block(b"INF2", b)

    sub_blocks = [(new_sub_block if new else old_sub_block)(s) for s in (subs if new else subs[1:])]
    order = [("sub", sub_blocks), ("ins", ins_blocks), ("smp", smp_blocks), ("pat", pat_blocks), ("gro", groove_blocks if new else [])]
    info_len = len(assemble({"sub": [0] * len(sub_blocks), "ins": [0] * len(ins_blocks), "smp": [0] * len(smp_blocks),
                             "pat": [0] * len(pat_blocks), "gro": [0] * len(groove_blocks if new else [])}))
    pos, ptrs, body = 32 + info_len, {}, b""
    for key, blocks in order:
        ptrs[key] = []
        for blk in blocks:
            ptrs[key].append(pos)
            pos += len(blk)
            body += blk
    out = b"-Furnace module-" + struct.pack("<HHI", version, 0, 32) + bytes(8) + assemble(ptrs) + body
    return zlib.compress(out) if compress else out


def simple(notes, version=228, **kw):
    """A one-pattern NES module: `notes` is {row: (note, ins, vol, fx)} for the first pulse channel (and channel 0 only)."""
    return build(version, patterns={(0, 0, 0): notes}, **kw)


def load(data, **kw):
    return fur.load(data, **kw)


def events(song, track="V1", kind=None):
    ev = song.tracks[track]
    return [e for e in ev if kind is None or e.__class__.__name__ == kind]


def notes(song, track="V1"):
    return events(song, track, "Note")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main([str(a) for a in argv])
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ reading
class ParseTests(unittest.TestCase):
    def test_header_and_names(self):
        m = fur.parse(simple({0: (C4, 0, None, [])}, comment="hello"))
        self.assertEqual((m.version, m.name, m.author, m.comment), (228, "Test Song", "Tester", "hello"))
        self.assertEqual(m.chips, [(NES, 5)])
        self.assertEqual(m.subsongs[0].hz, 60.0)
        self.assertEqual(m.subsongs[0].speeds, [6])
        self.assertEqual(m.patterns[(0, 0, 0)][0].note, C4)

    def test_zlib_compressed_files_read_the_same(self):
        plain = simple({0: (C4, 0, None, [])})
        packed = simple({0: (C4, 0, None, [])}, compress=True)
        self.assertNotEqual(plain, packed)
        self.assertTrue(fur.looks_like(plain) and fur.looks_like(packed))
        self.assertEqual(fur.parse(plain).patterns, fur.parse(packed).patterns)

    def test_inf2_modules_read_too(self):
        data = build(250, patterns={(0, 0, 0): {0: (C4, 0, None, [])}}, subs=[Sub("one")], compress=True)
        m = fur.parse(data)
        self.assertEqual((m.version, m.subsongs[0].name, m.chips), (250, "one", [(NES, 5)]))
        self.assertEqual(m.patterns[(0, 0, 0)][0].note, C4)

    def test_the_pattern_stream_unpacks_skips_and_effects(self):
        cells = {0: (C4, 1, 12, [(0x0F, 3)]), 5: (None, None, None, [(0x12, 1), (0xEC, 2)]), 200: (OFF, None, None, [])}
        m = fur.parse(build(patterns={(0, 0, 0): cells}, subs=[Sub(patlen=256)]))
        rows = m.patterns[(0, 0, 0)]
        self.assertEqual(sorted(rows), [0, 5, 200])
        self.assertEqual((rows[0].note, rows[0].ins, rows[0].vol, rows[0].fx), (C4, 1, 12, [(0x0F, 3)]))
        self.assertEqual(rows[5].fx, [(0x12, 1), (0xEC, 2)])

    def test_garbage_and_old_and_new_versions_are_refused_clearly(self):
        for bad in (b"", b"not a module", b"-Furnace module-", b"\x78\x9cgarbage"):
            with self.assertRaises(fur.FurnaceError):
                fur.parse(bad)
        old = bytearray(simple({0: (C4, 0, None, [])}))
        old[16:18] = struct.pack("<H", 100)
        with self.assertRaisesRegex(fur.FurnaceError, "older than this importer reads"):
            fur.parse(bytes(old))
        new = bytearray(simple({0: (C4, 0, None, [])}))
        new[16:18] = struct.pack("<H", 400)
        with self.assertRaisesRegex(fur.FurnaceError, "newer than this importer knows"):
            fur.parse(bytes(new))

    def test_a_patr_pattern_block_is_refused_with_its_reason(self):
        data = bytearray(simple({0: (C4, 0, None, [])}))
        i = bytes(data).find(b"PATN")
        data[i:i + 4] = b"PATR"
        with self.assertRaisesRegex(fur.FurnaceError, "older PATR layout"):
            fur.parse(bytes(data))

    def test_truncation_is_an_error_not_a_crash(self):
        data = simple({0: (C4, 1, 12, [(0x0F, 3)])}, instruments=[instrument(228, 34, "i", macros=[(0, [15, 8], None, None, 1)])])
        for cut in range(0, len(data) - 1, 7):
            try:
                fur.parse(data[:cut])
            except fur.FurnaceError:
                pass

    def test_a_chip_the_info_block_cannot_size_is_refused(self):
        data = bytearray(simple({0: (C4, 0, None, [])}))
        ids_at = bytes(data).find(b"INFO") + 8 + 24                    # time base, speeds, arp, hz, lengths, highlights, counts
        self.assertEqual(data[ids_at], NES)
        data[ids_at] = 0xFB                    # an id that is in no list
        with self.assertRaisesRegex(fur.FurnaceError, "does not know"):
            fur.parse(bytes(data))


# ------------------------------------------------------------------ playing
class NesTests(unittest.TestCase):
    def one(self, cells, **kw):
        res = load(simple(cells, **kw))
        self.assertEqual(len(res.songs), 1)
        return res.songs[0].song, res.report

    def test_notes_pitches_and_row_lengths(self):
        song, rep = self.one({0: (C4, 0, None, []), 1: (E4, 0, None, []), 2: (G4, 0, None, []), 3: (OFF, 0, None, [])})
        n = notes(song)
        self.assertEqual([(x.letter, x.acc, x.dur) for x in n], [("c", 0, 24), ("e", 0, 24), ("g", 0, 24)])
        self.assertEqual(song.bpm, 150)
        self.assertEqual(song.voices, 4)
        self.assertEqual(song.dacs, 1)
        tune = song.compile()
        self.assertEqual(tune.voices[0][0].pitch, 48)                        # C-4 is chipsynth pitch 48

    def test_the_song_runs_the_whole_pattern_and_loops_at_the_start(self):
        song, _ = self.one({0: (C4, 0, None, [])})
        total = sum(e.dur for e in events(song) if e.__class__.__name__ in ("Note", "Rest"))
        self.assertEqual(total, 16 * 24)
        self.assertFalse(song.once)
        self.assertFalse(events(song, kind="Loop"))
        self.assertEqual(song.compile().loop_tick, 0)

    def test_speed_effects_alternating_speeds_and_tick_rate(self):
        song, _ = self.one({0: (C4, 0, None, [(0x0F, 3)]), 1: (D4, 0, None, []), 2: (OFF, None, None, [])})
        self.assertEqual([x.dur for x in notes(song)], [12, 12])
        song, _ = self.one({0: (C4, 0, None, []), 1: (D4, 0, None, []), 2: (E4, 0, None, []), 3: (OFF, None, None, [])},
                           subs=[Sub(speeds=(6, 3))])
        self.assertEqual([x.dur for x in notes(song)], [24, 12, 24])
        song, _ = self.one({0: (C4, 0, None, [])}, subs=[Sub(hz=50.0)])
        self.assertEqual(song.bpm, 125)

    def test_a_groove_chosen_by_09(self):
        song, _ = self.one({0: (C4, 0, None, [(0x09, 0)]), 1: (D4, 0, None, []), 2: (E4, 0, None, []), 3: (OFF, None, None, [])},
                           grooves=[(2, 4)])
        self.assertEqual([x.dur for x in notes(song)], [8, 16, 8])

    def test_a_tick_rate_change_is_a_tempo_event_and_splits_a_note_that_spans_it(self):
        cells = {0: (C4, 0, None, []), 4: (None, None, None, [(0xF0, 75)])}      # F0 75: 75 bpm, 30 Hz
        song, _ = self.one(cells)
        ev = song.tracks["V1"]
        self.assertTrue(any(isinstance(e, sg.Tempo) and e.bpm == 75 for e in ev), ev)
        self.assertTrue(any(isinstance(e, sg.Extend) for e in ev))
        tune = song.compile()
        self.assertEqual(tune.tempo_events, [(96, 75)])
        self.assertEqual(len(tune.voices[0]), 1)                                  # still one note, held through the change

    def test_jump_back_marks_the_loop_and_stop_ends_the_song(self):
        subs = [Sub(orders=[[0, 1]])]
        pats = {(0, 0, 0): {0: (C4, 0, None, [])}, (0, 0, 1): {0: (D4, 0, None, []), 2: (None, None, None, [(0x0B, 1)])}}
        res = load(build(patterns=pats, subs=subs))
        song = res.songs[0].song
        loops = events(song, kind="Loop")
        self.assertEqual(len(loops), 1)
        self.assertEqual(song.compile().loop_tick, 16 * 24)
        self.assertFalse(song.once)
        pats[(0, 0, 1)][2] = (None, None, None, [(0xFF, 0)])
        song = load(build(patterns=pats, subs=subs)).songs[0].song
        self.assertTrue(song.once)
        self.assertFalse(events(song, kind="Loop"))
        total = sum(e.dur for e in events(song) if e.__class__.__name__ in ("Note", "Rest"))
        self.assertEqual(total, 16 * 24 + 3 * 24)                                 # the stop row is played

    def test_pattern_break_shortens_an_order(self):
        subs = [Sub(orders=[[0, 1]])]
        pats = {(0, 0, 0): {0: (C4, 0, None, [(0x0D, 2)])}, (0, 0, 1): {0: (D4, 0, None, [])}}
        song = load(build(patterns=pats, subs=subs)).songs[0].song
        self.assertEqual(sum(e.dur for e in events(song) if e.__class__.__name__ in ("Note", "Rest")), 24 + 14 * 24)

    def test_note_off_cut_and_delay(self):
        song, _ = self.one({0: (C4, 0, None, [(0xEC, 2)]), 2: (D4, 0, None, []), 3: (OFF, 0, None, []), 4: (E4, 0, None, [(0xED, 1)])})
        got = [(e.__class__.__name__, e.dur) for e in song.tracks["V1"] if isinstance(e, (sg.Note, sg.Rest))]
        # C-4 cut after 2 ticks (8), a rest to row 2, D-4 to the note off (24), a rest to the delayed E-4, E-4 to the end
        self.assertEqual(got, [("Note", 8), ("Rest", 40), ("Note", 24), ("Rest", 28), ("Note", 16 * 24 - 100)])

    def test_volume_column_and_instrument_choice(self):
        ins = [instrument(228, 34, "lead", macros=[(2, [3], None, None, 1)])]
        song, rep = self.one({0: (C4, 0, 9, []), 1: (D4, 0, 9, [])}, instruments=ins)
        vols = events(song, kind="Volume")
        self.assertEqual([v.value for v in vols], [9])
        self.assertEqual(song.instruments[events(song, kind="Select")[0].name].pw, 75)    # duty 3 is 75 %

    def test_volume_macro_becomes_an_envelope(self):
        ins = [instrument(228, 34, "pluck", macros=[(0, [15, 12, 9, 6, 3, 0], None, None, 1)])]
        song, rep = self.one({0: (C4, 0, None, [])}, instruments=ins)
        i = song.instruments["pluck"]
        self.assertEqual((i.vol, i.s), (15, 0))
        ins = [instrument(228, 34, "slow", macros=[(0, [8, 8, 8], 0, None, 4)])]                    # a speed of 4 ticks a step
        song, rep = self.one({0: (C4, 0, None, [])}, instruments=ins)
        self.assertEqual(song.instruments["slow"].vol, 8)

    def test_arpeggio_macro_and_arpeggio_effect(self):
        ins = [instrument(228, 34, "chord", macros=[(1, [0, 4, 7], 0, None, 1)])]
        song, rep = self.one({0: (C4, 0, None, [])}, instruments=ins)
        i = song.instruments["chord"]
        self.assertEqual((i.arp, i.arprate), ((0, 4, 7), 24))
        song, rep = self.one({0: (C4, 0, None, [(0x00, 0x47)]), 1: (D4, 0, None, [(0x00, 0)])})
        a, b = [song.instruments[e.name] for e in events(song, kind="Select")]
        self.assertEqual((a.arp, a.arprate, b.arp), ((0, 4, 7), 24, ()))
        song, rep = self.one({0: (C4, 0, None, [(0xE0, 3), (0x00, 0x37)])})
        self.assertEqual(song.instruments[events(song, kind="Select")[0].name].arprate, 8)

    def test_a_cycling_duty_macro_is_a_pulse_width_sweep(self):
        ins = [instrument(228, 34, "wobble", macros=[(2, [0, 1, 2, 3, 2, 1], 0, None, 1)])]
        song, rep = self.one({0: (C4, 0, None, [])}, instruments=ins)
        i = song.instruments["wobble"]
        self.assertEqual((i.pws_from, i.pws_to), (12, 75))
        self.assertTrue(1 <= i.pws_rate <= 15)

    def test_duty_effect_sets_the_duty_from_then_on(self):
        song, _ = self.one({0: (C4, 0, None, [(0x12, 0)]), 1: (D4, 0, None, [])})
        pw = [song.instruments[e.name].pw for e in events(song, kind="Select")]
        self.assertEqual(pw, [12])

    def test_triangle_noise_and_voice_numbers(self):
        pats = {(0, 2, 0): {0: (C4, 0, 5, [])}, (0, 3, 0): {0: (C4 + 3, 0, None, []), 1: (C4 + 4, 0, None, [])}}
        res = load(build(patterns=pats))
        s = res.songs[0].song
        self.assertEqual(s.instruments[events(s, "V3", "Select")[0].name].wave, "tri")
        self.assertFalse(events(s, "V3", "Volume"))                                # the triangle has no volume
        names = [e.name for e in events(s, "V4", "Select")]
        self.assertEqual(s.instruments[names[0]].wave, "noise")
        pitches = [s.compile().voices[3][k].pitch for k in range(2)]
        self.assertLess(pitches[0], pitches[1])                                    # a higher note is a higher noise

    def test_noise_mode_from_the_duty_macro(self):
        ins = [instrument(228, 34, "metal", macros=[(2, [1], None, None, 1)])]
        res = load(build(patterns={(0, 3, 0): {0: (C4, 0, None, [])}}, instruments=ins))
        s = res.songs[0].song
        self.assertEqual(s.instruments[events(s, "V4", "Select")[0].name].nmode, "short")

    def test_dpcm_sample_map_and_rate(self):
        data = bytes(range(1, 17))
        ins = [instrument(228, 34, "kick", sm=(0, 1, {C4: (C4, 0)}), ne={C4: (9, 255)})]
        res = load(build(instruments=ins, samples=[sample("Kick Drum", 1, data)],
                         patterns={(0, 4, 0): {0: (C4, 0, None, []), 2: (G4, 0, None, []), 4: (OFF, None, None, [])}}))
        s = res.songs[0].song
        plays = events(s, "D1", "Play")
        self.assertEqual(len(plays), 1)                                              # G4 has nothing mapped to it
        self.assertEqual(plays[0].dur, 48)                                           # up to the unmapped note, which ends it
        smp = res.songs[0].samples[plays[0].sample]
        self.assertEqual((smp.rate, b"".join(smp.tracks[0])), (9, data))
        self.assertTrue(any("no sample" in x for x in res.report.dropped))
        lib = dict(mml.builtin_library())
        lib["_mem"] = res.songs[0].samples
        self.assertTrue(s.compile(lib).dac[0])

    def test_dpcm_instrument_with_only_an_initial_sample(self):
        data = bytes(range(1, 17))
        ins = [instrument(228, 34, "kick", sm=(0, 0, {}))]
        res = load(build(instruments=ins, samples=[sample("Kick", 1, data)],
                         patterns={(0, 4, 0): {0: (C4, 0, None, [])}}))
        self.assertEqual(len(events(res.songs[0].song, "D1", "Play")), 1)

    def test_pcm_samples_are_converted_and_said_so(self):
        pcm = bytes(((i * 7) % 200) - 100 & 255 for i in range(400))
        ins = [instrument(228, 34, "snare", sm=(0, 2, {}))]
        res = load(build(instruments=ins, samples=[sample("s", 8, pcm)], patterns={(0, 4, 0): {0: (C4, 0, None, [])}}))
        self.assertTrue(res.songs[0].samples)
        self.assertTrue(any("PCM" in x for x in res.report.approximated))

    def test_what_is_not_carried_over_is_reported(self):
        ins = [instrument(228, 34, "p", macros=[(4, [1, 2], None, None, 1), (3, [1], None, None, 1)])]
        res = load(simple({0: (C4, 0, None, [(0x04, 0x47), (0x03, 8), (0xF5, 0)])}, instruments=ins))
        drops = "\n".join(res.report.dropped)
        self.assertIn("vibrato (04xy)", drops)
        self.assertIn("portamento (03xx)", drops)
        self.assertIn("its pitch macro", drops)

    def test_a_newer_version_than_the_checked_ones_is_read_and_said_so(self):
        cells = {0: (C4, 0, None, []), 1: (E4, 0, None, [])}
        for wide in (True, False):                       # the channel number of a pattern: 16 bits as the document says, or 8
            res = load(build(255, patterns={(0, 0, 0): cells}, wide=wide))
            self.assertEqual([x.dur for x in notes(res.songs[0].song)][:1], [24])
            self.assertTrue(any("newer than the files" in x for x in res.report.approximated))

    def test_old_and_new_info_blocks_give_the_same_song(self):
        cells = {0: (C4, 0, None, []), 1: (E4, 0, None, [])}
        a = load(simple(cells, version=228)).songs[0].song
        b = load(build(250, patterns={(0, 0, 0): cells})).songs[0].song
        self.assertEqual(a.tracks["V1"], b.tracks["V1"])
        self.assertEqual(a.bpm, b.bpm)

    def test_the_song_renders(self):
        song, _ = self.one({0: (C4, 0, None, []), 1: (E4, 0, None, [])})
        from chipsynth.player import render
        x = render(song.compile(), seconds=1.0)
        self.assertGreater(float(abs(x).max()), 0.01)


class OtherChipTests(unittest.TestCase):
    def test_sid_voices_envelope_wave_pulse_width_and_filter(self):
        flags1 = 0x04 | 0x10                              # pulse, routed to the filter
        flags2 = 0x01                                      # low pass
        ins = [instrument(228, 3, "lead", c64=(flags1, flags2, 0x2A, 0x8C, 0x0800, 0x400, 6))]
        res = load(build(chips=(SID6581,), instruments=ins, patterns={(0, 1, 0): {0: (C4, 0, None, [])}}))
        s = res.songs[0].song
        self.assertEqual((s.voices, s.dacs), (3, 0))
        i = s.instruments[events(s, "V2", "Select")[0].name]
        self.assertEqual((i.wave, i.a, i.d, i.s, i.r, i.pw), ("pulse", 2, 10, 8, 12, 50))
        self.assertEqual((i.filt, i.filt_res), ("lp", 6))
        self.assertTrue(0 <= i.filt_cut <= 15)
        self.assertTrue(any("filter is shared" in x for x in res.report.approximated))

    def test_sid_waveform_priority_and_macros_that_are_not_kept(self):
        ins = [instrument(228, 3, "x", macros=[(0, [15, 8], None, None, 1), (8, [3, 4], None, None, 1)], c64=(0x03, 0xC0, 0x00, 0xF0, 0x800, 0, 0))]
        res = load(build(chips=(SID8580,), instruments=ins, patterns={(0, 0, 0): {0: (C4, 0, None, [])}}))
        s = res.songs[0].song
        self.assertEqual(s.instruments[events(s, "V1", "Select")[0].name].wave, "saw")
        text = "\n".join(res.report.dropped) + "\n".join(res.report.approximated)
        self.assertIn("combines waves", text)
        self.assertIn("ring modulation", text)
        self.assertIn("cutoff macro", text)
        self.assertIn("volume (global on the SID)", text)

    def test_a_pulse_width_macro_sweeps_when_absolute(self):
        ins = [instrument(228, 3, "pw", c64=(0x84, 0, 0x00, 0xF0, 0x800, 0, 0),
                          macros=[(2, [400, 1200, 2000, 2800, 2000, 1200], 0, None, 1, True)])]
        res = load(build(chips=(SID6581,), instruments=ins, patterns={(0, 0, 0): {0: (C4, 0, None, [])}}))
        s = res.songs[0].song
        i = s.instruments[events(s, "V1", "Select")[0].name]
        self.assertEqual((i.pws_from, i.pws_to), (10, 68))
        self.assertTrue(any("sweep speed" in x for x in res.report.approximated))
        ins = [instrument(228, 3, "rel", c64=(0x04, 0, 0x00, 0xF0, 0x800, 0, 0), macros=[(2, [4, 8], 0, None, 1)])]
        res = load(build(chips=(SID6581,), instruments=ins, patterns={(0, 0, 0): {0: (C4, 0, None, [])}}))
        self.assertTrue(any("relative pulse width" in x for x in res.report.dropped))

    def test_the_sid_with_pcm_skips_its_fourth_channel(self):
        res = load(build(chips=(SID_PCM,), patterns={(0, 0, 0): {0: (C4, 0, None, [])}, (0, 3, 0): {0: (C4, 0, None, [])}}))
        self.assertEqual(res.songs[0].song.voices, 3)
        self.assertTrue(any("channel 4" in x and "software PCM" in x for x in res.report.dropped), res.report.dropped)

    def test_game_boy_pulses_only(self):
        ins = [instrument(228, 2, "gbp", gb=(0xF0, 0), macros=[(2, [1], None, None, 1)])]
        pats = {(0, c, 0): {0: (C4, 0, None, [])} for c in range(4)}
        res = load(build(chips=(GB,), instruments=ins, patterns=pats))
        s = res.songs[0].song
        self.assertEqual(s.voices, 2)
        self.assertEqual(s.instruments[events(s, "V1", "Select")[0].name].pw, 25)
        text = "\n".join(res.report.dropped)
        self.assertIn("wave channel", text)
        self.assertIn("noise channel", text)

    def test_several_chips_get_consecutive_voices(self):
        pats = {(0, 0, 0): {0: (C4, 0, None, [])}, (0, 5, 0): {0: (E4, 0, None, [])}}
        res = load(build(chips=(NES, SID6581), patterns=pats))
        s = res.songs[0].song
        self.assertEqual((s.voices, s.dacs), (7, 1))
        self.assertTrue(notes(s, "V5"))                                              # the SID's first voice follows the NES's four

    def test_an_unsupported_chip_is_named_and_a_file_with_nothing_playable_is_refused(self):
        pats = {(0, 0, 0): {0: (C4, 0, None, [])}, (0, 5, 0): {0: (E4, 0, None, [])}}
        res = load(build(chips=(NES, YM), patterns=pats))
        self.assertTrue(any("YM2612" in x and "channel 6" in x for x in res.report.dropped), res.report.dropped)
        with self.assertRaisesRegex(fur.FurnaceError, "no channel in this file can be imported"):
            load(build(chips=(YM,), patterns={(0, 0, 0): {0: (C4, 0, None, [])}}))

    def test_subsongs_are_picked_by_number_or_name(self):
        subs = [Sub("first"), Sub("second", speeds=(3,))]
        pats = {(0, 0, 0): {0: (C4, 0, None, [])}, (1, 0, 0): {0: (D4, 0, None, [])}}
        res = load(build(subs=subs, patterns=pats))
        self.assertEqual([x.name for x in res.songs], ["first", "second"])
        self.assertEqual([n.dur for n in notes(res.songs[1].song)], [16 * 12])                  # speed 3: 12 ticks a row
        for pick in ("2", "second"):
            self.assertEqual([x.name for x in load(build(subs=subs, patterns=pats), song=pick).songs], ["second"])
        with self.assertRaisesRegex(fur.FurnaceError, "no song"):
            load(build(subs=subs, patterns=pats), song="third")


# ------------------------------------------------------------------ the command line
class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_import_adds_songs_and_prints_the_report(self):
        f = self.write("tune.fur", simple({0: (C4, 0, None, [(0x04, 0x47)])}, compress=True))
        pkg = self.dir / "out.csp"
        rc, out, err = run("import", pkg, f)
        self.assertEqual(rc, 0, err)
        self.assertIn("added song 'tune'", out)
        self.assertIn("vibrato (04xy)", out)
        self.assertEqual(list(pk.load(pkg).songs), ["tune"])

    def test_several_subsongs_get_their_names_and_song_picks_one(self):
        f = self.write("multi.fur", build(subs=[Sub("a b"), Sub("c")], patterns={(0, 0, 0): {0: (C4, 0, None, [])}, (1, 0, 0): {0: (D4, 0, None, [])}}))
        pkg = self.dir / "m.csp"
        rc, out, err = run("import", pkg, f)
        self.assertEqual(rc, 0, err)
        self.assertEqual(list(pk.load(pkg).songs), ["multi_a_b", "multi_c"])
        pkg2 = self.dir / "m2.csp"
        rc, out, err = run("import", pkg2, f, "--song", "2", "--id", "only", "--report", self.dir / "r.json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(list(pk.load(pkg2).songs), ["only"])
        self.assertIn("approximated", json.loads((self.dir / "r.json").read_text()))

    def test_a_file_is_found_by_its_start_even_without_the_ending(self):
        f = self.write("tune.bin", simple({0: (C4, 0, None, [])}))
        rc, out, err = run("import", self.dir / "o.csp", f)
        self.assertEqual(rc, 0, err)

    def test_errors_are_one_line_messages(self):
        old = bytearray(simple({0: (C4, 0, None, [])}))
        old[16:18] = struct.pack("<H", 100)
        rc, out, err = run("import", self.dir / "o.csp", self.write("old.fur", bytes(old)))
        self.assertNotEqual(rc, 0)
        self.assertIn("older than this importer reads", err)
        rc, out, err = run("import", self.dir / "o.csp", self.write("junk.fur", b"nope nope nope"))
        self.assertNotEqual(rc, 0)
        self.assertIn("not a Furnace module", err)

    def test_dmf_is_refused_with_the_way_out(self):
        rc, out, err = run("import", self.dir / "o.csp", self.write("song.dmf", b"x" * 64))
        self.assertNotEqual(rc, 0)
        self.assertIn(".dmf", err)
        self.assertIn(".fur", err)


if __name__ == "__main__":
    unittest.main()
