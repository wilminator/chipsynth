# Copyright (C) 2026 Michael Allen Wilmes
"""MML text <-> Song (see song.py).  `from_mml` keeps patterns, repeats and loops as real objects and own-line
comments as notes; `to_mml` writes them back as `[pat]`, `!name` and `[..]n`.  Round trip: for any MML that parses,
`from_mml(text).compile()` equals `parse(text)`, and `from_mml(to_mml(song))` gives the same song again."""
import re
from dataclasses import replace

from . import mml
from .mml import MMLError, TICKS_WHOLE, LETTER, DURATIONS, VOICE_LABEL, SECTION, WORDS, NOTE_NAMES
from .song import (Song, Clip, SongError, Rel, explicit_ticks, TRACKS, Note, Degree, Rest, Length, Octave, Volume,
                   Tempo, Tie, Extend, Select, Fx, Tweak, Key, Bar, Loop, Once, Repeat, Call, Comment, Play, Stop,
                   Level)
from .synth import Instrument

NAME = r"[A-Za-z0-9_.-]+"


# ------------------------------------------------------------------ MML -> Song
class _Tok:
    """Turns the text of a voice line or a pattern body into events (the same grammar as `_Parser.run_text`)."""

    def __init__(self, song, patterns, lib):
        self.song, self.patterns, self.lib = song, patterns, lib

    def clip(self, name, dac, line, col):
        """Make sure clip `name` exists (reading a library pattern into the song if it was never defined here)."""
        c = self.song.clips.get(name)
        if c is not None:
            if c.raw is None and c.dac != dac:
                raise MMLError("pattern '%s' is used on both note lines and DAC lines; the song model keeps one "
                               "kind per pattern (split it in two)" % name, line, col)
            return c
        src = self.patterns.get(name) or self.lib["pat"].get(name)
        if src is None:
            raise MMLError("no pattern '%s'" % name, line, col)
        body, pline, pcol = src
        c = self.song.clips[name] = Clip(name, [], dac)
        c.items = self.tokens(body, pline, pcol, dac)
        return c

    def duration(self, text, i, line, col0, need=False):
        m = re.match(r"(\d*)(\.*)(;?)", text[i:])
        digits, dots, trip = m.group(1), m.group(2), m.group(3)
        if need and not digits:
            raise MMLError("l needs a length such as l8", line, col0 + i)
        if len(dots) > 2:
            raise MMLError("at most two dots", line, col0 + i)
        if digits:
            nv = int(digits)
            if nv not in DURATIONS:
                raise MMLError("length %d is not 1, 2, 4, 8, 16, 32 or 64" % nv, line, col0 + i)
            return explicit_ticks(nv, len(dots), bool(trip)), m.end()
        if not dots and not trip:
            return None, m.end()
        return Rel(len(dots), bool(trip)), m.end()

    def tokens(self, text, line, col0, dac):
        out, i, size = [], 0, len(text)

        def err(msg, at=None):
            raise MMLError(msg, line, col0 + (i if at is None else at))

        while i < size:
            c = text[i]
            if c.isspace():
                i += 1
                continue
            cl = c.lower()
            if c == "|":
                out.append(Bar())
                i += 1
            elif c == "[":
                j, d = i + 1, 1
                while j < size and d:
                    d += (text[j] == "[") - (text[j] == "]")
                    j += 1
                if d:
                    err("[ has no matching ]")
                body, k = text[i + 1:j - 1], j
                m = re.match(r"x?(\d+)", text[k:])
                count = 2
                if m:
                    count, k = int(m.group(1)), k + m.end()
                if not 1 <= count <= 99:
                    err("a repeat count is 1 to 99", k - 1)
                out.append(Repeat(count, self.tokens(body, line, col0 + i + 1, dac)))
                i = k
            elif c == "]":
                err("] without a [")
            elif c == "!":
                m = re.match(NAME, text[i + 1:])
                if not m:
                    err("! needs a pattern name")
                self.clip(m.group(0), dac, line, col0 + i)
                out.append(Call(m.group(0)))
                i += 1 + m.end()
            elif dac and (c.isalpha() or c == "_"):
                i = self.dac_word(out, text, i, line, col0)
            elif c == "@":
                m = re.match(NAME, text[i + 1:])
                if not m:
                    err("@ needs an instrument name")
                out.append(Select(m.group(0)))
                i += 1 + m.end()
            elif c in "<>":
                out.append(Octave(shift=1 if c == ">" else -1))
                i += 1
            elif c == "&":
                i += 1
                m = re.match(r"\s*(\d+)(?![0-9]*[a-gA-G])", text[i:])
                if m:
                    ticks, used = self.duration(text, i + m.start(1), line, col0)
                    out.append(Extend(ticks))
                    i += used + m.start(1)
                else:
                    out.append(Tie())
            elif c.isdigit():
                m = re.match(r"(\d+)([a-gA-G])", text[i:])
                if dac:
                    err("a number needs a note before it (c8) or a letter after it (5c)")
                if m and len(m.group(1)) == 1:
                    out.append(Octave(set=int(m.group(1))))
                    i += 1
                elif m:
                    err("octave is 0 to 8")
                else:
                    i = self.degree(out, text, i, line, col0)
            elif re.match(r"key(?![A-Za-z])", text[i:], re.I):
                i = self.key(out, text, i, line, col0)
            elif WORDS.match(text[i:]):
                word = re.match(r"[A-Za-z]+", text[i:]).group(0).lower()
                j = i + len(word)
                if word == "loop":
                    out.append(Loop())
                    i = j
                elif word == "once":
                    out.append(Once())
                    i = j
                else:
                    m2 = re.match(r"\s+(\S+)", text[j:])
                    if not m2:
                        err("%s needs a value" % word)
                    arg = m2.group(1)
                    out.append(Select(arg) if word == "instr" else Fx(arg) if word == "fx" else Tweak(word, arg))
                    i = j + m2.end()
            elif cl in LETTER:
                j, acc = i + 1, 0
                while j < size and text[j] in "#+-":
                    acc += -1 if text[j] == "-" else 1
                    j += 1
                dur, used = self.duration(text, j, line, col0)
                out.append(Note(cl, acc, dur))
                i = j + used
            elif cl in "rp" and not re.match(r"pw|pan|pat", text[i:], re.I):
                dur, used = self.duration(text, i + 1, line, col0)
                out.append(Rest(dur))
                i += 1 + used
            elif cl == "l":
                ticks, used = self.duration(text, i + 1, line, col0, need=True)
                out.append(Length(ticks))
                i += 1 + used
            elif cl == "o":
                m = re.match(r"o(\d)([a-gA-G][#+\-]*)?", text[i:], re.I)
                if not m:
                    err("o needs an octave 0 to 8")
                if int(m.group(1)) > 8:
                    err("octave is 0 to 8")
                boundary = None
                if m.group(2):
                    ls = LETTER[m.group(2)[0].lower()]
                    boundary = (ls + sum({"#": 1, "+": 1, "-": -1}[x] for x in m.group(2)[1:])) % 12
                out.append(Octave(set=int(m.group(1)), boundary=boundary))
                i += m.end()
                if i < size and (text[i].isdigit() or text[i] in ".;"):
                    err("write a space before the note after o4a: o4a is the octave boundary form")
            elif cl == "t" and re.match(r"t\d", text[i:], re.I):
                m = re.match(r"t(\d+)", text[i:], re.I)
                out.append(Tempo(int(m.group(1))))
                i += m.end()
            elif cl == "v":
                m = re.match(r"v(?:(\d+)|([+-])(\d*))", text[i:], re.I)
                if not m:
                    err("v needs a volume 0 to 15 (or v+ / v-)")
                if m.group(1) is not None:
                    out.append(Volume(value=int(m.group(1))))
                else:
                    step = int(m.group(3) or 1)
                    out.append(Volume(delta=step if m.group(2) == "+" else -step))
                i += m.end()
            else:
                m = re.match(r"[A-Za-z]+", text[i:])
                err("don't know '%s'" % (m.group(0) if m else c))
        return out

    def degree(self, out, text, i, line, col0):
        d = int(text[i])
        j, acc = i + 1, 0
        while j < len(text) and text[j] in "#+-":
            acc += -1 if text[j] == "-" else 1
            j += 1
        dur, used = None, 0
        if j < len(text) and text[j] == ":":
            dur, used = self.duration(text, j + 1, line, col0)
            used += 1
        out.append(Degree(d, acc, dur))
        return j + used

    def key(self, out, text, i, line, col0):
        m = re.match(r"key\s+off(?![A-Za-z])", text[i:], re.I)
        if m:
            out.append(Key())
            return i + m.end()
        m = re.match(r"key\s+([a-gA-G][#+\-]*)\s+([A-Za-z][A-Za-z0-9_-]*)", text[i:], re.I)
        if not m:
            raise MMLError("key needs a tonic and a scale (key c major, key a minor, key off)", line, col0 + i)
        tonic = LETTER[m.group(1)[0].lower()] + sum({"#": 1, "+": 1, "-": -1}[x] for x in m.group(1)[1:])
        out.append(Key(tonic % 12, m.group(2).lower()))
        return i + m.end()

    def dac_word(self, out, text, i, line, col0):
        m = re.match(r"[A-Za-z_]+", text[i:])
        w = m.group(0)
        wl, j = w.lower(), i + len(w)

        def num(what, signed=False):
            mm = re.match(r"\s*(-?\d+)" if signed else r"\s*(\d+)", text[j:])
            if not mm:
                raise MMLError("%s needs a number" % what, line, col0 + i)
            return int(mm.group(1)), j + mm.end()
        if wl == "r":
            dur, used = self.duration(text, j, line, col0)
            out.append(Rest(dur))
            return j + used
        if wl == "l":
            ticks, used = self.duration(text, j, line, col0, need=True)
            out.append(Length(ticks))
            return j + used
        if wl == "v":
            val, j2 = num("volume")
            out.append(Volume(value=val))
            return j2
        if wl == "t":
            bpm, j2 = num("tempo")
            out.append(Tempo(bpm))
            return j2
        if wl == "pan":
            val, j2 = num("pan", signed=True)
            out.append(Tweak("pan", str(val)))
            return j2
        if wl == "loop":
            out.append(Loop())
            return j
        if wl == "once":
            out.append(Once())
            return j
        if wl == "stop":
            out.append(Stop())
            return j
        if wl == "level":
            lvl, j2 = num("level")
            out.append(Level(lvl))
            return j2
        dur, used = self.duration(text, j, line, col0)
        out.append(Play(w, dur))
        return j + used


