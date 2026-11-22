# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The composer's note panel (E): effects and key changes for the picked notes, headless (SDL dummy driver).  The rows and
the keys of the dialog, the one-undo-step edit behind OK, and what the roll then shows."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from chipsynth.composer import notepanel as NP, panels
from chipsynth.composer.app import Composer
from chipsynth.composer.prefs import Prefs
from chipsynth.song import Song
from chipsynth.songedit import KeyMark

TUNE = "tempo 120\nV1: o4 l8 c d e f g a b >c | c d e f g a b <c\nV2: o3 l4 c e g c c e g c\n"
PATTERN = "[pat]\nriff = c4 d4 e4 r4\nV1: o4 !riff !riff\nV2: o3 c1 c1\n"


def key(k, mod=0):
    return pygame.event.Event(pygame.KEYDOWN, key=k, mod=mod, unicode="")


def down(pos):
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1)


def up(pos):
    return pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1)


class Base(unittest.TestCase):
    TEXT = TUNE

    def setUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        self.dir = Path(tempfile.mkdtemp())
        self.a = Composer(self.screen, Prefs(view="roll"), prefs_path=self.dir / "prefs.json", root=self.dir)
        self.a.frame()
        self.assertTrue(self.a.load_song(Song.from_mml(self.TEXT), "Test"))
        self.a.frame()

    def pick(self, *notes):
        self.a.set_selection({(0, s) for s in notes})
        self.a.frame()

    def press(self, *keys):
        for k in keys:
            self.a.frame([key(k)])

    def rows(self):
        return {r["key"]: r for r in self.a.fileopts["rows"]}

    def choose(self, row, text):
        """Move row `row` of the dialog to the choice whose text starts with `text` (a click on the row steps it on)."""
        r = self.rows()[row]
        for _ in range(len(r["choices"])):
            if r["choices"][r["i"]][1].startswith(text):
                return
            self.a.fileopts_step(list(self.rows()).index(row), 1)
        self.fail("no choice '%s' in %s" % (text, [t for _, t in r["choices"]]))

    def value(self, row):
        r = self.rows()[row]
        return r["choices"][r["i"]][0]

    def ok(self):
        self.a.frame()
        self.a.frame([down(self.a.menu_geo["opts"]["ok"].center), up(self.a.menu_geo["opts"]["ok"].center)])
        self.a.frame()


class OpenTests(Base):
    def test_e_opens_the_panel_for_the_picked_notes(self):
        self.pick(48)
        self.press(pygame.K_e)
        self.assertEqual(self.a.overlay, "fileopts")
        self.a.frame()
        self.assertEqual(self.a.fileopts["title"], "Notes: effects and key")
        self.assertEqual(list(self.rows()), ["vib", "arp", "arprate", "slide", "pws", "keymode", "tonic", "scale", "voices"])
        self.assertEqual(self.value("vib"), (0, 0))
        self.assertEqual(self.value("keymode"), "none")
        self.assertIn("1 note picked", self.a.fileopts["note"])
        self.assertIn("bar 1, beat 1.50", self.a.fileopts["note"])
        self.press(pygame.K_ESCAPE)
        self.assertIsNone(self.a.overlay)

    def test_with_nothing_picked_only_the_key_rows_show(self):
        self.a.cursor_tick = 192
        self.press(pygame.K_e)
        self.assertEqual(list(self.rows()), ["keymode", "tonic", "scale", "voices"])
        self.assertIn("No notes picked", self.a.fileopts["note"])
        self.assertIn("bar 1, beat 3.00", self.a.fileopts["note"])

    def test_the_inspector_has_a_button_and_it_opens_the_same_panel(self):
        self.pick(48)
        self.a.frame()
        seen = []
        orig = self.a.open_note_panel
        self.a.open_note_panel = lambda: seen.append(orig())
        # find the button by pressing every spot of the inspector's right edge would be silly: draw it and look at the tip
        self.a.frame()
        self.a.ui.pos = (-1, -1)
        found = False
        r = self.a.layout.right
        for y in range(r[1], r[1] + r[3], 6):
            self.a.ui.pos = (r[0] + 40, y)
            self.a.frame()
            if "Vibrato, arpeggio, slide" in (self.a.ui.tip or ""):
                self.a.frame([down(self.a.ui.pos), up(self.a.ui.pos)])
                found = True
                break
        if not found:                                                  # the inspector may need scrolling at this size
            self.a.insp_scroll = self.a.insp_max
            for y in range(r[1], r[1] + r[3], 6):
                self.a.ui.pos = (r[0] + 40, y)
                self.a.frame()
                if "Vibrato, arpeggio, slide" in (self.a.ui.tip or ""):
                    self.a.frame([down(self.a.ui.pos), up(self.a.ui.pos)])
                    found = True
                    break
        self.assertTrue(found)
        self.assertEqual(seen, [True])

    def test_the_mixed_rows_start_on_keep_and_a_custom_value_is_kept(self):
        self.pick(0, 48)
        self.a.edit(lambda: self.a.ed.set_note_effects([(0, 0)], vib=(5, 3), arp=(0, 5, 9)))
        self.pick(0, 48)
        self.press(pygame.K_e)
        self.assertEqual(self.value("vib"), NP.KEEP)
        self.assertEqual(self.value("arp"), NP.KEEP)
        self.assertEqual(self.value("slide"), 0)
        self.press(pygame.K_ESCAPE)
        self.pick(0)
        self.press(pygame.K_e)
        self.assertEqual(self.value("arp"), (0, 5, 9))                  # not a preset: shown as custom, and kept as it is
        self.assertTrue(self.rows()["arp"]["choices"][self.rows()["arp"]["i"]][1].startswith("custom"))
        self.press(pygame.K_RETURN)
        self.assertEqual(self.a.message, "Nothing to change.")


