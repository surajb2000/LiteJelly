/* LiteJelly web client.
 *
 * Card markup is cloned from a <template> and filled with textContent, so a
 * filename can never be interpreted as HTML.
 */
(function () {
  'use strict';

  // Android TV browsers are often several years behind desktop Chrome.
  (function polyfillLegacy() {
    if (!Element.prototype.replaceChildren) {
      const replaceChildren = function () {
        while (this.firstChild) this.removeChild(this.firstChild);
        for (let i = 0; i < arguments.length; i++) {
          const node = arguments[i];
          this.appendChild(typeof node === 'string' ? document.createTextNode(node) : node);
        }
      };
      [Element, Document, DocumentFragment].forEach(ctor => {
        if (ctor && ctor.prototype && !ctor.prototype.replaceChildren) {
          ctor.prototype.replaceChildren = replaceChildren;
        }
      });
    }

    if (!String.prototype.padStart) {
      String.prototype.padStart = function (length, pad) {
        let text = String(this);
        pad = pad === undefined ? ' ' : String(pad);
        while (text.length < length && pad.length) text = pad + text;
        return text.slice(-Math.max(length, String(this).length));
      };
    }

    if (!Element.prototype.remove) {
      Element.prototype.remove = function () {
        if (this.parentNode) this.parentNode.removeChild(this);
      };
    }
  })();

  const API = {
    config: '/api/config',
    library: '/api/library',
    rescan: '/api/rescan',
    playback: '/api/playback',
    seekpoint: '/api/seekpoint',
    progress: '/api/progress',
    thumbnail: '/api/thumbnail',
    subtitle: '/api/subtitle'
  };

  const SEEK_SMALL = 10;
  const SEEK_LARGE = 60;
  const SPEEDS = [0.75, 1, 1.25, 1.5, 2];
  const PROGRESS_SAVE_INTERVAL = 10000;
  const SEEK_COMMIT_DELAY = 300;
  const OSD_TIMEOUT = 3500;

  const state = {
    view: 'LIBRARY',
    videos: [],
    filtered: [],
    progress: {},
    filter: 'all',
    sort: 'recent',
    query: '',
    columns: 4,
    ffmpegAvailable: false,
    playback: null,
    offset: 0,
    scrubbing: false,
    seekTimer: null,
    pendingSeek: null,
    osdTimer: null,
    lastSave: 0,
    lastSavedPosition: -1,
    speedIndex: 1,
    aspect: 'contain',
    wakeLock: null,
    subtitleTracks: [],
    activeSubtitle: 'off',
    quality: 'auto',
    qualities: [],
    audioOffset: 0,
    lastPointerMove: 0
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const el = {};
  let cardTemplate = null;
  let thumbObserver = null;

  // --- Utilities -------------------------------------------------------
  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '0:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
    return (h > 0 ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
  }

  function formatDate(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (isNaN(date.getTime())) return '';
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function debounce(fn, wait) {
    let timer;
    return function () {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(null, args), wait);
    };
  }

  async function getJSON(url) {
    const response = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!response.ok) {
      let message = 'Request failed (' + response.status + ')';
      try {
        const body = await response.json();
        if (body && body.error) message = body.error;
      } catch (err) { /* fall back to the generic message */ }
      throw new Error(message);
    }
    return response.json();
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
  }

  function showToast(message, duration) {
    const toast = el.toast;
    if (!toast) return;
    toast.textContent = message;
    toast.classList.remove('hidden');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => toast.classList.add('hidden'), duration || 2200);
  }

  // --- Fullscreen ------------------------------------------------------
  function fullscreenElement() {
    return document.fullscreenElement || document.webkitFullscreenElement ||
      document.mozFullScreenElement || document.msFullscreenElement || null;
  }

  function toggleFullscreen() {
    if (!fullscreenElement()) {
      const target = state.view === 'PLAYER' ? el.player : document.documentElement;
      const request = target.requestFullscreen || target.webkitRequestFullscreen ||
        target.mozRequestFullScreen || target.msRequestFullscreen;
      if (!request) {
        showToast('Fullscreen is not supported on this device', 3000);
        return;
      }
      const result = request.call(target);
      if (result && result.catch) result.catch(() => showToast('Fullscreen blocked'));
    } else {
      const exit = document.exitFullscreen || document.webkitExitFullscreen ||
        document.mozCancelFullScreen || document.msExitFullscreen;
      if (exit) {
        const result = exit.call(document);
        if (result && result.catch) result.catch(() => {});
      }
    }
  }

  function syncFullscreenIcons() {
    const isFull = !!fullscreenElement();
    $('.fs-icon-enter').classList.toggle('hidden', isFull);
    $('.fs-icon-exit').classList.toggle('hidden', !isFull);
  }

  // --- Screen wake lock ------------------------------------------------
  async function acquireWakeLock() {
    if (!('wakeLock' in navigator) || state.wakeLock) return;
    try {
      state.wakeLock = await navigator.wakeLock.request('screen');
      state.wakeLock.addEventListener('release', () => { state.wakeLock = null; });
    } catch (err) { /* denied or unsupported */ }
  }

  function releaseWakeLock() {
    if (state.wakeLock) {
      state.wakeLock.release().catch(() => {});
      state.wakeLock = null;
    }
  }

  // --- Library ---------------------------------------------------------
  async function loadConfig() {
    try {
      const data = await getJSON(API.config);
      if (data.server_name) {
        el.serverName.textContent = data.server_name;
        document.title = data.server_name;
      }
      state.ffmpegAvailable = !!data.ffmpeg_available;
    } catch (err) { /* the library request reports connection problems */ }
  }

  async function loadLibrary(options) {
    const silent = options && options.silent;
    if (!silent) el.loading.classList.remove('hidden');
    try {
      const data = await getJSON(API.library);
      state.videos = Array.isArray(data.videos) ? data.videos : [];
      state.progress = data.progress || {};
      state.ffmpegAvailable = !!data.ffmpeg_available;
      applyFilters();
      renderContinueWatching();
    } catch (err) {
      showToast('Could not load library: ' + err.message, 5000);
      el.emptyTitle.textContent = 'Library unavailable';
      el.emptyHint.textContent = err.message;
      el.emptyState.classList.remove('hidden');
    } finally {
      el.loading.classList.add('hidden');
    }
  }

  function compareByName(a, b) {
    // Episodes share a series title, so fall back to season/episode numbers.
    const byTitle = (a.name || '').localeCompare(b.name || '', undefined,
      { numeric: true, sensitivity: 'base' });
    if (byTitle !== 0) return byTitle;
    if ((a.season || 0) !== (b.season || 0)) return (a.season || 0) - (b.season || 0);
    if ((a.episode || 0) !== (b.episode || 0)) return (a.episode || 0) - (b.episode || 0);
    return (a.filename || '').localeCompare(b.filename || '', undefined, { numeric: true });
  }

  function sortVideos(list) {
    const sorted = list.slice();
    switch (state.sort) {
      case 'name':
        sorted.sort(compareByName);
        break;
      case 'size':
        sorted.sort((a, b) => b.size - a.size);
        break;
      case 'folder':
        sorted.sort((a, b) =>
          (a.folder || '').localeCompare(b.folder || '', undefined, { numeric: true }) ||
          compareByName(a, b));
        break;
      default:
        sorted.sort((a, b) => b.modified_ts - a.modified_ts);
    }
    return sorted;
  }

  function applyFilters() {
    let list = state.videos;

    if (state.filter !== 'all') {
      list = list.filter(video => {
        const ext = (video.extension || '').toLowerCase();
        return state.filter === 'other' ? ext !== 'mp4' && ext !== 'mkv' : ext === state.filter;
      });
    }

    if (state.query) {
      const needle = state.query;
      list = list.filter(video =>
        (video.name || '').toLowerCase().indexOf(needle) !== -1 ||
        (video.filename || '').toLowerCase().indexOf(needle) !== -1 ||
        (video.folder || '').toLowerCase().indexOf(needle) !== -1);
    }

    state.filtered = sortVideos(list);
    renderGrid();
  }

  function progressFraction(video) {
    const entry = state.progress[video.id];
    if (!entry || entry.finished || !entry.duration) return 0;
    return Math.min(1, Math.max(0, entry.position / entry.duration));
  }

  function buildCard(video) {
    const card = cardTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.id = video.id;
    card.setAttribute('aria-label', 'Play ' + video.name);
    card.title = video.filename;

    $('.card-title', card).textContent = video.name;
    $('.card-letter', card).textContent = (video.name || '?').charAt(0).toUpperCase();
    $('.card-size', card).textContent = video.size_human || '';
    $('.card-date', card).textContent = formatDate(video.modified);

    const badge = $('.card-badge', card);
    badge.textContent = (video.extension || '').toUpperCase();
    badge.classList.add('ext-' + (video.extension || 'unknown').toLowerCase());

    const fraction = progressFraction(video);
    if (fraction > 0.01) {
      const wrap = $('.card-progress', card);
      wrap.classList.remove('hidden');
      $('.card-progress-fill', wrap).style.width = (fraction * 100).toFixed(1) + '%';
    }

    const img = $('.thumb-img', card);
    img.dataset.src = API.thumbnail + '?id=' + encodeURIComponent(video.id);
    if (thumbObserver) thumbObserver.observe(img);
    else loadThumbnail(img);

    return card;
  }

  function loadThumbnail(img) {
    if (!img.dataset.src || img.getAttribute('src')) return;
    img.addEventListener('load', () => {
      img.classList.add('loaded');
      const letter = $('.card-letter', img.parentElement);
      if (letter) letter.classList.add('hidden');
    }, { once: true });
    img.addEventListener('error', () => img.removeAttribute('src'), { once: true });
    img.src = img.dataset.src;
  }

  function setupThumbObserver() {
    if (!('IntersectionObserver' in window)) return;
    thumbObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        loadThumbnail(entry.target);
      });
    }, { rootMargin: '300px 0px' });
  }

  function renderGrid() {
    const count = state.filtered.length;
    el.mediaCount.textContent = count + (count === 1 ? ' video' : ' videos');

    if (!count) {
      el.grid.replaceChildren();
      el.emptyTitle.textContent = state.videos.length ? 'No matches' : 'No videos found';
      el.emptyHint.textContent = state.videos.length
        ? 'Try a different search or filter.'
        : 'Add folders to config.json, then rescan.';
      el.emptyState.classList.remove('hidden');
      return;
    }

    el.emptyState.classList.add('hidden');
    const fragment = document.createDocumentFragment();
    state.filtered.forEach(video => fragment.appendChild(buildCard(video)));
    el.grid.replaceChildren(fragment);
    requestAnimationFrame(measureColumns);
  }

  function renderContinueWatching() {
    const entries = Object.keys(state.progress)
      .map(key => state.progress[key])
      .filter(entry => entry && !entry.finished && entry.position > 15 && entry.duration > 0)
      .sort((a, b) => b.updated_at - a.updated_at)
      .slice(0, 12);

    const byId = new Map(state.videos.map(video => [video.id, video]));
    const items = entries
      .map(entry => ({ video: byId.get(entry.video_id), entry: entry }))
      .filter(item => item.video);

    if (!items.length) {
      el.continueSection.classList.add('hidden');
      el.continueRow.replaceChildren();
      return;
    }

    const fragment = document.createDocumentFragment();
    items.forEach(item => {
      const card = buildCard(item.video);
      card.classList.add('rail-card');
      const meta = $('.card-meta', card);
      const remaining = document.createElement('span');
      remaining.textContent = formatTime(Math.max(0, item.entry.duration - item.entry.position)) + ' left';
      meta.replaceChildren(remaining);
      fragment.appendChild(card);
    });
    el.continueRow.replaceChildren(fragment);
    el.continueSection.classList.remove('hidden');
  }

  function measureColumns() {
    const cards = $$('.card', el.grid);
    if (cards.length < 2) {
      state.columns = 1;
      return;
    }
    const firstTop = cards[0].offsetTop;
    let columns = 1;
    for (let i = 1; i < cards.length; i++) {
      if (cards[i].offsetTop === firstTop) columns++;
      else break;
    }
    state.columns = columns;
  }

  // --- Playback --------------------------------------------------------
  function displayTime() {
    if (!state.playback) return 0;
    const current = el.video.currentTime || 0;
    return state.playback.native_seek ? current : state.offset + current;
  }

  function displayDuration() {
    if (!state.playback) return 0;
    if (state.playback.duration > 0) return state.playback.duration;
    return isFinite(el.video.duration) ? el.video.duration : 0;
  }

  async function openVideo(videoId) {
    const video = state.videos.find(item => item.id === videoId);
    if (!video) return;

    el.osdTitle.textContent = video.name;
    el.osdBadge.textContent = 'Loading...';
    showPlayerView();
    el.buffering.classList.remove('hidden');
    showOSD(true);

    try {
      const plan = await getJSON(API.playback + '?id=' + encodeURIComponent(videoId) +
        '&quality=' + encodeURIComponent(state.quality) +
        '&adelay=' + encodeURIComponent(state.audioOffset));
      startPlayback(plan);
    } catch (err) {
      el.buffering.classList.add('hidden');
      showToast('Cannot play this file: ' + err.message, 5000);
      exitPlayer();
    }
  }

  function codecLabel(name) {
    const aliases = { libx264: 'h264', libx265: 'hevc', 'libvpx-vp9': 'vp9', libopus: 'opus' };
    return aliases[name] || name || 'unknown';
  }

  // "Remuxed" only changes the container, which is easy to mistake for a
  // re-encode, so spell out what happens to each stream.
  function describePipeline(plan) {
    const lines = [];
    const sourceVideo = codecLabel(plan.video_codec);
    if (plan.video_action === 'copy') {
      lines.push('Video: ' + sourceVideo + ' copied untouched' +
        (plan.height ? ' (' + plan.width + '\u00d7' + plan.height + ')' : ''));
    } else {
      lines.push('Video: ' + sourceVideo + ' re-encoded to ' +
        codecLabel(plan.target_video_codec) +
        (plan.output_height ? ' ' + plan.output_width + '\u00d7' + plan.output_height : ''));
    }

    if (!plan.audio_codec) {
      lines.push('Audio: none');
    } else if (plan.audio_action === 'copy') {
      lines.push('Audio: ' + codecLabel(plan.audio_codec) + ' copied untouched');
    } else {
      lines.push('Audio: ' + codecLabel(plan.audio_codec) + ' converted to ' +
        codecLabel(plan.target_audio_codec));
    }
    return lines.join('\n');
  }

  function describeQuality(plan) {
    const height = plan.output_height || plan.height;
    if (!height) return '';
    const scaled = plan.output_height && plan.height && plan.output_height < plan.height;
    return height + 'p' + (scaled ? ' (from ' + plan.height + 'p)' : '');
  }

  function startPlayback(plan, startAt) {
    state.playback = plan;
    state.pendingSeek = null;
    state.offset = 0;
    state.activeSubtitle = 'off';
    state.lastSavedPosition = -1;
    state.qualities = Array.isArray(plan.qualities) ? plan.qualities : [];
    if (plan.quality) state.quality = plan.quality;

    el.osdTitle.textContent = plan.title || '';
    const detail = describeQuality(plan);
    el.osdBadge.textContent = detail ? plan.badge + ' \u00b7 ' + detail : plan.badge || '';
    el.osdBadge.title = describePipeline(plan);
    updateQualityLabel();
    renderQualityMenu();

    // A restart passes an explicit time; only a fresh play picks a default.
    buildSubtitleMenu(plan, typeof startAt !== 'number');

    const resume = plan.resume && plan.resume.position > 15 ? plan.resume.position : 0;
    const startTime = typeof startAt === 'number' ? startAt : resume;
    loadSource(startTime, true);

    if (startTime > 0 && typeof startAt !== 'number') {
      showToast('Resuming at ' + formatTime(startTime), 3000);
    }

    updateMediaSession(plan);
    updateOSD();
    showOSD();
  }

  async function loadSource(time, initial) {
    const plan = state.playback;
    if (!plan) return;
    const video = el.video;
    const target = Math.max(0, time || 0);

    if (plan.native_seek) {
      state.offset = 0;
      if (!initial) {
        video.currentTime = target;
        return;
      }
      video.src = plan.url;
      if (target > 0) {
        video.addEventListener('loadedmetadata', () => { video.currentTime = target; }, { once: true });
      }
    } else {
      // Send the requested time as-is: snapping it onto a keyframe makes
      // ffmpeg rewind a whole GOP. Ask only where it will actually land, so
      // the clock and subtitles match the stream.
      let actual = target;
      if (target > 0 && plan.exact_seek === false) {
        try {
          const point = await getJSON(API.seekpoint + '?id=' + encodeURIComponent(plan.id) +
            '&t=' + target.toFixed(2) + '&quality=' + encodeURIComponent(state.quality));
          if (typeof point.start === 'number') actual = point.start;
        } catch (err) { /* fall back to the requested time */ }
      }
      if (state.playback !== plan) return;
      state.offset = actual;
      const separator = plan.url.indexOf('?') === -1 ? '?' : '&';
      video.src = plan.url + separator + 'ss=' + target.toFixed(2);
    }

    video.load();
    const started = video.play();
    if (started && started.catch) started.catch(() => { /* autoplay may need a gesture */ });

    // Cues are absolute, but a restarted pipe starts at the offset, so re-base them.
    if (!plan.native_seek) attachSubtitleTracks(state.offset);
  }

  function seekTo(seconds) {
    const plan = state.playback;
    if (!plan) return;
    const duration = displayDuration();
    const target = Math.max(0, duration ? Math.min(seconds, duration - 1) : seconds);

    if (plan.native_seek) {
      el.video.currentTime = target;
      updateOSD();
      return;
    }

    state.pendingSeek = target;
    renderProgress(target, duration, true);
    clearTimeout(state.seekTimer);
    state.seekTimer = setTimeout(() => {
      const value = state.pendingSeek;
      state.pendingSeek = null;
      if (value !== null) {
        el.buffering.classList.remove('hidden');
        loadSource(value, false);
      }
    }, SEEK_COMMIT_DELAY);
  }

  function seekBy(delta) {
    const base = state.pendingSeek !== null ? state.pendingSeek : displayTime();
    seekTo(base + delta);
    showToast((delta > 0 ? '+' : '') + delta + 's', 900);
  }

  function togglePlayPause() {
    if (el.video.paused) {
      const started = el.video.play();
      if (started && started.catch) started.catch(() => {});
    } else {
      el.video.pause();
    }
  }

  function showPlayerView() {
    state.view = 'PLAYER';
    el.library.classList.add('hidden');
    el.player.classList.remove('hidden');
    document.body.classList.add('playing');
    if (!history.state || history.state.view !== 'PLAYER') {
      history.pushState({ view: 'PLAYER' }, '');
    }
  }

  function exitPlayer(skipHistory) {
    saveProgress(true);
    clearTimeout(state.seekTimer);
    state.pendingSeek = null;

    const video = el.video;
    video.pause();
    video.removeAttribute('src');
    video.load();
    clearSubtitleTracks();
    state.subtitleTracks = [];
    state.activeSubtitle = 'off';

    state.playback = null;
    state.offset = 0;
    state.view = 'LIBRARY';
    el.player.classList.add('hidden');
    el.player.classList.remove('idle');
    el.library.classList.remove('hidden');
    el.buffering.classList.add('hidden');
    document.body.classList.remove('playing');
    hideOSD();
    closeMenus();
    releaseWakeLock();
    renderGrid();
    renderContinueWatching();

    if (!skipHistory && history.state && history.state.view === 'PLAYER') {
      history.back();
    }
  }

  // --- Subtitles -------------------------------------------------------
  function clearSubtitleTracks() {
    $$('track', el.video).forEach(track => track.remove());
    el.subtitleLayer.replaceChildren();
  }

  function attachSubtitleTracks(offset) {
    const plan = state.playback;
    if (!plan) return;
    clearSubtitleTracks();

    state.subtitleTracks.forEach(track => {
      if (track.burn_in_only) return;
      const element = document.createElement('track');
      element.kind = 'subtitles';
      element.label = track.label;
      if (track.language) element.srclang = track.language;
      element.src = API.subtitle + '?id=' + encodeURIComponent(plan.id) +
        '&track=' + encodeURIComponent(track.id) +
        '&offset=' + (offset || 0).toFixed(2);
      element.dataset.trackId = track.id;
      element.addEventListener('load', applyActiveSubtitle);
      el.video.appendChild(element);
    });

    applyActiveSubtitle();
  }

  function applyActiveSubtitle() {
    const elements = $$('track', el.video);
    const textTracks = el.video.textTracks;
    for (let i = 0; i < textTracks.length; i++) {
      const track = textTracks[i];
      const id = elements[i] ? elements[i].dataset.trackId : null;
      // 'hidden' keeps cuechange firing while suppressing the browser's own
      // rendering, which cannot be positioned above the control dock.
      track.mode = id === state.activeSubtitle ? 'hidden' : 'disabled';
      if (!track.cueListenerAttached) {
        track.cueListenerAttached = true;
        track.addEventListener('cuechange', () => renderCues(track));
      }
    }
    const active = Array.from(textTracks).find(track => track.mode === 'hidden');
    renderCues(active || null);
  }

  function renderCues(track) {
    if (!track || track.mode !== 'hidden' || !track.activeCues) {
      el.subtitleLayer.replaceChildren();
      return;
    }
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < track.activeCues.length; i++) {
      const cue = track.activeCues[i];
      const line = document.createElement('div');
      line.className = 'subtitle-cue';
      if (typeof cue.getCueAsHTML === 'function') {
        line.appendChild(cue.getCueAsHTML());
      } else {
        line.textContent = cue.text;
      }
      fragment.appendChild(line);
    }
    el.subtitleLayer.replaceChildren(fragment);
  }

  // Remembers a language, or 'off' if subtitles were explicitly turned off.
  function preferredSubtitle(tracks) {
    let saved = 'en';
    try {
      const stored = localStorage.getItem('litejelly_subtitle');
      if (stored) saved = stored;
    } catch (err) { /* storage unavailable */ }
    if (saved === 'off') return 'off';

    const usable = tracks.filter(track => !track.burn_in_only);
    if (!usable.length) return 'off';

    const sameLanguage = usable.filter(track =>
      (track.language || '').toLowerCase() === saved.toLowerCase());
    const notForced = list => list.filter(track => !track.forced);

    const choice = notForced(sameLanguage)[0] || sameLanguage[0] ||
      notForced(usable)[0] || usable[0];
    return choice ? choice.id : 'off';
  }

  function buildSubtitleMenu(plan, autoSelect) {
    clearSubtitleTracks();
    state.subtitleTracks = Array.isArray(plan.subtitles) ? plan.subtitles : [];
    const count = state.subtitleTracks.length;

    if (autoSelect) {
      state.activeSubtitle = preferredSubtitle(state.subtitleTracks);
    }

    attachSubtitleTracks(state.offset);

    el.btnSubtitles.classList.toggle('unavailable', count === 0);
    el.btnSubtitles.setAttribute('aria-label',
      count ? 'Subtitles, ' + count + ' available' : 'No subtitles available');

    renderSubtitleMenu();
  }

  function renderSubtitleMenu() {
    const menu = el.subtitleMenu;
    menu.replaceChildren();

    const heading = document.createElement('div');
    heading.className = 'popup-heading';
    heading.textContent = 'Subtitles';
    menu.appendChild(heading);

    const makeItem = (id, label, detail) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'popup-item';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('aria-checked', String(state.activeSubtitle === id));
      item.dataset.trackId = id;

      const text = document.createElement('span');
      text.className = 'popup-item-label';
      text.textContent = label;
      item.appendChild(text);

      if (detail) {
        const hint = document.createElement('span');
        hint.className = 'popup-item-hint';
        hint.textContent = detail;
        item.appendChild(hint);
      }
      menu.appendChild(item);
    };

    makeItem('off', 'Off', '');

    if (!state.subtitleTracks.length) {
      const empty = document.createElement('div');
      empty.className = 'popup-empty';
      empty.textContent = 'No subtitle tracks found for this file.';
      menu.appendChild(empty);
      return;
    }

    state.subtitleTracks.forEach(track => {
      const detail = track.burn_in_only
        ? 'Image, needs re-encode'
        : (track.kind === 'external' ? 'File' : 'Embedded');
      makeItem(track.id, track.label, detail);
    });
  }

  async function selectSubtitle(trackId) {
    const plan = state.playback;
    if (!plan) return;

    const track = state.subtitleTracks.find(item => item.id === trackId);

    if (track && track.burn_in_only) {
      // Bitmap subtitles have to be composited by ffmpeg, so restart the stream.
      const at = displayTime();
      showToast('Re-encoding with ' + track.label + '...', 3000);
      closeSubtitleMenu();
      try {
        const next = await getJSON(API.playback + '?id=' + encodeURIComponent(plan.id) +
          '&sub=' + encodeURIComponent(trackId));
        startPlayback(next, at);
        state.activeSubtitle = trackId;
        renderSubtitleMenu();
      } catch (err) {
        showToast('Could not enable that track: ' + err.message, 4000);
      }
      return;
    }

    state.activeSubtitle = trackId;
    try {
      localStorage.setItem('litejelly_subtitle',
        trackId === 'off' ? 'off' : ((track && track.language) || 'on'));
    } catch (err) { /* storage unavailable */ }
    applyActiveSubtitle();
    renderSubtitleMenu();
    closeSubtitleMenu();
    showToast(trackId === 'off' ? 'Subtitles off' : 'Subtitles: ' + (track ? track.label : ''), 1800);
  }
  function toggleSubtitleMenu() {
    if (el.subtitleMenu.classList.contains('hidden')) {
      closeQualityMenu();
      renderSubtitleMenu();
      el.subtitleMenu.classList.remove('hidden');
      el.btnSubtitles.setAttribute('aria-expanded', 'true');
      const first = $('.popup-item', el.subtitleMenu);
      if (first) first.focus();
      showOSD(true);
    } else {
      closeSubtitleMenu();
    }
  }

  function closeSubtitleMenu() {
    el.subtitleMenu.classList.add('hidden');
    el.btnSubtitles.setAttribute('aria-expanded', 'false');
  }

  // --- Quality ---------------------------------------------------------
  function updateQualityLabel() {
    const level = state.qualities.find(item => item.id === state.quality);
    el.qualityLabel.textContent = level ? level.label : 'Auto';
  }

  function renderQualityMenu() {
    const menu = el.qualityMenu;
    menu.replaceChildren();

    const heading = document.createElement('div');
    heading.className = 'popup-heading';
    heading.textContent = 'Quality';
    menu.appendChild(heading);

    const plan = state.playback;
    state.qualities.forEach(level => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'popup-item';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('aria-checked', String(state.quality === level.id));
      item.dataset.qualityId = level.id;

      const text = document.createElement('span');
      text.className = 'popup-item-label';
      text.textContent = level.label;
      item.appendChild(text);

      let hint = '';
      if (level.id === 'auto') hint = 'Server default';
      else if (level.id === 'original') {
        hint = plan && plan.height ? plan.height + 'p, no re-encode' : 'No re-encode';
      } else if (plan && plan.height && level.height >= plan.height) {
        hint = 'Re-encode, same size';
      } else {
        hint = 'Re-encode, smaller';
      }
      if (hint) {
        const note = document.createElement('span');
        note.className = 'popup-item-hint';
        note.textContent = hint;
        item.appendChild(note);
      }
      menu.appendChild(item);
    });

    if (plan) {
      const note = document.createElement('div');
      note.className = 'popup-note';
      describePipeline(plan).split('\n').forEach(line => {
        const row = document.createElement('div');
        row.textContent = line;
        note.appendChild(row);
      });
      menu.appendChild(note);
    }
  }

  async function selectQuality(qualityId) {
    const plan = state.playback;
    if (!plan || qualityId === state.quality) {
      closeQualityMenu();
      return;
    }

    const at = displayTime();
    const previousSubtitle = state.activeSubtitle;
    state.quality = qualityId;
    try {
      localStorage.setItem('litejelly_quality', qualityId);
    } catch (err) { /* storage unavailable */ }
    closeQualityMenu();
    el.buffering.classList.remove('hidden');

    try {
      const next = await getJSON(API.playback + '?id=' + encodeURIComponent(plan.id) +
        '&quality=' + encodeURIComponent(qualityId) +
        '&adelay=' + encodeURIComponent(state.audioOffset));
      startPlayback(next, at);
      if (previousSubtitle !== 'off') selectSubtitle(previousSubtitle);
      showToast('Quality: ' + (describeQuality(next) || qualityId), 2500);
    } catch (err) {
      el.buffering.classList.add('hidden');
      showToast('Could not switch quality: ' + err.message, 4000);
    }
  }

  function toggleQualityMenu() {
    if (el.qualityMenu.classList.contains('hidden')) {
      closeSubtitleMenu();
      renderQualityMenu();
      el.qualityMenu.classList.remove('hidden');
      el.btnQuality.setAttribute('aria-expanded', 'true');
      const first = $('.popup-item', el.qualityMenu);
      if (first) first.focus();
      showOSD(true);
    } else {
      closeQualityMenu();
    }
  }

  function closeQualityMenu() {
    el.qualityMenu.classList.add('hidden');
    el.btnQuality.setAttribute('aria-expanded', 'false');
  }

  function menusOpen() {
    return !el.subtitleMenu.classList.contains('hidden') ||
      !el.qualityMenu.classList.contains('hidden') ||
      !el.audioSyncMenu.classList.contains('hidden');
  }

  function closeMenus() {
    closeSubtitleMenu();
    closeQualityMenu();
    closeAudioSyncMenu();
  }

  // --- Audio sync ------------------------------------------------------
  const SYNC_STEPS = [-400, -300, -200, -150, -100, -50, 0, 50, 100, 150, 200, 300, 400];

  function updateSyncLabel() {
    const value = state.audioOffset;
    el.syncLabel.textContent = value === 0 ? '0' : (value > 0 ? '+' : '') + value;
    el.btnAudioSync.classList.toggle('adjusted', value !== 0);
  }

  function renderAudioSyncMenu() {
    const menu = el.audioSyncMenu;
    menu.replaceChildren();

    const heading = document.createElement('div');
    heading.className = 'popup-heading';
    heading.textContent = 'Audio sync';
    menu.appendChild(heading);

    SYNC_STEPS.forEach(value => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'popup-item';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('aria-checked', String(state.audioOffset === value));
      item.dataset.syncValue = String(value);

      const text = document.createElement('span');
      text.className = 'popup-item-label';
      text.textContent = value === 0 ? 'In sync (0 ms)'
        : (value > 0 ? '+' : '') + value + ' ms';
      item.appendChild(text);

      if (value !== 0) {
        const hint = document.createElement('span');
        hint.className = 'popup-item-hint';
        hint.textContent = value > 0 ? 'audio later' : 'audio earlier';
        item.appendChild(hint);
      }
      menu.appendChild(item);
    });

    const note = document.createElement('div');
    note.className = 'popup-note';
    const plan = state.playback;
    const auto = plan && plan.reorder_delay_ms ? plan.reorder_delay_ms : 0;
    const rows = ['Use this if speech does not match the lips.'];
    if (auto) rows.push('Automatic B-frame correction: ' + auto + ' ms');
    rows.forEach(line => {
      const row = document.createElement('div');
      row.textContent = line;
      note.appendChild(row);
    });
    menu.appendChild(note);
  }

  async function selectAudioOffset(value) {
    const plan = state.playback;
    closeAudioSyncMenu();
    if (!plan || value === state.audioOffset) return;

    const at = displayTime();
    const previousSubtitle = state.activeSubtitle;
    state.audioOffset = value;
    try {
      localStorage.setItem('litejelly_audio_offset', String(value));
    } catch (err) { /* storage unavailable */ }
    updateSyncLabel();
    el.buffering.classList.remove('hidden');

    try {
      const next = await getJSON(API.playback + '?id=' + encodeURIComponent(plan.id) +
        '&quality=' + encodeURIComponent(state.quality) +
        '&adelay=' + encodeURIComponent(value));
      startPlayback(next, at);
      if (previousSubtitle !== 'off') selectSubtitle(previousSubtitle);
      showToast('Audio sync ' + (value > 0 ? '+' : '') + value + ' ms', 2500);
    } catch (err) {
      el.buffering.classList.add('hidden');
      showToast('Could not adjust audio sync: ' + err.message, 4000);
    }
  }

  function toggleAudioSyncMenu() {
    if (el.audioSyncMenu.classList.contains('hidden')) {
      closeSubtitleMenu();
      closeQualityMenu();
      renderAudioSyncMenu();
      el.audioSyncMenu.classList.remove('hidden');
      el.btnAudioSync.setAttribute('aria-expanded', 'true');
      const current = $('.popup-item[aria-checked="true"]', el.audioSyncMenu);
      (current || $('.popup-item', el.audioSyncMenu)).focus();
      showOSD(true);
    } else {
      closeAudioSyncMenu();
    }
  }

  function closeAudioSyncMenu() {
    el.audioSyncMenu.classList.add('hidden');
    el.btnAudioSync.setAttribute('aria-expanded', 'false');
  }

  // --- Progress persistence --------------------------------------------
  function saveProgress(force) {
    const plan = state.playback;
    if (!plan) return;
    const position = displayTime();
    const duration = displayDuration();
    if (!duration || position < 5) return;

    const now = Date.now();
    if (!force && now - state.lastSave < PROGRESS_SAVE_INTERVAL) return;
    if (Math.abs(position - state.lastSavedPosition) < 1) return;
    state.lastSave = now;
    state.lastSavedPosition = position;

    const finished = position >= duration * 0.96;
    const payload = { id: plan.id, position: position, duration: duration };

    state.progress[plan.id] = {
      video_id: plan.id,
      position: finished ? 0 : position,
      duration: duration,
      finished: finished,
      updated_at: now / 1000
    };

    if (force && navigator.sendBeacon) {
      const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
      navigator.sendBeacon(API.progress, blob);
    } else {
      postJSON(API.progress, payload).catch(() => {});
    }
  }

  // --- OSD -------------------------------------------------------------
  function showOSD(sticky) {
    el.osd.classList.remove('hidden');
    el.player.classList.remove('idle');
    el.subtitleLayer.classList.add('raised');
    clearTimeout(state.osdTimer);
    if (sticky) return;
    state.osdTimer = setTimeout(() => {
      if (!el.video.paused && !menusOpen() && !state.scrubbing) {
        hideOSD();
      }
    }, OSD_TIMEOUT);
  }

  function hideOSD() {
    el.osd.classList.add('hidden');
    el.subtitleLayer.classList.remove('raised');
    // Hiding the chrome should hide the pointer with it.
    if (state.view === 'PLAYER') el.player.classList.add('idle');
  }

  function renderProgress(current, duration, pending) {
    const fraction = duration > 0 ? Math.min(1, current / duration) : 0;
    el.progressFill.style.width = (fraction * 100).toFixed(2) + '%';
    el.currentTime.textContent = formatTime(current);
    el.totalTime.textContent = formatTime(duration);
    el.progressWrap.classList.toggle('pending', !!pending);
    if (!state.scrubbing) {
      el.seekRange.value = String(Math.round(fraction * 1000));
    }
    el.seekRange.setAttribute('aria-valuetext', formatTime(current) + ' of ' + formatTime(duration));
  }

  function updateOSD() {
    if (!state.playback) return;
    const duration = displayDuration();
    const current = state.pendingSeek !== null ? state.pendingSeek : displayTime();
    renderProgress(current, duration, state.pendingSeek !== null);

    const buffered = el.video.buffered;
    if (buffered && buffered.length && duration > 0) {
      const end = state.offset + buffered.end(buffered.length - 1);
      el.progressBuffered.style.width = Math.min(100, (end / duration) * 100).toFixed(2) + '%';
    } else {
      el.progressBuffered.style.width = '0%';
    }
  }

  function updateMediaSession(plan) {
    if (!('mediaSession' in navigator) || !window.MediaMetadata) return;
    navigator.mediaSession.metadata = new window.MediaMetadata({
      title: plan.title || '',
      artist: plan.badge || '',
      artwork: [{
        src: API.thumbnail + '?id=' + encodeURIComponent(plan.id),
        sizes: '480x270',
        type: 'image/jpeg'
      }]
    });
    const actions = {
      play: () => el.video.play(),
      pause: () => el.video.pause(),
      seekbackward: () => seekBy(-SEEK_SMALL),
      seekforward: () => seekBy(SEEK_SMALL)
    };
    Object.keys(actions).forEach(action => {
      try {
        navigator.mediaSession.setActionHandler(action, actions[action]);
      } catch (err) { /* action unsupported */ }
    });
  }

  // --- Volume, speed, aspect -------------------------------------------
  function applyVolume(value) {
    el.video.volume = Math.min(1, Math.max(0, value));
    el.video.muted = el.video.volume === 0;
    el.volumeRange.value = String(Math.round(el.video.volume * 100));
    syncMuteIcon();
    try {
      localStorage.setItem('litejelly_volume', String(el.video.volume));
    } catch (err) { /* storage unavailable */ }
  }

  function syncMuteIcon() {
    const muted = el.video.muted || el.video.volume === 0;
    $('#icon-volume').classList.toggle('hidden', muted);
    $('#icon-muted').classList.toggle('hidden', !muted);
    el.btnMute.setAttribute('aria-label', muted ? 'Unmute' : 'Mute');
  }

  function cycleSpeed() {
    state.speedIndex = (state.speedIndex + 1) % SPEEDS.length;
    const speed = SPEEDS[state.speedIndex];
    el.video.playbackRate = speed;
    $('span', el.btnSpeed).textContent = speed + 'x';
    showToast('Speed ' + speed + 'x', 1500);
  }

  function toggleAspect() {
    state.aspect = state.aspect === 'contain' ? 'cover' : 'contain';
    el.video.style.objectFit = state.aspect;
    $('span', el.btnAspect).textContent = state.aspect === 'contain' ? 'FIT' : 'FILL';
    showToast(state.aspect === 'contain' ? 'Fit screen' : 'Zoom to fill', 1500);
  }

  // --- Keyboard / D-pad -------------------------------------------------
  function isTypingTarget(target) {
    return !!target && (target.tagName === 'INPUT' ||
      target.tagName === 'SELECT' || target.tagName === 'TEXTAREA');
  }

  function handleKeyDown(event) {
    if (state.view === 'PLAYER') handlePlayerKeys(event);
    else handleLibraryKeys(event);
  }

  function handleLibraryKeys(event) {
    const active = document.activeElement;

    if (event.key === '/' && !isTypingTarget(active)) {
      event.preventDefault();
      el.searchInput.focus();
      el.searchInput.select();
      return;
    }

    const arrows = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];
    if (arrows.indexOf(event.key) === -1) return;
    if (isTypingTarget(active) && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) return;

    const cards = $$('.card', el.grid);
    if (!cards.length) return;

    const inGrid = !!active && active.classList.contains('card') && el.grid.contains(active);
    if (!inGrid) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        cards[0].focus();
      }
      return;
    }

    const index = cards.indexOf(active);
    let next = index;
    switch (event.key) {
      case 'ArrowRight':
        next = Math.min(cards.length - 1, index + 1);
        break;
      case 'ArrowLeft':
        next = Math.max(0, index - 1);
        break;
      case 'ArrowDown':
        next = Math.min(cards.length - 1, index + state.columns);
        break;
      case 'ArrowUp':
        if (index < state.columns) {
          event.preventDefault();
          el.searchInput.focus();
          return;
        }
        next = index - state.columns;
        break;
    }

    event.preventDefault();
    if (next !== index) {
      cards[next].focus();
      cards[next].scrollIntoView({ block: 'nearest' });
    }
  }

  function handlePlayerKeys(event) {
    if (isTypingTarget(event.target) && event.target !== el.seekRange) return;

    // 10009 (Tizen) and 461 (webOS) are the remote's Back button.
    const isBackKey = event.keyCode === 10009 || event.keyCode === 461;

    if (menusOpen() &&
        (event.key === 'Escape' || event.key === 'Backspace' || isBackKey)) {
      event.preventDefault();
      const wasQuality = !el.qualityMenu.classList.contains('hidden');
      closeMenus();
      (wasQuality ? el.btnQuality : el.btnSubtitles).focus();
      return;
    }

    if (isBackKey) {
      event.preventDefault();
      exitPlayer();
      return;
    }

    let handled = true;
    switch (event.key) {
      case 'ArrowRight': seekBy(SEEK_SMALL); break;
      case 'ArrowLeft': seekBy(-SEEK_SMALL); break;
      case 'ArrowUp': seekBy(SEEK_LARGE); break;
      case 'ArrowDown': seekBy(-SEEK_LARGE); break;
      case ' ':
      case 'Enter':
      case 'k': togglePlayPause(); break;
      case 'f': toggleFullscreen(); break;
      case 'm': applyVolume(el.video.muted || el.video.volume === 0 ? 1 : 0); break;
      case 'c': toggleSubtitleMenu(); break;
      case 'q': toggleQualityMenu(); break;
      case 'a': toggleAudioSyncMenu(); break;
      case '+':
      case '=': applyVolume(el.video.volume + 0.1); break;
      case '-': applyVolume(el.video.volume - 0.1); break;
      case 'Escape':
      case 'Backspace':
      case 'BrowserBack': exitPlayer(); break;
      default: handled = false;
    }

    if (handled) {
      event.preventDefault();
      showOSD();
    }
  }

  // --- Wiring ----------------------------------------------------------
  function cacheElements() {
    el.serverName = $('#server-name');
    el.searchInput = $('#search-input');
    el.mediaCount = $('#media-count');
    el.clock = $('#clock');
    el.library = $('#library');
    el.grid = $('#video-grid');
    el.loading = $('#loading');
    el.emptyState = $('#empty-state');
    el.emptyTitle = $('#empty-title');
    el.emptyHint = $('#empty-hint');
    el.continueSection = $('#continue-section');
    el.continueRow = $('#continue-row');
    el.sortSelect = $('#sort-select');
    el.player = $('#player');
    el.video = $('#video-player');
    el.subtitleLayer = $('#subtitle-layer');
    el.buffering = $('#buffering');
    el.osd = $('#osd');
    el.osdTitle = $('#osd-title');
    el.osdBadge = $('#osd-badge');
    el.progressWrap = $('#progress-container');
    el.progressFill = $('#progress-fill');
    el.progressBuffered = $('#progress-buffered');
    el.seekRange = $('#seek-range');
    el.currentTime = $('#current-time');
    el.totalTime = $('#total-time');
    el.btnSubtitles = $('#btn-subtitles');
    el.subtitleMenu = $('#subtitle-menu');
    el.btnQuality = $('#btn-quality');
    el.qualityMenu = $('#quality-menu');
    el.qualityLabel = $('#quality-label');
    el.btnAudioSync = $('#btn-audiosync');
    el.audioSyncMenu = $('#audiosync-menu');
    el.syncLabel = $('#sync-label');
    el.btnMute = $('#btn-mute');
    el.volumeRange = $('#volume-range');
    el.btnSpeed = $('#btn-speed');
    el.btnAspect = $('#btn-aspect');
    el.pauseIndicator = $('#pause-indicator');
    el.toast = $('#toast');
    cardTemplate = $('#card-template');
  }

  function bindLibraryEvents() {
    const onCardActivate = event => {
      const card = event.target.closest('.card');
      if (card && card.dataset.id) openVideo(card.dataset.id);
    };
    el.grid.addEventListener('click', onCardActivate);
    el.continueRow.addEventListener('click', onCardActivate);

    el.searchInput.addEventListener('input', debounce(event => {
      state.query = event.target.value.toLowerCase().trim();
      applyFilters();
    }, 180));

    el.sortSelect.addEventListener('change', event => {
      state.sort = event.target.value;
      applyFilters();
    });

    $$('.chip').forEach(chip => {
      chip.addEventListener('click', () => {
        $$('.chip').forEach(other => {
          other.classList.remove('active');
          other.setAttribute('aria-pressed', 'false');
        });
        chip.classList.add('active');
        chip.setAttribute('aria-pressed', 'true');
        state.filter = chip.dataset.filter;
        applyFilters();
      });
    });

    $('#rescan-btn').addEventListener('click', async () => {
      showToast('Rescanning library...');
      try {
        await postJSON(API.rescan, {});
        setTimeout(() => loadLibrary({ silent: true }), 1500);
      } catch (err) {
        showToast('Rescan failed', 3000);
      }
    });

    $('#fullscreen-btn').addEventListener('click', toggleFullscreen);
  }

  function bindPlayerEvents() {
    const video = el.video;

    $('#player-back-btn').addEventListener('click', () => exitPlayer());
    $('#player-fs-btn').addEventListener('click', toggleFullscreen);
    $('#btn-play-pause').addEventListener('click', togglePlayPause);
    $('#btn-rewind').addEventListener('click', () => seekBy(-SEEK_SMALL));
    $('#btn-forward').addEventListener('click', () => seekBy(SEEK_SMALL));
    el.btnAspect.addEventListener('click', toggleAspect);
    el.btnSpeed.addEventListener('click', cycleSpeed);
    el.btnMute.addEventListener('click', () =>
      applyVolume(video.muted || video.volume === 0 ? 1 : 0));
    el.volumeRange.addEventListener('input', event => applyVolume(event.target.value / 100));

    el.btnSubtitles.addEventListener('click', event => {
      event.stopPropagation();
      toggleSubtitleMenu();
    });
    el.subtitleMenu.addEventListener('click', event => {
      const item = event.target.closest('.popup-item');
      if (item) selectSubtitle(item.dataset.trackId);
    });

    el.btnQuality.addEventListener('click', event => {
      event.stopPropagation();
      toggleQualityMenu();
    });
    el.qualityMenu.addEventListener('click', event => {
      const item = event.target.closest('.popup-item');
      if (item) selectQuality(item.dataset.qualityId);
    });

    el.btnAudioSync.addEventListener('click', event => {
      event.stopPropagation();
      toggleAudioSyncMenu();
    });
    el.audioSyncMenu.addEventListener('click', event => {
      const item = event.target.closest('.popup-item');
      if (item) selectAudioOffset(parseInt(item.dataset.syncValue, 10));
    });

    $('#player-touch-area').addEventListener('click', () => {
      if (menusOpen()) {
        closeMenus();
        return;
      }
      if (el.osd.classList.contains('hidden')) {
        showOSD();
      } else {
        togglePlayPause();
        showOSD();
      }
    });

    // The hidden OSD has pointer-events disabled, so listen on the container.
    el.player.addEventListener('mousemove', () => {
      const now = Date.now();
      if (now - state.lastPointerMove < 120) return;
      state.lastPointerMove = now;
      showOSD();
    });
    el.player.addEventListener('mouseleave', () => {
      if (!el.video.paused && !menusOpen()) hideOSD();
    });
    el.player.addEventListener('dblclick', event => {
      if (event.target.closest('.osd-bottom, .osd-top, .popup-menu')) return;
      toggleFullscreen();
    });

    el.seekRange.addEventListener('pointerdown', () => {
      state.scrubbing = true;
      showOSD(true);
    });
    el.seekRange.addEventListener('input', () => {
      const duration = displayDuration();
      if (!duration) return;
      state.scrubbing = true;
      renderProgress((el.seekRange.value / 1000) * duration, duration, true);
    });
    const commitScrub = () => {
      if (!state.scrubbing) return;
      const duration = displayDuration();
      state.scrubbing = false;
      if (duration) seekTo((el.seekRange.value / 1000) * duration);
      showOSD();
    };
    el.seekRange.addEventListener('change', commitScrub);
    el.seekRange.addEventListener('pointerup', commitScrub);

    video.addEventListener('timeupdate', () => {
      updateOSD();
      saveProgress(false);
    });
    video.addEventListener('progress', updateOSD);
    video.addEventListener('durationchange', updateOSD);
    video.addEventListener('waiting', () => el.buffering.classList.remove('hidden'));
    video.addEventListener('seeking', () => el.buffering.classList.remove('hidden'));
    video.addEventListener('canplay', () => el.buffering.classList.add('hidden'));
    video.addEventListener('playing', () => el.buffering.classList.add('hidden'));

    video.addEventListener('play', () => {
      $('#icon-play').classList.add('hidden');
      $('#icon-pause').classList.remove('hidden');
      el.pauseIndicator.classList.add('hidden');
      acquireWakeLock();
    });

    video.addEventListener('pause', () => {
      $('#icon-play').classList.remove('hidden');
      $('#icon-pause').classList.add('hidden');
      if (state.playback) el.pauseIndicator.classList.remove('hidden');
      saveProgress(true);
      releaseWakeLock();
    });

    video.addEventListener('ended', () => {
      if (!state.playback) return;
      const duration = displayDuration();
      // A restarted ffmpeg pipe can end early; only treat a real end as finished.
      if (!state.playback.native_seek && duration && displayTime() < duration - 5) {
        el.buffering.classList.add('hidden');
        return;
      }
      const id = state.playback.id;
      postJSON(API.progress, { id: id, position: 0, duration: duration, finished: true })
        .catch(() => {});
      state.progress[id] = {
        video_id: id, position: 0, duration: duration,
        finished: true, updated_at: Date.now() / 1000
      };
      showToast('Playback finished', 2500);
      exitPlayer();
    });

    video.addEventListener('error', () => {
      el.buffering.classList.add('hidden');
      if (!video.error || !state.playback) return;
      const messages = {
        1: 'Playback aborted.',
        2: 'Network error while streaming.',
        3: 'This file could not be decoded.',
        4: state.ffmpegAvailable
          ? 'This format is not supported by your browser.'
          : 'Unsupported format. Put ffmpeg next to server.py to enable transcoding.'
      };
      showToast(messages[video.error.code] || 'Playback error', 5000);
    });

    document.addEventListener('click', event => {
      if (!menusOpen()) return;
      const insideMenu = event.target.closest('.popup-menu');
      const onToggle = el.btnSubtitles.contains(event.target) ||
        el.btnQuality.contains(event.target);
      if (!insideMenu && !onToggle) closeMenus();
    });
  }

  function startClock() {
    const tick = () => {
      el.clock.textContent = new Date().toLocaleTimeString(undefined, {
        hour: 'numeric', minute: '2-digit'
      });
    };
    tick();
    setInterval(tick, 20000);
  }

  // Flexbox gap landed well after grid gap; older TV browsers need margins.
  function detectFlexGap() {
    const probe = document.createElement('div');
    probe.style.cssText =
      'display:flex;flex-direction:column;row-gap:12px;position:absolute;visibility:hidden';
    probe.appendChild(document.createElement('div'));
    probe.appendChild(document.createElement('div'));
    document.body.appendChild(probe);
    const supported = probe.scrollHeight === 12;
    probe.parentNode.removeChild(probe);
    if (!supported) document.documentElement.classList.add('no-flex-gap');
  }

  function init() {
    cacheElements();
    detectFlexGap();
    setupThumbObserver();
    bindLibraryEvents();
    bindPlayerEvents();
    startClock();

    window.addEventListener('error', event => {
      showToast('Script error: ' + (event.message || 'unknown'), 8000);
    });

    let storedVolume = 1;
    try {
      const saved = parseFloat(localStorage.getItem('litejelly_volume'));
      if (!isNaN(saved)) storedVolume = saved;
      const savedQuality = localStorage.getItem('litejelly_quality');
      if (savedQuality) state.quality = savedQuality;
      const savedOffset = parseInt(localStorage.getItem('litejelly_audio_offset'), 10);
      if (!isNaN(savedOffset)) state.audioOffset = savedOffset;
    } catch (err) { /* storage unavailable */ }
    applyVolume(storedVolume);
    updateQualityLabel();
    updateSyncLabel();

    document.addEventListener('keydown', handleKeyDown);
    document.addEventListener('fullscreenchange', syncFullscreenIcons);
    document.addEventListener('webkitfullscreenchange', syncFullscreenIcons);

    window.addEventListener('popstate', () => {
      if (state.view === 'PLAYER') exitPlayer(true);
    });

    window.addEventListener('resize', debounce(() => {
      if (state.view === 'LIBRARY') measureColumns();
    }, 200));

    window.addEventListener('pagehide', () => saveProgress(true));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) saveProgress(true);
      else if (state.view === 'PLAYER' && !el.video.paused) acquireWakeLock();
    });

    loadConfig();
    loadLibrary();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
