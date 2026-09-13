# 🛠️ LiteJelly Coding Guidelines & Architecture Principles

This document outlines the architectural standards, code quality conventions, and security rules for contributing to **LiteJelly**.

---

## 🧭 Core Engineering Philosophy

1. **Zero Runtime Dependencies**:
   - The LiteJelly server must run directly on any standard Python 3.10+ installation with **zero pip dependencies**.
   - Do not add packages to `requirements.txt` for server operation. Everything must use Python's rich standard library (`http.server`, `sqlite3`, `subprocess`, `threading`, `dataclasses`, `pathlib`, etc.).
2. **Zero Frontend Build Steps**:
   - The frontend must remain 100% vanilla HTML5, modern CSS3, and ES6+ JavaScript.
   - No `npm`, `node_modules`, Webpack, Vite, or frontend frameworks (React, Vue, etc.).
   - Total frontend bundle payload must remain under 100 KB uncompressed.
3. **Low-Spec Device First (10-Foot UI)**:
   - Always optimize for budget smart TVs (such as 32-inch 720p screens with 512 MB – 1 GB RAM running Cloud TV Lite or older Android TV).
   - Every interactive element must be fully usable via TV remote D-pad (Arrow keys + Enter + Back).
   - Avoid heavy DOM reflows, complex CSS filters, or memory-heavy animations that drop frames on low-end ARM processors.

---

## 🐍 Backend Architecture Guidelines (`litejelly/`)

### 1. Python Standards & Typing
- Target **Python 3.10+**.
- Always include `from __future__ import annotations` at the top of every module.
- Use explicit type hints for function signatures and data structures.
- Prefer `@dataclass` for data transfer objects and configuration models (`Video`, `MediaInfo`, `PlaybackPlan`, `SubtitleTrack`).

### 2. Filesystem Security & Path Traversal
- **CRITICAL**: Never pass user-supplied paths directly to `open()`, `os.path.join()`, or subprocess commands.
- All request parameters (`file`, `id`, `track`, etc.) must be strictly validated through `litejelly.paths.resolve_within` or `Application.resolve_video()`.
- Realpaths and symlinks must be resolved before containment checks to prevent symlink-based filesystem escapes.
- Reject paths containing `\x00` (null bytes), `..` components, or drive specifications.

### 3. Concurrency & Thread Safety
- LiteJelly runs on a multithreaded server model (`http.server.ThreadingHTTPServer` with `daemon_threads = True`).
- All shared resources (`Library._videos`, `ProgressStore._conn`, `ThumbnailService._inflight`, `FFmpegTools._probe_cache`) must be synchronized using `threading.Lock()` or `threading.RLock()`.
- **Single-Flight Pattern**: For expensive operations (e.g., thumbnail extraction), use `threading.Event` so multiple simultaneous requests for the same item wait for a single worker rather than launching duplicate FFmpeg processes.
- **Bounded Resource Semaphores**: Limit concurrent heavy subprocesses (transcoding, thumbnails) using `threading.BoundedSemaphore` to avoid exhausting CPU/RAM.

### 4. Subprocess & FFmpeg Management
- Always specify `creationflags = subprocess.CREATE_NO_WINDOW` on Windows (`os.name == 'nt'`) so console windows do not flash during background probes or transcodes.
- Never let a child inherit stdin. ffmpeg switches the controlling terminal to no-echo so it can read its interactive keys, and killing the server first leaves the shell needing a manual `stty echo`. Pass `stdin=subprocess.DEVNULL` and `-nostdin`.
- Always wrap subprocess lifecycles in `try ... finally:` blocks to guarantee termination (`process.terminate()` -> `process.wait(timeout=0.5)` -> `process.kill()`).
- Use the threaded `ReadAhead` ring buffer when streaming process `stdout` to avoid blocking FFmpeg when the network client pauses or buffers.
- Fast input seeking (`-ss` before `-i`) must be used for streaming transcodes.
- Pass the requested seek time unchanged. Snapping it onto a keyframe makes FFmpeg rewind to the previous one, and an open-GOP keyframe list cannot be trusted as entry points.
- Add `-noaccurate_seek` whenever the video is stream-copied. Accurate seek trims audio to the exact timestamp while copied video starts at a keyframe, which splits them by up to a whole GOP.

### 5. Database Conventions (`litejelly.store`)
- SQLite must always run with `PRAGMA journal_mode=WAL` for concurrent read/write support across threads.
- SQLite connections across threads must use `check_same_thread=False` accompanied by explicit threading locks.
- Store database tables in `.cache/litejelly.db` (gitignored).

### 6. The Admin Trust Boundary
- The library API is intentionally unauthenticated so any TV on the LAN can browse it. The admin API is **not**, and the two must never be blurred.
- Anything that changes `media_dirs` changes which files the server will hand out. Treat every admin write as equivalent to granting filesystem access.
- Guard every admin route with `RequestHandler.require_admin()`. It requires a valid session and, on writes, a same-origin request.
- Never store or log a password. `litejelly.auth` hashes with PBKDF2 and a per-password salt; compare with `hmac.compare_digest`, never `==`.
- Verify the password once at sign-in and carry the result in a session. Hashing is deliberately slow, so doing it per request would make every page load expensive and turn the login endpoint into a CPU exhaustion vector.
- First-account creation is loopback-only. Allowing it over the network makes ownership a race between the owner and anyone else who can reach the port.
- Credentials live in `credentials.json`, never in `settings.json`, which is served to the admin page.
- `Config.to_public_dict()` feeds the unauthenticated `/api/config`. Never add filesystem paths, binary locations, credentials or bind addresses to it; those belong in `to_admin_dict()`.
- Validate admin input in `litejelly.settings.validate()`, not in the route. Unknown keys are ignored and enumerated values (content types, presets) are whitelisted rather than pattern-matched.
- Persist user settings to `settings.json` beside `config.json`. Never write them into `.cache/`, which is disposable and safe to delete.

