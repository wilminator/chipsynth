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
Part A steps 1-5 done on branch `chipsynth-split` (2026-10-13): the move with same-module stand-ins in `blobs/`, the
boundary test, the engine tests moved to `chipsynth/tests`, and every import in `blobs/` and `tests/` now names
`chipsynth` directly. The stand-ins (`blobs/synth.py` etc., also usable as `python -m blobs.synth`) are unused by the
repo and kept for your own scripts; they move to `legacy/` when you say. chipsynth/README.md has the quick start. Git history now holds the old layout, so `legacy/music_before_split.py` is not needed.

**C0 done** (2026-10-13): `chipsynth/song.py` (the `Song` model: clips, repeats, calls, comments and every MML token as
an event; `Song.compile(lib)` plays the events through the parser's own voice state, so it gives exactly the `Tune`
that parsing the MML gives) and `chipsynth/songmml.py` (`Song.from_mml`, `song.to_mml`). `chipsynth/tests/test_song.py`
checks `parse == from_mml().compile()`, that the exported text compiles to the same tune, and that export is a fixpoint,
on the demo songs, generator output (every kind, mood, 3 seeds), a file using every feature, and every MML string in
the test suites. Decisions: the model is notation-level (a clip is relative to the caller's octave, like `[pat]`);
a pattern is note-kind or DAC-kind, not both; an uncalled pattern that does not read is kept as raw text; library
patterns are copied into the song when used, library instruments / fx / scales stay references until the package
step embeds them (C0b); lengths are tick counts written back as the fewest tokens (`4&16`); import drops inline
`//` comments and keeps own-line ones. Limits: a tie into a scale degree and lengths of 1, 2, 3, 5, 7, 11 ticks have no
MML spelling (`SongError`).

**C0b done** (2026-10-14): `chipsynth/package.py` (the `.csp` binary: META INST FXSE SCAL SAMP SONG chunks, varints,
append-only event tags, atomic deterministic save, newer major refused, unknown chunks skipped; `add_song` embeds the
library instruments / fx / scales and the DPCM samples a song uses, renaming a clashing sample `kick_a`),
`packageset.py` (`PackageSet`: `add`, `add_folder`, `songs`, `get`, `tune`, `player`; a bare `.mml` is a one-song
package; a clashing package name is refused and reported), `packagetext.py` + `python -m chipsynth list|dump|build|import|export`.
Game side: `music.toml` `tune = "forest:dawn"` (and `[tunes]` numbers) play a song from `music/forest.csp`; the maker's
Music table tune picker lists every package song. Tests: `chipsynth/tests/test_package.py`, `tests/test_musictable.py`.

**C1 done** (2026-10-14): `python -m blobs.compose [GAMEDIR] [SONG] [--nosound] [--reset]`, package `blobs/composer/`:
`model.py` (Roll from a Tune, `tune_from` seek = remainder + loop section so a seek is seamless, `mixdown` for mute/solo,
`View` with separate horizontal and vertical scale), `layout.py` (panels, draggable splitters, the too-small notice,
roll geometry), `prefs.py` (theme, font size, scales, panel sizes, window size, last song; `%APPDATA%/chipsynth/composer.json`),
`ui.py` (themes, fonts, immediate-mode widgets), `panels.py` (everything drawn), `app.py` (state, events, playback).
Piano roll with keyboard, ruler (click = where Play starts), beat/bar grid, loop-section shading, volume lane, D1/D2 lanes,
playhead (via `TunePlayer.position` and `Tune.sample_to_tick`), scroll bars, overview strip, voice strip with M/S,
inspector (song facts, instrument with ADSR graph, selected note), Songs list, F1 help, light/dark, font size, drop a file.
Engine additions: `Tune.sample_to_tick`, `TunePlayer.position`. Tests: `tests/test_composer.py`.
Screenshots: `docs/composer/`.

**C2 done** (2026-10-14): `chipsynth/songedit.py` `Editor(song, lib)` edits the Song's events (so MML export is exactly what was edited): `add_note`, `erase`, `set_pitch`, `resize`, `move`, `transpose`, `max_dur`, undo/redo (snapshots, `dirty` / `mark_saved`). Every edit keeps all other notes where they were (erase and shrink leave rests; grow eats the rest after the note), is all-or-nothing (`EditError`, song untouched), and keeps degrees as degrees and patterns as patterns (editing a note inside a pattern or repeat edits every use). `Song.compile(trace=Trace())` records which event made each note. Composer: click empty = add note (drag = length), drag = move, drag right end = resize, Delete, arrows nudge, `[` `]` snap (1/4 .. 1/16T), Ctrl+Z / Y, Ctrl+S (backup in `<root>/backups`) / Ctrl+Shift+S, `*` in the title, a warning on closing with unsaved changes. Saves a `.csp` (the whole package it came from) or `.mml`. Tests: `chipsynth/tests/test_songedit.py` (incl. random-edit fuzz on hand-written and generated songs), `EditTests` in `tests/test_composer.py`.
Not in C2 (later): multi-select, copy / paste, quantize (C7); the toolbar hides the zoom buttons at the 960 px minimum width.  

**C3 done** (2026-10-14): instrument editing from the inspector. `Editor.edit_instrument`, `edit_voice_instrument` (a voice with no `@instr` gets its own named copy, one undo step), `set_voice_instrument`, `copy_instrument`, `instrument_names`, `voice_instrument`. Inspector: instrument picker (< >), "Make my own copy", "also played by ..." warning, wave, pulse width / noise mode, A D S R, volume, pan, echo send, filter type / cutoff / resonance, vibrato rate / depth, all with - / + buttons, undoable, heard live while playing. Editing a library instrument makes it the song's own override (written under `[instr]`). Not yet: arpeggio, slide, pulse-width and filter sweeps (C5 per-note effects), a voice that changes instrument mid-song (the first `@instr` is what the inspector edits).  

**C4 done** (2026-10-14): drum lanes. `Editor.add_hit / erase_hit / move_hit / set_hit_sample`, `sample_names()`, `import_sample(name, dpcm.Sample)`; the compile trace now covers D1 / D2 (`Trace` has 6 lanes). Composer: click a drum lane to place the picked sample (one snap long), drag a hit to move it, Delete / arrows, a sample picker with Preview and Import WAV (dialog, or drop a .wav on the window; converted at DPCM rate 12 with `fit`), imported samples are saved inside a `.csp`, or as `music/samples/NAME.dpcm` when saving `.mml` from a game folder (refused with a reason otherwise). Not yet: stereo import, choosing the DPCM rate / trimming, per-hit volume (C5), `stop` / `level` events are kept but not drawn.  

**C5 done** (2026-10-14): song settings, tempo map, loop point, patterns. `Editor.set_song(title, author, comment, bpm, time, detune, once, echo, reverb)`, `set_tempo_at / remove_tempo / tempo_marks`, `set_loop / loop_at` (markers are inserted between notes, splitting a rest if needed, or between pattern calls; refused inside a note or pattern), `clip_uses / rename_clip / delete_clip` (unused only), `inside` + `make_unique` (replace a pattern call or repeat with its own copy of the events). `Piece.stack` records the calls and repeats an event is inside; `Trace` also holds tempo and loop markers. Composer inspector: editable title / author (a text box overlay), tempo, time signature, loop from the cursor bar, tempo changes list (also shown on the ruler), echo (time, feedback, mix, wide), reverb, tuning, once, a patterns list (delete unused), "Inside pattern ... / Make unique" for a selected note, and a scale guide (shades the roll's rows; starts from the song's first `key`). Not done: editing `key` events (a key makes letter notes invalid, so the guide is display-only), per-note effect changes (arp / slide / sweeps), making a new pattern from a selection (needs multi-select, C7).  

**C6 done** (2026-10-14): the tracker view (Tab or the Tracker button). Time runs down the page, one row per snap length, columns V1-V4, D1, D2; notes show on their start row with a `|` hold mark below, off-grid starts are dimmed, drum hits show the sample name; the playing row is highlighted and followed. Typing: z s x d c v g b h n j m and q 2 w 3 e r 5 t 6 y 7 u are a piano (two octaves, `-` `+` / Shift+Up/Down change octave), each key enters a note (a note starting on that row changes pitch and keeps its length, a held note is cut at the row, the new note stops where the next begins), plays a short audition and moves down; Enter places the picked sample in a drum column, Delete / Backspace erase, arrows / PageUp / PageDown / Home / End move, clicking a cell moves the cursor, Space plays from the cursor row, `[` `]` change the row size (the cursor keeps its tick). `Editor.put_note` does the tracker entry as one undo step; also fixes a bug where changing the pitch of a note that had an octave shift wrapped around it lost the shift (the fuzz test now checks the pitch it asked for). `blobs/composer/tracker.py` is the pure model. In the tracker the letter keys are the piano, so M / S / T are not shortcuts there (the buttons still work). Not done: a volume column and per-row effect columns (arp, vibrato...), pattern-order view.  

**C7 done** (2026-10-14): multi-select, clipboard, quantize, MIDI. `Editor.selection_notes / move_notes / erase_notes / quantize / paste_notes` (a selection is `[(voice, start)]`, each is one undo step and all-or-nothing; what a move or paste lands on is cut, not left overlapping; a note that starts inside the trailing rest of a voice may run past the end). Composer: Shift+click adds, Ctrl+drag boxes the picked voice's notes (Shift adds), Ctrl+A, arrows move / transpose the whole selection (Shift = octave), Delete, Ctrl+C / X / V (paste at the Play-from marker), Ctrl+D duplicate after itself, Q snap to the grid, Ctrl+E export MIDI; a `.mid` can be opened or dropped on the window. `chipsynth/midi.py`: `to_midi(tune)` (format 1: a tempo / time-signature track + one track per voice, 96 ticks per quarter) and `from_midi(bytes)` (format 0 / 1, running status, any division; times rounded to 1/32 notes; the first four parts with notes become V1-V4, a voice keeps the higher of overlapping notes; drums are left out with a note in the song's warnings; tempo changes kept). CLI: `python -m chipsynth import PKG.csp FILE.mid`, `export PKG.csp --midi`. The help window now has two columns. Tests: `chipsynth/tests/test_midi.py`, `SelectionTests`, `MultiSelectTests`.

**Composer status:** C0-C7 done. Not built (backlog): loop-range playback (loop just a selected stretch while writing), making a new pattern from a selection, editing `key` events and per-note effects (arp / slide / sweeps) from the roll, a volume column in the tracker, stereo / rate choice for WAV import, importing MIDI drums onto D1 / D2, FamiStudio / SID importers.

## Order of work
1. Part A steps 1-3 (move, boundary test, move tests), one zip.
2. Part A step 4 (imports) and the README quick start, one zip.
3. C0 (Song model, compiler, MML import / export), then C0b (the package format, `PackageSet`, game wiring), then C1 on.
   Confirming depth with you before each milestone (your credits rule).
4. Still open from earlier and slotted in when you say: generator use of effects / degrees / echo / filter, FamiStudio
   and SID imports (they land in `chipsynth` as importers into `Song` once it exists), packaging for PyPI.
