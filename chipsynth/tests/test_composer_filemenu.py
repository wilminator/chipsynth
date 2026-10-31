# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The composer's File menu, headless (SDL dummy driver): the menu itself, New / Open / Save, and every import and
export through the methods that take a path.  Fixtures are built here from the formats; no real songs are used."""
import os
import tempfile
import unittest
import wave
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import numpy as np
import pygame

from chipsynth import midi, mml, musicgen, package as pk
from chipsynth.composer.app import Composer
from chipsynth.composer.filemenu import significant
from chipsynth.composer.prefs import Prefs
from chipsynth.formats import famistudio_text as fam
from chipsynth.song import Song
from chipsynth.tests.test_goattracker_import import C4, sng, pat, gt

DATA = Path(__file__).parent / "data" / "famistudio"
TINY = (DATA / "tiny.txt").read_text()
TUNE = ("title Test\ntempo 120\necho 240ms,40,30\nV1: @lead c4 d4 e4 f4 | loop g4 a4 b4 >c4 |\nV2: @bass <c1 | loop e1 |\n")
ONE = [[0, gt.LOOPSONG, 0]] * 3


def key(k, mod=0):
    return pygame.event.Event(pygame.KEYDOWN, key=k, mod=mod, unicode="")


def down(pos):
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1)


def up(pos):
    return pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1)


def sng_bytes(subtunes=1, unsupported=False):
    """A GoatTracker song.  `unsupported` uses a pattern command and tables the importer reports as left out."""
    if unsupported:
        p = [(C4, 1, 1, 0x20), (gt.KEYOFF, 0, 0, 0)]
        ins = [(0x09, 0xA4, 1, 0, 1, 1, 0, 2, 0x10, "x")]
        tbl = [([0x11, 0x13, 0xFF], [0x80, 0x00, 0x01]), ([], []), ([0x80], [0x10]), ([0x01], [0x02])]
        return sng(ONE, [pat(*p)], ins, tbl)
    p = [(C4, 1, 0, 0), (gt.KEYOFF, 0, 0, 0)]
    ins = [(0, 0xF0, 0, 0, 0, 0, 0, 2, 0x10, "s")]
    return sng(ONE, [pat(*p)], ins, subtunes=[ONE] * subtunes)


def two_song_famistudio():
    """tiny.txt with its song written twice (the second named differently)."""
    lines = TINY.splitlines()
    head = lines[:9]
    song = lines[9:]
    second = [x.replace('Name="Main Theme"', 'Name="Second Theme"') for x in song]
    return "\n".join(head + song + second) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.a = Composer(self.screen, Prefs(view="roll"), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.a.frame()
        self.assertTrue(self.a.load_song(Song.from_mml(TUNE), "Test"))
        self.a.frame()

    def click(self, rect):
        r = pygame.Rect(rect)
        self.a.frame([down(r.center), up(r.center)])
        self.a.frame()

    def write(self, name, data):
        p = self.dir / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    def row(self, label, sub=False):
        a = self.a
        for rect, item in a.menu_geo["sub" if sub else "main"]:
            if item["label"].startswith(label):
                return rect
        self.fail("no menu row '%s' in %s" % (label, [i["label"] for _, i in a.menu_geo["sub" if sub else "main"]]))

    def open_menu(self):
        self.click(self.a.file_btn)
        self.assertEqual(self.a.overlay, "menu")

    def dirty(self, bpm=77):
        self.a.edit(lambda: self.a.ed.set_tempo_at(0, bpm))
        self.assertTrue(self.a.song_dirty)


class MenuTests(Base):
    def test_file_button_opens_the_menu_and_a_click_outside_closes_it(self):
        self.open_menu()
        labels = [i["label"] for _, i in self.a.menu_geo["main"]]
        self.assertEqual([x.split("  ")[0] for x in labels[:4]], ["New from generator...", "New blank song", "Open...", "Open from packages..."])
        for want in ("Save", "Save As...", "Import", "Export"):
            self.assertIn(want, labels)
        self.a.frame([down((5, 700)), up((5, 700))])
        self.a.frame()
        self.assertIsNone(self.a.overlay)
        self.assertIsNone(self.a.menu)

    def test_the_file_button_again_closes_it(self):
        self.open_menu()
        self.click(self.a.file_btn)
        self.assertIsNone(self.a.overlay)

    def test_f10_opens_and_escape_closes(self):
        self.a.frame([key(pygame.K_F10)])
        self.assertEqual(self.a.overlay, "menu")
        self.a.frame([key(pygame.K_ESCAPE)])
        self.assertIsNone(self.a.overlay)

    def test_import_and_export_submenus_list_every_format(self):
        self.open_menu()
        self.click(self.row("Import"))
        self.assertEqual(self.a.menu["sub"], "import")
        got = [i["label"] for _, i in self.a.menu_geo["sub"]]
        for want in ("MML", "MIDI", "FamiStudio text", "GoatTracker", "WAV sample"):
            self.assertTrue(any(want in x for x in got), (want, got))
        self.click(self.row("Export"))
        self.assertEqual(self.a.menu["sub"], "export")
        got = [i["label"] for _, i in self.a.menu_geo["sub"]]
        for want in ("WAV", "MIDI", "MML", "FamiStudio text", "Package copy"):
            self.assertTrue(any(want in x for x in got), (want, got))

    def test_hovering_import_opens_its_submenu(self):
        self.open_menu()
        r = pygame.Rect(self.row("Import"))
        self.a.frame([pygame.event.Event(pygame.MOUSEMOTION, pos=r.center, rel=(1, 1), buttons=(0, 0, 0))])
        self.a.frame()
        self.assertEqual(self.a.menu["sub"], "import")

    def test_keyboard_walks_the_menu_and_runs_a_row(self):
        a = self.a
        calls = []
        a.export_dialog = lambda fmt: calls.append(fmt)
        a.frame([key(pygame.K_F10)])
        main = [i for i in a.menu_items()]
        want = next(n for n, i in enumerate(main) if i and i["label"] == "Export")
        for _ in range(want):                                        # separators are skipped, so stop when Export is lit
            if a.menu["hi"] == want:
                break
            a.frame([key(pygame.K_DOWN)])
        self.assertEqual(a.menu["hi"], want)
        a.frame([key(pygame.K_RIGHT)])
        self.assertEqual((a.menu["sub"], a.menu["focus"]), ("export", "sub"))
        a.frame([key(pygame.K_DOWN), key(pygame.K_RETURN)])        # the second export row: MIDI
        self.assertEqual(calls, ["mid"])
        self.assertIsNone(a.overlay)

    def test_left_closes_a_submenu_and_escape_goes_back_one_step(self):
        a = self.a
        a.frame([key(pygame.K_F10)])
        a.menu["hi"] = next(n for n, i in enumerate(a.menu_items()) if i and i["label"] == "Import")
        a.frame([key(pygame.K_RIGHT)])
        self.assertEqual(a.menu["sub"], "import")
        a.frame([key(pygame.K_LEFT)])
        self.assertIsNone(a.menu["sub"])
        a.frame([key(pygame.K_RIGHT), key(pygame.K_ESCAPE)])
        self.assertIsNone(a.menu["sub"])
        self.assertEqual(a.overlay, "menu")
        a.frame([key(pygame.K_ESCAPE)])
        self.assertIsNone(a.overlay)

    def test_without_a_song_save_and_export_are_greyed_out(self):
        a = Composer(self.screen, Prefs(view="roll"), prefs_path=self.dir / "p2.json")
        a.frame()
        by = {i["label"]: i for i in a.menu_items() if i}
        self.assertFalse(by["Save"]["enabled"])
        self.assertFalse(by["Export"]["enabled"])
        a.menu_open()
        a.frame()
        self.click_on(a, "Export")
        self.assertEqual(a.overlay, "menu")                          # a greyed-out row does nothing
        self.assertFalse(next(i for i in a.submenu_items("import") if "WAV" in i["label"])["enabled"])

    def click_on(self, a, label):
        for rect, item in a.menu_geo["main"]:
            if item["label"] == label:
                a.frame([down(pygame.Rect(rect).center), up(pygame.Rect(rect).center)])
                return
        self.fail(label)

    def test_menu_draws_at_small_and_large_sizes_in_both_themes(self):
        for size in ((960, 600), (1280, 760), (1920, 1080)):
            self.a.screen = pygame.display.set_mode(size, pygame.RESIZABLE)
            for dark in (True, False):
                self.a.prefs.dark = dark
                self.a.frame([key(pygame.K_F10)])
                self.a.menu["sub"] = "export"
                self.a.frame()
                self.a.frame([key(pygame.K_ESCAPE), key(pygame.K_ESCAPE)])


class NewOpenSaveTests(Base):
    def test_new_blank_song_through_the_menu(self):
        self.open_menu()
        self.click(self.row("New blank song"))
        self.assertIsNone(self.a.overlay)
        self.assertEqual(self.a.label, "Untitled")
        self.assertEqual(self.a.source, "")
        self.assertEqual(self.a.tune.bars, 4)

    def test_new_from_generator_through_the_menu(self):
        self.open_menu()
        self.click(self.row("New from generator"))
        self.assertEqual(self.a.overlay, "new")

    def test_new_from_generator_after_dont_save_opens_the_dialog_and_does_not_ask_again(self):
        self.dirty()
        self.a.new_from_generator_dialog()
        self.assertEqual(self.a.overlay, "unsaved")
        self.a.finish_unsaved("discard")
        self.assertEqual(self.a.overlay, "new")

    def test_open_runs_the_open_dialog_and_packages_opens_the_songs_list(self):
        a = self.a
        called = []
        a.open_dialog = lambda: called.append("open")
        self.open_menu()
        self.click(self.row("Open..."))
        self.assertEqual(called, ["open"])
        self.open_menu()
        self.click(self.row("Open from packages"))
        self.assertEqual(a.overlay, "songs")

    def test_save_and_save_as_rows(self):
        a = self.a
        p = self.dir / "mine.mml"
        a.source = str(p)
        self.open_menu()
        self.click(self.row("Save"))
        self.assertTrue(p.exists())
        asked = []
        a.save_as = lambda: asked.append(1)
        self.open_menu()
        self.click(self.row("Save As"))
        self.assertEqual(asked, [1])

    def test_new_blank_asks_before_dropping_edits(self):
        self.dirty()
        self.assertFalse(self.a.new_blank())
        self.assertEqual(self.a.overlay, "unsaved")
        self.assertEqual(self.a.label, "Test")
        self.a.frame()
        self.assertTrue(self.a.finish_unsaved("discard"))
        self.assertEqual(self.a.label, "Untitled")


class ShortcutTests(Base):
    def test_the_existing_shortcuts_still_work(self):
        a = self.a
        seen = []
        a.open_dialog = lambda: seen.append("open")
        a.save_as = lambda: seen.append("save_as")
        a.export_midi_dialog = lambda: seen.append("midi")
        a.frame([key(pygame.K_o, pygame.KMOD_CTRL)])
        a.frame([key(pygame.K_s, pygame.KMOD_CTRL | pygame.KMOD_SHIFT)])
        a.frame([key(pygame.K_e, pygame.KMOD_CTRL)])
        self.assertEqual(seen, ["open", "save_as", "midi"])
        a.frame([key(pygame.K_n, pygame.KMOD_CTRL)])
        self.assertEqual(a.overlay, "new")
        a.frame([key(pygame.K_ESCAPE)])
        a.source = str(self.dir / "s.mml")
        a.frame([key(pygame.K_s, pygame.KMOD_CTRL)])
        self.assertTrue((self.dir / "s.mml").exists())

    def test_a_shortcut_with_the_menu_open_closes_it_and_runs(self):
        a = self.a
        a.frame([key(pygame.K_F10)])
        a.frame([key(pygame.K_n, pygame.KMOD_CTRL)])
        self.assertEqual(a.overlay, "new")
        self.assertIsNone(a.menu)

    def test_the_old_buttons_are_still_in_the_top_bar(self):
        # drawing the top bar must not raise, and New / Save still do their work
        self.a.frame()
        self.assertIsNotNone(self.a.file_btn)
        self.assertEqual(self.a.file_btn[0], 8)


class ImportTests(Base):
    def test_a_goattracker_song_opens_unsaved_and_its_report_is_shown(self):
        a = self.a
        p = self.write("tune.sng", sng_bytes(unsupported=True))
        self.assertTrue(a.import_file(p))
        self.assertEqual((a.source, a.song_id), ("", ""))
        self.assertEqual(a.tune.nvoices, 3)
        lines = a.last_report[1]
        self.assertTrue(significant(lines))
        self.assertTrue(any(x.startswith("left out:") and "pattern command" in x for x in lines), lines)
        self.assertEqual(a.overlay, "report")
        self.assertIn("left out", a.message)
        a.frame()
        a.frame([key(pygame.K_DOWN), key(pygame.K_RETURN)])
        self.assertIsNone(a.overlay)

    def test_a_clean_import_says_so_in_the_status_line_without_an_overlay(self):
        a = self.a
        self.assertTrue(a.import_file(self.write("clean.sng", sng_bytes())))
        self.assertIsNone(a.overlay)
        self.assertIn("Imported", a.message)
        self.assertTrue(a.last_report)

    def test_goattracker_asks_for_region_multispeed_and_subtune(self):
        a = self.a
        p = self.write("opts.sng", sng_bytes())
        self.assertTrue(a.import_begin(p))
        self.assertEqual(a.overlay, "fileopts")
        a.frame()
        self.assertEqual([r["key"] for r in a.fileopts["rows"]], ["region", "multispeed", "song"])
        self.assertEqual(a.fileopts_values()["region"], "pal")
        a.frame([key(pygame.K_RIGHT)])                              # focus is on Region: PAL -> NTSC
        self.assertEqual(a.fileopts_values()["region"], "ntsc")
        self.click(a.menu_geo["opts"]["ok"])
        self.assertIsNone(a.overlay)
        self.assertEqual(a.song.bpm, 150)                            # NTSC is 60 Hz; PAL is 125

    def test_cancelling_the_options_imports_nothing(self):
        a = self.a
        a.import_begin(self.write("c.sng", sng_bytes()))
        a.frame([key(pygame.K_ESCAPE)])
        self.assertEqual(a.label, "Test")
        self.assertIsNone(a.overlay)

    def test_several_subtunes_go_into_a_package_beside_the_file(self):
        a = self.a
        p = self.write("multi.sng", sng_bytes(subtunes=2))
        self.assertTrue(a.import_file(p))
        out = self.dir / "multi.csp"
        self.assertTrue(out.exists())
        self.assertEqual(list(pk.load(out).songs), ["multi_1", "multi_2"])
        self.assertEqual(a.song_id, "multi:multi_1")
        self.assertEqual(Path(a.packages.paths["multi"]), out)
        self.assertTrue(a.packages.has("multi:multi_2"))
        self.assertIn("2 songs", a.message)

    def test_one_subtune_of_several_opens_unsaved(self):
        a = self.a
        p = self.write("pick.sng", sng_bytes(subtunes=3))
        self.assertTrue(a.import_file(p, song="2"))
        self.assertEqual(a.source, "")
        self.assertFalse((self.dir / "pick.csp").exists())

    def test_the_options_dialog_names_where_several_songs_go_and_picks_one(self):
        a = self.a
        p = self.write("pk.sng", sng_bytes(subtunes=2))
        a.import_begin(p)
        self.assertIn("pk.csp", a.fileopts["note"])
        songs = next(r for r in a.fileopts["rows"] if r["key"] == "song")
        self.assertEqual([t for _, t in songs["choices"]], ["All 2 songs", "subtune 1", "subtune 2"])
        a.fileopts["focus"] = 2
        a.frame([key(pygame.K_RIGHT), key(pygame.K_RETURN)])        # "subtune 1", then Enter
        self.assertEqual(a.source, "")
        self.assertFalse((self.dir / "pk.csp").exists())

    def test_several_songs_join_the_open_package_and_keep_its_others(self):
        a = self.a
        existing = pk.Package(name="game")
        existing.add_song("old", Song.from_mml("title Old\nV1: c d e\n"))
        pk.save(existing, self.dir / "game.csp")
        a.packages.add(self.dir / "game.csp")
        self.assertTrue(a.open_song_id("game:old"))
        a.frame()
        p = self.write("new.sng", sng_bytes(subtunes=2))
        self.assertTrue(a.import_file(p))
        self.assertEqual(sorted(pk.load(self.dir / "game.csp").songs), ["new_1", "new_2", "old"])
        self.assertFalse((self.dir / "new.csp").exists())
        self.assertEqual(a.song_id, "game:new_1")
        self.assertTrue(a.packages.has("game:old"))
        self.assertTrue(a.import_file(p))                            # again: the same ids are replaced, none doubled
        self.assertEqual(sorted(pk.load(self.dir / "game.csp").songs), ["new_1", "new_2", "old"])

    def test_famistudio_song_with_a_sample_opens_unsaved_and_saves_as_a_package(self):
        a = self.a
        p = self.write("tiny.txt", TINY)
        self.assertTrue(a.import_file(p))
        self.assertEqual(a.source, "")
        self.assertTrue(any("Kick Drum" in n or "kick" in n.lower() for n in a.ed.sample_names()), a.ed.sample_names())
        self.assertTrue(significant(a.last_report[1]))
        self.assertEqual(a.overlay, "report")
        a.frame([key(pygame.K_ESCAPE)])
        self.assertTrue(a.save(self.dir / "tiny_out.csp"))
        again = pk.load(self.dir / "tiny_out.csp")
        self.assertEqual(len(again.songs), 1)
        self.assertTrue(again.samples)

    def test_famistudio_with_two_songs_asks_which_and_the_default_is_all(self):
        a = self.a
        p = self.write("two.txt", two_song_famistudio())
        a.import_begin(p)
        self.assertEqual(a.overlay, "fileopts")
        a.frame()
        songs = next(r for r in a.fileopts["rows"] if r["key"] == "song")
        self.assertEqual([t for _, t in songs["choices"]], ["All 2 songs", "Main Theme", "Second Theme"])
        self.click(a.menu_geo["opts"]["ok"])
        self.assertEqual(sorted(pk.load(self.dir / "two.csp").songs), ["two_Main_Theme", "two_Second_Theme"])
        self.assertEqual(a.song_id, "two:two_Main_Theme")
        self.assertTrue(pk.load(self.dir / "two.csp").samples)

    def test_famistudio_single_song_imports_without_a_dialog(self):
        a = self.a
        a.import_begin(self.write("one.txt", TINY))
        self.assertNotEqual(a.overlay, "fileopts")
        self.assertEqual(a.source, "")

    def test_mml_midi_and_wav_imports(self):
        a = self.a
        self.assertTrue(a.import_file(self.write("tune.mml", "title From MML\nV1: c d e f\n")))
        self.assertEqual((a.label, a.source), ("From MML", ""))
        song = Song.from_mml("title Mid\nV1: c d e f g\nV2: e1\n")
        mid = self.write("tune.mid", midi.to_midi(song.compile(mml.builtin_library())))
        self.assertTrue(a.import_file(mid))
        self.assertEqual(a.source, "")
        self.assertGreaterEqual(a.roll.nv, 1)
        t = np.arange(4000) / 44100.0
        wav = self.dir / "kick.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes((np.sin(2 * np.pi * 90 * t) * 20000).astype("<i2").tobytes())
        self.assertTrue(a.import_file(wav))
        self.assertIn("kick", a.ed.sample_names())

    def test_wrong_files_are_refused_with_a_message(self):
        a = self.a
        self.assertFalse(a.import_file(self.write("bad.sng", b"nope nope"), "goattracker"))
        self.assertIn("not a GoatTracker song", a.message)
        self.assertFalse(a.import_file(self.write("bad.txt", "hello\n"), "famistudio"))
        self.assertIn("not a FamiStudio", a.message)
        self.assertFalse(a.import_file(self.write("what.xyz", "?")))
        self.assertIn("Can't tell", a.message)
        self.assertFalse(a.import_file(self.dir / "missing.sng"))
        self.assertEqual(a.label, "Test")

    def test_importing_over_unsaved_edits_asks_first_for_every_kind(self):
        a = self.a
        sng_p = self.write("g.sng", sng_bytes())
        multi_p = self.write("m.sng", sng_bytes(subtunes=2))
        fam_p = self.write("f.txt", TINY)
        mml_p = self.write("m.mml", "V1: c d e\n")
        for n, p in enumerate((sng_p, multi_p, fam_p, mml_p)):
            self.dirty(60 + n)
            self.assertFalse(a.import_file(p), p)
            self.assertEqual(a.overlay, "unsaved", p)
            self.assertEqual(a.label, "Test")
            a.frame()
            a.finish_unsaved("cancel")
            self.assertTrue(a.song_dirty)
            self.assertFalse((self.dir / "m.csp").exists())            # nothing was written before the answer
            a.ed.saved_id = a.ed.undo_stack[-1][0]                       # clean again for the next kind

    def test_save_first_then_import_runs(self):
        a = self.a
        a.source = str(self.dir / "keep.mml")
        self.dirty()
        a.import_file(self.write("g.sng", sng_bytes()))
        self.assertEqual(a.overlay, "unsaved")
        self.assertTrue(a.finish_unsaved("save"))
        self.assertIn("77", (self.dir / "keep.mml").read_text())
        self.assertEqual(a.source, "")
        self.assertEqual(a.tune.nvoices, 3)

    def test_the_import_menu_rows_choose_a_file_then_import(self):
        a = self.a
        asked = []
        p = self.write("viamenu.sng", sng_bytes())
        a.choose_path = lambda save, title, pattern, *rest, **kw: asked.append((save, title, pattern)) or str(p)
        self.open_menu()
        self.click(self.row("Import"))
        self.click(self.row("GoatTracker", sub=True))
        self.assertEqual(asked, [(False, "Import GoatTracker song", "*.sng")])
        self.assertEqual(a.overlay, "fileopts")                      # the options come next
        a.frame([key(pygame.K_RETURN)])
        self.assertEqual(a.tune.nvoices, 3)

    def test_dropping_a_sng_or_txt_imports_it(self):
        a = self.a
        p = self.write("dropped.sng", sng_bytes())
        a.frame([pygame.event.Event(pygame.DROPFILE, file=str(p))])
        self.assertEqual(a.overlay, "fileopts")
        a.frame([key(pygame.K_ESCAPE)])
        a.frame([pygame.event.Event(pygame.DROPFILE, file=str(self.write("d.txt", TINY)))])
        self.assertEqual(a.source, "")
        self.assertNotEqual(a.label, "Test")


class ExportTests(Base):
    def frames(self, path):
        with wave.open(str(path), "rb") as w:
            return w.getnframes(), w.getnchannels(), w.getframerate()

    def test_every_format_is_written_through_the_exporter(self):
        a = self.a
        for fmt, name in (("wav", "o.wav"), ("mid", "o.mid"), ("mml", "o.mml"), ("famistudio", "o.txt"), ("csp", "o.csp")):
            self.assertTrue(a.export_as(fmt, self.dir / name), fmt)
            self.assertTrue((self.dir / name).stat().st_size > 0, fmt)
        n, ch, rate = self.frames(self.dir / "o.wav")
        self.assertEqual((ch, rate), (2, 44100))
        self.assertGreater(n, 44100)
        self.assertEqual((self.dir / "o.mid").read_bytes()[:4], b"MThd")
        self.assertEqual(Song.from_mml((self.dir / "o.mml").read_text()).title, "Test")
        self.assertTrue(fam.looks_like((self.dir / "o.txt").read_text(encoding="utf-8")))
        self.assertEqual(len(pk.load(self.dir / "o.csp").songs), 1)

    def test_the_wav_is_the_same_audio_the_command_line_makes(self):
        from chipsynth import exporter
        a = self.a
        a.export_as("wav", self.dir / "app.wav", loops=1, normalize=True)
        pkg = pk.Package(name="x")
        pkg.add_song("x", Song.from_mml(TUNE), mml.builtin_library())
        exporter.export_song(pkg, "x", self.dir / "cli.wav", exporter.AudioOptions(loops=1, normalize=True))
        self.assertEqual((self.dir / "app.wav").read_bytes(), (self.dir / "cli.wav").read_bytes())

    def test_wav_options_loops_normalize_and_selection(self):
        a = self.a
        a.export_as("wav", self.dir / "a.wav")
        a.export_as("wav", self.dir / "b.wav", loops=2)
        self.assertGreater(self.frames(self.dir / "b.wav")[0], self.frames(self.dir / "a.wav")[0])
        a.export_as("wav", self.dir / "n.wav", normalize=True)
        with wave.open(str(self.dir / "n.wav"), "rb") as w:
            peak = np.abs(np.frombuffer(w.readframes(w.getnframes()), "<i2")).max() / 32768.0
        self.assertAlmostEqual(20 * np.log10(peak), -1.0, delta=0.1)
        first = a.roll.notes[0][0]
        a.set_selection({(0, first.start)})
        a.export_as("wav", self.dir / "s.wav", part="selection")
        self.assertLess(self.frames(self.dir / "s.wav")[0], self.frames(self.dir / "a.wav")[0])

    def test_selection_with_nothing_selected_exports_everything_and_says_so(self):
        a = self.a
        a.export_as("wav", self.dir / "w.wav", part="selection")
        self.assertTrue(any("whole song" in x for x in a.last_report[1]))
        self.assertEqual(a.overlay, "report")

    def test_famistudio_export_reports_what_it_left_out(self):
        a = self.a
        self.assertTrue(a.export_as("famistudio", self.dir / "f.txt", region="pal"))
        lines = a.last_report[1]
        self.assertTrue(any(x.startswith("left out:") and "echo" in x for x in lines), lines)
        self.assertEqual(a.overlay, "report")
        self.assertIn("left out", a.message)
        self.assertIn("PAL timing", " ".join(lines))
        a.frame()                                                    # the overlay draws

    def test_midi_export_says_the_drum_lanes_are_not_in_it(self):
        a = self.a
        song = Song.from_mml(musicgen.generate("town", 1, "", None, dac=True), a.lib)
        self.assertTrue(a.load_song(song, "Drums"))
        self.assertTrue(a.export_as("mid", self.dir / "d.mid"))
        self.assertTrue(any(x.startswith("left out:") and "drum" in x for x in a.last_report[1]), a.last_report)

    def test_a_clean_export_just_reports_in_the_status_line(self):
        a = self.a
        a.load_song(Song.from_mml("title Plain\nV1: c d e f\n"), "Plain")
        self.assertTrue(a.export_as("mml", self.dir / "plain.mml"))
        self.assertIsNone(a.overlay)
        self.assertIn("Exported plain.mml", a.message)

    def test_overwriting_keeps_a_backup_and_a_name_without_an_ending_gets_one(self):
        a = self.a
        a.export_as("mid", self.dir / "same.mid")
        a.export_as("mid", self.dir / "same.mid")
        self.assertTrue(list((self.dir / "backups").glob("same.mid.*")))
        a.export_as("wav", self.dir / "noending")
        self.assertTrue((self.dir / "noending.wav").exists())

    def test_the_export_does_not_touch_the_open_song(self):
        a = self.a
        a.export_as("csp", self.dir / "x.csp")
        self.assertEqual(a.source, "")
        self.assertEqual(a.label, "Test")

    def test_a_song_that_was_edited_exports_the_edit(self):
        a = self.a
        self.dirty()
        a.export_as("mml", self.dir / "e.mml")
        self.assertIn("77", (self.dir / "e.mml").read_text())

    def test_export_through_the_menu_wav_has_an_options_dialog_then_the_chooser(self):
        a = self.a
        target = self.dir / "menu.wav"
        asked = []
        a.choose_path = lambda save, title, pattern, ext="", initialfile="", initialdir=None: asked.append((save, ext, initialfile)) or str(target)
        self.open_menu()
        self.click(self.row("Export"))
        self.click(self.row("WAV", sub=True))
        self.assertEqual(a.overlay, "fileopts")
        self.assertEqual([r["key"] for r in a.fileopts["rows"]], ["loops", "normalize", "part"])
        a.frame()
        a.frame([key(pygame.K_RIGHT)])                                # Loop trips: none -> 1 more
        a.frame([key(pygame.K_RETURN)])
        self.assertEqual(asked, [(True, ".wav", "Test")])
        self.assertTrue(target.exists())
        self.assertIn("Exported menu.wav", a.message)

    def test_export_through_the_menu_midi_goes_straight_to_the_chooser(self):
        a = self.a
        target = self.dir / "m.mid"
        a.choose_path = lambda *args, **kw: str(target)
        self.open_menu()
        self.click(self.row("Export"))
        self.click(self.row("MIDI", sub=True))
        self.assertTrue(target.exists())
        self.assertIsNone(a.overlay)

    def test_a_cancelled_chooser_writes_nothing(self):
        a = self.a
        a.choose_path = lambda *args, **kw: ""
        self.assertFalse(a.export_choose("mml"))
        self.assertEqual(list(self.dir.glob("*.mml")), [])

    def test_exporting_when_the_file_cannot_be_written_says_so(self):
        a = self.a
        blocker = self.write("blocker", "i am a file")
        self.assertFalse(a.export_as("wav", blocker / "sub" / "x.wav"))
        self.assertIn("Can't export", a.message)

    def test_last_report_row_shows_the_report_again(self):
        a = self.a
        a.export_as("famistudio", self.dir / "r.txt")
        a.frame([key(pygame.K_ESCAPE)])
        self.assertIsNone(a.overlay)
        self.open_menu()
        self.click(self.row("Last report"))
        self.assertEqual(a.overlay, "report")
        a.frame([key(pygame.K_RETURN)])
        self.assertIsNone(a.overlay)


class ReportOverlayTests(Base):
    def test_a_long_report_scrolls(self):
        a = self.a
        a.show_report("Long", ["left out: thing %d" % i for i in range(80)], "many")
        self.assertEqual(a.overlay, "report")
        a.frame()
        a.frame([key(pygame.K_PAGEDOWN)])
        self.assertGreater(a.report_view["scroll"], 0)
        a.frame()
        a.frame([key(pygame.K_ESCAPE)])
        self.assertIsNone(a.overlay)

    def test_only_facts_do_not_open_the_overlay(self):
        a = self.a
        a.show_report("Facts", ["note: written by FamiStudio 4.5.3"], "ok")
        self.assertIsNone(a.overlay)
        self.assertEqual(a.last_report[0], "Facts")


if __name__ == "__main__":
    unittest.main()
