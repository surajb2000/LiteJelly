# 🛠️ LiteJelly Coding Guidelines & Architecture Principles

This document outlines the architectural standards, code quality conventions, and security rules for contributing to **LiteJelly**.

---

## 🧭 Core Engineering Philosophy

1. **Zero Runtime Dependencies**:
   - The LiteJelly server must run directly on any standard Python 3.10+ installation with **zero pip dependencies**.
   - Do not add packages to `requirements.txt` for server operation. Everything must use Python's rich standard library (`http.server`, `sqlite3`, `subprocess`, `threading`, `dataclasses`, `pathlib`, etc.).
   - Optional browser-test dependencies live in `tools/requirements-browser.txt`, separate from runtime code and the default tests. Before installing any tool, package, browser or virtual environment on a developer's machine, stop, summarize the need, and obtain explicit approval.
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

### Module ownership

- `web.py` owns application lifecycle, route registration, HTTP validation,
   authorization and response orchestration. Resolve paths and validate finite
   media times here before passing requests into playback or streaming code.
- `admin_routes.py` holds every admin and account handler. Each guarded handler
   calls `require_admin` first, and writes pass `write=True`; keep new admin
   routes here rather than among the open library routes in `web.py`.
- `playback.py` builds request-local selections, response details, seek results
   and stream commands. It must not write a viewer's choices into shared
   `MediaInfo`, start live streams, or depend on HTTP handler globals.
- `ffmpeg.py` owns codec policy, file-signature probe caching and process launch
   primitives. Same-file probe sharing is metadata reuse, never stream reuse.
- `streaming.py` owns range parsing, private file handles, `ReadAhead` and the
   process pump. It writes through the `ResponseSink` protocol (`begin_response`,
   `write_body`), never a handler's private members. Every acquired stream slot
   must be released on success, disconnect, failed spawn or rejected
   registration during shutdown.
- Compatibility imports for `parse_range`, `ReadAhead` and the audio helpers
   remain in `web.py`; do not remove them as unused imports without migrating
   their callers. New tests should patch the owning playback/streaming module.
- Saved progress remains per video, not per screen. Changing that model requires
   an explicit profile/identity design and migration, not a playback-cache tweak.

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
- All shared resources (`Library._videos`, `ProgressStore._conn`, `ThumbnailService._pending`, `FFmpegTools._probe_cache`, `_pending_probes` and `_keyframe_cache`, `SessionStore`, `LoginThrottle`) must be synchronized using `threading.Lock()` or `threading.RLock()`.
- **Single-Flight Pattern**: For expensive operations (e.g., probing or thumbnail extraction), share a `threading.Event` or `Future` so simultaneous requests for the same item wait for one worker. Share failures as well as results, release waiters on shutdown, and keep different media files independent.
- Preserve `CapacityLimiter` across settings reloads: reducing its limit must not forget occupied slots. Thumbnail/trickplay worker counts and the HTTP connection limit are separate bounds, not a global CPU scheduler.
- Closing a worker service must reject new jobs, drain queued work and avoid blocking sentinel writes to a full queue. In-flight network I/O can finish under its timeout, but stopped services must not continue queuing follow-up work.

### 4. Subprocess & FFmpeg Management
- Always specify `creationflags = subprocess.CREATE_NO_WINDOW` on Windows (`os.name == 'nt'`) so console windows do not flash during background probes or transcodes.
- Never let a child inherit stdin. ffmpeg switches the controlling terminal to no-echo so it can read its interactive keys, and killing the server first leaves the shell needing a manual `stty echo`. Pass `stdin=subprocess.DEVNULL` and `-nostdin`.
- Always wrap subprocess lifecycles in `try ... finally:` blocks to guarantee termination. Stream cleanup uses `streaming.terminate_process`: terminate, wait up to two seconds, then kill and reap if necessary.
- Pass the owning service's cancellation event to `run_quiet` for background media work. Cancellation must kill and reap the child, and partially generated images must never become cache hits.
- Thumbnail and trickplay commands limit decoder, filter and encoder threads to one each. Keep `-threads 1` both before and after `-i`; these are separate codec contexts. This bounds those FFmpeg stages, not all process threads or total machine CPU usage, and does not change live playback encoding.
- Use the bounded deque-backed `streaming.ReadAhead` queue for process `stdout`. It provides a limited reserve; once full, backpressure still reaches FFmpeg.
- Fast input seeking (`-ss` before `-i`) must be used for streaming transcodes.
- Pass the requested seek time unchanged. Snapping it onto a keyframe makes FFmpeg rewind to the previous one, and an open-GOP keyframe list cannot be trusted as entry points.
- Add `-noaccurate_seek` whenever the video is stream-copied. Accurate seek trims audio to the exact timestamp while copied video starts at a keyframe, which splits them by up to a whole GOP.

