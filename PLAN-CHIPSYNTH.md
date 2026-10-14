# Plan: the `chipsynth` library and the song composer

Decided with Mike (2026-10-13): split the music engine into a subpackage first (it can become its own project later);
the library is called **chipsynth** (checked on PyPI: `chiptune` is taken, `chipsynth` is free); the composer is a
resizable hi-res pygame window that reads and writes the same `.mml` files, with a piano roll and a tracker grid
(switchable; the piano roll first).

Nothing here changes how anything sounds. Each step ends with the full test suite passing and one zip.

---------------------------------------------------------------------------------------------------------------------

## Part A: split the engine out as `chipsynth`

### What the dependency map shows
The core already depends on nothing but numpy/scipy (and pygame only inside the output class):

| module | needs | goes to |
|---|---|---|
| synth.py, synthfx.py, dpcm.py | numpy, scipy | chipsynth |
| mml.py (parser, Tune, library loader) | synth, dpcm (lazy) | chipsynth |
| music.py: TunePlayer, render, render_wav | synth | chipsynth/player.py |
| music.py: MusicOut, open_music_mixer (pygame) | pygame | chipsynth/output.py (pygame optional) |
| musicgen.py (the generator) | mml | chipsynth/generate.py |
| soundcursor.py (QBasic SOUND) | qbrt | stays in Blobs (it is the game's, not the engine's) |
| gamemusic.py, musictable.py, settings.py | the above + game | stays in Blobs |
| make_music.py, make_musictable.py, make_sample.py | maker window | stay in Blobs (they become users of chipsynth) |

### Layout
```
blobs_py/
  chipsynth/            # the library (pure: numpy, scipy; pygame only for live output)
    __init__.py         # the public API (below)
    synth.py  synthfx.py  dpcm.py  mml.py  player.py  output.py  generate.py
    tests/              # its own tests (moved from tests/: test_synth, test_music*, test_music_dac ...)
    README.md  pyproject.toml   (added when it leaves the tree)
  blobs/                # the game: imports chipsynth; thin shims keep old import paths working at first
  tests/                # the game's tests
```
Public API (what the composer and the game use, and what a stranger would): `parse`, `Tune`, `Note`, `MMLError`,
`load_library`, `builtin_library`, `TunePlayer`, `render`, `render_wav`, `MusicOut`, `open_music_mixer`, `generate`,
`dpcm` (`convert`, `read_wav`, `load`, `save`, `Sample`), `RATE`, `Instrument`.

### Steps
1. **Move, with shims.** `git mv`-style move of the seven modules; `music.py` is cut in two (`player.py` and
   `output.py`); `blobs/synth.py`, `blobs/mml.py`, ... become one-line re-export shims so nothing else changes. Nothing
   is deleted. Run the suite.
2. **Boundary test.** A test that scans `chipsynth/` and fails if anything imports `blobs`, `pygame` (outside
   `output.py`, and only lazily), or the game's modules.
3. **Move the engine tests** into `chipsynth/tests/` and point them at `chipsynth` directly; the game's own tests keep
   using Blobs. Run both.
4. **Migrate imports** in `blobs/` from the shims to `chipsynth` (a mechanical pass); the shims stay one release as
   re-exports, then move to `legacy/` (kept, not deleted).
5. **Packaging** (only when you say so): `pyproject.toml`, version 0.1.0, README with a 15-line quick start, licence
   header (your copyright; the licence choice is yours to make then). Optional extras: `chipsynth[play]` adds pygame.

### Decisions / risks
- The sample-folder convention (`music/samples` next to `music/lib`) is baked into `load_library`; it stays, but the
  folder becomes a parameter so other projects can lay their files out differently.
- `settings.py`, the music table and the town hints are *not* engine; they stay.
- Risk: import cycles when `music.py` is cut; the player has no pygame import, so none expected.
- Size: about a day of care; no new behaviour; ~771 tests must all still pass untouched at step 1.

---------------------------------------------------------------------------------------------------------------------

## Part B: the song composer (built only on the `chipsynth` API)

