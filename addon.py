"""Spotify Web API connector for PETEY."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from flask import jsonify, request
import requests

from petey.tools.registry import ToolSpec


SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = " ".join((
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-top-read",
    "user-read-currently-playing",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
))
MAX_RESULT_CHARS = 60_000
MAX_DJ_PROMPT_CHARS = 2_000
MAX_DJ_PERSONA_NAME_CHARS = 60
DJ_PERSONA_SLOTS = 3
MAX_PLAYLIST_ITEMS = 100
DJ_TOOL_SUPPRESSION_SECONDS = 30
DEFAULT_DJ_WRAPUP_SECONDS = 20
MAX_DJ_WRAPUP_SECONDS = 25
DJ_MAX_POLL_SECONDS = 6.0
DJ_MIN_POLL_SECONDS = 0.75
DJ_QUEUE_RETRY_SECONDS = 2.0
DEFAULT_DUCK_VOLUME = 18
DEFAULT_FADE_DURATION_MS = 700
DUCK_RELEASE_GRACE_SECONDS = 0.35
LEGACY_DJ_PROMPT = (
    "The current song {song} by {artist} has just started playing. "
    "You're a radio DJ announcing the next song and giving a quick fact."
)
DEFAULT_DJ_PROMPT = (
    "You're a radio DJ making a tight transition. Start with 'That was {song} by "
    "{artist}' once, then introduce {next_song} by {next_artist} and give one quick "
    "fact about the upcoming song or artist."
)


def _default_dj_personas() -> list[dict]:
    return [
        {"id": "1", "name": "Classic DJ", "prompt": DEFAULT_DJ_PROMPT},
        {"id": "2", "name": "DJ Persona 2", "prompt": ""},
        {"id": "3", "name": "DJ Persona 3", "prompt": ""},
    ]

MUSIC_INTENT = re.compile(
    r"\b(spotify|music|song|songs|track|tracks|artist|artists|album|albums|"
    r"playlist|playlists|play|pause|resume|stop|skip|next|previous|"
    r"search|listen|now playing|what('?s| is) (?:playing|on|the song)|"
    r"queue|shuffle|repeat|volume|like|favorite|top|discover)\b",
    re.IGNORECASE,
)
PLAYLIST_INTENT = re.compile(r"\bplaylist(?:s)?\b", re.IGNORECASE)
PLAYLIST_WRITE_INTENT = re.compile(
    r"\b(create|make|add|append|remove|delete|rename|change|edit|update|move|reorder)\b.*\bplaylist\b"
    r"|\bplaylist\b.*\b(create|make|add|append|remove|delete|rename|change|edit|update|move|reorder)\b",
    re.IGNORECASE,
)
PLAYER_CONTROL_INTENT = re.compile(
    r"\b(volume|louder|quieter|seek|shuffle|repeat|queue|device|speaker|transfer|move playback)\b",
    re.IGNORECASE,
)


class SpotifyError(ValueError):
    """A safe error suitable for the PETEY UI."""


def _bounded(value: object) -> dict:
    text = json.dumps(value, ensure_ascii=False)
    return {"result": text[:MAX_RESULT_CHARS], "truncated": len(text) > MAX_RESULT_CHARS}


class SpotifyAddon:
    """Manages Spotify API interactions and authentication."""

    def __init__(self, context):
        self._data_dir = Path(context.data_dir)
        self._config_path = self._data_dir / "config.json"
        self._token_path = self._data_dir / "token.json"
        self._lock = threading.RLock()
        self._emit_event = getattr(context, "emit_event", None)
        self._client_id = ""
        self._dj_mode = False
        self._dj_personas = _default_dj_personas()
        self._active_dj_persona = "1"
        self._mix_dj_with_profile = False
        self._post_album_art = True
        self._dj_wrapup_seconds = DEFAULT_DJ_WRAPUP_SECONDS
        self._duck_music = False
        self._duck_volume = DEFAULT_DUCK_VOLUME
        self._fade_duration_ms = DEFAULT_FADE_DURATION_MS
        self._manual_mute_restore_volume = None
        self._access_token = ""
        self._refresh_token = ""
        self._token_expires_at = 0.0
        self._auth_state = ""
        self._code_verifier = ""
        self._auth_expires_at = 0.0
        self._callback_server = None
        self._callback_thread = None
        self._watcher_stop = threading.Event()
        self._watcher_thread = None
        self._last_track_uri = None
        self._announced_track_uri = None
        self._suppressed_track_uri = None
        self._suppressed_track_until = 0.0
        self._duck_clients = {}
        self._duck_original_volume = None
        self._duck_device_id = ""
        self._duck_timers = {}
        self._duck_restore_timer = None
        self._fade_generation = 0
        self._load_config()
        self._load_token()

    def _load_config(self) -> None:
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
            self._client_id = str(payload.get("client_id") or "")
            self._dj_mode = payload.get("dj_mode", payload.get("announce_track_changes")) is True
            legacy_prompt = self._normalize_dj_prompt(str(payload.get("dj_prompt") or "").strip())
            self._dj_personas = self._validated_dj_personas(
                payload.get("dj_personas"), legacy_prompt=legacy_prompt,
            )
            active = str(payload.get("active_dj_persona") or "1")
            self._active_dj_persona = active if active in {"1", "2", "3"} else "1"
            self._mix_dj_with_profile = payload.get("mix_dj_with_profile") is True
            self._post_album_art = payload.get("post_album_art", True) is not False
            self._dj_wrapup_seconds = max(0, min(MAX_DJ_WRAPUP_SECONDS, int(
                payload.get("dj_wrapup_seconds", DEFAULT_DJ_WRAPUP_SECONDS)
            )))
            self._duck_music = payload.get("duck_music") is True
            self._duck_volume = max(0, min(100, int(
                payload.get("duck_volume", DEFAULT_DUCK_VOLUME)
            )))
            self._fade_duration_ms = max(100, min(3000, int(
                payload.get("fade_duration_ms", DEFAULT_FADE_DURATION_MS)
            )))
            saved_mute_volume = payload.get("manual_mute_restore_volume")
            self._manual_mute_restore_volume = (
                max(1, min(100, int(saved_mute_volume)))
                if saved_mute_volume is not None else None
            )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    def _write_config(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._config_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({
                "client_id": self._client_id,
                "dj_mode": self._dj_mode,
                "dj_prompt": self._active_persona()["prompt"],
                "dj_personas": self._dj_personas,
                "active_dj_persona": self._active_dj_persona,
                "mix_dj_with_profile": self._mix_dj_with_profile,
                "post_album_art": self._post_album_art,
                "dj_wrapup_seconds": self._dj_wrapup_seconds,
                "duck_music": self._duck_music,
                "duck_volume": self._duck_volume,
                "fade_duration_ms": self._fade_duration_ms,
                "manual_mute_restore_volume": self._manual_mute_restore_volume,
            }, indent=2),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self._config_path)

    def _load_token(self) -> None:
        try:
            payload = json.loads(self._token_path.read_text(encoding="utf-8"))
            self._access_token = payload.get("access_token", "")
            self._refresh_token = payload.get("refresh_token", "")
            self._token_expires_at = payload.get("expires_at", 0.0)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass

    def _save_token(self, access_token: str, refresh_token: str, expires_in: int) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = time.time() + expires_in - 60
        temporary = self._token_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": self._token_expires_at,
            }),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self._token_path)

    def _is_token_valid(self) -> bool:
        return bool(self._access_token) and time.time() < self._token_expires_at

    def _refresh_access_token(self) -> None:
        if not self._refresh_token or not self._client_id:
            raise SpotifyError("No valid Spotify credentials. Re-authorize in the Spotify panel.")
        try:
            response = requests.post(
                SPOTIFY_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            if response.status_code == 400:
                self._clear_token()
                raise SpotifyError("Spotify authorization expired. Re-authorize in the Spotify panel.")
            if response.status_code != 200:
                raise SpotifyError(f"Spotify token refresh failed (HTTP {response.status_code}).")
            data = response.json()
            self._save_token(
                data["access_token"],
                data.get("refresh_token", self._refresh_token),
                data.get("expires_in", 3600),
            )
        except requests.RequestException as exc:
            raise SpotifyError("Could not reach Spotify authentication server.") from exc

    def _clear_token(self) -> None:
        self._access_token = ""
        self._refresh_token = ""
        self._token_expires_at = 0.0
        try:
            self._token_path.unlink()
        except FileNotFoundError:
            pass

    def _api_request(self, method: str, path: str, **kwargs) -> dict:
        with self._lock:
            if not self._is_token_valid():
                self._refresh_access_token()
            url = f"{SPOTIFY_API_BASE}{path}"
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {self._access_token}"
            try:
                response = requests.request(method, url, headers=headers, timeout=10, **kwargs)
            except requests.RequestException as exc:
                raise SpotifyError("Could not reach Spotify API.") from exc
            if response.status_code == 401:
                self._access_token = ""
                try:
                    self._refresh_access_token()
                    headers["Authorization"] = f"Bearer {self._access_token}"
                    response = requests.request(method, url, headers=headers, timeout=10, **kwargs)
                except SpotifyError:
                    raise SpotifyError("Spotify authorization expired. Re-authorize in the Spotify panel.")
            if response.status_code == 429:
                raise SpotifyError("Spotify rate limit exceeded. Try again later.")
            if response.status_code == 404 and path.startswith("/me/player"):
                raise SpotifyError("No active Spotify device. Open Spotify on a device and start playback once.")
            if response.status_code == 404:
                raise SpotifyError("The requested Spotify resource was not found.")
            if response.status_code == 403 and (
                path == "/me/playlists" or path.startswith("/playlists/")
            ):
                raise SpotifyError(
                    "Spotify denied playlist access. Authorize Spotify again in the add-on "
                    "to grant the new playlist permissions, and make sure you own or collaborate on it."
                )
            if response.status_code == 403 and path.startswith("/me/player"):
                raise SpotifyError(
                    "Spotify denied that playback command. It requires Premium and a controllable active device."
                )
            if response.status_code >= 400:
                raise SpotifyError(f"Spotify API returned HTTP {response.status_code}.")
            if response.status_code == 204:
                return {"status": "ok"}
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError):
                return {"status": "ok"}

    def configure(self, client_id: object) -> dict:
        with self._lock:
            cid = str(client_id or "").strip()
            if not cid:
                raise SpotifyError("Enter your Spotify Client ID.")
            if len(cid) > 200:
                raise SpotifyError("Client ID is too long.")
            self._client_id = cid
            self._write_config()
            return self.status()

    def configure_dj_mode(
        self, enabled: object, prompt: object = None, *, personas: object = None,
        active_persona: object = None, mix_with_profile: object = False,
        post_album_art: object = True, wrapup_seconds: object = None,
    ) -> dict:
        if type(enabled) is not bool:
            raise SpotifyError("DJ Mode must be on or off.")
        if type(mix_with_profile) is not bool:
            raise SpotifyError("DJ profile blending must be on or off.")
        if type(post_album_art) is not bool:
            raise SpotifyError("Album artwork posting must be on or off.")
        try:
            cue_seconds = int(
                self._dj_wrapup_seconds if wrapup_seconds is None else wrapup_seconds
            )
        except (TypeError, ValueError) as exc:
            raise SpotifyError("DJ wrap-up time must be a whole number from 0 to 25 seconds.") from exc
        if cue_seconds < 0 or cue_seconds > MAX_DJ_WRAPUP_SECONDS:
            raise SpotifyError("DJ wrap-up time must be from 0 to 25 seconds.")
        selected = str(active_persona or self._active_dj_persona)
        if selected not in {"1", "2", "3"}:
            raise SpotifyError("Choose one of the three DJ personas.")
        legacy_prompt = self._normalize_dj_prompt(str(prompt or "").strip())
        validated = self._validated_dj_personas(personas, legacy_prompt=legacy_prompt)
        active = next(item for item in validated if item["id"] == selected)
        if enabled and not active["prompt"]:
            raise SpotifyError("Enter a prompt for the active DJ persona.")
        with self._lock:
            self._dj_mode = enabled
            self._dj_personas = validated
            self._active_dj_persona = selected
            self._mix_dj_with_profile = mix_with_profile
            self._post_album_art = post_album_art
            self._dj_wrapup_seconds = cue_seconds
            self._last_track_uri = None
            self._announced_track_uri = None
            self._suppressed_track_uri = None
            self._suppressed_track_until = 0.0
            self._write_config()
            return self.status()

    def configure_audio_ducking(
        self, enabled: object, volume: object, fade_duration_ms: object
    ) -> dict:
        if type(enabled) is not bool:
            raise SpotifyError("Music fading must be on or off.")
        try:
            duck_volume = max(0, min(100, int(volume)))
            fade_ms = max(100, min(3000, int(fade_duration_ms)))
        except (TypeError, ValueError) as exc:
            raise SpotifyError("Fade volume and duration must be numbers.") from exc
        with self._lock:
            self._duck_music = enabled
            self._duck_volume = duck_volume
            self._fade_duration_ms = fade_ms
            self._write_config()
        if not enabled:
            self._restore_ducked_volume()
        return self.status()

    @staticmethod
    def _normalize_dj_prompt(prompt: str) -> str:
        if prompt == LEGACY_DJ_PROMPT:
            return DEFAULT_DJ_PROMPT
        normalized = prompt.replace(
            "The current song {song} by {artist} has just started playing.",
            "The outgoing song {song} by {artist} has about twenty seconds left.",
        ).replace(
            "The outgoing song {song} by {artist} has about ten seconds left.",
            "The outgoing song {song} by {artist} has about twenty seconds left.",
        )
        normalized = re.sub(
            r"up next we have \{song\} by \{artist\}",
            "up next we have {next_song} by {next_artist}",
            normalized,
            flags=re.IGNORECASE,
        )
        return normalized.replace("<song by artist>", "{song} by {artist}")

    @classmethod
    def _validated_dj_personas(cls, value: object, *, legacy_prompt: str = "") -> list[dict]:
        supplied = value if isinstance(value, list) else []
        defaults = _default_dj_personas()
        result = []
        for index in range(DJ_PERSONA_SLOTS):
            item = supplied[index] if index < len(supplied) and isinstance(supplied[index], dict) else {}
            name = str(item.get("name") or defaults[index]["name"]).strip()
            if len(name) > MAX_DJ_PERSONA_NAME_CHARS:
                raise SpotifyError(
                    f"DJ persona names must be {MAX_DJ_PERSONA_NAME_CHARS} characters or fewer."
                )
            raw_prompt = item.get("prompt")
            if raw_prompt is None and index == 0:
                raw_prompt = legacy_prompt or defaults[index]["prompt"]
            persona_prompt = cls._normalize_dj_prompt(str(raw_prompt or "").strip())
            if len(persona_prompt) > MAX_DJ_PROMPT_CHARS:
                raise SpotifyError(
                    f"DJ persona prompts must be {MAX_DJ_PROMPT_CHARS} characters or fewer."
                )
            result.append({
                "id": str(index + 1),
                "name": name or defaults[index]["name"],
                "prompt": persona_prompt,
            })
        return result

    def _active_persona(self) -> dict:
        return next(
            (item for item in self._dj_personas if item["id"] == self._active_dj_persona),
            self._dj_personas[0],
        )

    @staticmethod
    def _persona_instruction(name: str, prompt: str, mix_with_profile: bool) -> str:
        if mix_with_profile:
            return (
                f"Blend the current PETEY system profile with the Spotify DJ persona named {name}. "
                "Keep PETEY's established personality and voice while applying these DJ directions:\n"
                f"{prompt}"
            )
        return (
            f"For this Spotify transition, use the DJ persona named {name} as the response style. "
            "Follow these DJ directions closely:\n"
            f"{prompt}"
        )

    def save_auth(self, code: object, state: object) -> dict:
        """Validate the OAuth callback and exchange its code using PKCE."""
        with self._lock:
            code_str = str(code or "").strip()
            if not code_str:
                raise SpotifyError("No authorization code provided.")
            if not self._client_id:
                raise SpotifyError("Set your Spotify Client ID first.")
            if (
                not self._auth_state
                or not secrets.compare_digest(str(state or ""), self._auth_state)
                or time.time() >= self._auth_expires_at
            ):
                raise SpotifyError("Spotify authorization request expired or did not match. Try again.")
            verifier = self._code_verifier
            try:
                response = requests.post(
                    SPOTIFY_TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": code_str,
                        "redirect_uri": REDIRECT_URI,
                        "client_id": self._client_id,
                        "code_verifier": verifier,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
                if response.status_code != 200:
                    raise SpotifyError("Authorization code exchange failed. The code may have expired.")
                data = response.json()
                self._save_token(
                    data["access_token"],
                    data["refresh_token"],
                    data.get("expires_in", 3600),
                )
                self._auth_state = ""
                self._code_verifier = ""
                self._auth_expires_at = 0.0
            except requests.RequestException as exc:
                raise SpotifyError("Could not reach Spotify authentication server.") from exc
            return self.status()

    def status(self) -> dict:
        with self._lock:
            return {
                "client_id": self._client_id,
                "authorized": self._is_token_valid() or bool(self._refresh_token),
                "dj_mode": self._dj_mode,
                "dj_prompt": self._active_persona()["prompt"],
                "dj_personas": [dict(item) for item in self._dj_personas],
                "active_dj_persona": self._active_dj_persona,
                "mix_dj_with_profile": self._mix_dj_with_profile,
                "post_album_art": self._post_album_art,
                "dj_wrapup_seconds": self._dj_wrapup_seconds,
                "duck_music": self._duck_music,
                "duck_volume": self._duck_volume,
                "fade_duration_ms": self._fade_duration_ms,
                "spotify_muted": self._manual_mute_restore_volume is not None,
                "spotify_restore_volume": self._manual_mute_restore_volume,
            }

    @staticmethod
    def _device_params(arguments: dict) -> dict:
        device_id = str((arguments or {}).get("device_id") or "").strip()
        return {"device_id": device_id} if device_id else {}

    def playback_state(self, _arguments: dict | None = None) -> dict:
        data = self._api_request("GET", "/me/player")
        device = data.get("device") or {} if isinstance(data, dict) else {}
        item = data.get("item") or {} if isinstance(data, dict) else {}
        artists = item.get("artists") or [] if isinstance(item, dict) else []
        return {
            "playing": bool(data.get("is_playing")) if isinstance(data, dict) else False,
            "shuffle": bool(data.get("shuffle_state")) if isinstance(data, dict) else False,
            "repeat": str(data.get("repeat_state") or "off") if isinstance(data, dict) else "off",
            "progress_ms": int(data.get("progress_ms") or 0) if isinstance(data, dict) else 0,
            "track": str(item.get("name") or "") if isinstance(item, dict) else "",
            "artist": ", ".join(
                str(artist.get("name") or "") for artist in artists
                if isinstance(artist, dict) and artist.get("name")
            ),
            "device": {
                "id": str(device.get("id") or "") if isinstance(device, dict) else "",
                "name": str(device.get("name") or "") if isinstance(device, dict) else "",
                "type": str(device.get("type") or "") if isinstance(device, dict) else "",
                "volume_percent": device.get("volume_percent") if isinstance(device, dict) else None,
                "supports_volume": bool(device.get("supports_volume")) if isinstance(device, dict) else False,
            },
        }

    def list_devices(self, _arguments: dict | None = None) -> dict:
        data = self._api_request("GET", "/me/player/devices")
        devices = []
        for device in data.get("devices") or []:
            if not isinstance(device, dict):
                continue
            devices.append({
                "id": str(device.get("id") or ""),
                "name": str(device.get("name") or "Unknown device"),
                "type": str(device.get("type") or ""),
                "active": bool(device.get("is_active")),
                "restricted": bool(device.get("is_restricted")),
                "volume_percent": device.get("volume_percent"),
                "supports_volume": bool(device.get("supports_volume")),
            })
        return {"count": len(devices), "devices": devices}

    def _resolve_device_id(self, reference: object) -> str:
        requested = str(reference or "").strip()
        if not requested:
            raise SpotifyError("Provide a Spotify device name or ID.")
        devices = self.list_devices({})["devices"]
        exact = [item for item in devices if requested in (item["id"], item["name"])]
        if not exact:
            exact = [item for item in devices if item["name"].casefold() == requested.casefold()]
        if not exact:
            exact = [item for item in devices if requested.casefold() in item["name"].casefold()]
        if len(exact) != 1:
            names = ", ".join(item["name"] for item in exact or devices) or "none available"
            if exact:
                raise SpotifyError(f"More than one Spotify device matches {requested!r}: {names}.")
            raise SpotifyError(f"No Spotify device matches {requested!r}. Available devices: {names}.")
        return exact[0]["id"]

    def set_volume(self, arguments: dict) -> dict:
        try:
            volume = int((arguments or {}).get("volume_percent"))
        except (TypeError, ValueError) as exc:
            raise SpotifyError("Volume must be a percentage from 0 to 100.") from exc
        if not 0 <= volume <= 100:
            raise SpotifyError("Volume must be a percentage from 0 to 100.")
        params = {"volume_percent": volume, **self._device_params(arguments)}
        self._api_request("PUT", "/me/player/volume", params=params)
        if volume > 0:
            with self._lock:
                if self._manual_mute_restore_volume is not None:
                    self._manual_mute_restore_volume = None
                    self._write_config()
        return {"status": "volume_set", "volume_percent": volume}

    def set_muted(self, arguments: dict) -> dict:
        muted = (arguments or {}).get("muted")
        if type(muted) is not bool:
            raise SpotifyError("Spotify mute must be on or off.")
        state = self.playback_state({})
        device = state.get("device") or {}
        current = device.get("volume_percent")
        if not device.get("supports_volume") or current is None:
            raise SpotifyError("The active Spotify device does not support volume control.")
        device_id = str(device.get("id") or "")
        with self._lock:
            if muted:
                if int(current) > 0:
                    restore_volume = int(current)
                else:
                    restore_volume = int(self._manual_mute_restore_volume or 50)
                target = 0
            else:
                restore_volume = self._manual_mute_restore_volume
                target = int(restore_volume or (current if int(current) > 0 else 50))
        params = {"volume_percent": max(0, min(100, target))}
        if device_id:
            params["device_id"] = device_id
        self._api_request("PUT", "/me/player/volume", params=params)
        with self._lock:
            self._fade_generation += 1
            self._manual_mute_restore_volume = restore_volume if muted else None
            self._write_config()
        return {
            "status": "muted" if muted else "unmuted",
            "muted": muted,
            "volume_percent": target,
            "restore_volume": restore_volume if muted else None,
        }

    def seek(self, arguments: dict) -> dict:
        try:
            position = int((arguments or {}).get("position_ms"))
        except (TypeError, ValueError) as exc:
            raise SpotifyError("Seek position must be a non-negative number of milliseconds.") from exc
        if position < 0:
            raise SpotifyError("Seek position must be a non-negative number of milliseconds.")
        self._api_request("PUT", "/me/player/seek", params={
            "position_ms": position, **self._device_params(arguments),
        })
        return {"status": "seeked", "position_ms": position}

    def set_shuffle(self, arguments: dict) -> dict:
        state = (arguments or {}).get("enabled")
        if type(state) is not bool:
            raise SpotifyError("Shuffle enabled must be true or false.")
        self._api_request("PUT", "/me/player/shuffle", params={
            "state": str(state).lower(), **self._device_params(arguments),
        })
        return {"status": "shuffle_set", "enabled": state}

    def set_repeat(self, arguments: dict) -> dict:
        state = str((arguments or {}).get("mode") or "").strip().lower()
        if state not in {"off", "track", "context"}:
            raise SpotifyError("Repeat mode must be off, track, or context.")
        self._api_request("PUT", "/me/player/repeat", params={
            "state": state, **self._device_params(arguments),
        })
        return {"status": "repeat_set", "mode": state}

    def transfer_playback(self, arguments: dict) -> dict:
        device_id = self._resolve_device_id((arguments or {}).get("device"))
        play = (arguments or {}).get("play", True)
        if type(play) is not bool:
            raise SpotifyError("Transfer play must be true or false.")
        self._api_request("PUT", "/me/player", json={"device_ids": [device_id], "play": play})
        return {"status": "transferred", "device_id": device_id, "playing": play}

    def add_to_queue(self, arguments: dict) -> dict:
        reference = str((arguments or {}).get("track") or "").strip()
        if not reference:
            raise SpotifyError("Provide a song name or Spotify track URI to queue.")
        uri = reference if reference.startswith("spotify:track:") else ""
        selected = None
        if not uri:
            matches = self.search({"query": reference, "type": "track", "limit": 1})["results"]
            if not matches:
                raise SpotifyError(f"Spotify could not find a track for {reference!r}.")
            selected = matches[0]
            uri = str(selected.get("uri") or "")
        self._api_request("POST", "/me/player/queue", params={
            "uri": uri, **self._device_params(arguments),
        })
        return {
            "status": "queued", "uri": uri,
            "track": selected or {"name": reference},
        }

    def _start_volume_fade(self, start: int, target: int, device_id: str, duration_ms: int) -> None:
        with self._lock:
            self._fade_generation += 1
            generation = self._fade_generation

        def run():
            steps = max(2, min(6, round(duration_ms / 175)))
            delay = duration_ms / steps / 1000
            for step in range(1, steps + 1):
                with self._lock:
                    if generation != self._fade_generation:
                        return
                volume = round(start + (target - start) * step / steps)
                try:
                    params = {"volume_percent": max(0, min(100, volume))}
                    if device_id:
                        params["device_id"] = device_id
                    self._api_request("PUT", "/me/player/volume", params=params)
                except SpotifyError:
                    return
                if step < steps:
                    self._watcher_stop.wait(delay)

        threading.Thread(target=run, name="petey-spotify-volume-fade", daemon=True).start()

    def _restore_ducked_volume(self) -> None:
        with self._lock:
            self._duck_restore_timer = None
            if self._duck_clients:
                return
            original = self._duck_original_volume
            device_id = self._duck_device_id
            duration = self._fade_duration_ms
            manually_muted = self._manual_mute_restore_volume is not None
            self._duck_clients.clear()
            timers = list(self._duck_timers.values())
            self._duck_timers.clear()
            self._duck_original_volume = None
            self._duck_device_id = ""
        for timer in timers:
            timer.cancel()
        if original is None:
            return
        try:
            state = self.playback_state({})
            current = state["device"].get("volume_percent")
        except SpotifyError:
            current = None
        target = 0 if manually_muted else original
        self._start_volume_fade(int(current if current is not None else target), target, device_id, duration)

    def speech_duck(self, active: object, client_id: object) -> dict:
        if type(active) is not bool:
            raise SpotifyError("Speech state must be active or inactive.")
        client = str(client_id or "").strip()[:100]
        if not client:
            raise SpotifyError("Speech ducking requires a client ID.")
        with self._lock:
            enabled = self._duck_music
            old_timer = self._duck_timers.pop(client, None)
            restore_timer = self._duck_restore_timer
            if active:
                self._duck_restore_timer = None
        if old_timer is not None:
            old_timer.cancel()
        if active and restore_timer is not None:
            restore_timer.cancel()
        if not enabled:
            return {"status": "disabled"}
        if active:
            with self._lock:
                was_empty = not self._duck_clients
                continuing_session = self._duck_original_volume is not None
                self._duck_clients[client] = time.monotonic()
            if was_empty and not continuing_session:
                try:
                    state = self.playback_state({})
                    device = state["device"]
                    current = device.get("volume_percent")
                    if not state["playing"] or not device.get("supports_volume") or current is None:
                        with self._lock:
                            self._duck_clients.pop(client, None)
                        return {"status": "unsupported"}
                    with self._lock:
                        self._duck_original_volume = int(current)
                        self._duck_device_id = str(device.get("id") or "")
                        target = min(int(current), self._duck_volume)
                        duration = self._fade_duration_ms
                    self._start_volume_fade(int(current), target, self._duck_device_id, duration)
                except SpotifyError:
                    with self._lock:
                        self._duck_clients.pop(client, None)
                    return {"status": "unavailable"}
            timer = threading.Timer(180, lambda: self.speech_duck(False, client))
            timer.daemon = True
            with self._lock:
                self._duck_timers[client] = timer
            timer.start()
            return {"status": "ducked"}
        with self._lock:
            self._duck_clients.pop(client, None)
            should_restore = not self._duck_clients
        if should_restore:
            timer = threading.Timer(DUCK_RELEASE_GRACE_SECONDS, self._restore_ducked_volume)
            timer.daemon = True
            with self._lock:
                previous = self._duck_restore_timer
                self._duck_restore_timer = timer
            if previous is not None:
                previous.cancel()
            timer.start()
        return {"status": "restore_pending" if should_restore else "active_elsewhere"}

    @staticmethod
    def _playback_track(data: object) -> dict | None:
        if not isinstance(data, dict) or not data.get("is_playing"):
            return None
        item = data.get("item") or {}
        if not isinstance(item, dict) or not item.get("uri"):
            return None
        artists = item.get("artists") or []
        album = item.get("album") or {}
        return {
            "uri": str(item["uri"]),
            "name": str(item.get("name") or "Unknown track"),
            "artists": ", ".join(
                str(artist.get("name") or "")
                for artist in artists
                if isinstance(artist, dict) and artist.get("name")
            ),
            "album": str(album.get("name") or "") if isinstance(album, dict) else "",
            "release_date": str(album.get("release_date") or "") if isinstance(album, dict) else "",
            "progress_ms": max(0, int(data.get("progress_ms") or 0)),
            "duration_ms": max(0, int(item.get("duration_ms") or 0)),
        }

    @staticmethod
    def _queued_track(data: object, current_uri: str) -> dict | None:
        queue = data.get("queue") if isinstance(data, dict) else None
        for item in queue if isinstance(queue, list) else []:
            if not isinstance(item, dict) or not item.get("uri") or item.get("uri") == current_uri:
                continue
            artists = item.get("artists") or []
            album = item.get("album") or {}
            images = album.get("images") or [] if isinstance(album, dict) else []
            external_urls = item.get("external_urls") or {}
            return {
                "uri": str(item["uri"]),
                "name": str(item.get("name") or "Unknown track"),
                "artists": ", ".join(
                    str(artist.get("name") or "")
                    for artist in artists
                    if isinstance(artist, dict) and artist.get("name")
                ),
                "album": str(album.get("name") or "") if isinstance(album, dict) else "",
                "release_date": str(album.get("release_date") or "") if isinstance(album, dict) else "",
                "image_url": str(images[0].get("url") or "")
                if images and isinstance(images[0], dict) else "",
                "spotify_url": str(external_urls.get("spotify") or "")
                if isinstance(external_urls, dict) else "",
            }
        return None

    def check_playback_change(self) -> float:
        with self._lock:
            enabled = self._dj_mode
            authorized = self._is_token_valid() or bool(self._refresh_token)
            persona = dict(self._active_persona())
            mix_with_profile = self._mix_dj_with_profile
            post_album_art = self._post_album_art
            wrapup_seconds = self._dj_wrapup_seconds
        if not enabled or not authorized or not callable(self._emit_event):
            self._last_track_uri = None
            self._announced_track_uri = None
            return DJ_MAX_POLL_SECONDS
        track = self._playback_track(
            self._api_request("GET", "/me/player/currently-playing")
        )
        if track is None:
            return DJ_MAX_POLL_SECONDS
        duration_ms = track["duration_ms"]
        remaining_ms = duration_ms - track["progress_ms"]
        with self._lock:
            if track["uri"] != self._last_track_uri:
                self._last_track_uri = track["uri"]
                self._announced_track_uri = None
            if self._announced_track_uri == track["uri"]:
                return DJ_MAX_POLL_SECONDS
            suppress = (
                track["uri"] == self._suppressed_track_uri
                and time.monotonic() < self._suppressed_track_until
            )
            if suppress or time.monotonic() >= self._suppressed_track_until:
                self._suppressed_track_uri = None
                self._suppressed_track_until = 0.0
        if suppress:
            return DJ_MAX_POLL_SECONDS
        if duration_ms <= 0:
            return DJ_MIN_POLL_SECONDS
        cue_remaining_ms = max(int(DJ_MIN_POLL_SECONDS * 1000), wrapup_seconds * 1000)
        if remaining_ms > cue_remaining_ms:
            seconds_until_cue = (remaining_ms - cue_remaining_ms) / 1000
            return min(DJ_MAX_POLL_SECONDS, max(DJ_MIN_POLL_SECONDS, seconds_until_cue))
        upcoming = self._queued_track(
            self._api_request("GET", "/me/player/queue"), track["uri"]
        )
        if upcoming is None:
            return min(DJ_QUEUE_RETRY_SECONDS, max(DJ_MIN_POLL_SECONDS, remaining_ms / 1000))
        with self._lock:
            if self._announced_track_uri == track["uri"]:
                return DJ_MAX_POLL_SECONDS
            self._announced_track_uri = track["uri"]
        song = track["name"]
        artist = track["artists"] or "Unknown artist"
        next_song = upcoming["name"]
        next_artist = upcoming["artists"] or "Unknown artist"
        prompt = persona["prompt"] or DEFAULT_DJ_PROMPT
        rendered_prompt = (
            prompt.replace("{song}", song).replace("{artist}", artist)
            .replace("{next_song}", next_song).replace("{next_artist}", next_artist)
        )
        details = self._persona_instruction(
            persona["name"], rendered_prompt, mix_with_profile,
        )
        if "{next_song}" not in prompt and "{next_artist}" not in prompt:
            details += f"\nUpcoming track: {next_song} by {next_artist}."
        if upcoming["album"] or upcoming["release_date"]:
            details += (
                f"\nUpcoming-track metadata: album {upcoming['album'] or 'Unknown'}; "
                f"release date {upcoming['release_date'] or 'Unknown'}."
            )
        timing = (
            "the outgoing song is ending now"
            if wrapup_seconds == 0 else
            f"Spotify reports about {wrapup_seconds} seconds remain"
        )
        details += (
            f"\nTiming: {timing}. Mention the outgoing song's "
            "title and artist only once, then immediately focus on introducing the supplied upcoming "
            "song. Keep the transition short enough to finish near the changeover."
        )
        metadata = {
            "spotify_track_uri": track["uri"],
            "spotify_next_track_uri": upcoming["uri"],
            "spotify_dj_cue": "track_transition",
            "spotify_dj_persona": persona["name"],
        }
        if post_album_art:
            metadata.update({
                "chat_image_url": upcoming["image_url"],
                "chat_image_link": upcoming["spotify_url"],
                "chat_image_alt": f"Album artwork for {next_song} by {next_artist}",
                "chat_image_caption": f"Up next: {next_song} by {next_artist}",
            })
        self._emit_event(details, speak=True, metadata=metadata)
        return DJ_MAX_POLL_SECONDS

    def _watch_playback(self) -> None:
        delay = DJ_MIN_POLL_SECONDS
        while not self._watcher_stop.wait(delay):
            try:
                delay = self.check_playback_change()
            except (SpotifyError, RuntimeError, ValueError):
                delay = DJ_MAX_POLL_SECONDS

    def start(self) -> None:
        if self._watcher_thread is not None:
            return
        self._watcher_thread = threading.Thread(
            target=self._watch_playback,
            name="petey-spotify-track-watch",
            daemon=True,
        )
        self._watcher_thread.start()

    def _start_callback_server(self) -> None:
        if self._callback_server is not None:
            return
        addon = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_error(404)
                    return
                values = parse_qs(parsed.query)
                error = str(values.get("error", [""])[0])
                try:
                    if error:
                        raise SpotifyError("Spotify authorization was declined: " + error)
                    addon.save_auth(
                        values.get("code", [""])[0],
                        values.get("state", [""])[0],
                    )
                    title = "Spotify connected"
                    message = "Spotify is connected to PETEY. You can close this window."
                    status = 200
                except SpotifyError as exc:
                    title = "Spotify connection failed"
                    message = str(exc)
                    status = 400
                body = (
                    "<!doctype html><meta charset='utf-8'><title>" + html.escape(title) + "</title>"
                    "<style>body{font:18px system-ui;background:#101814;color:#eef8f1;"
                    "max-width:620px;margin:15vh auto;padding:32px}h1{color:#56d68b}</style>"
                    "<h1>" + html.escape(title) + "</h1><p>" + html.escape(message) + "</p>"
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        try:
            self._callback_server = ThreadingHTTPServer(("127.0.0.1", 8888), CallbackHandler)
        except OSError as exc:
            raise SpotifyError("Spotify callback port 8888 is busy. Close the app using it and try again.") from exc
        self._callback_thread = threading.Thread(
            target=self._callback_server.serve_forever,
            name="petey-spotify-oauth",
            daemon=True,
        )
        self._callback_thread.start()

    def get_auth_url(self) -> dict:
        """Build a state-protected Spotify PKCE authorization URL."""
        with self._lock:
            if not self._client_id:
                raise SpotifyError("Set your Spotify Client ID first.")
            self._start_callback_server()
            self._code_verifier = secrets.token_urlsafe(64)
            self._auth_state = secrets.token_urlsafe(24)
            self._auth_expires_at = time.time() + 10 * 60
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(self._code_verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            params = {
                "client_id": self._client_id,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPES,
                "state": self._auth_state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "show_dialog": "true",
            }
            return {
                "url": f"{SPOTIFY_AUTH_URL}?{urlencode(params)}",
                "redirect_uri": REDIRECT_URI,
            }

    def search(self, arguments: dict) -> dict:
        """Search for tracks on Spotify."""
        arguments = arguments if isinstance(arguments, dict) else {}
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise SpotifyError("Provide a search query.")
        search_type = str(arguments.get("type") or "track").strip().lower()
        if search_type not in ("track", "artist", "album", "playlist"):
            search_type = "track"
        try:
            limit = max(1, min(int(arguments.get("limit") or 5), 10))
        except (TypeError, ValueError):
            limit = 5
        data = self._api_request("GET", "/search", params={
            "q": query,
            "type": search_type,
            "limit": limit,
        })
        container = data.get(f"{search_type}s") or {}
        items = container.get("items") or [] if isinstance(container, dict) else []
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            artists = item.get("artists") or []
            artist_names = ", ".join(
                str(artist.get("name") or "")
                for artist in artists
                if isinstance(artist, dict) and artist.get("name")
            )
            album = item.get("album") or {}
            if not isinstance(album, dict):
                album = {}
            if search_type == "track":
                results.append({
                    "name": str(item.get("name") or "Unknown track"),
                    "artist": artist_names,
                    "album": str(album.get("name") or ""),
                    "album_uri": str(album.get("uri") or ""),
                    "duration_ms": item.get("duration_ms", 0),
                    "uri": item.get("uri", ""),
                    "preview_url": item.get("preview_url", ""),
                })
            elif search_type == "artist":
                followers = item.get("followers") or {}
                results.append({
                    "name": str(item.get("name") or "Unknown artist"),
                    "followers": followers.get("total", 0) if isinstance(followers, dict) else 0,
                    "genres": item.get("genres") or [],
                    "uri": item.get("uri", ""),
                })
            elif search_type == "album":
                results.append({
                    "name": str(item.get("name") or "Unknown album"),
                    "artist": artist_names,
                    "release_date": item.get("release_date", ""),
                    "uri": item.get("uri", ""),
                })
            elif search_type == "playlist":
                owner = item.get("owner") or {}
                tracks = item.get("tracks") or {}
                results.append({
                    "name": str(item.get("name") or "Unknown playlist"),
                    "owner": owner.get("display_name", "") if isinstance(owner, dict) else "",
                    "tracks": tracks.get("total", 0) if isinstance(tracks, dict) else 0,
                    "uri": item.get("uri", ""),
                })
        return {"query": query, "type": search_type, "count": len(results), "results": results}

    def _album_context_for_track(self, uri: str) -> str:
        """Resolve a track URI to its album so playback has a next item."""
        prefix = "spotify:track:"
        if not uri.startswith(prefix):
            return ""
        track_id = uri[len(prefix):].strip()
        if not track_id or ":" in track_id:
            return ""
        try:
            track = self._api_request("GET", f"/tracks/{track_id}")
        except SpotifyError:
            return ""
        album = (track.get("album") or {}) if isinstance(track, dict) else {}
        return str(album.get("uri") or "") if isinstance(album, dict) else ""

    def play(self, arguments: dict, *, suppress_dj: bool = False) -> dict:
        """Start playback from a Spotify URI or a human song search."""
        arguments = arguments if isinstance(arguments, dict) else {}
        uri = str(arguments.get("uri") or "").strip()
        context_uri = str(arguments.get("context_uri") or "").strip()
        query = str(arguments.get("query") or "").strip()
        position_ms = arguments.get("position_ms")
        if uri and not uri.startswith("spotify:"):
            query, uri = uri, ""
        selected = None
        if query and not uri and not context_uri:
            matches = self.search({"query": query, "type": "track", "limit": 1})["results"]
            if not matches or not matches[0].get("uri"):
                raise SpotifyError(f"Spotify could not find a playable track for {query!r}.")
            selected = matches[0]
            uri = str(selected["uri"])
            context_uri = str(selected.get("album_uri") or "")
        if not uri and not context_uri:
            raise SpotifyError("Provide a song name, track URI, or context URI to play.")
        if uri and not context_uri:
            context_uri = self._album_context_for_track(uri)
        payload = {}
        if uri and context_uri:
            payload["context_uri"] = context_uri
            payload["offset"] = {"uri": uri}
        elif uri:
            payload["uris"] = [uri]
        else:
            payload["context_uri"] = context_uri
        if position_ms is not None:
            payload["position_ms"] = int(position_ms)
        suppress_uri = uri if suppress_dj and uri.startswith("spotify:track:") else ""
        if suppress_uri:
            with self._lock:
                self._suppressed_track_uri = suppress_uri
                self._suppressed_track_until = time.monotonic() + DJ_TOOL_SUPPRESSION_SECONDS
        try:
            self._api_request("PUT", "/me/player/play", json=payload)
        except Exception:
            with self._lock:
                if self._suppressed_track_uri == suppress_uri:
                    self._suppressed_track_uri = None
                    self._suppressed_track_until = 0.0
            raise
        result = {"status": "playing", "uri": uri or context_uri}
        if selected:
            result["track"] = selected
            result["message"] = f"Playing {selected['name']} by {selected.get('artist') or 'unknown artist'}."
        return result

    def play_from_chat(self, arguments: dict) -> dict:
        """Play a requested track and prevent DJ Mode from echoing PETEY's own reply."""
        return self.play(arguments, suppress_dj=True)

    def pause(self, _arguments: dict) -> dict:
        """Pause playback on Spotify."""
        self._api_request("PUT", "/me/player/pause")
        return {"status": "paused"}

    def resume(self, _arguments: dict) -> dict:
        """Resume playback on Spotify."""
        self._api_request("PUT", "/me/player/play")
        return {"status": "resumed"}

    def skip_next(self, _arguments: dict) -> dict:
        """Skip to the next track."""
        self._api_request("POST", "/me/player/next")
        return {"status": "skipped_next"}

    def skip_previous(self, _arguments: dict) -> dict:
        """Skip to the previous track."""
        self._api_request("POST", "/me/player/previous")
        return {"status": "skipped_previous"}

    def currently_playing(self, _arguments: dict) -> dict:
        """Get the currently playing track."""
        data = self._api_request("GET", "/me/player/currently-playing")
        if not data:
            return {
                "playing": False, "has_track": False,
                "muted": self._manual_mute_restore_volume is not None,
                "restore_volume": self._manual_mute_restore_volume,
                "message": "Nothing is currently playing.",
            }
        item = data.get("item") or {}
        if not isinstance(item, dict) or not item:
            return {
                "playing": False, "has_track": False,
                "muted": self._manual_mute_restore_volume is not None,
                "restore_volume": self._manual_mute_restore_volume,
                "message": "Spotify did not report a playable track.",
            }
        artists = item.get("artists") or []
        album = item.get("album") or {}
        images = album.get("images") or [] if isinstance(album, dict) else []
        external_urls = item.get("external_urls") or {}
        return {
            "playing": bool(data.get("is_playing")),
            "has_track": True,
            "name": item.get("name", ""),
            "artist": ", ".join(str(a.get("name") or "") for a in artists if isinstance(a, dict)),
            "album": album.get("name", "") if isinstance(album, dict) else "",
            "image_url": str(images[0].get("url") or "")
            if images and isinstance(images[0], dict) else "",
            "spotify_url": str(external_urls.get("spotify") or "")
            if isinstance(external_urls, dict) else "",
            "progress_ms": max(0, int(data.get("progress_ms") or 0)),
            "duration_ms": max(0, int(item.get("duration_ms") or 0)),
            "uri": item.get("uri", ""),
            "muted": self._manual_mute_restore_volume is not None,
            "restore_volume": self._manual_mute_restore_volume,
        }

    def get_top_tracks(self, arguments: dict) -> dict:
        """Get user's top tracks."""
        time_range = arguments.get("time_range", "short_term")
        if time_range not in ("short_term", "medium_term", "long_term"):
            time_range = "short_term"
        try:
            limit = max(1, min(int(arguments.get("limit") or 10), 50))
        except (TypeError, ValueError):
            limit = 10
        data = self._api_request("GET", "/me/top/tracks", params={
            "time_range": time_range,
            "limit": limit,
        })
        items = data.get("items") or []
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            artists = item.get("artists") or []
            album = item.get("album") or {}
            results.append({
                "name": str(item.get("name") or "Unknown track"),
                "artist": ", ".join(str(a.get("name") or "") for a in artists if isinstance(a, dict)),
                "album": album.get("name", "") if isinstance(album, dict) else "",
                "album_uri": album.get("uri", "") if isinstance(album, dict) else "",
                "uri": item.get("uri", ""),
            })
        return {"time_range": time_range, "count": len(results), "tracks": results}

    @staticmethod
    def _playlist_id(reference: object) -> str:
        value = str(reference or "").strip()
        if value.startswith("spotify:playlist:"):
            value = value.split(":", 2)[2]
        elif "open.spotify.com/playlist/" in value:
            value = value.split("open.spotify.com/playlist/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        return value if re.fullmatch(r"[A-Za-z0-9]{8,80}", value) else ""

    @staticmethod
    def _playlist_summary(item: object) -> dict | None:
        if not isinstance(item, dict) or not item.get("id"):
            return None
        owner = item.get("owner") or {}
        contents = item.get("items") or item.get("tracks") or {}
        return {
            "id": str(item["id"]),
            "uri": str(item.get("uri") or ""),
            "name": str(item.get("name") or "Untitled playlist"),
            "description": str(item.get("description") or ""),
            "owner": str(owner.get("display_name") or "") if isinstance(owner, dict) else "",
            "public": item.get("public"),
            "collaborative": bool(item.get("collaborative")),
            "item_count": int(contents.get("total") or 0) if isinstance(contents, dict) else 0,
            "snapshot_id": str(item.get("snapshot_id") or ""),
        }

    def list_playlists(self, arguments: dict) -> dict:
        try:
            limit = max(1, min(int((arguments or {}).get("limit") or 20), 50))
            offset = max(0, min(int((arguments or {}).get("offset") or 0), 100_000))
        except (TypeError, ValueError) as exc:
            raise SpotifyError("Playlist limit and offset must be numbers.") from exc
        data = self._api_request("GET", "/me/playlists", params={"limit": limit, "offset": offset})
        playlists = []
        for item in data.get("items") or []:
            summary = self._playlist_summary(item)
            if summary:
                playlists.append(summary)
        return {
            "count": len(playlists),
            "total": int(data.get("total") or len(playlists)),
            "offset": offset,
            "playlists": playlists,
        }

    def _resolve_playlist(self, reference: object) -> dict:
        requested = str(reference or "").strip()
        if not requested:
            raise SpotifyError("Provide a playlist name, URI, URL, or ID.")
        playlist_id = self._playlist_id(requested)
        if playlist_id:
            data = self._api_request("GET", f"/playlists/{playlist_id}")
            summary = self._playlist_summary(data)
            if summary:
                return summary
            raise SpotifyError("Spotify did not return that playlist.")
        matches = []
        offset = 0
        while offset < 500:
            page = self.list_playlists({"limit": 50, "offset": offset})
            matches.extend(
                item for item in page["playlists"]
                if item["name"].casefold() == requested.casefold()
            )
            offset += page["count"]
            if matches or page["count"] == 0 or offset >= page["total"]:
                break
        if not matches:
            raise SpotifyError(f"No Spotify playlist named {requested!r} was found.")
        if len(matches) > 1:
            choices = ", ".join(f"{item['name']} ({item['id']})" for item in matches[:8])
            raise SpotifyError(f"More than one playlist has that name: {choices}. Use its ID.")
        return matches[0]

    def get_playlist_items(self, arguments: dict) -> dict:
        playlist = self._resolve_playlist((arguments or {}).get("playlist"))
        try:
            limit = max(1, min(int((arguments or {}).get("limit") or 50), 50))
            offset = max(0, min(int((arguments or {}).get("offset") or 0), 100_000))
        except (TypeError, ValueError) as exc:
            raise SpotifyError("Playlist limit and offset must be numbers.") from exc
        data = self._api_request(
            "GET", f"/playlists/{playlist['id']}/items",
            params={"limit": limit, "offset": offset},
        )
        items = []
        for entry in data.get("items") or []:
            item = entry.get("item") or entry.get("track") or {} if isinstance(entry, dict) else {}
            if not isinstance(item, dict) or not item.get("uri"):
                continue
            artists = item.get("artists") or []
            items.append({
                "name": str(item.get("name") or "Unknown item"),
                "artist": ", ".join(
                    str(artist.get("name") or "") for artist in artists
                    if isinstance(artist, dict) and artist.get("name")
                ),
                "uri": str(item.get("uri") or ""),
                "type": str(item.get("type") or "track"),
            })
        return {
            "playlist": playlist,
            "count": len(items),
            "total": int(data.get("total") or len(items)),
            "offset": offset,
            "items": items,
        }

    @staticmethod
    def _requested_tracks(arguments: dict) -> list[str]:
        value = (arguments or {}).get("tracks")
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = value
        else:
            values = []
        tracks = [str(item or "").strip() for item in values]
        tracks = [item for item in tracks if item]
        if not tracks:
            raise SpotifyError("Provide at least one song name or Spotify track URI.")
        if len(tracks) > MAX_PLAYLIST_ITEMS:
            raise SpotifyError(f"A maximum of {MAX_PLAYLIST_ITEMS} playlist items can be changed at once.")
        return tracks

    def _resolve_track_uris(self, tracks: list[str]) -> tuple[list[str], list[dict]]:
        uris = []
        resolved = []
        for reference in tracks:
            if reference.startswith("spotify:track:") or reference.startswith("spotify:episode:"):
                uris.append(reference)
                resolved.append({"query": reference, "uri": reference})
                continue
            matches = self.search({"query": reference, "type": "track", "limit": 1})["results"]
            if not matches or not matches[0].get("uri"):
                raise SpotifyError(f"Spotify could not find a track for {reference!r}.")
            track = matches[0]
            uris.append(str(track["uri"]))
            resolved.append({
                "query": reference,
                "name": track.get("name", ""),
                "artist": track.get("artist", ""),
                "uri": track["uri"],
            })
        return uris, resolved

    def create_playlist(self, arguments: dict) -> dict:
        name = str((arguments or {}).get("name") or "").strip()
        if not name or len(name) > 100:
            raise SpotifyError("Playlist name must contain 1 to 100 characters.")
        public = (arguments or {}).get("public", False)
        if type(public) is not bool:
            raise SpotifyError("Playlist public must be true or false.")
        description = str((arguments or {}).get("description") or "").strip()[:300]
        created = self._api_request("POST", "/me/playlists", json={
            "name": name, "public": public, "description": description,
        })
        playlist = self._playlist_summary(created)
        if playlist is None:
            raise SpotifyError("Spotify created the playlist but did not return its ID.")
        tracks = (arguments or {}).get("tracks")
        added = []
        if tracks:
            result = self.add_playlist_items({"playlist": playlist["id"], "tracks": tracks})
            added = result["added"]
        return {"status": "created", "playlist": playlist, "added": added}

    def add_playlist_items(self, arguments: dict) -> dict:
        playlist = self._resolve_playlist((arguments or {}).get("playlist"))
        uris, resolved = self._resolve_track_uris(self._requested_tracks(arguments))
        payload = {"uris": uris}
        if (arguments or {}).get("position") is not None:
            try:
                position = int(arguments["position"])
            except (TypeError, ValueError) as exc:
                raise SpotifyError("Playlist position must be a non-negative number.") from exc
            if position < 0:
                raise SpotifyError("Playlist position must be a non-negative number.")
            payload["position"] = position
        data = self._api_request("POST", f"/playlists/{playlist['id']}/items", json=payload)
        return {
            "status": "added", "playlist": playlist,
            "added": resolved, "snapshot_id": str(data.get("snapshot_id") or ""),
        }

    def remove_playlist_items(self, arguments: dict) -> dict:
        playlist = self._resolve_playlist((arguments or {}).get("playlist"))
        uris, resolved = self._resolve_track_uris(self._requested_tracks(arguments))
        data = self._api_request(
            "DELETE", f"/playlists/{playlist['id']}/items",
            json={"items": [{"uri": uri} for uri in uris]},
        )
        return {
            "status": "removed", "playlist": playlist,
            "removed": resolved, "snapshot_id": str(data.get("snapshot_id") or ""),
        }

    def update_playlist(self, arguments: dict) -> dict:
        playlist = self._resolve_playlist((arguments or {}).get("playlist"))
        payload = {}
        if "name" in (arguments or {}):
            name = str(arguments.get("name") or "").strip()
            if not name or len(name) > 100:
                raise SpotifyError("Playlist name must contain 1 to 100 characters.")
            payload["name"] = name
        if "description" in (arguments or {}):
            payload["description"] = str(arguments.get("description") or "").strip()[:300]
        if "public" in (arguments or {}):
            if type(arguments.get("public")) is not bool:
                raise SpotifyError("Playlist public must be true or false.")
            payload["public"] = arguments["public"]
        if not payload:
            raise SpotifyError("Provide a new playlist name, description, or visibility.")
        self._api_request("PUT", f"/playlists/{playlist['id']}", json=payload)
        return {"status": "updated", "playlist": playlist, "changes": payload}

    def tool_specs(self) -> list:
        def music_intent(message):
            return bool(MUSIC_INTENT.search(message or ""))

        specs = [
            ToolSpec(
                name="search_spotify",
                description="Search for tracks, artists, albums, or playlists on Spotify.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (song name, artist, etc.)",
                        },
                        "type": {
                            "type": "string",
                            "description": "Search type: track, artist, album, or playlist. Defaults to track.",
                            "enum": ["track", "artist", "album", "playlist"],
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of results (1-10, default 5).",
                            "minimum": 1,
                            "maximum": 10,
                        },
                    },
                    "required": ["query"],
                },
                handler=self.search,
                available_when=music_intent,
            ),
            ToolSpec(
                name="play_spotify",
                description=(
                    "Play music on Spotify. For a normal request such as 'play Now on Spotify', "
                    "pass the requested song and artist words in query; the add-on searches and "
                    "plays the first matching track. Use uri only when a Spotify URI is already known."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "description": "Spotify track URI (e.g., spotify:track:4PTG3Z6ehGkBFwjyBzQGy7)",
                        },
                        "query": {
                            "type": "string",
                            "description": "Song title and optional artist to find and play.",
                        },
                        "context_uri": {
                            "type": "string",
                            "description": "Spotify context URI (album or playlist)",
                        },
                        "position_ms": {
                            "type": "integer",
                            "description": "Position in milliseconds to start from",
                        },
                    },
                },
                handler=self.play_from_chat,
                available_when=music_intent,
            ),
            ToolSpec(
                name="pause_spotify",
                description="Pause the currently playing track on Spotify.",
                parameters={"type": "object", "properties": {}},
                handler=self.pause,
                available_when=music_intent,
            ),
            ToolSpec(
                name="resume_spotify",
                description="Resume playback on Spotify.",
                parameters={"type": "object", "properties": {}},
                handler=self.resume,
                available_when=music_intent,
            ),
            ToolSpec(
                name="skip_next_spotify",
                description="Skip to the next track on Spotify.",
                parameters={"type": "object", "properties": {}},
                handler=self.skip_next,
                available_when=music_intent,
            ),
            ToolSpec(
                name="skip_previous_spotify",
                description="Skip to the previous track on Spotify.",
                parameters={"type": "object", "properties": {}},
                handler=self.skip_previous,
                available_when=music_intent,
            ),
            ToolSpec(
                name="currently_playing_spotify",
                description="Get the currently playing track on Spotify.",
                parameters={"type": "object", "properties": {}},
                handler=self.currently_playing,
                available_when=music_intent,
            ),
            ToolSpec(
                name="get_top_tracks_spotify",
                description="Get the user's top tracks on Spotify.",
                parameters={
                    "type": "object",
                    "properties": {
                        "time_range": {
                            "type": "string",
                            "description": "Time range: short_term (4 weeks), medium_term (6 months), or long_term (years). Defaults to short_term.",
                            "enum": ["short_term", "medium_term", "long_term"],
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of tracks (1-50, default 10).",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                },
                handler=self.get_top_tracks,
                available_when=music_intent,
            ),
            ToolSpec(
                name="get_spotify_playback_state",
                description="Get Spotify playback state, active device, volume, shuffle, and repeat mode.",
                parameters={"type": "object", "properties": {}},
                handler=self.playback_state,
                available_when=lambda message: bool(PLAYER_CONTROL_INTENT.search(message or "")),
                control_capability="spotify_player_control",
                control_label="Control Spotify playback",
            ),
            ToolSpec(
                name="list_spotify_devices",
                description="List available Spotify Connect devices and their current volume support.",
                parameters={"type": "object", "properties": {}},
                handler=self.list_devices,
                available_when=lambda message: bool(PLAYER_CONTROL_INTENT.search(message or "")),
                control_capability="spotify_player_control",
                control_label="Control Spotify playback",
            ),
            ToolSpec(
                name="set_spotify_volume",
                description="Set Spotify playback volume from 0 to 100 percent.",
                parameters={
                    "type": "object",
                    "properties": {
                        "volume_percent": {"type": "integer", "minimum": 0, "maximum": 100},
                        "device_id": {"type": "string", "description": "Optional Spotify device ID."},
                    },
                    "required": ["volume_percent"],
                },
                handler=self.set_volume,
                available_when=lambda message: bool(PLAYER_CONTROL_INTENT.search(message or "")),
                explicit_when=lambda message: bool(re.search(r"\b(volume|louder|quieter)\b", message or "", re.I)),
                control_capability="spotify_player_control",
                control_label="Control Spotify playback",
            ),
            ToolSpec(
                name="seek_spotify",
                description="Seek the current Spotify track to an exact position in milliseconds.",
                parameters={
                    "type": "object",
                    "properties": {
                        "position_ms": {"type": "integer", "minimum": 0},
                        "device_id": {"type": "string"},
                    },
                    "required": ["position_ms"],
                },
                handler=self.seek,
                available_when=lambda message: bool(PLAYER_CONTROL_INTENT.search(message or "")),
                control_capability="spotify_player_control",
                control_label="Control Spotify playback",
            ),
            ToolSpec(
                name="set_spotify_shuffle",
                description="Turn Spotify shuffle on or off.",
                parameters={
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}, "device_id": {"type": "string"}},
                    "required": ["enabled"],
                },
                handler=self.set_shuffle,
                available_when=lambda message: bool(PLAYER_CONTROL_INTENT.search(message or "")),
                control_capability="spotify_player_control",
                control_label="Control Spotify playback",
            ),
            ToolSpec(
                name="set_spotify_repeat",
                description="Set Spotify repeat mode to off, track, or context.",
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["off", "track", "context"]},
                        "device_id": {"type": "string"},
                    },
                    "required": ["mode"],
                },
                handler=self.set_repeat,
                available_when=lambda message: bool(PLAYER_CONTROL_INTENT.search(message or "")),
                control_capability="spotify_player_control",
                control_label="Control Spotify playback",
            ),
            ToolSpec(
                name="transfer_spotify_playback",
                description="Move Spotify playback to an available device selected by name or ID.",
                parameters={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string", "description": "Spotify device name or ID."},
                        "play": {"type": "boolean", "description": "Continue playback after transfer."},
                    },
                    "required": ["device"],
                },
                handler=self.transfer_playback,
                available_when=lambda message: bool(PLAYER_CONTROL_INTENT.search(message or "")),
                control_capability="spotify_player_control",
                control_label="Control Spotify playback",
            ),
            ToolSpec(
                name="add_to_spotify_queue",
                description="Add a song, track URI, or episode URI to the current Spotify queue.",
                parameters={
                    "type": "object",
                    "properties": {
                        "track": {"type": "string", "description": "Song and artist search or Spotify URI."},
                        "device_id": {"type": "string"},
                    },
                    "required": ["track"],
                },
                handler=self.add_to_queue,
                available_when=lambda message: bool(PLAYER_CONTROL_INTENT.search(message or "")),
                control_capability="spotify_player_control",
                control_label="Control Spotify playback",
            ),
            ToolSpec(
                name="list_spotify_playlists",
                description="List playlists owned or followed by the current Spotify user.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        "offset": {"type": "integer", "minimum": 0},
                    },
                },
                handler=self.list_playlists,
                available_when=lambda message: bool(PLAYLIST_INTENT.search(message or "")),
                control_capability="spotify_playlist_read",
                control_label="Read Spotify playlists",
            ),
            ToolSpec(
                name="get_spotify_playlist_items",
                description="Read songs and episodes in one of the current user's Spotify playlists.",
                parameters={
                    "type": "object",
                    "properties": {
                        "playlist": {"type": "string", "description": "Playlist name, URI, URL, or ID."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        "offset": {"type": "integer", "minimum": 0},
                    },
                    "required": ["playlist"],
                },
                handler=self.get_playlist_items,
                available_when=lambda message: bool(PLAYLIST_INTENT.search(message or "")),
                control_capability="spotify_playlist_read",
                control_label="Read Spotify playlists",
            ),
            ToolSpec(
                name="create_spotify_playlist",
                description="Create a public or private Spotify playlist and optionally add songs to it.",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "public": {"type": "boolean"},
                        "tracks": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                    },
                    "required": ["name"],
                },
                handler=self.create_playlist,
                available_when=lambda message: bool(PLAYLIST_WRITE_INTENT.search(message or "")),
                explicit_when=lambda message: bool(PLAYLIST_WRITE_INTENT.search(message or "")),
                control_capability="spotify_playlist_write",
                control_label="Create and edit Spotify playlists",
            ),
            ToolSpec(
                name="add_spotify_playlist_items",
                description="Add one or more songs to a Spotify playlist, resolving song names automatically.",
                parameters={
                    "type": "object",
                    "properties": {
                        "playlist": {"type": "string", "description": "Playlist name, URI, URL, or ID."},
                        "tracks": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
                        "position": {"type": "integer", "minimum": 0},
                    },
                    "required": ["playlist", "tracks"],
                },
                handler=self.add_playlist_items,
                available_when=lambda message: bool(PLAYLIST_WRITE_INTENT.search(message or "")),
                explicit_when=lambda message: bool(PLAYLIST_WRITE_INTENT.search(message or "")),
                control_capability="spotify_playlist_write",
                control_label="Create and edit Spotify playlists",
            ),
            ToolSpec(
                name="remove_spotify_playlist_items",
                description="Remove one or more songs from a Spotify playlist without clearing other items.",
                parameters={
                    "type": "object",
                    "properties": {
                        "playlist": {"type": "string", "description": "Playlist name, URI, URL, or ID."},
                        "tracks": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
                    },
                    "required": ["playlist", "tracks"],
                },
                handler=self.remove_playlist_items,
                available_when=lambda message: bool(PLAYLIST_WRITE_INTENT.search(message or "")),
                explicit_when=lambda message: bool(PLAYLIST_WRITE_INTENT.search(message or "")),
                control_capability="spotify_playlist_write",
                control_label="Create and edit Spotify playlists",
            ),
            ToolSpec(
                name="update_spotify_playlist",
                description="Rename a Spotify playlist or change its description or public visibility.",
                parameters={
                    "type": "object",
                    "properties": {
                        "playlist": {"type": "string", "description": "Playlist name, URI, URL, or ID."},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "public": {"type": "boolean"},
                    },
                    "required": ["playlist"],
                },
                handler=self.update_playlist,
                available_when=lambda message: bool(PLAYLIST_WRITE_INTENT.search(message or "")),
                explicit_when=lambda message: bool(PLAYLIST_WRITE_INTENT.search(message or "")),
                control_capability="spotify_playlist_write",
                control_label="Create and edit Spotify playlists",
            ),
        ]
        return specs

    def close(self) -> None:
        with self._lock:
            original = self._duck_original_volume
            device_id = self._duck_device_id
            timers = list(self._duck_timers.values())
            self._duck_timers.clear()
            restore_timer, self._duck_restore_timer = self._duck_restore_timer, None
            self._duck_clients.clear()
            self._fade_generation += 1
        for timer in timers:
            timer.cancel()
        if restore_timer is not None:
            restore_timer.cancel()
        if original is not None:
            try:
                params = {"volume_percent": original}
                if device_id:
                    params["device_id"] = device_id
                self._api_request("PUT", "/me/player/volume", params=params)
            except SpotifyError:
                pass
        self._watcher_stop.set()
        watcher, self._watcher_thread = self._watcher_thread, None
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=2)
        server, self._callback_server = self._callback_server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._callback_thread = self._callback_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)


