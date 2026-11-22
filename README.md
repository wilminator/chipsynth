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
| `formats/` | other tools' files, with a report of what could not be carried over: `formats.famistudio_text.load(text)` -> songs, samples, report; `formats.goattracker.load(bytes)` and `formats.sf2.load(bytes)` and `formats.sidwizard.load(bytes)` likewise; the writers are `formats.famistudio_export.dump(tune)` and `formats.goattracker_export.dump(tune)` and `formats.famitracker_export.dump(tune)` -> file, report |
| `midi.py` | Standard MIDI files: `to_midi(tune)`, `from_midi(bytes, voices=None, drum_lanes="auto")` -> Song (a voice per part, up to 16; `voices=N` folds the rest into gaps; channel 10 drums go to D1 / D2, and to D3-D8 when hits fall together) |
| `songedit.py` | `Editor`: note-level edits of a Song (add, erase, pitch, resize, move) with undo / redo |
| `song.py`, `songmml.py` | the `Song` model (clips, repeats, calls kept as objects), `Song.compile()` to a `Tune`, MML import / export: `Song.from_mml(text)`, `song.to_mml()` |
| `package.py`, `packageset.py`, `packagetext.py` | the `.csp` package file (several songs + shared instruments / samples), `PackageSet` (load many, pick `package:song`), text dump / build |
| `components.py`, `exporter.py`, `__main__.py` | the command line (below): list / filter / export / import components, export a song as wav, mid, mml or csp |
| `player.py` | `TunePlayer`: schedules a `Tune` on the synth, loops, tails ring over the restart |
| `output.py` | `MusicOut`: live playback through pygame (the only place pygame is used) |
| `musicgen.py` | a seeded tune generator (`generate(kind, seed, mood, tempo)` returns MML text; options below) |

The tune language is described in `DESIGN-MUSIC.md` (project root) and, shorter, on the Music editor's help pages.
Rendering is exact: the same samples come out however the output is cut into blocks.

## The generator and its options

`generate(kind, seed, mood, tempo)` returns a finished tune as MML text; the same arguments always give the same text. Kinds:
`title`, `victory`, `gameover`, `fight`, `boss`, `world`, `maze`, `town`. Moods: `calm`, `tense`, `bright`, `dark`.

    python -m chipsynth generate boss --seed 7 --mood dark -o boss.mml

Everything past the four arguments is an **option**. Options are off by default and, with all of them off, the text is exactly
what the generator has always written (a test pins it), so a game with fixed seeds keeps its music. An option never moves a
note: it adds words to the same notes. Each one is a word in the composer's New dialog (toggle buttons, or typed after the
kind with `new_from_generator`), a command-line flag and a keyword of `generate`.

