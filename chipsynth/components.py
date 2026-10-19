# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The parts of a package file, for the command line: list them, filter them, write them out as MML, put MML in.

    comps = components(pkg)                                  # every song, instrument, effect set, scale and sample
    comps = select(comps, types=["song"], names=["d*"])      # the filter `list` and `export` use
    export_mml(pkg, comps, "out/")                           # out/dawn.mml, out/lib/forest.mml, out/samples/kick.dpcm
    import_mml(pkg, text, lib, song_id="dawn")               # a song from MML text (shared=True: a library file)

A component has a type, a name and a scope: "shared" for what the package holds for all its songs, or the id of the
song that holds it (`add_song` copies what a song uses into the song, so most instruments live in a song).  A song
is always written as one self-contained .mml; the lib file holds the other selected components.  The layout under
the output folder is a game's `music/` folder: songs at the top, `lib/` for the library file, `samples/` for DPCM.
"""
import copy
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from . import dpcm, mml, package as pk
from .song import Play, Song
from .songmml import instrument_text

TYPES = ("song", "instrument", "fx", "scale", "sample")
ALIASES = {"songs": "song", "instr": "instrument", "instruments": "instrument", "inst": "instrument",
           "effect": "fx", "effects": "fx", "key": "scale", "keys": "scale", "scales": "scale",
           "samples": "sample", "smp": "sample"}
SHARED = "shared"
FILE_SAMPLE = re.compile(r"[A-Za-z_]+$")           # the names the [sample] section can load from a .dpcm file


class ComponentError(Exception):
    pass


@dataclass
class Component:
    type: str
    name: str
    scope: str = SHARED                              # SHARED, or the id of the song that holds it
    detail: str = ""
    obj: object = None                               # Song / Instrument / [(key, value)] / steps / Sample / (file, rate)


def parse_types(values):
    """['song,sample', 'fx'] -> ['song', 'sample', 'fx'] (aliases accepted; 'all' is every type)."""
    out = []
    for v in values or []:
        for t in v.split(","):
            t = t.strip().lower()
            if not t:
                continue
            t = "all" if t == "all" else ALIASES.get(t, t)
            if t == "all":
                out.extend(x for x in TYPES if x not in out)
            elif t not in TYPES:
                raise ComponentError("unknown component type '%s' (types: %s)" % (t, ", ".join(TYPES)))
            elif t not in out:
                out.append(t)
    return out


def components(pkg, lib=None):
    """Every component of a package, songs first: the package's shared ones, then those each song holds itself."""
    base = lib or mml.builtin_library()
    out = []
    for sid, song in pkg.songs.items():
        try:
            secs = "%.1fs" % pkg.tune(sid, base).seconds
        except mml.MMLError as e:
            secs = "error"
        out.append(Component("song", sid, SHARED, "%s  %s" % (secs, song.title or ""), song))
    parts = [(SHARED, pkg)] + [(sid, s) for sid, s in pkg.songs.items()]
    for scope, holder in parts:
        for k, v in holder.instruments.items():
            out.append(Component("instrument", k, scope, instrument_text(k, v).partition(" ")[2], v))
        for k, v in holder.fx.items():
            out.append(Component("fx", k, scope, " ".join("%s=%s" % kv for kv in v), v))
        for k, v in holder.keys.items():
            out.append(Component("scale", k, scope, " ".join(str(x) for x in v), v))
    for k, smp in pkg.samples.items():
        size = sum(len(p) for t in smp.tracks for p in t)
        out.append(Component("sample", k, SHARED, "rate %d, %d byte(s), %s" % (
            smp.rate, size, "stereo" if smp.stereo else "mono"), smp))
    for sid, song in pkg.songs.items():
        for k, (f, r) in song.samples.items():
            out.append(Component("sample", k, sid, "file %s%s" % (f, " rate=%d" % r if r is not None else ""), (f, r)))
    return out


def select(comps, types=None, names=None):
    """Keep the components of the given types (None: all) whose name matches a glob in `names` (None: all).
    A name may be written TYPE:NAME to match one type only."""
    types = parse_types(types)
    out = []
    for c in comps:
        if types and c.type not in types:
            continue
        if names and not any(_matches(c, n) for n in names):
            continue
        out.append(c)
    return out


