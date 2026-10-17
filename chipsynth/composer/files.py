# Copyright (C) 2026 Michael Allen Wilmes
"""File helpers the composer needs: a game folder's music layout and saving with a backup (no dependency on the game)."""
import shutil
import time
from pathlib import Path

from chipsynth import mml


def tune_dir(root):
    """Where a game folder keeps its music."""
    return Path(root) / "music"


_LIB = {}


def library(root):
    """The folder's music library (music/lib, and the samples beside it), or the built-in one.  Cached until a file in
    music/lib changes."""
    d = tune_dir(root) / "lib"
    try:
        stamp = tuple(sorted((p.name, p.stat().st_mtime_ns) for p in d.glob("*.mml"))) if d.is_dir() else ()
        hit = _LIB.get(str(root))
        if hit and hit[0] == stamp:
            return hit[1]
        lib = mml.load_library(d)
    except OSError:
        return mml.builtin_library()
    _LIB[str(root)] = (stamp, lib)
    return lib


def backup_file(root, path):
    """Copy an existing file to <root>/backups/<name>.<timestamp>; returns the copy or None."""
    path = Path(path)
    if not path.exists():
        return None
    dest = Path(root) / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = dest / ("%s.%s" % (path.name, stamp))
    n = 1
    while out.exists():                      # two saves inside the same second
        out = dest / ("%s.%s-%d" % (path.name, stamp, n))
        n += 1
    shutil.copyfile(path, out)
    return out


def save_with_backup(root, path, data):
    """Back up what is being replaced, then write.  Saving identical bytes is a no-op."""
    path = Path(path)
    if path.exists() and path.read_bytes() == data:
        return False
    backup_file(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True
