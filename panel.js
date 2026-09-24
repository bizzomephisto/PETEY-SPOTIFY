(() => {
  const id = name => document.getElementById(`spotify-${name}`);
  const base = '/api/addons/spotify';
  const duckingClientId = window.crypto?.randomUUID?.()
    || `spotify-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  let duckingRequest = Promise.resolve();

  async function api(path, options = {}) {
    const response = await fetch(base + path, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Spotify request failed.');
    return payload;
  }

  function setStatus(message, type = '') {
    id('status').textContent = message;
    id('status').dataset.type = type;
  }

  function setSectionStatus(name, message, type = '') {
    const target = id(`${name}-status`);
    target.textContent = message;
    target.dataset.type = type;
  }

  function formatDuration(ms) {
    const min = Math.floor(ms / 60000);
    const sec = Math.floor((ms % 60000) / 1000);
    return `${min}:${sec.toString().padStart(2, '0')}`;
  }

  function renderTrack(track, showPlay = true) {
    const item = document.createElement('div');
    item.className = 'spotify-track';

    const info = document.createElement('div');
    info.className = 'spotify-track-info';

    const name = document.createElement('div');
    name.className = 'spotify-track-name';
    name.textContent = track.name || 'Unknown';

    const artist = document.createElement('div');
    artist.className = 'spotify-track-artist';
    artist.textContent = track.artist || '';

    const meta = document.createElement('div');
    meta.className = 'spotify-track-meta';
    if (track.album) meta.textContent = track.album;
    if (track.duration_ms) meta.textContent += (meta.textContent ? ' · ' : '') + formatDuration(track.duration_ms);

    info.append(name, artist, meta);
    item.append(info);

    if (showPlay && track.uri) {
      const playBtn = document.createElement('button');
      playBtn.className = 'spotify-play-btn';
      playBtn.type = 'button';
      playBtn.textContent = '▶';
      playBtn.title = 'Play';
      playBtn.addEventListener('click', async () => {
        try {
          await api('/play', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uri: track.uri, context_uri: track.album_uri || '' }),
          });
          refreshNowPlaying();
          setStatus(`Playing: ${track.name}`, 'success');
        } catch (error) {
          setStatus(error.message, 'error');
        }
      });
      item.append(playBtn);
    }

    return item;
  }

  async function refreshNowPlaying() {
    try {
      const data = await api('/currently-playing');
      const container = id('now-playing');
      container.textContent = '';
      if (!data.playing) {
        const p = document.createElement('p');
        p.className = 'hint';
        p.textContent = 'Nothing is currently playing.';
        container.append(p);
        return;
      }
      const track = document.createElement('div');
      track.className = 'spotify-now-track';

      const name = document.createElement('div');
      name.className = 'spotify-now-name';
      name.textContent = data.name || 'Unknown';

      const artist = document.createElement('div');
      artist.className = 'spotify-now-artist';
      artist.textContent = data.artist || '';

      const album = document.createElement('div');
      album.className = 'spotify-now-album';
      album.textContent = data.album || '';

      const progress = document.createElement('div');
      progress.className = 'spotify-now-progress';
      progress.textContent = `${formatDuration(data.progress_ms || 0)} / ${formatDuration(data.duration_ms || 0)}`;

      track.append(name, artist, album, progress);
      container.append(track);
    } catch (error) {
      const container = id('now-playing');
      container.textContent = '';
      const p = document.createElement('p');
      p.className = 'hint';
      p.textContent = error.message;
      container.append(p);
    }
  }

  async function refreshStatus() {
    try {
      const status = await api('/status');
      const clientId = id('client-id');
      if (clientId && status.client_id) clientId.value = status.client_id;
      id('dj-mode').checked = Boolean(status.dj_mode);
      id('dj-prompt').value = status.dj_prompt || '';
      id('duck-music').checked = Boolean(status.duck_music);
      id('duck-volume').value = Number(status.duck_volume ?? 18);
      id('fade-duration').value = Number(status.fade_duration_ms ?? 700);
      if (status.authorized) setStatus('Spotify connected.', 'success');
      return status;
    } catch (error) {
      setStatus(error.message, 'error');
    }
  }

  id('save-config').addEventListener('click', async () => {
    const clientId = id('client-id').value.trim();
    try {
      setStatus('Saving...');
      await api('/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: clientId }),
      });
      setStatus('Client ID saved.', 'success');
    } catch (error) {
      setStatus(error.message, 'error');
    }
  });

  id('authorize').addEventListener('click', async () => {
    try {
      const data = await api('/auth-url');
      window.open(data.url, '_blank', 'noopener,noreferrer');
      setStatus('Finish authorization in your browser…');
      const deadline = Date.now() + 10 * 60 * 1000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const status = await api('/status');
        if (status.authorized) {
          setStatus('Spotify connected.', 'success');
          refreshNowPlaying();
          break;
        }
      }
    } catch (error) {
      setStatus(error.message, 'error');
    }
  });

  id('save-announcements').addEventListener('click', async () => {
    try {
      setSectionStatus('dj', 'Saving…');
      const status = await api('/announcements', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: id('dj-mode').checked,
          prompt: id('dj-prompt').value,
        }),
      });
      id('dj-mode').checked = Boolean(status.dj_mode);
      id('dj-prompt').value = status.dj_prompt || '';
      setSectionStatus('dj', 'DJ Mode saved.', 'success');
    } catch (error) {
      setSectionStatus('dj', error.message, 'error');
    }
  });

  id('save-ducking').addEventListener('click', async () => {
    try {
      setSectionStatus('duck', 'Saving…');
      const status = await api('/audio-ducking', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          enabled: id('duck-music').checked,
          volume_percent: Number(id('duck-volume').value),
          fade_duration_ms: Number(id('fade-duration').value),
        }),
      });
      id('duck-music').checked = Boolean(status.duck_music);
      id('duck-volume').value = Number(status.duck_volume ?? 18);
      id('fade-duration').value = Number(status.fade_duration_ms ?? 700);
      setSectionStatus('duck', 'Music fading saved.', 'success');
    } catch (error) {
      setSectionStatus('duck', error.message, 'error');
    }
  });

  function reportSpeechState(active, keepalive = false) {
    duckingRequest = duckingRequest.catch(() => {}).then(() => fetch(`${base}/speech-duck`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({active: Boolean(active), client_id: duckingClientId}),
      keepalive,
    })).then(response => {
      if (!response.ok) throw new Error('Spotify music fading request failed.');
    });
  }

  window.addEventListener('petey:voice-playback', event => {
    reportSpeechState(Boolean(event.detail?.active));
  });
  window.addEventListener('pagehide', () => {
    fetch(`${base}/speech-duck`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({active: false, client_id: duckingClientId}),
      keepalive: true,
    }).catch(() => {});
  });

  id('search-btn').addEventListener('click', async () => {
    const query = id('search-input').value.trim();
    const type = id('search-type').value;
    if (!query) return;
    try {
      setStatus('Searching...');
      const data = await api('/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, type, limit: 10 }),
      });
      const container = id('search-results');
      container.textContent = '';
      if (!data.results || data.results.length === 0) {
        const p = document.createElement('p');
        p.className = 'hint';
        p.textContent = 'No results found.';
        container.append(p);
        return;
      }
      for (const track of data.results) {
        container.append(renderTrack(track));
      }
      setStatus(`Found ${data.results.length} result(s).`, 'success');
    } catch (error) {
      setStatus(error.message, 'error');
    }
  });

  id('search-input').addEventListener('keydown', event => {
    if (event.key === 'Enter') id('search-btn').click();
  });

  id('top-btn').addEventListener('click', async () => {
    const timeRange = id('top-range').value;
    try {
      setStatus('Loading top tracks...');
      const data = await api('/top-tracks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ time_range: timeRange, limit: 10 }),
      });
      const container = id('top-results');
      container.textContent = '';
      if (!data.tracks || data.tracks.length === 0) {
        const p = document.createElement('p');
        p.className = 'hint';
        p.textContent = 'No top tracks found for this time range.';
        container.append(p);
        return;
      }
      for (const track of data.tracks) {
        container.append(renderTrack(track));
      }
      setStatus(`Loaded ${data.tracks.length} top track(s).`, 'success');
    } catch (error) {
      setStatus(error.message, 'error');
    }
  });

  id('btn-pause').addEventListener('click', async () => {
    try {
      await api('/pause', { method: 'POST' });
      setStatus('Paused.', 'success');
      refreshNowPlaying();
    } catch (error) {
      setStatus(error.message, 'error');
    }
  });

  id('btn-resume').addEventListener('click', async () => {
    try {
      await api('/resume', { method: 'POST' });
      setStatus('Resumed.', 'success');
      refreshNowPlaying();
    } catch (error) {
      setStatus(error.message, 'error');
    }
  });

  id('btn-next').addEventListener('click', async () => {
    try {
      await api('/next', { method: 'POST' });
      setStatus('Skipped to next.', 'success');
      setTimeout(refreshNowPlaying, 1000);
    } catch (error) {
      setStatus(error.message, 'error');
    }
  });

  id('btn-previous').addEventListener('click', async () => {
    try {
      await api('/previous', { method: 'POST' });
      setStatus('Skipped to previous.', 'success');
      setTimeout(refreshNowPlaying, 1000);
    } catch (error) {
      setStatus(error.message, 'error');
    }
  });

  window.addEventListener('petey:view', event => {
    if (event.detail.view === 'addon-spotify') {
      refreshStatus();
      refreshNowPlaying();
    }
  });

  refreshStatus();
})();
