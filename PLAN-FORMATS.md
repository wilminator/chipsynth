# Importing and exporting FamiStudio and C64 SID editor projects

Study, 2026-10-19. No code yet: this says what it would take, what each format would cost, and what to build first.
It builds on the command line from PR #1 (`import`, `export-song`, `components.py`, `exporter.py`).

## 1. Summary

| target | verdict | why |
|---|---|---|
| **FamiStudio** (text format `.txt`) | **Do first.** Import and export, about 2 slices | chipsynth's channel layout and sample format already are the NES's; MIT; the text format is line based and its writer is short |
| FamiStudio binary `.fms` | Later, import only if at all | compressed binary dump of the in-memory model, versioned per release, "not compatible with forks"; the text format carries the same data |
| **GoatTracker 2** (`.sng`) | **Do second.** Import and export, about 3 slices | the simple, de facto interchange format of the SID scene (SID Factory II imports it); GPL-2+ |
| SID Factory II (`.sf2`) | Skip a native format; reach it through GoatTracker | a driver-defined C64 memory image, no spec found; it already imports GoatTracker `.sng` |
| Furnace (`.fur`) | Third, optional, large | one format covers SID *and* the NES (and more); actively developed; but version-gated, with per-chip instrument types |
| CheeseCutter (`.ct`), SID-Wizard (`.swm`) | Skip for now | no format spec found; SID-Wizard is in maintenance mode; both are reachable through SID Factory II / their own exporters |

Nothing here maps exactly. chipsynth's `Instrument` is a static recipe (waveform, ADSR, vibrato, arpeggio, pulse sweep,
filter sweep); trackers drive instruments with *per-frame programs* (envelopes, tables). Every import is an approximation
that must say what it dropped, and every export must quantize to the target's frame clock. The design in section 5 is
built around that.

## 2. What was checked, and what was not

Checked against the projects' own pages or source (links in section 8):

- FamiStudio is MIT, written in C#, and its `Source/IO` folder holds `FamistudioTextFile.cs`, `ProjectFile.cs`, `FamitrackerTextFile.cs`, `FamitrackerBinaryFile.cs`, `FamitoneMusicFile.cs`, `NsfFile.cs`, `MidiFile.cs`, `VgmFile.cs`, `WaveFile.cs`, `RomFile.cs` and more. Latest release 4.5.3 (the page shows "10 Aug", no year).
- The FamiStudio text format and `.fms` header (details in section 3), read from the source.
- Furnace is GPL-2-or-later / GPL-3, has the C64 SID (6581 and 8580), imports `.dmf`, and documents `.fur` in `papers/format.md`. Latest release shown is v0.6.8.3 ("12 Jun", no year).
- CheeseCutter is GPL-2.0, with an SDL2 port merged "as of 2026/3"; `ct2util` exports `.ct` to PRG or SID files.
- SID Factory II: build 20260314, open beta, imports GoatTracker, CheeseCutter and MOD files, exports nothing but its own packer/relocator.
- SID-Wizard: 1.97 released 24 June 2026 by Hermit, who said no new features are planned after 1.95.
- GoatTracker: Debian lists 2.77, GPL-2-or-later, Lasse Oorni (copyright to 2025).

**Not verified** (the sites were unreachable from the study environment, or say nothing):

- GoatTracker's `.sng` byte layout. Section 4 is from the constants a third-party converter publishes (`GTS5` header, 25-byte instruments,
  63 instruments, 208 patterns, 32 subtunes, 255-row tables, 128 rows per pattern) and from memory of the format. **Slice 0 for
  GoatTracker is reading `gsong.c` / `gcommon.h` and fixing this section.**
- SID Factory II's license (no LICENSE file seen on the repository page; one article says "open source"), SID-Wizard's license
  ("open source" informally, source shipped in the release), and any `.ct` / `.swm` / `.sf2` layout.
