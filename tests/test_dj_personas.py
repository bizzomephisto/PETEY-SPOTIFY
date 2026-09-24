from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from addon import DEFAULT_DJ_PROMPT, SpotifyAddon, SpotifyError


class SpotifyDjPersonaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.emit_event = Mock()
        self.addon = SpotifyAddon(SimpleNamespace(
            data_dir=Path(self.directory.name), emit_event=self.emit_event,
        ))

    def test_panel_exposes_three_personas_and_both_dj_toggles(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "panel.html").read_text(encoding="utf-8")
        script = (root / "panel.js").read_text(encoding="utf-8")
        for slot in (1, 2, 3):
            self.assertIn(f'id="spotify-dj-name-{slot}"', html)
            self.assertIn(f'id="spotify-dj-prompt-{slot}"', html)
        self.assertIn('id="spotify-dj-mix-profile"', html)
        self.assertIn('id="spotify-dj-post-album-art"', html)
        self.assertIn("mix_with_profile: id('dj-mix-profile').checked", script)
        self.assertIn("post_album_art: id('dj-post-album-art').checked", script)

    def test_three_personas_active_choice_and_toggles_persist(self):
        personas = [
            {"name": "Classic", "prompt": DEFAULT_DJ_PROMPT},
            {"name": "Night Owl", "prompt": "Be relaxed for {next_song} by {next_artist}."},
            {"name": "Sports Voice", "prompt": "Announce {next_song} with stadium energy."},
        ]
        status = self.addon.configure_dj_mode(
            True, personas=personas, active_persona="2",
            mix_with_profile=True, post_album_art=False,
        )
        self.assertEqual(len(status["dj_personas"]), 3)
        self.assertEqual(status["active_dj_persona"], "2")
        self.assertEqual(status["dj_prompt"], personas[1]["prompt"])
        self.assertTrue(status["mix_dj_with_profile"])
        self.assertFalse(status["post_album_art"])

        restored = SpotifyAddon(SimpleNamespace(
            data_dir=Path(self.directory.name), emit_event=Mock(),
        )).status()
        self.assertEqual(restored["dj_personas"][1]["name"], "Night Owl")
        self.assertEqual(restored["active_dj_persona"], "2")
        self.assertTrue(restored["mix_dj_with_profile"])
        self.assertFalse(restored["post_album_art"])

    def test_legacy_prompt_migrates_to_first_persona(self):
        legacy = "Introduce {next_song} by {next_artist} in a dry voice."
        Path(self.directory.name, "config.json").write_text(
            json.dumps({"dj_mode": True, "dj_prompt": legacy}), encoding="utf-8",
        )
        migrated = SpotifyAddon(SimpleNamespace(
            data_dir=Path(self.directory.name), emit_event=Mock(),
        )).status()
        self.assertEqual(migrated["dj_personas"][0]["prompt"], legacy)
        self.assertEqual(len(migrated["dj_personas"]), 3)
        self.assertTrue(migrated["post_album_art"])

    def test_transition_blends_profile_and_can_omit_album_art(self):
        self.addon.configure_dj_mode(
            True,
            personas=[
                {"name": "Classic", "prompt": DEFAULT_DJ_PROMPT},
                {"name": "Night Owl", "prompt": "Introduce {next_song} by {next_artist}."},
                {"name": "Third", "prompt": "Keep it short."},
            ],
            active_persona="2", mix_with_profile=True, post_album_art=False,
        )
        self.addon._access_token = "token"
        self.addon._token_expires_at = time.time() + 300
        current = {
            "is_playing": True,
            "progress_ms": 85_000,
            "item": {
                "uri": "spotify:track:old", "name": "Old Song", "duration_ms": 100_000,
                "artists": [{"name": "Old Artist"}],
                "album": {"name": "Old Album", "release_date": "2000"},
            },
        }
        upcoming = {
            "queue": [{
                "uri": "spotify:track:new", "name": "New Song", "duration_ms": 180_000,
                "artists": [{"name": "New Artist"}],
                "album": {
                    "name": "New Album", "release_date": "2020",
                    "images": [{"url": "https://images.example/art.jpg"}],
                },
                "external_urls": {"spotify": "https://open.spotify.com/track/new"},
            }],
        }
        self.addon._api_request = Mock(side_effect=[current, upcoming])

        self.addon.check_playback_change()

        prompt = self.emit_event.call_args.args[0]
        metadata = self.emit_event.call_args.kwargs["metadata"]
        self.assertIn("Blend the current PETEY system profile", prompt)
        self.assertIn("Night Owl", prompt)
        self.assertNotIn("chat_image_url", metadata)
        self.assertEqual(metadata["spotify_dj_persona"], "Night Owl")

    def test_active_persona_requires_a_prompt_when_dj_mode_is_enabled(self):
        with self.assertRaisesRegex(SpotifyError, "active DJ persona"):
            self.addon.configure_dj_mode(
                True,
                personas=[
                    {"name": "One", "prompt": DEFAULT_DJ_PROMPT},
                    {"name": "Empty", "prompt": ""},
                    {"name": "Three", "prompt": "Short."},
                ],
                active_persona="2", mix_with_profile=False, post_album_art=True,
            )


if __name__ == "__main__":
    unittest.main()