def _matches(c, pattern):
    t, colon, rest = pattern.partition(":")
    if colon and ALIASES.get(t.lower(), t.lower()) in TYPES:
        return ALIASES.get(t.lower(), t.lower()) == c.type and fnmatch.fnmatchcase(c.name, rest)
    return fnmatch.fnmatchcase(c.name, pattern)


# ------------------------------------------------------------------ out
def song_samples(song):
    """The names of the samples a song's DAC tracks play."""
    return sorted({e.sample for e in pk._all_events(song) if isinstance(e, Play)})


def song_to_mml(pkg, sid, notes=None):
    """(MML text, {sample name: Sample}) for one song of a package.  The samples a song plays are held by the package,
    so the text declares each one under [sample] (read from NAME.dpcm in the samples folder) and they come back
    to be written there.  Names the [sample] section cannot load are reported in `notes`."""
    song = copy.deepcopy(pkg.songs[sid])
    files = {}
    for name in song_samples(song):
        if name in song.samples or name not in pkg.samples:
            continue
        files[name] = pkg.samples[name]
        if FILE_SAMPLE.match(name):
            song.samples[name] = (name, None)
        elif notes is not None:
            notes.append("song '%s': sample '%s' has digits in its name, so MML cannot load it from a file; "
                         "import the .csp instead" % (sid, name))
    return song.to_mml(), files


def lib_to_mml(pkg, comps):
    """(MML text, {sample name: Sample}) of a library file holding the selected non-song components (header-only
    MML: [instr] / [fx] / [key] / [sample]).  When two songs hold a different thing under one name, the first wins."""
    lib = Song(title=pkg.name)
    files = {}
    for c in comps:
        if c.type == "instrument":
            lib.instruments.setdefault(c.name, c.obj)
        elif c.type == "fx":
            lib.fx.setdefault(c.name, c.obj)
        elif c.type == "scale":
            lib.keys.setdefault(c.name, c.obj)
        elif c.type == "sample":
            if isinstance(c.obj, dpcm.Sample):
                files[c.name] = c.obj
                lib.samples.setdefault(c.name, (c.name, None))
            else:
                lib.samples.setdefault(c.name, c.obj)
    return lib.to_mml(), files


def export_mml(pkg, comps, outdir, notes=None):
    """Write the selected components under `outdir`; returns the paths written, in order.  Songs become `ID.mml`; the
    others go in one library file `lib/PACKAGE.mml` (their samples as `samples/NAME.dpcm`)."""
    out = Path(outdir)
    written = []
    others = [c for c in comps if c.type != "song"]
    for c in comps:
        if c.type != "song":
            continue
        text, files = song_to_mml(pkg, c.name, notes)
        written.append(_write(out / (c.name + ".mml"), text))
        written.extend(_write_samples(out / "samples", files))
    if others:
        text, files = lib_to_mml(pkg, others)
        name = pkg.name if pk.ID.match(pkg.name or "") else "package"
        written.append(_write(out / "lib" / (name + ".mml"), text))
        written.extend(_write_samples(out / "samples", files))
    return list(dict.fromkeys(written))


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _write_samples(folder, files):
    out = []
    for name, smp in files.items():
        p = folder / (name + ".dpcm")
        dpcm.save(p, smp)
        out.append(p)
    return out


# ------------------------------------------------------------------ in
def import_mml(pkg, text, lib=None, song_id=None, shared=False):
    """Put MML text in a package.  As a song (the default) it is stored as `song_id`, with what it uses from `lib`
    copied in; returns the Song.  With shared=True the text is a library file: its instruments, effect sets, scales
    and samples are added to the package for all its songs (a name already there is replaced); returns the Song read."""
    lib = lib or mml.builtin_library()
    song = Song.from_mml(text, lib)
    if not shared:
        pkg.add_song(song_id, song, lib)
        return pkg.songs[song_id]
    pkg.instruments.update(song.instruments)
    pkg.fx.update(song.fx)
    pkg.keys.update(song.keys)
    finder = mml._Parser("", lib)
    finder.sampledefs = dict(song.samples)
    for name in song.samples:
        smp = finder.find_sample(name)
        if smp is None:
            raise ComponentError("sample '%s' (%s) was not found; give the folder holding its .dpcm file (--samples)"
                                 % (name, song.samples[name][0]))
        pkg.samples[name] = copy.copy(smp)
        pkg.samples[name].name = name
    return song