class KeyHandlingTests(Base):
    def test_the_dialog_keys(self):
        self.pick(48)
        self.press(pygame.K_e)
        fo = self.a.fileopts
        self.assertEqual(fo["focus"], 0)
        self.press(pygame.K_DOWN, pygame.K_DOWN)
        self.assertEqual(fo["focus"], 2)
        self.press(pygame.K_UP)
        self.assertEqual(fo["focus"], 1)
        self.press(pygame.K_RIGHT)                                      # Arpeggio: off -> major
        self.assertEqual(self.value("arp"), (0, 4, 7))
        self.press(pygame.K_RIGHT)
        self.assertEqual(self.value("arp"), (0, 3, 7))
        self.press(pygame.K_LEFT, pygame.K_LEFT)
        self.assertEqual(self.value("arp"), ())
        self.press(pygame.K_LEFT)                                       # wraps round to the last preset
        self.assertEqual(self.value("arp"), (0, 5, 7))
        self.press(pygame.K_TAB)
        self.assertEqual(fo["focus"], 2)
        self.a.frame([key(pygame.K_TAB, pygame.KMOD_SHIFT)])
        self.assertEqual(fo["focus"], 1)
        self.press(pygame.K_UP, pygame.K_UP)                            # wraps the other way, to the last row
        self.assertEqual(fo["focus"], len(fo["rows"]) - 1)
        self.press(pygame.K_SPACE)
        self.assertEqual(self.value("voices"), "all")
        self.assertEqual(self.a.overlay, "fileopts")

    def test_delete_clears_every_effect_row_and_enter_applies(self):
        self.pick(48)
        self.a.edit(lambda: self.a.ed.set_note_effects([(0, 48)], vib=(5, 3), slide=12, arp=(0, 4, 7), pws=(25, 75, 6)))
        self.pick(48)
        self.press(pygame.K_e)
        self.assertEqual(self.value("slide"), 12)
        self.press(pygame.K_DELETE)
        self.assertEqual([self.value(k) for k in ("vib", "arp", "slide", "pws")], [(0, 0), (), 0, (0, 0, 0)])
        self.assertEqual(self.value("keymode"), "none")                  # the key rows are not effects
        self.press(pygame.K_RETURN)
        self.assertIsNone(self.a.overlay)
        self.assertEqual(self.a.roll.notes[0][1].marks, "")
        self.assertIn("Effects set on 1 note", self.a.message)

    def test_escape_cancels_and_changes_nothing(self):
        self.pick(48)
        before = self.a.song.to_mml()
        self.press(pygame.K_e, pygame.K_RIGHT, pygame.K_ESCAPE)
        self.assertIsNone(self.a.overlay)
        self.assertEqual(self.a.song.to_mml(), before)
        self.assertFalse(self.a.ed.can_undo())

    def test_other_keys_do_not_leak_through_to_the_roll(self):
        self.pick(48)
        self.press(pygame.K_e)
        before = (self.a.song.to_mml(), self.a.sel_voice, self.a.loop_on)
        self.press(pygame.K_m, pygame.K_l, pygame.K_k, pygame.K_2, pygame.K_q)
        self.assertEqual((self.a.song.to_mml(), self.a.sel_voice, self.a.loop_on), before)
        self.assertEqual(self.a.overlay, "fileopts")

    def test_e_is_the_piano_in_the_tracker_and_the_button_works_there(self):
        self.a.set_mode("tracker")
        self.a.frame()
        self.press(pygame.K_e)
        self.assertNotEqual(self.a.overlay, "fileopts")                 # a piano key, as before
        self.a.tr.col, self.a.tr.row = 0, 0
        self.a.song and self.a.open_note_panel()
        self.assertEqual(self.a.overlay, "fileopts")
        self.assertEqual(self.a.panel_ctx["targets"], [(0, 0)])         # the note at the cursor


