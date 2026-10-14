# Copyright (C) 2026 Michael Allen Wilmes
"""The song generator (see DESIGN-MUSIC.md): a context kind, a seed and an optional mood become a
finished tune in MML text, so a generated song is an ordinary tune file the maker can save and edit.

    text = generate("fight", 42, mood="tense")      # same arguments, same song, every time
    meta(text)      -> {"kind": "fight", "mood": "tense", "seed": 42, "tempo": 163, "sum": "1a2b3c4d"}
    is_edited(text) -> True once the body no longer matches its recorded checksum

Structure: a key and scale, a chord progression (one chord a bar, or two in the fast kinds), a melody
built from two-bar phrases (A A' B A''), an arpeggio or harmony voice, a bass, and noise drums.
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
                return {"kind": d["kind"], "mood": "" if d.get("mood", "-") == "-" else d["mood"], "seed": int(d["seed"]),
                        "tempo": int(d["tempo"]), "sum": d["sum"]}
            except (KeyError, ValueError):
                return None
    return None


def is_edited(text):
    m = meta(text)
    return m is not None and m["sum"] != _sum(text)


# ------------------------------------------------------------------ composing
class Song:
    def __init__(self, kind, seed, mood, tempo):
        p = PRESETS[kind]
        self.kind, self.mood, self.p = kind, mood, p
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


def generate(kind, seed, mood="", tempo=None):
    """A complete tune in MML.  kind: one of KINDS; mood: '', calm, tense, bright or dark."""
    if kind not in PRESETS:
        raise ValueError("unknown kind %r" % kind)
    song = Song(kind, int(seed), mood if mood in MOODS else "", tempo)
    voices = {1: song.melody(), 2: song.arp_line(), 3: song.bass_line(), 4: song.drum_line()}
    if song.drums == "none":
        voices.pop(4)
    em = {v: Emitter() for v in voices}
    name = "%s %d%s" % (kind.capitalize(), int(seed), " (%s)" % song.mood if song.mood else "")
    lines = ["// generated:", "title " + name, "author Blobs generator",
             "comment %s in %s, %d bpm" % (kind, song.mode, song.bpm), "tempo %d" % song.bpm, "time 4/4"]
    if song.once:
        lines.append("once")
    lines.append("")
    for first in range(0, song.bars, 4):
        n = min(4, song.bars - first)
        lines.append("// bars %d-%d" % (first + 1, first + n))
        for v, bars in voices.items():
            setup = ""
            if first == 0:
                setup = "l8 v%d pan %d " % (VOLUME[v], PAN[v])
            text = " ".join(em[v].bar(bars[b]) + " |" for b in range(first, first + n))
            lines.append("V%d: %s%s" % (v, setup, text))
        lines.append("")
    text = "\n".join(lines)
    record = "// generated: kind=%s mood=%s seed=%d tempo=%d sum=" % (kind, song.mood or "-", int(seed), song.bpm)
    text = text.replace("// generated:", record + "00000000", 1)
    return text.replace("sum=00000000", "sum=" + _sum(text), 1)   # the sum covers all but the record line
