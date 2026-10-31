# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the composer's File menu does with files, without any window: read a foreign file into songs plus the report
of what could not be carried over (the same loaders `python -m chipsynth import` uses), and the tables of formats.

    names = song_names("goattracker", path)                  # what the file holds, to ask which
    loaded = read_import("famistudio", path, lib, region="pal", song=None)
    loaded.songs     # [(song id, Song, {sample name: Sample}, title)]
    loaded.report    # a formats.report.Report (or None for a format that has none)
"""
from dataclasses import dataclass, field
from pathlib import Path

from chipsynth import package as pk
from chipsynth.formats import goattracker, famistudio_text as fam
from chipsynth.formats.report import Report

# kind -> (menu label, dialog title, file patterns, suffixes it is recognised by)
IMPORTS = {
    "mml": ("MML text...", "Import MML", "*.mml", (".mml",)),
    "mid": ("MIDI file...", "Import MIDI", "*.mid *.midi", (".mid", ".midi")),
    "famistudio": ("FamiStudio text (.txt)...", "Import FamiStudio text", "*.txt", (".txt",)),
    "goattracker": ("GoatTracker 2 (.sng)...", "Import GoatTracker song", "*.sng", (".sng",)),
    "wav": ("WAV sample...", "Import a WAV as a drum sample", "*.wav", (".wav",)),
}
# format -> (menu label, dialog title, file pattern, default extension)
EXPORTS = {
    "wav": ("WAV audio...", "Export WAV", "*.wav", ".wav"),
    "mid": ("MIDI file...  Ctrl+E", "Export MIDI", "*.mid", ".mid"),
    "mml": ("MML text...", "Export MML", "*.mml", ".mml"),
    "famistudio": ("FamiStudio text (.txt)...", "Export FamiStudio text", "*.txt", ".txt"),
    "csp": ("Package copy (.csp)...", "Export a one-song package", "*.csp", ".csp"),
}


class FileOpError(Exception):
    pass


@dataclass
class Loaded:
    songs: list = field(default_factory=list)       # [(song id, Song, samples, title)]
    report: Report = None
    source: str = ""                                # what the file is called in messages


def kind_of(path):
    """The import kind a file name suggests, or None."""
    suffix = Path(path).suffix.lower()
    for kind, spec in IMPORTS.items():
        if suffix in spec[3]:
            return kind
    return None


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise FileOpError("can't read %s (%s)" % (Path(path).name, e))


def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except OSError as e:
        raise FileOpError("can't read %s (%s)" % (Path(path).name, e))


def song_names(kind, path):
    """The names of the songs a FamiStudio or GoatTracker file holds (empty for other kinds), in file order."""
    name = Path(path).name
    try:
        if kind == "famistudio":
            text = _read_text(path)
            if not fam.looks_like(text):
                raise FileOpError("%s is not a FamiStudio text file (it should start with a Project line)" % name)
            return [s.attrs.get("Name", "Song %d" % (i + 1)) for i, s in enumerate(fam.parse(text).songs)]
        if kind == "goattracker":
            data = _read_bytes(path)
            if not goattracker.looks_like(data):
                raise FileOpError("%s is not a GoatTracker song (it should start with GTS5)" % name)
            tune = goattracker.parse(data)
            return ["subtune %d" % (i + 1) for i in range(len(tune.orders))]
    except (fam.FamiStudioError, goattracker.GoatTrackerError) as e:
        raise FileOpError("%s: %s" % (name, e))
    return []


def read_import(kind, path, lib=None, region=None, multispeed=1, song=None):
    """A FamiStudio or GoatTracker file -> Loaded.  `song` picks one (FamiStudio: a name or number; GoatTracker: the
    subtune number).  Song ids follow the command line: the file name, with _NAME (or _N) added when it holds several."""
    name = Path(path).name
    stem = pk.Package.safe_id(Path(path).stem)
    out = Loaded(source=name)
    try:
        if kind == "famistudio":
            text = _read_text(path)
            if not fam.looks_like(text):
                raise FileOpError("%s is not a FamiStudio text file (it should start with a Project line)" % name)
            res = fam.load(text, song=song, region=region)
            out.report = res.report
            for imp in res.songs:
                sid = stem if len(res.songs) == 1 else "%s_%s" % (stem, pk.Package.safe_id(imp.name, "song"))
                out.songs.append((sid, imp.song, imp.samples, imp.name))
        elif kind == "goattracker":
            data = _read_bytes(path)
            if not goattracker.looks_like(data):
                raise FileOpError("%s is not a GoatTracker song (it should start with GTS5)" % name)
            sub = int(song) if song not in (None, "") else None
            res = goattracker.load(data, region=region or "pal", multispeed=multispeed, subtune=sub)
            out.report = res.report
            for n, imp in enumerate(res.songs, 1):
                sid = stem if len(res.songs) == 1 else "%s_%d" % (stem, sub or n)
                out.songs.append((sid, imp.song, imp.samples, imp.name))
        else:
            raise FileOpError("don't know how to import a '%s' file here" % kind)
    except ValueError as e:                  # FamiStudioError and GoatTrackerError are ValueErrors; so is a bad subtune number
        raise FileOpError("%s: %s" % (name, e))
    return out


def report_lines(report):
    """The Report as lines of text (notes first, then approximated, then left out)."""
    return report.lines() if report is not None else []


def count_changes(lines):
    """(approximated, left out) counts in report lines."""
    return (sum(1 for x in lines if x.startswith("approximated:")), sum(1 for x in lines if x.startswith("left out:")))
