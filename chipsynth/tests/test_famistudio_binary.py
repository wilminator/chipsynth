# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""FamiStudio .fms import.  Every file here is built by `write_fms` below, a small writer made from the format as
FamiStudio's ProjectSaveBuffer / Serialize methods define it (MIT, read only): no real project is used or committed.
The writer is the only place the layout is spelled out for the tests, so a test that passes shows the reader and the
writer agree on the layout read from the source, not that a real FamiStudio file reads (that needs the real program)."""
import contextlib
import io
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from chipsynth import __main__ as cli, package as pk
from chipsynth.formats import famistudio_binary as fms, famistudio_text as fam
from chipsynth.tests.test_famistudio_import import TINY

# ------------------------------------------------------------------ the writer


class W:
    """ProjectSaveBuffer: little endian numbers, UTF-16 strings with an i32 byte count (-1 for empty), counted arrays."""

    def __init__(self):
        self.b = bytearray()

    def bool(self, x):
        self.b += struct.pack("<B", 1 if x else 0)

    def u8(self, x):
        self.b += struct.pack("<B", x)

    def i8(self, x):
        self.b += struct.pack("<b", x)

    def u16(self, x):
        self.b += struct.pack("<H", x)

    def i32(self, x):
        self.b += struct.pack("<i", x)

    def f32(self, x):
        self.b += struct.pack("<f", x)

    def s(self, text):
        if not text:
            self.i32(-1)
        else:
            raw = text.encode("utf-16-le")
            self.i32(len(raw))
            self.b += raw

    def bytes(self, data):
        self.i32(len(data))
        self.b += data

    def shorts(self, values):
        if values is None:
            self.i32(-1)
        else:
            self.i32(len(values))
            self.b += struct.pack("<%dh" % len(values), *values)

    def ints(self, values):
        if values is None:
            self.i32(-1)
        else:
            self.i32(len(values))
            for x in values:
                self.i32(x)


def export_settings(w):
    """Version 19: twelve export configurations (with some content, so reading past them is tested)."""
    w.i32(5)                                         # audio
    for t in ("wav", "44100", "128", "loop"):
        w.s(t)
    w.i32(2), w.i32(120), w.i32(0)
    w.bool(True), w.bool(False), w.bool(True)
    w.i32(1)
    w.i32(5), w.i32(0), w.bool(True), w.i32(0), w.i32(0), w.i32(0)
    w.i32(-1)                                        # video
    for t in ("piano", "1080p", "60", "128", "8000"):
        w.s(t)
    for n in (1, 0, -1, 2, 2):
        w.i32(n)
    for t in ("ff0000", "wide", "zoom"):
        w.s(t)
    w.i32(-1), w.s("persp"), w.bool(True), w.bool(False)
    w.i32(0)
    for t in ("n", "a", "c", "nsf", "m"):             # nsf
        w.s(t)
    w.i32(1), w.i32(7), w.bool(True)
    for t in ("fds", "n", "a", "m"):                  # rom / fds
        w.s(t)
    w.i32(0)
    w.i32(-1), w.bool(True), w.bool(True), w.i32(24), w.s("mode")        # midi
    w.i32(1), w.i32(5), w.i32(0), w.i32(1), w.i32(2)
    w.i32(-1)                                        # vgm
    for t in ("title", "game", "sys", "me", "date", "by", "notes"):
        w.s(t)
    w.bool(True)
    w.bool(False), w.i32(1), w.i32(4), w.bool(True)                       # famistudio text
    w.i32(1), w.i32(4), w.bool(False)                                     # famitracker text
    for _ in range(2):                                                    # famistudio music, then sfx
        w.s("ca65"), w.bool(True), w.s("{project}"), w.s("dmc"), w.s("mode"), w.bool(False), w.bool(True)
        w.i32(1), w.i32(3), w.bool(True)
        w.s("fmt"), w.s("mode"), w.bool(True)
        w.i32(1), w.i32(3), w.bool(True)
    # (the loop above wrote music, sfx, music, sfx: famistudio music/sfx, then famitone2 music/sfx)


def expansion_bytes(exp, v):
    """What an expansion instrument writes between its colour/expansion and its envelope mask."""
    w = W()
    if exp == 3:                                     # FDS
        w.u8(1), w.u8(0), w.u8(5), w.u16(300), w.u8(9), w.u8(2)
        if v >= 18:
            w.u8(1)
        if v >= 16:
            w.bool(True), w.u8(1), w.u8(2)
        w.i32(128), w.i32(0), w.bool(True), w.shorts([1, -2, 3])
    elif exp == 5:                                   # N163
        w.u8(0), w.u8(16), w.u8(0), w.u8(1), w.i32(128), w.i32(0), w.bool(True), w.shorts(None)
        if v >= 17:
            w.bool(True)
    elif exp == 6:                                   # S5B
        if v >= 16:
            w.bool(True), w.i8(3), w.u8(2), w.u16(1000)
    elif exp == 2:                                   # VRC7
        w.u8(0), w.b.extend(bytes(range(8)))
        if v >= 19:
            w.bool(True), w.bool(False)
    elif exp == 7:                                   # EPSM
        w.u8(1), w.b.extend(bytes(31))
        if v >= 16:
            w.bool(True), w.i8(3), w.u8(0), w.u16(4000)
    elif exp == 1:                                   # VRC6
        w.u8(1)
    return bytes(w.b)


def write_fms(p, version=19, payload_size=None):
    """A .fms file from a dict model `p` (see tiny_project for the shape)."""
    w = W()
    v = version
    w.i32(100)                                       # next unique id
    for _ in range(4):
        w.bool(True)                                 # sort flags (v15+)
    w.s(p.get("name", "")), w.s(p.get("author", "")), w.s(p.get("copyright", ""))
    w.i32(p.get("mask", 0)), w.i32(p.get("n163", 1)), w.i32(p.get("tempo", 0)), w.bool(p.get("pal", False))
    if v >= 19:
        export_settings(w)
    if v >= 17:
        w.i32(p.get("tuning", 440))
    if v >= 16:
        w.i32(2)                                     # two folders
        w.i32(0), w.s("Songs"), w.bool(True)
        w.i32(1), w.s("Leads"), w.bool(False)
        w.bool(False), w.bool(True), w.bool(False)   # sound engine options
        w.bool(True), w.i32(30)                      # override bass cutoff
        w.i32(0b101)                                 # mixer overrides for 2A03 and Vrc7
        for _ in range(2):
            w.f32(1.5), w.f32(-5.0), w.i32(14000)
            if v >= 18:
                w.i32(5)
    w.i32(len(p.get("samples", [])))
    for smp in p.get("samples", []):
        write_sample(w, smp, v)
    w.i32(len(p.get("instruments", [])))
    for inst in p.get("instruments", []):
        write_instrument(w, inst, v)
    w.i32(len(p.get("arps", [])))
    for arp in p.get("arps", []):
        w.i32(arp["id"]), w.s(arp["name"]), w.i32(0x123456)
        if v >= 16:
            w.s("")
        write_envelope(w, *arp["env"])
    w.i32(len(p["songs"]))
    for song in p["songs"]:
        write_song(w, song, v)
    raw = bytes(w.b)
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = comp.compress(raw) + comp.flush()
    size = len(raw) if payload_size is None else payload_size
    return b"FMS!" + struct.pack("<ii", v, size) + body


def write_sample(w, s, v):
    w.i32(s["id"]), w.s(s["name"])
    w.bool(s.get("wav", False))
    if s.get("wav"):
        w.i32(44100), w.shorts([0, 100, -100, 50])
    else:
        w.bytes(s["data"])
    w.i32(0x445566)
    if v >= 15:
        w.i32(0)
    if v >= 16:
        w.s("")
    w.i32(s.get("rate", 15)), w.i32(15), w.i32(s.get("volume", 100)), w.i32(s.get("padding", 0))
    w.bool(s.get("reverse", False)), w.bool(s.get("trim", False)), w.bool(False)
    for pair in s.get("envelope", [(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.0)]):
        w.i32(pair[0]), w.f32(pair[1])
    w.s("source.wav")
    w.f32(s.get("fine", 1.0)), w.i32(s.get("initial", 32))


def write_envelope(w, length, loop, release, relative, values):
    w.i32(length), w.i32(loop), w.i32(release), w.bool(relative)
    w.i32(len(values))
    w.b.extend(struct.pack("<%db" % len(values), *values))


def write_instrument(w, inst, v):
    w.i32(inst["id"]), w.s(inst["name"]), w.i32(0x778899)
    exp = inst.get("exp", 0)
    w.i32(exp)
    w.b.extend(expansion_bytes(exp, v))
    mask = 0
    for i in inst.get("envs", {}):
        mask |= 1 << i
    w.u16(mask)
    for i in sorted(inst.get("envs", {})):
        length, loop, release, relative, values = inst["envs"][i]
        write_envelope(w, length, loop, release, relative, list(values) + [0] * (256 - len(values)))
    if v >= 16:
        w.s("Leads" if inst["name"] == "Lead" else "")
    if exp == 0:
        maps = inst.get("maps", [])
        w.i32(len(maps))
        for note, _, _, _ in maps:
            w.i32(note)
        for _, sample, loop, pitch in maps:
            w.i32(sample), w.bool(loop), w.i32(pitch), w.bool(False), w.i32(32)


FX_BITS = {"volume": 0, "vibrato": 1, "fine": 3, "speed": 4, "duty": 7, "note_delay": 8, "cut_delay": 9, "volume_slide": 10, "delta": 11, "phase": 12, "env": 13}


def write_note(w, n, v):
    val = n["val"]
    w.u8(val), w.u8(n.get("flags", 0))
    musical = val not in (0, 0x80, 0xFF)
    if musical:
        w.u8(n.get("slide", 0)), w.i32(n.get("inst", -1))
        w.u16(n.get("dur", 1)), w.u16(n.get("rel", 0))
    fx = n.get("fx", {})
    mask = 0
    for name in fx:
        mask |= 1 << FX_BITS[name]
    if "vibrato" in fx:
        mask |= 1 << 2
    w.u16(mask)
    if "volume" in fx:
        w.u8(fx["volume"])
    if "vibrato" in fx:
        w.u8(fx["vibrato"])
    if "speed" in fx:
        w.u8(fx["speed"])
    if "fine" in fx:
        w.i8(fx["fine"])
    if "duty" in fx:
        w.u8(fx["duty"])
    if "note_delay" in fx:
        w.u8(fx["note_delay"])
    if "cut_delay" in fx:
        w.u8(fx["cut_delay"])
    if "volume" in fx and "volume_slide" in fx:
        w.u8(fx["volume_slide"])
    if "delta" in fx:
        w.u8(fx["delta"])
    if "phase" in fx:
        w.u8(fx["phase"])
    if v >= 16 and "env" in fx:
        w.u16(fx["env"])
    w.i32(n.get("arp", -1))


def write_song(w, s, v):
    w.i32(s["id"]), w.i32(s["plen"]), w.i32(s["length"]), w.i32(s["blen"]), w.s(s["name"])
    w.i32(s.get("ft_tempo", 150)), w.i32(s.get("ft_speed", 6)), w.i32(0xABCDEF)
    w.i32(s.get("loop", 0)), w.i32(s.get("note_len", 10)), w.ints(s.get("groove", [s.get("note_len", 10)]))
    if v >= 19:
        w.i32(0)
    for i in range(s["length"]):
        c = s.get("custom", {}).get(i)
        w.bool(c is not None)
        plen, nlen, blen, groove = c if c else (0, 0, 0, None)
        w.i32(plen), w.i32(nlen), w.i32(blen), w.ints(groove), w.i32(0)
    if v >= 16:
        w.s("Songs")
    for ch in s["channels"]:
        w.i32(s["id"]), w.i32(len(ch["patterns"])), w.i32(ch["type"])
        for pid, name, notes in ch["patterns"]:
            w.i32(pid), w.s(name), w.i32(ch["type"]), w.i32(0x111111), w.i32(s["id"])
            w.i32(len(notes))
            for time, note in notes:
                w.b.extend(struct.pack("<h", time))
                write_note(w, note, v)
        for p in range(256):
            w.i32(ch.get("instances", {}).get(p, -1))


# note values: C0 = 1
C1, C2, C3, C4, E4, G4, B1 = 13, 25, 37, 49, 53, 56, 24
STOP = 0
SQ1, SQ2, TRI, NOISE, DPCM, V6A, V6B, V6SAW = range(8)


def tiny_project():
    """tiny.txt, written as a project (with the VRC6 expansion switched on so its saw channel exists)."""
    def pattern(pid, name, notes):
        return (pid, name, notes)

    def note(val, dur=10, inst=-1, **fx):
        return dict(val=val, dur=dur, inst=inst, fx=fx)

    sq1 = [pattern(10, "A", [(0, note(C4, 20, 2)), (20, note(E4, 20, volume=9)), (40, dict(val=STOP)), (50, note(G4, 30, 4))])]
    chans = [
        dict(type=SQ1, patterns=sq1, instances={0: 10, 1: 10}),
        dict(type=SQ2, patterns=[]),
        dict(type=TRI, patterns=[pattern(11, "T", [(0, note(C2, 80, 2))])], instances={0: 11}),
        dict(type=NOISE, patterns=[pattern(12, "N", [(0, note(C1, 10, 2)), (40, note(B1, 10, 2))])], instances={1: 12}),
        dict(type=DPCM, patterns=[pattern(13, "D", [(0, note(C4, 10, 3))])], instances={0: 13}),
        dict(type=V6A, patterns=[]),
        dict(type=V6B, patterns=[]),
        dict(type=V6SAW, patterns=[pattern(14, "V", [(0, note(C3, 10))])], instances={0: 14}),
    ]
    return dict(
        name="Tiny", author="Test Author", mask=1,
        samples=[dict(id=1, name="Kick Drum", data=bytes.fromhex("aaaaaa55aaaa55aa"), initial=32)],
        instruments=[
            dict(id=2, name="Lead", envs={0: (6, 5, -1, False, [15, 12, 10, 8, 6, 6]), 3: (1, -1, -1, False, [1])}),
            dict(id=3, name="Hit", maps=[(C4, 1, False, 12)]),
            dict(id=4, name="Arp", envs={1: (3, 0, -1, False, [0, 4, 7])}),
        ],
        songs=[dict(id=5, name="Main Theme", plen=80, length=2, blen=40, loop=1, note_len=10, groove=[10], channels=chans)])


def tiny_fms(version=19, **changes):
    p = tiny_project()
    p.update(changes)
    return write_fms(p, version)


def simple_project(**song):
    """One square channel, one instrument, one song of one pattern of four notes (FamiStudio tempo, no expansions)."""
    notes = [(i * 10, dict(val=C4 + i, dur=10, inst=1)) for i in range(4)]
    s = dict(id=5, name="S", plen=80, length=1, blen=40, loop=-1, note_len=10, groove=[10], channels=[
        dict(type=SQ1, patterns=[(10, "p", notes)], instances={0: 10}), dict(type=SQ2, patterns=[]), dict(type=TRI, patterns=[]),
        dict(type=NOISE, patterns=[]), dict(type=DPCM, patterns=[])])
    s.update(song)
    return dict(name="Simple", instruments=[dict(id=1, name="I")], songs=[s])


# ------------------------------------------------------------------ the tests


def result_core(res):
    """What must be the same however a project reached the importer."""
    from chipsynth import song as sg
    imp = res.songs[0]
    s = imp.song
    tracks = {k: v for k, v in s.tracks.items() if any(not isinstance(e, sg.Rest) for e in v)}      # an empty channel: no track or only rests
    return dict(title=s.title, bpm=s.bpm, time=s.time, tracks=tracks, instruments=s.instruments,
                samples={k: (v.rate, v.tracks) for k, v in imp.samples.items()})


class HeaderTests(unittest.TestCase):
    def test_the_magic_and_the_supported_versions(self):
        self.assertTrue(fms.looks_like(tiny_fms()))
        self.assertFalse(fms.looks_like(b"GTS5"))
        self.assertFalse(fms.looks_like(b""))
        self.assertEqual((fms.OLDEST, fms.NEWEST), (15, 19))

    def test_the_magic_is_the_u32_from_famistudio_little_endian(self):
        self.assertEqual(tiny_fms()[:4], struct.pack("<I", 0x21534D46))

    def test_a_newer_project_version_is_refused_with_the_way_out(self):
        data = bytearray(tiny_fms())
        data[4:8] = struct.pack("<i", 20)
        with self.assertRaises(fms.FamiStudioError) as cm:
            fms.load(bytes(data))
        self.assertIn("newer", str(cm.exception))
        self.assertIn("Export > FamiStudio Text", str(cm.exception))

    def test_an_older_project_version_is_refused_with_the_release_and_the_way_out(self):
        data = bytearray(tiny_fms())
        data[4:8] = struct.pack("<i", 14)
        with self.assertRaises(fms.FamiStudioError) as cm:
            fms.load(bytes(data))
        self.assertIn("older", str(cm.exception))
        self.assertIn("FamiStudio 4.0", str(cm.exception))
        data[4:8] = struct.pack("<i", 0)
        with self.assertRaises(fms.FamiStudioError):
            fms.load(bytes(data))

    def test_not_a_project_and_too_short(self):
        for bad in (b"", b"FMS!", b"FMS!\x13\x00\x00\x00", b"hello world, not a project", b"GTS5" + bytes(20)):
            with self.assertRaises(fms.FamiStudioError):
                fms.load(bad)

    def test_a_payload_that_does_not_decompress_or_is_short_is_an_error(self):
        data = tiny_fms()
        with self.assertRaises(fms.FamiStudioError):
            fms.load(data[:12] + bytes([0xFF] * 40))
        with self.assertRaises(fms.FamiStudioError) as cm:
            fms.load(write_fms(tiny_project(), 19, payload_size=10 ** 7))
        self.assertIn("shorter", str(cm.exception))
        with self.assertRaises(fms.FamiStudioError):
            fms.load(write_fms(tiny_project(), 19, payload_size=0))
        with self.assertRaises(fms.FamiStudioError):
            fms.load(write_fms(tiny_project(), 19, payload_size=-5))

    def test_every_cut_of_a_project_is_an_error_not_a_crash(self):
        """A file cut short is refused; one cut only in the deflate stream's last bytes (all the data is there) may still read."""
        data = tiny_fms()
        for cut in range(0, len(data), 7):
            try:
                fms.load(data[:cut])
            except fms.FamiStudioError:
                continue
            self.assertGreater(cut, len(data) - 16, cut)

    def test_every_cut_of_the_payload_is_an_error_not_a_crash(self):
        """The payload ends early but its header says it is whole: the reader must stop at the end."""
        p = tiny_project()
        w_full = write_fms(p, 19)
        raw = zlib.decompressobj(-15).decompress(w_full[12:])
        for cut in range(0, len(raw), 11):
            comp = zlib.compressobj(9, zlib.DEFLATED, -15)
            body = comp.compress(raw[:cut]) + comp.flush()
            with self.assertRaises(fms.FamiStudioError):
                fms.load(b"FMS!" + struct.pack("<ii", 19, cut) + body)

    def test_a_corrupt_count_is_refused(self):
        raw = bytearray(zlib.decompressobj(-15).decompress(tiny_fms()[12:]))
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        # the first string (the project name) claims a million bytes
        raw[8:12] = struct.pack("<i", 1000000)
        body = comp.compress(bytes(raw)) + comp.flush()
        with self.assertRaises(fms.FamiStudioError):
            fms.load(b"FMS!" + struct.pack("<ii", 19, len(raw)) + body)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.res = fms.load(tiny_fms())
        self.imp = self.res.songs[0]

    def test_the_project_is_the_same_music_as_its_text_export(self):
        text = fam.load(TINY)
        self.assertEqual(result_core(self.res), result_core(text))

    def test_the_report_names_the_format_version_and_what_was_left_out(self):
        text = "\n".join(self.res.report.lines())
        self.assertIn("FamiStudio project file, format version 19 (FamiStudio 4.5)", text)
        self.assertIn("VRC6Saw", text)
        self.assertIn("expansion audio (VRC6)", text)

    def test_the_same_losses_as_the_text_importer_is_reported(self):
        text = fam.load(TINY).report
        for line in text.approximated:
            self.assertIn(line, self.res.report.approximated)

    def test_every_supported_version_reads_the_same_project(self):
        want = result_core(self.res)
        for v in range(fms.OLDEST, fms.NEWEST + 1):
            got = fms.load(tiny_fms(v))
            self.assertEqual(result_core(got), want, "version %d" % v)
            self.assertIn("format version %d (FamiStudio %s)" % (v, fms.RELEASES[v]), "\n".join(got.report.lines()))

    def test_expansion_instruments_are_read_past_in_every_version(self):
        for v in range(fms.OLDEST, fms.NEWEST + 1):
            p = tiny_project()
            p["instruments"] = [dict(id=20 + e, name="X%d" % e, exp=e, envs={0: (2, -1, -1, False, [15, 3])}) for e in (1, 2, 3, 5, 6, 7)] + p["instruments"]
            got = fms.load(write_fms(p, v))
            self.assertEqual(result_core(got), result_core(self.res), "version %d" % v)

    def test_song_names_and_the_song_choice(self):
        p = tiny_project()
        second = dict(p["songs"][0], id=6, name="Second")
        p["songs"].append(second)
        data = write_fms(p)
        self.assertEqual(fms.song_names(data), ["Main Theme", "Second"])
        self.assertEqual(len(fms.load(data).songs), 2)
        self.assertEqual([s.name for s in fms.load(data, song="Second").songs], ["Second"])
        self.assertEqual([s.name for s in fms.load(data, song="1").songs], ["Main Theme"])
        with self.assertRaises(fms.FamiStudioError):
            fms.load(data, song="Nope")

    def test_a_project_with_no_songs_is_refused(self):
        with self.assertRaises(fms.FamiStudioError):
            fms.load(write_fms(dict(name="x", songs=[])))

    def test_pal_and_the_region_override(self):
        base = simple_project()
        ntsc = fms.load(write_fms(base)).songs[0].song.bpm
        pal = fms.load(write_fms(dict(base, pal=True))).songs[0].song.bpm
        self.assertLess(pal, ntsc)
        self.assertEqual(fms.load(write_fms(base), region="pal").songs[0].song.bpm, pal)
        self.assertIn("PAL", "\n".join(fms.load(write_fms(dict(base, pal=True))).report.lines()))

    def test_tuning_is_reported_from_version_17_only(self):
        p = dict(simple_project(), tuning=432)
        self.assertTrue(any("432" in m for m in fms.load(write_fms(p, 17)).report.approximated))
        self.assertTrue(any("432" in m for m in fms.load(write_fms(p, 19)).report.approximated))
        self.assertFalse(any("Hz" in m for m in fms.load(write_fms(p, 16)).report.approximated))

    def test_famitracker_tempo_uses_speed_and_tempo(self):
        p = dict(simple_project(), tempo=1)
        p["songs"][0].update(plen=16, blen=4, ft_tempo=150, ft_speed=6)
        p["songs"][0]["channels"][0]["patterns"] = [(10, "p", [(0, dict(val=C4, dur=1, inst=1))])]
        song = fms.load(write_fms(p)).songs[0].song
        self.assertEqual(song.bpm, 150)
        from chipsynth import song as sg
        first = next(e for e in song.tracks["V1"] if isinstance(e, sg.Note))
        self.assertEqual(first.dur, 24)

    def test_custom_pattern_length_moves_the_next_pattern(self):
        p = tiny_project()
        p["songs"][0]["custom"] = {0: (40, 10, 40, [10])}
        res = fms.load(write_fms(p))
        from chipsynth import mml
        t = res.songs[0].song.compile(dict(mml.builtin_library(), _mem=res.songs[0].samples))
        self.assertEqual(t.length, (4 + 8) * 24)

    def test_groove_is_averaged_and_reported(self):
        p = simple_project()
        p["songs"][0].update(note_len=5, groove=[6, 5], plen=40, blen=20)
        self.assertTrue(any("groove" in m for m in fms.load(write_fms(p)).report.approximated))

    def test_effects_the_importer_cannot_play_are_reported(self):
        p = simple_project()
        notes = p["songs"][0]["channels"][0]["patterns"][0][2]
        notes[0][1]["fx"] = {"volume": 7, "fine": -3, "vibrato": 0x34}
        notes[1][1]["slide"] = G4
        notes[2][1]["fx"] = {"note_delay": 2, "phase": 1, "env": 99}
        notes[3][1]["flags"] = 1
        res = fms.load(write_fms(p))
        text = "\n".join(res.report.dropped)
        for what in ("FinePitch", "VibratoSpeed", "slide", "NoteDelay", "PhaseReset"):
            self.assertIn(what, text)

    def test_note_volume_and_an_unnamed_instrument_carry(self):
        from chipsynth import song as sg
        p = simple_project()
        p["songs"][0]["channels"][0]["patterns"][0][2][1][1]["fx"] = {"volume": 7}
        song = fms.load(write_fms(p)).songs[0].song
        self.assertIn(sg.Volume(value=7), song.tracks["V1"])

    def test_two_patterns_with_the_same_name_do_not_clash(self):
        p = simple_project()
        ch = p["songs"][0]["channels"][0]
        ch["patterns"].append((11, "p", [(0, dict(val=E4, dur=10, inst=1))]))
        p["songs"][0].update(length=2)
        ch["instances"] = {0: 10, 1: 11}
        from chipsynth import song as sg
        track = fms.load(write_fms(p)).songs[0].song.tracks["V1"]
        self.assertEqual(sum(isinstance(e, sg.Note) for e in track), 5)      # four notes, then the other pattern's one

    def test_a_channel_type_that_does_not_exist_is_refused(self):
        p = simple_project()
        p["songs"][0]["channels"][0]["type"] = 99
        with self.assertRaises(fms.FamiStudioError):
            fms.load(write_fms(p))

    def test_a_song_longer_than_famistudio_allows_is_refused(self):
        with self.assertRaises(fms.FamiStudioError):
            fms.load(write_fms(simple_project(length=300)))