Entry: `python -m blobs.compose GAMEDIR [tune]` and a launcher entry "Composer" (the text Music editor stays).

### The song model is the saved format (decided 2026-10-13)
MML is the **import / export** format, not the saved one. The composer edits a native `Song` and saves it in the
native package format (Part C). That removes the earlier worry (a visual editor flattening hand-written repeats and
patterns): the native `Song` keeps *clips* (named patterns, repeats, loops) as real objects, so
MML -> Song keeps them, and Song -> MML writes them back as `[pat]`, `!name` and `[..]n`.
- **Importer** (`MML text -> Song`): the existing parser extended to keep structure; comments are kept as notes where
  they sit on a line of their own, otherwise dropped (the original file is untouched anyway; import never overwrites it).
- **Exporter** (`Song -> MML text`): this is the "canonical writer" from the first draft of the plan, now an exporter. It
  is deterministic and tested by `MML -> Song -> MML -> Tune` equality on every example and on the generator's output.
- **Compiler** (`Song -> Tune`): expands clips into the flat `Tune` that `TunePlayer` already plays. The player does not
  change at all.

### Screens (a normal desktop window the user can resize or maximise, minimum 960x600; mouse + keyboard)
Not an 8-bit-styled app: it is a modern tool for writing 8-bit-sounding music. Real anti-aliased fonts, a clean
layout that reflows to the window (the piano roll takes all spare space; side panels keep fixed widths and collapse
below the minimum), high-DPI aware, light/dark themes. The window size and panel layout are remembered per user.

Flexible, not fixed (Mike, 2026-10-13): the user sets the **horizontal and vertical view scale separately** (time zoom and
pitch/row zoom, independent of each other), sets the **font size** (UI text scales with it), and **drags the borders
between the panels and the main display** to resize them (splitters; sizes remembered). The minimum size is enforced
by a notice ("This window is too small - please make it larger") drawn over the work area instead of a hard limit, so
the window never fights the user's window manager.
- **Top bar:** play / stop / loop, position, tempo, time signature, snap (1/4 ... 1/32, triplets), zoom, view switch.
- **Left, voice strip:** V1-V4 and D1-D2: name, instrument, volume, pan, send, mute/solo, colour.
- **Centre, piano roll** (lane per voice, the selected one in front, others ghosted): pencil / select / erase tools,
  drag to move and resize, velocity (volume) lane beneath; a DAC lane shows sample blocks with their lengths.
- **Centre, tracker grid** (switchable): rows = steps, columns per voice = note / instrument / volume / effect.
- **Right, inspector:** the instrument editor (wave, pw, ADSR drawn as a graph, vib / slide / arp / pws, filter, send,
  pan), the song settings (echo, reverb, key), and a sample list (play, import).
- **Bottom:** error / status line and a mini overview of the whole song with the playhead.

### Behaviour
- Playback uses `TunePlayer`; editing while playing rebuilds the player at the playhead (no gap, using `skip_to`).
- Undo / redo (command objects), copy / paste, multi-select, transpose, quantize, duplicate bar, loop-range playback.
- Keyboard note entry (piano keys) with audition; MIDI-style preview of a clicked note through the voice's instrument.
- Save = the native package (`save_with_backup`); Save As; Export MML / Import MML; the text Music editor keeps working on loose `.mml` files, and "Send to composer" imports one into a package.

### Milestones (each ends in a zip + tests; I write tests for the model/commands, you run the interaction)
- **C0** `Song` model, `Song -> Tune` compiler, MML importer / exporter, round-trip tests (no UI, no file format yet).
- **C0b** the native package format and loader (Part C), `dump` command, tests.
- **C1** the window, piano roll display, scrolling / zoom, playback with a live playhead.
- **C2** note editing (pencil / select / move / resize / erase), snap, undo / redo, save.
- **C3** voice strip and the instrument inspector (envelope graph, wave, fx, filter, send).
- **C4** DAC lanes, sample list and import button, stereo samples (samples embed in the package).
- **C5** song settings (tempo map, echo, reverb, key / scale-aware piano roll, per-note effect changes), clips.
- **C6** the tracker view over the same model.
- **C7** polish: copy / paste / transpose / quantize, shortcuts, help, loop-range, MIDI import / export (stretch).

