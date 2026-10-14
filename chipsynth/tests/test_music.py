import os, sys, tempfile, time, unittest, wave
from pathlib import Path
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
import numpy as np
from chipsynth import mml, musicgen
from chipsynth.mml import parse, MMLError, duration_text, TICKS_WHOLE
from chipsynth.output import MusicOut
from chipsynth.player import TunePlayer, render, render_wav
from chipsynth.synth import RATE


def notes(text, voice=0):
    return parse(text).voices[voice]


class ParserTests(unittest.TestCase):
    def test_pitches_and_default_octave(self):
        self.assertEqual([n.pitch for n in notes("V1: c d e f g a b")], [48, 50, 52, 53, 55, 57, 59])
        self.assertEqual([n.pitch for n in notes("V1: c# d- b#")], [49, 49, 60])

    def test_octave_prefix_sticks_and_relative_shifts(self):
        self.assertEqual([n.pitch for n in notes("V1: 5c d")], [60, 62])
        self.assertEqual([n.pitch for n in notes("V1: o4 > c < < c")], [60, 36])
        self.assertEqual([n.pitch for n in notes("V1: >c <c")], [60, 48])
        self.assertEqual([n.pitch for n in notes("V1: o2 c 6c c")], [24, 72, 72])

    def test_octave_boundary_rolls_over_at_the_note(self):
        # o4a: octave 4 runs from A to G#, so c after a is the C above, and g is the top of the octave
        self.assertEqual([n.pitch for n in notes("V1: o4a a b c g")], [57, 59, 60, 67])

    def test_durations(self):
        d = [n.dur for n in notes("V1: c1 c2 c4 c8 c16 c32 c64")]
        self.assertEqual(d, [384, 192, 96, 48, 24, 12, 6])
        self.assertEqual([n.dur for n in notes("V1: c4. c8.. c8; c")], [144, 84, 32, 48])
        self.assertEqual([n.dur for n in notes("V1: l16 c c8 c")], [24, 48, 24])
        self.assertEqual([n.start for n in notes("V1: c4 r4 c4")], [0, 192])

    def test_ties(self):
        n = notes("V1: c4&c8")
        self.assertEqual((len(n), n[0].dur), (1, 144))
        n = notes("V1: c4&8 d")
        self.assertEqual((len(n), n[0].dur, n[1].start), (2, 144, 144))
        self.assertEqual(len(notes("V1: c4&d4")), 2)                 # no tie across different pitches
        with self.assertRaises(MMLError):
            parse("V1: r4 &8")

    def test_repeats_and_patterns(self):
        self.assertEqual(len(notes("V1: [c d]3")), 6)
        self.assertEqual(len(notes("V1: [c [d e]2]2")), 10)
        self.assertEqual(len(notes("V1: [c d]x4")), 8)
        self.assertEqual(len(notes("pat riff = c d\nV1: !riff !riff")), 4)
        self.assertEqual(len(notes("[pat]\nriff e f g\nV1: !riff")), 3)
        with self.assertRaises(MMLError):
            parse("V1: !nope")

    def test_voice_lines_keep_appending_and_other_labels(self):
        t = parse("V1: c4\nV2: e4\nP1: d4\nv1: f4")
        self.assertEqual([n.start for n in t.voices[0]], [0, 96, 192])
        self.assertEqual(len(t.voices[1]), 1)
        t = parse("D1: r4 r4\nV1: c")                  # a DAC line (samples: see test_music_dac.py)
        self.assertEqual(t.dac, [[], []])
        with self.assertRaises(mml.MMLError):          # no sample folder, so a sample name can't be found
            parse("D1: kick kick\nV1: c")

    def test_instruments(self):
        t = parse("[instr]\nfoo wave=saw pw=30 a=3 d=4 s=5 r=6 vol=7 pan=-2\nV1: @foo c @lead d")
        a, b = t.voices[0]
        self.assertEqual((a.inst.wave, a.inst.pw, a.inst.a, a.inst.d, a.inst.s, a.inst.r, a.inst.vol, a.inst.pan),
                         ("saw", 30, 3, 4, 5, 6, 7, -2))
        self.assertEqual(b.inst.wave, "pulse")
        self.assertEqual(parse("V1: instr bass c").voices[0][0].inst.wave, "tri")
        self.assertEqual(parse("import instr.flute\nV1: @flute c").voices[0][0].inst.wave, "sine")
        with self.assertRaises(MMLError) as cm:
            parse("V1: @nope c")
        self.assertEqual((cm.exception.line, cm.exception.col), (1, 5))
        with self.assertRaises(MMLError):
            parse("[instr]\nfoo wave=banjo")
        with self.assertRaises(MMLError):
            parse("[instr]\nfoo a=16")

    def test_header_wins_over_library(self):
        t = parse("[instr]\nlead wave=saw\nV1: @lead c")
        self.assertEqual(t.voices[0][0].inst.wave, "saw")

    def test_inline_tweaks_copy_the_instrument(self):
        n = notes("V1: wave tri c pw 30 d adsr 1,2,3,4 e")
        self.assertEqual(n[0].inst.wave, "tri")
        self.assertEqual(n[1].inst.pw, 30)
        self.assertEqual((n[2].inst.a, n[2].inst.d, n[2].inst.s, n[2].inst.r), (1, 2, 3, 4))
        self.assertEqual(n[0].inst.pw, 25)                         # the first note kept its own copy
        self.assertEqual(parse("V1: pan -3 c").voices[0][0].pan, -3)

    def test_volume(self):
        self.assertEqual([n.vol for n in notes("V1: v8 c v+ d v- e v+3 f v0 g")], [8, 9, 8, 11, 0])
        with self.assertRaises(MMLError):
            parse("V1: v16 c")

    def test_tempo_maps_ticks_to_samples(self):
        t = parse("tempo 120\nV1: c4 c4")
        self.assertEqual(t.tick_to_sample(96), RATE // 2)
        t = parse("tempo 120\nV1: c4 t60 c4 c4")
        self.assertEqual(t.tick_to_sample(96), 22050)
        self.assertEqual(t.tick_to_sample(192), 22050 + 44100)
        self.assertEqual(t.bpm_at(0), 120)
        self.assertEqual(t.bpm_at(100), 60)
        self.assertAlmostEqual(parse("V1: t60 c1").seconds, 4.0, places=3)

    def test_bars_are_checked(self):
        self.assertEqual(parse("V1: c1 | c1 |").warnings, [])
        w = parse("V1: c4 c4 c4 |").warnings
        self.assertTrue(w and "bar" in w[0])
        self.assertEqual(parse("time 3/4\nV1: c2. | c2. |").warnings, [])
        self.assertEqual(parse("time 6/8\nV1: c2. |").warnings, [])
        self.assertEqual(parse("time 3/4\nV1: c4").bar_ticks, 288)

    def test_loop_once_and_voice_lengths(self):
        t = parse("V1: c1 | loop c1 |")
        self.assertEqual((t.loop_tick, t.length, t.bars), (384, 768, 2))
        t = parse("V1: c2 loop c2")
        self.assertEqual(t.loop_tick, 0)
        self.assertTrue(any("loop" in w for w in t.warnings))      # snapped back to its bar
        self.assertTrue(parse("once\nV1: c").once)
        self.assertTrue(parse("V1: c once").once)
        t = parse("V1: c1 | c1\nV2: c1")
        self.assertTrue(any("shorter" in w for w in t.warnings))

    def test_comments_blank_lines_and_header_text(self):
        t = parse("// hi\ntitle My Song // not this\nauthor Mike\ncomment a b c\n/* block\nV2: c */\nV1: c // x\n\n")
        self.assertEqual((t.title, t.author, t.comment), ("My Song", "Mike", "a b c"))
        self.assertEqual((len(t.voices[0]), len(t.voices[1])), (1, 0))
        self.assertEqual(parse("tune 50\nV1: c").detune, 50)

    def test_errors_carry_line_and_column(self):
        cases = [("V1: c8 x", 1, 8), ("V1: c8\nV1: d [e", 2, 7), ("V1: c5", 1, 6), ("V1: 9c", 1, 6),
                 ("V1: ]", 1, 5), ("V1: t500 c", 1, 5), ("V1: o9 c", 1, 5), ("tempo fast", 1, 1),
                 ("flarp 3", 1, 1), ("[bogus]", 1, 1), ("V1: 99", 1, 5)]
        for text, line, col in cases:
            with self.assertRaises(MMLError, msg=text) as cm:
                parse(text)
            self.assertEqual((cm.exception.line, cm.exception.col), (line, col), text)
        with self.assertRaises(MMLError) as cm:
            parse("V1: o4c8")
        self.assertIn("space", cm.exception.msg)
        self.assertIn("line 1, col 5", str(MMLError("x", 1, 5)))

    def test_pattern_errors_name_the_call_site(self):
        with self.assertRaises(MMLError) as cm:
            parse("pat bad = c d x\nV1: c !bad")
        self.assertEqual(cm.exception.line, 1)
        self.assertIn("used at line 2", cm.exception.msg)

    def test_out_of_range_notes(self):
        for text in ("V1: o0 < c", "V1: o8 > c", "V1: o8 b#"):
            with self.assertRaises(MMLError, msg=text):
                parse(text)

    def test_event_cap(self):
        with self.assertRaises(MMLError) as cm:
            parse("V1: [[[c]99]99]99")
        self.assertIn("too long", cm.exception.msg)
        t = parse("V1: [c]99")
        self.assertEqual(t.events, 198)

    def test_duration_text(self):
        self.assertEqual(duration_text(24), ["16"])
        self.assertEqual(duration_text(120), ["4", "16"])
        self.assertEqual(duration_text(144), ["4."])
        self.assertEqual(duration_text(384), ["1"])
        self.assertEqual(duration_text(48 + 24), ["8."])
        for ticks in range(24, 400, 24):                           # every sum parses back to the same length
            text = "V1: c" + "&".join(duration_text(ticks))
            self.assertEqual(parse(text).length, ticks, text)

    def test_library_directory_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "instr.mml").write_text("[instr]\nzap wave=saw a=2\n[pat]\nrif c d e\n")
            lib = mml.load_library(d)
            t = parse("V1: @zap !rif", lib)
            self.assertEqual((t.voices[0][0].inst.wave, len(t.voices[0])), ("saw", 3))
            self.assertIn("lead", lib["instr"])
        self.assertIn("kick", mml.load_library("/no/such/dir")["instr"])


