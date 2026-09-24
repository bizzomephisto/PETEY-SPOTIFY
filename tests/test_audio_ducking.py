from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from addon import SpotifyAddon


class SpotifyAudioDuckingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addon = SpotifyAddon(SimpleNamespace(
            data_dir=Path(self.directory.name), emit_event=Mock(),
        ))
        self.addon._duck_music = True
        self.addon.playback_state = Mock(return_value={
            "playing": True,
            "device": {"id": "speaker", "volume_percent": 72, "supports_volume": True},
        })
        self.addon._start_volume_fade = Mock()

    def tearDown(self):
        restore_timer = self.addon._duck_restore_timer
        if restore_timer is not None:
            restore_timer.cancel()
        for timer in self.addon._duck_timers.values():
            timer.cancel()

    def test_consecutive_speech_segments_keep_original_volume(self):
        self.assertEqual(self.addon.speech_duck(True, "browser")["status"], "ducked")
        self.assertEqual(self.addon._duck_original_volume, 72)
        self.assertEqual(self.addon.speech_duck(False, "browser")["status"], "restore_pending")

        self.addon.playback_state.return_value["device"]["volume_percent"] = 18
        self.assertEqual(self.addon.speech_duck(True, "browser")["status"], "ducked")

        self.assertEqual(self.addon._duck_original_volume, 72)
        self.assertEqual(self.addon.playback_state.call_count, 1)
        self.assertIsNone(self.addon._duck_restore_timer)

    def test_final_release_restores_after_grace_period(self):
        self.addon.speech_duck(True, "browser")
        self.addon.speech_duck(False, "browser")
        pending = self.addon._duck_restore_timer
        self.assertIsNotNone(pending)
        pending.cancel()

        self.addon._restore_ducked_volume()

        self.assertIsNone(self.addon._duck_original_volume)
        self.addon._start_volume_fade.assert_called_with(72, 72, "speaker", 700)


if __name__ == "__main__":
    unittest.main()
