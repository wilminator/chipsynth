# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""python -m chipsynth COMMAND ...   (packages: see package.py; `COMMAND -h` lists a command's options)

  list SOURCE... [--type T] [--name GLOB]
                                  the components of packages / folders / .mml files: songs, instruments, fx (effect
                                  sets), scales and samples.  --type song,sample,...  --name 'd*' (TYPE:NAME too)
  dump PKG.csp [-o FILE]          the package as text (stdout by default)
  build TEXT [-o PKG.csp]         assemble a package from a dump
  import PKG.csp FILE.mml ...     add songs (song id = file name, with `_` for anything an id may not hold) to a package, creating it if needed
                                  (FILE.mid works too; FILE.txt from FamiStudio's File > Export > FamiStudio Text works too)
                                    --id NAME     the song id, when there is one file
                                    --shared      the files are libraries ([instr] [fx] [key] [sample]): their
                                                  parts are added to the package for all its songs
                                    --lib DIR     a game's music/lib (instruments and the like the MML uses)
                                    --samples DIR where NAME.dpcm files for [sample] are
                                    --no-replace  refuse a song id that is already in the package
                                    --voices N    (MIDI) make songs of N voices; the others' notes fill the gaps
                                    --song NAME   (FamiStudio) only this song (a name or a number); --region ntsc|pal
  export PKG.csp [options]        components as MML: each song as ID.mml, the rest in lib/PACKAGE.mml, DPCM samples
                                  in samples/NAME.dpcm (a game's music folder).  Without --type: songs and the
                                  package's shared parts.
                                    -d DIR   where to write (default .)   -o FILE   write one thing to FILE ('-' = stdout)
                                    --type T --name GLOB   which components   --midi   songs as .mid instead
  export-song SOURCE[:SONG] [SONG] -o OUT [options]      (also: render)
                                  one song as wav, mid, mml or csp (from OUT's ending, or --format)
                                    wav only: --volume V (0.5, 50%, -6dB)  --normalize [--peak DB, default -1]
                                              --start T  --end T   (1.5 | 250ms | 1:05.5 | 88200f = frames)
                                              --loops N  (trips round the loop)  --tail SECONDS  (default 2)
  generate KIND [options]         a generated tune as MML (stdout, or -o FILE.mml):
                                    --seed N  --mood calm|tense|bright|dark  --tempo BPM
                                    --dac         drums as samples on D1 / D2          (kick, snare, hats, crash, toms)
                                    --dac-bass    the bass as pitched samples on D2
                                    --voice       a rare one-shot "hey" on D1 (boss intro, turnarounds)
                                    --samples DIR also write the kit's .dpcm files the tune uses into DIR
                                                  (a game's music/samples; the built-in kit needs no files)
                                    --wav FILE    also render the tune to a WAV
"""
import argparse
import sys
from pathlib import Path

from . import components as comp, exporter, mml, midi, package as pk, packagetext
from .packageset import PackageSet
from .song import Song


def opt(args, flag):
    if flag in args:
        i = args.index(flag)
        val = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
        return val
    return None


def generate_cmd(args):
    from . import kit, musicgen
    out, samples, wav = opt(args, "-o"), opt(args, "--samples"), opt(args, "--wav")
    seed, mood, tempo = opt(args, "--seed"), opt(args, "--mood"), opt(args, "--tempo")
    flags = {f: f in args for f in ("--dac", "--dac-bass", "--voice")}
    args = [a for a in args if a not in flags]
    if len(args) != 1 or args[0] not in musicgen.KINDS or (mood or "") not in musicgen.MOODS:
        print("generate KIND [--seed N] [--mood M] [--tempo BPM] [--dac] [--dac-bass] [--voice] [-o FILE] [--samples DIR] "
              "[--wav FILE]\n  kinds: %s\n  moods: %s" % (", ".join(musicgen.KINDS), ", ".join(m for m in musicgen.MOODS if m)),
              file=sys.stderr)
        return 2
    try:
        seed, tempo = int(seed or 1), (int(tempo) if tempo else None)
    except ValueError:
        print("error: --seed and --tempo are numbers", file=sys.stderr)
        return 2
    text = musicgen.generate(args[0], seed, mood or "", tempo, dac=flags["--dac"], dac_bass=flags["--dac-bass"],
                             voice=flags["--voice"])
    if out:
        Path(out).write_text(text, encoding="utf-8", newline="\n")
        print("wrote", out)
    else:
        sys.stdout.write(text)
    if samples:
        names = sorted({n for n in kit_names(text)})
        for p in kit.write_files(samples, names):
            print("wrote", p, file=sys.stderr if not out else sys.stdout)
    if wav:
        from .player import render_wav
        render_wav(mml.parse(text), wav)
        print("wrote", wav, file=sys.stderr if not out else sys.stdout)
    return 0


def kit_names(text):
    """The kit sample names a tune's [sample] section declares."""
    import re
    return re.findall(r"^(kit_[A-Za-z_]+) = ", text, re.M)


def glue(args, *flags):
    """`--volume -3dB` -> `--volume=-3dB`: argparse would take the -3dB for an option of its own."""
    out = []
    for i, a in enumerate(args):
        if i and args[i - 1] in flags and a[:1] == "-" and a[1:2].isdigit():
            out[-1] += "=" + a
        else:
            out.append(a)
    return out


def parser(cmd, text):
    return argparse.ArgumentParser(prog="python -m chipsynth " + cmd, description=text)


def library(args):
    """The library for --lib DIR (a game's music/lib) and --samples DIR; the built-in one without them."""
    lib = mml.load_library(args.lib) if args.lib else mml.builtin_library()
    if args.samples:
        lib["_sampledir"] = Path(args.samples)
    return lib


def add_library_options(p):
    p.add_argument("--lib", metavar="DIR", help="a game's music/lib folder: instruments, patterns and the like the MML uses")
    p.add_argument("--samples", metavar="DIR", help="the folder with NAME.dpcm files (default: DIR/../samples of --lib)")


def add_filter_options(p):
    p.add_argument("--type", action="append", metavar="T",
                   help="only these component types, comma separated: %s (repeatable; 'all')" % ", ".join(comp.TYPES))
    p.add_argument("--name", action="append", metavar="GLOB",
                   help="only components whose name matches (repeatable); TYPE:NAME matches one type")


def list_cmd(args):
    p = parser("list", "List the components of packages (.csp), .mml files and folders of them.")
    p.add_argument("sources", nargs="+", metavar="SOURCE")
    add_filter_options(p)
    add_library_options(p)
    a = p.parse_args(args)
    ps = PackageSet(library(a))
    for src in a.sources:
        ps.add_folder(src) if Path(src).is_dir() else ps.add(src)
    for name, pkg in ps.packages.items():
        shown = comp.select(comp.components(pkg, ps.lib), a.type, a.name)
        print("%s  (package %s: %d song(s))" % (ps.paths[name], name, len(pkg.songs)))
        for c in shown:
            label = "%s:%s" % (name, c.name) if c.type == "song" else c.name
            print("  %-10s %-28s %-9s %s" % (c.type, label, c.scope if c.type != "song" else "", c.detail.strip()))
        if not shown:
            print("  (nothing matches)")
    for prob in ps.problems:
        print("problem:", prob, file=sys.stderr)
    return 0


def import_cmd(args):
    p = parser("import", "Add songs from MML (or MIDI) files to a package, creating the package if it is not there.")
    p.add_argument("package", metavar="PKG.csp")
    p.add_argument("files", nargs="+", metavar="FILE")
    p.add_argument("--id", metavar="NAME", help="the song id (default: the file name, bad characters become _); needs exactly one file")
    p.add_argument("--shared", action="store_true",
                   help="the files are libraries: add their instruments, fx, scales and samples to the package")
    p.add_argument("--no-replace", action="store_true", help="refuse a song id that the package already has")
    p.add_argument("--voices", type=int, metavar="N",
                   help="(MIDI) make songs of N voices: the most important parts stay and the others' notes fill their gaps "
                        "(default: a voice for every part, up to 16)")
    p.add_argument("--song", metavar="NAME", help="(FamiStudio) import only this song, by name or number (default: every song)")
    p.add_argument("--region", choices=("ntsc", "pal"), help="(FamiStudio) the timing to use (default: what the file says)")
    p.add_argument("--report", metavar="FILE", help="(FamiStudio) also write what was changed or left out, as JSON")
    add_library_options(p)
    a = p.parse_args(args)
    if a.id and len(a.files) != 1:
        p.error("--id needs exactly one file")
    path = Path(a.package)
    pkg = pk.load(path) if path.exists() else pk.Package(name=path.stem)
    lib = library(a)
    for f in a.files:
        is_midi = Path(f).suffix.lower() in (".mid", ".midi")
        if a.shared:
            if is_midi:
                p.error("--shared takes MML library files, not MIDI")
            comp.import_mml(pkg, Path(f).read_text(encoding="utf-8"), lib, shared=True)
            print("added the shared parts of", f)
            continue
        if Path(f).suffix.lower() == ".txt":
            import_famistudio(a, pkg, f, lib)
            continue
        sid = a.id or pk.Package.safe_id(Path(f).stem)
        if sid in pkg.songs and a.no_replace:
            raise pk.PackageError("the package already has a song '%s' (leave out --no-replace to replace it)" % sid)
        verb = "replaced" if sid in pkg.songs else "added"
        if is_midi:
            pkg.add_song(sid, midi.from_midi(Path(f).read_bytes(), voices=a.voices), lib)
        else:
            comp.import_mml(pkg, Path(f).read_text(encoding="utf-8"), lib, song_id=sid)
        print("%s song '%s' from %s" % (verb, sid, f))
    pk.save(pkg, path)
    print("%s: %d song(s)" % (path, len(pkg.songs)))
    return 0


def import_famistudio(a, pkg, f, lib):
    from .formats import famistudio_text as fam
    text = Path(f).read_text(encoding="utf-8-sig")
    if not fam.looks_like(text):
        raise pk.PackageError("%s is not a FamiStudio text file (it should start with a Project line)" % f)
    res = fam.load(text, song=a.song, region=a.region)
    if a.id and len(res.songs) != 1:
        raise pk.PackageError("--id needs one song: the file has %d (pick one with --song)" % len(res.songs))
    stem = pk.Package.safe_id(Path(f).stem)
    for imp in res.songs:
        sid = a.id or (stem if len(res.songs) == 1 else "%s_%s" % (stem, pk.Package.safe_id(imp.name, "song")))
        if sid in pkg.songs and a.no_replace:
            raise pk.PackageError("the package already has a song '%s' (leave out --no-replace to replace it)" % sid)
        verb = "replaced" if sid in pkg.songs else "added"
        mem = dict(lib)
        mem["_mem"] = {**lib.get("_mem", {}), **imp.samples}
        pkg.add_song(sid, imp.song, mem)
        print("%s song '%s' from %s (%s)" % (verb, sid, f, imp.name))
    for line in res.report.lines():
        print("  " + line)
    if a.report:
        import json
        Path(a.report).write_text(json.dumps(res.report.as_dict(), indent=2), encoding="utf-8")


def export_cmd(args):
    p = parser("export", "Write the components of a package as MML (songs one per file, the rest in a library file).")
    p.add_argument("package", metavar="PKG.csp")
    p.add_argument("-d", metavar="DIR", default=".", help="the folder to write into (default: here)")
    p.add_argument("-o", metavar="FILE", help="write the one thing selected to FILE ('-': standard output)")
    p.add_argument("--midi", action="store_true", help="write the songs as .mid files instead of MML")
    add_filter_options(p)
    add_library_options(p)
    a = p.parse_args(args)
    lib = library(a)
    pkg = exporter.open_source(a.package, lib)
    everything = comp.components(pkg, lib)
    if a.type:
        pool = everything
    else:                           # what a song holds itself is already inside its .mml
        pool = [c for c in everything if c.scope == comp.SHARED]
    picked = comp.select(pool, a.type, a.name)
    if a.midi:
        picked = [c for c in picked if c.type == "song"]
    if not picked:
        raise comp.ComponentError("nothing matches (see `list` for what %s holds)" % a.package)
    notes = []
    if a.o:
        songs = [c for c in picked if c.type == "song"]
        if len(songs) > 1 or (songs and len(picked) > 1):
            raise comp.ComponentError("-o writes one thing; %d are selected (use -d DIR, or narrow --type / --name)"
                                      % len(picked))
        if songs and a.midi:
            r = exporter.export_song(pkg, songs[0].name, a.o, fmt="mid", lib=lib)
            written = r.written
        elif a.o == "-":
            text = comp.song_to_mml(pkg, songs[0].name, notes)[0] if songs else comp.lib_to_mml(pkg, picked)[0]
            sys.stdout.write(text)
            written = []
        elif songs:
            written = exporter.export_song(pkg, songs[0].name, a.o, fmt="mml", lib=lib).written
        else:
            text, files = comp.lib_to_mml(pkg, picked)
            written = [comp._write(Path(a.o), text)] + comp._write_samples(Path(a.o).parent / "samples", files)
    elif a.midi:
        d = Path(a.d)
        written = []
        for c in picked:
            written.extend(exporter.export_song(pkg, c.name, d / (c.name + ".mid"), fmt="mid", lib=lib).written)
    else:
        written = comp.export_mml(pkg, picked, a.d, notes)
    for w in written:
        print("wrote", w)
    for n in notes:
        print("note:", n, file=sys.stderr)
    return 0


def export_song_cmd(args):
    p = parser("export-song", "Export one song as wav, mid, mml or csp.  Options marked (wav) apply to a wav only.")
    p.add_argument("source", metavar="SOURCE[:SONG]", help="a .csp or .mml; SOURCE:SONG picks a song of a package")
    p.add_argument("song", nargs="?", metavar="SONG", help="the song id (not needed when there is only one)")
    p.add_argument("-o", "--output", metavar="OUT", required=True, help="the file to write")
    p.add_argument("-f", "--format", choices=exporter.FORMATS, help="default: from the ending of OUT")
    p.add_argument("--volume", metavar="V", default="1", help="(wav) master volume: 0.5, 50%% or -6dB (default 1)")
    p.add_argument("--normalize", action="store_true", help="(wav) scale the loudest sample to --peak; volume is applied after")
    p.add_argument("--peak", metavar="DB", default="-1", help="(wav) the level --normalize aims for (default -1 dBFS)")
    p.add_argument("--start", metavar="T", default="0", help="(wav) first moment kept: 1.5, 250ms, 1:05.5 or 88200f (frames)")
    p.add_argument("--end", metavar="T", help="(wav) where to stop (default: the end of the song and its tail)")
    p.add_argument("--loops", type=int, default=0, metavar="N", help="(wav) trips round the loop after the first pass")
    p.add_argument("--tail", type=float, default=2.0, metavar="S", help="(wav) seconds to let the last notes ring (default 2)")
    add_library_options(p)
    a = p.parse_args(glue(args, "--volume", "--peak"))
    src, song = exporter.split_source(a.source)
    lib = library(a)
    pkg = exporter.open_source(src, lib)
    if a.song and song and a.song != song:
        p.error("two songs named: %s and %s" % (song, a.song))
    opts = exporter.AudioOptions(volume=exporter.gain(a.volume), normalize=a.normalize, peak_db=exporter.decibels(a.peak),
                                 start=exporter.frames(a.start), end=exporter.frames(a.end) if a.end else None,
                                 loops=a.loops, tail=a.tail)
    r = exporter.export_song(pkg, a.song or song, a.output, opts, a.format, lib)
    for w in r.written:
        print("wrote", w, *(["(%.2fs, peak %s)" % (r.seconds, "silent" if r.peak_db is None else "%.1f dBFS" % r.peak_db)]
                            if r.format == "wav" and w == r.path else []))
    for n in r.notes:
        print("note:", n, file=sys.stderr)
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], list(argv[1:])
    try:
        if cmd == "list":
            return list_cmd(args)
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
        elif cmd == "import":
            return import_cmd(args)
        elif cmd == "export":
            return export_cmd(args)
        elif cmd in ("export-song", "render"):
            return export_song_cmd(args)
        elif cmd == "generate":
            return generate_cmd(args)
        else:
            print(__doc__)
            return 2
    except (pk.PackageError, mml.MMLError, midi.MidiError, comp.ComponentError, exporter.ExportError, OSError) as e:
        print("error:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
