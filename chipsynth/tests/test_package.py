# Copyright (C) 2026 Michael Allen Wilmes
"""The package file, PackageSet and the dump / build text (PLAN-CHIPSYNTH.md, C0b)."""
import dataclasses
import glob
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from chipsynth import dpcm, mml, package as pk, packagetext
from chipsynth.packageset import PackageSet
from chipsynth.song import Song
from chipsynth.tests.test_song import FEATURES, norm, bare, lib as any_lib

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).resolve().parent / "data"
TUNE = "tempo 120\ntitle Hello\nV1: l8 c d e f g a b >c\n"


def demo_package():
    pkg = pk.Package(name="demo", author="Mike", comment="all the demos")
    for f in sorted(glob.glob(str(DATA / "demo_songs" / "*.mml"))):
        pkg.add_song(Path(f).stem, Song.from_mml(Path(f).read_text()))
    return pkg


def kick(seed=1, name="kick"):
    t = np.arange(3000) / 44100.0
    return dpcm.convert(np.sin(2 * np.pi * (100 + seed * 40) * t) * np.exp(-t * 20), 44100, rate=12, name=name)


def drum_lib(**smp):
    lb = mml.builtin_library()
    lb["_mem"] = dict(smp)
    return lb


class CodecTests(unittest.TestCase):
    def test_save_load_save_is_byte_identical(self):
        b = pk.dumps(demo_package())
        self.assertEqual(pk.dumps(pk.loads(b)), b)

    def test_every_demo_song_plays_the_same_from_the_package(self):
        pkg = pk.loads(pk.dumps(demo_package()))
        for sid in pkg.songs:
            t0 = mml.parse((DATA / "demo_songs" / (sid + ".mml")).read_text())
            t1 = pkg.tune(sid)
            self.assertEqual(dataclasses.replace(t0, warnings=[], instruments={}),
                             dataclasses.replace(t1, warnings=[], instruments={}), sid)

    def test_a_song_with_every_feature_survives_the_file(self):
        lb = any_lib()
        lb["pat"]["drumpat"] = ("kick8 r8", 1, 1)
        lb["fx"]["bright"] = [("vib", "4,2")]
        song = Song.from_mml(FEATURES, lb)
        back = pk.decode_song(pk.encode_song(song))
        self.assertEqual(back, dataclasses.replace(song, warnings=back.warnings))
        self.assertEqual(norm(back.compile(lb)), norm(song.compile(lb)))

    def test_echo_floats_and_negative_numbers_are_exact(self):
        song = Song.from_mml("tune -37\necho 333ms,41,29,wide\nV1: c")
        back = pk.decode_song(pk.encode_song(song))
        self.assertEqual((back.detune, back.echo), (song.detune, song.echo))

    def test_varints(self):
        for n in (0, 1, 127, 128, 300, 2 ** 31, 2 ** 40):
            w = pk._W()
            w.u(n)
            self.assertEqual(pk._R(w.done()).u(), n)
        for n in (0, 1, -1, 63, -64, 1000, -1000, -2 ** 33):
            w = pk._W()
            w.s(n)
            self.assertEqual(pk._R(w.done()).s(), n)

    def test_text_is_utf8(self):
        song = Song.from_mml("title Café ♪\nV1: c")
        self.assertEqual(pk.decode_song(pk.encode_song(song)).title, "Café ♪")


class FileTests(unittest.TestCase):
    def test_not_a_package(self):
        with self.assertRaises(pk.PackageError):
            pk.loads(b"RIFFxxxxxxxxxxxx")

    def test_every_truncation_is_an_error_not_a_crash(self):
        b = pk.dumps(demo_package())
        for cut in list(range(0, 60)) + list(range(60, len(b), 997)):
            try:
                pk.loads(b[:cut])
            except pk.PackageError:
                pass
            except Exception as e:                                  # pragma: no cover
                self.fail("cut at %d raised %r" % (cut, e))

    def test_a_newer_major_version_is_refused_with_a_clear_message(self):
        b = bytearray(pk.dumps(demo_package()))
        b[4:6] = struct.pack("<H", pk.MAJOR + 1)
        with self.assertRaises(pk.PackageError) as cm:
            pk.loads(bytes(b))
        self.assertIn("update chipsynth", str(cm.exception))

    def test_a_newer_minor_version_and_unknown_chunks_are_read(self):
        pkg = demo_package()
        b = bytearray(pk.dumps(pkg))
        b[6:8] = struct.pack("<H", pk.MINOR + 3)
        b += b"XTRA" + struct.pack("<I", 5) + b"hello"
        back = pk.loads(bytes(b))
        self.assertEqual(list(back.songs), list(pkg.songs))

    def test_save_is_atomic_and_load_names_an_unnamed_package(self):
        d = Path(tempfile.mkdtemp())
        p = pk.Package()
        p.add_song("a", Song.from_mml(TUNE))
        pk.save(p, d / "x" / "forest.csp")
        self.assertEqual([f.name for f in (d / "x").iterdir()], ["forest.csp"])
        self.assertEqual(pk.load(d / "x" / "forest.csp").name, "forest")

    def test_unreadable_file(self):
        with self.assertRaises(pk.PackageError):
            pk.load(Path(tempfile.mkdtemp()) / "nope.csp")


