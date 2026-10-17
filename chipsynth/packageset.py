# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Any number of packages at once, and a way to pick a song from them (PLAN-CHIPSYNTH.md, Part C).

    pkgs = PackageSet(lib)                    # lib: the game's library (music/lib), or None for the built-in one
    pkgs.add("music/forest.csp")              # a package file
    pkgs.add("music/lute.mml")                # a bare .mml is a one-song package named after the file: "lute:lute"
    pkgs.add_folder("music")                  # every .csp (and .mml unless mml=False) in a folder; mistakes go to .problems
    pkgs.songs()                              # [SongRef("forest:dawn", title, ...)]
    pkgs.player("forest:dawn", loop=True)     # a TunePlayer, ready for MusicOut

Song ids are "package:song".  Two packages with the same name do not silently replace each other: the second is
refused and the reason is kept in `.problems`.
"""
from dataclasses import dataclass
from pathlib import Path

from . import mml, package as pk
from .player import TunePlayer
from .song import Song


@dataclass
class SongRef:
    id: str
    package: str
    song: str
    title: str
    path: str
    set: object = None

    @property
    def seconds(self):
        """Playing time of one pass (compiles the song the first time)."""
        return self.set.tune(self.id).seconds


class PackageSet:
    def __init__(self, lib=None):
        self.lib = lib or mml.builtin_library()
        self.packages = {}              # name -> Package
        self.paths = {}                 # name -> source path
        self.problems = []
        self._tunes = {}

    # ---- loading
    def add(self, path):
        """Load one .csp or .mml file; returns the Package.  Raises PackageError / MMLError if it cannot be read."""
        p = Path(path)
        if p.suffix.lower() == ".mml":
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise pk.PackageError("can't read %s (%s)" % (p.name, e))
            if not pk.ID.match(p.stem):
                raise pk.PackageError("'%s': a file name used as a package name is letters, digits and _ . -" % p.name)
            pkg = pk.Package(name=p.stem)
            pkg.songs[p.stem] = Song.from_mml(text, self.lib)
        else:
            pkg = pk.load(p)
            if not pk.ID.match(pkg.name):
                raise pk.PackageError("'%s': the package name '%s' is not letters, digits and _ . -" % (p.name, pkg.name))
        if pkg.name in self.packages:
            raise pk.PackageError("'%s': a package named '%s' is already loaded (from %s)"
                                  % (p.name, pkg.name, self.paths[pkg.name]))
        self.packages[pkg.name] = pkg
        self.paths[pkg.name] = str(p)
        self._tunes = {k: v for k, v in self._tunes.items() if not k.startswith(pkg.name + ":")}
        return pkg

    def add_folder(self, folder, mml_files=True):
        """Load every package in a folder (not its subfolders), packages first, then loose .mml files.  A file that
        cannot be loaded is skipped and reported in .problems.  Returns how many were loaded."""
        d = Path(folder)
        if not d.is_dir():
            return 0
        names = sorted(d.glob("*.csp")) + (sorted(d.glob("*.mml")) if mml_files else [])
        n = 0
        for p in names:
            try:
                self.add(p)
                n += 1
            except (pk.PackageError, mml.MMLError) as e:
                self.problems.append("%s: %s" % (p.name, e))
        return n

    def remove(self, name):
        self.packages.pop(name, None)
        self.paths.pop(name, None)
        self._tunes = {k: v for k, v in self._tunes.items() if not k.startswith(name + ":")}

    # ---- looking up
    def split(self, song_id):
        pkg, colon, song = song_id.partition(":")
        if not colon or not pkg or not song:
            raise pk.PackageError("a song id looks like package:song (got %r)" % song_id)
        if pkg not in self.packages:
            raise pk.PackageError("no package '%s' is loaded" % pkg)
        if song not in self.packages[pkg].songs:
            raise pk.PackageError("no song '%s' in package '%s'" % (song, pkg))
        return pkg, song

    def has(self, song_id):
        try:
            self.split(song_id)
            return True
        except pk.PackageError:
            return False

    def songs(self):
        return [SongRef("%s:%s" % (n, sid), n, sid, s.title or sid, self.paths[n], self)
                for n, p in self.packages.items() for sid, s in p.songs.items()]

    def get(self, song_id):
        pkg, song = self.split(song_id)
        return self.packages[pkg].songs[song]

    def tune(self, song_id):
        if song_id not in self._tunes:
            pkg, song = self.split(song_id)
            self._tunes[song_id] = self.packages[pkg].tune(song, self.lib)
        return self._tunes[song_id]

    def player(self, song_id, loop=True, volume=1.0):
        return TunePlayer(self.tune(song_id), loop=loop, volume=volume)