class SampleTests(unittest.TestCase):
    def sample_project(self, **smp):
        base = dict(id=1, name="S1", data=bytes([0x55, 0xAA, 0x0F]))
        base.update(smp)
        notes = [(0, dict(val=C4, dur=10, inst=3))]
        chans = [dict(type=t, patterns=[]) for t in (SQ1, SQ2, TRI, NOISE)] + [
            dict(type=DPCM, patterns=[(10, "d", notes)], instances={0: 10})]
        return dict(name="D", samples=[base], instruments=[dict(id=3, name="Kit", maps=[(C4, 1, False, 9)])],
                    songs=[dict(id=5, name="S", plen=80, length=1, blen=40, channels=chans)])

    def test_a_plain_dmc_sample_comes_through_with_its_rate(self):
        res = fms.load(write_fms(self.sample_project()))
        smp = next(iter(res.songs[0].samples.values()))
        self.assertEqual(smp.tracks, [[bytes([0x55, 0xAA, 0x0F])]])
        self.assertEqual(smp.rate, 9)

    def test_reverse_bits_is_applied(self):
        res = fms.load(write_fms(self.sample_project(reverse=True)))
        smp = next(iter(res.songs[0].samples.values()))
        self.assertEqual(smp.tracks, [[bytes([0xAA, 0x55, 0xF0])]])

    def test_padding_modes_fill_with_55_or_aa_by_the_last_bit(self):
        for padding, size in ((1, 16), (2, 17), (6, 1)):
            res = fms.load(write_fms(self.sample_project(data=bytes([0x01] * 3), padding=padding)))
            smp = next(iter(res.songs[0].samples.values()))
            self.assertEqual(len(smp.tracks[0][0]), size, padding)
        res = fms.load(write_fms(self.sample_project(data=bytes([0x80] * 3), padding=1)))
        self.assertEqual(next(iter(res.songs[0].samples.values())).tracks[0][0][3:], bytes([0xAA] * 13))
        res = fms.load(write_fms(self.sample_project(data=bytes([0x01] * 3), padding=1)))
        self.assertEqual(next(iter(res.songs[0].samples.values())).tracks[0][0][3:], bytes([0x55] * 13))

    def test_a_sample_with_processing_is_left_out_and_said_so(self):
        for smp in (dict(wav=True), dict(volume=80), dict(rate=12), dict(trim=True), dict(fine=1.2),
                    dict(envelope=[(0, 1.0), (1, 0.5), (2, 1.0), (3, 1.0)])):
            res = fms.load(write_fms(self.sample_project(**smp)))
            self.assertFalse(res.songs[0].samples, smp)
            self.assertTrue(any("S1" in m and "FamiStudio Text" in m for m in res.report.dropped), (smp, res.report.dropped))

    def test_a_clean_sample_is_not_reported_as_left_out(self):
        res = fms.load(write_fms(self.sample_project(initial=40)))
        self.assertFalse(any("S1" in m and "left out" in m for m in res.report.dropped))
        self.assertTrue(any("initial DMC value" in m for m in res.report.approximated))