def _comment_text(raw):
    s = raw.strip()[2:]
    return s[1:] if s.startswith(" ") else s


def from_mml(text, lib=None):
    """MML text -> Song.  Raises MMLError (with .line / .col) on a mistake, exactly as `parse` does."""
    lib = lib or mml.builtin_library()
    mml.parse(text, lib)                               # the authority on what is a mistake
    hdr = mml._Parser(text, lib, header_only=True).run()
    song = Song(title=hdr.title, author=hdr.author, comment=hdr.comment, bpm=hdr.bpm, time=hdr.time,
                detune=hdr.detune, once=hdr.once, echo=hdr.echo, reverb=hdr.reverb,
                instruments={k: replace(v) for k, v in hdr.instruments.items()},
                fx={k: list(v) for k, v in hdr._fxsets.items()}, keys=dict(hdr._keys),
                samples=dict(hdr._sampledefs), warnings=list(hdr.warnings))
    patterns = dict(hdr._patterns)
    tok = _Tok(song, patterns, lib)

    pending, seen_voice = [], False
    section = None
    for n, raw in enumerate(mml._blank_comments(text).split("\n"), 1):
        if raw.strip().startswith("//"):
            pending.append(_comment_text(raw))
            continue
        line = raw.split("//", 1)[0]
        if not line.strip():
            continue
        m = SECTION.match(line)
        if m:
            song.notes += pending
            pending = []
            continue
        vm = VOICE_LABEL.match(line)
        if not vm:
            song.notes += pending
            pending = []
            continue
        label = vm.group(1).upper()
        name = label if label[0] == "D" else "V" + label[1]
        dac = label[0] == "D"
        items = song.tracks[name]
        items.extend(Comment(t) for t in pending)
        pending = []
        items.extend(tok.tokens(vm.group(2), n, vm.start(2) + 1, dac))
    song.footer = pending

    for name, (body, pline, pcol) in patterns.items():        # patterns nobody calls
        if name in song.clips:
            continue
        for dac in (False, True):
            try:
                tok.clip(name, dac, pline, pcol)
                break
            except MMLError:
                song.clips.pop(name, None)
        else:
            song.clips[name] = Clip(name, [], False, raw=body)
    song.clips = {k: song.clips[k] for k in [*patterns, *song.clips] if k in song.clips}
    return song


