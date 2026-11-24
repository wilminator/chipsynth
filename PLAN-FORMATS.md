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
| CheeseCutter (`.ct`) | Skip for now | no format spec found; reachable through SID Factory II |
| **SID-Wizard** (`.swm`) | **Import and export built** (sections 4 and 9) | the spec ships in the release (`SWM-spec.src`) and the source is WTF-licensed; the player source settles the rest |

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
- SID Factory II's license (no LICENSE file seen on the repository page; one article says "open source"), and any `.ct` / `.sf2` layout. (SID-Wizard: found later, section 4.)
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
| Expansion chips (VRC6, VRC7, FDS, MMC5, N163, S5B, EPSM) | V5 and up (import) | import: VRC6 pulses and saw, MMC5 pulses, S5B / EPSM squares and the FDS / N163 wavetables (to the nearest chipsynth wave, reported approximate) take extra voices; VRC7, EPSM FM and rhythm and the MMC5 PCM channel are skipped and reported (section 9) |
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
| **SID-Wizard** | WTF (source README, verified 2026-11-22) | 1.97, 2026-06-24 | `.swm` ("SWM1"), spec in the source (`SWM-spec.src`) | maintained; author says feature-complete; **import built** |
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

### SID-Wizard `.swm` ("SWM1")

Read 2026-11-22. **Licences:** SID-Wizard 1.97's own README (the `anarkiwi/sid-wizard` repository carries the 1.97 sources under `native/`, plus a Vessel MIDI port) says: "let's declare SID-Wizard as a WTF-licensed tool, so you can do WTF you want with it" (Hermit Software). `pysidwizard` 0.7.0
(`anarkiwi/pysidwizard`, Josh Bailey), a separate from-first-principles reader / writer / player, is Apache-2.0. Both were read only; nothing was copied.