- Whether FamiStudio's MIDI file code reads, writes or both.
- The exact compression FamiStudio uses for the `.fms` payload (it is .NET `System.IO.Compression` "Optimal"; probably deflate/zlib).

**Licensing rule for this work:** chipsynth is GPL-3.0-or-later. FamiStudio (MIT), GoatTracker (GPL-2+) and Furnace (GPL-2+/3)
can be read and their formats reimplemented freely. CheeseCutter shows plain "GPL-2.0": do not copy its code into chipsynth
(GPL-2-only is not compatible with GPL-3). In every case write the codec from the format, do not paste source, and
never commit someone else's songs as test data (see section 6).

## 3. FamiStudio

### Formats

- **Text** (`FamistudioTextFile.cs`): one keyword per line followed by `Key="value"` attributes (a `"` inside a value is doubled).
  Tabs nest but are not required. The first line is `Project`. Order: `Project` (Version, TempoMode, Name, Author,
  Copyright, PAL, Tuning, Expansions, ...), `DPCMSample` (Name, DmcInitialValue, `Data` as hex: only the final delta data is stored),
  `Instrument` (with `Envelope` lines: Type, Length, Loop, Release, Relative, Values; expansion instruments add chip fields), `Arpeggio`, then
  `Song` (Name, Length, LoopPoint, tempo fields) holding `Channel` blocks of `Pattern` blocks of `Note` lines, and `PatternInstance`
  lines (Time, Pattern) that place patterns. The loader accepts a file only if its version string starts with the first three characters of the
  running FamiStudio's version. Writing one pinned version (4.5) and reading any 4.x is therefore a design choice, not a given.
- **Binary** (`ProjectFile.cs`): `0x21534D46` ("FMS!") as a little-endian u32, a u32 version (newer than the running build is rejected),
  then a compressed serialization of the whole model. The same `Serialize` method reads and writes, so the layout is
  whatever the C# classes are in that release, with version branches. Forks are warned off. Cost: high and fragile.
- FamiStudio's source also has code for FamiTracker text and binary files (`.txt`, `.ftm`; import is documented, whether it writes both was not checked), NSF, the FamiTone sound engine, ROM, VGM, WAV, MP3 and MIDI.
  Anyone who needs `.fms` can use FamiStudio to export text; we only need the text format.

### Mapping

| FamiStudio | chipsynth | note |
|---|---|---|
| Square 1 / Square 2 | V1 / V2 `pulse` | duty 12.5 / 25 / 50 / 75 % is chipsynth `pw` (1-99), so import is exact and export snaps to the nearest of four |
| Triangle | V3 `tri` | no volume on the NES triangle: export ignores note volume there |
| Noise | V4 `noise` | NES has 16 noise periods and two modes; chipsynth has `nmode` long/short, matching the mode bit; pitch maps to period |
| DPCM | D1 (DAC) | **chipsynth's `.dpcm` is the NES delta format and uses the NES rate table** (`synth.DPCM_RATES`, index 0-15), so samples round trip as bytes; FamiStudio's `Data` is the same 1-bit deltas |
| Expansion chips (VRC6, VRC7, FDS, MMC5, N163, S5B, EPSM) | none | import: skip the channels and report; later maybe VRC6 saw / FDS and N163 waves to chipsynth `saw` / a wavetable if the engine gets one |
| volume / pitch / arpeggio / duty envelopes (per frame) | `a d s r vol`, `arp`, `vib`, `pws` | the hard part, see below |
| note effects (vibrato, slide, volume slide, pitch, cut, delay, speed) | `Fx` events, `slide`, `Tempo` | partial |
| tempo (frames per row, or FamiTracker BPM mode), PAL / NTSC | `Tempo` events, 96 ticks per quarter | NTSC frame 60.0988 Hz, PAL 50.0070 Hz; rows -> ticks needs one rule for "row length" |
| patterns of N rows, custom pattern length, loop point | `Song` clips, repeats, `Loop` | import as unrolled events plus clips where the pattern is reused; the loop point maps to `Loop` |

### The envelope problem (decides the fidelity)

