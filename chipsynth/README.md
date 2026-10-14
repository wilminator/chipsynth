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

## Tests

    python -m unittest discover -t . -s chipsynth/tests      (from the project root)

`tests/test_boundary.py` fails if the library ever imports the game or anything beyond numpy and scipy.