# ------------------------------------------------------------------ Song -> MML
def _length_table():
    table = {}
    for n in DURATIONS:
        for dots in range(3):
            for trip in (False, True):
                t = explicit_ticks(n, dots, trip)
                cost = dots + (2 if trip else 0)
                txt = "%d%s%s" % (n, "." * dots, ";" if trip else "")
                if t not in table or cost < table[t][0]:
                    table[t] = (cost, txt)
    return table


_COINS = _length_table()
_BIG = max(_COINS)
_LIMIT = 1000
_BEST = [None] * (_LIMIT + 1)
_BEST[0] = (0, 0, ())
for _t in range(1, _LIMIT + 1):
    for _coin, (_cost, _txt) in _COINS.items():
        _prev = _BEST[_t - _coin] if _coin <= _t else None
        if _prev is not None:
            _cand = (_prev[0] + 1, _prev[1] + _cost, _prev[2] + ((_coin, _txt),))
            if _BEST[_t] is None or _cand[:2] < _BEST[_t][:2]:
                _BEST[_t] = _cand


def length_pieces(ticks):
    """A length in ticks as the fewest MML length tokens that add up to it: ['4', '16'] ...  Raises SongError for
    a length no tokens can make (1 tick, 5 ticks ...)."""
    out = []
    while ticks > _LIMIT:
        out.append(_COINS[_BIG][1])
        ticks -= _BIG
    best = _BEST[ticks] if ticks >= 0 else None
    if best is None:
        raise SongError("a length of %d ticks cannot be written in MML" % ticks)
    out.extend(txt for _, txt in sorted(best[2], reverse=True))
    return out