class ApplyTests(Base):
    def test_one_note_gets_vibrato_from_the_dialog_and_undo_takes_it_back(self):
        self.pick(48)
        original = self.a.song.to_mml()
        self.press(pygame.K_e)
        self.choose("vib", "medium")
        self.ok()
        self.assertIsNone(self.a.overlay)
        self.assertEqual(self.a.roll.notes[0][1].marks, "~")
        self.assertEqual([n.marks for n in self.a.roll.notes[0] if n.marks], ["~"])
        self.assertIn("vib 5,3", self.a.song.to_mml())
        self.assertEqual(self.a.note_sel, (0, 1))                       # the note stays picked
        self.assertEqual(len(self.a.ed.undo_stack), 1)
        self.press(pygame.K_z)
        self.a.frame([key(pygame.K_z, pygame.KMOD_CTRL)])
        self.assertEqual(self.a.song.to_mml(), original)

    def test_several_notes_and_several_effects_are_one_edit(self):
        self.pick(48, 96, 144)
        self.press(pygame.K_e)
        self.choose("arp", "minor  ")
        self.choose("slide", "16th")
        self.choose("pws", "10-90")
        self.ok()
        marks = [n.marks for n in self.a.roll.notes[0][:5]]
        self.assertEqual(marks[0], "")
        self.assertEqual(marks[4], "")
        for m in marks[1:4]:
            self.assertIn("^m", m)
            self.assertIn("w", m)
        self.assertEqual(len(self.a.ed.undo_stack), 1)
        self.assertEqual(len(self.a.selection()), 3)                    # still picked

    def test_a_key_change_from_the_dialog(self):
        self.pick(192)                                                  # V1's 5th note (tick 192)
        before = [[(n.start, n.pitch) for n in v] for v in self.a.tune.voices]
        self.press(pygame.K_e)
        self.choose("keymode", "set")
        self.choose("tonic", "D")
        self.choose("scale", "minor")
        self.ok()
        self.assertIsNone(self.a.overlay)
        self.assertEqual(self.a.ed.key_marks(), [KeyMark(0, 192, 2, "minor")])
        self.assertEqual([[(n.start, n.pitch) for n in v] for v in self.a.tune.voices], before)
        self.assertEqual(panels.key_labels(self.a.ed, self.a.roll.names), [(192, "D minor (V1)", [0])])
        self.a.frame()                                                  # the ruler draws it
        self.assertIn("Key D minor from bar 1", self.a.message)
        # edit it again: the row offers to remove it, and the dialog starts on its key
        self.pick(192)
        self.press(pygame.K_e)
        self.assertEqual((self.value("tonic"), self.value("scale")), (2, "minor"))
        self.assertIn("remove", [t for _, t in self.rows()["keymode"]["choices"]][2])
        self.choose("keymode", "remove")
        self.ok()
        self.assertEqual(self.a.ed.key_marks(), [])
        self.assertEqual([[(n.start, n.pitch) for n in v] for v in self.a.tune.voices], before)

    def test_key_for_all_voices_skips_the_voices_that_are_over(self):
        self.pick(192)
        self.press(pygame.K_e)
        self.choose("keymode", "set")
        self.choose("voices", "all")
        self.ok()
        self.assertEqual({(m.voice, m.tick) for m in self.a.ed.key_marks()}, {(0, 192), (1, 192)})

    def test_effects_and_key_are_one_undo_step(self):
        self.pick(192)
        original = self.a.song.to_mml()
        self.press(pygame.K_e)
        self.choose("vib", "deep")
        self.choose("keymode", "set")
        self.ok()
        self.assertEqual(len(self.a.ed.undo_stack), 1)
        self.assertTrue(self.a.ed.key_marks())
        self.assertEqual(self.a.roll.notes[0][4].marks, "~")
        self.a.frame([key(pygame.K_z, pygame.KMOD_CTRL)])
        self.assertEqual(self.a.song.to_mml(), original)

    def test_a_refused_edit_leaves_the_song_alone_and_the_dialog_open_as_it_was(self):
        self.a.load_song(Song.from_mml(PATTERN), "Pattern")
        self.a.frame()
        self.pick(96)                                                   # d, inside a pattern: the key cannot go there
        before = self.a.song.to_mml()
        self.press(pygame.K_e)
        self.choose("vib", "deep")
        self.choose("keymode", "set")
        self.ok()
        self.assertEqual(self.a.overlay, "fileopts")                    # back, so nothing typed is lost
        self.assertEqual(self.value("vib"), (6, 6))
        self.assertEqual(self.value("keymode"), "set")
        self.assertIn("pattern", self.a.message)
        self.assertEqual(self.a.song.to_mml(), before)                  # not even the vibrato went in
        self.assertFalse(self.a.ed.can_undo())
        self.choose("keymode", "leave")                                  # take the key out and it works
        self.ok()
        self.assertIsNone(self.a.overlay)
        self.assertTrue(any(n.marks == "~" for n in self.a.roll.notes[0]))

    def test_the_status_line_names_a_hovered_notes_effects(self):
        self.pick(48)
        self.a.edit(lambda: self.a.ed.set_note_effects([(0, 48)], vib=(5, 3), arp=(0, 4, 7)))
        self.a.note_hover = (0, 1)
        self.a.frame()
        from chipsynth.composer import model as M
        self.assertEqual(M.marks_words(self.a.roll.notes[0][1].marks), "vibrato, arpeggio")


