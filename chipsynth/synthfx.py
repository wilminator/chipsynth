# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared effect bus of the chiptune mixer: an echo and a reverb (see DESIGN-MUSIC.md).

Every voice has a `send` level; the sends of all voices are summed (stereo, already panned) and run through the
echo and the reverb once.  The wet signal is added to the dry mix.  Both keep their delay lines between calls, and
all feedback is computed in blocks no longer than the shortest delay, so the result is exactly the same however the
render is cut into chunks, and the tail of a loop rings over its restart.
"""
import numpy as np

from .synth import RATE


class Echo:
    """A feedback delay.  time in seconds, feedback 0-0.9, mix 0-1; wide = ping-pong between the channels."""

    def __init__(self, time, feedback, mix, wide=False):
        self.d = max(1, int(round(time * RATE)))
        self.fb = float(feedback)
        self.mix = float(mix)
        self.wide = bool(wide)
        self.buf = np.zeros((self.d, 2), dtype=np.float64)      # the last d output samples (ring, oldest first)

    def process(self, x):
        """The echoes of x only (the dry signal is the caller's).  w[n] = x[n] + fb * w[n-d]; the echo heard is w[n-d]."""
        n = len(x)
        wet = np.empty((n, 2), dtype=np.float64)
        i = 0
        while i < n:
            k = min(self.d, n - i)
            old = self.buf[:k]                                   # w[n - d] for these k samples
            tap = old[:, ::-1] if self.wide else old
            wet[i:i + k] = tap
            self.buf = np.concatenate([self.buf[k:], x[i:i + k] + self.fb * tap])
            i += k
        return (wet * self.mix).astype(np.float32)


class _Comb:
    def __init__(self, d, g):
        self.d, self.g = d, g
        self.buf = np.zeros(d, dtype=np.float64)

    def process(self, x):
        n = len(x)
        out = np.empty(n)
        i = 0
        while i < n:
            k = min(self.d, n - i)
            y = x[i:i + k] + self.g * self.buf[:k]
            out[i:i + k] = y
            self.buf = np.concatenate([self.buf[k:], y])
            i += k
        return out


class _Allpass:
    def __init__(self, d, g=0.5):
        self.d, self.g = d, g
        self.xbuf = np.zeros(d)
        self.ybuf = np.zeros(d)

    def process(self, x):
        n = len(x)
        out = np.empty(n)
        i = 0
        while i < n:
            k = min(self.d, n - i)
            xs = x[i:i + k]
            y = -self.g * xs + self.xbuf[:k] + self.g * self.ybuf[:k]
            out[i:i + k] = y
            self.xbuf = np.concatenate([self.xbuf[k:], xs])
            self.ybuf = np.concatenate([self.ybuf[k:], y])
            i += k
        return out


COMBS = (1116, 1188, 1277, 1356)             # Freeverb's tunings at 44.1 kHz
ALLPASS = (556, 441)
SPREAD = 23                                    # right channel delays are longer by this many samples


class Reverb:
    """A small Schroeder reverb (four combs, two all-passes per channel).  amount 0-15."""

    def __init__(self, amount):
        self.amount = max(0, min(15, int(amount)))
        a = self.amount / 15.0
        g = 0.70 + 0.2 * a                         # longer rooms with a bigger amount
        self.wet = 0.12 + 0.3 * a
        self.l = ([_Comb(d, g) for d in COMBS], [_Allpass(d) for d in ALLPASS])
        self.r = ([_Comb(d + SPREAD, g) for d in COMBS], [_Allpass(d + SPREAD) for d in ALLPASS])

    def _chan(self, x, side):
        combs, aps = side
        y = sum(c.process(x) for c in combs) * 0.25
        for ap in aps:
            y = ap.process(y)
        return y

    def process(self, x):
        mono = (x[:, 0].astype(np.float64) + x[:, 1]) * 0.5
        out = np.empty((len(x), 2), dtype=np.float32)
        out[:, 0] = self._chan(mono, self.l) * self.wet
        out[:, 1] = self._chan(mono, self.r) * self.wet
        return out


class Bus:
    """The echo and the reverb together (either may be None)."""

    def __init__(self, echo=None, reverb=None):
        self.echo, self.reverb = echo, reverb

    def process(self, send):
        wet = np.zeros_like(send)
        if self.echo is not None:
            wet += self.echo.process(send)
        if self.reverb is not None:
            wet += self.reverb.process(send)
        return wet