**What was read:** `native/sources/SWM-spec.src` (Hermit's own description of the file: header offsets, instrument layout, note and effect values),
`SWMconvert.c` (`ProcessSWMver1` is a reader written by the format's author; `GenerateMIDI` shows how rows, the orderlist, transpose and tempo play),
`include/player.asm` (the 6502 player: the sequencer, packed empty rows, the tempo counter, the wave/arpeggio and pulse tables), and from `pysidwizard`
`docs/format.md`, `constants.py`, `reader.py`, `model.py` and the grammar. Where two of them describe the same thing they agree (header, the back-to-front layout,
pattern packing, orderlist commands, instrument layout, note numbering); the places only the player settles are noted below.

**Layout** (all bytes; a `.swm` is a C64 PRG, so two load-address bytes come first; a `.P00` has a 26-byte `C64File` header before those; a bare module is also read):

| part | content |
|---|---|
| header, 64 bytes | `SWM1`; 4 frame speed (1-8); 11 default pattern length; 12 sequence count; 13 pattern count; 14 instrument count; 15 chord table size; 16 tempo table size; 19 driver; 20 tuning (0 = 440 Hz, 1 = 432 Hz, 2 = just intonation for C); 24-63 author text; the rest is obsolete editor settings |
| sequences | one orderlist per channel, three per subtune; each is its bytes then one size byte |
| patterns | each is its packed rows, then a size byte (unpacked bytes + 1 for the `$FF` end) and a row-count byte |
| instruments | each is its data (without the last `$FF`), then a size byte, then an 8-character name |
| chord table, tempo table | as many bytes as the header says |
| subtune tempos | two bytes (left, right) per subtune: the last bytes of the file |

So the file is read from its end. Orderlist bytes: `$00-$7F` pattern (1-based), `$80-$9F` transpose (byte - `$90`), `$A0-$AF` main volume, `$B0-$EF` this channel's tempo (byte - `$B0`),
`$F0-$FD` no-op, `$FE` end, `$FF` then a position (below `$80`: loop to that byte of this orderlist; `$80` and up: jump to another subtune). Pattern rows: a note byte, whose
bit 7 says an instrument byte follows, whose bit 7 says an effect byte follows, which takes a value byte when it is below `$20`; notes `$01-$5F` (1 is C-0), `$7E` gate off, `$60-$6F` vibrato amplitude,
`$78-$7F` other note effects; a byte `$70-$77` after a `$00` stands for 2-9 more `$00` rows. Instruments: 16 header bytes (3 attack/decay, 4 sustain/release, 5 vibrato,
7 arpeggio speed, 8 default chord, 9 octave shift, 10 and 11 the pulse and filter table offsets), then the wave/arpeggio, pulse and filter tables of 3-byte rows, each ending in `$FF`.
A tempo is the number of player calls a row lasts; a tempo list repeats (a funk tempo is two entries). The player is called `frame speed` times per video frame.

**How it was checked:** Hermit's own converter (`SWMconvert.c`, compiled locally, not committed) wrote SWM files from a MIDI file and the importer read them back to the same notes; its
SWM-to-MIDI export was then compared note by note (start tick and pitch, all three channels) with the importer's output for hand-built files with transposes, an orderlist tempo, a track-tempo
pattern effect, a subtune tempo, an instrument octave shift and packed gaps: identical. 6000 randomly damaged files gave errors, never a crash or a hang. **Not checked:** a real song saved by the editor
(none was to hand), the result by ear, and everything the notes in section 9 list.

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
7. **Licenses.** The SF2 license is unknown: ask the author before depending on it. SID-Wizard's own README declares it WTF-licensed and `pysidwizard` is Apache-2.0 (both read only, section 4). CheeseCutter is GPL-2.0 only: no code is copied.

Related design notes (not part of this work): an application-defined voice/DAC count, and the vertically scrollable track list (composer Tracks view).

## 8. Sources

- FamiStudio: https://github.com/BleuBleu/FamiStudio (MIT; `FamiStudio/Source/IO/`), `FamistudioTextFile.cs`, `ProjectFile.cs`, releases page
- Furnace: https://github.com/tildearrow/furnace (README; `papers/format.md`; releases)
- CheeseCutter: https://github.com/theyamo/CheeseCutter (GPL-2.0); `ct2util(1)` man page on manpages.debian.org
- SID Factory II: https://github.com/Chordian/sidfactory2 and https://blog.chordian.net/sf2/
- SID-Wizard: https://sourceforge.net/projects/sid-wizard/ ; CSDb release pages for 1.93-1.97 (csdb.dk), theoasisbbs.com coverage of 1.93 and 1.95; the 1.97 sources (`native/sources/SWM-spec.src`, `SWMconvert.c`, `include/player.asm`, README) in https://github.com/anarkiwi/sid-wizard ; https://github.com/anarkiwi/pysidwizard (Apache-2.0; `docs/format.md`, `src/pysidwizard/`)
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
`python -m chipsynth export-song SOURCE[:SONG] -o OUT.txt [--region ntsc|pal]` (NTSC by default), and the composer's File > Export > FamiStudio text.
V1, V2 -> Square 1, 2; V3 -> Triangle; V4 -> Noise; D1 -> DPCM. The volume ADSR and the arpeggio are sampled at the frame rate into FamiStudio envelopes.
**Version policy (decided 2026-11-22):** no `Version` attribute is written, ever. FamiStudio's text format only loads in the release that wrote it, and the versions keep marching; newer
FamiStudio source skips the check when `Version` is absent, and the author confirmed that his upgraded FamiStudio opens such files (round-3 test set, 16 songs, NTSC and PAL). A release that still
demands a version (4.2.0 refused both a 4.5.3 and a 4.2.0 stamp) needs upgrading: the report says so. The earlier `--fs-version`, `--fs-reference`, the composer's Version row and its settings
(`fs_stamp`, `fs_typed`, `fs_ref`) were removed; the code is kept in `legacy/famistudio_version.py`. The importer reads files with or without a Version. **Not verified** against a real FamiStudio 4.2.0. Dropped, and said so in the Report: more than four voices and any DAC
beyond D1, echo, reverb, the filter, vibrato, slide, pan, pulse-width sweeps and tempo changes. **Not verified:** the files have been read back by chipsynth's own importer only,
not opened in a real FamiStudio (a test set of generated songs, NTSC and PAL, was made for the author to check by eye and ear).

**FamiTracker text export: built** (branch `famitracker-export`). `chipsynth/formats/famitracker_export.py`; `python -m chipsynth export-song SOURCE[:SONG] -o OUT.ftm.txt` (or `-f famitracker`; a
plain `.txt` stays FamiStudio's) `[--region ntsc|pal]`, and the composer's File > Export > FamiTracker text. Why: FamiStudio's own text only loads in the release that wrote it, and FamiTracker's
`# FamiTracker text export 0.4.2` (read by FamiTracker and by FamiStudio's File > Import > FamiTracker Text, which accepts exactly that version) has been stable. Written from the format, with FamiStudio's
reader (`FamitrackerTextFile.cs`, `FamitrackerBaseFile.cs`, MIT) read to be sure every line is one it accepts. V1, V2 -> Pulse 1, 2; V3 -> Triangle; V4 -> Noise; D1 -> DPCM (`DPCMDEF` / `DPCM`, `KEYDPCM`).
`INST2A03` instruments with `MACRO` sequences: volume (the ADSR sampled once a frame, looping on its sustain), duty (the pulse width snapped to the NES's four, a pulse-width sweep sampled a frame at a time,
the noise mode), arpeggio (semitone offsets a frame at a time, looping); instruments that sound alike are one. Note volume is written on every note. Timing: a row is a whole number of ticks; `speed` and `tempo`
are chosen so FamiTracker's tempo counter lands the row length (150 / tempo frames a tick on NTSC, 125 / tempo on PAL) within a tenth of a percent where the range allows, preferring a tempo near the default;
patterns are whole bars (16-64 rows where possible, so the loop point lands on a pattern's first row), identical patterns are shared, and the loop is `Bxx` on the song's last row (`C00` for a `once` song).
Left out, and said in the Report: voices past V4, DACs past D1, echo, reverb, detune, the filter, vibrato, slide, pan, tempo changes, the release tail (notes are cut where they end), the volume envelope on the triangle
(no volume on the NES), the arpeggio on noise; over the limits (128 patterns, 64 instruments, 36 samples) the end or the extras go. The check is a reader of our own in `tests/test_famitracker_export.py`
that is as strict as FamiStudio's about where things sit on a line, plays the file with FamiStudio's copy of the tempo counter, and compares every note's frame and pitch.
**Not verified:** the files were not opened in FamiTracker or FamiStudio (neither can run here); `KEYDPCM`'s trailing fields, the sense of a macro's release field (none is written) and the noise note letters were taken
from FamiStudio's writer and reader, not from FamiTracker's own source.

**Composer File menu: built** (PR #21). `chipsynth/composer/filemenu.py`: New (blank or from the generator), Open, Save, Save As, Import (MML, MIDI, FamiStudio text, GoatTracker) and
Export (WAV, MIDI, MML, FamiStudio text, package copy), the same writers and importers as the command line, with a "Last report..." entry and the unsaved-edits dialog
(Save, Don't save, Cancel) before anything replaces the open song.

Not built: VRC7, EPSM FM and rhythm (see the fidelity paragraph at the end of this section).

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
Not built: a writer (GoatTracker `.sng` export); the Furnace (`.fur`) importer (SID-Wizard: below). Not verified: whether a tempo command takes effect on its own row or the next one (the player reads rows ahead of the gate timer);
the tables were checked against the player's source and against hand-built files, not against real songs or by ear.

**SID Factory II `.sf2` import: slice 1 built.** `chipsynth/formats/sf2.py`; `python -m chipsynth import PKG.csp FILE.sf2 [--song N] [--region pal|ntsc] [--report FILE]`
(found by the `.sf2` ending or by the word 0x1337 after the load address) and File > Import > SID Factory II (also by dropping a `.sf2`).
*License and what was read:* the repository (github.com/Chordian/sidfactory2, checked out 2026-11-22) ships `SIDFactoryII/COPYING`, the GPL version 2 text, and its sources name
no version, so by section 9 of that licence any published version applies (this resolves the "unverified" in section 4 and decision 7 for reading it). Read only, nothing copied:
`driver/driver_info.h,.cpp` (the descriptor blocks), `datasources/datasource_sequence.cpp` and `datasource_orderlist.cpp` (the byte formats), `auxilarydata/*` (song list, PAL / NTSC, table row text),
`components/component_track.cpp` (note names), `converters/` (`sf2_interface.h`, `gt/source_sng.cpp`, `cc/source_ct.cpp`, `jch/converter_jch.cpp`: how other formats map onto the tables, which fixes the table semantics),
and the driver notes and the user manual in `dist/documentation/`. **No stand-alone file-format document was found in the repository**; the layout below is from the editor's source. The driver binaries have no source, so the 6502 code was not read.
*Layout:* a PRG (load address, then memory). At the load address the word 0x1337, then blocks of (id, size, data): 1 descriptor (driver name in screen codes, version), 2 driver common (not needed), 3 tables
(type, id, name, layout 0 row / 1 column major, address, columns, rows), 4 instrument cell names, 5 music data (track count, order list and sequence pointer tables and sizes), up to 0xff. Tables are found **by name**
(`Commands`, `Instruments`, `Wave`, `Pulse`, `Filter`, `Arp`, `Tempo`, `HR`, `Init`: ids differ between drivers). A word at 0x0ffb points at the editor's blocks (type, version, size): 2 hardware (byte 1: 0 PAL, 1 NTSC),
4 table row text (instrument names), 5 song list (count and names). Songs share sequences and tables; each has its own order lists, at `base + song * tracks * size`.
Order list: values below 0x80 are sequences, 0x80-0xfd set the transpose (value - 0xa0 semitones), 0xfe ends, 0xff loops to the byte position that follows. Sequence: [0xc0+command] [0xa0+instrument] [0x80+duration, +0x10 tied] note, ending in 0x7f;
note 0 is a gate-off, 0x7e keeps the gate on, 1-0x6f are notes (C-0 = 0, so 0x30 is C-4); a duration is the number of extra rows the event lasts and holds until changed. A row is `tempo + 1` frames;
the Tempo table is a chain that ends at 0x7f and wraps to its first row, the song's Init row names where it starts.
*Carried:* three tracks to V1-V3, notes, gate-offs, transpose, the loop (found as the first repeat of the whole player state, so tracks of different lengths loop together), stop markers, the tempo chain and tempo command (driver 11),
several songs (as in the editor's list), PAL / NTSC from the file, instrument names, and per instrument the ADSR, the waveform (the one played longest), a fixed or shifted pitch from the wave table, a cycling wave table as `arp`,
a pulse table's start width and sweep (the same classification as goattracker.py), and the commands local / persistent ADSR, wave pointer, pulse pointer and arpeggio (chord table). Drivers 12, 13, 15, 16 read their own instrument bytes (waveform, pulse width, wave table); other drivers keep the notes and timing only.
*Reported, not carried:* slide, vibrato, portamento, fret slide, main volume, filter programs and the filter table, hard restart (a sound detail), ring / sync bits, tied notes (played as new notes), the Hubbard driver's extras, a linear pulse sweep (drivers 15-16),
tracks 4-12 of the multi-SID drivers, instrument variants a command asks for beyond the five listed, any other driver (for example NP20).
*Verified:* the container, the order list and sequence formats, the tempo chain, the Init and Arp tables and the pulse and wave row layouts are from the editor's source, and the importer loads all 67 `.sf2` files in the repository's `music` folder
(read locally, none copied) with every song's loop found and each song compiling to a chipsynth tune, plus 1500 randomly damaged copies without anything but an `Sf2Error`. Tests build their files by hand (`tests/test_sf2_import.py`).
*Not verified* (from the converters and notes, not from the driver code or by ear): that a wave table row lasts one frame and that a jump costs none; that a pulse add row adds its value every frame; the arpeggio command's step length
(taken as speed + 1 frames, and the report says so); that a tempo command takes effect on the next row and for all tracks; what a tied note and a bare 0x7e after a gate-off do to the envelope (a bare 0x7e plays the last pitch again);
the loop target when an order list loops to a sequence byte without a transpose byte in front (the transpose in force at the end of the list is kept); notes above the 96-note table that the drivers use as noise pitches (clamped to 107);
the pulse width byte of drivers 12-16 ("XY: X = middle 4 bits, Y = top 4 bits" in the notes, read as the byte's high nibble = middle, low nibble = top). No song was compared against the real player or by ear. Not built: an `.sf2` writer.
**FamiStudio `.fms` project import: built.** `chipsynth/formats/famistudio_binary.py` reads the binary project into the text importer's own `Project` model and
`famistudio_text.load_project` converts it, so the mapping, the envelope fits and the Report are the same code. Written from `ProjectFile.cs`, `Compression.cs`,
`ProjectBuffer.cs` and the `Serialize` methods of `Project`, `Song`, `Channel`, `Pattern`, `Note`, `Instrument`, `Envelope`, `Arpeggio` and `DPCMSample` (MIT, read only, on the
author's machine from the current master, project format version 19). The header is `FMS!` (u32 `0x21534D46`), an i32 format version, an i32 payload size and a raw deflate
stream (.NET `DeflateStream`, no zlib header); the payload is little endian, strings are UTF-16 with an i32 byte count, references are ids.
Versions read: 15 to 19 (FamiStudio 4.1 to 4.5), every branch of those read from the source; older (before 4.1) and newer files are refused with the release and the way out (export FamiStudio
text). `python -m chipsynth import PKG.csp FILE.fms` (by the `.fms` ending or by `FMS!`, with `--song`, `--region`, `--report`), and the composer's File > Import > FamiStudio project, drop and import dialog.
Same as the text importer: the five NES channels, instruments, DPCM, tempo, loop point; expansion chips, pitch envelopes, effects and the rest are reported. Differences from text: the project export
settings, folders, mixer settings and colours are read past and dropped; a DPCM sample whose source is a WAV, or DMC data with volume, rate, pitch, trim or envelope changes, is left out and
reported (FamiStudio makes the final bytes with its own encoder when it opens the project; only the plain case is rebuilt, with its bit reverse and padding).
Also built: the text importer checks `Version`. FamiStudio only opens text whose version starts with its own first three characters (4.5 now); chipsynth still reads any file but
reports a version that is not 4.x as approximate and any other 4.x as a note that FamiStudio 4.5 would refuse it.
Not verified: no real `.fms` file was read (none may be committed and the program cannot run here): the tests use files built by a writer made from the same reading of the source, so they check that
reader and writer agree, not that a real project reads. Versions 15 to 18 differ from 19 only in gated fields, checked the same way.
**SID-Wizard `.swm` import: built.** `chipsynth/formats/sidwizard.py` (layout and sources in section 4): the reader (the PRG, bare and `.P00` forms; truncation, bad table pointers and counts that do not add up are errors), the three
SID voices as V1-V3, notes, rests and gate-offs (`$7E`), the orderlists (patterns, transpose, a channel's tempo, the loop position), the subtune tempo (single or funk) and the tempo pattern effects `$10`, `$11`, `$13`, `$14`,
the frame speed from the header (a player call is 4/N ticks; exact for N = 1, 2, 4), several subtunes, and per instrument the ADSR, the first waveform, the starting pulse width, the octave shift, a wave table that loops over
relative notes (-> `arp`, `arprate`, fitted as for GoatTracker) and a pulse table that sweeps (-> `pws_from`, `pws_to`, `pws_rate`, only on a pulse wave). The channels loop on their own in SID-Wizard but chipsynth has one loop point: the song
is unrolled to the latest loop target and the least common multiple of the channels' loop lengths (the longest one if that passes twenty minutes), and says so.
The command line (a `.swm` is found by its ending or by `SWM1` at the start; a single song is named for the file):

    python -m chipsynth import PKG.csp FILE.swm [--region pal|ntsc] [--song N] [--report FILE]

and the composer's File > Import > SID-Wizard (and dropping a `.swm`), which asks the region and the subtune. `--multispeed` is for GoatTracker only: a `.swm` records its own frame speed. The file records no PAL or NTSC either, so `--region` defaults to pal.
Reported, not carried over: instrument vibrato, filter tables, wave tables that play chords, absolute notes, detune, or rows once before settling, pulse tables that zigzag or change width before setting one, the tempo table's programs
(`$12`, `$15`) and every other pattern effect and small effect by name (slides, portamento, vibrato, set waveform, set sustain and so on), note-column vibrato, sync, ring, gate on, the orderlist's main volume, and a jump to another subtune;
legato is played as a new note (said as approximate); a tuning other than 440 Hz is played at 440 Hz. A song with no key-off is not made to stop: the last note holds, as in the player.
**Not verified:** no song saved by the real editor was read (the layout was checked against Hermit's own converter, section 4), nothing was compared with SID-Wizard by ear; that a tempo effect changes the row it is on (read from `player.asm`), how tempo programs are numbered (so effects `$12` and `$15` are reported, not played), that
a repeat row (`$01-$0F` in the wave table) lasts one call more than its number (read from the player), and the 2-call delay at the start of a note (not modelled). Table programs were classified from the player source and hand-built files, not from real songs.
Not built: the rest of the pattern commands and the filter tables; SID-Wizard (`.swm`), SID Factory II (`.sf2`) and Furnace (`.fur`) importers. Not verified: whether a tempo command takes effect on its own row or the next one (the player reads rows ahead of the gate timer);
the tables were checked against the player's source and against hand-built files, not against real songs or by ear.

**SID-Wizard `.swm` export: built** (branch `sidwizard-export`). `chipsynth/formats/sidwizard_export.py`, `dump(tune, region="pal", multispeed=1, name=None, tempo="exact") -> (bytes, Report)`, shaped like the GoatTracker writer
(`tests/test_sidwizard_export.py`, `tests/test_composer_sidwizard_export.py`). V1-V3 are the three SID voices; V4 and up, the DACs, echo, reverb, loudness, pan, slides, detune and the filter are left out and said. Notes, rests, gate-offs
(`$7E`), the tempo and the loop point are kept: the orderlists end in `FF position` (or `FE` for a `once` song, with a short silent last pattern, because the player eats the last row before an `FE`). Per instrument: ADSR (with the
player's hard restart), waveform, pulse width, a pulse sweep as a pulse table (up, down, jump back), an arpeggio as a wave table, and vibrato as the instrument's calculated-vibrato byte (the nearest amplitude and period the player's exponent table makes;
the fit and the difference from chipsynth's sine are reported). The tempo is a number of video frames a row (3 or more); `exact` writes the `$10` tempo effect on the rows that need it, `speed` one tempo. From the command line and the composer:

    python -m chipsynth export-song music/forest.csp:dawn -o dawn.swm --region pal
    python -m chipsynth import music/back.csp dawn.swm --region pal

File > Export > SID-Wizard asks PAL or NTSC, the multispeed and the tempo mode. Limits (all reported when hit): 100 patterns, 36 instruments, 124 orderlist entries, 248 bytes a pattern, notes C-0 to A#7 (others move by octaves).
**Verified** against the format author's own sources, read only and never copied (`anarkiwi/sid-wizard` 1.97 and `anarkiwi/pysidwizard` 0.7.0, cloned outside the repository): (1) the real 6502 player (the one in pysidwizard's `SWMPlayer`)
was run on written files for a battery of random songs and its register log compared with the exported notes: every gate-on lands on the row's frame plus the player's 2-frame start delay, at each tempo, loop and `once` ending; the frequency table
(`FREQ`) is the player's register for every note 0-94 to within 1 unit; the vibrato swing and the pulse sweeps match the model; (2) Hermit's `SWMconvert` (compiled locally, SWM to MIDI) reads the files and gives the same notes for songs at the native tempo
(it ignores the `$10` effect, so only those); (3) `pysidwizard.parse_swm` reads them and `build_swm` rebuilds the identical bytes. **Not verified:** that the SID-Wizard editor opens a written file (it is not available here);
that audio sounds right (no SID chip emulation render was compared, only register logs), the NTSC pitch table (the file has no region), and the drops (filter, volume, loudness).
**Finding (not fixed, the importer is unchanged):** a frame speed of N makes only the instrument tables (wave/arpeggio, and the pulse table when the instrument asks) run N times a video frame; the sequencer and vibrato run once. So `--multispeed` here
means faster tables at the same row lengths, and `export` keeps tempos in video frames. The importer (`formats/sidwizard.py`) takes the tempo as player calls (`4/N` ticks a call), which is only right for N = 1: a file at N = 2 or 4 is read at the wrong speed
(the exporter's tests therefore do not read back multispeed files). The importer also plays a loop round many times for some files written at a non-native tempo (the channels then seem to loop at different lengths in its simulation; the cause was not tracked down), and drops vibrato; such a round trip
shows the first pass correct and repeats after it, so the tests compare the first pass only.

**GoatTracker `.sng` export: built** (branch `gt-writer`). `chipsynth/formats/goattracker_export.py`, with the reader as its check (`tests/test_goattracker_export.py`):
`python -m chipsynth export-song SOURCE[:SONG] -o OUT.sng [--region pal|ntsc] [--multispeed 1|2|4] [--gt-tempo exact|speed]` (pal by default, as the importer), and the composer's File > Export > GoatTracker 2.
V1-V3 -> the three voices; notes, rests, key-offs, the loop point (a `once` song ends in a silent pattern that repeats, said in the Report), orders, patterns of a bar (shared when equal) and instruments with ADSR,
waveform and pulse width. Arpeggio -> a looping wavetable (a frame a row, so steps are whole frames), pulse sweep -> a pulse table that ramps up and down (at most 127 a frame, so a fast wide sweep is narrowed),
vibrato -> a relative speed table entry found by running the importer's own model (the player's cycles are whole frames, so only some rates and depths exist; the nearest is used and listed).
Timing: rows of the coarsest grid (at most a sixteenth) that every note, loop and tempo change lies on; a row is at least 3 frames; row lengths come from the tempo rounded cumulatively, and where they are not all equal the speed is
changed on the rows that differ (command F on voice 1), which is exact to half a frame; `--gt-tempo speed` writes one speed instead. At the native tempo (125 bpm PAL, 150 NTSC, any multispeed) notes and timing round trip tick for tick.
Left out and reported: more voices, DACs, echo, reverb, detune, differences in note and instrument volume, pan, slides, the filter, the short noise mode; a sine is a triangle; the song is cut, and says so, past 208 patterns or 253 order entries.
Two importer fixes came with it: the end of a song was found one frame late (the last note without a key-off was a frame too long), and an instrument's vibrato was lost on a row that had a tempo or other non-portamento command.
**Checked against the real player** (GoatTracker 2.77, its own `player.s` packed by `gt2reloc` and run under `py65`, every SID write traced; `chipsynth/tests/gt_oracle.py`, `test_goattracker_oracle.py`, skipped without the two tools; nothing of GoatTracker is copied): the demo songs and one-feature songs (vibrato, arpeggio, pulse sweep, ADSR, rests, tempo changes, loops, multispeed x1/x2/x4, PAL and NTSC) start every note on the right call (after a fixed pre-roll of 6 x multispeed + 1 calls), with the right pitch, waveform, AD/SR and loop period.
Bugs found and fixed: the first wavetable row was `0x80` (keep the frequency), so every note was silent (now `0x00`); the player's default optimisations skip one call a row in vibrato, portamento and the pulse table, so their rates are stretched by L / (L - 1) (exporter and importer allow for it); the gate timer is now 2 (1 when a row is shorter than six frames); arpeggio rates follow the tempo in force; a command (1xy, 2xy, 4xy) lasts for its own row only (the importer wrongly held it). Inherent, and reported: NTSC plays 0.65 semitone sharp, the release tail is cut by the hard restart, a pulse sweep is capped at 127 units a call, vibrato is quantised.
Still unverified: the cutoff to Hz curve (30 Hz + 46 Hz a step); playback by ear; the editor's own loader (the packer reads the same `.sng`).
Settled by the same check (the old "to check against `gplay.c`" list): a tempo command takes effect on its own row; wavetable right column `0x80` keeps and `0x00` is +0, `0x60`-`0x7f` go below the note, relative notes wrap in 7 bits; a waveform row with the gate off or `0xe0`-`0xef` releases; the 16-bit speed has the left column as the high byte; the filter row layout; F00 starts on the left tempo, F01 on the right; one-shot commands (5xy - Fxy) leave vibrato alone; the pulse width before a table sets one is the channel's previous width (not 50%).
Corrected: a command does NOT stay in force until the next note, it lasts for its own row (cmd 0 clears it).
Filter and master volume are heard as chipsynth's per-note filter and per-note volume, not as the chip's.
**FamiStudio import fidelity: expansion chips and table-driven instruments** (`fs-fidelity`). `famistudio_text.py` and `envelope.py`; tests in `tests/test_famistudio_fidelity.py`, built by hand.
Files with no expansion chip and no instrument that needs it import exactly as before (the earlier tests pass unchanged).

*Expansion chips.* A channel is imported only when its chip is in the project's `Expansions` (the channel names and envelope names are from `Channel.cs` / `Envelope.cs`, MIT, read only). Channels with notes take the voices after V1-V4, in this order:
VRC6 (Square1, Square2, Saw), MMC5 (Square1, Square2), FDS, N163 (Wave1-8), S5B (Square1-3), EPSM squares; a song has 16 voices at most, so 12 fit, and the rest are reported ("no voice left"). Each track starts with a comment naming its FamiStudio channel.
VRC6 pulses use the eight VRC6 duties (`(n+1)/16`), the VRC6 saw is `saw`, MMC5 pulses use the NES duties. S5B and EPSM squares are 50% pulses (their noise, hardware envelope and mixer are reported, not played).
FDS and N163 waveforms (the first wave of the table) become the nearest of sine, triangle, saw or a pulse of any width (`envelope.fit_wave`: every phase and the inverted wave are tried, the error is the RMS difference of the two shapes at equal RMS), and every such instrument is reported as approximate with its error;
FDS modulation, wave sequences and the VRC6 saw master volume are not played. Skipped and reported: VRC7, EPSM FM and rhythm, the MMC5 PCM channel.

*Table-driven instruments.* An instrument is played as one note with the fitted `a d s r` as before when everything fits. When it does not (decision 2), each note is played a frame at a time: a short note wherever the pitch, volume or duty changes, on a flat instrument (`NAME_sim`, `_d0..` per duty):
a volume envelope whose fit error is over `GOOD`, an arpeggio envelope that is not a repeating cycle of 8 steps or fewer, an arpeggio named by a note (`Arpeggio="..."`, which was ignored before), a duty envelope that is not a sweep, a noise-mode envelope, and a pitch envelope.
The volume of a step is the envelope times the note's volume, rounded to 0-15 (exact); a volume envelope's release point goes on past the end of the note, up to the next note. Frame `f` starts `round(f x ticks per frame)` ticks into the note, so a step is within half a tick of its frame.
The error is reported: the tick size of a frame, and the RMS and worst pitch rounding in semitones (a pitch envelope moves in less than a semitone, a chipsynth note cannot). A pitch envelope that is a small looping swing (under a semitone, measured on C4) is a vibrato (`vib`) instead, with its RMS error.
When a frame is shorter than a tick (a tempo of about 30 bpm), nothing is simulated and the fit is used, with a report line.
**Not verified:** FamiStudio's pitch envelope unit and sign were not found in the documentation or the source files that could be fetched; this assumes one unit is one step of the NES period register (16 per square cycle, 32 for the triangle, 14 for the VRC6 saw), positive is higher, and a relative envelope adds its value each frame and stops where a non-looping one ends.
Pitch envelopes on the noise, FDS, N163, S5B and EPSM channels are still reported as left out (their units differ). Whether the VRC6 saw plays the octave its note name says, how FamiStudio holds the last value of an envelope without a loop, and that a note without a release point stops dead at its end, are taken from the existing importer and not checked against FamiStudio.
Nothing was compared against FamiStudio's own playback or by ear; the check was that the rendered frames follow the envelope (a 15 / 5 envelope renders at about 2.9 : 1).
Furnace `.fur` importer (branch `furnace-import`): `python -m chipsynth import PKG.csp FILE.fur [--song NAME|N] [--report FILE]` and composer File > Import. Formats 157 to 259 are read (Furnace 0.6pre5 and newer); versions 157 to 251 were checked against real modules, 252 to 259 only against the format document. Older files, newer files and `PATR` (pre-0.6 pattern) modules are refused with the reason. Chips imported: NES 2A03 (two pulses, triangle, noise, DPCM as `D1`), C64 SID (3 voices) and the two Game Boy pulse channels; every other chip, the Game Boy wave and noise channels and a SID's PCM channel are skipped and reported. One Furnace tick is 4 chipsynth ticks, so speed changes, alternating speeds and grooves keep their exact row lengths; tick-rate effects (`Cxxx`, `F0xx`) become `Tempo` events. Carried over: notes, off/release, ADSR, duty and pulse width, volume and arpeggio macros (through `fit_volume` and `fit_arpeggio`), `00xy` arpeggio, `EC`, `ED`, `12xx`, `0B`/`0D` jumps, `FF` stop, the loop point, and samples (hardware DPCM directly, PCM converted through `dpcm.convert_wav` and reported as approximate). Subsongs become songs of one package.
Not built: `.dmf` (its format document could not be reached, so it is refused with a pointer to Furnace's File > Save as `.fur`); SID ring/sync/volume/cutoff macros and a relative duty macro; vibrato, portamento and the other pattern effects (listed as left out). Not verified: playback by ear; the NES noise note-to-period map, the triangle's octave and the SID filter mapping; 16-bit pattern channel numbers in versions 252 to 259. The tests use a module builder written from the same document, so a misreading shared by both would go unnoticed; real modules were parsed and compiled locally (396 files, no crashes) but are not committed.
Furnace `.fur` writer (branch `furnace-export`): `python -m chipsynth export-song PKG.csp:SONG -o OUT.fur [--target nes|sid] [--region ntsc|pal]` and composer File > Export > Furnace module. It writes format version 228 (Furnace 0.6.8.1), the newest the format document describes: Furnace 0.6.8.3 itself saves 232, but 229 to 232 only add VERA and YM2612 flags, and Furnace refuses a module newer than itself, so 228 opens in every release from 0.6.8.1. NES: V1, V2 pulses, V3 triangle, V4 noise, D1 DPCM (samples padded to the 16n+1 bytes the hardware reads); C64 SID (6581): V1-V3. Instruments carry volume macros from the ADSR (shared where a gate always lasts to the plateau, one per gate where a note is released early), duty macros, arpeggio macros and vibrato (a pitch macro); a SID keeps its hardware ADSR, wave, 12-bit duty and a static filter. Speed, groove (`09xx`), tempo changes and the loop (`0Bxx`; `FFxx` for `once`) land every note within one frame.
**Verified against the real Furnace:** v0.6.8.3 (format 232) was built from source (cmake, headless) in a scratch directory outside the repo and every file the writer makes was loaded (`-info`) and rendered to wav (`-output`); pitch of pulse, triangle, noise and SID voices, note onsets, grooves and tempo changes (under one frame), the loop over two passes, `once`, the volume envelope and release, duty widths, arpeggio and vibrato were measured on those renders against chipsynth's own render of the same tune (autocorrelation pitch, envelope RMS, onset detection). PAL and NTSC both load and play at their own rate. This also showed that Furnace's NES pulse sounds an octave above its note name, so the writer puts pulse notes an octave down (the triangle needs no shift). **Not verified:** the Furnace master branch (the build was not finished); Furnace's GUI; playback by ear; the timing of a 6581 filter (the filter is shared by the three voices in Furnace, so its sweep is left out and a static filter only approximates the direction); the NES DPCM rates on PAL (Furnace's table differs from the NTSC one). The suite (`tests/test_furnace_export.py`) cannot run Furnace: it uses a strict block reader of our own and a round trip through the importer, which is not independent.
Importer findings from the same run (the importer was not changed): it reads an NES pulse an octave lower than Furnace plays it; its macro release semantics differ from Furnace's (Furnace holds the value at the release index while the note is held and plays the tail after release); it accepts `Cxxx` over the range C0-CF (Furnace's tick rate effect is C0-C3); and a loop that is not on a bar line is snapped down to a bar, as a chipsynth song loops at a bar.
