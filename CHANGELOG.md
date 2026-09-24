# Changelog

## v0.5.1 — 2026-09-24

- Keep consecutive streamed speech segments in one volume-ducking session so an
  ordinary chat reply restores the volume captured before PETEY began speaking.

## v0.5.0 — 2026-09-24

- Save and name three independent DJ personas and choose the active persona.
- Optionally blend the active DJ persona with the current PETEY system profile.
- Add an independent toggle for posting upcoming album artwork to chat.
- Preserve existing single-prompt configurations as the first DJ persona.

## v0.4.2 — 2026-09-23

- Keep DJ Mode and music-fading save confirmations inside their own settings cards.

## v0.4.1 — 2026-09-23

- Post the upcoming track's album artwork beneath the DJ reply in PETEY's chat feed.
- Link the artwork to the track on Spotify and label it with the upcoming title and artist.

## v0.4.0 — 2026-09-23

- Add volume, seek, shuffle, repeat, queue, device listing, and playback transfer tools.
- Add tools to list and inspect personal playlists, create playlists, add or remove
  selected songs, and edit playlist details.
- Add optional music fading tied to PETEY's actual browser speech lifecycle. The
  active device fades down only when speech begins and returns to its captured volume
  when speech ends.
- Request Spotify's private, collaborative, public, and private playlist scopes.
- Use Spotify's 2026 `/items` playlist endpoints and ten-result search limit.

## v0.3.5 — 2026-09-23

- Start DJ transitions about twenty seconds before the outgoing song ends.
- Read Spotify's queue and give PETEY both outgoing and upcoming track metadata.
- Mention the outgoing title and artist once, then focus the introduction and quick
  fact on the upcoming song.
- Retry briefly instead of guessing when Spotify has not exposed the next queue item.

## v0.3.4 — 2026-09-23

- Cue DJ Mode when roughly ten seconds remain in the outgoing song, giving PETEY
  time to begin its wrap-up before Spotify advances.
- Poll adaptively as the cue approaches and announce each track at most once.
- Migrate the original default new-song prompt to an outgoing-song radio prompt.

## v0.3.3 — 2026-09-23

- Search, play, pause, resume, and skip Spotify content through conversational tools.
- Continue through a track's album instead of stopping after one requested song.
- Add DJ Mode with an editable prompt for one announcement and quick fact per new track.
- Suppress the duplicate DJ event for a track PETEY just started directly.
- Respect PETEY's global speaker mute for DJ announcements.
- Store PKCE credentials and tokens only in the add-on's private data directory.\n
