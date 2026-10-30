# chipsynth

A small chip-style synthesizer, a tune language (MML) and a player, in Python.  Four voices (pulse, saw, triangle,
sine, noise) plus two DAC sample channels, rendered to stereo at 44.1 kHz.  Needs numpy and scipy; pygame only for live
sound.  Copyright (C) 2026 Michael Allen Wilmes.

## Quick start

    from chipsynth import parse, TunePlayer, render_wav

    tune = parse("""
    title Hello
    tempo 120
    echo 240ms,40,30
    V1: @lead l8 o4 c e g >c< g e c4 r4
    V2: @bass l4 o2 c g c g
    D1: loop r1 r1
    """)
    render_wav(tune, "hello.wav")             # the first pass plus a tail, as a 16-bit stereo WAV

    player = TunePlayer(tune)                 # live: ask for the next block of samples as often as you like
    block = player.render(4096)               # float32 array of shape (4096, 2)

    from chipsynth import MusicOut            # plays on a pygame mixer channel (needs pygame and an audio device)
    out = MusicOut(); out.start(tune)

## What is in it

| module | does |
|---|---|
| `mml.py` | the tune language: parser, `Tune`, `Note`, the instrument / pattern / effect / scale / sample library |
| `synth.py`, `synthfx.py` | the voices, envelopes, effects, filter; the shared echo and reverb bus |
| `dpcm.py` | WAV to NES-style delta samples, the `.dpcm` file format (`python -m chipsynth.dpcm demo|convert`) |
| `formats/` | other tools' files, with a report of what could not be carried over: `formats.famistudio_text.load(text)` -> songs, samples, report; `formats.goattracker.load(bytes)` likewise |
| `midi.py` | Standard MIDI files: `to_midi(tune)`, `from_midi(bytes, voices=None)` -> Song (a voice per part, up to 16; `voices=N` folds the rest into gaps) |
| `songedit.py` | `Editor`: note-level edits of a Song (add, erase, pitch, resize, move) with undo / redo |
| `song.py`, `songmml.py` | the `Song` model (clips, repeats, calls kept as objects), `Song.compile()` to a `Tune`, MML import / export: `Song.from_mml(text)`, `song.to_mml()` |
| `package.py`, `packageset.py`, `packagetext.py` | the `.csp` package file (several songs + shared instruments / samples), `PackageSet` (load many, pick `package:song`), text dump / build |
| `components.py`, `exporter.py`, `__main__.py` | the command line (below): list / filter / export / import components, export a song as wav, mid, mml or csp |
| `player.py` | `TunePlayer`: schedules a `Tune` on the synth, loops, tails ring over the restart |
| `output.py` | `MusicOut`: live playback through pygame (the only place pygame is used) |
| `musicgen.py` | a seeded tune generator (`generate(kind, seed, mood, tempo)` returns MML text) |

The tune language is described in `DESIGN-MUSIC.md` (project root) and, shorter, on the Music editor's help pages.
Rendering is exact: the same samples come out however the output is cut into blocks.

## WAV import options (`dpcm.py`)

`python -m chipsynth.dpcm convert IN.wav GAMEDIR` with no options writes exactly what it always did (rate 12, stereo mixed to
mono, normalized to 0.98). Options, applied in this order (channel, trim, cut to length, fade, then resample and normalize):

| option | does |
|---|---|
| `--rate N` or `--rate auto` | DPCM rate index 0-15 (the NES table); `auto` picks the highest rate whose result fits the size cap |
| `--channel mix\|left\|right\|stereo` | mono mixdown, one channel, or two tracks for D1 + D2 (`--stereo` is the same as `--channel stereo`) |
| `--no-normalize` | keep the WAV's own level instead of scaling the peak to 0.98 |
| `--trim [DB]` | cut leading and trailing silence below DB dBFS (default -48) |
| `--fade MS` | linear fade-out over the last MS milliseconds |
| `--max-seconds S`, `--max-bytes N` | cut the recording to S seconds, or the result to N bytes per track |
| `--truncate` | a result over the DPCM size cap (64 parts of 4081 bytes) is cut to fit instead of refused |
| `--fit` | turn the level down until it follows cleanly (cleaner, quieter) |
| `--preview` | print the size, duration, chosen rate and parts, and write nothing |

    python -m chipsynth.dpcm convert in.wav GAMEDIR --rate auto --channel left --trim --fade 50 --preview

In Python: `dpcm.convert_wav(x, wav_rate, dpcm.ImportOptions(...))` returns `(Sample, notes)`, and `dpcm.preview(...)` returns
the size, seconds, rate, parts and any error without writing. The maker's Sample import settings screen has the same
options and shows that preview live.

## The song composer (optional, needs pygame)

`chipsynth/composer/` is a resizable piano-roll / tracker window for writing songs. It is an optional extra: the rest of
the library never imports it, and it is the only part that imports pygame at the top of a module (install `pygame`
yourself). It imports nothing from any host application.

    python -m chipsynth.composer [GAMEDIR] [SONG] [--nosound] [--reset]