class DrawTests(Base):
    def test_it_draws_at_small_and_large_sizes_with_the_panel_open(self):
        self.pick(48, 96)
        self.a.edit(lambda: self.a.ed.set_note_effects([(0, 48)], vib=(5, 3), slide=12, arp=(0, 3, 7), pws=(25, 75, 6)))
        self.a.ed.set_key_at(192, 2, "minor", voices=[0, 1])
        self.a.sync()
        for size in ((960, 600), (1280, 760), (1920, 1080)):
            self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)
            self.a.screen = self.screen
            for dark in (True, False):
                self.a.prefs.dark = dark
                for mode in ("roll", "stack", "tracker"):
                    self.a.set_mode(mode)
                    self.a.overlay = None
                    self.a.frame()
                    self.a.set_selection({(0, 48), (0, 96)})
                    self.a.open_note_panel()
                    self.a.frame()
                    self.assertEqual(self.a.overlay, "fileopts", (size, mode))
                    box = self.a.menu_geo["opts"]["ok"]
                    self.assertTrue(self.screen.get_rect().contains(box), (size, mode, box))     # the buttons stay on screen
                    self.a.fileopts_finish(False)
                    self.a.frame()


class RowTests(unittest.TestCase):
    """The pure part: rows and results, no window."""

    def test_a_uniform_selection_starts_on_its_value_and_a_mixed_one_on_keep(self):
        s = [dict(vib=(5, 3), arp=(), arprate=8, slide=0, pws=(0, 0, 0)), dict(vib=(5, 3), arp=(0, 4, 7), arprate=8, slide=12, pws=(0, 0, 0))]
        rows = {r[0]: r for r in NP.effect_rows(s)}
        self.assertEqual(rows["vib"][2][rows["vib"][3]][0], (5, 3))
        self.assertEqual(rows["arp"][2][rows["arp"][3]][0], NP.KEEP)
        self.assertEqual(rows["slide"][3], 0)
        self.assertEqual(rows["arprate"][2][rows["arprate"][3]][0], 8)

    def test_effects_from_only_lists_what_changed(self):
        s = [dict(vib=(5, 3), arp=(), arprate=8, slide=0, pws=(0, 0, 0))]
        rows = NP.effect_rows(s)
        values = {r[0]: r[2][r[3]][0] for r in rows}
        self.assertEqual(NP.effects_from(rows, values), {})
        values["slide"] = 24
        values["vib"] = NP.KEEP
        self.assertEqual(NP.effects_from(rows, values), {"slide": 24})

    def test_key_from(self):
        v = dict(keymode="set", tonic=2, scale="minor", voices="all")
        self.assertEqual(NP.key_from(v, 96, 1, [0, 1, 2]), dict(tick=96, tonic=2, scale="minor", voices=[0, 1, 2]))
        v["voices"] = "this"
        self.assertEqual(NP.key_from(v, 96, 1, [0, 1, 2])["voices"], [1])
        self.assertIsNone(NP.key_from(dict(keymode="none"), 96, 1, [0]))
        self.assertEqual(NP.key_from(dict(keymode="remove"), 96, 1, [0]), dict(tick=96, voices=[1], remove=True))
        self.assertEqual(NP.key_from(dict(keymode="off"), 0, 0, [0])["tonic"], None)


if __name__ == "__main__":
    unittest.main()
