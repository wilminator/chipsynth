# Blobs! chiptune engine -- design notes

Author: Mike.  Status: design talk in progress.  Built so far: see 'Built' at the end.

## Decided
- **Where music plays:** fights (incl. boss fights), world map and mazes, towns, title screen, victory, game over.
- **Sources:** both a tune editor (hand-written tunes) and a procedural generator (fills any place with no tune written).
- **Sound hardware (a merge of NES APU and C64 SID II):** 4 synth channels + 2 DACs.
  - Each channel: 5 waveforms (square with pulse width, sawtooth, triangle, sine, noise), independent volume,
    independent full ADSR envelope (attack, decay, sustain level, release).
  - 2 DACs: NES DPCM-compatible (1-bit delta samples at the NES's fixed rates, plus direct 7-bit level load);
    samples are imported from WAV files in the tune editor, converted to DPCM and stored with the tune.
  - Rendered to one PCM buffer and played, like the fight effects (soundcursor.py / pygame mixer).
- **Tune language:** a new, cleaner text MML (not PLAY-compatible), edited as text in a maker tool.
- **Assignment:** a music table file mapping contexts (title, victory, game over, fight, boss fight, world map N,
  maze N, town N) to tunes, with a default per kind; anything unlisted is generated, seeded by the place's number.
- **Player controls:** music volume, SFX volume and mute, saved in a settings file (key repeat setting joins it).
- **Transitions depend on context** (e.g. hard cut entering a fight; fade entering a town or maze).

## MML decisions
- **Voices:** one labelled line per voice (e.g. `P1:` pulse/channel 1, `D1:` a DAC); lines for the same voice keep appending.
- **Instruments:** named, defined once (`inst lead = pulse 25% a0 d8 s10 r4 vol12`, selected with `@lead`), plus inline
  commands to tweak one parameter mid-line (waveform, pulse width, A, D, S, R, volume).
- **Reuse:** named patterns (`pat riff = c8 e g e`, played with `!riff`) and repeat blocks (`[ ... ]x4`), plus a loop marker.
- **Timing:** musical lengths (default `l8`, dotted, ties `&`, triplets) and BPM (`t140`).
- **DACs:** named samples played like notes (`sample kick = kick.wav rate 15` declared once; the name plays it; a direct 7-bit
  level command sets the DAC level).

## Generator, transitions, quality
- **Generator input:** context kind picks a style preset; the place number seeds the melody (a place always sounds the same).
  An optional per-place **mood** knob (calm / tense / bright / dark) and tempo override live in the music table.
- **Generator style:** structured -- key/scale and chord progression; triangle bass, pulse melody, second voice arpeggio or
  harmony, noise + DAC drums; loops cleanly after 8-16 bars.
- **After a fight** the map / maze / town tune resumes at the position it was interrupted at (fading in); fights cut in hard.
- **Mix:** 44.1 kHz, 16-bit, band-limited waveforms. numpy is now a dependency (requirements.txt); Mike will install it in his venv.

## Pitch notation (decided)
- A note is `[[octave]note]duration[;|.]`: an optional octave prefix (a number, or one or more `<` / `>` angle brackets),
  the note letter (`a`-`g`, `#`/`+` sharp, `-` flat), the duration, then `.` (dotted) or `;` (the triplet operator).
- The octave prefix is **persistent**: `5c8` sets the voice's octave to 5 from then on; `<c8` / `>>c8` shift relative to the
  current octave from then on. A bare `<` or `>` shifts the octave as in PLAY.
- `o4` sets the starting octave. `o4a` (a note after it) moves the octave boundary: octave numbers roll over at that note
  instead of C (default C).
- **Scale degrees** are supported through a `key` command (`key c major`, `key a minor`, modes, pentatonic): while a key is on,
  digits are scale steps (`1 3 5 8`, 8 = the octave), `+`/`-` raise/lower, octave commands still apply; letter notes are not
  mixed in; `key off` returns to letter notes. Absolute (`5c8`), relative (`<c8`) and degree styles are all supported.

## More MML decisions
- **Volume:** 0-15 like the NES (`v12`, `v+` / `v-` step by one), applied on top of the instrument's volume.
- **Rests, ties, comments:** `r` rest (`p` also accepted), `&` tie (no re-attack), `//` line comments and `/* */` blocks.
- **Expression:** vibrato (rate and depth), pitch slide / portamento (also sweeps for drums), arpeggio, pulse-width sweep or
  modulation. Command letters and parameters for these are still to be chosen.

## Architecture (decided)
- A **separate music engine module** (new): parses MML into events, renders chunks with numpy, tracks play position for loops,
  fades and resume. soundcursor.py stays as it is, for the effects (including death animations).
- Both feed the same pygame mixer: music on its own channel(s), effects on others, with independent music and SFX volumes.
- Test early: fight effects playing over the battle music must mix cleanly (neither cuts the other off).

## Instruments, imports, ADSR (decided)
- **ADSR units:** A, D, R are rates 0-15 (0 = instant, 15 = slowest, on a time curve; SID-like), S is a level 0-15 (15 = full volume).
- **Imports:** instruments (and patterns, samples) are brought in by key, like an include: `import key` or `import section.key`,
  with shorthand for the common sections (`instr flute` = `import instr.flute`). Inline ADSR commands work after an import to
  tweak it for that voice.
- **Where definitions live:** the tune file's own header (sections such as [instr], [pat], [sample]) first, then shared library
  files (music/lib/instr.mml, pat.mml, sample.mml) as the fallback. A header entry overrides the library for that tune;
  a tune can be self-contained or lean on the shared instruments, and the generator uses the library.

## Instrument and effect syntax (decided)
- **Instrument definition:** one line in a header section, name then key=value pairs:
  `[instr]` / `flute wave=pulse pw=25 a=0 d=8 s=10 r=4 vol=12` (waves: pulse, saw, tri, sine, noise); effects may be
  given as defaults (`vib=`, `slide=`, `arp=`, `pws=`).
- **Effect commands in a voice line:** short commands with numeric parameters: `vib 5,3` (rate, depth; `vib off`), `slide 12`
  (glide into the next note over 12 ticks), `arp 0,4,7` (semitone offsets to cycle; `arp off`), `pws 25,75,40` (sweep pulse
  width from 25% to 75% at rate 40).
- **Voice tweaks by reference:** a header section (e.g. `[fx]`) holds named sets of tweaks (`wobble vib=5,3 pws=25,75,40`),
  called with the same import syntax (`import fx.wobble`, shorthand `fx wobble`), also usable alongside an instrument.
  Header first, then library (music/lib/fx.mml), like instruments.

## Effect ranges and header sections (decided)
- **Vibrato** `vib rate,depth`: rate 0-15 (0 = off, 15 about 12 Hz), depth 0-15 in 1/16-semitone steps.
- **Slide** `slide n`: n in 1/48 of a quarter note (tempo-synced; 0 = off), so `slide 12` is a 16th note.
- **Arpeggio** `arp o1,o2,...`: up to 8 semitone offsets (-24..24); rate `arprate n` in steps per beat (tempo-synced).
- **Pulse width** `pw` 1-99 percent; **sweep** `pws from,to,rate`: rate 0-15 like ADSR (0 = off, 15 fastest), bouncing back and forth.
- **Header sections:** `[instr]`, `[fx]`, `[pat]` (patterns), `[sample]` (DAC samples), `[key]` (named scales for degree mode).

## Generator presets (decided)
- **Context styles (defaults):** fight fast and driving (150-170 BPM, minor, busy drums); boss heavy (130-150 BPM, minor, saw bass,
  big drums); world adventurous (100-120 BPM, major, open melody); maze eerie (70-90 BPM, minor/phrygian, sparse, echo and
  vibrato); town warm (90-110 BPM, major/mixolydian, gentle arpeggios); title grand (~100 BPM); victory a bright ~4-bar
  fanfare played once; game over a slow falling minor phrase played once.
- **Moods** change mode, tempo, density and brightness: calm (about -20% tempo, major/lydian, sparse, sine/triangle, long releases);
  tense (+10%, minor/phrygian, repeating motifs, pulse sweeps and vibrato); bright (+10%, major, many arpeggios, high register,
  pulse 25-50%); dark (-10%, minor/locrian, low register, saw/noise textures, fewer notes). A `tempo` key overrides the BPM.

## Town music hints (decided)
- A new town-speech record **`\x08` MUSIC** (like the fight / IF / SKIP records) placed where the change should happen:
  a kind, a transition (`cut` / `fade`, default fade) and arguments. The town editor gets a "Music hint" entry with pickers.
- **Kinds:** set mood (calm / tense / bright / dark), switch tune, set tempo, set volume, stop the music, restore the music
  (after a stop), and **revert to defaults** (the place's normal music from the table).
- **Duration:** a hint holds until the music is changed again, by another hint or by a map change (leaving the town resets it).
- **Tune reference:** the music TOML has a `[tunes]` list giving each tune a number (`1 = "lute"`, ...); the record stores the
  number and the editor shows a picker of names. Renaming a file only needs the list updated.
- Needs no master-update step: old speeches contain no such record.

## Gap fixes (decided)
- **Echo / reverb:** a per-tune effect bus: `echo time,feedback,mix` (e.g. `echo 240ms,40,30`; `wide` = ping-pong) and a simple
  `reverb 0-15`, set in the tune header; each voice has a `send 0-15` (also an instrument / fx key). One shared bus keeps CPU low.
- **Stereo:** stereo output; `pan -8..8` per voice (also an instrument / fx key), default centre; constant-power panning; DACs
  panned the same way; mono mixes down fine.
- **DACs:** the 16 NES DPCM rates (NTSC table); 1-bit samples up to 4081 bytes; WAV import resamples to the chosen rate, downmixes to
  mono, normalises and delta-encodes, and reports the size, refusing (or offering to trim / speed up) when too big. A DAC can also
  take a direct 7-bit level.
- **Failures:** never crash. No audio device or a bad tune: continue silently with a short one-line note (shown once in the message
  window); a tune that fails to parse falls back to the generator for that place; music pauses when the window loses focus and
  resumes; parse errors in the maker show file, line and column.

## DAC import: stereo split and long samples (decided)
- **Stereo WAV:** the import offers to split it into two samples, one per channel: left on DAC 1 (panned left) and right on DAC 2
  (panned right), triggered together by one name.
- **Samples over the 4081-byte limit:** the import offers to split them into several parts, each at most the maximum buffer length
  (voice.1, voice.2, ...). One sample name plays the whole chain back to back on that DAC with no audible join (the engine queues the
  next part as one ends); the parts can still be played individually by number. A stereo split that is also long chains both channels.

## Remaining candidates settled (decided)
- **Noise:** SID-style: the note sets the noise frequency (the noise generator is clocked at the note's frequency, so any note is
  a noise pitch, not 16 steps). Drums (kick, snare, hat) are ordinary `[instr]` entries with short ADSR and `slide` for pitch
  drops. An optional `nmode short|long` gives the NES metallic or white-noise character (long is the default).
- **Tuning and range:** A4 = 440 Hz, notes o0-o8 (about 16 Hz to 7.9 kHz); notes outside are a parse error. `tune +n` is a master
  detune in cents, and a per-voice `detune` is allowed (chorus effects).
- **Bars:** a `time 4/4` header (default 4/4); `|` bar lines are checked (the parser warns when a voice's bar has the wrong length);
  the loop marker snaps to a bar; after a fight a resumed tune picks up on the next bar boundary.
- **Ducking:** music dips while sound effects play, with a short smooth ramp; the level is a setting in the settings file
  (`duck` 0-100 %, 0 = off; default about 30).
- **Tool features:** a preview key (play / stop, and from the cursor line), an MML help page (`?`), a `title` / `author` / `comment`
  header shown in the music list, and an event counter (events against the 200,000 cap, length in bars and seconds).
- **Filter:** SID-like: one shared filter with `filter lp|bp|hp|off cutoff,res` (cutoff 0-15 on an exponential curve, resonance 0-15),
  `fsweep from,to,rate` for sweeps, and per-voice routing (voices opt in; the DACs bypass it, as on the SID). Also an instrument key.

## Files
- **Tune = the MML text file** (`music/NAME.mml`); the game parses it when it loads. Imported DPCM samples are stored beside it.
- **Music table = a TOML file** (`music/music.toml`, author's choice): contexts -> tune + transition (cut / fade), with per-kind
  defaults. Read with Python's tomllib (3.11+; Mike runs 3.12); writing needs a small writer of our own.

## Music table (music/music.toml) -- decided
- **Tune numbers:** `[tunes]` lists numbered tunes (used by town music hints).
- **Layout:** one table per context kind, keyed by place number: `[title]`, `[victory]`, `[gameover]`, `[fight]`, `[boss]`,
  `[world.0]`, `[maze.3]`, `[town.5]`, plus `[world.default]`, `[maze.default]`, `[town.default]`.
- **Keys in an entry:** `tune` (an .mml name; omitted = generated), `transition` (`cut` / `fade`), `mood` (calm / tense /
  bright / dark) and `tempo` (BPM) for generated music, `volume` (0-15 trim) and `resume` (true / false: pick the tune up
  again after a fight). For generated music `dac`, `dac_bass` and `voice` (true / false, all off by default) add the
  built-in sample kit: drums on D1 / D2 (kick, snare, hats, crash, toms), the bass as pitched samples, and a rare
  shouted accent. The kit is made in code (`chipsynth/kit.py`), so a game needs no sample files; a `NAME.dpcm` of the same
  name in `music/samples` replaces a kit sound. On the command line:

  ```
  python -m chipsynth generate fight --seed 3 --dac --dac-bass --voice -o fight.mml
  ```

  In the composer, `Ctrl+N` accepts the words `dac`, `dac-bass` and `voice` after the kind, seed and mood.
- **Lookup order:** the place's own entry, then its kind's `.default`, then the built-in defaults.
- **Built-in transition defaults:** cut for fight, boss, title, victory, game over; fade for world, maze, town.
- **Editing:** a Music screen in the maker with pickers (contexts list; tune picker with a "Generated" row; transition, mood,
  tempo, volume, resume; Space previews). It writes the TOML with a backup; hand-editing still works.

## Looping and length (decided)
- **Loop marker:** a tune plays through once, then repeats from the `loop` marker; no marker = the whole tune loops from the start;
  `once` makes a tune play a single time (victory, game over jingles).
- **Length limit:** an **event-count cap** (about 200,000 events per tune), not a time cap; a tune over the cap is refused with a
  clear message, and the music tool shows the count. Playback is streamed (the mixer renders a few seconds ahead in chunks), so a
  tune can in principle run indefinitely; a loop repeats until the music changes.
- **Seamless loop:** release tails, echoes and slides ringing at the loop end carry over the restart (no gap or click).

## Import utilities (later)
- **FamiStudio import** (requested): convert FamiStudio projects/exports into Blobs tunes (NES channels -> our 4 channels + DACs;
  DPCM samples map straight to the DACs). Check which FamiStudio export (its text export, .fms project, or a standard like MIDI)
  is the most practical to read before building.
- **SID tracker import** (requested, if a common tool is found): candidates to evaluate are GoatTracker, SID-Wizard
  and CheeseCutter; pick the one with a documented, widely used song format (SID has 3 voices, so map them onto our channels).
  Imports produce ordinary tune files in the new MML/format, so an imported tune can be edited by hand afterwards.

## Still to decide
Nothing about the design. Build order next: mixer core, parser, generator, Music screen, game hooks, imports. Tests: render-to-buffer
checks (headless); Mike runs them.

## Generate-and-save (added 2026-10-11)
The Music screen in the maker can run the song generator (choose context/mood/seed), preview it, and
SAVE the result as an ordinary MML tune file, so it can be edited like a composed one. Because the
tune is the MML text, the generator must emit real MML (not just events). Details still to settle.
DECIDED: a saved generated tune's header records preset, mood and seed (so the same song can be
regenerated), and the notes are plain editable MML. The maker warns before regenerating over a file
that has been edited since it was generated (the header keeps a checksum of the generated body).

## Built (core, 2026-10-11)
- synth.py mixer core; mml.py parser (header, [instr], [pat], 4 voices, notes/octaves/rests/ties/dots/triplets, volume, tempo,
  inline wave/pw/adsr/pan, repeats, patterns, bars, loop/once, library fallback with built-in instruments); music.py player (streams,
  loops, MusicOut thread on a reserved pygame channel, WAV render); musicgen.py generator (all kinds and moods, MML out, `// generated:`
  record with checksum); make_music.py (list, editor, play, generate, rename, delete, WAV). Oscillators free-run while a voice is
  off so chunking never changes the sound.
- Echo/reverb bus built (synthfx.py): header `echo 240ms,40,30[,wide]` / `echo off`, `reverb 0-15`; `send 0-15` per voice, instrument or fx key (default 15; kick/snare/hat/crash 0); first repeat is the full mix level, each later one scaled by feedback; reverb is a Freeverb-style 4 comb + 2 all-pass pair per channel; exact under any chunking, tails carry over loop restarts.
- Filter built (synth.py, uses scipy.signal.lfilter): `filter lp|bp|hp,cutoff[,res]`, `fsweep from,to,rate` (triangle at rate x 0.5 Hz), also instrument/fx keys; cutoff 0-15 = 100 Hz..8 kHz exponential, Q = 0.7 + 0.9 x res; an RBJ biquad whose coefficients refresh every 32 samples of note age (so chunking never changes the sound); state resets on each note; the voice's bus send is filtered too; DACs bypass.
- DAC lines built (dpcm.py, synth._Dac chains, mml dac_word, music._schedule_dac): `D1:`/`D2:` play samples by name from `music/samples/NAME.dpcm` (file: BDPC header, rate, 1-2 tracks, parts <= 4081 bytes). A name plays it until the next name / `stop`; `r` rests; `l v t pan level loop once [..]n !pat` work. `[sample] alias = file rate=N`, `import sample.name`, library `lib/*.mml` may hold `[sample]`. Stereo: left on D1, right on D2, started together; long samples chain parts with the level carried across. `convert(..., fit=True)` turns the level down so a loud sample does not slope-overload.
- Sample import tool built (make_sample.py; launcher entry after Music table): list of music/samples with rate/size/length/stereo/parts, Enter imports a WAV (name in music/wav or a path), settings screen (name, DPCM rate with live size estimate, stereo mix/split, loudness normal/fit), Space previews the real conversion, S saves (backups/ keeps what it replaces), D moves a sample to backups/. `python -m chipsynth.dpcm demo GAMEDIR` writes kick/snare/zap demo WAVs; `python -m chipsynth.dpcm convert in.wav GAMEDIR [--name --rate N|auto --channel mix|left|right|stereo --no-normalize --trim [DB] --fade MS --max-seconds S --max-bytes N --truncate --fit --preview]` is the command-line version. Import options (`dpcm.ImportOptions`, `convert_wav`, `preview`; the defaults give the same bytes as plain `convert`): target rate or auto, channel pick, normalize on/off, trim silence, fade-out, max length, cut-to-cap; the settings screen has them all and shows the result's size, duration, rate and parts before saving (see chipsynth/README.md).
- Not yet: music.toml,
  game hooks. Note for the hooks: the game's mixer is mono 22050 (soundcursor.open_mixer); music needs 44100 stereo, so the game must
  init the mixer once at 44100 stereo and set mixer_rate to match.

## Built record layout (stage 4)
`\x08` + three signed 16-bit numbers: kind (0 mood, 1 tune, 2 tempo, 3 volume, 4 stop, 5 restore, 6 revert), arg (mood 0-4 =
default/calm/tense/bright/dark; tune number from `[tunes]`; BPM; volume 0-15; else 0), cut (0 fade, 1 cut). 7 characters.
Old speeches contain none, so old games play unchanged. Ignored away from a town or without a music director.

## Built: expression effects (slice 1 of the second music pass)
`vib rate,depth`, `arp o1,o2,...` (+ `arprate n`, steps per beat, default 8), `pws from,to,rate` are sticky per voice until changed or
`off`; `slide n` glides into the NEXT note only (from the previous note's pitch; an instrument's `slide=` default applies to every
note). All of them can be instrument keys or `[fx]` sets (`fx wobble`, library music/lib/fx.mml). Vibrato rate*0.8 Hz;
pulse-width sweep is a triangle at rate*0.5 Hz; the synth keeps the phase exact so chunking never changes the sound. The generator
does not use them yet.

## Built: key / degree mode (slice 2)
`key c major` (any tonic with # or -, modes major/minor/ionian/dorian/phrygian/lydian/mixolydian/aeolian/locrian,
pentatonic, minor-pentatonic, or a name from a `[key]` section: `dream = 0 2 3 7 9`, also in music/lib/key.mml and
`import key.name`) turns the voice's digits into scale degrees until `key off`. Degrees run 1 to (scale notes + 2): a
7-note scale has 8 = the octave and 9 = the 2nd above it; a pentatonic scale has 6 = the octave. `+` / `-` raise / lower a
semitone; `:` starts a length (`5:8.`, `3:4;`); a bare degree uses `l`. `o4`, `<` and `>` still move the octave; letter notes
and the `5c8` octave prefix are not used while a key is on. The key belongs to the voice (patterns inherit it).
