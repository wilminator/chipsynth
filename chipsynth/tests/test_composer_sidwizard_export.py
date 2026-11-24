# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""File > Export > SID-Wizard in the composer: the menu entry, the option dialog and the written file."""
import unittest

from chipsynth import mml
from chipsynth.formats import sidwizard as sw
from chipsynth.tests.test_composer_filemenu import Base


class SidWizardExportTests(Base):
    def test_the_file_menu_has_a_sid_wizard_export_entry(self):
        from chipsynth.composer import fileops as fo
        self.assertEqual(fo.EXPORTS["sidwizard"][3], ".swm")
        self.open_menu()
        self.click(self.row("Export"))
        labels = [i["label"] for _, i in self.a.menu_geo["sub"]]
        self.assertTrue(any(x.startswith("SID-Wizard") for x in labels), labels)

    def test_export_as_writes_a_swm_the_importer_reads_and_reports(self):
        a = self.a
        self.assertTrue(a.export_as("sidwizard", self.dir / "o.swm"))
        data = (self.dir / "o.swm").read_bytes()
        self.assertTrue(sw.looks_like(data))
        self.assertEqual(sw.load(data, "pal").songs[0].song.voices, 3)
        self.assertEqual(a.overlay, "report")
        self.assertTrue(any("two video frames" in x for x in a.last_report[1]), a.last_report[1])
        a.frame()

    def test_the_suffix_is_added_and_the_options_are_used(self):
        a = self.a
        self.assertTrue(a.export_as("sidwizard", self.dir / "bare", region="ntsc", multispeed=2))
        data = (self.dir / "bare.swm").read_bytes()
        self.assertEqual(data[2 + 4], 2)
        self.assertTrue(any("NTSC" in x for x in a.last_report[1]), a.last_report[1])

    def test_the_dialog_asks_for_region_multispeed_and_tempo(self):
        a = self.a
        seen = {}
        a.export_choose = lambda fmt, **kw: seen.update(fmt=fmt, **kw) or True
        self.assertTrue(a.export_dialog("sidwizard"))
        self.assertEqual(a.overlay, "fileopts")
        a.frame()
        self.assertIn("SID-Wizard", a.fileopts["title"])
        self.assertEqual([r["key"] for r in a.fileopts["rows"]], ["region", "multispeed", "gt_tempo"])
        self.assertEqual(a.fileopts_values(), {"region": "pal", "multispeed": 1, "gt_tempo": "exact"})
        a.fileopts_step(1, 1)
        self.assertTrue(a.fileopts_finish(True))
        self.assertEqual((seen["fmt"], seen["multispeed"], seen["region"]), ("sidwizard", 2, "pal"))


if __name__ == "__main__":
    unittest.main()