`python -m chipsynth.compose` is the same command. GAMEDIR is a folder with a `music/` folder (loose `.mml` files, `.csp`
packages, `music/lib`); SONG is a song id like `forest:dawn` or a `.mml` / `.csp` path. With no arguments it opens a
generated tune.

The centre of the window has three views (Tab or the buttons at the top switch): **Tracks** lists every track as a lane
of the same height, one below the other, scrolling up and down (the track height is a setting: Ctrl+Shift+wheel or the
**T** buttons, remembered between sessions); **Piano roll** is one large piano roll for all the voices; **Tracker** is
the grid with time running down the page.

## Command line (`python -m chipsynth`)

A `.csp` package holds songs and what they share. Its *components* are of five types: `song`, `instrument`, `fx` (effect
sets), `scale` and `sample` (DPCM). `add_song` copies what a song uses into the song, so most instruments, effect sets and
scales belong to a song (their scope in `list`); the rest are `shared` by all the package's songs. `python -m chipsynth -h`
and `COMMAND -h` give every option.

| command | does |
|---|---|
| `import PKG.csp FILE.mml...` | add songs to a package (created if missing). `--id NAME` names the song, `--shared` adds a library file's instruments / fx / scales / samples to the package, `--no-replace` refuses an id already there, `--lib DIR` / `--samples DIR` say where a game's `music/lib` and `.dpcm` files are. `.mid` files work too, and so do FamiStudio text files (`.txt`, from File > Export > FamiStudio Text; `--song NAME`, `--region ntsc|pal`, `--report FILE`), and so do GoatTracker 2 songs (`.sng`; `--song N` is the subtune, `--region pal|ntsc` defaults to pal, `--multispeed 1|2|4`, `--report FILE`: the file records none of these) |
| `list SOURCE...` | the components of `.csp` / `.mml` / folders: `--type song,sample,...` (aliases `instr`, `key`), `--name 'd*'` (a glob; `TYPE:NAME` for one type) |
| `export PKG.csp` | components as MML: `-d DIR` (songs as `ID.mml`, the rest in `lib/PACKAGE.mml`, samples in `samples/NAME.dpcm`, which is a game's `music/` layout), or `-o FILE` (`-` for stdout) for one thing; `--type` / `--name` choose; `--midi` writes songs as `.mid`. With no `--type`: songs and the shared parts |
| `export-song SOURCE[:SONG] [SONG] -o OUT` | one song as `wav`, `mid`, `mml` or `csp` (from OUT's ending, or `-f`). `render` is the same command |
| `generate`, `dump`, `build` | as before |

`export-song` options for a **WAV** (any of them with another format is an error):

| option | does |
|---|---|
| `--volume V` | master volume: `0.5`, `50%` or `-6dB` (default 1). Anything over full scale is clipped and reported |
| `--normalize`, `--peak DB` | scale the loudest sample to `DB` dBFS (default -1) of the part you keep, before `--volume` is applied |
| `--start T`, `--end T` | keep from `T` to `T`: `1.5`, `250ms`, `1:05.5` or `88200f` (audio frames, 44100 per second). Past the natural end the song keeps looping if `--loops` > 0, else is silent |
| `--loops N` | trips round the loop after the first pass (nothing for a `once` song, which says so) |
| `--tail S` | seconds to let the last notes ring (default 2) |

    python -m chipsynth import music/forest.csp dawn.mml dusk.mml --lib game/music/lib
    python -m chipsynth list music/forest.csp --type song,sample
    python -m chipsynth export music/forest.csp -d out --name 'd*'
    python -m chipsynth export-song music/forest.csp:dawn -o dawn.wav --loops 2 --normalize --volume -3dB --start 2 --end 1:30

A song exported as MML (`export`, or `export-song -f mml`) declares the package samples it plays under `[sample]` and writes
them as `samples/NAME.dpcm` beside it; import it with `--samples samples` to get the same package back. In Python the same
work is `components.py` (`components`, `select`, `export_mml`, `import_mml`) and `exporter.py` (`export_song`, `AudioOptions`).

## Tests

    python -m unittest discover -t . -s chipsynth/tests      (from the project root)

`tests/test_boundary.py` fails if the library ever imports the game or anything beyond numpy and scipy (pygame is allowed
only lazily in `output.py` and in `composer/`). The suite needs nothing outside this directory (test data is in
`tests/data/`), so it also runs from a standalone checkout of chipsynth.

## Licence

Copyright (C) 2026 Michael Allen Wilmes.

chipsynth is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the file `LICENSE` for the full text.

Under the GPL, a program that includes or links chipsynth (the Free Software Foundation reads linking that way) must be released under the GPL too. Music you make with it is yours; the licence covers the code, not the songs it renders.
