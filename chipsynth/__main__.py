# Copyright (C) 2026 Michael Allen Wilmes
"""python -m chipsynth COMMAND ...   (packages: see package.py)

  list PATH...                    the songs in packages / folders / .mml files: id, title, seconds
  dump PKG.csp [-o FILE]          the package as text (stdout by default)
  build TEXT [-o PKG.csp]         assemble a package from a dump
  import PKG.csp FILE.mml ...     add songs (song id = file name) to a package, creating it if needed
                                  (FILE.mid works too: the first four parts with notes)
  export PKG.csp [-d DIR] [--midi]  write every song as MML (or as .mid files)
"""
import sys
from pathlib import Path

from . import mml, midi, package as pk, packagetext
from .packageset import PackageSet
from .song import Song


def opt(args, flag):
    if flag in args:
        i = args.index(flag)
        val = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
        return val
    return None


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], list(argv[1:])
    try:
        if cmd == "list":
            ps = PackageSet()
            for a in args:
                if Path(a).is_dir():
                    ps.add_folder(a)
                else:
                    ps.add(a)
            for ref in ps.songs():
                print("%-28s %6.1fs  %s" % (ref.id, ref.seconds, ref.title))
            for p in ps.problems:
                print("problem:", p, file=sys.stderr)
        elif cmd == "dump" and len(args) >= 1:
            out = opt(args, "-o")
            text = packagetext.dump_text(pk.load(args[0]))
            if out:
                Path(out).write_text(text, encoding="utf-8", newline="\n")
            else:
                sys.stdout.write(text)
        elif cmd == "build" and len(args) >= 1:
            out = opt(args, "-o") or str(Path(args[0]).with_suffix(".csp"))
            pk.save(packagetext.build_text(Path(args[0]).read_text(encoding="utf-8")), out)
            print("wrote", out)
        elif cmd == "import" and len(args) >= 2:
            path = Path(args[0])
            pkg = pk.load(path) if path.exists() else pk.Package(name=path.stem)
            for f in args[1:]:
                if Path(f).suffix.lower() in (".mid", ".midi"):
                    song = midi.from_midi(Path(f).read_bytes())
                else:
                    song = Song.from_mml(Path(f).read_text(encoding="utf-8"))
                pkg.add_song(Path(f).stem, song)
            pk.save(pkg, path)
            print("%s: %d song(s)" % (path, len(pkg.songs)))
        elif cmd == "export" and len(args) >= 1:
            as_midi = "--midi" in args
            args = [a for a in args if a != "--midi"]
            d = Path(opt(args, "-d") or ".")
            d.mkdir(parents=True, exist_ok=True)
            pkg = pk.load(args[0])
            for sid, song in pkg.songs.items():
                if as_midi:
                    (d / (sid + ".mid")).write_bytes(midi.to_midi(song.compile(pkg.library())))
                    print("wrote", d / (sid + ".mid"))
                else:
                    (d / (sid + ".mml")).write_text(song.to_mml(), encoding="utf-8", newline="\n")
                    print("wrote", d / (sid + ".mml"))
        else:
            print(__doc__)
            return 2
    except (pk.PackageError, mml.MMLError, midi.MidiError, OSError) as e:
        print("error:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
