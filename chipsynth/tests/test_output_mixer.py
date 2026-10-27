# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""open_music_mixer: falls back to DirectSound on Windows and reports why it failed."""
import os
import sys
import types
import unittest
from unittest import mock

from chipsynth import output


def fake_pygame(fail_drivers):
    calls = []
    mixer = types.SimpleNamespace()
    state = {"init": None}

    def init(**kw):
        drv = os.environ.get("SDL_AUDIODRIVER")
        calls.append(drv)
        if drv in fail_drivers:
            raise RuntimeError("WASAPI can't initialize audio client:")
        state["init"] = (kw["frequency"], kw["size"], kw["channels"])
    mixer.init = init
    mixer.quit = lambda: state.update(init=None)
    mixer.get_init = lambda: state["init"]
    mixer.get_num_channels = lambda: 8
    mixer.set_num_channels = lambda n: None
    mixer.set_reserved = lambda n: None
    return types.SimpleNamespace(mixer=mixer), calls


class MixerFallbackTests(unittest.TestCase):
    def run_open(self, platform, fail, env=None):
        pg, calls = fake_pygame(fail)
        with mock.patch.dict(sys.modules, {"pygame": pg}), mock.patch.object(sys, "platform", platform), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            if not env:
                os.environ.pop("SDL_AUDIODRIVER", None)
            result = output.open_music_mixer()
            driver_after = os.environ.get("SDL_AUDIODRIVER")
            seen = list(calls)
            self.note = output.MusicOut().note if result is None else ""
        return result, seen, driver_after

    def test_windows_retries_with_directsound(self):
        result, calls, _ = self.run_open("win32", {None})
        self.assertIsNotNone(result)
        self.assertEqual(calls, [None, "directsound"])
        self.assertIn("WASAPI", output.LAST_MIXER_ERROR)

    def test_both_fail_reports_the_reason_and_restores_the_environment(self):
        result, calls, after = self.run_open("win32", {None, "directsound"})
        self.assertIsNone(result)
        self.assertIn("WASAPI", output.LAST_MIXER_ERROR)
        self.assertIsNone(after)
        self.assertIn("WASAPI", self.note)

    def test_other_platforms_do_not_retry(self):
        result, calls, _ = self.run_open("linux", {None})
        self.assertIsNone(result)
        self.assertEqual(calls, [None])

    def test_a_user_chosen_driver_is_respected(self):
        result, calls, _ = self.run_open("win32", {"wasapi"}, env={"SDL_AUDIODRIVER": "wasapi"})
        self.assertIsNone(result)
        self.assertEqual(calls, ["wasapi"])


if __name__ == "__main__":
    unittest.main()