| word | flag | `generate(...)` | adds |
|---|---|---|---|
| `dac` | `--dac` | `dac=True` | the drums as samples on D1 / D2 (kick, snare, hats, crash, toms) instead of the noise voice |
| `dac-bass` | `--dac-bass` | `dac_bass=True` | the bass as pitched samples on D2 |
| `voice` | `--voice` | `voice=True` | a rare one-shot "hey" on D1 (boss intro, turnarounds, a jingle's end) |
| `echo` | `--echo` | `fx="echo"` | a header `echo` whose delay is a beat fraction of the song's tempo (a dotted eighth in a town, an eighth in a fight, a full beat in a maze), ping-pong (`wide`) in the roomy kinds; the bass is sent (nearly) dry and the arpeggio sits behind the lead |
| `reverb` | `--reverb` | `fx="reverb"` | a header `reverb` sized by kind (small in a fight, large in a maze or a game over), same sends |
| `filter` | `--filter` | `fx="filter"` | a low-pass on the voices that suit the kind: a swept (`fsweep`) resonant filter on a boss's buzz lead, a gently opening one on a fight lead, a world or title arpeggio, a breathing filter on the maze and game-over pads (which become saws so there is something to filter). Not on a victory jingle (too short) |
| `vibrato` | `--vibrato` | `fx="vibrato"` | `vib` on the lead's held notes (a quarter note or longer) and `vib off` on the short ones; faster for tense, gentler for calm |
| `slide` | `--slide` | `fx="slide"` | `slide` into some leaps of the lead, and into the octave jumps of a fight or boss bass, only between notes that touch; a game over wails (most notes slide) |
| `sweep` | `--sweep` | `fx="sweep"` | `pws` on a pulse arpeggio, a pulse lead (not a flute or a saw) or a pad (which becomes a pulse pad) |
| `fx` | `--fx` | `fx="all"` | all six effect words at once |
| `degrees` | `--degrees` | `degrees=True` | the lead, arpeggio and bass written as scale degrees under a `key` line (`key c# minor`, `o4 1:4 3 5`); change the key line to transpose the song |

`fx` takes a word, a list of words or a string (`"echo,filter"`). A word a kind has nothing for adds nothing, and is not
recorded: `generate("victory", 2, fx="filter")` is the plain victory tune. When echo, reverb or a filter is on the melodic
voices are turned down one to three volume steps to leave room for what they add. The first line of a generated tune records
what was written (`fx=echo+filter+degrees`), `musicgen.fx_options(text)` reads it back as `(effects, degrees)`, and the
composer's regenerate-warning (`is_edited`) covers it.

    python -m chipsynth generate town --seed 5 --mood calm --echo --reverb --degrees
    python -m chipsynth generate boss --seed 7 --fx --dac -o boss.mml

The WAV, MIDI, MML and package writers handle all of it. The FamiStudio exporter has no echo, reverb, filter, vibrato,
slide or pulse sweep to write, and its report says so ("left out: echo and reverb", "slide into a note", "instrument
'Inst2': vibrato, filter, filter sweep"); equal instruments are written once, so a tune whose vibrato switches on and off
does not turn into dozens of identical FamiStudio instruments. There is no GoatTracker exporter yet.

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
| `--gain DB` | change the level by DB dB after normalizing (default 0); a boost past full scale clips, and the report says so |
| `--fit` / `--no-fit` | turn the level down until it follows cleanly (cleaner, quieter); off here, on for `import` and the composer |
| `--preview` | print the size, duration, chosen rate and parts, and write nothing |

    python -m chipsynth.dpcm convert in.wav GAMEDIR --rate auto --channel left --trim --fade 50 --preview

`--rate` also takes the Hz of a table rate (`8363`). The DAC plays only the 16 NES DPCM rates (4182 to 33144 Hz): a higher
rate is brighter and bigger (rate 15 is about 4.1 KB a second, rate 0 about 0.5 KB), a low one is dull and noisy but suits
kicks and bass.

The same options come in two more places, with the same defaults (rate 12, channels mixed, normalized, level fitted):

    python -m chipsynth import music/forest.csp kick.wav --id kick --rate 7 --channel left --trim --gain -3

adds the WAV to the package as a sample, and File > Import > WAV (or dropping a `.wav` on the composer) opens an options
dialog with the same choices. Either one reports the rate, length, bytes and clipping (the status line, and Last report).

In Python: `dpcm.convert_wav(x, wav_rate, dpcm.ImportOptions(...), name, stats)` returns `(Sample, notes)` (`stats` is
filled with peak, clipped and frames; `dpcm.report_lines` words it), and `dpcm.preview(...)` returns
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

In the tracker each note shows its volume (`v10`) beside its name; a note with the volume most of its voice's notes have
is drawn dim, so the notes that differ stand out. With the cursor on a note, `,` / `.` lower / raise its volume (Shift: by
4), or press `/` and type the volume: one digit (0, 2-9) sets it at once, `1` waits for a second digit (10-15) or Enter,
Backspace edits and Esc cancels. Each change is one undo step, and it is written to the song as a `v` command, so it
survives MML export and import.

### The File menu

The **File** button at the left of the top bar (or F10) opens the menu; Up / Down, Right / Left (open or close a submenu),
Enter and Esc work it from the keyboard. The shortcuts keep working with or without the menu.

| entry | does |
|---|---|
| New from generator... (Ctrl+N), New blank song | a generated tune, or four empty bars; both open unsaved |
| Open... (Ctrl+O), Open from packages... | a `.csp` / `.mml` / `.mid` file chooser; the Songs list of the loaded packages |
| Save (Ctrl+S), Save As... (Ctrl+Shift+S) | as before: `.csp` writes a package, `.mml` text, `.mid` a MIDI export |
| Import > MML, MIDI, FamiStudio text (.txt), FamiStudio project (.fms), GoatTracker 2 (.sng), SID Factory II (.sf2), SID-Wizard (.swm), WAV sample | the same readers as `python -m chipsynth import` |
| Export > WAV, MIDI (Ctrl+E), MML, FamiStudio text (.txt), FamiTracker text (.ftm.txt), Package copy (.csp) | the same writer as `python -m chipsynth export-song`, for the song as it is now (unsaved edits included) |
| Last report... | shows the latest import / export report again |

What a conversion approximated or left out is never silent: the status line counts it, and a short report window lists
every line (a clean conversion needs no window). Dropping a `.sng`, `.sf2`, `.swm`, `.txt`, `.fms` or `.wav` on the window imports it too.

* **WAV** export asks for the trips round the loop, normalizing to -1 dBFS, and the whole song or just the selected notes;
  the last notes ring for two seconds. **FamiStudio** export asks NTSC or PAL and which Version to stamp (the default, a Reference file..., a typed version, or None; the last choice is remembered; see "FamiStudio versions" below); **FamiTracker** export asks NTSC or PAL; **GoatTracker** export asks PAL or NTSC, the multispeed and the tempo mode (see below); **GoatTracker** import asks the region
  (the file does not record it), the multispeed and which subtune; a FamiStudio file with several songs asks which; **SID Factory II** import asks the timing (as the file says, NTSC or PAL) and which song; a **SID-Wizard** import asks the region and the subtune (the file records its own frame speed).
* **One song** from a FamiStudio, GoatTracker or SID-Wizard file opens as a new unsaved song (Save asks where to put it), like a
  MIDI file. **Several songs** are written into a `.csp` package as `import` does: the open song's package when it has
  a `.csp` file, else `NAME.csp` beside the imported file (an existing file keeps its other songs, and the same ids are
  replaced, with a copy of the old file in `backups/`). The first of them opens. The options window says where they go.
* **MIDI drums** (channel 10, General MIDI percussion) import onto the DAC lanes: kick, snare, hats, crash and toms play the
  built-in kit samples (the note-to-sample table is `GM_DRUMS` in `chipsynth/midi.py`) and velocity is the volume. Kick /
  snare / toms prefer D1 and hats / crash D2, and when two hits fall on the same moment (the same 1/32 grid step) the
  stronger keeps its lane and the other moves to the free one. General MIDI drums are polyphonic, so a third, fourth, ...
  simultaneous hit goes to D3, D4, ... (the lowest free lane, in the order kit priority sorts them) up to D8. The **Drum
  lanes** option decides how far that goes: **Auto** (the default) uses as many lanes as the file's busiest moment needs
  and writes `dacs N` in the song; a number from 2 to 8 allows at most that many. A file whose drums never need more
  than two lanes imports exactly as it always did (no `dacs` line). Percussion the kit has no sample for (cowbell,
  congas, ...) and hits that fit on no allowed lane are listed in the import report (in `python -m chipsynth import` as
  `warning:` lines), never dropped silently, and a `drums:` line says how many lanes were used. The composer asks for the
  Drum lanes (a row in the options window, like the other imports) when you Import > MIDI, Open a `.mid` or drop one on
  the window and the file has percussion; a file without drums opens at once. Export writes the kit hits of every DAC lane
  (D1 to D8) as channel 10 notes, simultaneous ones as several notes at one tick (the shouted "hey", the pitched bass and
  your own samples have no MIDI drum note and are reported as left out), so a round trip keeps the drums. A file without
  percussion imports exactly as before.

  ```
  python -m chipsynth import PKG.csp FILE.mid --drum-lanes auto
  python -m chipsynth import PKG.csp FILE.mid --drum-lanes 4
  ```

  A song with more than two DAC lanes plays and renders (WAV, MIDI) normally and the roll and tracker show every lane. The
  FamiStudio export has one DPCM channel, so it keeps D1 and reports `DAC tracks D2 and up` as left out.
* Anything that would replace the open song (New, Open, Import) first asks about unsaved edits: Save, Don't save, Cancel.
* Import MML makes an unsaved copy (Open edits the file itself). Export over an existing file keeps a copy in `backups/`.

In Python the menu's work is methods that take a path (no file dialog): `Composer.import_file(path, kind, region=,
multispeed=, song=, package=)`, `Composer.export_as(fmt, path, loops=, normalize=, part=, region=, fs_version=, fs_reference=)` and `new_blank()`,
over the window-free helpers in `composer/fileops.py`.

### FamiStudio versions

FamiStudio's text format only loads in a FamiStudio whose version matches the one that wrote the file (its first two
numbers; newer sources skip the check when the file has no `Version` at all). Versions keep moving, so chipsynth
names one in one place only, `DEFAULT_VERSION` in `chipsynth/formats/famistudio_export.py` (4.5.3), and lets you
override it. To find the setting for your FamiStudio, save any project with File > Export > FamiStudio Text, then:

```
python -m chipsynth export-song SONG.csp -o OUT.txt --fs-reference FROM_YOUR_FAMISTUDIO.txt
```

copies the exact Version string from that file (the report says if the file has none, and refuses a file that is not
FamiStudio text). Or type the version yourself, or leave it out:

```
python -m chipsynth export-song SONG.csp -o OUT.txt --fs-version 4.2.0
python -m chipsynth export-song SONG.csp -o OUT.txt --fs-version none
```

The composer's File > Export > FamiStudio text dialog has the same three choices and remembers the last one. The
chipsynth importer reads files with or without a Version. The export report always says which stamp was written.

### Effects and key changes on notes

Pick one or more notes and press **E** (or the **Effects and key...** button at the foot of the inspector; in the tracker the
button, since E is a piano key there). The dialog works like the File menu's options: Up / Down pick a row, Left / Right (or
a click, or Space) change it, Enter applies, Esc cancels, Delete sets every effect to off.

| row | does |
|---|---|
| Vibrato, Arpeggio (and its speed), Slide, Pulse sweep | set the effect on the picked notes only; the notes around them keep what they had. Notes that differ start on "keep each note's own"; a value that is not one of the presets is shown as "custom" and kept |
| Key change | at the first picked note's start (the Play-from marker when nothing is picked): leave it, set the key below, remove the key change that starts there, or `key off`. Notes after it are rewritten as degrees of the key (letters after `key off`) so every one sounds as before. "applies to" is this voice or all voices |

A note's effects show as a small mark at its right end (`~` vibrato, `/` slide, `^` arpeggio, `^m` a minor one, `w` pulse sweep;
the status line spells them out when the pointer is on a note), and key changes show on the ruler and in the inspector's
"Key changes" list (its **x** removes one). The effects and the key change of one OK are one undo step; if either is refused
(a key inside a note or a pattern, a pattern that would have to be both letters and degrees: use Make unique first) nothing
changes and the dialog comes back with the reason in the status line.

In MML this is `vib`, `arp`, `arprate`, `slide` and `pws` words before the note, with the old value written again before the
next note, and `key d minor` before the notes it governs. In Python it is the Editor's own calls, each one undo step:

    ed.set_note_effects([(voice, start_tick), ...], vib=(5, 3), arp=(0, 4, 7), arprate=8, slide=12, pws=(25, 75, 6))
    ed.set_key_at(tick, 2, "minor", voices=[0, 1])      # tonic 0-11, or None for `key off`; keep_pitch=False modulates
    ed.remove_key_at(tick, voices=[0, 1])
    ed.edit_notes(selection, effects={...}, key={...})  # both in one step: what the dialog does

## Command line (`python -m chipsynth`)

A `.csp` package holds songs and what they share. Its *components* are of five types: `song`, `instrument`, `fx` (effect
sets), `scale` and `sample` (DPCM). `add_song` copies what a song uses into the song, so most instruments, effect sets and
scales belong to a song (their scope in `list`); the rest are `shared` by all the package's songs. `python -m chipsynth -h`
and `COMMAND -h` give every option.

| command | does |
|---|---|
| `import PKG.csp FILE.mml...` | add songs to a package (created if missing). `--id NAME` names the song, `--shared` adds a library file's instruments / fx / scales / samples to the package, `--no-replace` refuses an id already there, `--lib DIR` / `--samples DIR` say where a game's `music/lib` and `.dpcm` files are. `.mid` files work too, and so do FamiStudio text files (`.txt`, from File > Export > FamiStudio Text; `--song NAME`, `--region ntsc|pal`, `--report FILE`) and FamiStudio projects (`.fms`, formats 15 to 19 = FamiStudio 4.1 to 4.5, same options), and so do GoatTracker 2 songs (`.sng`; `--song N` is the subtune, `--region pal|ntsc` defaults to pal, `--multispeed 1|2|4`, `--report FILE`: the file records none of these; the pattern commands, wavetables, pulse tables and the filter are carried over where chipsynth can play them, and the report lists what is approximated or left out), and SID Factory II songs (`.sf2`; `--song N` is the song, `--region pal|ntsc` overrides the PAL / NTSC the file records, `--report FILE`), and SID-Wizard songs (`.swm`; `--song N`, `--region pal|ntsc` (default pal: the file does not say), `--report FILE`) |
| `list SOURCE...` | the components of `.csp` / `.mml` / folders: `--type song,sample,...` (aliases `instr`, `key`), `--name 'd*'` (a glob; `TYPE:NAME` for one type) |
| `export PKG.csp` | components as MML: `-d DIR` (songs as `ID.mml`, the rest in `lib/PACKAGE.mml`, samples in `samples/NAME.dpcm`, which is a game's `music/` layout), or `-o FILE` (`-` for stdout) for one thing; `--type` / `--name` choose; `--midi` writes songs as `.mid`. With no `--type`: songs and the shared parts |
| `export-song SOURCE[:SONG] [SONG] -o OUT` | one song as `wav`, `mid`, `mml`, `csp`, `famistudio` (`.txt`), `famitracker` (`.ftm.txt`; a plain `.txt` is `famistudio`, so a FamiTracker file ends in `.ftm.txt` or needs `-f famitracker`) or `goattracker` (`.sng`) (from OUT's ending, or `-f`). `render` is the same command. FamiStudio: `--region ntsc|pal`, `--fs-version X.Y.Z`. GoatTracker: V1-V3 only; `--region pal|ntsc` (default pal, as the importer), `--multispeed 1|2|4`, `--gt-tempo exact|speed`; what it left out or approximated is printed |
| `generate`, `dump`, `build` | as before |

`export-song` options for a **WAV** (any of them with another format is an error):

| option | does |
|---|---|
| `--volume V` | master volume: `0.5`, `50%` or `-6dB` (default 1). Anything over full scale is clipped and reported |
| `--normalize`, `--peak DB` | scale the loudest sample to `DB` dBFS (default -1) of the part you keep, before `--volume` is applied |
| `--start T`, `--end T` | keep from `T` to `T`: `1.5`, `250ms`, `1:05.5` or `88200f` (audio frames, 44100 per second). Past the natural end the song keeps looping if `--loops` > 0, else is silent |
| `--loops N` | trips round the loop after the first pass (nothing for a `once` song, which says so) |
| `--tail S` | seconds to let the last notes ring (default 2) |

**FamiTracker text** (`-f famitracker`, `--region ntsc|pal`) is for FamiTracker and for FamiStudio's File > Import > FamiTracker Text. Unlike FamiStudio's own
text format, which only loads in the release that wrote it, FamiTracker's `# FamiTracker text export 0.4.2` has not changed between versions. V1, V2 -> Pulse 1, 2;
V3 -> Triangle; V4 -> Noise; D1 -> DPCM (samples and their keys). Instruments are 2A03 instruments with volume (the ADSR sampled a frame at a time), duty (a
pulse-width sweep too) and arpeggio sequences; notes carry their volume; the order list has a loop (`Bxx`; `once` songs halt with `C00`). Echo, reverb, filter,
vibrato, slide, pan, tempo changes, voices past V4, DACs past D1 and the release tail are left out, and said so in the report.

    python -m chipsynth export-song music/forest.csp:dawn -o dawn.ftm.txt --region pal

    python -m chipsynth import music/forest.csp dawn.mml dusk.mml --lib game/music/lib
    python -m chipsynth list music/forest.csp --type song,sample
    python -m chipsynth export music/forest.csp -d out --name 'd*'
    python -m chipsynth export-song music/forest.csp:dawn -o dawn.wav --loops 2 --normalize --volume -3dB --start 2 --end 1:30

A song exported as MML (`export`, or `export-song -f mml`) declares the package samples it plays under `[sample]` and writes
them as `samples/NAME.dpcm` beside it; import it with `--samples samples` to get the same package back. In Python the same
work is `components.py` (`components`, `select`, `export_mml`, `import_mml`) and `exporter.py` (`export_song`, `AudioOptions`).

A song exported as **GoatTracker 2** keeps V1-V3 as the three SID voices, with notes, rests, key-offs, the loop point and the tempo,
and each instrument's ADSR, waveform and pulse width; an arpeggio, a pulse-width sweep and vibrato become a wavetable, a pulse
table and a speed table entry. More voices, the DACs, echo, reverb, volume, pan, slides and the filter are left out, and every
change is printed. The file does not record PAL or NTSC or the multispeed, so open it in the importer (or play it) with the same
settings:

    python -m chipsynth export-song music/forest.csp:dawn -o dawn.sng --region pal --multispeed 1
    python -m chipsynth import music/back.csp dawn.sng --region pal --multispeed 1

`--gt-tempo exact` (the default) changes the speed on the rows that need it when the song's tempo is not a whole number of frames
a row, so every note starts on the nearest frame; `--gt-tempo speed` writes one speed (a whole number of frames a row) and says how
far the tempo is out. A row lasts at least 3 frames, so a very fast song is moved to coarser rows (or use `--multispeed 2`).

## Tests

    python -m unittest discover -t . -s chipsynth/tests      (from the project root)

`tests/test_boundary.py` fails if the library ever imports the game or anything beyond numpy and scipy (pygame is allowed
only lazily in `output.py` and in `composer/`). The suite needs nothing outside this directory (test data is in
`tests/data/`), so it also runs from a standalone checkout of chipsynth.

## Licence

Copyright (C) 2026 Michael Allen Wilmes.

chipsynth is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the file `LICENSE` for the full text.

Under the GPL, a program that includes or links chipsynth (the Free Software Foundation reads linking that way) must be released under the GPL too. Music you make with it is yours; the licence covers the code, not the songs it renders.