### Decisions / risks
- Resolution & fonts: pygame default fonts are plain; I would bundle one open-licence monospace font (small) so the
  roll reads well. Say if you would rather not add a font file.
- Mouse text input in pygame needs a small widget set (buttons, sliders, text boxes): written once, reused.
- Writer fidelity is the hard part: anything the parser can express, the writer must be able to say back, or the
  composer must refuse and warn. The round-trip test (every `.mml` in the repo's examples and the generator's output)
  is the safety net.
- This is the biggest piece of work so far: roughly C0 half a day, C1-C2 a day, C3-C5 about two days, C6-C7 a day.
  I will confirm depth with you before each milestone (your credits rule).

---------------------------------------------------------------------------------------------------------------------

## Part C: the package format and the multi-package player (decided: binary chunks; music.toml says `pkg:song`)

### The package file (`*.csp`, "chipsynth package")
An IFF-style container: magic `CSPK`, format version, then tagged length-prefixed chunks; a reader skips chunks it does
not know (forward compatibility) and refuses a newer *major* version with a clear message.
- `META` package name, author, comment, created / modified.
- `INST` / `FXSE` / `SCAL` shared instruments, effect sets and scales (songs refer to them by name; a song may also carry
  its own, which override).
- `SAMP` shared DPCM samples (the same bytes as a `.dpcm` file, so nothing is converted twice).
- `SONG` one per song: title, author, header (tempo map, time, echo, reverb), clips, tracks (V1-V4, D1-D2) as event
  lists, loop point, once-flag. Songs are addressed by a stable **id** (a short name) as well as a title.
- A `python -m chipsynth dump pkg.csp` prints everything as text, for diffs and debugging; `build` re-assembles from
  that text. Saves are deterministic (same song, same bytes).

### The player API
```
pkgs = PackageSet()                      # any number of packages
pkgs.add("music/forest.csp"); pkgs.add("music/town.csp"); pkgs.add_folder("music")
pkgs.songs()            -> [SongRef("forest:dawn", title, seconds, package) ...]
song = pkgs.get("forest:dawn")           # a Song; compile -> Tune -> TunePlayer as today
pkgs.player("forest:dawn", loop=True)    # TunePlayer ready for MusicOut
```
- A bare `.mml` file still loads as a one-song package named after the file (so everything existing keeps working);
  ids stay unique (`pkg:song`), clashes are reported, not silently overridden.
- `MusicOut` and the game see only a `TunePlayer`; the player itself does not change.

### Game side
- `music.toml` entries accept `tune = "pkg:song"`; a bare name still means a loose `.mml` file. `[tunes]` numbering (the
  town hints) accepts the same. The maker's Music table picks from the songs of every loaded package.
- Music editor (text): unchanged, plus "Import into package" and "Export MML".

---------------------------------------------------------------------------------------------------------------------

## Status
Part A steps 1-4 done on branch `chipsynth-split` (2026-10-13): the move with same-module stand-ins in `blobs/`, the
boundary test, the engine tests moved to `chipsynth/tests`, and every import in `blobs/` and `tests/` now names
`chipsynth` directly. The stand-ins (`blobs/synth.py` etc., also usable as `python -m blobs.synth`) are unused by the
repo and kept for your own scripts; they move to `legacy/` when you say. Next: the quick-start README, then composer C0. Git history now holds the old layout, so `legacy/music_before_split.py` is not needed.

## Order of work
1. Part A steps 1-3 (move, boundary test, move tests), one zip.
2. Part A step 4 (imports) and the README quick start, one zip.
3. C0 (Song model, compiler, MML import / export), then C0b (the package format, `PackageSet`, game wiring), then C1 on.
   Confirming depth with you before each milestone (your credits rule).
4. Still open from earlier and slotted in when you say: generator use of effects / degrees / echo / filter, FamiStudio
   and SID imports (they land in `chipsynth` as importers into `Song` once it exists), packaging for PyPI.
