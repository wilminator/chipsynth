# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The composer's File menu: New, Open, Save, Import and Export, as a mixin of the Composer.

Every action has a method that takes a path (`import_file`, `export_as`, `open_path`, `save`), so it runs without a
file dialog; the menu and its dialogs (`choose_path`) only find the path and the options, then call it.  What the
format code could not carry over (approximated / left out) goes to the status line and, when there is any, to the
report overlay; "Last report..." in the menu shows it again.

How a multi-song import behaves (FamiStudio songs, GoatTracker subtunes):
  * one song comes in as a new, unsaved song (Save asks where to put it), like opening a MIDI file;
  * several songs are written into a .csp package, as `python -m chipsynth import` does: the open song's package
    when it has a .csp file, else NAME.csp beside the imported file (an existing file keeps its other songs and the
    same ids are replaced).  The first of them opens.  The options dialog says where they will go.
Anything that replaces the open song first asks about unsaved edits (guard_switch).
"""
from pathlib import Path

import pygame

from chipsynth import exporter, mml, package as pk
from chipsynth.package import PackageError
from chipsynth.song import Song, SongError
from chipsynth.synth import RATE

from . import fileops as fo
from .files import backup_file, save_with_backup

BLANK = "title Untitled\ntempo 120\nV1: r1 r1 r1 r1\n"
SEP = None
WARNING = ("approximated:", "left out:", "warning:")


def significant(lines):
    """True when report lines hold something the person should read (not only facts)."""
    return any(x.startswith(WARNING) for x in lines)


class FileMenu:
    # ------------------------------------------------------------ the menu
    def menu_open(self):
        self.menu = dict(hi=self.first_enabled(self.menu_items(), 0, 1), sub=None, subhi=0, focus="main", fresh=True,
                         mouse=None)
        self.overlay = "menu"

    def menu_close(self):
        self.menu = None
        if self.overlay == "menu":
            self.overlay = None

    def menu_items(self):
        """The main column: dicts with label, hint, act (a method) or sub, enabled; None is a separator."""
        has = self.song is not None
        return [
            dict(label="New from generator...", hint="Ctrl+N", act=self.new_from_generator_dialog),
            dict(label="New blank song", act=self.new_blank),
            dict(label="Open...", hint="Ctrl+O", act=self.open_dialog),
            dict(label="Open from packages...", act=self.open_songs_overlay),
            SEP,
            dict(label="Save", hint="Ctrl+S", act=self.save, enabled=has),
            dict(label="Save As...", hint="Ctrl+Shift+S", act=self.save_as, enabled=has),
            SEP,
            dict(label="Import", sub="import"),
            dict(label="Export", sub="export", enabled=has),
            SEP,
            dict(label="Last report...", act=self.report_reopen, enabled=bool(self.last_report)),
        ]

    def submenu_items(self, which):
        has = self.song is not None
        if which == "import":
            return [dict(label=spec[0], act=(lambda k=kind: self.import_dialog_kind(k)), enabled=has or kind != "wav")
                    for kind, spec in fo.IMPORTS.items()]
        return [dict(label=spec[0], act=(lambda f=fmt: self.export_dialog(f)), enabled=has)
                for fmt, spec in fo.EXPORTS.items()]

    @staticmethod
    def first_enabled(items, start, step):
        """The index of the first usable row from `start` going `step` (wraps), or `start` if there is none."""
        n = len(items)
        for k in range(n):
            i = (start + step * k) % n
            if items[i] is not None and items[i].get("enabled", True):
                return i
        return start

    def menu_run(self, item):
        """Close the menu and do what the row says (a row with a submenu opens it instead)."""
        if item is None or not item.get("enabled", True):
            return False
        if item.get("sub"):
            self.menu["sub"], self.menu["subhi"], self.menu["focus"] = item["sub"], 0, "sub"
            self.menu["subhi"] = self.first_enabled(self.submenu_items(item["sub"]), 0, 1)
            return True
        self.menu_close()
        item["act"]()
        return True

    def menu_key(self, e):
        k, m = e.key, self.menu
        main = self.menu_items()
        sub = self.submenu_items(m["sub"]) if m["sub"] else None
        in_sub = sub is not None and m["focus"] == "sub"
        items, name = (sub, "subhi") if in_sub else (main, "hi")
        if k == pygame.K_ESCAPE:
            if in_sub:
                m["sub"], m["focus"] = None, "main"
            else:
                self.menu_close()
        elif k in (pygame.K_DOWN, pygame.K_UP):
            step = 1 if k == pygame.K_DOWN else -1
            m[name] = self.first_enabled(items, (m[name] + step) % len(items), step)
        elif k == pygame.K_RIGHT and not in_sub and main[m["hi"]] and main[m["hi"]].get("sub"):
            self.menu_run(main[m["hi"]])
        elif k == pygame.K_LEFT and in_sub:
            m["sub"], m["focus"] = None, "main"
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            self.menu_run(items[m[name]])
        elif k in (pygame.K_HOME, pygame.K_END):
            m[name] = self.first_enabled(items, 0 if k == pygame.K_HOME else len(items) - 1, 1 if k == pygame.K_HOME else -1)

    # ------------------------------------------------------------ small dialogs
    def choose_path(self, save, title, pattern, ext="", initialfile="", initialdir=None):
        """A file chooser (tkinter).  Returns the path, or "" when cancelled or there is no dialog on this machine."""
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            types = [(title, pattern), ("All files", "*.*")]
            if save:
                path = filedialog.asksaveasfilename(title=title, defaultextension=ext, initialfile=initialfile,
                                                    initialdir=initialdir, filetypes=types)
            else:
                path = filedialog.askopenfilename(title=title, initialdir=initialdir, filetypes=types)
            root.destroy()
            return path or ""
        except Exception:
            self.say("No file dialog here: drag the file onto the window (a .sng, .txt, .mid, .mml, .csp or .wav).")
            return ""

    def open_songs_overlay(self):
        self.overlay = "songs"
        self.overlay_scroll = 0

    def show_options(self, title, rows, apply, note="", ok="OK"):
        """The small options dialog.  rows: [(key, label, [(value, text), ...], start index)]; `apply` gets {key: value}."""
        self.fileopts = dict(title=title, note=note, ok=ok, apply=apply, focus=0,
                             rows=[dict(key=k, label=lab, choices=list(ch), i=i) for k, lab, ch, i in rows])
        self.overlay = "fileopts"

    def fileopts_values(self):
        return {r["key"]: r["choices"][r["i"]][0] for r in self.fileopts["rows"]}

    def fileopts_step(self, row, d):
        r = self.fileopts["rows"][row]
        r["i"] = (r["i"] + d) % len(r["choices"])
        self.fileopts["focus"] = row

    def fileopts_finish(self, ok):
        values = self.fileopts_values()
        d, self.fileopts, self.overlay = self.fileopts, None, None
        return bool(ok) and d["apply"](values) is not False

    def fileopts_key(self, e):
        k, d = e.key, self.fileopts
        if k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.fileopts_finish(True)
        elif k == pygame.K_ESCAPE:
            self.fileopts_finish(False)
        elif k in (pygame.K_UP, pygame.K_DOWN, pygame.K_TAB):
            back = k == pygame.K_UP or (k == pygame.K_TAB and e.mod & pygame.KMOD_SHIFT)
            d["focus"] = (d["focus"] + (-1 if back else 1)) % len(d["rows"])
        elif k in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_SPACE):
            self.fileopts_step(d["focus"], -1 if k == pygame.K_LEFT else 1)

    def show_report(self, title, lines, summary):
        """Say `summary` in the status line and keep the lines; open the report overlay when there is something to read."""
        self.last_report = (title, list(lines))
        self.say(summary, 8.0)
        if significant(lines):
            self.report_view = dict(title=title, lines=list(lines), scroll=0)
            self.overlay = "report"

    def report_reopen(self):
        if self.last_report:
            self.report_view = dict(title=self.last_report[0], lines=list(self.last_report[1]), scroll=0)
            self.overlay = "report"

    def report_key(self, e):
        k = e.key
        if k in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE, pygame.K_SPACE):
            self.overlay = self.report_view = None
        elif k in (pygame.K_UP, pygame.K_DOWN, pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            d = {pygame.K_UP: -1, pygame.K_DOWN: 1, pygame.K_PAGEUP: -8, pygame.K_PAGEDOWN: 8}[k]
            self.report_view["scroll"] = max(0, self.report_view["scroll"] + d)

    # ------------------------------------------------------------ New
    def new_blank(self, guard=True):
        """New > a blank song: four empty bars on one voice, unsaved."""
        if guard and self.song_dirty:
            return self.guard_switch(lambda: self.new_blank(guard=False), "start a new song")
        song = Song.from_mml(BLANK, self.lib)
        if self.load_song(song, "Untitled", ""):
            self.say("A blank song: click the grid to add notes. Save to keep it.", 5.0)
            return True
        return False

    # ------------------------------------------------------------ Import
    def import_dialog_kind(self, kind):
        """Import > KIND: choose the file, then import_begin."""
        _, title, pattern, _ = fo.IMPORTS[kind]
        if kind == "wav":
            return self.import_dialog()
        path = self.choose_path(False, title, pattern, initialdir=str(Path(self.source).parent) if self.source else None)
        return bool(path) and self.import_begin(path, kind)

    def import_begin(self, path, kind=None):
        """A file has been chosen: ask for the options its format needs (the region of a GoatTracker song, which of
        several songs...), then import_file.  Formats that need nothing import at once."""
        kind = kind or fo.kind_of(path)
        if kind is None:
            self.say("Can't tell what %s is: import takes .mml, .mid, .txt (FamiStudio), .sng (GoatTracker) or .wav." % Path(path).name, 8.0)
            return False
        if kind not in ("famistudio", "goattracker"):
            return self.import_file(path, kind)
        try:
            names = fo.song_names(kind, path)
        except fo.FileOpError as e:
            self.say("Can't import: %s" % e, 8.0)
            return False
        if kind == "famistudio" and len(names) < 2:
            return self.import_file(path, kind)
        if len(names) > 1:
            songs = [("", "All %d songs" % len(names))] + [(str(i + 1), n) for i, n in enumerate(names)]
        else:
            songs = [("", names[0] if names else "the song")]
        target = self.import_target(path)
        note = ""
        if len(names) > 1:
            note = ("Several songs go into %s (%s) and the first opens; one song opens unsaved."
                    % (target.name, "the open package" if self.package_path() else "new, beside the file"))
        if kind == "famistudio":
            rows = [("region", "Timing", [(None, "as the file says"), ("ntsc", "NTSC"), ("pal", "PAL")], 0),
                    ("song", "Songs", songs, 0)]
        else:
            rows = [("region", "Region", [("pal", "PAL (50 Hz)"), ("ntsc", "NTSC (60 Hz)")], 0),
                    ("multispeed", "Multispeed", [(1, "x1"), (2, "x2"), (4, "x4")], 0),
                    ("song", "Subtune", songs, 0)]
            note = ("The file does not record its region or multispeed. " + note).strip()
        self.show_options("Import %s" % Path(path).name, rows, ok="Import", note=note, apply=lambda v: self.import_file(
            path, kind, region=v["region"], multispeed=v.get("multispeed", 1), song=v["song"] or None))
        return True

    def package_path(self):
        """The .csp file of the open song's package, or None (a loose song, a new song, a .mml)."""
        if not self.song_id or ":" not in self.song_id or not self.packages.has(self.song_id):
            return None
        name, _ = self.packages.split(self.song_id)
        p = Path(self.packages.paths[name])
        return p if p.suffix.lower() == ".csp" else None

    def import_target(self, path):
        """Where several imported songs go: the open package, else NAME.csp beside the imported file."""
        return self.package_path() or Path(path).with_suffix(".csp")

    def import_file(self, path, kind=None, region=None, multispeed=1, song=None, package=None, guard=True):
        """Import a file of any kind the File menu knows (see the module doc).  Returns True when it came in.
        `song` picks one song of a multi-song file; `package` is the .csp several songs go to."""
        path = Path(path)
        kind = kind or fo.kind_of(path)
        if kind == "wav":
            return self.import_wav(path)
        if kind == "mid":
            return self.open_path(path, guard=guard)
        if kind == "mml":
            return self.import_mml(path, guard)
        if kind not in ("famistudio", "goattracker"):
            self.say("Can't tell what %s is: import takes .mml, .mid, .txt (FamiStudio), .sng (GoatTracker) or .wav." % path.name, 8.0)
            return False
        try:
            loaded = fo.read_import(kind, path, self.lib, region=region, multispeed=multispeed, song=song)
        except fo.FileOpError as e:
            self.say("Can't import: %s" % e, 8.0)
            return False
        if not loaded.songs:
            self.say("%s has no songs to import." % path.name, 6.0)
            return False
        target = Path(package) if package else self.import_target(path)
        if guard and self.song_dirty:
            return self.guard_switch(lambda: self.apply_import(loaded, kind, target), "import " + path.name)
        return self.apply_import(loaded, kind, target)

    def import_mml(self, path, guard=True):
        """Import > MML: the text becomes a new unsaved song (open it with Open to edit the file itself)."""
        if guard and self.song_dirty:
            return self.guard_switch(lambda: self.import_mml(path, guard=False), "import " + Path(path).name)
        try:
            song = Song.from_mml(Path(path).read_text(encoding="utf-8", errors="replace"), self.lib)
        except (mml.MMLError, SongError, OSError) as e:
            self.say("Can't import %s: %s" % (Path(path).name, e), 8.0)
            return False
        if self.load_song(song, song.title or Path(path).stem, ""):
            self.say("Imported %s. Save to keep it." % Path(path).name, 6.0)
            return True
        return False

    def apply_import(self, loaded, kind, target):
        """Put read songs in the window (one song) or into a package file (several); show the report."""
        lines = fo.report_lines(loaded.report)
        label = "FamiStudio" if kind == "famistudio" else "GoatTracker"
        if len(loaded.songs) == 1:
            sid, song, samples, title = loaded.songs[0]
            lib = None
            if samples:
                lib = dict(self.lib)
                lib["_mem"] = {**(self.lib.get("_mem") or {}), **samples}
            if not self.load_song(song, song.title or title, "", "", lib=lib):
                return False
            self.imported.update(samples)
            what = "Imported %s from %s" % (title or sid, loaded.source)
        else:
            try:
                first = self.import_into_package(loaded, target)
            except (PackageError, OSError, mml.MMLError, SongError) as e:
                self.say("Can't import into %s: %s" % (target.name, e), 8.0)
                return False
            if not self.open_song_id(first, guard=False):
                return False
            what = "Imported %d songs from %s into %s" % (len(loaded.songs), loaded.source, target.name)
        ap, dr = fo.count_changes(lines)
        extra = ""
        if ap or dr:
            extra = ": %d thing%s approximated, %d left out" % (ap, "" if ap == 1 else "s", dr)
        self.show_report("%s import of %s" % (label, loaded.source), lines or ["note: nothing was changed or left out"],
                         what + extra + ("." if extra else ". Save to keep it." if len(loaded.songs) == 1 else "."))
        return True

    def import_into_package(self, loaded, target):
        """Add the songs to the package file `target` (created if missing; same ids are replaced) and load it.
        Returns the id of the first song as the PackageSet knows it."""
        target = Path(target)
        pkg = pk.load(target) if target.exists() else pk.Package(name=pk.Package.safe_id(target.stem))
        for sid, song, samples, _title in loaded.songs:
            lib = dict(self.lib)
            lib["_mem"] = {**(self.lib.get("_mem") or {}), **samples}
            pkg.add_song(sid, song, lib)
        save_with_backup(self.root or target.parent, target, pk.dumps(pkg))
        for n, src in list(self.packages.paths.items()):
            if Path(src) == target:
                self.packages.remove(n)                 # read again below, so what it holds is what is on disk
        self.packages.add(target)
        return "%s:%s" % (pkg.name, loaded.songs[0][0])

    # ------------------------------------------------------------ Export
    def export_dialog(self, fmt):
        """Export > FORMAT: the options the format has, then the file chooser, then export_as."""
        if self.song is None:
            self.say("There is no song to export.", 3.0)
            return False
        if fmt == "wav":
            sel = self.selection_range()
            rows = [("loops", "Loop trips", [(n, "none" if n == 0 else "%d more" % n) for n in range(4)], 0),
                    ("normalize", "Level", [(False, "as it is"), (True, "normalize to -1 dBFS")], 0),
                    ("part", "Part", [("song", "whole song")] + ([("selection", "selected notes")] if sel else []), 0)]
            self.show_options("Export WAV", rows, ok="Choose file...",
                              note="Trips only count when the song loops; the last notes ring for 2 seconds.",
                              apply=lambda v: self.export_choose(fmt, **v))
            return True
        if fmt == "famistudio":
            self.show_options("Export FamiStudio text", [("region", "Timing", [("ntsc", "NTSC"), ("pal", "PAL")], 0)],
                              ok="Choose file...", note="V1-V4 and one DPCM track only; what is left out is reported.",
                              apply=lambda v: self.export_choose(fmt, **v))
            return True
        return self.export_choose(fmt)

    def export_choose(self, fmt, **opts):
        _, title, pattern, ext = fo.EXPORTS[fmt]
        stem = Path(self.source).stem if self.source else pk.Package.safe_id(self.label or "song")
        path = self.choose_path(True, title, pattern, ext, stem, str(Path(self.source).parent) if self.source else None)
        return bool(path) and self.export_as(fmt, path, **opts)

    def export_package(self):
        """(a one-song Package of the song as it is now, its id), for exporter.export_song."""
        sid = self.song_id.split(":", 1)[1] if ":" in self.song_id else pk.Package.safe_id(
            Path(self.source).stem if self.source else self.label, "song")
        pkg = pk.Package(name=sid, author=self.song.author)
        pkg.add_song(sid, self.song, self.ed.lib)
        return pkg, sid

    def export_as(self, fmt, path, loops=0, normalize=False, part="song", region="ntsc"):
        """Write the open song to `path` as wav, mid, mml, famistudio or csp with exporter.export_song, the code
        `python -m chipsynth export-song` runs.  A file it replaces is copied to backups first.  Returns True."""
        if self.song is None:
            self.say("There is no song to export.", 3.0)
            return False
        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(fo.EXPORTS[fmt][3])
        opts = exporter.AudioOptions(loops=loops, normalize=normalize)
        extra = []
        if fmt == "wav" and part == "selection":
            sel = self.selection_range()
            if sel is None:
                extra.append("warning: no notes are selected, so the whole song was exported")
            else:
                tune = self.ed.tune
                opts.start, opts.end = tune.tick_to_sample(sel[0]), tune.tick_to_sample(sel[1]) + 1 * RATE
        try:
            pkg, sid = self.export_package()
            if fmt != "mml":
                backup_file(self.root or path.parent, path)
            r = exporter.export_song(pkg, sid, path, opts, fmt, lib=self.ed.lib, region=region)
        except (exporter.ExportError, PackageError, SongError, mml.MMLError, OSError) as e:
            self.say("Can't export %s: %s" % (path.name, e), 8.0)
            return False
        lines = list(r.notes)
        if fmt != "famistudio":                          # only FamiStudio's notes include plain facts
            lines = ["warning: " + x for x in lines]
        else:
            lines = [x if x.startswith(WARNING) else "note: " + x for x in lines]
        if fmt == "mid" and self.roll is not None and any(self.roll.dac):
            lines.append("left out: the drum / sample lanes (D1, D2) are not in a MIDI file")
        lines = extra + lines
        what = "Exported %s" % path.name
        if fmt == "wav":
            what += " (%.1fs, peak %s)" % (r.seconds, "silent" if r.peak_db is None else "%.1f dBFS" % r.peak_db)
        elif len(r.written) > 1:
            what += " and %d sample file%s" % (len(r.written) - 1, "" if len(r.written) == 2 else "s")
        ap, dr = fo.count_changes(lines)
        warn = sum(1 for x in lines if x.startswith("warning:"))
        extra_text = ", ".join(t for t in (("%d approximated" % ap) if ap else "", ("%d left out" % dr) if dr else "",
                                           ("%d warning%s" % (warn, "" if warn == 1 else "s")) if warn else "") if t)
        self.show_report("Export of %s" % path.name, lines or ["note: nothing was changed or left out"],
                         what + (" (%s)." % extra_text if extra_text else "."))
        return True
