# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""FamiStudio binary projects (.fms) -> the same model the text importer reads, then the same conversion.

Written from the format as FamiStudio's own reader defines it (Source/IO/ProjectFile.cs, Source/Project/*.cs,
Source/Utils/Compression.cs; MIT licensed, read only, no code copied).

    "FMS!" (u32 0x21534D46, little endian) | i32 project format version | i32 size of the payload | raw deflate payload

The payload is one flat serialization of the project: little endian numbers, a string is an i32 byte count (-1 = empty)
and UTF-16 text, an array is an i32 count and its items, a reference to a song, instrument, arpeggio, sample or pattern is
the i32 id of the thing (-1 = none).  The layout changes with the format version, so only the versions whose branches
were read from the source are accepted:

    15 = FamiStudio 4.1   16 = 4.2   17 = 4.3   18 = 4.4   19 = 4.5

Older projects (before 4.1) and newer ones are refused with a message saying what to do, never guessed at.  Open the file in
a current FamiStudio and save it, or export FamiStudio text, which chipsynth reads for any 4.x.

What carries over is what the text importer carries over (it does the conversion): this module only rebuilds that
importer's `Project` (the same attributes the text file would have held) from the bytes.  Two things differ from text.
A DPCM sample in a project keeps its source (a WAV, or DMC data with processing options) and FamiStudio makes the final
bytes with its own encoder when it opens the file.  Only the plain case (DMC data, no volume, rate, pitch, trim or
envelope changes) is rebuilt here, with the bit reverse and padding FamiStudio applies; any other sample is left out
and reported (FamiStudio's text export writes the processed data for those).  The project's export settings, folders,
mixer settings and colours are read past and dropped.
"""
import struct
import zlib

from .famistudio_text import (FChannel, FSong, FamiStudioError, NOTE_NAMES, Project, load_project)
from .report import Report

MAGIC = b"FMS!"
OLDEST, NEWEST = 15, 19
RELEASES = {15: "4.1", 16: "4.2", 17: "4.3", 18: "4.4", 19: "4.5"}
OLD_RELEASES = {1: "1.0", 2: "1.1", 3: "1.2", 4: "1.4", 5: "2.0", 6: "2.1", 7: "2.2", 8: "2.3", 9: "2.4", 10: "3.0",
                11: "3.1", 12: "3.2", 13: "3.3", 14: "4.0"}
MAX_PAYLOAD = 1 << 28
MAX_SONG_LENGTH = 256
MAX_SAMPLE_SIZE = (255 << 4) + 1          # DPCMSample.MaxSampleSize

EXPANSIONS = ("", "VRC6", "VRC7", "FDS", "MMC5", "N163", "S5B", "EPSM")
ENVELOPES = ("Volume", "Arpeggio", "Pitch", "DutyCycle", "FDSWave", "FDSMod", "N163Wave", "Repeat", "MixerSettings", "NoiseFreq")
CHANNELS = (("Square1", "Square2", "Triangle", "Noise", "DPCM", "VRC6Square1", "VRC6Square2", "VRC6Saw")
            + tuple("VRC7FM%d" % i for i in range(1, 7)) + ("FDS", "MMC5Square1", "MMC5Square2", "MMC5DPCM")
            + tuple("N163Wave%d" % i for i in range(1, 9)) + tuple("S5BSquare%d" % i for i in range(1, 4))
            + tuple("EPSMSquare%d" % i for i in range(1, 4)) + tuple("EPSMFM%d" % i for i in range(1, 7))
            + tuple("EPSMRythm%d" % i for i in range(1, 7)))
CHANNEL_COUNTS = (3, 6, 1, 2, None, 3, 15)           # VRC6 VRC7 FDS MMC5 N163 S5B EPSM (N163: the project's count)

# effect bits of a note (Note.Effect*): mask bit -> the text attribute name(s)
FX_VOLUME, FX_VIB_SPEED, FX_VIB_DEPTH, FX_FINE, FX_SPEED, FX_FDS_DEPTH, FX_FDS_SPEED, FX_DUTY = range(8)
FX_NOTE_DELAY, FX_CUT_DELAY, FX_VOL_SLIDE, FX_DELTA, FX_PHASE, FX_ENV_PERIOD = 8, 9, 10, 11, 12, 13


def looks_like(data):
    """True when the bytes start with the FamiStudio project magic."""
    return bytes(data[:4]) == MAGIC


def _cdiv(a, b):
    """C# integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


class _Reader:
    """Little endian reads over the decompressed payload, as the project buffer reads them."""

    def __init__(self, buf, version):
        self.buf, self.pos, self.v = buf, 0, version

    def take(self, n):
        if n < 0 or self.pos + n > len(self.buf):
            raise FamiStudioError("the project ends in the middle of its data: the file is truncated or corrupted")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def unpack(self, fmt, n):
        return struct.unpack("<" + fmt, self.take(n))[0]

    def bool(self):
        return self.take(1)[0] != 0

    def u8(self):
        return self.take(1)[0]

    def i8(self):
        return self.unpack("b", 1)

    def u16(self):
        return self.unpack("H", 2)

    def i16(self):
        return self.unpack("h", 2)

    def i32(self):
        return self.unpack("i", 4)

    def f32(self):
        return self.unpack("f", 4)

    def skip(self, n):
        self.take(n)

    def count(self, each=1):
        """An item count that must fit in what is left of the payload."""
        n = self.i32()
        if n < 0 or n * each > len(self.buf) - self.pos:
            raise FamiStudioError("the project has an impossible item count (%d): it is corrupted" % n)
        return n

    def string(self):
        n = self.i32()
        if n < 0:
            return ""
        return self.take(n).decode("utf-16-le", errors="replace").replace("\0", "")

    def bytes(self):
        return bytes(self.take(self.count()))

    def sbytes(self):
        return [b - 256 if b > 127 else b for b in self.take(self.count())]

    def shorts(self):
        n = self.i32()
        if n < 0:
            return None
        if n * 2 > len(self.buf) - self.pos:
            raise FamiStudioError("the project has an impossible array size (%d): it is corrupted" % n)
        return list(struct.unpack("<%dh" % n, self.take(n * 2)))

    def ints(self):
        """FamiStudio writes 32 bit items but reads the low 16 bits of each (ProjectLoadBuffer.Serialize(ref int[]));
        the values it keeps there (grooves) are small, so this reads them the same way."""
        n = self.i32()
        if n < 0:
            return None
        if n * 4 > len(self.buf) - self.pos:
            raise _corrupt(n)
        return [struct.unpack_from("<h", self.take(4))[0] for _ in range(n)]


def _corrupt(n):
    return FamiStudioError("the project has an impossible array size (%d): it is corrupted" % n)


# ------------------------------------------------------------------ the file
def decompress(data):
    """The payload of a .fms file (after checking the header)."""
    data = bytes(data)
    if len(data) < 12 or not looks_like(data):
        raise FamiStudioError("this is not a FamiStudio project (it does not start with FMS!)")
    version = struct.unpack_from("<i", data, 4)[0]
    if version > NEWEST:
        raise FamiStudioError("this project is format version %d, newer than the newest this importer reads (%d, FamiStudio %s): "
                              "open it in the FamiStudio that made it and use File > Export > FamiStudio Text, "
                              "which chipsynth reads" % (version, NEWEST, RELEASES[NEWEST]))
    if version < OLDEST:
        what = " (FamiStudio %s)" % OLD_RELEASES[version] if version in OLD_RELEASES else ""
        raise FamiStudioError("this project is format version %d%s, older than the oldest this importer reads (%d, FamiStudio %s): "
                              "open it in a current FamiStudio and save it again, or use File > Export > FamiStudio Text"
                              % (version, what, OLDEST, RELEASES[OLDEST]))
    size = struct.unpack_from("<i", data, 8)[0]
    if size <= 0 or size > MAX_PAYLOAD:
        raise FamiStudioError("the project says its data is %d bytes: it is corrupted" % size)
    inflater = zlib.decompressobj(-15)                       # .NET DeflateStream: raw deflate, no zlib header
    try:
        payload = inflater.decompress(data[12:], size)
    except zlib.error as e:
        raise FamiStudioError("the project's data does not decompress (%s): the file is corrupted" % e)
    if len(payload) < size:
        raise FamiStudioError("the project's data is shorter than it says (%d of %d bytes): the file is truncated" % (len(payload), size))
    return version, payload


def parse(data):
    """Read a .fms file into the importer's Project (raises FamiStudioError for anything it cannot read)."""
    version, payload = decompress(data)
    r = _Reader(payload, version)
    try:
        prj = _project(r)
    except (struct.error, IndexError, UnicodeError) as e:
        raise FamiStudioError("the project could not be read (%s): the file is corrupted" % e)
    prj.format_version = version
    if not prj.songs:
        raise FamiStudioError("the project has no songs")
    return prj


def song_names(data):
    return [s.attrs.get("Name", "Song %d" % (i + 1)) for i, s in enumerate(parse(data).songs)]


def load(data, song=None, region=None):
    """A .fms file -> famistudio_text.Result (songs, report), by the text importer's own conversion."""
    prj = parse(data)
    report = Report()
    report.note("FamiStudio project file, format version %d (FamiStudio %s)" % (prj.format_version, RELEASES[prj.format_version]))
    return load_project(prj, song=song, region=region, report=report)


# ------------------------------------------------------------------ the project
def _skip_song_list(r):
    for _ in range(r.count(5)):
        r.skip(5)                                            # SongListExportSettings: id, enabled


def _skip_export_settings(r):
    """Project.Serialize at version 19: twelve export configurations, none of which matter here."""
    s = r.string
    r.skip(4)                                                # audio: song id
    for _ in range(4):
        s()                                                  # format, sample rate, bit rate, loop mode
    r.skip(4 * 3 + 3)                                        # loop count, duration, delay; separate files, separate intro, stereo
    _skip_export_channels(r)
    r.skip(4)                                                # video: song id
    for _ in range(5):
        s()                                                  # mode, resolution, frame rate, audio and video bit rate
    r.skip(4 * 5)                                            # loop count, delay, osc columns, window, thickness
    for _ in range(3):
        s()                                                  # osc colour, piano roll width, zoom
    r.skip(4)                                                # piano roll rows
    s()                                                      # perspective
    r.skip(2)                                                # overlay registers, stereo
    _skip_export_channels(r)
    for _ in range(5):
        s()                                                  # nsf: name, artist, copyright, format, mode
    _skip_song_list(r)
    for _ in range(4):
        s()                                                  # rom/fds: type, name, artist, mode
    _skip_song_list(r)
    r.skip(4 + 2 + 4)                                        # midi: song id, two flags, pitch wheel range
    s()
    for _ in range(r.count(16)):
        r.skip(16)                                           # per instrument: song id, mode, type id, index
    r.skip(4)                                                # vgm: song id
    for _ in range(7):
        s()                                                  # track title, game, system, composer, date, vgm by, notes
    r.skip(1)                                                # smooth loop
    r.skip(1)                                                # famistudio text: delete unused data
    _skip_song_list(r)
    _skip_song_list(r)                                       # famitracker text
    _skip_music_config(r)                                    # famistudio music
    _skip_sfx_config(r)                                      # famistudio sfx
    _skip_music_config(r)                                    # famitone2 music
    _skip_sfx_config(r)                                      # famitone2 sfx


def _skip_export_channels(r):
    for _ in range(r.count(21)):
        r.skip(4 + 4 + 1 + 4 + 4 + 4)                        # song id, type, enabled, panning, transpose, trigger


def _skip_music_config(r):
    r.string()
    r.skip(1)
    for _ in range(3):
        r.string()
    r.skip(2)
    _skip_song_list(r)


def _skip_sfx_config(r):
    r.string()
    r.string()
    r.skip(1)
    _skip_song_list(r)


def _project(r):
    v = r.v
    r.skip(4)                                                # next unique id
    r.skip(4)                                                # sort songs, instruments, samples, arpeggios (v15+)
    name, author, copyright_ = r.string(), r.string(), r.string()
    mask = r.i32()
    n163 = r.i32()
    tempo = r.i32()
    pal = r.bool()
    if not 0 <= mask < 128:
        raise FamiStudioError("the project's expansion audio mask (%d) is not valid: it is corrupted" % mask)
    if tempo not in (0, 1):
        raise FamiStudioError("the project's tempo mode (%d) is not valid: it is corrupted" % tempo)
    if v >= 19:
        _skip_export_settings(r)
    tuning = r.i32() if v >= 17 else 440
    if v >= 16:
        for _ in range(r.count(9)):
            r.skip(4)
            r.string()
            r.skip(1)                                        # folders: type, name, expanded
        r.skip(3)                                            # sound engine options
        override_bass = r.bool()
        if override_bass:
            r.skip(4)
        overrides = r.i32()
        for i in range(8):
            if overrides & (1 << i):
                r.skip(4 + 4 + 4 + (4 if v >= 18 else 0))    # mixer: volume dB, treble dB, rolloff, (bass cutoff)
    attrs = {"TempoMode": "FamiTracker" if tempo else "FamiStudio", "Name": name, "Author": author, "Copyright": copyright_}
    if pal:
        attrs["PAL"] = "true"
    if tuning != 440:
        attrs["Tuning"] = str(tuning)
    expansions = [EXPANSIONS[i] for i in range(1, 8) if mask & (1 << (i - 1))]
    if expansions:
        attrs["Expansions"] = ",".join(expansions)
        if mask & 16:
            attrs["NumN163Channels"] = str(n163)
    prj = Project(attrs=attrs)
    channel_count = 5 + sum(CHANNEL_COUNTS[i] if CHANNEL_COUNTS[i] is not None else n163 for i in range(7) if mask & (1 << i))
    sample_names = _samples(r, prj)
    inst_names = _instruments(r, prj, sample_names)
    arp_names = _arpeggios(r, prj)
    for _ in range(r.count(20)):
        _song(r, prj, channel_count, inst_names, arp_names)
    return prj


def _unique(name, used, default):
    base = name or default
    out, n = base, 1
    while out in used:
        n += 1
        out = "%s (%d)" % (base, n)
    used.add(out)
    return out


def _samples(r, prj):
    """DPCM samples.  Returns {id: name}; the samples this module can rebuild go in prj.samples, the others in prj.skipped."""
    v, ids, used = r.v, {}, set()
    for _ in range(r.count(20)):
        sid = r.i32()
        name = _unique(r.string(), used, "sample")
        ids[sid] = name
        is_wav = r.bool()
        if is_wav:
            r.skip(4)
            wav = r.shorts()
            source = None
        else:
            source = r.bytes()
        r.skip(4)                                            # colour
        if v >= 15:
            r.skip(4)                                        # DPCM bank
        if v >= 16:
            r.string()                                       # folder
        rate, _preview, volume, padding = r.i32(), r.i32(), r.i32(), r.i32()
        reverse, trim, pal_processing = r.bool(), r.bool(), r.bool()
        envelope = [(r.i32(), r.f32()) for _ in range(4)]
        r.string()                                           # source file name
        fine = r.f32()
        initial = r.i32()
        plain = (not is_wav and volume == 100 and abs(fine - 1.0) < 1e-5 and rate == 15 and not trim
                 and all(abs(vol - 1.0) <= 0.02 for _, vol in envelope))
        if not plain:
            why = "from a WAV" if is_wav else "with volume, rate, pitch or trim options"
            prj.skipped[name] = ("the DPCM sample '%s' is processed %s that FamiStudio applies with its own encoder when it opens "
                                 "the project; it is left out (File > Export > FamiStudio Text writes the processed data)" % (name, why))
            continue
        data = bytearray(source)
        if reverse:
            data = bytearray(int("{:08b}".format(b)[::-1], 2) for b in data)
        data = _pad(data, padding)
        attrs = {"Name": name}
        if initial != 32:
            attrs["DmcInitialValue"] = str(initial)
        prj.samples[name] = (bytes(data[:MAX_SAMPLE_SIZE]), attrs)
    return ids


def _pad(data, mode):
    """DPCMSample.Process's padding step (modes 1, 2, 5, 6; the round modes belong to the WAV encoder)."""
    n = len(data)
    if mode in (1, 2, 5, 6):
        if mode == 1:
            size = _cdiv(n + 15, 16) * 16
        elif mode == 2:
            size = _cdiv(n - 1 + 15, 16) * 16 + 1
        elif mode == 5:
            size = _cdiv(n, 16) * 16
        else:
            size = _cdiv(n - 1, 16) * 16 + 1
        if size != n:
            fill = 0xAA if n and data[-1] & 0x80 else 0x55
            data = data[:size] + bytearray([fill] * (size - n))
    return data


def _envelope(r):
    length, loop = r.i32(), r.i32()
    release = r.i32()
    relative = r.bool()
    values = r.sbytes()
    return length, loop, release, relative, values


def _instruments(r, prj, sample_names):
    v, ids, used = r.v, {}, set()
    for _ in range(r.count(20)):
        iid = r.i32()
        name = _unique(r.string(), used, "instrument")
        ids[iid] = name
        r.skip(4)                                            # colour
        exp = r.i32()
        if not 0 <= exp < len(EXPANSIONS):
            raise FamiStudioError("instrument '%s' has an unknown expansion (%d): the file is corrupted" % (name, exp))
        if exp == 3:                                         # FDS
            r.skip(3 + 2 + 1 + 1)                            # master volume, wave preset, mod preset, mod speed (u16), depth, delay
            if v >= 18:
                r.skip(1)                                    # wave count
            if v >= 16:
                r.skip(3)                                    # auto mod, denominator, numerator
            r.skip(4 + 4 + 1)                                # resample period, offset, normalize
            r.shorts()
        elif exp == 5:                                       # N163
            r.skip(3)
            r.skip(1)                                        # wave count
            r.skip(4 + 4 + 1)
            r.shorts()
            if v >= 17:
                r.skip(1)                                    # auto position
        elif exp == 6:                                       # S5B
            if v >= 16:
                r.skip(1 + 1 + 1 + 2)                        # auto pitch, octave, shape, period
        elif exp == 2:                                       # VRC7
            r.skip(1 + 8)
            if v >= 19:
                r.skip(2)                                    # override release and stop
        elif exp == 7:                                       # EPSM
            r.skip(1 + 31)
            if v >= 16:
                r.skip(1 + 1 + 1 + 2)
        elif exp == 1:                                       # VRC6
            r.skip(1)
        mask = r.u16()
        envs = {}
        for i in range(len(ENVELOPES)):
            if mask & (1 << i):
                length, loop, release, relative, values = _envelope(r)
                zero = 15 if i == 0 else 2 if i == 8 else 0
                resizable = i != 5
                if resizable and (length == 0 or all(x == zero for x in values[:length])):
                    continue                                 # an envelope FamiStudio would not write to text either
                a = {"Type": ENVELOPES[i], "Length": str(length)}
                if loop >= 0:
                    a["Loop"] = str(loop)
                if release >= 0:
                    a["Release"] = str(release)
                if relative:
                    a["Relative"] = "true"
                a["Values"] = ",".join(str(x) for x in values[:length])
                envs[ENVELOPES[i]] = a
        if v >= 16:
            r.string()                                       # folder
        attrs = {"Name": name}
        if exp:
            attrs["Expansion"] = EXPANSIONS[exp]
        maps = []
        if exp == 0:
            notes = [r.i32() for _ in range(r.count(4))]
            for note in notes:
                sample, loop, pitch = r.i32(), r.bool(), r.i32()
                override, initial = r.bool(), r.i32()        # v13+
                if sample in sample_names:
                    m = {"Note": _note_name(note), "Sample": sample_names[sample], "Pitch": str(pitch), "Loop": "true" if loop else "false"}
                    if override:
                        m["DmcInitialValue"] = str(initial)
                    maps.append(m)
        prj.instruments[name] = {"attrs": attrs, "envelopes": envs, "maps": maps}
    return ids


def _arpeggios(r, prj):
    v, ids, used = r.v, {}, set()
    for _ in range(r.count(20)):
        aid = r.i32()
        name = _unique(r.string(), used, "arpeggio")
        ids[aid] = name
        r.skip(4)
        if v >= 16:
            r.string()
        length, loop, _release, _relative, values = _envelope(r)
        a = {"Name": name, "Length": str(length)}
        if loop >= 0:
            a["Loop"] = str(loop)
        a["Values"] = ",".join(str(x) for x in values[:length])
        prj.arpeggios[name] = a
    return ids


def _note_name(value):
    if value == 0:
        return "Stop"
    if value == 0x80:
        return "Release"
    if value == 0xFF:
        return ""
    return NOTE_NAMES[(value - 1) % 12] + str((value - 1) // 12)


def _song(r, prj, channel_count, inst_names, arp_names):
    v = r.v
    r.skip(4)                                                # id
    pattern_len, length, beat_len = r.i32(), r.i32(), r.i32()
    name = r.string()
    ft_tempo, ft_speed = r.i32(), r.i32()
    r.skip(4)                                                # colour
    loop = r.i32()
    note_len = r.i32()
    groove = r.ints()
    if v >= 19:
        r.skip(4)                                            # groove padding mode
    if not 0 <= length <= MAX_SONG_LENGTH:
        raise _bad_length(name, length)
    famitracker = prj.attrs["TempoMode"] == "FamiTracker"
    if not famitracker and note_len <= 0:
        raise FamiStudioError("song '%s' has a note length of %d: the file is corrupted" % (name, note_len))
    attrs = {"Name": name, "Length": str(length), "LoopPoint": str(loop)}
    if famitracker:
        attrs.update(PatternLength=str(pattern_len), BeatLength=str(beat_len), FamiTrackerTempo=str(ft_tempo), FamiTrackerSpeed=str(ft_speed))
    else:
        attrs.update(PatternLength=str(_cdiv(pattern_len, note_len)), BeatLength=str(_cdiv(beat_len, note_len)), NoteLength=str(note_len),
                     Groove="-".join(str(g) for g in (groove or [note_len])))
    song = FSong(attrs=attrs)
    for i in range(length):
        use, plen, nlen, blen = r.bool(), r.i32(), r.i32(), r.i32()
        cgroove = r.ints()                                   # v10+
        r.skip(4)                                            # its groove padding mode
        if not use:
            continue
        if famitracker:
            song.custom[i] = {"Time": str(i), "Length": str(plen)}
        else:
            if nlen <= 0:
                raise FamiStudioError("song '%s' pattern %d has a note length of %d: the file is corrupted" % (name, i + 1, nlen))
            song.custom[i] = {"Time": str(i), "Length": str(_cdiv(plen, nlen)), "NoteLength": str(nlen),
                              "Groove": "-".join(str(g) for g in (cgroove or [nlen])), "BeatLength": str(_cdiv(blen, nlen))}
    if v >= 16:
        r.string()                                           # folder
    for _ in range(channel_count):
        song.channels.append(_channel(r, inst_names, arp_names))
    prj.songs.append(song)


def _bad_length(name, length):
    return FamiStudioError("song '%s' has %d patterns (the most FamiStudio makes is %d): the file is corrupted" % (name, length, MAX_SONG_LENGTH))


def _channel(r, inst_names, arp_names):
    r.skip(4)                                                # song id
    n = r.count(8)
    ctype = r.i32()
    if not 0 <= ctype < len(CHANNELS):
        raise FamiStudioError("a channel has an unknown type (%d): the file is corrupted" % ctype)
    ch = FChannel(type=CHANNELS[ctype])
    by_id, used = {}, set()
    for _ in range(n):
        pid = r.i32()
        name = _unique(r.string(), used, "Pattern")
        r.skip(12)                                           # channel type, colour, song id
        by_id[pid] = name
        notes = ch.patterns.setdefault(name, [])
        for _ in range(r.count(2 + 1 + 1 + 2)):
            time = r.i16()
            note = _note(r, time, inst_names, arp_names)
            if note is not None:
                notes.append(note)
    for p in range(256):
        pid = r.i32()
        if pid in by_id:
            ch.instances[p] = by_id[pid]
    return ch


def _note(r, time, inst_names, arp_names):
    """One note, as the attributes FamiStudio's text export writes for it (None when it holds nothing)."""
    v = r.v
    val, flags = r.u8(), r.u8()
    musical = val not in (0, 0x80, 0xFF)
    slide, inst, duration, release = 0, -1, 0, 0
    if musical:                                              # v10+: slide and instrument only on musical notes
        slide = r.u8()
        inst = r.i32()
        duration, release = r.u16(), r.u16()
    mask = r.u16()
    fx = {}
    if mask & (1 << FX_VOLUME):
        fx["Volume"] = r.u8()
    if mask & ((1 << FX_VIB_SPEED) | (1 << FX_VIB_DEPTH)):
        vib = r.u8()
        fx["VibratoSpeed"], fx["VibratoDepth"] = vib >> 4, vib & 15
    if mask & (1 << FX_SPEED):
        fx["Speed"] = r.u8()
    if mask & (1 << FX_FINE):
        fx["FinePitch"] = r.i8()
    if mask & (1 << FX_FDS_SPEED):
        fx["FdsModSpeed"] = r.u16()
    if mask & (1 << FX_FDS_DEPTH):
        fx["FdsModDepth"] = r.u8()
    if mask & (1 << FX_DUTY):
        fx["DutyCycle"] = r.u8()
    if mask & (1 << FX_NOTE_DELAY):
        fx["NoteDelay"] = r.u8()
    if mask & (1 << FX_CUT_DELAY):
        fx["CutDelay"] = r.u8()
    both = (1 << FX_VOLUME) | (1 << FX_VOL_SLIDE)
    if mask & both == both:
        fx["VolumeSlideTarget"] = r.u8()
    if mask & (1 << FX_DELTA):
        fx["DeltaCounter"] = r.u8()
    if mask & (1 << FX_PHASE):
        fx["PhaseReset"] = r.u8()
    if v >= 16 and mask & (1 << FX_ENV_PERIOD):
        fx["EnvelopePeriod"] = r.u16()
    arp = r.i32()                                            # v7+
    if val == 0xFF and not fx:
        return None
    a = {"Time": str(time)}
    if val == 0 or musical:
        a["Value"] = _note_name(val)
        if musical:
            a["Duration"] = str(duration)
            if release > 0:
                a["Release"] = str(release)
            if inst in inst_names:
                a["Instrument"] = inst_names[inst]
            if arp in arp_names:
                a["Arpeggio"] = arp_names[arp]
            if slide:
                a["SlideTarget"] = _note_name(slide)
    if flags & 1:
        a["Attack"] = "false"
    a.update({k: str(x) for k, x in fx.items()})
    return a