A FamiStudio volume envelope is a list of per-frame values with an optional loop and release point. chipsynth has an
ADSR with 4-bit rates. Three levels of ambition:

1. **Fit** (recommended for slice 1): classify each envelope as a shape (instant, linear decay, attack then hold, decay to sustain) and pick
   `a d s r`; arpeggio envelopes become `arp`/`arprate` when they are a short repeating cycle; duty envelopes become `pws` when they are a
   monotone sweep, else the first duty; pitch envelopes become `vib` when periodic, else `slide`. Anything that does not fit is
   approximated and reported in the conversion report. Exact for the common one-shot decays, wrong for hand-drawn ones.
2. **Bake**: render each distinct envelope as per-note fixed instruments (one chipsynth instrument per envelope, ticks per frame). Needs no engine
   change but explodes the instrument count and loses editability.
3. **Engine**: add table-driven instruments to `Instrument` (per-frame volume/pitch/duty lists with loop and release points) and to `synth.py`.
   Faithful both ways; touches the synth, `package.py` (new event/instrument tags, append-only), `songmml.py` and the composer. This is a project of its own and the right answer
   if NES / SID fidelity matters beyond "recognisable". Decision needed (section 7).

Export has the same problem in reverse and is easier: sample the ADSR (and `vib`, `arp`, `pws`) at the frame rate into FamiStudio
envelopes. That is exact up to the 60 Hz sampling.

### Other limits to report on export

- `saw` and `sine` voices do not exist on the NES: map to the nearest (`saw` -> pulse 12.5% or an expansion channel if the user opts in; `sine` -> triangle) and warn.
- More than one note at a time per voice cannot happen in chipsynth, so that direction is fine. FamiStudio's four-channel limit means `V1-V4` fit exactly; the second DAC (D2) is dropped.
- chipsynth's echo / reverb / filter / pan have no NES equivalent: dropped with a warning.

### Cost

| piece | work |
|---|---|
| text parser/writer (`chipsynth/formats/famistudio_text.py`) | small: the grammar is ~10 keywords |
| row/frame <-> tick mapping, PAL/NTSC | small, shared with GoatTracker |
| envelope fit (level 1) | medium: the part to test hardest |
| DPCM samples | small: bytes in, bytes out |
| CLI hooks | trivial after PR #1 |
| **Total** | **2 slices** (import; export), plus a third only if envelopes go to level 3 |

## 4. C64 SID editors

### Which are actively maintained (evidence, as of 2026-10)

| editor | license | latest seen | format | verdict |
|---|---|---|---|---|
| **SID Factory II** | unverified (open source) | build 20260314; "next release" notes pending | `.sf2`; imports GoatTracker, CheeseCutter, MOD | active; reach via GoatTracker import |
| **SID-Wizard** | unverified (source in the release) | 1.97, 2026-06-24 | `.swm` ("SWM1"), no spec found | maintained; author says feature-complete |
| **Furnace** | GPL-2+/3 | v0.6.8.3 | `.fur`, documented | very active; C64 SID among its many chips |
| **CheeseCutter** | GPL-2.0 | SDL2 port merged 2026-03 | `.ct`, no spec found | alive |
| **GoatTracker 2** | GPL-2+ | 2.77 (Debian, copyright to 2025); Stereo variant | `.sng` "GTS5" | stable; the format others import |

DefleMask (`.dmf`, closed source) is not a target: Furnace reads it, so Furnace covers it.

### What a SID can and cannot say, against chipsynth

