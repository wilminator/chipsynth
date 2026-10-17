# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The song generator (see DESIGN-MUSIC.md): a context kind, a seed and an optional mood become a
finished tune in MML text, so a generated song is an ordinary tune file the maker can save and edit.

    text = generate("fight", 42, mood="tense")      # same arguments, same song, every time
    meta(text)      -> {"kind": "fight", "mood": "tense", "seed": 42, "tempo": 163, "sum": "1a2b3c4d"}
    is_edited(text) -> True once the body no longer matches its recorded checksum

Structure: a key and scale, a chord progression (one chord a bar, or two in the fast kinds), a melody
built from two-bar phrases (A A' B A''), an arpeggio or harmony voice, a bass, and noise drums.

Options that switch the DAC lanes on (all off by default; with them off the text is exactly what it always was):

    generate("fight", 42, dac=True)                  # drums on D1 / D2 (kick, snare, hats, crash, toms), no noise voice
    generate("fight", 42, dac_bass=True)             # the bass as pitched samples on D2 instead of the synth voice
    generate("boss", 7, voice=True)                  # a rare one-shot "hey" accent on D1 (boss intro, turnarounds)

The samples are the built-in kit (kit.py): a song names them in its [sample] section and the parser finds them with no
files, in a package they are embedded, and a file of the same name in music/samples replaces one.
"""
import random
import re
import zlib

from .mml import TICKS_WHOLE, duration_text, note_name

KINDS = ("title", "victory", "gameover", "fight", "boss", "world", "maze", "town")
MOODS = ("", "calm", "tense", "bright", "dark")
SLOT = TICKS_WHOLE // 16                       # a 16th note: 24 ticks

SCALES = {
    "major": (0, 2, 4, 5, 7, 9, 11), "minor": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10), "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11), "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
}
MAJORISH = ("major", "mixolydian", "lydian")
PROGRESSIONS = {
    True: [[0, 4, 5, 3], [0, 3, 4, 3], [0, 5, 3, 4], [0, 3, 0, 4], [0, 4, 3, 4]],      # major-like
    False: [[0, 5, 2, 6], [0, 3, 4, 0], [0, 6, 5, 6], [0, 5, 3, 4], [0, 2, 5, 4]],     # minor-like
}
RHYTHMS = {
    "sparse": [[16], [8, 8], [12, 4], [8, 4, 4], [4, 4, 8], [6, 2, 8], [8, 6, 2]],
    "mid": [[4, 4, 4, 4], [4, 4, 2, 2, 4], [4, 2, 2, 4, 4], [6, 2, 4, 4], [2, 2, 4, 4, 4],
            [4, 6, 2, 4], [8, 4, 2, 2], [3, 1, 4, 4, 4]],
    "dense": [[2] * 8, [2, 2, 4, 2, 2, 4], [2, 1, 1, 2, 2, 4, 4], [4, 2, 2, 2, 2, 4],
              [2, 2, 2, 2, 4, 4], [1, 1, 2, 2, 2, 2, 2, 2, 2], [3, 1, 2, 2, 3, 1, 4]],
}
# bpm range, modes, bars, lead / bass / arp instruments, bass pattern, arp pattern, drums, density, once
PRESETS = {
    "fight": dict(bpm=(150, 170), modes=("minor", "phrygian", "dorian"), bars=16, lead="lead", bass="bass",
                  arp="arp", bassp="eighth", arpp="sixteenth", drums="busy", dens=0.75, fast=True),
    "boss": dict(bpm=(130, 150), modes=("minor", "phrygian", "locrian"), bars=16, lead="buzz", bass="sawbass",
                 arp="arp", bassp="eighth", arpp="eighth", drums="heavy", dens=0.6, fast=True),
    "world": dict(bpm=(100, 120), modes=("major", "mixolydian", "lydian"), bars=16, lead="lead2", bass="bass",
                  arp="arp", bassp="quarter", arpp="eighth", drums="light", dens=0.5),
    "maze": dict(bpm=(70, 90), modes=("minor", "phrygian", "dorian"), bars=8, lead="flute", bass="pad",
                 arp="bell", bassp="half", arpp="sparse", drums="sparse", dens=0.3),
    "town": dict(bpm=(90, 110), modes=("major", "mixolydian"), bars=8, lead="flute", bass="bass",
                 arp="arp", bassp="walk", arpp="eighth", drums="none", dens=0.45),
    "title": dict(bpm=(96, 104), modes=("major", "minor"), bars=16, lead="lead", bass="bass",
                  arp="organ", bassp="quarter", arpp="eighth", drums="light", dens=0.55),
    "victory": dict(bpm=(130, 150), modes=("major",), bars=4, lead="bright", bass="bass",
                    arp="arp", bassp="quarter", arpp="eighth", drums="light", dens=0.7, once=True, up=True),
    "gameover": dict(bpm=(60, 75), modes=("minor", "phrygian"), bars=4, lead="flute", bass="pad",
                     arp="bell", bassp="half", arpp="sparse", drums="none", dens=0.3, once=True, down=True),
}
DRUM = {"kick": (24, "kick"), "snare": (67, "snare"), "hat": (84, "hat"), "crash": (72, "crash")}   # pitch, instrument
VOLUME = {1: 14, 2: 9, 3: 13, 4: 12}

# ---- the DAC lanes (D1, D2)
DAC_SAMPLE = {"kick": "kit_kick", "snare": "kit_snare", "hat": "kit_hat", "ohat": "kit_ohat", "crash": "kit_crash",
              "tom": "kit_tom", "tomlo": "kit_tomlo", "hey": "kit_hey"}
DAC_VEL = {"kick": 14, "snare": 12, "hat": 11, "ohat": 11, "crash": 12, "tom": 12, "tomlo": 12, "hey": 13}
DAC_BASS_VEL = 13
DAC_LOW = ("kick", "snare", "tom", "tomlo", "hey")                 # the body of the kit: lane D1 (hats and crash go to D2)
DAC_PRIORITY = ("crash", "hey", "kick", "snare", "tom", "tomlo", "ohat", "hat")   # who wins when two hits share a lane
DAC_PAN = {1: 0, 2: 2}
# where a voice accent may fall: kind -> ((bar, slot, chance), ...); bar -1 is the last bar
VOICE_SPOTS = {"boss": ((0, 0, 1.0), (-1, 8, 1.0)), "fight": ((-1, 12, 0.5),), "title": ((-1, 12, 0.5),),
               "world": ((-1, 12, 0.5),), "victory": ((-1, 4, 1.0),)}
PAN = {1: 0, 2: -3, 3: 0, 4: 2}


# ------------------------------------------------------------------ the header record
def _sum(text):
    body = "\n".join(l for l in text.split("\n") if not l.startswith("// generated:"))
    return "%08x" % (zlib.crc32(body.encode("utf-8")) & 0xFFFFFFFF)


def meta(text):
    """The `// generated:` record of a generated tune, or None."""
    for line in text.split("\n")[:5]:
        if line.startswith("// generated:"):
            d = dict(kv.split("=", 1) for kv in line[len("// generated:"):].split() if "=" in kv)
            try:
                m = {"kind": d["kind"], "mood": "" if d.get("mood", "-") == "-" else d["mood"], "seed": int(d["seed"]),
                     "tempo": int(d["tempo"]), "sum": d["sum"]}
                if d.get("dac"):
                    m["dac"] = d["dac"]                    # letters: d drums, b bass, v voice accent (absent = none)
                return m
            except (KeyError, ValueError):
                return None
    return None


def is_edited(text):
    m = meta(text)
    return m is not None and m["sum"] != _sum(text)


def dac_options(text):
    """(drums, bass, voice) switched on in a generated tune's record; all False for a plain or hand-written tune."""
    m = meta(text)
    code = m.get("dac", "") if m else ""
    return "d" in code, "b" in code, "v" in code


# ------------------------------------------------------------------ composing
class Song:
    def __init__(self, kind, seed, mood, tempo):
        p = PRESETS[kind]
        self.kind, self.mood, self.p, self.seed = kind, mood, p, seed
        self.rng = random.Random("%s:%s:%s" % (kind, seed, mood))
        r = self.rng
        modes = list(p["modes"])
        dens, bpm = p["dens"], r.randint(*p["bpm"])
        self.reg = 0                                   # register shift in scale steps
        self.lead, self.bass, self.arp = p["lead"], p["bass"], p["arp"]
        self.drums, self.arpp = p["drums"], p["arpp"]
        if mood == "calm":
            modes, bpm, dens = ["major", "lydian"], bpm * 0.8, dens * 0.6
            self.lead, self.arpp = "flute", ("sparse" if self.arpp == "sixteenth" else self.arpp)
            self.drums = "none" if self.drums in ("busy", "heavy") else self.drums
        elif mood == "tense":
            modes, bpm, dens = ["minor", "phrygian"], bpm * 1.1, min(0.9, dens * 1.15)
        elif mood == "bright":
            modes, bpm, dens = ["major"], bpm * 1.1, min(0.9, dens * 1.2)
            self.reg, self.arpp = 7, "sixteenth" if self.arpp != "sparse" else "eighth"
            self.lead = "lead" if self.lead in ("flute", "buzz") else self.lead
        elif mood == "dark":
            modes, bpm, dens = ["minor", "locrian"], bpm * 0.9, dens * 0.7
            self.reg = -7
            self.lead = "buzz" if self.lead in ("lead", "lead2", "flute", "bright") else self.lead
            self.bass = "sawbass" if self.bass == "bass" else self.bass
        self.mode = r.choice(modes)
        self.scale = SCALES[self.mode]
        self.tonic = r.randrange(12)
        self.dens = dens
        self.bpm = int(round(tempo if tempo else bpm))
        self.bpm = max(30, min(300, self.bpm))
        self.bars = p["bars"]
        self.once = p.get("once", False)
        prog = r.choice(PROGRESSIONS[self.mode in MAJORISH])
        self.chords = [prog[b % len(prog)] for b in range(self.bars)]
        self.fast = p.get("fast", False)
        if self.fast:                                      # two chords a bar
            self.halves = [prog[(2 * b + h) % len(prog)] for b in range(self.bars) for h in (0, 1)]
        else:
            self.halves = [c for c in self.chords for _ in (0, 1)]

    # ---- pitches
    def pitch(self, step):
        octv, idx = divmod(step, 7)
        return self.tonic + 12 * octv + self.scale[idx]

    def chord_steps(self, degree):
        return {degree % 7, (degree + 2) % 7, (degree + 4) % 7}

    def snap(self, step, degree):
        tones = self.chord_steps(degree)
        for off in (0, 1, -1, 2, -2, 3, -3):
            if (step + off) % 7 in tones:
                return step + off
        return step

    # ---- melody
    def rhythm(self):
        r, d = self.rng, self.dens
        if d < 0.35:
            pool = r.choices(("sparse", "mid"), (7, 3))[0]
        elif d <= 0.65:
            pool = r.choices(("sparse", "mid", "dense"), (15, 60, 25))[0]
        else:
            pool = r.choices(("mid", "dense"), (45, 55))[0]
        return list(r.choice(RHYTHMS[pool]))

    def phrase(self, bar0):
        """Two bars of (slot, length, step or None)."""
        r, p = self.rng, self.p
        weights = [(-3, 1), (-2, 2), (-1, 5), (0, 2), (1, 5), (2, 2), (3, 1)]
        if p.get("up"):
            weights = [(-2, 1), (-1, 2), (0, 2), (1, 5), (2, 5), (3, 2), (4, 1)]
        if p.get("down"):
            weights = [(-3, 2), (-2, 5), (-1, 6), (0, 2), (1, 1), (2, 1)]
        steps, choices = [w[0] for w in weights], [w[1] for w in weights]
        cur = 28 + self.reg + r.choice((0, 2, 4))
        home = cur
        out = []
        for b in range(2):
            slot = 0
            for ln in self.rhythm():
                if slot == 0 and b == 0 or slot in (0, 8):
                    cur = self.snap(cur, self.halves[2 * (bar0 + b) + (slot // 8)])
                else:
                    cur += r.choices(steps, choices)[0]
                cur = max(home - 4, min(home + 7, cur))
                rest = slot != 0 and r.random() < (1 - self.dens) * 0.3
                out.append((b * 16 + slot, ln, None if rest else cur))
                slot += ln
        return out

    def melody(self):
        """Bars of events for the lead, from phrases A A' B A''."""
        r = self.rng
        a, a2, b = self.phrase(0), None, self.phrase(2)
        a2 = [(s, l, st if (i < len(a) - 2 or st is None) else st + r.choice((-2, -1, 1, 2)))
              for i, (s, l, st) in enumerate(a)]
        plan = [a, a2, b, a]
        if self.bars >= 16:                                # a second half with phrases of its own
            c, d = self.phrase(8), self.phrase(10)
            c2 = [(s, l, st if (i < len(c) - 2 or st is None) else st + r.choice((-2, -1, 1, 2)))
                  for i, (s, l, st) in enumerate(c)]
            plan += [c, c2, d, a]
        events = [[] for _ in range(self.bars)]
        for ph in range(self.bars // 2):
            src = plan[ph % len(plan)]
            for slot, ln, step in src:
                bar, s = ph * 2 + slot // 16, slot % 16
                if step is not None and s in (0, 8) and ln >= 4:
                    step = self.snap(step, self.halves[2 * bar + s // 8])
                events[bar].append((s, ln, None if step is None else self.pitch(step), self.lead))
        if self.once:
            tonic = self.pitch(28 + self.reg)
            events[-1] = [(0, 16, tonic if self.mode in MAJORISH or self.p.get("down") else tonic, self.lead)]
            if len(events) > 1 and self.p.get("up"):
                events[-2] = [(0, 4, self.pitch(28 + self.reg + 4), self.lead), (4, 4, self.pitch(28 + self.reg + 7), self.lead),
                              (8, 8, self.pitch(28 + self.reg + 9), self.lead)]
        return events

    # ---- accompaniment
    def bass_line(self):
        events = []
        for bar in range(self.bars):
            ev = []
            for h in (0, 1):
                deg = self.halves[2 * bar + h]
                root = self.pitch(14 + deg)
                third, fifth = self.pitch(14 + deg + 2), self.pitch(14 + deg + 4)
                base = h * 8
                pat = self.p["bassp"]
                if pat == "eighth":
                    ev += [(base + 0, 2, root), (base + 2, 2, root), (base + 4, 2, root + 12), (base + 6, 2, root)]
                elif pat == "quarter":
                    ev += [(base + 0, 4, root), (base + 4, 4, fifth if h else root)]
                elif pat == "half":
                    ev += [(base, 8, root)]
                else:                                      # walk
                    ev += [(base + 0, 4, root), (base + 4, 4, third if h == 0 else fifth)]
            if self.once and bar == self.bars - 1:
                ev = [(0, 16, self.pitch(14))]
            events.append([(s, ln, p, self.bass) for s, ln, p in ev])
        return events

    def arp_line(self):
        events = []
        for bar in range(self.bars):
            ev = []
            for h in (0, 1):
                deg = self.halves[2 * bar + h]
                tones = [self.pitch(28 + self.reg // 2 + deg + k) for k in (0, 2, 4, 7)]
                base, pat = h * 8, self.arpp
                if pat == "sixteenth":
                    order = (0, 1, 2, 3, 2, 1, 2, 1)
                    ev += [(base + i, 1, tones[order[i]]) for i in range(8)]
                elif pat == "eighth":
                    order = (0, 1, 2, 1)
                    ev += [(base + 2 * i, 2, tones[order[i]]) for i in range(4)]
                else:                                      # sparse
                    if self.rng.random() < 0.7:
                        ev += [(base, 4, self.rng.choice(tones)), (base + 4, 4, None)]
                    else:
                        ev += [(base, 8, None)]
            if self.once and bar == self.bars - 1:
                ev = [(0, 16, self.pitch(28 + self.reg // 2 + 4))]
            events.append([(s, ln, p, self.arp) for s, ln, p in ev])
        return events

    def drum_line(self):
        events = []
        style, r = self.drums, self.rng
        for bar in range(self.bars):
            hits = {}
            if style == "busy":
                hits = {0: "kick", 8: "kick", 4: "snare", 12: "snare", 2: "hat", 6: "hat", 10: "hat", 14: "hat"}
                if r.random() < 0.5:
                    hits[10] = "kick"
            elif style == "heavy":
                hits = {0: "kick", 3: "kick", 8: "kick", 11: "kick", 4: "snare", 12: "snare", 6: "hat", 14: "hat"}
                if bar % 8 == 0:
                    hits[0] = "crash"
            elif style == "light":
                hits = {0: "kick", 8: "kick", 4: "hat", 12: "snare"}
            elif style == "sparse":
                hits = {0: "kick"} if bar % 2 == 0 else {}
                hits[12] = "hat"
            if style in ("busy", "heavy") and bar % 4 == 3:
                hits.update({12: "snare", 13: "snare", 14: "snare", 15: "snare"})
            if self.once and bar == self.bars - 1:
                hits = {0: "crash"} if style != "none" else {}
            slots = sorted(hits)
            ev = []
            for i, s in enumerate(slots):
                nxt = slots[i + 1] if i + 1 < len(slots) else 16
                ln = 16 if (self.once and bar == self.bars - 1) else min(2, nxt - s)
                pitch, inst = DRUM[hits[s]]
                ev.append((s, ln, pitch, inst))
            events.append(ev)
        return events

    # ---- the DAC lanes.  They draw on random generators of their own, so nothing above ever changes.
    def dac_rng(self, what):
        return random.Random("%s:%s:%s:%s" % (self.kind, self.seed, self.mood, what))

    def dac_style(self):
        style = self.drums
        if style == "none":
            style = "toll" if self.kind == "gameover" else "soft"        # a DAC kit can give them a quiet beat
        if self.mood == "calm" and style in ("busy", "heavy", "light"):
            style = "soft"
        return style

    def dac_drums(self):
        """Per bar a list of (slot, drum, velocity): the kit's patterns for this kind, mood and tempo.  Fast tempos
        get eighth-note hats, slow ones quarter-note hats and a half-time snare; dark thins the hats and adds low toms,
        bright opens the hats, tense pushes extra kicks."""
        r, style, mood, bpm = self.dac_rng("drums"), self.dac_style(), self.mood, self.bpm
        slow, fast = bpm < 90, bpm >= 140
        bars = []
        for bar in range(self.bars):
            h = []

            def add(slot, key, vel=None):
                h.append((slot, key, DAC_VEL[key] if vel is None else vel))

            def hats(step, start, skip=(), on=11, off=8):
                for s in range(start, 16, step):
                    if s not in skip:
                        add(s, "hat", on if s % 4 == 0 else off)
            if self.once and bar == self.bars - 1:                      # the last chord of a jingle
                if style != "toll":
                    add(0, "kick")
                add(0, "crash")
                bars.append(h)
                continue
            odd, fill = bar % 2 == 1, bar % 4 == 3 and not self.once
            if style == "busy":
                if slow:
                    add(0, "kick")
                    add(8, "snare")
                    if r.random() < 0.4:
                        add(10, "kick")
                    hats(4, 0)
                else:
                    for s in (0, 8):
                        add(s, "kick")
                    for s in (4, 12):
                        add(s, "snare")
                    if r.random() < 0.5:
                        add(10, "kick")
                    open14 = fast and odd
                    hats(2 if bpm >= 120 else 4, 0 if bpm >= 120 else 2, skip=(14,) if open14 else ())
                    if open14:
                        add(14, "ohat")
                if bar > 0 and bar % 8 == 0:
                    add(0, "crash")
                if fill:
                    h = [x for x in h if x[0] < 12 or x[1] in ("hat", "ohat")]
                    if r.random() < 0.5:
                        for k, s in enumerate((12, 13, 14, 15)):
                            add(s, "snare", 9 + k)
                    else:
                        add(12, "tom")
                        add(13, "tom", 10)
                        add(14, "tomlo")
                        add(15, "tomlo", 13)
            elif style == "heavy":
                if slow:
                    for s in (0, 6):
                        add(s, "kick")
                    add(8, "snare")
                else:
                    for s in (0, 3, 8, 11):
                        add(s, "kick")
                    for s in (4, 12):
                        add(s, "snare")
                add(6, "ohat")
                add(14, "hat")
                if not slow:
                    add(2, "hat", 8)
                    add(10, "hat", 8)
                if bar % 8 == 0:
                    add(0, "crash")
                if fill:
                    h = [x for x in h if x[0] < 8 or x[1] in ("hat", "ohat")]
                    add(8, "tom")
                    add(10, "tom", 11)
                    add(12, "tomlo")
                    add(14, "tomlo", 13)
                    add(15, "snare", 13)
            elif style == "light":
                add(0, "kick")
                if slow:
                    add(8, "snare")
                    hats(8, 4)
                else:
                    add(8, "kick", 12)
                    if r.random() < 0.35:
                        add(10, "kick", 11)
                    add(12, "snare")
                    if bpm >= 110:
                        add(4, "snare", 10)
                    if bpm >= 100:
                        hats(4, 2, skip=(14,) if (mood == "bright" and odd) else (), on=9, off=7)
                    else:
                        hats(8, 4)
                if self.once and bar == self.bars - 2:                   # a snare roll into the final chord
                    h = [x for x in h if x[0] < 8 or x[1] == "kick"]
                    for k, s in enumerate((8, 10, 12, 13, 14, 15)):
                        add(s, "snare", 8 + k)
                elif bar % 8 == 7 and not self.once:
                    add(14, "snare", 10)
                    add(15, "snare", 12)
                elif bar % 8 == 0 and bar > 0:
                    add(0, "crash", 10)
            elif style == "sparse":
                if bar % 2 == 0:
                    add(0, "kick", 12)
                if bar % 4 == 1:
                    add(8, "tomlo", 10)
                add(12, "ohat" if bar % 4 == 3 else "hat", 9)
            elif style == "toll":
                add(0, "tomlo", 12 if bar == 0 else 10)
                if bar % 2 == 1:
                    add(8, "tomlo", 8)
            else:                                                       # soft
                add(0, "kick", 11)
                if not slow or bar % 2 == 0:
                    add(8, "kick", 9)
                hats(8, 4, on=8, off=8)
            # mood on top of the pattern
            if mood == "tense" and style in ("busy", "heavy", "light") and r.random() < 0.5:
                add(6, "kick", 11)
            if mood == "tense" and odd and not fill and style in ("busy", "heavy"):
                add(15, "snare", 8)
            if mood == "dark" and style not in ("toll", "sparse"):
                if not odd:
                    add(10, "tomlo", 9)
                h = [x for x in h if not (x[1] == "hat" and x[0] % 4 == 2 and odd)]
            if mood == "bright" and style in ("busy", "heavy"):
                h = [(s, "ohat", v) if (k == "hat" and s == 14 and odd) else (s, k, v) for s, k, v in h]
            bars.append(h)
        return bars

    def dac_bass(self):
        """Per bar a list of (slot, length, kit sample name, velocity): the synth bass line as pitched samples."""
        from . import kit
        return [[(s, ln, kit.bass_name(p), DAC_BASS_VEL) for s, ln, p, _ in bar if p is not None] for bar in self.bass_line()]

    def voice_spots(self):
        """[(bar, slot)] where the one-shot voice accent falls: rare, and only on a boss intro, a turnaround or a jingle's end."""
        r, out = self.dac_rng("voice"), []
        for bar, slot, chance in VOICE_SPOTS.get(self.kind, ()):
            if r.random() < chance:
                out.append((bar % self.bars, slot))
        return out


# ------------------------------------------------------------------ writing MML
class Emitter:
    def __init__(self):
        self.oct = None
        self.inst = None

    def bar(self, events):
        toks, at = [], 0
        for s, ln, pitch, inst in sorted(events, key=lambda e: e[0]):
            if s > at:
                toks += self.rests(s - at)
            ln = min(ln, 16 - s)
            if pitch is None:
                toks += self.rests(ln)
            else:
                toks.append(self.note(pitch, ln, inst))
            at = s + ln
        if at < 16:
            toks += self.rests(16 - at)
        return " ".join(toks)

    def rests(self, slots):
        return ["r" + ("" if p == "8" else p) for p in duration_text(slots * SLOT)]

    def note(self, pitch, slots, inst):
        out = ""
        if inst != self.inst:
            out += "@%s " % inst
            self.inst = inst
        octv = pitch // 12
        if octv != self.oct:
            out += str(octv)
            self.oct = octv
        pieces = duration_text(slots * SLOT)
        out += note_name(pitch) + ("" if pieces[0] == "8" else pieces[0])
        for extra in pieces[1:]:
            out += "&" + extra
        return out


class DacEmitter:
    """One DAC line (D1 / D2): a sample name plays until the next name, so every hit lasts until the next one."""

    def __init__(self):
        self.vel = None

    def rests(self, slots):
        return ["r" + ("" if p == "8" else p) for p in duration_text(slots * SLOT)]

    def bar(self, events):
        """events: [(slot, sample name, velocity)] in one bar."""
        ev = sorted(events, key=lambda e: e[0])
        if not ev:
            return " ".join(self.rests(16))
        toks = self.rests(ev[0][0]) if ev[0][0] else []
        for i, (s, name, vel) in enumerate(ev):
            nxt = ev[i + 1][0] if i + 1 < len(ev) else 16
            if vel != self.vel:
                toks.append("v%d" % vel)
                self.vel = vel
            pieces = duration_text((nxt - s) * SLOT)
            toks.append(name + ("" if pieces[0] == "8" else pieces[0]))
            toks += ["r" + ("" if p == "8" else p) for p in pieces[1:]]       # a long hit is a hit, then rests
        return " ".join(toks)


def _one_per_slot(hits):
    """[(slot, drum, vel)] -> {slot: (drum, vel)} where a lane plays one thing at a time: the strongest wins."""
    out = {}
    for s, key, vel in hits:
        if s not in out or DAC_PRIORITY.index(key) < DAC_PRIORITY.index(out[s][0]):
            out[s] = (key, vel)
    return out


def dac_lanes(song, drums, bass, spots):
    """{1: bars, 2: bars} of [(slot, sample name, velocity)] for D1 and D2 (a lane with nothing to play is left out)."""
    n = song.bars
    d1, d2 = [[] for _ in range(n)], [[] for _ in range(n)]
    if drums:
        for bar, hits in enumerate(song.dac_drums()):
            lows = _one_per_slot([h for h in hits if h[1] in DAC_LOW or bass])
            highs = _one_per_slot([h for h in hits if h[1] not in DAC_LOW and not bass])
            d1[bar] += [(s, DAC_SAMPLE[k], v) for s, (k, v) in lows.items()]
            d2[bar] += [(s, DAC_SAMPLE[k], v) for s, (k, v) in highs.items()]
    if bass:
        d2 = [[(s, name, vel) for s, ln, name, vel in bar] for bar in song.dac_bass()]
    if spots:
        slot_seconds = 60.0 / song.bpm / 4
        span = max(2, int(0.42 / slot_seconds + 0.999))
        for bar, slot in spots:
            keep = [e for e in d1[bar] if not slot <= e[0] < min(16, slot + span)]
            d1[bar] = keep + [(slot, DAC_SAMPLE["hey"], DAC_VEL["hey"])]
    return {k: lane for k, lane in ((1, d1), (2, d2)) if any(lane)}


def generate(kind, seed, mood="", tempo=None, dac=False, dac_bass=False, voice=False):
    """A complete tune in MML.  kind: one of KINDS; mood: '', calm, tense, bright or dark.

    dac: the drums are samples on D1 / D2 (kick, snare, hats, crash, toms) instead of the noise voice.
    dac_bass: the bass is pitched samples on D2 instead of the synth voice (not for the pad-bass kinds, maze and gameover).
    voice: a rare one-shot "hey" on D1, only on a boss intro, a turnaround or the end of a jingle.
    All three are off by default, and then the text is exactly what the generator has always written."""
    if kind not in PRESETS:
        raise ValueError("unknown kind %r" % kind)
    song = Song(kind, int(seed), mood if mood in MOODS else "", tempo)
    dac_bass = bool(dac_bass) and song.bass != "pad"
    spots = song.voice_spots() if voice else []
    dac, voice = bool(dac), bool(spots)
    voices = {1: song.melody(), 2: song.arp_line(), 3: song.bass_line(), 4: song.drum_line()}
    if song.drums == "none" or dac:
        voices.pop(4)
    if dac_bass:
        voices.pop(3)
    lanes = dac_lanes(song, dac, dac_bass, spots) if (dac or dac_bass or voice) else {}
    em = {v: Emitter() for v in voices}
    name = "%s %d%s" % (kind.capitalize(), int(seed), " (%s)" % song.mood if song.mood else "")
    extras = [w for w, on in (("drums", dac), ("bass", dac_bass), ("voice accent", voice)) if on]
    lines = ["// generated:", "title " + name, "author Blobs generator",
             "comment %s in %s, %d bpm%s" % (kind, song.mode, song.bpm, ", DAC " + ", ".join(extras) if extras else ""),
             "tempo %d" % song.bpm, "time 4/4"]
    if song.once:
        lines.append("once")
    lines.append("")
    used = []
    for lane in lanes.values():
        for bar in lane:
            for _, sample, _ in bar:
                if sample not in used:
                    used.append(sample)
    if used:                                           # the kit is built in; declaring it lets the editor list it
        lines.append("[sample]")
        lines += ["%s = %s" % (sample, sample) for sample in used]
        lines.append("")
    dem = {k: DacEmitter() for k in lanes}
    for first in range(0, song.bars, 4):
        n = min(4, song.bars - first)
        lines.append("// bars %d-%d" % (first + 1, first + n))
        for v, bars in voices.items():
            setup = ""
            if first == 0:
                setup = "l8 v%d pan %d " % (VOLUME[v], PAN[v])
            text = " ".join(em[v].bar(bars[b]) + " |" for b in range(first, first + n))
            lines.append("V%d: %s%s" % (v, setup, text))
        for k, bars in lanes.items():
            setup = "l8 pan %d " % DAC_PAN[k] if first == 0 else ""
            text = " ".join(dem[k].bar(bars[b]) + " |" for b in range(first, first + n))
            lines.append("D%d: %s%s" % (k, setup, text))
        lines.append("")
    text = "\n".join(lines)
    code = ("d" if dac else "") + ("b" if dac_bass else "") + ("v" if voice else "")
    record = "// generated: kind=%s mood=%s seed=%d tempo=%d%s sum=" % (kind, song.mood or "-", int(seed), song.bpm,
                                                                      " dac=" + code if code else "")
    text = text.replace("// generated:", record + "00000000", 1)
    return text.replace("sum=00000000", "sum=" + _sum(text), 1)   # the sum covers all but the record line