class EmbedTests(unittest.TestCase):
    def test_library_things_a_song_uses_are_copied_in(self):
        lb = mml.builtin_library()
        lb["fx"]["wob"] = [("vib", "5,3")]
        lb["key"]["blues"] = (0, 3, 5, 6, 7, 10)
        pkg = pk.Package(name="p")
        song = Song.from_mml("V1: @flute fx wob key c blues 1 3 5\n", lb)
        self.assertEqual(song.instruments, {})
        pkg.add_song("s", song, lb)
        s = pkg.songs["s"]
        self.assertIn("flute", s.instruments)
        self.assertEqual(s.fx["wob"], [("vib", "5,3")])
        self.assertEqual(s.keys["blues"], (0, 3, 5, 6, 7, 10))
        # and it plays with no library at all
        back = pk.loads(pk.dumps(pkg))
        self.assertGreater(back.songs["s"].compile(mml.builtin_library()).length, 0)
        self.assertEqual(song.instruments, {})                      # the original song was not touched

    def test_samples_move_into_the_package(self):
        lb = drum_lib(kick=kick())
        pkg = pk.Package(name="p")
        pkg.add_song("a", Song.from_mml("V1: c\nD1: kick8 r8\n", lb), lb)
        self.assertIn("kick", pkg.samples)
        back = pk.loads(pk.dumps(pkg))
        t = back.tune("a", mml.builtin_library())                   # no sample folder, no memory: all in the file
        self.assertEqual(t.dac[0][0][1], "play")
        self.assertEqual(back.samples["kick"].tracks, kick().tracks)

    def test_same_sample_is_shared_and_a_different_one_with_the_same_name_is_renamed(self):
        pkg = pk.Package(name="p")
        for sid, smp in (("a", kick(1)), ("b", kick(1)), ("c", kick(2))):
            lb = drum_lib(kick=smp)
            pkg.add_song(sid, Song.from_mml("D1: kick8\n", lb), lb)
        self.assertEqual(sorted(pkg.samples), ["kick", "kick_a"])
        tunes = {s: pkg.tune(s) for s in "abc"}
        self.assertIs(tunes["a"].dac[0][0][2].tracks == tunes["b"].dac[0][0][2].tracks, True)
        self.assertNotEqual(tunes["a"].dac[0][0][2].tracks, tunes["c"].dac[0][0][2].tracks)
        self.assertIn("kick_a", pkg.songs["c"].to_mml())            # the exported text is still valid MML
        Song.from_mml(pkg.songs["c"].to_mml(), pkg.library())

    def test_bad_song_id(self):
        with self.assertRaises(pk.PackageError):
            pk.Package().add_song("no good", Song())

    def test_package_wide_shared_instruments_are_used_when_a_song_has_none(self):
        pkg = pk.Package(name="p")
        pkg.instruments["zap"] = dataclasses.replace(mml.BUILTIN_INSTR["lead"], pw=7)
        pkg.songs["s"] = Song.from_mml("V1: @lead c\n")
        pkg.songs["s"].tracks["V1"].insert(0, __import__("chipsynth.song", fromlist=["Select"]).Select("zap"))
        back = pk.loads(pk.dumps(pkg))
        self.assertEqual(back.tune("s").voices[0][0].inst.pw, 25)   # @lead later wins; zap resolved without error