def _dur(dur):
    """(first token text, [more pieces]) for a length field."""
    if dur is None:
        return "", []
    if isinstance(dur, Rel):
        return "." * dur.dots + (";" if dur.trip else ""), []
    pieces = length_pieces(dur)
    return pieces[0], pieces[1:]


def _acc(n):
    return "#" * n if n > 0 else "-" * -n


_DEFAULT = Instrument()


def instrument_text(name, i):
    """`name wave=pulse pw=25 ...`, only what differs from a fresh Instrument."""
    d, parts = _DEFAULT, []
    for k in ("wave", "pw", "a", "d", "s", "r", "vol", "pan", "nmode"):
        if getattr(i, k) != getattr(d, k):
            parts.append("%s=%s" % (k, getattr(i, k)))
    if (i.vib_rate, i.vib_depth) != (d.vib_rate, d.vib_depth):
        parts.append("vib=%d,%d" % (i.vib_rate, i.vib_depth))
    if i.slide != d.slide:
        parts.append("slide=%d" % i.slide)
    if i.arp:
        parts.append("arp=" + ",".join(str(x) for x in i.arp))
    if i.arprate != d.arprate:
        parts.append("arprate=%d" % i.arprate)
    if (i.pws_from, i.pws_to, i.pws_rate) != (0, 0, 0):
        parts.append("pws=%d,%d,%d" % (i.pws_from, i.pws_to, i.pws_rate))
    if i.filt:
        parts.append("filter=%s,%d,%d" % (i.filt, i.filt_cut, i.filt_res))
    if (i.fs_from, i.fs_to, i.fs_rate) != (0, 0, 0):
        parts.append("fsweep=%d,%d,%d" % (i.fs_from, i.fs_to, i.fs_rate))
    if i.send != d.send:
        parts.append("send=%d" % i.send)
    return "%s %s" % (name, " ".join(parts) if parts else "wave=%s" % i.wave)


def echo_text(echo):
    secs, fb, mix, wide = echo
    return "echo %dms,%d,%d%s" % (round(secs * 1000), round(fb * 100), round(mix * 100), ",wide" if wide else "")


def _comment_lines(text, prefix="// "):
    return [(prefix + t).rstrip() for t in (text.split("\n") if text else [""])]


