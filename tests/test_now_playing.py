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


if __name__ == "__main__":
    unittest.main()
