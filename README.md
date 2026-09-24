# Spotify Add-on for PETEY

Search and play songs on Spotify using the Spotify Web API.

Current version: **0.4.2**

## Features

- Search for tracks, artists, albums, and playlists on Spotify
- Play natural-language song requests without requiring PETEY to construct a Spotify URI
- Continue through the song's album after a requested track finishes
- Optionally cue PETEY about twenty seconds before a song ends to wrap up the outgoing track and introduce the queued song
- Post the upcoming song's album artwork in the chat feed with a Spotify link
- Fade Spotify down while PETEY speaks and restore the previous device volume afterward
- Control volume, seek position, shuffle, repeat, queue, and Spotify Connect devices
- List and inspect your playlists, create playlists, add or remove songs, and edit playlist details
- Play tracks and playlists on active Spotify devices
- Pause, resume, skip forward, and skip backward playback
- View the currently playing track
- Access your personal top tracks across different time ranges
- Conversational tools that Petey can invoke when you mention music/Spotify intent

## Setup

### 1. Create a Spotify App

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Click **Create App**
3. Give it a name (e.g., "PETEY Spotify")
4. Set the **Redirect URI** to `http://127.0.0.1:8888/callback`
5. Copy the **Client ID**

### 2. Install the Add-on

Download `petey-spotify-v0.4.2.zip` from the [latest release](https://github.com/bizzomephisto/PETEY-SPOTIFY/releases/latest), then extract it.

Copy the `spotify/` folder to your PETEY data root's `addons/` directory:

```
<petey-data-root>/addons/spotify/
```

### 3. Enable the Add-on

1. Open PETEY's **Add-ons** screen
2. Find **Spotify** and enable it
3. Restart PETEY

### 4. Configure

1. Open the **Spotify** sidebar panel
2. Paste your **Client ID**
3. Click **Authorize with Spotify**
4. Complete authorization in your browser; PETEY receives the callback automatically

## Conversational Tools

Petey gains these tools when the add-on is enabled and authorized:

| Tool | Description |
|------|-------------|
| `search_spotify` | Search for tracks, artists, albums, or playlists |
| `play_spotify` | Play a track or playlist by URI |
| `get_spotify_playback_state` | Read the active device, volume, shuffle, and repeat state |
| `list_spotify_devices` | List available Spotify Connect devices |
| `set_spotify_volume` | Set playback volume |
| `seek_spotify` | Seek within the current track |
| `set_spotify_shuffle` | Turn shuffle on or off |
| `set_spotify_repeat` | Set repeat mode |
| `transfer_spotify_playback` | Move playback to another Spotify device |
| `add_to_spotify_queue` | Queue a song by name or URI |
| `list_spotify_playlists` | List the current user's playlists |
| `get_spotify_playlist_items` | Read the items in a playlist |
| `create_spotify_playlist` | Create a playlist with optional initial songs |
| `add_spotify_playlist_items` | Add songs to a playlist |
| `remove_spotify_playlist_items` | Remove selected songs from a playlist |
| `update_spotify_playlist` | Rename a playlist or change its description or visibility |
| `pause_spotify` | Pause playback |
| `resume_spotify` | Resume playback |
| `skip_next_spotify` | Skip to the next track |
| `skip_previous_spotify` | Skip to the previous track |
| `currently_playing_spotify` | Get the currently playing track |
| `get_top_tracks_spotify` | Get your top tracks |

### Example Usage

Ask Petey:
- "Search Spotify for Bohemian Rhapsody"
- "Play some jazz"
- "What's playing right now?"
- "Skip to the next song"
- "What are my top tracks?"

## API Routes

All routes are under `/api/addons/spotify/`:

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/status` | Get add-on status |
| PUT | `/config` | Save Client ID |
| GET | `/auth-url` | Get Spotify authorization URL |
| POST | `/auth` | Exchange authorization code for tokens |
| POST | `/search` | Search Spotify |
| POST | `/play` | Start/resume playback |
| POST | `/pause` | Pause playback |
| POST | `/resume` | Resume playback |
| POST | `/next` | Skip to next track |
| POST | `/previous` | Skip to previous track |
| GET | `/currently-playing` | Get current playback state |
| POST | `/top-tracks` | Get user's top tracks |

## Security

- Credentials are stored in the add-on's writable `addon-data/spotify/` directory
- Token files have `0600` permissions
- The Client ID is never exposed to the chat model
- Authorization uses Spotify's state-protected Authorization Code with PKCE flow

## Requirements

- Spotify Premium account (required for playback control)
- A Spotify Connect device (desktop app, phone, speaker, etc.)