def _event_tokens(e, nested):
    """The MML tokens for one event."""
    if isinstance(e, (Note, Degree)):
        first, more = _dur(e.dur)
        if isinstance(e, Note):
            head = e.letter + _acc(e.acc) + first
        else:
            head = "%d%s" % (e.degree, _acc(e.acc)) + (":" + first if first else "")
        return [head + "".join("&" + p for p in more)]
    if isinstance(e, Rest):
        if isinstance(e.dur, int):
            return ["r" + p for p in length_pieces(e.dur)]
        return ["r" + _dur(e.dur)[0]]
    if isinstance(e, Play):
        if isinstance(e.dur, int):
            pieces = length_pieces(e.dur)
            return [e.sample + pieces[0]] + ["r" + p for p in pieces[1:]]
        return [e.sample + _dur(e.dur)[0]]
    if isinstance(e, Length):
        pieces = length_pieces(e.ticks)
        if len(pieces) != 1:
            raise SongError("a default length of %d ticks cannot be written in MML" % e.ticks)
        return ["l" + pieces[0]]
    if isinstance(e, Octave):
        out = []
        if e.set is not None:
            out.append("o%d%s" % (e.set, NOTE_NAMES[e.boundary % 12] if e.boundary is not None else ""))
        elif e.boundary is not None:
            raise SongError("an octave boundary needs an octave")
        out.extend([">" if e.shift > 0 else "<"] * abs(e.shift))
        return out
    if isinstance(e, Volume):
        if e.value is not None:
            return ["v%d" % e.value]
        return ["v%s%s" % ("+" if e.delta >= 0 else "-", abs(e.delta) if abs(e.delta) != 1 else "")]
    if isinstance(e, Tempo):
        return ["t%d" % e.bpm]
    if isinstance(e, Tie):
        return ["&"]
    if isinstance(e, Extend):
        return ["&" + "&".join(length_pieces(e.ticks))]
    if isinstance(e, Select):
        return ["@" + e.name]
    if isinstance(e, Fx):
        return ["fx", e.name]
    if isinstance(e, Tweak):
        return [e.word, e.arg]
    if isinstance(e, Key):
        if e.tonic is None:
            return ["key", "off"]
        return ["key", NOTE_NAMES[e.tonic % 12], e.scale]
    if isinstance(e, Bar):
        return ["|"]
    if isinstance(e, Loop):
        return ["loop"]
    if isinstance(e, Once):
        return ["once"]
    if isinstance(e, Stop):
        return ["stop"]
    if isinstance(e, Level):
        return ["level", str(e.level)]
    if isinstance(e, Call):
        return ["!" + e.name]
    if isinstance(e, Comment):
        return ["/* %s */" % e.text.replace("*/", "* /").replace("\n", " ")]
    if isinstance(e, Repeat):
        return ["[" + " ".join(_join(_event_stream(e.items, True))) + "]%d" % e.count]
    raise SongError("cannot write %r" % (e,))


def _event_stream(items, nested):
    out = []
    for e in items:
        out.extend(_event_tokens(e, nested))
    return out


def _join(tokens):
    """Join tokens with spaces, except that a held note's `&8` follows its note directly.  `fx name`, `vib 5,3`
    pairs stay together because they are written as two tokens separated by one space."""
    out = []
    for t in tokens:
        if out and t.startswith("&") and len(t) > 1:
            out[-1] += t
        else:
            out.append(t)
    return out


def to_mml(song):
    """Song -> MML text (deterministic: the same song always gives the same text)."""
    out = []
    for t in song.notes:
        out.extend(_comment_lines(t))
    for key in ("title", "author", "comment"):
        if getattr(song, key):
            out.append("%s %s" % (key, getattr(song, key)))
    out.append("tempo %d" % song.bpm)
    out.append("time %d/%d" % song.time)
    if song.detune:
        out.append("tune %d" % song.detune)
    if song.echo:
        out.append(echo_text(song.echo))
    if song.reverb:
        out.append("reverb %d" % song.reverb)
    if song.once:
        out.append("once")

    def section(name, lines):
        if lines:
            out.extend(["", "[%s]" % name] + lines)
    section("instr", [instrument_text(k, v) for k, v in song.instruments.items()])
    section("fx", ["%s %s" % (k, " ".join("%s=%s" % kv for kv in pairs)) for k, pairs in song.fx.items()])
    section("key", ["%s = %s" % (k, " ".join(str(x) for x in steps)) for k, steps in song.keys.items()])
    section("sample", ["%s = %s%s" % (k, f, " rate=%d" % r if r is not None else "") for k, (f, r) in song.samples.items()])
    section("pat", ["%s = %s" % (c.name, c.raw if c.raw is not None else " ".join(_join(_event_stream(c.items, True))))
                    for c in song.clips.values()])

    for name in TRACKS:
        items = song.tracks.get(name) or []
        if not items:
            continue
        out.append("")
        cur, size, prev = [], 0, None

        def flush():
            nonlocal cur, size
            if cur:
                out.append("%s: %s" % (name, " ".join(_join(cur))))
            cur, size = [], 0
        for e in items:
            if isinstance(e, Comment):
                flush()
                out.extend(_comment_lines(e.text))
                prev = None
                continue
            toks = _event_tokens(e, False)
            if prev == "&" and toks and toks[0][:1].isdigit():
                raise SongError("a tie cannot go into a scale degree in MML")
            cur.extend(toks)
            size += sum(len(t) + 1 for t in toks)
            prev = toks[-1] if toks else prev
            if size > 100:
                flush()
        flush()
    for t in song.footer:
        out.extend(_comment_lines(t))
    return "\n".join(out) + "\n"
