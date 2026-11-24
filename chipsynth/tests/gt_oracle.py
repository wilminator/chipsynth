# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""An independent check for the GoatTracker exporter: play a `.sng` with GoatTracker's own 6502 player.

`gt2reloc` (GoatTracker 2's packer, which assembles the real player routine, `player.s`, around a song) turns the `.sng` into
a C64 program; `py65` (a 6502 emulator) runs that program's init and play routines the way a C64 would, once a frame; every
write to the SID registers is recorded.  What comes out is the register trace of the real player, with no SID emulation and
nothing of chipsynth's own reading of the format in the loop.

Neither tool is part of chipsynth.  `gt2reloc` is in Debian / Ubuntu's `goattracker` package (or build it from GoatTracker's
source: `make gt2reloc` in `src/`, with the SDL 1.2 development files); `py65` is `pip install py65`.  Put the program's
path in CHIPSYNTH_GT2RELOC, or have it on the PATH.  Without both, the oracle tests skip.

Nothing of GoatTracker is copied here: only its command line and the C64 conventions (init at the load address, play at +3,
the SID at $D400) are used.
"""
import math
import os
import shutil
import subprocess
import tempfile

try:
    from py65.devices.mpu6502 import MPU
    from py65.memory import ObservableMemory
except ImportError:                                  # pragma: no cover - depends on the machine
    MPU = ObservableMemory = None

CLOCK = {"pal": 985248.0, "ntsc": 1022730.0}
HZ = {"pal": 50.0, "ntsc": 60.0}
C0_HZ = 440.0 * 2 ** (-57 / 12)
PREROLL = lambda mult: 6 * mult + 1            # calls before the first row sounds: the player's default tempo, then one


def reloc_path():
    return os.environ.get("CHIPSYNTH_GT2RELOC") or shutil.which("gt2reloc")


def available():
    return MPU is not None and bool(reloc_path())


def pack(sng, region="pal", mult=1, optimise=True):
    """`.sng` bytes -> the C64 program (.prg bytes) with GoatTracker's player.  gt2reloc's file name buffer is short, so it runs
    in its own temporary directory under plain names.  `optimise` False is the packer's -I0 -O0 -R0 (every shortcut off)."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "a.sng"), "wb") as f:
            f.write(sng)
        args = [reloc_path(), "a.sng", "a.prg", "-S%d" % mult, "-N" if region == "ntsc" else "-P"]
        if not optimise:
            args += ["-I0", "-O0", "-R0"]
        run = subprocess.run(args, cwd=d, capture_output=True, text=True)
        if run.returncode:
            raise RuntimeError("gt2reloc failed: " + run.stdout + run.stderr)
        with open(os.path.join(d, "a.prg"), "rb") as f:
            return f.read()


def trace(prg, calls, mult=1, subtune=0):
    """Run the player: init, then `calls` play calls.  Returns one tuple of the 25 voice and filter registers per call."""
    load = prg[0] | prg[1] << 8
    mem = ObservableMemory()
    mpu = MPU(memory=mem)
    for i, b in enumerate(prg[2:]):
        mem._subject[load + i] = b
    regs = [0] * 32

    def write(addr, val):
        regs[addr - 0xD400] = val
        return val
    mem.subscribe_to_write(range(0xD400, 0xD420), write)

    def call(addr, a=0):
        mpu.a, mpu.x, mpu.y, mpu.sp = a, 0, 0, 0xFD
        mem._subject[0x01FE], mem._subject[0x01FF], mem._subject[0x0300] = 0xFF, 0x02, 0x00        # RTS lands on a BRK
        mpu.pc = addr
        for _ in range(200000):
            if mpu.pc == 0x0300:
                return
            mpu.step()
        raise RuntimeError("the player did not return")
    call(load, subtune)
    out = []
    for _ in range(calls):
        call(load + 3)
        out.append(tuple(regs[:25]))
    return out


def semitones(freq, region):
    """The SID frequency register as semitones above C-0 (A-4 = 57), or None for 0."""
    hz = freq * CLOCK[region] / 16777216.0
    return 12 * math.log2(hz / C0_HZ) if hz > 0 else None


def voice(tr, v, region="pal"):
    """[(control byte, pitch in semitones, pulse width in 4096ths, AD, SR)] a call, for voice v (0-2)."""
    o = v * 7
    return [(r[o + 4], semitones(r[o] | r[o + 1] << 8, region), r[o + 2] | (r[o + 3] & 15) << 8, r[o + 5], r[o + 6]) for r in tr]


def gates(tr, v, region="pal"):
    """The notes of voice v: [{on, off, ctrl, pitch, ad, sr}], `on` the call the gate rose on, `off` the call it fell on (None: never).
    The pitch, control byte and envelope are what the voice holds on the call after the gate rose (the first call only
    holds the test bit)."""
    vc = voice(tr, v, region)
    notes, prev = [], 0
    for i, (ctrl, pitch, pw, ad, sr) in enumerate(vc):
        gate = ctrl & 1
        if gate and not prev:
            nxt = vc[min(i + 1, len(vc) - 1)]
            notes.append({"on": i, "off": None, "ctrl": nxt[0], "pitch": nxt[1], "ad": nxt[3], "sr": nxt[4]})
        elif prev and not gate and notes:
            notes[-1]["off"] = i
        prev = gate
    return notes


def planned(tune, region, mult, v):
    """The notes chipsynth plays on voice v for one pass: [(start in calls, pitch)]."""
    fps = HZ[region] * mult
    return [(tune.tick_to_sample(n.start) / 44100.0 * fps, n.pitch) for n in tune.voices[v]]