class PlayerTests(unittest.TestCase):
    TUNE = "tempo 120\nV1: @lead o4 l8 c d e f g a b >c |\nV2: @bass o2 c2 g2 |\nV4: @kick o2 c4 r4 @snare 5g4 r4 |\n"

    def test_renders_sound_and_stays_in_range(self):
        a = render(parse(self.TUNE))
        self.assertEqual(a.shape[1], 2)
        self.assertGreater(float(np.abs(a).max()), 0.05)
        self.assertLess(float(np.abs(a).max()), 1.0)

    def test_chunked_render_equals_one_render_including_loops(self):
        t = parse(self.TUNE)
        whole = TunePlayer(t).render(300000)
        p = TunePlayer(t)
        chunks = np.concatenate([p.render(7000) for _ in range(43)])[:300000]
        self.assertTrue(np.allclose(whole, chunks, atol=1e-5))

    def test_loops_keep_playing_and_schedule_more_passes(self):
        t = parse("V1: @organ c1 | loop e1 |")
        p = TunePlayer(t)
        seg = RATE * 4
        for _ in range(6):
            a = p.render(seg)
            self.assertGreater(float(np.sqrt((a ** 2).mean())), 0.02)
        self.assertGreaterEqual(p.passes, 5)
        self.assertFalse(p.done)

    def test_loop_restarts_at_the_loop_marker(self):
        # the first bar is c, then bar two (e) repeats: after the first pass only e is heard again
        t = parse("V1: @organ c1 | loop e1 |")
        p = TunePlayer(t)
        a = p.render(RATE * 12)
        def dominant(x):
            spec = np.abs(np.fft.rfft(x[:, 0] * np.hanning(len(x))))
            return np.argmax(spec[20:2000]) + 20
        n = RATE * 2
        c_bin = dominant(a[RATE // 4:RATE // 4 + n])
        e1 = dominant(a[RATE * 4 + RATE // 4:RATE * 4 + RATE // 4 + n])
        e2 = dominant(a[RATE * 8 + RATE // 4:RATE * 8 + RATE // 4 + n])
        self.assertLess(abs(e1 - e2), 3)
        self.assertGreater(e1, c_bin * 1.1)

    def test_once_tune_finishes_and_is_then_silent(self):
        p = TunePlayer(parse("once\nV1: c4 d4"))
        for _ in range(40):
            p.render(RATE // 2)
        self.assertTrue(p.done)
        self.assertEqual(float(np.abs(p.render(1000)).max()), 0.0)

    def test_empty_tune_does_not_hang(self):
        p = TunePlayer(parse("title nothing"))
        self.assertTrue(p.done)
        self.assertEqual(p.render(500).shape, (500, 2))

    def test_detune_scales_pitch(self):
        a = TunePlayer(parse("tune 0\nV1: @organ c1")).render(RATE)
        b = TunePlayer(parse("tune 1200\nV1: @organ c1")).render(RATE)
        def peak(x):
            return np.argmax(np.abs(np.fft.rfft(x[:, 0]))[20:4000]) + 20
        self.assertAlmostEqual(peak(b) / peak(a), 2.0, delta=0.05)

    def test_render_wav_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = render_wav(parse(self.TUNE), Path(d) / "t.wav", loops=1)
            with wave.open(str(path)) as f:
                self.assertEqual((f.getnchannels(), f.getsampwidth(), f.getframerate()), (2, 2, 44100))
                self.assertGreater(f.getnframes(), RATE * 4)

    def test_tempo_changes_play_at_the_right_time(self):
        t = parse("tempo 120\nV1: @organ c4 t60 e4 e4")
        p = TunePlayer(t)
        self.assertEqual(p._notes[0][1][0], RATE // 2)             # the second note starts after one 120-bpm beat
        self.assertEqual(p._notes[0][2][0], RATE // 2 + RATE)


class FakeChannel:
    def __init__(self):
        self.cur, self.queued, self.stopped = None, None, 0

    def get_busy(self):
        return self.cur is not None

    def get_queue(self):
        q = self.queued
        if q is not None:                                   # pretend the playing chunk finished
            self.cur, self.queued = q, None
        return None

    def play(self, snd):
        self.cur = snd
        self.log.append(("play", snd))

    def queue(self, snd):
        self.queued = snd
        self.log.append(("queue", snd))

    def stop(self):
        self.stopped += 1
        self.cur = self.queued = None


class FakeMixer:
    def __init__(self):
        self.ch = FakeChannel()
        self.ch.log = []
        self.sounds = []

    def Channel(self, n):
        return self.ch

    def Sound(self, buffer):
        self.sounds.append(len(buffer))
        return ("snd", len(buffer))


class OutTests(unittest.TestCase):
    def test_music_out_feeds_chunks_and_stops(self):
        mx = FakeMixer()
        out = MusicOut(mixer=mx)
        self.assertTrue(out.start(parse("V1: @organ c1 | c1 |")))
        deadline = time.time() + 5
        while len(mx.sounds) < 4 and time.time() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(mx.sounds), 4)
        self.assertEqual(set(mx.sounds), {MusicOut.CHUNK * 4})      # 16-bit stereo
        self.assertTrue(out.playing)
        out.stop()
        self.assertFalse(out.playing)
        self.assertGreaterEqual(mx.ch.stopped, 1)

    def test_no_mixer_is_silent_not_a_crash(self):
        out = MusicOut.__new__(MusicOut)
        MusicOut.__init__(out, mixer=False)
        out.mixer = None
        self.assertFalse(out.start(parse("V1: c")))
        out.stop()


class GeneratorTests(unittest.TestCase):
    def test_same_arguments_same_song_and_seeds_differ(self):
        a = musicgen.generate("fight", 7, "tense")
        self.assertEqual(a, musicgen.generate("fight", 7, "tense"))
        self.assertNotEqual(a, musicgen.generate("fight", 8, "tense"))
        self.assertNotEqual(a, musicgen.generate("fight", 7, "calm"))
        self.assertNotEqual(a, musicgen.generate("boss", 7, "tense"))

    def test_every_kind_and_mood_parses_cleanly_with_full_bars(self):
        for kind in musicgen.KINDS:
            for mood in musicgen.MOODS:
                for seed in (0, 1, 2, 99):
                    t = parse(musicgen.generate(kind, seed, mood))
                    self.assertEqual(t.warnings, [], (kind, mood, seed))
                    self.assertEqual(t.length % t.bar_ticks, 0)
                    self.assertGreater(len(t.voices[0]), 3)
                    self.assertGreater(len(t.voices[2]), 1)

    def test_kinds_follow_their_presets(self):
        for kind in ("victory", "gameover"):
            t = parse(musicgen.generate(kind, 3))
            self.assertTrue(t.once, kind)
            self.assertEqual(t.bars, 4)
        self.assertFalse(parse(musicgen.generate("world", 3)).once)
        for kind, (lo, hi) in (("fight", (150, 170)), ("maze", (70, 90)), ("town", (90, 110)), ("world", (100, 120))):
            self.assertTrue(lo <= parse(musicgen.generate(kind, 5)).bpm <= hi, kind)
        self.assertEqual(parse(musicgen.generate("world", 1)).bars, 16)
        self.assertEqual(parse(musicgen.generate("fight", 1)).bars, 16)

    def test_mood_changes_tempo_and_tempo_overrides(self):
        calm = parse(musicgen.generate("world", 4, "calm")).bpm
        plain = parse(musicgen.generate("world", 4, "")).bpm
        bright = parse(musicgen.generate("world", 4, "bright")).bpm
        self.assertLess(calm, plain)
        self.assertGreater(bright, plain)
        self.assertEqual(parse(musicgen.generate("world", 4, "calm", tempo=133)).bpm, 133)

    def test_drums_and_octaves_stay_in_range(self):
        t = parse(musicgen.generate("boss", 12))
        names = {n.inst.wave for n in t.voices[3]}
        self.assertEqual(names, {"noise"})
        for v in t.voices:
            for n in v:
                self.assertTrue(0 <= n.pitch <= 107)
        self.assertEqual(len(parse(musicgen.generate("town", 1)).voices[3]), 0)       # no drums in a town

    def test_record_and_edit_detection(self):
        text = musicgen.generate("maze", 21, "dark")
        m = musicgen.meta(text)
        self.assertEqual((m["kind"], m["mood"], m["seed"]), ("maze", "dark", 21))
        self.assertEqual(m["tempo"], parse(text).bpm)
        self.assertFalse(musicgen.is_edited(text))
        self.assertTrue(musicgen.is_edited(text.replace("V1:", "V1: r8", 1)))
        self.assertIsNone(musicgen.meta("title x\nV1: c"))
        self.assertFalse(musicgen.is_edited("title x\nV1: c"))
        self.assertEqual(musicgen.meta(musicgen.generate("fight", 1))["mood"], "")

    def test_generated_tune_renders_to_sound(self):
        a = render(parse(musicgen.generate("title", 5)), seconds=6)
        self.assertGreater(float(np.sqrt((a ** 2).mean())), 0.02)
        self.assertLess(float(np.abs(a).max()), 1.2)

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            musicgen.generate("nope", 1)


class PositionTests(unittest.TestCase):
    def test_sample_to_tick_inverts_tick_to_sample_across_tempo_changes(self):
        t = mml.parse("tempo 120\nV1: c4 t60 d4 t200 e4 f2\n")
        for tick in (0, 50, 96, 200, 300, 500):
            self.assertAlmostEqual(t.sample_to_tick(t.tick_to_sample(tick)), tick, delta=0.05)

    def test_player_position_folds_into_the_loop(self):
        t = mml.parse("tempo 240\nV1: c1 loop d1 e1\n")
        p = TunePlayer(t)
        p.render(int(t.seconds * 44100) + 5000)
        self.assertLessEqual(p.position(), t.tick_to_sample(t.length))
        self.assertGreaterEqual(p.position(), t.tick_to_sample(t.loop_tick) - 1)
        q = TunePlayer(t)
        q.render(1000)
        self.assertEqual(q.position(), 1000)
        self.assertEqual(q.position(lag=10.0), 0)
