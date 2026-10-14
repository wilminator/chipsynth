# Copyright (C) 2026 Michael Allen Wilmes
"""Standard MIDI files in and out (format 0 / 1 read, format 1 written).

    data = to_midi(tune)               # a Tune -> .mid bytes: a tempo track, then one track per voice that has notes
    song = from_midi(data)             # .mid bytes -> a Song (the first four tracks / channels with notes)

Our time is 96 ticks to a quarter note, which is exactly the file's division on the way out.  Coming in, times are
rounded to 1/32 notes (12 ticks) so every length can be written as MML.  Drums (channel 10) are left out, and a voice is one
note at a time: where notes overlap the higher one wins and the lower is cut.
"""
import struct

from .song import Song, Note, Rest, Octave, Tempo, SongError

PPQ = 96
GRID = 12
LETTERS = (("c", 0), ("c", 1), ("d", 0), ("d", 1), ("e", 0), ("f", 0), ("f", 1), ("g", 0), ("g", 1), ("a", 0), ("a", 1), ("b", 0))


class MidiError(ValueError):
    pass


def _varlen(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _chunk(tag, body):
    return tag + struct.pack(">I", len(body)) + body


def _track(events):
    """events: [(tick, order, bytes)] -> a track chunk (sorted by time, note-offs before note-ons)."""
    body, last = bytearray(), 0
    for tick, _, data in sorted(events, key=lambda e: (e[0], e[1])):
        body += _varlen(tick - last) + data
        last = tick
    body += _varlen(0) + b"\xff\x2f\x00"
    return _chunk(b"MTrk", bytes(body))


def to_midi(tune):
    """A Tune as a format 1 MIDI file."""
    us = lambda bpm: int(round(60_000_000 / bpm))
    num, den = tune.time
    meta = [(0, 0, b"\xff\x03" + bytes([len(b"tempo")]) + b"tempo"),
            (0, 1, b"\xff\x58\x04" + bytes([num, max(0, den.bit_length() - 1), 24, 8])),
            (0, 2, b"\xff\x51\x03" + us(tune.bpm).to_bytes(3, "big"))]
    for tick, bpm in tune.tempo_events:
        meta.append((int(tick), 2, b"\xff\x51\x03" + us(bpm).to_bytes(3, "big")))
    tracks = [_track(meta)]
    for vi, voice in enumerate(tune.voices):
        if not voice:
            continue
        name = ("V%d" % (vi + 1)).encode()
        ev = [(0, 0, b"\xff\x03" + bytes([len(name)]) + name)]
        for n in voice:
            vel = max(1, min(127, round(n.vol * 127 / 15))) if n.vol else 1
            ev.append((int(n.start), 2, bytes([0x90 | vi, n.pitch, vel])))
            ev.append((int(n.start + n.dur), 1, bytes([0x80 | vi, n.pitch, 0])))
        tracks.append(_track(ev))
    head = struct.pack(">HHH", 1, len(tracks), PPQ)
    return _chunk(b"MThd", head) + b"".join(tracks)


# ------------------------------------------------------------------ reading
def _read_var(data, i):
    n = 0
    for _ in range(4):
        b = data[i]
        i += 1
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            return n, i
    raise MidiError("bad variable-length number")


def parse(data):
    """-> (division, [track events]) where an event is (tick, kind, a, b, channel); kinds: on, off, tempo, time."""
    try:
        if data[:4] != b"MThd":
            raise MidiError("not a MIDI file")
        hlen = struct.unpack(">I", data[4:8])[0]
        fmt, ntrk, div = struct.unpack(">HHH", data[8:14])
        if div & 0x8000:
            raise MidiError("MIDI files timed in SMPTE frames are not supported")
        i = 8 + hlen
        tracks = []
        while i + 8 <= len(data) and len(tracks) < ntrk:
            tag, size = data[i:i + 4], struct.unpack(">I", data[i + 4:i + 8])[0]
            body = data[i + 8:i + 8 + size]
            i += 8 + size
            if tag != b"MTrk":
                continue
            tracks.append(_parse_track(body))
        return div, tracks
    except (IndexError, struct.error):
        raise MidiError("the MIDI file is cut short or damaged")


def _parse_track(body):
    ev, i, tick, status = [], 0, 0, 0
    while i < len(body):
        delta, i = _read_var(body, i)
        tick += delta
        b = body[i]
        if b == 0xFF:
            kind, i = body[i + 1], i + 2
            ln, i = _read_var(body, i)
            payload = body[i:i + ln]
            i += ln
            if kind == 0x51 and ln == 3:
                ev.append((tick, "tempo", int.from_bytes(payload, "big"), 0, 0))
            elif kind == 0x58 and ln >= 2:
                ev.append((tick, "time", payload[0], 1 << payload[1], 0))
            elif kind == 0x2F:
                break
            continue
        if b in (0xF0, 0xF7):
            ln, i = _read_var(body, i + 1)
            i += ln
            continue
        if b & 0x80:
            status, i = b, i + 1
        hi, ch = status & 0xF0, status & 0x0F
        if hi in (0xC0, 0xD0):
            i += 1
        else:
            a, c = body[i], body[i + 1]
            i += 2
            if hi == 0x90 and c:
                ev.append((tick, "on", a, c, ch))
            elif hi == 0x80 or hi == 0x90:
                ev.append((tick, "off", a, 0, ch))
    return ev


def from_midi(data):
    """MIDI bytes -> Song (raises MidiError if it cannot be read)."""
    div, tracks = parse(data)
    scale = PPQ / div
    q = lambda t: int(round(t * scale / GRID)) * GRID
    voices, drums = [], 0
    bpm, time, tempos = None, None, []
    for ev in tracks:
        open_, notes = {}, {}
        for tick, kind, a, b, ch in ev:
            if kind == "tempo":
                t = round(60_000_000 / max(1, a))
                if bpm is None and tick == 0:
                    bpm = t
                else:
                    tempos.append((q(tick), t))
            elif kind == "time" and time is None:
                time = (a, b)
            elif kind == "on" and ch != 9:
                open_[(ch, a)] = tick
            elif kind == "off" and ch != 9 and (ch, a) in open_:
                notes.setdefault(ch, []).append((open_.pop((ch, a)), tick, a))
            elif kind in ("on", "off") and ch == 9:
                drums += 1
        for ch, ns in sorted(notes.items()):
            voices.append(sorted(ns))
    voices = [v for v in voices if v]
    song = Song()
    song.bpm = max(30, min(300, bpm or 120))
    if time and 1 <= time[0] <= 32 and time[1] in (1, 2, 4, 8, 16, 32):
        song.time = time
    if len(voices) > 4:
        song.warnings.append("the file has %d parts with notes: only the first four were used" % len(voices))
    if drums:
        song.warnings.append("the drum part was left out")
    for k, ns in enumerate(voices[:4]):
        song.tracks["V%d" % (k + 1)] = _voice_events(ns, q, tempos if k == 0 else [])
    if not voices:
        raise MidiError("the file has no notes")
    return song


def _voice_events(notes, q, tempos):
    """[(start, end, pitch)] in file ticks -> events: one note at a time (the higher wins), rests between."""
    mono = []
    for s, e, p in notes:
        s, e = q(s), max(q(e), q(s) + GRID)
        if mono and s < mono[-1][1]:
            if s == mono[-1][0]:
                if p > mono[-1][2]:
                    mono[-1] = (s, e, p)
                continue
            mono[-1] = (mono[-1][0], s, mono[-1][2])
        mono.append((s, e, p))
    out, pos, octave = [], 0, 4
    tempos = sorted(tempos)

    def rest_to(t):
        nonlocal pos
        while tempos and tempos[0][0] <= t:
            at, bpm = tempos.pop(0)
            if at > pos:
                out.append(Rest(at - pos))
                pos = at
            out.append(Tempo(max(30, min(300, bpm))))
        if t > pos:
            out.append(Rest(t - pos))
            pos = t

    for s, e, p in mono:
        rest_to(s)
        if p // 12 != octave:
            octave = p // 12
            out.append(Octave(set=octave))
        letter, acc = LETTERS[p % 12]
        out.append(Note(letter, acc, e - s))
        pos = e
    return out