### 5. Database Conventions (`litejelly.store`)
- SQLite must always run with `PRAGMA journal_mode=WAL` for concurrent read/write support across threads.
- SQLite connections across threads must use `check_same_thread=False` accompanied by explicit threading locks.
- Durable tables belong in `data/litejelly.db`, not the disposable asset cache. Migrate the legacy database through SQLite backup, retaining its source and committed WAL data.
- Persist media IDs by canonical root and relative path. Never resolve a scanned item's old `dir_index` against a newer directory list. The initial identity catalog retains legacy IDs under the documented unchanged-folder-order upgrade assumption.
- Do not prune progress merely because a drive or file is absent during startup. History deletion requires an explicit retention decision.

### 6. The Admin Trust Boundary
- The library API is intentionally unauthenticated so any TV on the LAN can browse it. The admin API is **not**, and the two must never be blurred.
- Anything that changes `media_dirs` changes which files the server will hand out. Treat every admin write as equivalent to granting filesystem access.
- Guard every admin route with `RequestHandler.require_admin()`. It requires a valid session and, on writes, a same-origin request.
- Never store an admin password in plaintext or log credentials. `litejelly.auth` hashes with PBKDF2 and a per-password salt; compare with `hmac.compare_digest`, never `==`. The existing OpenSubtitles account file is a separate server-side credential store and is never included in settings exports.
- Verify the password once at sign-in and carry the result in a session. Hashing is deliberately slow, so doing it per request would make every page load expensive and turn the login endpoint into a CPU exhaustion vector.
- First-account creation is loopback-only. Allowing it over the network makes ownership a race between the owner and anyone else who can reach the port. A `credentials.json` that exists but cannot be read keeps setup closed until `--reset-admin` repairs it.
- Credentials live in `credentials.json`, never in `settings.json`, which is served to the admin page.
- `Config.to_public_dict()` feeds the unauthenticated `/api/config`. Never add filesystem paths, binary locations, credentials or bind addresses to it; those belong in `to_admin_dict()`.
- Validate admin input in `litejelly.settings.validate()`, not in the route. Unknown keys are ignored and enumerated values (content types, presets) are whitelisted rather than pattern-matched.
- Persist user settings to `settings.json` beside `config.json`. Never write them into `.cache/`, which is disposable and safe to delete.
- Sidecar discovery must enforce containment as well as media lookup. Uploaded files use unique temporary outputs and exclusive final-name reservations; checking `exists()` before replacing a file is not a no-overwrite guarantee.
- Use `litejelly.net.open_remote` for outbound provider requests. Validate every redirect, require public resolved addresses, pin the chosen address for connection, and retain TLS hostname checks. Do not forward account credentials across hosts or restore environment-proxy behavior without an explicit security design.

### 7. Request Bodies and Keep-Alive
- If a request is rejected before its body is read, the unread bytes stay queued on the socket and the next keep-alive request parses them as a request line. `send_api_error()` drains the body for this reason; leave that call in place when adding error paths.
- Reject unsupported transfer framing and duplicate lengths before dispatch. Maintain both connection limits and socket idle timeouts; an accept backlog is not a thread limit.

### 8. Applying Configuration at Runtime
- Build every replacement service before changing live state. If any build fails, close what was built and leave the running services, stream limit and `settings.json` as they were (`_save_and_apply` restores the previous file).
- `FFmpegTools`, `SubtitleService` and `ThumbnailService` capture configuration values. Rebuild derived services together, cancel the retired generation, retain the shared stream limiter and restore the enrichment completion callback.
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
- Continue Watching builds request-local series successors once per needed series. Do not scan and sort the whole library for every finished episode, or add a persistent cache without progress and library invalidation rules.

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

### Local commit hooks

The versioned `.githooks/pre-commit` hook runs `tools/pre_commit.py` using Git
and Python 3.10+ only. It checks the staged index, not unstaged working files:

- Whitespace errors and merge-conflict markers reported by `git diff --cached --check`.
- Python compilation without execution or bytecode output, and valid JSON without NaN/Infinity.
- Accidental credentials, environment files, generated data, database files and bundled executables.
- Newly added files larger than 5 MiB, checked before reading their contents.

The path checks are a guard against accidental commits, not a full secret
scanner. Python syntax is checked by the selected interpreter; the supported
Python-version matrix remains a CI responsibility. JavaScript syntax,
cross-file behavior, browser journeys and mutation checks remain test/CI gates.