class PackageSetTests(unittest.TestCase):
    def folder(self):
        d = Path(tempfile.mkdtemp())
        pkg = pk.Package(name="forest", author="m")
        pkg.add_song("dawn", Song.from_mml(TUNE))
        pkg.add_song("dusk", Song.from_mml("tempo 90\nV1: c2 d2\n"))
        pk.save(pkg, d / "forest.csp")
        (d / "lute.mml").write_text(TUNE)
        (d / "broken.mml").write_text("V1: zzz\n")
        (d / "junk.csp").write_bytes(b"nope")
        return d

    def test_folder_loads_packages_and_loose_mml_and_reports_the_rest(self):
        ps = PackageSet()
        n = ps.add_folder(self.folder())
        self.assertEqual(n, 2)
        self.assertEqual(sorted(r.id for r in ps.songs()), ["forest:dawn", "forest:dusk", "lute:lute"])
        self.assertEqual(len(ps.problems), 2)
        self.assertTrue(any("broken.mml" in p for p in ps.problems))

    def test_folder_can_skip_loose_mml(self):
        ps = PackageSet()
        ps.add_folder(self.folder(), mml_files=False)
        self.assertEqual(sorted(r.package for r in ps.songs()), ["forest", "forest"])

    def test_get_tune_player_and_refs(self):
        ps = PackageSet()
        ps.add_folder(self.folder())
        self.assertEqual(ps.get("forest:dawn").title, "Hello")
        self.assertGreater(ps.tune("forest:dusk").length, 0)
        self.assertIs(ps.tune("forest:dusk"), ps.tune("forest:dusk"))
        self.assertEqual(ps.player("forest:dawn").render(512).shape, (512, 2))
        ref = [r for r in ps.songs() if r.id == "forest:dusk"][0]
        self.assertEqual(ref.title, "dusk")
        self.assertGreater(ref.seconds, 1.0)

    def test_a_clashing_package_name_is_refused_not_overridden(self):
        d = self.folder()
        ps = PackageSet()
        ps.add(d / "forest.csp")
        other = Path(tempfile.mkdtemp())
        p2 = pk.Package(name="forest")
        p2.add_song("x", Song.from_mml("V1: c"))
        pk.save(p2, other / "other.csp")
        with self.assertRaises(pk.PackageError) as cm:
            ps.add(other / "other.csp")
        self.assertIn("already loaded", str(cm.exception))
        self.assertTrue(ps.has("forest:dawn") and not ps.has("forest:x"))

    def test_bad_ids(self):
        ps = PackageSet()
        ps.add_folder(self.folder())
        for bad in ("nocolon", ":x", "forest:", "nopkg:x", "forest:nosong"):
            with self.assertRaises(pk.PackageError):
                ps.get(bad)


class TextTests(unittest.TestCase):
    def test_dump_then_build_gives_the_same_bytes(self):
        lb = drum_lib(kick=kick())
        pkg = demo_package()
        pkg.instruments["zap"] = dataclasses.replace(mml.BUILTIN_INSTR["bell"], pw=9)
        pkg.fx["wob"] = [("vib", "5,3")]
        pkg.keys["blues"] = (0, 3, 5, 6, 7, 10)
        pkg.add_song("drums", Song.from_mml("V1: c\nD1: kick8 r8\n", lb), lb)
        b = pk.dumps(pkg)
        text = packagetext.dump_text(pkg)
        self.assertIn("#! song drums", text)
        self.assertIn("#! sample kick", text)
        self.assertEqual(pk.dumps(packagetext.build_text(text)), b)

    def test_build_rejects_a_text_that_is_not_a_dump(self):
        with self.assertRaises(pk.PackageError):
            packagetext.build_text("tempo 120\n")


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        r = subprocess.run([sys.executable, "-m", "chipsynth", *map(str, args)], capture_output=True, text=True,
                           cwd=str(ROOT))
        return r.returncode, r.stdout, r.stderr

    def test_import_list_dump_build_export(self):
        d = Path(tempfile.mkdtemp())
        (d / "one.mml").write_text(TUNE)
        (d / "two.mml").write_text("V1: c2 d2\n")
        csp = d / "pack.csp"
        self.assertEqual(self.run_cli("import", csp, d / "one.mml", d / "two.mml")[0], 0)
        code, out, _ = self.run_cli("list", csp)
        self.assertEqual(code, 0)
        self.assertIn("pack:one", out)
        self.assertIn("pack:two", out)
        txt = d / "pack.txt"
        self.assertEqual(self.run_cli("dump", csp, "-o", txt)[0], 0)
        csp2 = d / "again.csp"
        self.assertEqual(self.run_cli("build", txt, "-o", csp2)[0], 0)
        self.assertEqual(csp2.read_bytes().replace(b"pack", b"pack"), csp.read_bytes())
        self.assertEqual(self.run_cli("export", csp, "-d", d / "out")[0], 0)
        self.assertTrue((d / "out" / "one.mml").exists())

    def test_errors_exit_nonzero(self):
        code, _, err = self.run_cli("dump", "/no/such.csp")
        self.assertEqual(code, 1)
        self.assertIn("error", err)


if __name__ == "__main__":
    unittest.main()
