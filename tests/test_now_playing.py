from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from addon import SpotifyAddon


class SpotifyNowPlayingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addon = SpotifyAddon(SimpleNamespace(
            data_dir=Path(self.directory.name), emit_event=Mock(),
        ))

    def test_paused_track_keeps_metadata_and_album_art(self):
        self.addon._api_request = Mock(return_value={
            "is_playing": False,
            "progress_ms": 42_000,
            "item": {
                "name": "Night Drive",
                "artists": [{"name": "The Signals"}],
                "duration_ms": 180_000,
                "uri": "spotify:track:abc",
                "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
                "album": {
                    "name": "City Lights",
                    "images": [{"url": "https://i.scdn.co/image/art"}],
                },
            },
        })

        result = self.addon.currently_playing({})

        self.assertFalse(result["playing"])
        self.assertTrue(result["has_track"])
        self.assertEqual(result["name"], "Night Drive")
        self.assertEqual(result["artist"], "The Signals")
        self.assertEqual(result["image_url"], "https://i.scdn.co/image/art")
        self.assertEqual(result["progress_ms"], 42_000)

    def test_no_playback_returns_no_track(self):
        self.addon._api_request = Mock(return_value=None)

        result = self.addon.currently_playing({})

        self.assertFalse(result["playing"])
        self.assertFalse(result["has_track"])

    def test_mute_persists_and_unmute_restores_exact_volume(self):
        self.addon.playback_state = Mock(return_value={
            "playing": True,
            "device": {
                "id": "shed-speaker",
                "volume_percent": 67,
                "supports_volume": True,
            },
        })
        self.addon._api_request = Mock(return_value={})

        muted = self.addon.set_muted({"muted": True})

        self.assertTrue(muted["muted"])
        self.assertEqual(muted["restore_volume"], 67)
        self.addon._api_request.assert_called_with(
            "PUT", "/me/player/volume",
            params={"volume_percent": 0, "device_id": "shed-speaker"},
        )

        restored = SpotifyAddon(SimpleNamespace(
            data_dir=Path(self.directory.name), emit_event=Mock(),
        ))
        self.assertTrue(restored.status()["spotify_muted"])
        self.assertEqual(restored.status()["spotify_restore_volume"], 67)
        restored.playback_state = Mock(return_value={
            "playing": True,
            "device": {
                "id": "shed-speaker",
                "volume_percent": 0,
                "supports_volume": True,
            },
        })
        restored._api_request = Mock(return_value={})

        unmuted = restored.set_muted({"muted": False})

        self.assertFalse(unmuted["muted"])
        self.assertEqual(unmuted["volume_percent"], 67)
        restored._api_request.assert_called_with(
            "PUT", "/me/player/volume",
            params={"volume_percent": 67, "device_id": "shed-speaker"},
        )


if __name__ == "__main__":
    unittest.main()
