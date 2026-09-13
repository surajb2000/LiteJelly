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
- Always wrap subprocess lifecycles in `try ... finally:` blocks to guarantee termination (`process.terminate()` -> `process.wait(timeout=0.5)` -> `process.kill()`).
- Use the threaded `ReadAhead` ring buffer when streaming process `stdout` to avoid blocking FFmpeg when the network client pauses or buffers.
- Fast input seeking (`-ss` before `-i`) must be used for streaming transcodes.
- Pass the requested seek time unchanged. Snapping it onto a keyframe makes FFmpeg rewind to the previous one, and an open-GOP keyframe list cannot be trusted as entry points.
- Add `-noaccurate_seek` whenever the video is stream-copied. Accurate seek trims audio to the exact timestamp while copied video starts at a keyframe, which splits them by up to a whole GOP.

### 5. Database Conventions (`litejelly.store`)
- SQLite must always run with `PRAGMA journal_mode=WAL` for concurrent read/write support across threads.
- SQLite connections across threads must use `check_same_thread=False` accompanied by explicit threading locks.
- Store database tables in `.cache/litejelly.db` (gitignored).

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

### 4. Media Player & Web APIs
- Seamlessly handle browser media events: `timeupdate`, `loadedmetadata`, `play`, `pause`, `ended`, `error`.
- Use the Screen Wake Lock API (`navigator.wakeLock.request('screen')`) during active playback and release on exit or pause.
- Catch and handle auto-play restrictions gracefully (prompts user with a "Press Play to start" toast).

---

## 🧪 Testing & Verification

- Every new core feature or parser modification should include corresponding unit tests in `tests/test_litejelly.py`.
- Run tests locally before committing:
  ```bash
  python -m unittest discover -s tests
  ```
- Ensure all 36+ existing tests pass with zero regressions.
- For A/V synchronization testing, utilize `tools/avsync_probe.py` to measure clock drift against test clips.