class TextVersionTests(unittest.TestCase):
    """The small item folded in: the text importer says what FamiStudio itself would do with the file's Version."""

    def lines(self, version):
        text = TINY.replace('Version="4.5.3"', 'Version="%s"' % version)
        return fam.load(text).report

    def test_a_4_5_file_has_only_the_fact(self):
        r = self.lines("4.5.3")
        self.assertIn("written by FamiStudio 4.5.3", r.notes)
        self.assertFalse(any("refuse" in m for m in r.notes + r.approximated))

    def test_another_4_x_is_noted_as_refused_by_4_5(self):
        r = self.lines("4.2.0")
        self.assertTrue(any("4.5 would refuse" in m for m in r.notes))
        self.assertFalse(any("not a 4.x" in m for m in r.approximated))

    def test_a_version_that_is_not_4_x_is_reported_as_approximate_and_still_read(self):
        for v in ("3.9.0", "5.0.0", "2.1.0"):
            r = fam.load(TINY.replace('Version="4.5.3"', 'Version="%s"' % v))
            self.assertTrue(any("not a 4.x" in m and v in m for m in r.report.approximated), v)
            self.assertEqual(len(r.songs), 1)

    def test_a_file_with_no_version_says_nothing_about_it(self):
        r = fam.load(TINY.replace(' Version="4.5.3"', ""))
        self.assertFalse(any("refuse" in m or "4.x" in m for m in r.report.notes + r.report.approximated))
        self.assertFalse(any(m.startswith("written by") for m in r.report.notes))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main([str(a) for a in argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_import_by_the_fms_ending(self):
        f = self.tmp / "tiny.fms"
        f.write_bytes(tiny_fms())
        rc, out, err = self.run_cli("import", self.tmp / "p.csp", f, "--report", self.tmp / "r.json")
        self.assertEqual(rc, 0, err)
        self.assertIn("added song 'tiny'", out)
        self.assertIn("format version 19", out)
        self.assertEqual(list(pk.load(self.tmp / "p.csp").songs), ["tiny"])
        self.assertIn("VRC6Saw", "\n".join(json.loads((self.tmp / "r.json").read_text())["dropped"]))

    def test_import_by_the_magic_when_the_name_says_nothing(self):
        f = self.tmp / "project.dat"
        f.write_bytes(tiny_fms())
        rc, out, err = self.run_cli("import", self.tmp / "p.csp", f)
        self.assertEqual(rc, 0, err)
        self.assertIn("added song 'project'", out)

    def test_two_songs_become_two_ids_and_song_picks_one(self):
        p = tiny_project()
        p["songs"].append(dict(p["songs"][0], id=6, name="Second"))
        f = self.tmp / "two.fms"
        f.write_bytes(write_fms(p))
        rc, out, err = self.run_cli("import", self.tmp / "p.csp", f)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(pk.load(self.tmp / "p.csp").songs), ["two_Main_Theme", "two_Second"])
        rc, out, err = self.run_cli("import", self.tmp / "q.csp", f, "--song", "Second")
        self.assertEqual(rc, 0, err)
        self.assertEqual(list(pk.load(self.tmp / "q.csp").songs), ["two"])

    def test_a_newer_project_is_an_error_message_not_a_traceback(self):
        data = bytearray(tiny_fms())
        data[4:8] = struct.pack("<i", 25)
        f = self.tmp / "new.fms"
        f.write_bytes(bytes(data))
        rc, out, err = self.run_cli("import", self.tmp / "p.csp", f)
        self.assertNotEqual(rc, 0)
        self.assertIn("newer", err + out)
        self.assertFalse((self.tmp / "p.csp").exists())

    def test_a_text_file_still_imports_and_a_junk_txt_still_fails_clearly(self):
        (self.tmp / "t.txt").write_text(TINY)
        rc, out, err = self.run_cli("import", self.tmp / "p.csp", self.tmp / "t.txt")
        self.assertEqual(rc, 0, err)
        (self.tmp / "j.txt").write_text("hello\n")
        rc, out, err = self.run_cli("import", self.tmp / "q.csp", self.tmp / "j.txt")
        self.assertNotEqual(rc, 0)
        self.assertIn("not a FamiStudio file", err + out)


if __name__ == "__main__":
    unittest.main()
