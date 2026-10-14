# Copyright (C) 2026 Michael Allen Wilmes
"""Live playback: MusicOut puts a TunePlayer on a pygame mixer channel from a small background thread (the maker's
preview; the game uses it too).  Without pygame or an audio device it says so and stays silent.  pygame is imported
only when a mixer is opened, so the rest of chipsynth never needs it.
"""
import threading
import time

import numpy as np

from .player import TunePlayer
from .synth import RATE, to_pcm16

# ------------------------------------------------------------------ playing
def open_music_mixer():
    """pygame.mixer at 44.1 kHz stereo 16-bit with channel 0 kept for the music; None if unavailable."""
    try:
        import pygame
        init = pygame.mixer.get_init()
        if init and (init[0] != RATE or init[2] != 2):
            pygame.mixer.quit()
            init = None
        if not init:
            pygame.mixer.init(frequency=RATE, size=-16, channels=2, buffer=1024)
        pygame.mixer.set_num_channels(max(8, pygame.mixer.get_num_channels()))
        pygame.mixer.set_reserved(1)
        return pygame.mixer
    except Exception:
        return None


SLEW = 0.12                     # most the volume may move per chunk: ducking and volume changes ramp


def slew(cur, target, step):
    """Move cur toward target by at most step."""
    if cur < target:
        return min(target, cur + step)
    return max(target, cur - step)


class MusicOut:
    """Background-thread player: renders chunks ahead and queues them on a reserved mixer channel.

    `channel` lets the game keep two of these (decks) so one tune can fade out while the next fades in.
    `volume` (0-1) may be changed at any time; fades are ramps on top of it.  stop() returns the TunePlayer,
    which start(..., player=that) picks up again where it was (a tune interrupted by a fight)."""
    CHUNK = RATE // 10

    def __init__(self, mixer=None, channel=0):
        self.mixer = mixer if mixer is not None else open_music_mixer()
        self.channel = channel
        self.player = None
        self.thread = None
        self._stop = threading.Event()
        self._fade_out = 0.0
        self._old = None
        self.note = "" if self.mixer else "No audio device: music is silent."
        self.volume = 1.0

    @property
    def playing(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, tune=None, loop=True, player=None, fade_in=0.0):
        self.stop()
        if not self.mixer:
            return False
        self.player = player if player is not None else TunePlayer(tune, loop=loop, volume=self.volume)
        self._stop = threading.Event()
        self._fade_out = 0.0
        self.thread = threading.Thread(target=self._run, args=(self.player, self._stop, fade_in),
                                       daemon=True)
        self.thread.start()
        return True

    def stop(self, fade=0.0):
        """Stop the music.  fade > 0 lets it ramp down on its own thread (this call returns at once);
        otherwise it stops now.  Returns the TunePlayer (None if nothing was started)."""
        player = self.player
        th = self.thread
        if self._old is not None and fade <= 0:           # a fade-out still running: end it now
            self._fade_out = 0.0
            self._old.join(timeout=1.0)
            self._old = None
            self._silence()
        if th is not None:
            if fade > 0 and th.is_alive():
                self._fade_out = fade
                self._stop.set()
                self._old, self.thread = th, None     # the thread cleans up after itself
                return player
            self._stop.set()
            th.join(timeout=1.0)
            self.thread = None
        self._silence()
        return player

    def _silence(self):
        if self.mixer:
            try:
                self.mixer.Channel(self.channel).stop()
            except Exception:
                pass

    def seconds(self):
        return self.player.seconds if self.player else 0.0

    def _run(self, player, stop, fade_in):
        try:
            ch = self.mixer.Channel(self.channel)
            played = 0.0                                    # seconds of music rendered so far
            gain = None                                     # the smoothed volume (volume changes ramp)
            span = self.CHUNK / RATE
            while not stop.is_set():
                if player.done and not ch.get_busy():
                    break
                if ch.get_queue() is None:
                    if player.done:
                        time.sleep(0.05)
                        continue
                    ramp = 1.0 if fade_in <= 0 else min(1.0, (played + span) / fade_in)
                    gain = slew(gain, self.volume, SLEW) if gain is not None else self.volume
                    player.volume = gain * ramp
                    snd = self.mixer.Sound(buffer=to_pcm16(player.render(self.CHUNK)))
                    played += span
                    if ch.get_busy():
                        ch.queue(snd)
                    else:
                        ch.play(snd)
                else:
                    time.sleep(0.02)
            if stop.is_set() and self._fade_out > 0:
                fade, n = self._fade_out, 0
                steps = max(1, int(fade / span))
                base = player.volume
                while n < steps and not player.done:
                    if ch.get_queue() is None:
                        n += 1
                        player.volume = base * (1.0 - n / steps)
                        snd = self.mixer.Sound(buffer=to_pcm16(player.render(self.CHUNK)))
                        if ch.get_busy():
                            ch.queue(snd)
                        else:
                            ch.play(snd)
                    else:
                        time.sleep(0.02)
                player.volume = base
                time.sleep(span)
                ch.stop()
        except Exception as e:                      # never crash the caller over audio
            self.note = "Music stopped: %s" % e