Hooks never format, rewrite or stage files automatically. This preserves
partial staging. Fix the reported problem, review the diff and stage only
the intended changes before retrying the commit.

Adding these files does not activate them. After approval, enable them for
this clone with:
```bash
git config --local core.hooksPath .githooks
```
On Unix or in Git Bash, also run `chmod +x .githooks/pre-commit`. Record its
executable mode when committing the hook with
`git update-index --chmod=+x .githooks/pre-commit` after staging it. Git does
not automatically enable versioned hooks in new clones. Check for an existing
`core.hooksPath` before replacing it. `LITEJELLY_PYTHON` can select an existing
interpreter explicitly; otherwise the launcher tries `python3`, then `python`.

Run the same staged checks manually without activation:
```bash
python tools/pre_commit.py
```

Recommended follow-ups, subject to explicit tool-installation approval:

- **Formatting:** Ruff for Python; optionally Prettier for JavaScript/CSS/HTML/Markdown.
   Prefer check-only hooks and explicit formatting before staging. Introduce
   formatting in a separate reviewed commit, since some existing tests compare
   source strings and may need behavior-based replacements first.
- **Linting:** a small Ruff rule set, expanded after the existing baseline is reviewed.
- **Pre-push/CI:** the full unit suite, mutation checks and optional browser journeys;
   these are too expensive to run on every commit.
- **Secret scanning:** a dedicated scanner in CI after approval, beyond the local path guard.

No formatter, hook manager, package or browser is installed by these commands.

### Regression suites

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
- Mutation checks run through `tools/mutation_runner.py` in a temporary copy. Require a clean baseline and explicit assertion failures; test errors, missing targets, survivors and timeouts are failures of the check, not successful detections. Never infer success from stderr text or mutate a developer's working files in place.
- Run the optional browser suite with `python -m unittest discover -s tests/browser -v` only in an environment where its tooling is already installed. API and media-clock fixtures make ordering deterministic; use observable state or events rather than fixed sleeps. Traces and screenshots belong in the ignored `test-results/browser/` directory.
- Keep the distinction between structural checks, simulated browser journeys and real-media verification explicit. Headless geometry tests cannot prove hardware-overlay behavior or A/V synchronization on a TV.
- Multi-screen HTTP regressions use real sockets and controlled child processes,
  not real codec playback. Keep distinct byte content in range fixtures so a
  wrong seek cannot pass unnoticed. Cover disconnect/seek isolation, busy slots,
  HEAD requests and failed-spawn cleanup. `tools/mutate_safety.ps1` includes
  negative controls for range offsets, stream offsets and playback audio trim.
- Give new helper functions concise purpose docstrings. Document time units, state changes, failure behavior and cancellation where applicable; keep incident histories in regression tests instead of narrating implementation lines.
- For A/V synchronization testing, utilize `tools/avsync_probe.py` to measure clock drift against test clips. Its beep-onset detection is not yet reliable at sub-100 ms resolution; prefer packet and timestamp measurements for small offsets.

### Performance regression checks

- `tests/test_lifecycle.py` checks shared cold probes, independent-file concurrency,
   cache invalidation, cancellation and the existing deque-based stream buffer.
   Three overlapping same-file requests must launch one probe, not three.
- `tests/test_nextup.py` checks unchanged Continue Watching selection and one
   ordering pass per series. Four finished episodes must need four ordering-key
   evaluations, not sixteen.
- `tests/test_thumbnails.py` and `tests/test_trickplay.py` check thread limits
   on every preview fallback path. Real FFmpeg also produced both preview types
   successfully with these flags during local verification.
- A local single-run comparison against `bf0f3ba` on 2026-10-01 used 10,000
   fully watched synthetic episodes across 100 series: Continue Watching took
   7.80 seconds before and 0.011 seconds after, with identical output. This is
   a specific worst-case history workload, not a general CPU or TV benchmark.
- `tools/measure_library.py [count ...]` builds throwaway libraries and reports
   scan time, the per-scan change check, and `/api/library` size and latency
   with and without gzip. Measure with it before changing library or listing
   code. On 2026-10-01 at 10,000 files (local SSD, loopback): scan 4.2 s, change
   check 0.5 s, 5,436 KB of JSON (322 ms) or 442 KB gzipped (106 ms).
- `tests/test_performance.py` pins the three fixes that followed by counting
   work, not timing it: subtitle discovery resolves only candidate names (a film
   in a 3,334-file folder took 2,529 ms before, 62 ms after), the public listing
   is built once per scan, and JSON of 8 KB or more is gzipped when accepted.
   `tools/mutate_performance.ps1` is the matching negative-control gate.