### 7. Request Bodies and Keep-Alive
- If a request is rejected before its body is read, the unread bytes stay queued on the socket and the next keep-alive request parses them as a request line. `send_api_error()` drains the body for this reason; leave that call in place when adding error paths.

### 8. Applying Configuration at Runtime
- `FFmpegTools`, `SubtitleService` and `ThumbnailService` capture values from the config when constructed. Replacing `Application.config` alone leaves them pointing at the old binaries and limits, so rebuild them together in `Application.apply_config()`.
- `Library` owns its directory list. Change it through `Library.set_media_dirs()` so the lock is held, the scan fingerprint is cleared, and any scan already in flight is discarded rather than publishing stale `dir_index` values.
- `port` and `host` cannot be rebound on a live server. They are saved and reported through `settings.RESTART_REQUIRED` instead of being applied.

### 9. Logging
- Get a logger with `logging.getLogger("litejelly.<module>")`. Handlers are installed once by `litejelly.logs.configure()`; never call `basicConfig` or add handlers elsewhere.
- Pick the level by who needs the message: `info` for things an operator cares about, `warning`/`error` for problems, `debug` for diagnosing a specific fault, `trace` for per-chunk or per-frame detail.
- INFO is the floor for what gets recorded. Do not add a setting that can hide warnings or errors; "quiet" belongs in the log *viewer's* filter, not in capture.
- The console is quiet by default but always shows warnings and errors. Anything a user would need to see when something breaks must be at least `warning`.
- When an external tool fails, log its stderr. "Could not make a thumbnail" without ffmpeg's reason is not actionable from the admin page.
- Use lazy formatting (`log.info("Indexed %d", count)`), not f-strings, so suppressed records cost nothing.
- Anything polled by the admin page should be excluded in `log_message`, or the log fills with requests for the log.

### 10. Work That Blocks a Request
- A TV browser opens only a handful of connections. Never do slow work inside a request handler: generating thumbnails there held those connections behind a worker semaphore and starved the page.
- Queue the work, answer `202` immediately, and let the client retry. Give up after a bounded number of failures so a file that can never succeed is not retried on every page load.

---

## 🌐 Frontend Architecture Guidelines (`static/`)

### 1. JavaScript Conventions (`app.js`)
- Enclose all client code inside an Immediately Invoked Function Expression (IIFE) with `'use strict';` to prevent polluting the global window scope.
- Maintain legacy Android TV browser compatibility (Chromium 60–80 era). Include polyfills if using newer Web APIs (`replaceChildren`, `padStart`, `Element.remove`).
- Use `requestAnimationFrame` for DOM measurement and grid column layout calculations.
- Debounce window resize events and search input queries.

### 2. XSS & Template Security
- **Never use `innerHTML` with unsanitized dynamic data** (such as filenames or metadata).
- Render cards by cloning the semantic `<template id="card-template">` and setting content exclusively via `.textContent` or `.setAttribute()`.

### 3. TV Remote Focus & Navigation
- All interactive components must be native `<button>` or have `tabindex="0"` and an appropriate ARIA role.
- Maintain spatial navigation index math (`focusIndex + 1`, `focusIndex + gridColumns`) in `handleLibraryKeys`.
- When focus updates, always invoke `scrollIntoView({ block: 'nearest' })`. Do not use `behavior: 'smooth'` or `block: 'center'` during D-pad repeats; both cause visible jank on low-powered TV browsers.
- Ensure high-contrast focus outlines (neon cyan `--accent` border + glow) are defined in CSS for both `:focus` and `.focused` classes.

### 3a. IntersectionObserver
- Observe an element that is certain to have a layout box, such as the card. An element with no box is never reported, and the symptom is silent: the feature simply never runs.
- This has bitten twice. First with the `hidden` attribute, then with an absolutely positioned image inside a padding-ratio box whose percentage height resolved to zero.
- Anything driven by the observer needs a fallback path, because a lazy-loading optimisation that silently never fires looks identical to a broken feature.

### 4. Media Player & Web APIs
- Seamlessly handle browser media events: `timeupdate`, `loadedmetadata`, `play`, `pause`, `ended`, `error`.
- Use the Screen Wake Lock API (`navigator.wakeLock.request('screen')`) during active playback and release on exit or pause.
- Catch and handle auto-play restrictions gracefully (prompts user with a "Press Play to start" toast).

---

## 🧪 Testing & Verification

- Every new core feature or parser modification should include corresponding unit tests, added in the same change rather than deferred.
  - `tests/test_litejelly.py` — path containment, range parsing, subtitles, title parsing.
  - `tests/test_admin.py` — settings validation, admin access control, library reconfiguration.
  - `tests/test_avsync.py` — regression tests for the seeking and A/V sync fixes.
- Bugs found by measurement get a test that pins the measured behaviour, with the measurement recorded in the docstring. The A/V desync cost days to diagnose; the tests exist so it cannot return silently.
- Run tests locally before committing:
  ```bash
  python -m unittest discover -s tests
  ```
- Ensure all existing tests pass with zero regressions.
- For A/V synchronization testing, utilize `tools/avsync_probe.py` to measure clock drift against test clips. Its beep-onset detection is not yet reliable at sub-100 ms resolution; prefer packet and timestamp measurements for small offsets.