def setup(context):
    addon = SpotifyAddon(context)
    prefix = f"/api/addons/{context.addon_id}"
    endpoint_prefix = f"addon_{context.addon_id.replace('-', '_')}"

    def safely(operation):
        try:
            return jsonify(operation())
        except SpotifyError as exc:
            return jsonify({"error": str(exc)}), 400

    def get_status():
        return jsonify(addon.status())

    def update_config():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a JSON object."}), 400
        return safely(lambda: addon.configure(payload.get("client_id", "")))

    def get_auth_url():
        return safely(addon.get_auth_url)

    def save_auth():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a JSON object."}), 400
        return safely(lambda: addon.save_auth(
            payload.get("code", ""), payload.get("state", "")
        ))

    def save_dj_mode():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a JSON object."}), 400
        return safely(lambda: addon.configure_dj_mode(
            payload.get("enabled"), payload.get("prompt"),
            personas=payload.get("personas"),
            active_persona=payload.get("active_persona"),
            mix_with_profile=payload.get("mix_with_profile", False),
            post_album_art=payload.get("post_album_art", True),
            wrapup_seconds=payload.get("wrapup_seconds"),
        ))

    def save_audio_ducking():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a JSON object."}), 400
        return safely(lambda: addon.configure_audio_ducking(
            payload.get("enabled"), payload.get("volume_percent"),
            payload.get("fade_duration_ms"),
        ))

    def speech_duck():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a JSON object."}), 400
        return safely(lambda: addon.speech_duck(
            payload.get("active"), payload.get("client_id")
        ))

    def do_search():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "query" not in payload:
            return jsonify({"error": "Expected a JSON object with 'query'."}), 400
        return safely(lambda: addon.search(payload))

    def do_play():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Expected a JSON object."}), 400
        return safely(lambda: addon.play(payload))

    def do_pause():
        return safely(lambda: addon.pause({}))

    def do_resume():
        return safely(lambda: addon.resume({}))

    def do_next():
        return safely(lambda: addon.skip_next({}))

    def do_previous():
        return safely(lambda: addon.skip_previous({}))

    def do_currently_playing():
        return safely(lambda: addon.currently_playing({}))

    def do_seek():
        payload = request.get_json(silent=True) or {}
        return safely(lambda: addon.seek(payload))

    def do_mute():
        payload = request.get_json(silent=True) or {}
        return safely(lambda: addon.set_muted(payload))

    def do_top_tracks():
        payload = request.get_json(silent=True) or {}
        return safely(lambda: addon.get_top_tracks(payload))

    routes = (
        ("status", "status", ["GET"], get_status),
        ("config", "config", ["PUT"], update_config),
        ("auth-url", "auth_url", ["GET"], get_auth_url),
        ("auth", "auth", ["POST"], save_auth),
        ("announcements", "announcements", ["PUT"], save_dj_mode),
        ("audio-ducking", "audio_ducking", ["PUT"], save_audio_ducking),
        ("speech-duck", "speech_duck", ["POST"], speech_duck),
        ("search", "search", ["POST"], do_search),
        ("play", "play", ["POST"], do_play),
        ("pause", "pause", ["POST"], do_pause),
        ("resume", "resume", ["POST"], do_resume),
        ("next", "next", ["POST"], do_next),
        ("previous", "previous", ["POST"], do_previous),
        ("currently-playing", "currently_playing", ["GET"], do_currently_playing),
        ("seek", "seek", ["POST"], do_seek),
        ("mute", "mute", ["POST"], do_mute),
        ("top-tracks", "top_tracks", ["POST"], do_top_tracks),
    )

    for path, name, methods, handler in routes:
        context.app.add_url_rule(
            f"{prefix}/{path}",
            endpoint=f"{endpoint_prefix}_{name}",
            view_func=handler,
            methods=methods,
        )

    addon.start()
    return addon
