# Copyright (C) 1994, 2026 Michael Allen Wilmes
"""chipsynth: a small chip-style synthesizer, a tune language (MML) and a player.

    from chipsynth import parse, TunePlayer, render_wav
    tune = parse("tempo 120\nV1: c d e f g2")
    render_wav(tune, "song.wav")                  # or: TunePlayer(tune).render(4096) for live samples

Four voices (pulse, saw, triangle, sine, noise) and two DAC sample channels, rendered to stereo at 44.1 kHz, with
vibrato / slide / arpeggio / pulse sweep, a scale-degree mode, a shared echo and reverb, a filter, and NES-style
delta samples.  The core needs only numpy and scipy; pygame is used only by `output.MusicOut` for live sound.
"""
from . import dpcm
from .mml import MMLError, Note, Tune, builtin_library, load_library, parse
from .musicgen import generate
from .song import Song, Clip
from .player import TunePlayer, render, render_wav
from .synth import RATE, Instrument

__all__ = ["dpcm", "MMLError", "Note", "Tune", "builtin_library", "load_library", "parse", "generate",
           "TunePlayer", "render", "render_wav", "RATE", "Instrument", "Song", "Clip", "MusicOut", "open_music_mixer"]


def __getattr__(name):                 # MusicOut / open_music_mixer live in output.py, which is only loaded on demand
    if name in ("MusicOut", "open_music_mixer"):
        from . import output
        return getattr(output, name)
    raise AttributeError(name)
