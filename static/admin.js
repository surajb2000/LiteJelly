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
    if (panelId === 'panel-logs') { loadLogs(); }
    setAutoRefresh(panelId === 'panel-logs' && $('log-auto').checked);
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

  // Auth rides on an HttpOnly session cookie, so there is nothing to keep here.
  function request(method, url, body) {
    var headers = { 'X-LiteJelly-Admin': '1' };
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
    $('log_verbosity').value = settings.log_verbosity || 'info';
    $('log_to_file').checked = settings.log_to_file !== false;
    $('log_to_console').checked = settings.log_to_console === true;
    $('log_max_mb').value = settings.log_max_mb == null ? '' : settings.log_max_mb;
    $('log_backups').value = settings.log_backups == null ? '' : settings.log_backups;

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
      log_verbosity: $('log_verbosity').value,
      log_to_file: $('log_to_file').checked,
      log_to_console: $('log_to_console').checked,
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
      stream_buffer_mb: 'stream_buffer_mb',
      log_max_mb: 'log_max_mb',
      log_backups: 'log_backups'
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

  // -- logs ----------------------------------------------------------------

  var logTimer = null;

  function setAutoRefresh(on) {
    if (logTimer) {
      window.clearInterval(logTimer);
      logTimer = null;
    }
    if (on) { logTimer = window.setInterval(loadLogs, 4000); }
  }

  function humanBytes(bytes) {
    if (!bytes) { return '0 B'; }
    var units = ['B', 'KB', 'MB', 'GB'];
    var value = bytes;
    var unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return value.toFixed(unit ? 1 : 0) + ' ' + units[unit];
  }

  function loadLogs() {
    var url = '/api/admin/logs?lines=' + encodeURIComponent($('log-lines').value) +
              '&level=' + encodeURIComponent($('log-filter').value);
    return request('GET', url).then(function (result) {
      if (!result.ok) { return; }
      renderLogs(result.data);
    });
  }

  function renderLogs(data) {
    var view = $('log-view');
    var atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
    clear(view);

    var entries = data.entries || [];
    if (!entries.length) {
      var empty = document.createElement('span');
      empty.className = 'log-empty';
      var message;
      if (data.to_file === false) {
        message = 'Logging to a file is turned off.';
      } else if ($('log-filter').value) {
        message = 'No lines match this filter.';
      } else {
        message = 'Nothing recorded yet.';
      }
      empty.appendChild(document.createTextNode(message));
      view.appendChild(empty);
    } else {
      for (var i = 0; i < entries.length; i++) {
        view.appendChild(logLine(entries[i]));
      }
    }

    var meta = [];
    if (data.file) { meta.push(data.file); }
    if (data.exists) { meta.push(humanBytes(data.size)); }
    meta.push(entries.length + (entries.length === 1 ? ' line' : ' lines') + ' shown');
    text($('log-meta'), meta.join(' \u00b7 '));
    text($('log-path'), data.file
      ? 'Written to ' + data.file + ' in the ' + (data.folder || 'logs') +
        ' folder beside server.py.'
      : '');

    if (atBottom) { view.scrollTop = view.scrollHeight; }
  }

  function logLine(entry) {
    var row = document.createElement('span');
    row.className = 'log-line' + (entry.level ? ' lvl-' + entry.level.toLowerCase() : '');
    // textContent throughout: log messages contain filenames from disk.
    var parts = [];
    if (entry.time) { parts.push(entry.time); }
    if (entry.level) { parts.push(pad(entry.level, 7)); }
    if (entry.logger) { parts.push(entry.logger + ':'); }
    parts.push(entry.message);
    row.textContent = parts.join(' ');
    return row;
  }

  function pad(text, width) {
    var out = String(text);
    while (out.length < width) { out += ' '; }
    return out;
  }

  function clearLogs() {
    if (!window.confirm('Clear the log file?')) { return; }
    request('POST', '/api/admin/logs/clear', {}).then(function () {
      loadLogs();
    });
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

  // -- session -------------------------------------------------------------

  function showGate(which) {
    $('gate').hidden = false;
    $('main').hidden = true;
    $('who').hidden = true;
    $('sign-out').hidden = true;
    $('setup-form').hidden = which !== 'setup';
    $('setup-remote').hidden = which !== 'setup-remote';
    $('login-form').hidden = which !== 'login';
    var focus = which === 'setup' ? 'setup-username'
              : which === 'login' ? 'login-username' : null;
    if (focus) { $(focus).focus(); }
  }

  function showSettings(username) {
    $('gate').hidden = true;
    $('main').hidden = false;
    text($('who'), username || '');
    $('who').hidden = !username;
    $('sign-out').hidden = false;
    $('acct-username').value = username || '';
  }

  function refreshSession() {
    return request('GET', '/api/admin/session').then(function (result) {
      var data = result.data || {};
      if (data.state === 'setup') {
        if (data.min_password_length) {
          text($('setup-rule'), 'At least ' + data.min_password_length + ' characters.');
        }
        showGate(data.can_set_up_here ? 'setup' : 'setup-remote');
        return false;
      }
      if (data.state === 'login') {
        showGate('login');
        if (data.locked_seconds > 0) {
          gateError('login-error', 'Too many attempts. Try again in ' +
                    (Math.floor(data.locked_seconds / 60) + 1) + ' minutes.');
        }
        return false;
      }
      showSettings(data.username);
      return true;
    });
  }

  function gateError(id, message) {
    var node = $(id);
    if (!message) { node.hidden = true; return; }
    text(node, message);
    node.hidden = false;
  }

  function submitSetup(event) {
    event.preventDefault();
    gateError('setup-error', '');
    var password = $('setup-password').value;
    if (password !== $('setup-confirm').value) {
      gateError('setup-error', 'The two passwords do not match.');
      return;
    }
    request('POST', '/api/admin/setup', {
      username: $('setup-username').value,
      password: password
    }).then(function (result) {
      if (!result.ok) {
        gateError('setup-error', errorText(result, 'Could not create the account.'));
        return;
      }
      $('setup-password').value = '';
      $('setup-confirm').value = '';
      showSettings(result.data.username);
      load();
    });
  }

  function submitLogin(event) {
    event.preventDefault();
    gateError('login-error', '');
    request('POST', '/api/admin/login', {
      username: $('login-username').value,
      password: $('login-password').value
    }).then(function (result) {
      $('login-password').value = '';
      if (!result.ok) {
        var message = errorText(result, 'Sign in failed.');
        if (result.data && result.data.remaining_attempts != null) {
          message += ' ' + result.data.remaining_attempts + ' attempts left.';
        }
        gateError('login-error', message);
        return;
      }
      showSettings(result.data.username);
      load();
    });
  }

  function signOut() {
    request('POST', '/api/admin/logout', {}).then(function () {
      window.location.reload();
    });
  }

  function submitPassword(event) {
    if (event) { event.preventDefault(); }
    var node = $('acct-status');
    node.className = 'save-status';
    var next = $('acct-new').value;
    if (next !== $('acct-confirm').value) {
      node.className = 'save-status error';
      text(node, 'The two passwords do not match.');
      return;
    }
    request('POST', '/api/admin/password', {
      username: $('acct-username').value,
      current_password: $('acct-current').value,
      new_password: next
    }).then(function (result) {
      $('acct-current').value = '';
      $('acct-new').value = '';
      $('acct-confirm').value = '';
      if (!result.ok) {
        node.className = 'save-status error';
        text(node, errorText(result, 'Could not update the account.'));
        return;
      }
      node.className = 'save-status ok';
      text(node, 'Updated');
      showSettings(result.data.username);
    });
  }

  function errorText(result, fallback) {
    var data = result.data || {};
    if (data.errors && data.errors.length) { return data.errors.join(' '); }
    return data.error || fallback;
  }

  // -- load and save -------------------------------------------------------

  function load() {
    return request('GET', API).then(function (result) {
      if (result.status === 401 || result.status === 403) {
        refreshSession();
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
      $('denied').hidden = true;
      fillStatus(result.data);
      fillForm(result.data.settings || {});
      setStatus('');
    });
  }

  function save(event) {
    event.preventDefault();
    setStatus('Saving…');
    showBanner('');
    $('save').disabled = true;

    request('POST', API, collect()).then(function (result) {
      $('save').disabled = false;
      if (result.status === 401) {
        refreshSession();
        return;
      }
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
    $('setup-form').addEventListener('submit', submitSetup);
    $('login-form').addEventListener('submit', submitLogin);
    $('acct-save').addEventListener('click', submitPassword);
    $('sign-out').addEventListener('click', signOut);
    $('log-refresh').addEventListener('click', loadLogs);
    $('log-clear').addEventListener('click', clearLogs);
    $('log-filter').addEventListener('change', loadLogs);
    $('log-lines').addEventListener('change', loadLogs);
    $('log-auto').addEventListener('change', function () {
      setAutoRefresh(this.checked && !$('panel-logs').hidden);
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

    refreshSession().then(function (signedIn) {
      if (signedIn) { load(); }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
