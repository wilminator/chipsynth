# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""A package as text, for diffs and debugging (`python -m chipsynth dump` / `build`).

    #! package          name: forest / author: ... / comment: ... / created: ... / modified: ...
    #! shared           [instr] [fx] [key] sections (only when the package shares any)
    #! sample kick      the sample's .dpcm bytes in base64
    #! song dawn        the song as MML (what Song.to_mml writes)

`build_text(dump_text(pkg))` gives back a package that saves to the same bytes.
"""
import base64
import textwrap

from . import dpcm, mml, package as pk
from .song import Song

HEAD = "#! "
FIELDS = ("name", "author", "comment", "created", "modified")


def dump_text(pkg):
    out = [HEAD + "package"]
    for k in FIELDS:
        out.append("%s: %s" % (k, getattr(pkg, k)))
    if pkg.instruments or pkg.fx or pkg.keys:
        out.append(HEAD + "shared")
        out.append(Song(instruments=pkg.instruments, fx=pkg.fx, keys=pkg.keys).to_mml().rstrip("\n"))
    for name, smp in pkg.samples.items():
        out.append(HEAD + "sample " + name)
        out.extend(textwrap.wrap(base64.b64encode(dpcm.dumps(smp)).decode("ascii"), 76))
    for sid, song in pkg.songs.items():
        out.append(HEAD + "song " + sid)
        out.append(song.to_mml().rstrip("\n"))
    return "\n".join(out) + "\n"


def build_text(text):
    blocks, cur = [], None
    for line in text.split("\n"):
        if line.startswith(HEAD):
            cur = [line[len(HEAD):].strip(), []]
            blocks.append(cur)
        elif cur is not None:
            cur[1].append(line)
    if not blocks or blocks[0][0] != "package":
        raise pk.PackageError("a package dump starts with '#! package'")
    pkg = pk.Package()
    songs = []
    for head, lines in blocks:
        kind, _, arg = head.partition(" ")
        body = "\n".join(lines)
        if kind == "package":
            for ln in lines:
                key, colon, val = ln.partition(":")
                if colon and key.strip() in FIELDS:
                    setattr(pkg, key.strip(), val[1:] if val.startswith(" ") else val)
        elif kind == "shared":
            sh = Song.from_mml(body)
            pkg.instruments, pkg.fx, pkg.keys = sh.instruments, sh.fx, sh.keys
        elif kind == "sample":
            try:
                pkg.samples[arg] = dpcm.loads(base64.b64decode("".join(lines).strip()), arg)
            except (ValueError, dpcm.DpcmError) as e:
                raise pk.PackageError("sample '%s': %s" % (arg, e))
        elif kind == "song":
            songs.append((arg, body))
        else:
            raise pk.PackageError("unknown block '#! %s'" % kind)
    lib = pkg.library(mml.builtin_library())
    for sid, body in songs:
        pkg.songs[sid] = Song.from_mml(body, lib)
    return pkg
