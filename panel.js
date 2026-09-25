(() => {
  const id = name => document.getElementById(`spotify-${name}`);
  const base = '/api/addons/spotify';
  const duckingClientId = window.crypto?.randomUUID?.()
    || `spotify-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  let duckingRequest = Promise.resolve();
  let playback = {playing: false, has_track: false, progress_ms: 0, duration_ms: 0};
  let playbackSyncedAt = 0;
  let nowPlayingRequest = null;
  let railPlayer = null;

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

  const djPersonaFields = [1, 2, 3].map(slot => ({
    id: String(slot),
    name: id(`dj-name-${slot}`),
    prompt: id(`dj-prompt-${slot}`),
  }));

  function updateDjPersonaOptions(selected) {
    const select = id('dj-active-persona');
    const current = String(selected || select.value || '1');
    select.textContent = '';
    djPersonaFields.forEach((field, index) => {
      const option = document.createElement('option');
      option.value = field.id;
      option.textContent = field.name.value.trim() || `DJ Persona ${index + 1}`;
      select.append(option);
    });
    select.value = djPersonaFields.some(field => field.id === current) ? current : '1';
  }

  function renderDjSettings(status) {
    const personas = Array.isArray(status.dj_personas) ? status.dj_personas : [];
    djPersonaFields.forEach((field, index) => {
      const persona = personas[index] || {};
      field.name.value = persona.name || `DJ Persona ${index + 1}`;
      field.prompt.value = persona.prompt || (index === 0 ? status.dj_prompt || '' : '');
    });
    updateDjPersonaOptions(status.active_dj_persona || '1');
    id('dj-mode').checked = Boolean(status.dj_mode);
    id('dj-mix-profile').checked = Boolean(status.mix_dj_with_profile);
    id('dj-post-album-art').checked = status.post_album_art !== false;
    id('dj-wrapup-seconds').value = Number(status.dj_wrapup_seconds ?? 20);
    updateDjWrapupLabel();
  }

  function updateDjWrapupLabel() {
    const seconds = Number(id('dj-wrapup-seconds').value || 0);
    id('dj-wrapup-output').textContent = seconds === 0
      ? 'Right when it ends'
      : `${seconds} second${seconds === 1 ? '' : 's'} early`;
  }

  function formatDuration(ms) {
    const safe = Math.max(0, Number(ms) || 0);
    const min = Math.floor(safe / 60000);
    const sec = Math.floor((safe % 60000) / 1000);
    return `${min}:${sec.toString().padStart(2, '0')}`;
  }

  function currentProgress() {
    const elapsed = playback.playing && playbackSyncedAt ? Date.now() - playbackSyncedAt : 0;
    return Math.min(Number(playback.duration_ms) || Infinity, Math.max(0, Number(playback.progress_ms) + elapsed));
  }

  function updatePlayhead() {
    const progress = currentProgress();
    const duration = Number(playback.duration_ms) || 0;
    document.querySelectorAll('[data-spotify-progress]').forEach(input => {
      input.max = String(Math.max(1, duration));
      input.value = String(Math.min(duration || progress, progress));
      input.style.setProperty('--spotify-progress', `${duration ? (progress / duration) * 100 : 0}%`);
    });
    document.querySelectorAll('[data-spotify-time]').forEach(label => {
      label.textContent = `${formatDuration(progress)} / ${formatDuration(duration)}`;
    });
  }

  function albumArt(data, className) {
    const image = document.createElement('img');
    image.className = className;
    image.src = data.image_url;
    image.alt = data.album ? `${data.album} album artwork` : 'Album artwork';
    image.loading = 'lazy';
    image.referrerPolicy = 'no-referrer';
    return image;
  }

  function makePlayhead(className) {
    const wrap = document.createElement('div');
    wrap.className = className;
    const range = document.createElement('input');
    range.type = 'range';
    range.min = '0';
    range.step = '1000';
    range.dataset.spotifyProgress = '';
    range.setAttribute('aria-label', 'Song position');
    range.addEventListener('input', () => {
      playback.progress_ms = Number(range.value);
      playbackSyncedAt = Date.now();
      updatePlayhead();
    });
    range.addEventListener('change', async () => {
      try {
        await api('/seek', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({position_ms: Number(range.value)}),
        });
      } catch (error) {
        setStatus(error.message, 'error');
        refreshNowPlaying();
      }
    });
    const time = document.createElement('span');
    time.dataset.spotifyTime = '';
    wrap.append(range, time);
    return wrap;
  }

  function renderPagePlayer(message = '') {
    const container = id('now-playing');
    container.replaceChildren();
    if (!playback.has_track) {
      const p = document.createElement('p');
      p.className = 'hint';
      p.textContent = message || 'Nothing is currently playing.';
      container.append(p);
      return;
    }
    const layout = document.createElement('div');
    layout.className = 'spotify-now-layout';
    if (playback.image_url) layout.append(albumArt(playback, 'spotify-now-art'));
    const track = document.createElement('div');
    track.className = 'spotify-now-track';
    const name = document.createElement('div');
    name.className = 'spotify-now-name';
    name.textContent = playback.name || 'Unknown';
    const artist = document.createElement('div');
    artist.className = 'spotify-now-artist';
    artist.textContent = playback.artist || '';
    const album = document.createElement('div');
    album.className = 'spotify-now-album';
    album.textContent = playback.album || '';
    track.append(name, artist, album, makePlayhead('spotify-now-playhead'));
    layout.append(track);
    container.append(layout);
    updatePlayhead();
  }

  function renderRailPlayer(message = '') {
    if (!railPlayer) return;
    const {art, name, artist, play, empty, content} = railPlayer;
    const hasTrack = Boolean(playback.has_track);
    content.hidden = !hasTrack;
    empty.hidden = hasTrack;
    empty.textContent = message || 'Nothing is playing.';
    if (!hasTrack) return;
    art.hidden = !playback.image_url;
    if (playback.image_url && art.src !== playback.image_url) art.src = playback.image_url;
    art.alt = playback.album ? `${playback.album} album artwork` : 'Album artwork';
    name.textContent = playback.name || 'Unknown track';
    artist.textContent = playback.artist || 'Unknown artist';
    play.textContent = playback.playing ? '❚❚' : '▶';
    play.setAttribute('aria-label', playback.playing ? 'Pause' : 'Play');
    play.title = playback.playing ? 'Pause' : 'Play';
    updatePlayhead();
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
    if (nowPlayingRequest) return nowPlayingRequest;
    nowPlayingRequest = api('/currently-playing').then(data => {
      playback = {...playback, ...data};
      playbackSyncedAt = Date.now();
      renderPagePlayer();
      renderRailPlayer();
      return data;
    }).catch(error => {
      renderPagePlayer(error.message);
      renderRailPlayer(error.message);
    }).finally(() => { nowPlayingRequest = null; });
    return nowPlayingRequest;
  }

  async function runPlaybackControl(action) {
    try {
      await api(`/${action}`, {method: 'POST'});
      if (action === 'pause') playback.playing = false;
      if (action === 'resume') playback.playing = true;
      playbackSyncedAt = Date.now();
      renderRailPlayer();
      setStatus(action === 'next' ? 'Skipped to next.'
        : action === 'previous' ? 'Skipped to previous.'
          : action === 'pause' ? 'Paused.' : 'Resumed.', 'success');
      window.setTimeout(refreshNowPlaying, action === 'next' || action === 'previous' ? 900 : 150);
    } catch (error) {
      setStatus(error.message, 'error');
    }
  }

  function mountRailPlayer(container) {
    container.classList.add('spotify-command-player');
    const content = document.createElement('div');
    content.className = 'spotify-command-content';
    const art = document.createElement('img');
    art.className = 'spotify-command-art';
    art.loading = 'lazy';
    art.referrerPolicy = 'no-referrer';
    const copy = document.createElement('div');
    copy.className = 'spotify-command-copy';
    const name = document.createElement('strong');
    const artist = document.createElement('span');
    copy.append(name, artist);
    const playhead = makePlayhead('spotify-command-playhead');
    const controls = document.createElement('div');
    controls.className = 'spotify-command-controls';
    const previous = document.createElement('button');
    previous.type = 'button'; previous.textContent = '⏮'; previous.title = 'Previous';
    previous.setAttribute('aria-label', 'Previous song');
    const play = document.createElement('button');
    play.type = 'button'; play.setAttribute('aria-label', 'Play');
    const next = document.createElement('button');
    next.type = 'button'; next.textContent = '⏭'; next.title = 'Next';
    next.setAttribute('aria-label', 'Next song');
    previous.addEventListener('click', () => runPlaybackControl('previous'));
    play.addEventListener('click', () => runPlaybackControl(playback.playing ? 'pause' : 'resume'));
    next.addEventListener('click', () => runPlaybackControl('next'));
    controls.append(previous, play, next);
    content.append(art, copy, playhead, controls);
    const empty = document.createElement('p');
    empty.className = 'command-empty';
    container.append(content, empty);
    railPlayer = {container, content, art, name, artist, play, empty};
    renderRailPlayer();

    const panelCard = container.closest('.command-card');
    const active = () => document.visibilityState === 'visible'
      && document.documentElement.dataset.commandRail === 'open'
      && !panelCard?.classList.contains('collapsed');
    const poll = window.setInterval(() => { if (active()) refreshNowPlaying(); }, 15000);
    const tick = window.setInterval(() => { if (active()) updatePlayhead(); }, 1000);
    const observer = new MutationObserver(() => { if (active()) refreshNowPlaying(); });
    observer.observe(document.documentElement, {attributes: true, attributeFilter: ['data-command-rail']});
    if (panelCard) observer.observe(panelCard, {attributes: true, attributeFilter: ['class']});
    if (active()) refreshNowPlaying();
    return () => {
      window.clearInterval(poll);
      window.clearInterval(tick);
      observer.disconnect();
      railPlayer = null;
    };
  }

  async function refreshStatus() {
    try {
      const status = await api('/status');
      const clientId = id('client-id');
      if (clientId && status.client_id) clientId.value = status.client_id;
      renderDjSettings(status);
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
          personas: djPersonaFields.map(field => ({
            id: field.id,
            name: field.name.value,
            prompt: field.prompt.value,
          })),
          active_persona: id('dj-active-persona').value,
          mix_with_profile: id('dj-mix-profile').checked,
          post_album_art: id('dj-post-album-art').checked,
          wrapup_seconds: Number(id('dj-wrapup-seconds').value),
        }),
      });
      renderDjSettings(status);
      setSectionStatus('dj', 'DJ Mode saved.', 'success');
    } catch (error) {
      setSectionStatus('dj', error.message, 'error');
    }
  });

  djPersonaFields.forEach(field => {
    field.name.addEventListener('input', () => updateDjPersonaOptions());
  });
  id('dj-wrapup-seconds').addEventListener('input', updateDjWrapupLabel);

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
    runPlaybackControl('pause');
  });

  id('btn-resume').addEventListener('click', async () => {
    runPlaybackControl('resume');
  });

  id('btn-next').addEventListener('click', async () => {
    runPlaybackControl('next');
  });

  id('btn-previous').addEventListener('click', async () => {
    runPlaybackControl('previous');
  });

  window.addEventListener('petey:view', event => {
    if (event.detail.view === 'addon-spotify') {
      refreshStatus();
      refreshNowPlaying();
    }
  });

  if (typeof window.peteyInterface?.registerPanel === 'function') {
    window.peteyInterface.registerPanel({
      id: 'spotify-now-playing',
      title: 'Now playing',
      icon: '♫',
      render: mountRailPlayer,
    });
  }

  refreshStatus();
})();
