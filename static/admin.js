/* LiteJelly admin page.
 *
 * No build step and no framework, same as the player. Kept to ES5-safe syntax
 * so it runs on the same engines: no optional chaining, no template literals
 * in hot paths, no replaceChildren.
 */
(function () {
  'use strict';

  var API = '/api/admin/settings';

  var state = {
    contentTypes: ['mixed'],
    presets: [],
    restartFields: [],
    localIp: '',
    port: 0
  };

  function $(id) { return document.getElementById(id); }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  // -- tabs ----------------------------------------------------------------

  function showPanel(panelId) {
    var tabs = $('tabs').querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) {
      var selected = tabs[i].getAttribute('data-panel') === panelId;
      tabs[i].setAttribute('aria-selected', selected ? 'true' : 'false');
      tabs[i].className = selected ? 'tab active' : 'tab';
      $(tabs[i].getAttribute('data-panel')).hidden = !selected;
    }
    try {
      window.localStorage.setItem('litejelly_admin_tab', panelId);
    } catch (e) { /* private mode */ }
  }

  function initTabs() {
    var tabs = $('tabs').querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function () {
        showPanel(this.getAttribute('data-panel'));
      });
    }
    var remembered = null;
    try {
      remembered = window.localStorage.getItem('litejelly_admin_tab');
    } catch (e) { /* private mode */ }
    showPanel(remembered && $(remembered) ? remembered : 'panel-library');
  }

  function text(node, value) {
    clear(node);
    node.appendChild(document.createTextNode(value == null ? '' : String(value)));
  }

  // Token support: /admin?token=... keeps working for remote access without
  // storing the token anywhere it could leak to the library page.
  function adminToken() {
    var match = /[?&]token=([^&]+)/.exec(window.location.search);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function request(method, url, body) {
    var token = adminToken();
    var headers = { 'X-LiteJelly-Admin': '1' };
    if (token) { headers['X-Admin-Token'] = token; }
    var options = { method: method, headers: headers, credentials: 'same-origin' };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
  }

  function showBanner(message, kind) {
    var banner = $('banner');
    if (!message) { banner.hidden = true; return; }
    banner.className = 'banner' + (kind ? ' ' + kind : '');
    text(banner, message);
    banner.hidden = false;
  }

  function setStatus(message, kind) {
    var node = $('save-status');
    node.className = 'save-status' + (kind ? ' ' + kind : '');
    text(node, message);
  }

  // -- media directory rows ------------------------------------------------

  function fillSelect(select, values, selected) {
    clear(select);
    for (var i = 0; i < values.length; i++) {
      var option = document.createElement('option');
      option.value = values[i];
      option.appendChild(document.createTextNode(
        values[i].charAt(0).toUpperCase() + values[i].slice(1)));
      if (values[i] === selected) { option.selected = true; }
      select.appendChild(option);
    }
  }

  function addDirRow(entry) {
    entry = entry || { path: '', content_type: 'mixed', label: '' };
    var fragment = $('tpl-dir').content.cloneNode(true);
    var row = fragment.querySelector('.dir');
    row.querySelector('.dir-path').value = entry.path || '';
    row.querySelector('.dir-label').value = entry.label || '';
    fillSelect(row.querySelector('.dir-type'), state.contentTypes,
               entry.content_type || 'mixed');
    row.querySelector('.dir-remove').addEventListener('click', function () {
      row.parentNode.removeChild(row);
    });
    $('dirs').appendChild(fragment);
    return row;
  }

  function readDirs() {
    var rows = $('dirs').querySelectorAll('.dir');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var path = rows[i].querySelector('.dir-path').value.trim();
      if (!path) { continue; }
      out.push({
        path: path,
        content_type: rows[i].querySelector('.dir-type').value,
        label: rows[i].querySelector('.dir-label').value.trim()
      });
    }
    return out;
  }

  // -- form <-> settings ---------------------------------------------------

  function fillForm(settings) {
    $('server_name').value = settings.server_name || '';
    $('host').value = settings.host || '';
    $('port').value = settings.port || '';
    $('scan_interval').value = settings.scan_interval || '';
    $('thumbnail_workers').value = settings.thumbnail_workers || '';
    $('stream_buffer_mb').value = settings.stream_buffer_mb || '';
    $('allow_hevc_direct').checked = !!settings.allow_hevc_direct;
    $('ffmpeg_path').value = settings.ffmpeg_path || '';
    $('ffprobe_path').value = settings.ffprobe_path || '';
    $('admin_token').value = settings.admin_token || '';

    var tc = settings.transcode || {};
    fillSelect($('tc_preset'), state.presets, tc.preset);
    $('tc_crf').value = tc.crf == null ? '' : tc.crf;
    $('tc_max_concurrent').value = tc.max_concurrent == null ? '' : tc.max_concurrent;
    $('tc_max_video_bitrate').value = tc.max_video_bitrate || '';
    $('tc_audio_bitrate').value = tc.audio_bitrate || '';
    setResolution(tc.resolution);

    clear($('dirs'));
    var dirs = settings.media_dirs || [];
    for (var i = 0; i < dirs.length; i++) { addDirRow(dirs[i]); }
    if (!dirs.length) { addDirRow(); }

    state.port = settings.port || state.port;
    $('setup').hidden = dirs.length > 0;
    updateRemoteUrl();
  }

  // The select only lists common rungs; keep a custom value from config.json.
  function setResolution(value) {
    var select = $('tc_resolution');
    if (!value) { return; }
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) {
        select.value = value;
        return;
      }
    }
    var option = document.createElement('option');
    option.value = value;
    option.appendChild(document.createTextNode(value + ' (custom)'));
    select.appendChild(option);
    select.value = value;
  }

  function updateRemoteUrl() {
    var token = $('admin_token').value.trim();
    var wrap = $('remote-url-wrap');
    if (!token || !state.localIp) {
      wrap.hidden = true;
      return;
    }
    $('remote-url').value = 'http://' + state.localIp + ':' + state.port +
                            '/admin?token=' + encodeURIComponent(token);
    wrap.hidden = false;
  }

  function generateToken() {
    var alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    var out = '';
    var i;
    if (window.crypto && window.crypto.getRandomValues) {
      var bytes = new Uint8Array(24);
      window.crypto.getRandomValues(bytes);
      for (i = 0; i < bytes.length; i++) {
        out += alphabet.charAt(bytes[i] % alphabet.length);
      }
    } else {
      for (i = 0; i < 24; i++) {
        out += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
      }
    }
    return out;
  }

  function number(id) {
    var value = $(id).value.trim();
    return value === '' ? null : Number(value);
  }

  function collect() {
    var payload = {
      server_name: $('server_name').value.trim(),
      host: $('host').value.trim(),
      media_dirs: readDirs(),
      allow_hevc_direct: $('allow_hevc_direct').checked,
      ffmpeg_path: $('ffmpeg_path').value.trim(),
      ffprobe_path: $('ffprobe_path').value.trim(),
      admin_token: $('admin_token').value.trim(),
      transcode: {
        preset: $('tc_preset').value,
        resolution: $('tc_resolution').value.trim(),
        max_video_bitrate: $('tc_max_video_bitrate').value.trim(),
        audio_bitrate: $('tc_audio_bitrate').value.trim()
      }
    };

    var numeric = {
      port: 'port',
      scan_interval: 'scan_interval',
      thumbnail_workers: 'thumbnail_workers',
      stream_buffer_mb: 'stream_buffer_mb'
    };
    for (var key in numeric) {
      if (!numeric.hasOwnProperty(key)) { continue; }
      var value = number(numeric[key]);
      if (value !== null) { payload[key] = value; }
    }

    var crf = number('tc_crf');
    if (crf !== null) { payload.transcode.crf = crf; }
    var concurrent = number('tc_max_concurrent');
    if (concurrent !== null) { payload.transcode.max_concurrent = concurrent; }

    return payload;
  }

  function fillStatus(data) {
    text($('stat-version'), data.version || '—');
    var library = data.library || {};
    text($('stat-videos'), library.count == null ? '—' :
         library.count + (library.scanning ? ' (scanning…)' : ''));
    var ffmpeg = data.ffmpeg || {};
    text($('stat-ffmpeg'), ffmpeg.available ? ffmpeg.ffmpeg_path : 'Not found');
    text($('stat-ffprobe'), ffmpeg.can_probe ? ffmpeg.ffprobe_path : 'Not found');
    text($('stat-settings'), data.settings_file || '—');
    if (!ffmpeg.available) {
      showBanner('ffmpeg was not found. Only browser-native files will play, ' +
                 'and thumbnails and embedded subtitles are unavailable.', 'warn');
    }
  }

  // -- directory picker ----------------------------------------------------

  var picker = { current: '', target: null };

  function openPicker(row) {
    picker.target = row;
    $('picker').hidden = false;
    var start = row ? row.querySelector('.dir-path').value.trim() : '';
    loadPicker(start);
  }

  function loadPicker(path) {
    var url = '/api/admin/browse';
    if (path) { url += '?path=' + encodeURIComponent(path); }
    var token = adminToken();
    if (token) { url += (path ? '&' : '?') + 'token=' + encodeURIComponent(token); }

    request('GET', url).then(function (result) {
      if (!result.ok) {
        // Most likely the folder vanished; fall back to the drive list.
        if (path) { loadPicker(''); }
        return;
      }
      picker.current = result.data.path || '';
      text($('picker-path'), picker.current || 'Select a drive');
      var list = $('picker-list');
      clear(list);

      if (result.data.parent) {
        appendPickerItem(list, '⬑ ' + result.data.parent, result.data.parent);
      }
      var entries = result.data.entries || [];
      for (var i = 0; i < entries.length; i++) {
        appendPickerItem(list, entries[i].name, entries[i].path);
      }
      $('picker-choose').disabled = !picker.current;
    });
  }

  function appendPickerItem(list, label, path) {
    var fragment = $('tpl-picker-item').content.cloneNode(true);
    var button = fragment.querySelector('.picker-item');
    button.appendChild(document.createTextNode(label));
    button.addEventListener('click', function () { loadPicker(path); });
    list.appendChild(fragment);
  }

  function closePicker() {
    $('picker').hidden = true;
    picker.target = null;
  }

  // -- load and save -------------------------------------------------------

  function load() {
    return request('GET', API + tokenQuery()).then(function (result) {
      if (result.status === 403) {
        $('main').hidden = true;
        $('denied').hidden = false;
        text($('denied-message'), result.data.error || 'Access denied.');
        return;
      }
      if (!result.ok) {
        showBanner('Could not load settings.', 'error');
        return;
      }
      state.contentTypes = result.data.content_types || ['mixed'];
      state.presets = result.data.presets || [];
      state.restartFields = result.data.restart_required_fields || [];
      state.localIp = result.data.local_ip || '';
      $('main').hidden = false;
      $('denied').hidden = true;
      fillStatus(result.data);
      fillForm(result.data.settings || {});
      setStatus('');
    });
  }

  function tokenQuery() {
    var token = adminToken();
    return token ? '?token=' + encodeURIComponent(token) : '';
  }

  function save(event) {
    event.preventDefault();
    setStatus('Saving…');
    showBanner('');
    $('save').disabled = true;

    request('POST', API + tokenQuery(), collect()).then(function (result) {
      $('save').disabled = false;
      if (!result.ok) {
        var errors = result.data.errors || [result.data.error || 'Save failed.'];
        showBanner(errors.join(' · '), 'error');
        setStatus('Not saved', 'error');
        return;
      }
      fillForm(result.data.settings || {});
      var notes = [];
      if (result.data.warnings && result.data.warnings.length) {
        notes = notes.concat(result.data.warnings);
      }
      if (result.data.restart_required && result.data.restart_required.length) {
        notes.push('Restart LiteJelly to apply: ' +
                   result.data.restart_required.join(', ') + '.');
      }
      showBanner(notes.join(' '), notes.length ? 'warn' : '');
      setStatus('Saved', 'ok');
    });
  }

  function init() {
    initTabs();
    $('form').addEventListener('submit', save);
    $('reload').addEventListener('click', function () { load(); });
    $('add-dir').addEventListener('click', function () { addDirRow(); });
    $('browse').addEventListener('click', function () {
      var rows = $('dirs').querySelectorAll('.dir');
      openPicker(rows.length ? rows[rows.length - 1] : addDirRow());
    });
    $('picker-cancel').addEventListener('click', closePicker);
    $('picker-choose').addEventListener('click', function () {
      if (picker.target && picker.current) {
        picker.target.querySelector('.dir-path').value = picker.current;
      }
      closePicker();
    });
    $('gen-token').addEventListener('click', function () {
      $('admin_token').value = generateToken();
      updateRemoteUrl();
    });
    $('admin_token').addEventListener('input', updateRemoteUrl);
    $('port').addEventListener('input', function () {
      state.port = Number($('port').value) || state.port;
      updateRemoteUrl();
    });
    $('rescan').addEventListener('click', function () {
      var node = $('rescan-status');
      node.className = 'save-status';
      text(node, 'Rescanning…');
      request('POST', '/api/rescan', {}).then(function () {
        window.setTimeout(function () {
          load().then(function () {
            node.className = 'save-status ok';
            text(node, 'Done');
          });
        }, 1200);
      });
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !$('picker').hidden) { closePicker(); }
    });
    load();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
