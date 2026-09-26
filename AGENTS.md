# Spotify add-on coding-agent context

Scope: this repository. Current add-on version: 0.7.1; PETEY add-on API: 1.
Read PETEY Desktop's `ADDONS.md` and `docs/addons.md` before changing host contracts.

## Product boundary

This add-on integrates Spotify Web API playback, search, devices, queue, playlists,
personal top tracks, DJ transitions, and a live Command Center player. It controls
Spotify Connect; it does not stream Spotify audio through PETEY. A Spotify Premium
account and an active Connect device are required for playback operations.

## Architecture and data flow

- `petey-addon.json` declares ID `spotify`, API 1, update repository, and panel assets.
- `addon.py:SpotifyAddon` owns OAuth PKCE, token refresh, Web API requests, playback
  operations, DJ configuration, speech ducking, the playback watcher, tools, and API.
- `_load_config()`/`_write_config()` store bounded nonsecret settings.
  `_load_token()`/`_save_token()` keep OAuth tokens in private add-on data with
  owner-only permissions where supported. Public status never includes tokens.
- `get_auth_url()` creates state and PKCE data; the loopback callback server accepts
  the matching state once; `save_auth()` exchanges the code and persists tokens.
- `_api_request()` is the single authenticated Spotify transport and refresh path.
- `playback_state()` normalizes active and paused tracks. The watcher drives DJ
  transitions and Command Center state without confusing paused metadata for no track.
- DJ mode stores three validated personas. A transition may combine the selected DJ
  instructions with PETEY's system profile and may emit album art plus a Spotify link.
- Speech ducking captures the exact device volume once, shares it across consecutive
  speech segments, fades safely, and restores only after the final release grace period.
- `tool_specs()` provides separately gated read/playback/playlist capabilities.
  Handler validation remains authoritative even if schemas are bypassed.
- `panel.js` owns OAuth/setup, controls, playhead polling, DJ editor, and the bounded
  `window.peteyInterface` Command Center panel.

## Preserve these contracts

- Never expose client configuration secrets, access tokens, refresh tokens, PKCE
  verifier, or OAuth state to tools, status JSON, logs, or emitted chat content.
- OAuth uses Authorization Code with PKCE and exact state matching. Keep the callback
  loopback-only and close it during shutdown.
- Validate Spotify URIs, IDs, device references, volume, seek, repeat, shuffle,
  playlist visibility, item counts, and result limits at handler dispatch.
- Keep tool availability intent-gated and PETEY-control-gated. Playlist mutations and
  playback actions must not occur from unrelated chat.
- Audio ducking must preserve the user's pre-speech volume across overlapping segments,
  device changes, mute/unmute, failures, and close. Avoid accumulating fade threads.
- DJ polling must be bounded, stop cleanly, avoid repeat announcements, and never post
  album art unless the configured option permits it.
- Use `textContent` for remote Spotify metadata. Keep external image/link URLs HTTPS.
- Writable state belongs in `addon-data/spotify`; disabled discovery starts no watcher.

## Task map

| Task | Primary anchors |
| --- | --- |
| OAuth/tokens | `_load_token`, `_save_token`, `get_auth_url`, `save_auth` |
| Web API transport | `_api_request`, token refresh helpers |
| Playback/devices | `playback_state`, `play`, pause/seek/volume/device methods |
| Playlists | list/resolve/create/add/remove/update methods |
| DJ transitions | persona validators, `check_playback_change`, `_watch_playback` |
| Speech ducking | `_start_volume_fade`, `speech_duck`, `_restore_ducked_volume` |
| Model tools | `tool_specs` |
| API/panel | `setup`, `panel.js`, `panel.html`, `panel.css` |
| Tests | `tests/test_audio_ducking.py`, `test_dj_personas.py`, `test_now_playing.py` |

## Validation

```bash
PYTHONPATH=/path/to/PETEY-DESKTOP python -m unittest discover -s tests -v
python -m py_compile addon.py
python -m json.tool petey-addon.json >/dev/null
node --check panel.js
```

Routine tests must mock Spotify and must not use a real account. Live smoke tests use
a dedicated app/device and should cover authorization, token refresh, paused state,
volume restore, one DJ transition, and shutdown. Bump manifest, README, changelog,
release archive name, and repository tag together for releases.