| SID | chipsynth |
|---|---|
| 3 voices | 4 voices + 2 DAC: V4 and the DACs do not export to a single SID. Options: drop (warn), or two SIDs (GoatTracker Stereo, Furnace SID2) |
| pulse (12-bit width), saw, triangle, noise, combined waveforms, ring mod, hard sync | pulse, saw, tri, noise (+ sine, which the SID lacks): combined/ring/sync absent from `Instrument`; import maps them to the main waveform and reports |
| hardware ADSR (4-bit each) | `a d s r` is 0-15: **the best fit in this whole study**; the SID's nonlinear decay curve is approximated |
| one multimode filter shared by all voices (LP / BP / HP, 11-bit cutoff, resonance, per-voice routing) | per-instrument `filt`, `filt_cut`, `filt_res`, `fsweep` (per voice): approximate, because the real filter is global and rings across voices |
| 50 / 60 Hz frame driver | same frame-clock mapping as the NES |

### GoatTracker 2 `.sng` (from section 2's caveat: verify first)

Verified against GoatTracker 2.77 (r172, `gsong.c`, `gcommon.h`, `gplay.c`; GPL-2+, read only, nothing copied). No compression:
`GTS5` header (the loader also takes `GTS3` and `GTS4`); song name, author and copyright (32 bytes each); a byte with the number of subtunes;
per subtune and channel (3) a length byte L and then L+1 bytes of orderlist (the last is the restart position); a byte with the number of
instruments (highest used, up to 64), each 9 bytes (AD, SR, wave, pulse, filter and speed table pointers, vibrato delay, gate timer,
first-wave byte) and a 16-byte name; four tables (wave, pulse, filter, speed), each a length byte and then the left column and the right column
(255 rows at most); a byte with the number of patterns (up to 208), each a row count (up to 128) and rows of 4 bytes (note, instrument, command, data).
Orderlist bytes: `0x00-0xcf` pattern, `0xd0-0xdf` repeat count, `0xe0-0xef` transpose (`0xf0` = none), `0xff` loop to the next byte.
Pattern notes: `0x60-0xbc` notes, `0xbd` rest, `0xbe` key off, `0xbf` key on, `0xff` end of pattern.
The format does not record the target machine (PAL/NTSC) or multispeed: the importer needs an option for both.

What is hard: **the tables are programs**. A GoatTracker instrument is a pointer into wave / pulse / filter tables that step
once per frame with jumps and delays, and a pattern command can change tempo, vibrato, portamento, or table pointers mid-note. To import faithfully you must
*run the player* per note and then classify what happened (same classification as the NES envelopes, section 3). Common idioms recover well: wavetable
arpeggios (-> `arp`), pulse tables ramping the width (-> `pws`), filter tables (-> `fsweep`), a speed table (-> `vib`). The rest is approximated and reported.

Export is again the easier direction: write instruments with ADSR and a first waveform, a wavetable for `arp`, a pulsetable for `pws`,
a speedtable for `vib`; quantize note starts to frames (speed = frames per row from the tempo), 3 voices per SID.

Reaching SID Factory II: its importer reads `.sng`, so a good `.sng` exporter is also a path into SF2, and from SF2 into a playable `.sid`/PRG.
`.sid` files are a different thing (6502 player code + data); importing one would need a 6502 + SID emulator and register-trace to notes. Out of scope.

### Furnace `.fur`

Header: 32 bytes starting `-Furnace module-`, a 16-bit version (228 = 0.6.8.1; fields gated by `>=VER` / `<VER`), possibly zlib-compressed, then
tagged blocks (`INF2`/`INFO`, `INS2`, `PATN`/`PATR`, `SNG2`/`SONG`, `WAVE`, `SMP2`, `FLAG`, `ADI2`, `GROV`). SID chips are ids `0x07` (8580),
`0x47` (6581), `0xe2` (6581 + PCM), `0xf0` (SID2), `0xf5` (SID3); Furnace also supports the NES (its chip id is not in the part of the spec read here), so **one codec could serve both targets**. Costs: the version gating,
macro-driven instruments (the same "program" problem as above, but with named macros instead of tables, which is easier to read), and a moving target (a new format version about every release). Worth a spike once GoatTracker works,
because the classification code is reusable; not before.

### Cost

