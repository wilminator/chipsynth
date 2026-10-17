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
| `midi.py` | Standard MIDI files: `to_midi(tune)`, `from_midi(bytes)` -> Song |
| `songedit.py` | `Editor`: note-level edits of a Song (add, erase, pitch, resize, move) with undo / redo |
| `song.py`, `songmml.py` | the `Song` model (clips, repeats, calls kept as objects), `Song.compile()` to a `Tune`, MML import / export: `Song.from_mml(text)`, `song.to_mml()` |
| `package.py`, `packageset.py`, `packagetext.py` | the `.csp` package file (several songs + shared instruments / samples), `PackageSet` (load many, pick `package:song`), text dump / build; `python -m chipsynth list|dump|build|import|export` |
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
yourself). It imports nothing from the Blobs game.

    python -m chipsynth.composer [GAMEDIR] [SONG] [--nosound] [--reset]

`python -m chipsynth.compose` is the same command. GAMEDIR is a folder with a `music/` folder (loose `.mml` files, `.csp`
packages, `music/lib`); SONG is a song id like `forest:dawn` or a `.mml` / `.csp` path. With no arguments it opens a
generated tune.

## Tests

    python -m unittest discover -t . -s chipsynth/tests      (from the project root)

`tests/test_boundary.py` fails if the library ever imports the game or anything beyond numpy and scipy (pygame is allowed
only lazily in `output.py` and in `composer/`). The suite needs nothing outside this directory (test data is in
`tests/data/`), so it also runs from a standalone checkout of chipsynth.

## Licence

Copyright (C) 2026 Michael Allen Wilmes.

chipsynth is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the file `LICENSE` for the full text.

Under the GPL, a program that includes or links chipsynth (the Free Software Foundation reads linking that way) must be released under the GPL too. Music you make with it is yours; the licence covers the code, not the songs it renders.
