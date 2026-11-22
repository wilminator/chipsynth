# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Furnace modules through the composer's File > Import (headless, SDL dummy driver) and the window-free helpers."""
import os
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from chipsynth import package as pk
from chipsynth.composer import fileops as fo
from chipsynth.tests.test_composer_filemenu import Base, key
from chipsynth.tests.test_furnace_import import C4, E4, Sub, build, simple


def one_song(effects=()):
    return simple({0: (C4, 0, None, list(effects)), 1: (E4, 0, None, [])})


def two_songs():
    return build(subs=[Sub("first"), Sub("second")],
                 patterns={(0, 0, 0): {0: (C4, 0, None, [])}, (1, 0, 0): {0: (E4, 0, None, [])}})


class FileOpsTests(unittest.TestCase):
    def test_kind_and_names(self):
        self.assertEqual(fo.kind_of("x/song.FUR"), "furnace")
        self.assertIn("furnace", fo.IMPORTS)

    def test_read_import_gives_ids_like_the_command_line(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        (d / "two.fur").write_bytes(two_songs())
        self.assertEqual(fo.song_names("furnace", d / "two.fur"), ["first", "second"])
        loaded = fo.read_import("furnace", d / "two.fur")
        self.assertEqual([s[0] for s in loaded.songs], ["two_first", "two_second"])
        one = fo.read_import("furnace", d / "two.fur", song="2")
        self.assertEqual([s[0] for s in one.songs], ["two"])
        (d / "bad.fur").write_bytes(b"nope")
        with self.assertRaises(fo.FileOpError):
            fo.song_names("furnace", d / "bad.fur")
        with self.assertRaises(fo.FileOpError):
            fo.read_import("furnace", d / "bad.fur")


class ImportTests(Base):
    def test_the_import_menu_lists_furnace(self):
        self.open_menu()
        self.click(self.row("Import"))
        self.assertTrue(any("Furnace" in i["label"] for _, i in self.a.menu_geo["sub"]))

    def test_a_single_song_opens_unsaved_with_its_report(self):
        a = self.a
        p = self.write("tune.fur", one_song([(0x04, 0x47)]))
        self.assertTrue(a.import_begin(p))                         # one subsong: no options to ask for
        self.assertIsNone(a.overlay if a.overlay != "report" else None)
        self.assertEqual((a.source, a.song_id), ("", ""))
        self.assertEqual(a.tune.nvoices, 4)
        lines = a.last_report[1]
        self.assertTrue(any(x.startswith("left out:") and "vibrato" in x for x in lines), lines)
        self.assertEqual(a.overlay, "report")
        self.assertIn("Furnace import", a.last_report[0])

    def test_several_subsongs_ask_which_and_one_can_be_picked(self):
        a = self.a
        p = self.write("pick.fur", two_songs())
        self.assertTrue(a.import_begin(p))
        self.assertEqual(a.overlay, "fileopts")
        a.frame()
        self.assertEqual([r["key"] for r in a.fileopts["rows"]], ["song"])
        self.assertEqual([t for _, t in a.fileopts["rows"][0]["choices"]], ["All 2 songs", "first", "second"])
        a.fileopts["focus"] = 0
        a.frame([key(pygame.K_RIGHT), key(pygame.K_RETURN)])
        self.assertIsNone(a.overlay if a.overlay != "report" else None)
        self.assertEqual(a.source, "")
        self.assertFalse((self.dir / "pick.csp").exists())

    def test_all_subsongs_go_into_a_package_beside_the_file(self):
        a = self.a
        p = self.write("multi.fur", two_songs())
        self.assertTrue(a.import_file(p))
        out = self.dir / "multi.csp"
        self.assertEqual(list(pk.load(out).songs), ["multi_first", "multi_second"])
        self.assertIn("2 songs", a.message)

    def test_a_refused_module_says_why_and_changes_nothing(self):
        a = self.a
        before = a.label
        self.assertFalse(a.import_file(self.write("junk.fur", b"nope nope nope")))
        self.assertIn("not a Furnace module", a.message)
        self.assertEqual(a.label, before)

    def test_dropping_a_fur_imports_it(self):
        a = self.a
        p = self.write("dropped.fur", one_song())
        a.frame([pygame.event.Event(pygame.DROPFILE, file=str(p))])
        self.assertEqual(a.source, "")
        self.assertNotEqual(a.label, "Test")


if __name__ == "__main__":
    unittest.main()
