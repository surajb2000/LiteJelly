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

  /* Which kind of screen is this, judged by capability rather than identity.
   *
   * No browser reports "I am a television", and user-agent strings lie, so
   * the question asked here is the one that actually changes the design:
   * can the thing hover, and how big is it. A wide screen that cannot hover
   * is being driven by a remote from across a room.
   */
  const PROFILE_KEY = 'litejelly.profile';
  const PROFILES = ['tv', 'desktop', 'phone'];

  function mediaMatch(query) {
    return !!(window.matchMedia && window.matchMedia(query).matches);
  }

  function detectProfile() {
    if (Math.min(window.innerWidth, window.innerHeight) < 560
        || window.innerWidth < 900) {
      return 'phone';
    }
    // hover is Chrome 38+, pointer 41+, so both are safe on the TV floor.
    if (mediaMatch('(hover: none)') || mediaMatch('(pointer: coarse)')
        || mediaMatch('(pointer: none)')) {
      return 'tv';
    }
    if (!window.matchMedia) return 'desktop';
    return 'desktop';
  }

  function storedProfile() {
    try {
      const saved = window.localStorage.getItem(PROFILE_KEY);
      return PROFILES.indexOf(saved) === -1 ? '' : saved;
    } catch (err) {
      return '';
    }
  }

  function applyProfile(name, remember) {
    if (PROFILES.indexOf(name) === -1) return;
    state.profile = name;
    document.documentElement.setAttribute('data-profile', name);
    if (!remember) return;
    try {
      window.localStorage.setItem(PROFILE_KEY, name);
    } catch (err) {
      // Private mode; the profile just will not persist.
    }
  }

  // A remote that turns out to have a mouse should stop being treated as a
  // remote. Detection is a starting guess, not a verdict.
  function watchInputModality() {
    if (storedProfile()) return;
    function sawPointer() {
      if (state.profile === 'tv' && window.innerWidth >= 900) {
        applyProfile('desktop', false);
      }
      document.removeEventListener('mousemove', sawPointer, true);
    }
    document.addEventListener('mousemove', sawPointer, true);
  }

  const API = {
    config: '/api/config',
    library: '/api/library',
    rescan: '/api/rescan',
    playback: '/api/playback',
    seekpoint: '/api/seekpoint',
    skip: '/api/skip',
    progress: '/api/progress',
    thumbnail: '/api/thumbnail',
    artwork: '/api/artwork',
    details: '/api/details',
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
    profile: 'desktop',
    videos: [],
    filtered: [],
    entries: [],
    progress: {},
    filter: 'all',
    category: 'all',
    sort: 'recent',
    query: '',
    seriesId: null,
    season: 'all',
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
    lastPointerMove: 0,
    continueWatching: [],
    upNextTimer: null,
    upNextRemaining: 0,
    skipSegment: null,
    skipRetry: null
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const el = {};
  let cardTemplate = null;
  let thumbObserver = null;
  let thumbFallbackTimer = null;

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
      state.continueWatching = Array.isArray(data.continue_watching)
        ? data.continue_watching : [];
      state.ffmpegAvailable = !!data.ffmpeg_available;
      renderCategoryChips();
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
    const byTitle = (a.title || a.name || '').localeCompare(b.title || b.name || '', undefined,
      { numeric: true, sensitivity: 'base' });
    if (byTitle !== 0) return byTitle;
    return compareByEpisode(a, b);
  }

  function compareByEpisode(a, b) {
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

  // --- Grouping --------------------------------------------------------

  function episodeCode(video) {
    if (video.season == null || video.episode == null) return '';
    return 'S' + String(video.season).padStart(2, '0') +
           'E' + String(video.episode).padStart(2, '0');
  }

  // Series are derived on the client so the payload stays a flat list.
  function groupIntoSeries(videos) {
    const series = new Map();
    videos.forEach(video => {
      if (!video.series_id) return;
      let group = series.get(video.series_id);
      if (!group) {
        group = {
          isSeries: true,
          id: video.series_id,
          title: video.title || video.name,
          category: video.category,
          episodes: [],
          seasons: {},
          newestTs: 0,
          size: 0
        };
        series.set(video.series_id, group);
      }
      group.episodes.push(video);
      if (video.season != null) group.seasons[video.season] = true;
      if (video.modified_ts > group.newestTs) {
        group.newestTs = video.modified_ts;
        group.newest = video;
      }
      group.size += video.size || 0;
    });
    return series;
  }

  function seriesById(id) {
    return groupIntoSeries(state.videos).get(id) || null;
  }

  // A series is sorted as if it were a single item with the newest episode's date.
  function buildEntries(videos) {
    const series = groupIntoSeries(videos);
    const seen = new Set();
    const entries = [];
    videos.forEach(video => {
      if (!video.series_id) {
        entries.push(video);
        return;
      }
      if (seen.has(video.series_id)) return;
      seen.add(video.series_id);
      const group = series.get(video.series_id);
      group.modified_ts = group.newestTs;
      group.name = group.title;
      entries.push(group);
    });
    return entries;
  }

  function categoryCounts() {
    const counts = { shows: 0, anime: 0, movies: 0 };
    state.videos.forEach(video => {
      if (counts[video.category] != null) counts[video.category] += 1;
    });
    return counts;
  }

  function renderCategoryChips() {
    const counts = categoryCounts();
    const present = Object.keys(counts).filter(key => counts[key] > 0);
    // Only worth showing when the library actually spans more than one kind.
    const useful = present.length > 1;
    el.categoryChips.classList.toggle('hidden', !useful);
    if (!useful) {
      state.category = 'all';
      return;
    }
    $$('.chip', el.categoryChips).forEach(chip => {
      const key = chip.dataset.category;
      chip.classList.toggle('hidden', key !== 'all' && !counts[key]);
    });
  }

  function applyFilters() {
    let list = state.videos;

    if (state.seriesId) {
      list = list.filter(video => video.series_id === state.seriesId);
      if (state.season !== 'all') {
        list = list.filter(video => String(video.season) === state.season);
      }
      state.filtered = list.slice().sort(compareByEpisode);
      state.entries = state.filtered;
      renderGrid();
      return;
    }

    if (state.category !== 'all') {
      list = list.filter(video => video.category === state.category);
    }

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
    // Searching is a hunt for one file, so show episodes rather than series.
    state.entries = state.query ? state.filtered : buildEntries(state.filtered);
    renderGrid();
  }

  function openSeries(seriesId) {
    const group = seriesById(seriesId);
    if (!group) return;
    state.seriesId = seriesId;
    state.season = 'all';
    renderSeriesHeader(group);
    applyFilters();
    window.scrollTo(0, 0);
    focusFirstCard();
  }

  function closeSeries() {
    if (!state.seriesId) return false;
    state.seriesId = null;
    state.season = 'all';
    renderSeriesHeader(null);
    applyFilters();
    return true;
  }

  function renderSeriesHeader(group) {
    el.seriesBack.classList.toggle('hidden', !group);
    el.seasonChips.classList.add('hidden');

    if (!group) {
      el.sectionTitle.textContent = 'Media Library';
      el.sectionSubtitle.textContent = 'Local streaming optimized for TV & mobile';
      el.seriesPlot.textContent = '';
      el.seriesPlot.classList.add('hidden');
      renderCategoryChips();
      return;
    }

    el.categoryChips.classList.add('hidden');
    el.sectionTitle.textContent = group.title;
    const seasons = Object.keys(group.seasons).map(Number).sort((a, b) => a - b);
    const parts = [group.episodes.length + (group.episodes.length === 1 ? ' episode' : ' episodes')];
    if (seasons.length > 1) {
      parts.push(seasons.length + ' seasons');
    }
    el.sectionSubtitle.textContent = parts.join(' · ');
    showSeriesPlot(group);

    if (seasons.length > 1) {
      const fragment = document.createDocumentFragment();
      fragment.appendChild(seasonChip('all', 'All seasons'));
      seasons.forEach(season => {
        fragment.appendChild(seasonChip(String(season), 'Season ' + season));
      });
      el.seasonChips.replaceChildren(fragment);
      el.seasonChips.classList.remove('hidden');
    }
  }

  // Plots are too long to ship with every item, so fetch the one on screen.
  function showSeriesPlot(group) {
    el.seriesPlot.textContent = '';
    el.seriesPlot.classList.add('hidden');
    const first = group.episodes.slice().sort(compareByEpisode)[0];
    if (!first) return;

    getJSON(API.details + '?id=' + encodeURIComponent(first.id)).then(data => {
      if (state.seriesId !== group.id) return;  // Moved on while it loaded.
      const meta = (data && data.metadata) || {};
      if (!meta.plot) return;
      el.seriesPlot.textContent = meta.plot;
      el.seriesPlot.classList.remove('hidden');
    }).catch(() => { /* metadata is optional */ });
  }

  function seasonChip(value, label) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip' + (state.season === value ? ' active' : '');
    chip.dataset.season = value;
    chip.setAttribute('aria-pressed', state.season === value ? 'true' : 'false');
    chip.textContent = label;
    chip.addEventListener('click', () => {
      state.season = value;
      $$('.chip', el.seasonChips).forEach(other => {
        const on = other.dataset.season === value;
        other.classList.toggle('active', on);
        other.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      applyFilters();
    });
    return chip;
  }

  function focusFirstCard() {
    requestAnimationFrame(() => {
      const first = $('.card', el.grid);
      if (first) first.focus();
    });
  }

  function progressFraction(video) {
    const entry = state.progress[video.id];
    if (!entry || entry.finished || !entry.duration) return 0;
    return Math.min(1, Math.max(0, entry.position / entry.duration));
  }

  function buildCard(video, episodeStyle) {
    const card = cardTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.id = video.id;
    card.setAttribute('aria-label', 'Play ' + video.name);
    card.title = video.filename;

    // Inside a series the heading can drop the show name; anywhere else it
    // would leave "Episode 2" with no idea which show that is.
    const inSeries = episodeStyle === undefined ? !!state.seriesId : episodeStyle;
    const heading = inSeries ? episodeHeading(video) : video.name;
    $('.card-title', card).textContent = heading;
    $('.card-letter', card).textContent = (heading || '?').charAt(0).toUpperCase();
    $('.card-size', card).textContent = video.size_human || '';
    $('.card-date', card).textContent = formatDate(video.modified);

    const code = episodeCode(video);
    if (inSeries && code) {
      const sub = $('.card-subtitle', card);
      sub.textContent = code;
      sub.classList.remove('hidden');
    }

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
    const shape = artworkShape(video, inSeries);
    card.classList.add('shape-' + shape);
    img.dataset.src = artworkUrl(video, shape);
    if (thumbObserver) thumbObserver.observe(card);
    else loadThumbnail(img);

    return card;
  }

  /* Posters are portrait and episode stills are landscape, so the shape has
   * to follow the content. Forcing both into one box is what cropped every
   * poster to a slice, and what made a series look like the same picture
   * repeated once per episode.
   */
  function artworkUrl(video, shape) {
    // An episode's own frame identifies it; the series poster does not.
    if (shape === 'still' || !video.has_poster) {
      return API.thumbnail + '?id=' + encodeURIComponent(video.id);
    }
    return API.artwork + '?id=' + encodeURIComponent(video.id);
  }

  function artworkShape(video, inSeries) {
    if (inSeries || (video.episode != null && video.series_id)) return 'still';
    return video.has_poster ? 'poster' : 'still';
  }

  // Without an episode name, repeating the show title on every row says
  // nothing; the number at least identifies the episode.
  function episodeHeading(video) {
    if (video.episode_title) return video.episode_title;
    if (video.episode != null) return 'Episode ' + video.episode;
    return video.title || video.name;
  }

  function buildSeriesCard(group) {
    const card = cardTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.seriesId = group.id;
    card.classList.add('series-card');
    card.setAttribute('aria-label', 'Open ' + group.title);
    card.title = group.title;

    $('.card-title', card).textContent = group.title;
    $('.card-letter', card).textContent = (group.title || '?').charAt(0).toUpperCase();

    const count = group.episodes.length;
    const sub = $('.card-subtitle', card);
    sub.textContent = count + (count === 1 ? ' episode' : ' episodes');
    sub.classList.remove('hidden');

    $('.card-size', card).textContent = humanSize(group.size);
    $('.card-date', card).textContent = group.newest ? formatDate(group.newest.modified) : '';

    const badge = $('.card-badge', card);
    badge.textContent = group.category === 'anime' ? 'ANIME' : 'SERIES';
    badge.classList.add('badge-series');

    // Resume marker for whichever episode is part-watched.
    const started = group.episodes.find(episode => progressFraction(episode) > 0.01);
    if (started) {
      const wrap = $('.card-progress', card);
      wrap.classList.remove('hidden');
      $('.card-progress-fill', wrap).style.width =
        (progressFraction(started) * 100).toFixed(1) + '%';
    }

    const poster = group.episodes.slice().sort(compareByEpisode)[0] || group.newest;
    const img = $('.thumb-img', card);
    if (poster) {
      // A series is represented by its poster, never by one episode's frame.
      const shape = poster.has_poster ? 'poster' : 'still';
      card.classList.add('shape-' + shape);
      img.dataset.src = artworkUrl(poster, shape);
      if (thumbObserver) thumbObserver.observe(card);
      else loadThumbnail(img);
    }

    return card;
  }

  function humanSize(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes || 0;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return value.toFixed(1) + ' ' + units[unit];
  }

  // The server answers 202 while a thumbnail is still being made, which the
  // browser reports as a load error. Retrying is what makes them appear on a
  // slow machine; giving up on the first failure left every card blank.
  const THUMB_RETRY_DELAYS = [1500, 4000, 9000, 20000];

  function loadThumbnail(img) {
    if (!img.dataset.src || img.getAttribute('src')) return;
    attemptThumbnail(img, img.dataset.src);
  }

  function attemptThumbnail(img, src) {
    img.addEventListener('load', () => {
      img.classList.add('loaded');
      const letter = $('.card-letter', img.parentElement);
      if (letter) letter.classList.add('hidden');
    }, { once: true });
    img.addEventListener('error', () => retryThumbnail(img), { once: true });
    img.src = src;
  }

  function retryThumbnail(img) {
    img.removeAttribute('src');
    const attempt = Number(img.dataset.attempt || 0);
    if (attempt >= THUMB_RETRY_DELAYS.length) return;  // Keep the letter.
    img.dataset.attempt = String(attempt + 1);
    setTimeout(() => {
      if (!document.body.contains(img)) return;
      // Cache-bust so the browser does not reuse the 202 as the answer.
      attemptThumbnail(img, img.dataset.src + '&try=' + img.dataset.attempt);
    }, THUMB_RETRY_DELAYS[attempt]);
  }

  function setupThumbObserver() {
    if (!('IntersectionObserver' in window)) return;
    // Observe the card, not the <img>. The image is absolutely positioned
    // inside a padding-ratio box, and an engine that resolves its percentage
    // height to zero gives it no layout box, which the observer never reports.
    thumbObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        const img = $('.thumb-img', entry.target);
        if (img) loadThumbnail(img);
      });
    }, { rootMargin: '300px 0px' });
  }

  // Safety net for engines where the observer never reports anything. Without
  // it, a TV browser that misbehaves here shows no thumbnails at all, forever.
  function ensureThumbnailsRequested() {
    clearTimeout(thumbFallbackTimer);
    thumbFallbackTimer = setTimeout(() => {
      const images = $$('.thumb-img', el.grid).concat($$('.thumb-img', el.continueRow));
      if (!images.length) return;
      if (images.some(img => img.getAttribute('src'))) return;
      images.forEach(loadThumbnail);
    }, 2500);
  }

  function renderGrid() {
    const entries = state.entries;
    const count = entries.length;
    const videoCount = state.filtered.length;
    el.mediaCount.textContent = videoCount + (videoCount === 1 ? ' video' : ' videos');

    if (!count) {
      el.grid.replaceChildren();
      el.emptyTitle.textContent = state.videos.length ? 'No matches' : 'No videos found';
      el.emptyHint.textContent = state.videos.length
        ? 'Try a different search or filter.'
        : 'Add a media folder on the admin page, then rescan.';
      el.emptyState.classList.remove('hidden');
      return;
    }

    el.emptyState.classList.add('hidden');
    const fragment = document.createDocumentFragment();
    entries.forEach(entry => {
      fragment.appendChild(entry.isSeries ? buildSeriesCard(entry) : buildCard(entry));
    });
    el.grid.replaceChildren(fragment);
    // Portrait tiles want a narrower column than landscape ones, and a grid
    // cannot size its tracks from the shape of its children.
    el.grid.classList.toggle('grid-poster',
                             !state.seriesId && $$('.shape-poster', el.grid).length
                               > $$('.shape-still', el.grid).length);
    requestAnimationFrame(measureColumns);
    ensureThumbnailsRequested();
  }

  function renderContinueWatching() {
    // The server decides what belongs here: one row per series, and the next
    // episode once the last one was finished.
    const byId = new Map(state.videos.map(video => [video.id, video]));
    const items = state.continueWatching
      .map(entry => ({ video: byId.get(entry.id), entry: entry }))
      .filter(item => item.video);

    if (!items.length) {
      el.continueSection.classList.add('hidden');
      el.continueRow.replaceChildren();
      return;
    }

    const fragment = document.createDocumentFragment();
    items.forEach(item => {
      const card = buildCard(item.video, false);
      card.classList.add('rail-card');
      const meta = $('.card-meta', card);
      const label = document.createElement('span');
      if (item.entry.next_up) {
        label.textContent = 'Next episode';
      } else {
        const left = Math.max(0, item.entry.duration - item.entry.position);
        label.textContent = item.entry.duration
          ? formatTime(left) + ' left'
          : 'Resume';
      }
      meta.replaceChildren(label);
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

    cancelUpNext();
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

  // --- Up next ---------------------------------------------------------

  const UP_NEXT_SECONDS = 10;

  function playSibling(key) {
    const target = state.playback && state.playback[key];
    if (!target) return;
    // Record where we got to before moving on, or the jump loses the position.
    saveProgress(true);
    openVideo(target);
  }

  function syncEpisodeButtons(plan) {
    el.btnPrevEpisode.classList.toggle('hidden', !(plan && plan.prev_id));
    el.btnNextEpisode.classList.toggle('hidden', !(plan && plan.next_id));
  }

  // --- Skip intro / credits --------------------------------------------

  function currentSkipSegment() {
    const plan = state.playback;
    if (!plan || !plan.skip_segments || !plan.skip_segments.length) return null;
    const now = displayTime();
    for (let i = 0; i < plan.skip_segments.length; i++) {
      const segment = plan.skip_segments[i];
      // Stop offering it in the last couple of seconds, or the button flickers
      // away just as someone reaches for it.
      if (now >= segment.start && now < segment.end - 1) return segment;
    }
    return null;
  }

  function syncSkipButton() {
    if (state.view !== 'PLAYER' || pendingUpNext()) {
      el.skipSegment.classList.add('hidden');
      return;
    }
    const segment = currentSkipSegment();
    if (!segment) {
      el.skipSegment.classList.add('hidden');
      state.skipSegment = null;
      return;
    }
    if (state.skipSegment !== segment) {
      state.skipSegment = segment;
      el.skipSegment.textContent = segment.label;
    }
    el.skipSegment.classList.remove('hidden');
  }

  function skipCurrentSegment() {
    const segment = state.skipSegment;
    if (!segment) return;
    el.skipSegment.classList.add('hidden');
    state.skipSegment = null;
    seekTo(segment.end);
  }

  // A lookup that was still in flight when playback began; ask again rather
  // than let the episode play as though it had no intro.
  const SKIP_RETRY_DELAYS = [2500, 6000, 15000];

  function cancelSkipRetry() {
    if (state.skipRetry) clearTimeout(state.skipRetry);
    state.skipRetry = null;
  }

  function scheduleSkipRetry(plan, attempt) {
    cancelSkipRetry();
    if (!plan.skip_pending || attempt >= SKIP_RETRY_DELAYS.length) return;
    state.skipRetry = setTimeout(async () => {
      state.skipRetry = null;
      if (state.playback !== plan) return;
      try {
        const data = await getJSON(API.skip + '?id=' + encodeURIComponent(plan.id));
        if (state.playback !== plan) return;
        if (data.skip_segments && data.skip_segments.length) {
          plan.skip_segments = data.skip_segments;
          plan.skip_pending = false;
          renderSegmentMarkers(plan);
          syncSkipButton();
          return;
        }
        plan.skip_pending = !!data.skip_pending;
      } catch (err) {
        // Offline or the lookup failed; the episode just has no skip button.
      }
      scheduleSkipRetry(plan, attempt + 1);
    }, SKIP_RETRY_DELAYS[attempt]);
  }

  function renderSegmentMarkers(plan) {
    const bar = el.segmentMarkers;
    if (!bar) return;
    while (bar.firstChild) bar.removeChild(bar.firstChild);

    const duration = plan && plan.duration;
    const segments = (plan && plan.skip_segments) || [];
    if (!duration || !segments.length) return;

    for (let i = 0; i < segments.length; i++) {
      const segment = segments[i];
      const start = Math.max(0, Math.min(1, segment.start / duration));
      const end = Math.max(0, Math.min(1, segment.end / duration));
      if (end <= start) continue;
      const mark = document.createElement('span');
      mark.className = 'segment-mark segment-' + (segment.kind || 'intro');
      mark.style.left = (start * 100).toFixed(3) + '%';
      // Always wide enough to see, however short the segment is.
      mark.style.width = Math.max(0.6, (end - start) * 100).toFixed(3) + '%';
      mark.title = segment.label;
      bar.appendChild(mark);
    }
  }

  function showUpNext(video) {
    hideOSD();
    el.video.pause();
    el.upNextTitle.textContent = video.title || video.name;
    const code = episodeCode(video);
    const parts = [];
    if (code) parts.push(code);
    if (video.episode_title) parts.push(video.episode_title);
    el.upNextSub.textContent = parts.join(' \u00b7 ');

    state.upNextRemaining = UP_NEXT_SECONDS;
    renderUpNextCountdown();
    el.upNext.classList.remove('hidden');
    el.upNextPlay.focus();

    clearInterval(state.upNextTimer);
    state.upNextTimer = setInterval(() => {
      state.upNextRemaining -= 1;
      renderUpNextCountdown();
      if (state.upNextRemaining <= 0) playUpNext(video.id);
    }, 1000);
  }

  function renderUpNextCountdown() {
    el.upNextCountdown.textContent = state.upNextRemaining > 0
      ? '(' + state.upNextRemaining + ')' : '';
  }

  function cancelUpNext() {
    clearInterval(state.upNextTimer);
    state.upNextTimer = null;
    el.upNext.classList.add('hidden');
  }

  function playUpNext(videoId) {
    cancelUpNext();
    openVideo(videoId);
  }

  function pendingUpNext() {
    return !el.upNext.classList.contains('hidden');
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
    syncEpisodeButtons(plan);
    renderSegmentMarkers(plan);
    scheduleSkipRetry(plan, 0);

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
    if (started && started.catch) {
      // Auto-advance calls play() without a fresh gesture, which a browser may
      // refuse. Say so rather than leaving a black screen.
      started.catch(() => {
        el.pauseIndicator.classList.remove('hidden');
        showOSD(true);
        showToast('Press play to start', 4000);
      });
    }

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
    cancelUpNext();
    cancelSkipRetry();
    el.skipSegment.classList.add('hidden');
    state.skipSegment = null;
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
    // The resume rail is built server-side, so refresh it rather than guessing.
    loadLibrary({ silent: true });

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
    const rows = ['Use this if speech does not match the lips.',
      'Applied on the server, so it survives seeking.'];
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
    syncSkipButton();
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

    // Back should step out of a series before it leaves the app.
    // 10009 (Tizen) and 461 (webOS) are the remote's Back button.
    const isBack = event.key === 'Escape' || event.key === 'Backspace' ||
                   event.key === 'BrowserBack' ||
                   event.keyCode === 10009 || event.keyCode === 461;
    if (isBack && !isTypingTarget(active)) {
      if (closeSeries()) {
        event.preventDefault();
        focusFirstCard();
        return;
      }
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

    // Up next owns the remote while it is showing: seeking a finished video
    // would be meaningless, and Back should stop the countdown, not the app.
    if (pendingUpNext()) {
      if (event.key === 'Escape' || event.key === 'Backspace' || isBackKey) {
        event.preventDefault();
        cancelUpNext();
        exitPlayer();
        return;
      }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        (document.activeElement === el.upNextPlay ? el.upNextCancel : el.upNextPlay).focus();
        return;
      }
      return;  // Enter and Space activate the focused button natively.
    }

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
      case 'n': playSibling('next_id'); break;
      case 'p': playSibling('prev_id'); break;
      case 's': skipCurrentSegment(); break;
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
    el.sectionTitle = $('#section-title');
    el.sectionSubtitle = $('#section-subtitle');
    el.seriesPlot = $('#series-plot');
    el.seriesBack = $('#series-back');
    el.categoryChips = $('#category-chips');
    el.seasonChips = $('#season-chips');
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
    el.segmentMarkers = $('#segment-markers');
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
    el.btnPrevEpisode = $('#btn-prev-episode');
    el.btnNextEpisode = $('#btn-next-episode');
    el.skipSegment = $('#skip-segment');
    el.upNext = $('#up-next');
    el.upNextTitle = $('#up-next-heading');
    el.upNextSub = $('#up-next-sub');
    el.upNextPlay = $('#up-next-play');
    el.upNextCancel = $('#up-next-cancel');
    el.upNextCountdown = $('#up-next-countdown');
    el.toast = $('#toast');
    cardTemplate = $('#card-template');
  }

  function bindChipGroup(group, key) {
    if (!group) return;
    $$('.chip', group).forEach(chip => {
      chip.addEventListener('click', () => {
        $$('.chip', group).forEach(other => {
          other.classList.remove('active');
          other.setAttribute('aria-pressed', 'false');
        });
        chip.classList.add('active');
        chip.setAttribute('aria-pressed', 'true');
        state[key] = chip.dataset[key];
        if (key === 'category' && state.seriesId) closeSeries();
        applyFilters();
      });
    });
  }

  function bindLibraryEvents() {
    const onCardActivate = event => {
      const card = event.target.closest('.card');
      if (!card) return;
      if (card.dataset.seriesId) openSeries(card.dataset.seriesId);
      else if (card.dataset.id) openVideo(card.dataset.id);
    };
    el.grid.addEventListener('click', onCardActivate);
    el.continueRow.addEventListener('click', onCardActivate);

    el.seriesBack.addEventListener('click', () => {
      closeSeries();
      focusFirstCard();
    });

    el.searchInput.addEventListener('input', debounce(event => {
      state.query = event.target.value.toLowerCase().trim();
      // A search spans the whole library, not the series being browsed.
      if (state.query && state.seriesId) closeSeries();
      applyFilters();
    }, 180));

    el.sortSelect.addEventListener('change', event => {
      state.sort = event.target.value;
      applyFilters();
    });

    bindChipGroup(el.categoryChips, 'category');
    bindChipGroup($('.filter-chips[aria-label="Filter by format"]'), 'filter');

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
    el.btnPrevEpisode.addEventListener('click', () => playSibling('prev_id'));
    el.btnNextEpisode.addEventListener('click', () => playSibling('next_id'));
    el.skipSegment.addEventListener('click', skipCurrentSegment);
    el.upNextCancel.addEventListener('click', () => {
      cancelUpNext();
      exitPlayer();
    });
    el.upNextPlay.addEventListener('click', () => {
      const nextId = state.playback && state.playback.next_id;
      if (nextId) playUpNext(nextId);
    });
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
      const nextId = state.playback.next_id;
      postJSON(API.progress, { id: id, position: 0, duration: duration, finished: true })
        .catch(() => {});
      state.progress[id] = {
        video_id: id, position: 0, duration: duration,
        finished: true, updated_at: Date.now() / 1000
      };

      const following = nextId && state.videos.find(item => item.id === nextId);
      if (following) {
        showUpNext(following);
        return;
      }
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
    applyProfile(storedProfile() || detectProfile(), false);
    watchInputModality();
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
