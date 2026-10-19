# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export one song of a package (or of a .mml file) in any format chipsynth writes: wav, mp3, ogg, mid, mml or csp.

    pkg = open_source("music/forest.csp")
    r = export_song(pkg, "dawn", "dawn.wav", AudioOptions(volume=0.8, normalize=True, start=frames("2s"), loops=1))

Only the audio formats are rendered, so only they take AudioOptions (mp3 and ogg are written by the optional soundfile
package, which bundles libsndfile).  What the options do, in this order:
  1. render the first pass, `loops` more trips round the loop (when the song loops) and `tail` seconds to ring out,
     or up to `end` when that is given (past the natural end the song keeps looping if loops > 0, else is silent);
  2. keep the part from `start` to `end`, then cut the silence at the end when `trim_db` is set (a sound effect);
  3. normalize: scale so the loudest sample is at `peak_db` dBFS (the unclipped signal: nothing was cut off yet);
  4. volume: multiply by the master volume.  Anything over full scale is clipped, and counted in the result.
Times are seconds ("1.5", "1.5s"), milliseconds ("250ms"), "m:ss" ("1:05.5") or audio frames at 44100 per second ("88200f").
"""
import re
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import components, midi, mml, package as pk
from .player import TunePlayer
from .song import Song
from .synth import RATE, to_pcm16

FORMATS = ("wav", "mp3", "ogg", "mid", "mml", "csp")
AUDIO = ("wav", "mp3", "ogg")                      # the rendered ones; mp3 and ogg need the soundfile package
SUFFIXES = {".wav": "wav", ".mp3": "mp3", ".ogg": "ogg", ".mid": "mid", ".midi": "mid", ".mml": "mml", ".csp": "csp"}
ENCODERS = {"mp3": ("MP3", "MPEG_LAYER_III"), "ogg": ("OGG", "VORBIS")}        # soundfile's format and subtype
PAD = 0.02                                          # seconds kept after the last sound when trimming


class ExportError(Exception):
    pass


@dataclass
class AudioOptions:
    volume: float = 1.0                  # master volume, a multiplier
    normalize: bool = False
    peak_db: float = -1.0                # the peak normalize aims for, dBFS
    start: int = 0                       # first frame kept
    end: int = None                      # frame to stop at (not kept); None: the natural end
    loops: int = 0                       # trips round the loop after the first pass
    tail: float = 2.0                    # seconds left to ring out after the song's last note
    trim_db: float = None                # cut the silence at the end: everything after the last sample above this
                                         # level (dBFS, relative to the loudest sample) plus PAD; None keeps it

    def used(self):
        """The options set to something other than their default (those that mean nothing outside audio)."""
        d = AudioOptions()
        return [k for k in ("volume", "normalize", "start", "end", "loops", "trim_db") if getattr(self, k) != getattr(d, k)]


@dataclass
class Result:
    path: Path
    format: str
    frames: int = 0                      # WAV only
    peak_db: float = None                # WAV only: the loudest sample as written, dBFS, at most 0 (None: silence)
    clipped: int = 0                     # WAV only: samples cut off at full scale
    written: list = field(default_factory=list)      # every file written (the song's samples go beside an .mml)
    notes: list = field(default_factory=list)

    @property
    def seconds(self):
        return self.frames / RATE


# ------------------------------------------------------------------ reading what the user typed
def frames(text):
    """A position -> audio frames: '1.5', '1.5s', '250ms', '1:05.5' (seconds) or '88200f' (frames)."""
    t = str(text).strip().lower()
    try:
        if t.endswith("frames"):
            return _whole(t[:-6], text)
        if t.endswith("f"):
            return _whole(t[:-1], text)
        if t.endswith("ms"):
            sec = float(t[:-2]) / 1000
        elif ":" in t:
            m, _, s = t.rpartition(":")
            sec = float(m) * 60 + float(s)
        else:
            sec = float(t[:-1] if t.endswith("s") else t)
    except ValueError:
        raise ExportError("'%s' is not a time (try 1.5, 250ms, 1:05.5 or 88200f)" % text)
    if sec < 0:
        raise ExportError("'%s': a time cannot be negative" % text)
    return int(round(sec * RATE))


def _whole(digits, text):
    try:
        n = int(digits)
    except ValueError:
        raise ExportError("'%s' is not a frame count (try 88200f)" % text)
    if n < 0:
        raise ExportError("'%s': a frame count cannot be negative" % text)
    return n


def gain(text):
    """A volume -> multiplier: '0.5', '50%' or '-6dB'."""
    t = str(text).strip().lower()
    try:
        if t.endswith("db"):
            return 10 ** (float(t[:-2]) / 20)
        if t.endswith("%"):
            return float(t[:-1]) / 100
        v = float(t)
    except ValueError:
        raise ExportError("'%s' is not a volume (try 0.5, 50%% or -6dB)" % text)
    if v < 0:
        raise ExportError("'%s': a volume cannot be negative" % text)
    return v


def decibels(text):
    """A level -> dBFS: '-1' or '-1dB' (0 or less)."""
    t = str(text).strip().lower()
    try:
        v = float(t[:-2] if t.endswith("db") else t)
    except ValueError:
        raise ExportError("'%s' is not a level in dB (try -1 or -3dB)" % text)
    if v > 0:
        raise ExportError("'%s': a peak above 0 dBFS would clip" % text)
    return v


# ------------------------------------------------------------------ sources
def open_source(path, lib=None):
    """A Package from a .csp, or a one-song package from a .mml named after the file (as PackageSet does)."""
    p = Path(path)
    if p.suffix.lower() == ".mml":
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ExportError("can't read %s (%s)" % (p.name, e))
        pkg = pk.Package(name=p.stem)
        pkg.songs[p.stem] = Song.from_mml(text, lib or mml.builtin_library())
        return pkg
    return pk.load(p)


def pick_song(pkg, song_id=None):
    if song_id:
        if song_id not in pkg.songs:
            raise ExportError("no song '%s' in '%s' (songs: %s)" % (song_id, pkg.name, ", ".join(pkg.songs) or "none"))
        return song_id
    if len(pkg.songs) != 1:
        raise ExportError("'%s' has %d songs; say which: %s" % (pkg.name, len(pkg.songs), ", ".join(pkg.songs) or "none"))
    return next(iter(pkg.songs))


def split_source(text):
    """'forest.csp:dawn' -> ('forest.csp', 'dawn'); a path that exists as given (a drive letter, say) is left whole."""
    if Path(text).exists() or ":" not in text:
        return text, None
    head, _, tail = text.rpartition(":")
    return (head, tail) if Path(head).exists() else (text, None)


# ------------------------------------------------------------------ audio
def render_audio(tune, opts=None):
    """float32 stereo (n, 2) for the options, plus (peak dBFS or None, clipped samples, notes)."""
    o = opts or AudioOptions()
    if o.loops < 0:
        raise ExportError("loops cannot be negative")
    player = TunePlayer(tune, loop=o.loops > 0)
    notes = []
    if o.loops and not player.loop:
        notes.append("the song does not loop (%s), so --loops has no effect"
                     % ("it is marked once" if tune.once else "it has no section after the loop point"))
    total = o.end if o.end is not None else player.frames(o.loops, o.tail)
    if o.end is not None and o.end <= o.start:
        raise ExportError("the end (frame %d) is not after the start (frame %d)" % (o.end, o.start))
    if o.start >= total:
        raise ExportError("the start (%.2fs) is past the end of the audio (%.2fs)" % (o.start / RATE, total / RATE))
    audio = player.render(total)[o.start:total]
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if o.trim_db is not None and peak > 0:
        loud = np.nonzero(np.abs(audio).max(axis=1) > peak * 10 ** (o.trim_db / 20))[0]
        audio = audio[:min(len(audio), int(loud[-1]) + 1 + int(PAD * RATE))]
    if o.normalize:
        if peak > 0:
            audio = audio * (10 ** (o.peak_db / 20) / peak)
        else:
            notes.append("the audio is silent, so it could not be normalized")
    if o.volume != 1.0:
        audio = audio * o.volume
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    clipped = int((np.abs(audio) > 1.0).sum())
    if clipped:
        notes.append("%d sample(s) were over full scale and are clipped (lower --volume, or use --normalize)" % clipped)
    return audio, (min(0.0, 20 * np.log10(peak)) if peak > 0 else None), clipped, notes


def write_wav(path, audio):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(to_pcm16(audio))


def write_audio(path, audio, fmt):
    """Write float stereo samples as wav (always available), or as mp3 / ogg through the soundfile package."""
    if fmt == "wav":
        return write_wav(path, audio)
    try:
        import soundfile
    except ImportError:
        raise ExportError("writing %s needs the soundfile package (pip install soundfile, or chipsynth[encode])" % fmt)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    form, subtype = ENCODERS[fmt]
    soundfile.write(str(path), np.clip(audio, -1.0, 1.0), RATE, format=form, subtype=subtype)


# ------------------------------------------------------------------ the song
def format_of(path, fmt=None):
    if fmt:
        if fmt.lower() not in FORMATS:
            raise ExportError("unknown format '%s' (formats: %s)" % (fmt, ", ".join(FORMATS)))
        return fmt.lower()
    f = SUFFIXES.get(Path(path).suffix.lower())
    if not f:
        raise ExportError("can't tell the format from '%s'; end it in %s or give --format" % (path, ", ".join(SUFFIXES)))
    return f


def export_song(pkg, song_id, path, opts=None, fmt=None, lib=None):
    """Write song `song_id` of `pkg` to `path` as wav, mid, mml or csp (see the module doc).  Returns a Result."""
    opts = opts or AudioOptions()
    fmt = format_of(path, fmt)
    song_id = pick_song(pkg, song_id)
    if fmt not in AUDIO and opts.used():
        raise ExportError("%s only applies to audio (%s), not a %s"
                          % (", ".join("--" + k.replace("_db", "") for k in opts.used()), ", ".join(AUDIO), fmt))
    path = Path(path)
    r = Result(path, fmt)
    try:
        if fmt in AUDIO:
            audio, r.peak_db, r.clipped, r.notes = render_audio(pkg.tune(song_id, lib), opts)
            write_audio(path, audio, fmt)
            r.frames, r.written = len(audio), [path]
        elif fmt == "mid":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(midi.to_midi(pkg.tune(song_id, lib)))
            r.written = [path]
        elif fmt == "mml":
            text, files = components.song_to_mml(pkg, song_id, r.notes)
            r.written = [components._write(path, text)] + components._write_samples(path.parent / "samples", files)
        else:
            one = pk.Package(name=song_id, author=pkg.author, comment=pkg.comment)
            one.add_song(song_id, pkg.songs[song_id], pkg.library(lib))
            pk.save(one, path)
            r.written = [path]
    except mml.MMLError as e:
        raise ExportError("song '%s' does not compile: %s" % (song_id, e))
    return r