| piece | work |
|---|---|
| verify the `.sng` layout against GoatTracker's source | small |
| `.sng` reader/writer for songs, orderlists, patterns, instruments | medium |
| table "player" to classify instruments on import | medium-large: the risk item |
| frame quantizer, tempo/speed, PAL/NTSC option | small (shared) |
| 3-voice limit / stereo | small |
| **Total** | **3 slices** (reader + patterns; table classifier; writer), Furnace +3 |

## 5. Design

```
chipsynth/formats/
    __init__.py          registry: format name, suffixes, can_import, can_export
    report.py            ConversionReport(dropped=[...], approximated=[...], notes=[...]); never silent
    frames.py            tick <-> frame mapping for PAL/NTSC and speed/rows; quantize and report the error in ticks
    envelope.py          classify per-frame lists -> a d s r / arp / vib / pws, with a fit error
    famistudio_text.py   load(text) -> (Song, Report); dump(tune, options) -> (text, Report)
    goattracker.py       load(bytes) -> (Song, Report); dump(tune, options) -> (bytes, Report)
```

- Importers return a `Song` (the model that keeps clips and calls), as `midi.from_midi` does; exporters take a compiled `Tune` (as `midi.to_midi` does).
- CLI (on top of PR #1): `import PKG FILE.txt|FILE.sng` picks the importer by suffix or `--from famistudio|goattracker`; `export-song -f famistudio|goattracker`;
  `list --formats` shows what is registered. Options: `--region ntsc|pal`, `--multispeed N`, `--fit-tolerance`, `--strict` (turn any dropped item into an error).
- Every conversion prints its report; `--report FILE` writes it as JSON. A conversion that changed the sound must say so.
- No new runtime dependency (hex, struct and zlib are standard library). The boundary test (`tests/test_boundary.py`) stays green.
- If section 3's level 3 is chosen, the order is: engine tables first (own PR with `package.py` tags appended), then the codecs.

## 6. Tests

- **Round trips we control:** `Song -> FamiStudio text -> Song` and `Song -> .sng -> Song` for songs built to stay inside the target's limits
  (3-4 voices, in-range volumes, frame-aligned notes). Assert note-for-note equality plus an audio comparison (render both, bounded RMS difference) for envelopes.
- **Fixtures written by hand from the format**, small and checked in: one file per feature (duty change, DPCM, vibrato, tempo change, loop point, a wavetable arpeggio, a pulse ramp).
- **Real files from the tools** are only used if the user generates them with their own copy and chooses to commit them: song files carry their authors' copyright.
  HVSC and community `.sng` files must not be committed.
- **Failure cases:** truncated files, a newer version, an unknown keyword, a 5th channel, a sample over the DPCM cap: each gives an error or a report line, never a crash.
- **Check against the real tool:** the user opens the exported file in FamiStudio / GoatTracker and plays it. This cannot run in CI.

## 7. Decisions (recorded 2026-10-26)

1. **Order.** FamiStudio text first. GoatTracker second; a Furnace spike is optional.
2. **Fidelity.** Table-driven instruments are simulated with short per-frame MML notes (no engine change). Plain decays are fitted and reported.
   Native table-driven instruments stay on the backlog.
3. **Over-limit exports.** Drop and warn; `--strict` refuses instead.
4. **Versions.** Latest GA of each format: FamiStudio 4.5.3 for writing, GoatTracker 2.77 (to verify against Silver Fork 2.76 V1.1 before pinning).
5. **Expansion audio.** `--expansion vrc6|fds|n163|mmc5|none`. Channels are allocated by waveform: VRC6 first, FDS and N163 second; VRC7 only
   approximately. Chip details are to be verified before building.
6. **Import with more than four voices.** Every source channel is kept as a named part. V1-V4 and D1-D2 are built from the most important parts
   (ranked, gaps filled), with a report and a `--voices` override.
7. **Licenses.** SF2 and SID-Wizard licenses are unknown: ask the authors before depending on either. CheeseCutter is GPL-2.0 only: no code is copied.

Related design notes (not part of this work): an application-defined voice/DAC count, and the vertically scrollable track list (composer Tracks view).

## 8. Sources

- FamiStudio: https://github.com/BleuBleu/FamiStudio (MIT; `FamiStudio/Source/IO/`), `FamistudioTextFile.cs`, `ProjectFile.cs`, releases page
- Furnace: https://github.com/tildearrow/furnace (README; `papers/format.md`; releases)
- CheeseCutter: https://github.com/theyamo/CheeseCutter (GPL-2.0); `ct2util(1)` man page on manpages.debian.org
- SID Factory II: https://github.com/Chordian/sidfactory2 and https://blog.chordian.net/sf2/
- SID-Wizard: https://sourceforge.net/projects/sid-wizard/ ; CSDb release pages for 1.93-1.97 (csdb.dk), theoasisbbs.com coverage of 1.93 and 1.95
- GoatTracker: Debian package `goattracker` 2.77+ds-1 and its copyright file; ChiptuneSAK's `goat_tracker` module (published constants for `GTS5`)

## 9. Status

**FamiStudio text import: built (slice 1).** `chipsynth/formats/famistudio_text.py`, with `envelope.py` (the fits) and `report.py`;
`python -m chipsynth import PKG.csp FILE.txt`. The grammar, the note and channel names, the noise period rule and the envelope stepping were
read from FamiStudio's own source (`FamistudioTextFile.cs`, `ChannelState*.cs`, MIT) on the author's machine, not from memory.

Carried over: the five NES channels (Square 1 and 2 -> V1, V2; Triangle -> V3; Noise -> V4; DPCM -> D1), notes and stops, note volume,
instruments (volume envelope fitted to `a d s r vol`, duty to `pw` or a `pws` sweep, a repeating arpeggio to `arp` / `arprate`, noise mode),
DPCM samples with their rate, tempo (FamiStudio and FamiTracker tempo modes, NTSC and PAL), custom pattern lengths, the loop point, several songs per file.
A row is `96 / BeatLength` ticks, so rows land on clean lengths, and the song's bpm is set from the row length on the NES.
A bar is a pattern when the pattern is a whole number of beats, so the loop point lands on a bar.

Reported, not carried over: expansion chips, pitch envelopes, slides, vibrato, speed changes, note delay and cut delay, fine pitch, grooves (played
at their average), per-pattern tempo settings, looping DPCM, volume changes in the middle of a note, a volume envelope that is not an attack, decay and sustain
(the closest fit is used and its error is printed), arpeggios that are not a repeating cycle. The noise channel keeps its order of pitches, not its true
shift rate: the top periods are above what chipsynth can play.

**FamiStudio text export: built** (PR #19). `chipsynth/formats/famistudio_export.py`, with the reader as its check (what is written must read back; `tests/test_famistudio_export.py`).
`python -m chipsynth export-song SOURCE[:SONG] -o OUT.txt [--region ntsc|pal] [--fs-version X.Y.Z]` (default 4.5.3, NTSC), and the composer's File > Export > FamiStudio text.
V1, V2 -> Square 1, 2; V3 -> Triangle; V4 -> Noise; D1 -> DPCM. The volume ADSR and the arpeggio are sampled at the frame rate into FamiStudio envelopes. The file says it was
written by FamiStudio `--fs-version`, because FamiStudio only reads files whose version starts like its own. Dropped, and said so in the Report: more than four voices and any DAC
beyond D1, echo, reverb, the filter, vibrato, slide, pan, pulse-width sweeps and tempo changes. **Not verified:** the files have been read back by chipsynth's own importer only,
not opened in a real FamiStudio (a test set of generated songs, NTSC and PAL, was made for the author to check by eye and ear).

**Composer File menu: built** (PR #21). `chipsynth/composer/filemenu.py`: New (blank or from the generator), Open, Save, Save As, Import (MML, MIDI, FamiStudio text, GoatTracker) and
Export (WAV, MIDI, MML, FamiStudio text, package copy), the same writers and importers as the command line, with a "Last report..." entry and the unsaved-edits dialog
(Save, Don't save, Cancel) before anything replaces the open song.

Not built: simulating table-driven instruments with short per-frame notes (decision 2 above; the fit and the report come first);
reading a version check (the importer reads any 4.x file and ignores `Version`, where FamiStudio itself refuses other versions);
the FamiStudio binary `.fms` (import only, if at all); expansion chips (VRC6, VRC7, FDS, MMC5, N163, S5B, EPSM; the importer skips their channels and reports them).

**GoatTracker `.sng` import: slices 1, 2 and 3 built.** `chipsynth/formats/goattracker.py`: the reader (all of the layout above, truncation and bad files are
errors), the three voices as V1-V3, notes, rests, key-offs, order list repeat and transpose, the restart position as the loop point, tempo changes,
and per instrument the ADSR, the first waveform and the starting pulse width. A frame is 4 ticks (4/N with multispeed N), so timing is exact at any speed.

Slice 2 (command line and table classification): `python -m chipsynth import PKG.csp FILE.sng [--region pal|ntsc] [--multispeed 1|2|4] [--song N] [--report FILE]`
(found by the `.sng` ending or by `GTS3/4/5` at the start; the song id is the file name through `Package.safe_id`, with `_N` added when a file has several
subtunes). `--region` defaults to pal because the file does not say. The player's tables were run as `gplay.c` (r172, GPL-2+, read only, nothing copied) steps them:

| idiom | becomes | what is checked |
|---|---|---|
| wavetable that loops from its first row over relative notes (`0x00-0x7f`; `0x80` = no change) | `arp`, `arprate` | each row is a frame, a delay row (`0x00-0x0f`) takes effect after its delay, jumps are free; run per frame, fitted by `fit_arpeggio` (8 steps at most, up to 32 a beat) |
| pulse table: set rows (`0x8n`) and timed ramps (frames, signed speed) that rise and fall | `pws_from`, `pws_to`, `pws_rate` | run per frame to its repeat; a rise and a fall is a sweep, a ramp that jumps back or ends is played as a back-and-forth sweep (reported as approximate); only on a pulse wave |
| speed table entry as vibrato, from pattern command 4 or the instrument's own pointer (which only works with a vibrato delay of 1 or more) | `vib_rate`, `vib_depth` on an instrument variant `NAME_vibR_D` | the swing is run per frame (the player's turn-around counter), its period gives the rate and its size against the note's SID frequency gives the depth, so a fixed entry is deeper on a low note; entries with the high bit set are relative to the note |

Vibrato is a triangle swing in the player and a sine in chipsynth, so it is always listed as approximate.

**Slice 3 (branch `gt-commands`): the pattern commands, the rest of the wavetable and pulse idioms, and the filter.** A note that a command or
table changes while it sounds is played as a run of chipsynth notes (one instrument per note), so each part strikes its envelope again; the Report says so.

| command or table | becomes | approximate because |
|---|---|---|
| 1xy / 2xy portamento (speed = 16-bit speed table entry, left column high) | a one-frame note, then notes that glide (`Tweak slide`, at most 192/48 of a beat each) to the semitone the slide reaches; a later command, or 0, stops it and the pitch stays | a linear frequency slide is played as an exponential glide ending on a rounded semitone; the slide is taken to last until the next note or command |
| 3xy tone portamento | the new note with a slide as long as the player needs to cover the distance from the voice's last pitch; 300 (tie note) is a plain new note | the held note is struck again; a slide over 192/48 of a beat arrives early |
| 4xy vibrato on a row after the note | the note is split there, the second part with the vibrato variant (or none for 400) | struck again |
| 5xy / 6xy set AD / SR | the instrument variant `NAME_adXY` / `NAME_srXY` for that note; the next note is the instrument again | a change on a row after the note is reported (it would change a sounding envelope) |
| 7xy set waveform | a variant with that wave; ignored, as GoatTracker's own text says, while a wavetable is still running; on a later row it splits the note | combined waves become one wave; a silent or gate-off waveform ends the note |
| 8xy / 9xy set wavetable / pulse pointer | the other table plays from that note (8xy also on a later row, splitting the note); 0 stops the table, and a stopped arpeggio or sweep is switched off in the variant | |
| Axy, Bxy, Cxy and instrument filter tables | the filter is played for the whole song a frame at a time (mode, resonance, voice mask, cutoff); each note gets `filter`, and `fsweep` if the cutoff moves while it sounds | the SID filter is shared and chipsynth's is per note, so a note carries the filter as it is when it starts; the cutoff curve is chipsynth's; combined modes pick one; a sweep is a back-and-forth swing at the speed of its first rise or fall; a filter that changes mode or routing mid-note, or is switched on mid-note, is not followed |
| Dxy master volume | a `Volume` event on all three voices | takes effect on the next note; D with a high digit (the timing mark) is ignored |
| Exy funk tempo, F00 / F01 | row lengths alternate between the entry's two values on all channels; a tempo command ends it; F00 / F01 resume it (the loop point is found on the funk timeline) | what 00 and 01 recall is read from the command's description, not from the player |
| wavetable with absolute notes (`0x81` and up), a pass before the loop, no loop, or waveform changes with no arpeggio | the table is run frame by frame for each note: a run of short notes (a noise hit, then the tone), then the endless tail as `arp` / `arprate` or a held note | arpeggio steps are limited to two octaves; a repeat that changes waveform plays its first step |
| pulse table that changes the width before setting one | runs from 50% | the width the player had then is not in the file |
| pulse table that zigzags | a cycle that is one sweep repeated is that sweep, exactly; an uneven zigzag (up to 8 peaks) is an even sweep between its lowest and highest width | uneven zigzags only |

Still reported, not carried over: wavetables that run commands (rows `0xf0` and up; they are skipped for pointer commands and the instrument is left plain); pulse
tables that drift or zigzag more than 8 times; a 5xy / 6xy or a tone portamento on a row after the note; a portamento on a note whose wavetable plays an arpeggio; a speed table entry
that is missing (for portamento, funk tempo) or has speed 0 or is under 1/32 of a semitone (vibrato); vibrato delays (vibrato starts at once); a filter that is switched on part-way through a note.

Fixed in slice 2: the song's bpm was multiplied by the multispeed, which played a multispeed song at N times its speed; it follows the video rate only (125 pal, 150 ntsc).
Instrument names are made into identifiers (anything but letters, digits and `_` becomes `_`).
Not built: a writer (GoatTracker `.sng` export); SID-Wizard (`.swm`), SID Factory II (`.sf2`) and Furnace (`.fur`) importers. Not verified: whether a tempo command takes effect on its own row or the next one (the player reads rows ahead of the gate timer);
the tables were checked against the player's source and against hand-built files, not against real songs or by ear.
Slice 3 was written without the player source in reach (`gplay.c` could not be fetched from the environment it was built in): the command descriptions come from GoatTracker's published command list,
the table and frame behaviour from slice 2's reading of the player, and the rest from memory. **To check against `gplay.c`:** that a command (portamento, vibrato) stays in force until a later command or note
(taken here: until the next note or a later row with a command); the speed table's 16-bit speed (left column as the high byte); the filter table row layout (`0x80`+type bits and resonance / voice mask, `0x00` set cutoff, `0x01`-`0x7f`
time and signed speed); that a relative wavetable note wraps in 7 bits (`(note + right) & 0x7f`, so `0x7f` is one below); that a waveform row with the gate bit off or `0xe0`-`0xef` is silence or a release;
what F00 / F01 recall; that one-shot commands (5xy - Fxy) leave the instrument's vibrato alone; the cutoff to Hz curve (30 Hz + 46 Hz a step); and that the pulse width before a table sets one is not kept from the note before.
Filter and master volume are heard as chipsynth's per-note filter and per-note volume, not as the chip's.
