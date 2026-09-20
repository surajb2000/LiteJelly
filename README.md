# 🎬 LiteJelly

**Ultra-lightweight, zero-dependency personal media streaming server tailored for budget smart TVs (Cloud TV Lite, Android TV, webOS) and mobile devices.**

---

## 🌟 Overview

Heavy media servers like Plex, Jellyfin, or Emby often struggle on low-spec hardware (such as 32-inch budget smart TVs with 512 MB – 1 GB RAM). Their browser clients are heavy web applications (>10–30 MB JS bundles) that lag, drop frames, or run out of memory.

**LiteJelly** is built from the ground up to solve this:
- **Zero Dependencies**: Pure Python standard library backend (no `pip install` required).
- **Featherweight Frontend**: 193 KB of HTML, CSS and vanilla JavaScript for the whole player and library, with no frameworks and no build step. A further 445 KB of self-hosted font is fetched once and then cached for a year, so a return visit is the 193 KB alone. For comparison, the clients this replaces ship 10–30 MB of JavaScript before any of it runs.
- **Smart Transcoding & Remuxing**: Offloads codec heavy-lifting (HEVC/x265, AC-3, DTS, 10-bit) to the host server via portable FFmpeg.
- **10-Foot TV Experience**: The interface sizes itself to the screen it is on, judged by what the device can do rather than by what its user agent claims to be.
- **Local Synchronization**: SQLite-backed playback progress (WAL mode) shared instantly across all devices on your local network.

---

## 📐 Architecture & Technology

```mermaid
graph TD
    Client["Browser / TV / Mobile (HTML5 + Vanilla JS)"]
    Server["server.py (Entry Point)"]
    Web["litejelly.web (Application & Routes)"]
    Lib["litejelly.library (Library Scanner & Index)"]
    Store["litejelly.store (SQLite ProgressStore)"]
    FFmpeg["litejelly.ffmpeg (FFmpegTools & PlaybackPlanner)"]
    Subs["litejelly.subtitles (SubtitleService)"]
    Thumbs["litejelly.thumbnails (ThumbnailService)"]
    Buffer["ReadAhead (Buffered Process Pipe)"]

    Client <-->|"HTTP / Range Requests / JSON API"| Web
    Server --> Web
    Web --> Lib
    Web --> Store
    Web --> FFmpeg
    Web --> Subs
    Web --> Thumbs
    Web --> Buffer
    Buffer -->|"Fragmented MP4 Stream (stdout)"| Client
```

### Tech Stack
- **Backend**: Python 3.10+ Standard Library (`http.server`, `sqlite3`, `subprocess`, `threading`, `dataclasses`, `pathlib`).
- **Media Engine**: FFmpeg & FFprobe (auto-detected in app folder or on system `PATH`).
- **Frontend**: Vanilla ES6+ JavaScript (IIFE, template cloning, polyfills for older Android TV browsers), semantic HTML5, and responsive CSS3 glassmorphism.
- **Database**: SQLite with WAL (Write-Ahead Logging) enabled.

---

## 🚀 Key Features

### 1. Intelligent Playback Planning
LiteJelly probes every video before streaming to find the fastest, lowest-overhead playback path:
- **Direct Play**: Browser-native containers (MP4, WebM) with H.264 video and AAC/MP3/Opus audio stream directly with HTTP 206 partial content range requests.
- **Direct Remux (Stream Copy)**: Files with compatible H.264 video but incompatible audio (AC-3, DTS, TrueHD) copy the video stream at 0% CPU usage and re-encode only the audio to stereo AAC.
- **Full Transcode**: Incompatible video codecs (HEVC/H.265, 10-bit, MPEG-2, VC-1) are transcoded on-the-fly to standard 720p/1080p H.264 + AAC in fragmented MP4 chunks.
- **Quality Ladder**: User-selectable qualities (`Auto`, `Original`, `1080p`, `720p`, `480p`, `360p`).
- **Hardware encoding, chosen by measurement**: NVENC, QuickSync, AMF, VAAPI and Media Foundation are offered only if they survive a real timed encode, because `ffmpeg -encoders` cheerfully lists encoders that fail the moment you use them. On this laptop it lists five that do not run. `Auto` times software too and keeps whichever was fastest - which was not always the GPU.

### 2. Accurate Seeking & A/V Sync
- **A real timeline, not a restarted pipe**: A piped fMP4 handed to `<video src>` has no timeline to seek within, so every seek used to restart FFmpeg. The same pipe fed through MediaSource gives the browser absolute timestamps: a seek inside the buffer costs no network request at all, and a distant one costs exactly one. Anything that goes wrong drops back to the plain path for the rest of that playback.
- **Seek Landing Prediction**: Open-GOP encodes (x265's default) flag CRA frames as keyframes even though they are not valid entry points. FFprobe performs the same seek FFmpeg will, so the player is told where the stream *actually* begins rather than where it was asked to begin.
- **Matched Stream Entry**: FFmpeg's accurate seek trims audio to the exact timestamp, but copied video can only start on a keyframe, leaving the two seconds apart. `-noaccurate_seek` is used for stream copies so both begin at the same point; re-encoded video keeps accurate seek.
- **Timing adjuster**: One panel for both streams, a millisecond at a time if you want it. The step size cycles through 1, 10, 50, 250 and 1000 ms, so a small correction and a large one both take a few presses. Subtitles shift instantly in the browser; audio delay is applied by FFmpeg and so takes effect once you stop pressing.
- **Hover readout on the seek bar**: The time under the pointer is shown before you click, rather than after you have landed somewhere else.
- **Picture previews while scrubbing**: One sprite sheet per file, built from keyframes only, so you seek to a shot rather than to a number. Measured at roughly 0.3s of CPU per minute of video - a 24-minute episode costs about seven seconds and 224 KB.
- **Skips land forward, not back**: A stream copy can only begin on a keyframe, so a skip used to land on the keyframe *before* the target and replay what you asked to skip. Measured on a ten-second-GOP file, asking to jump seven seconds returned to the same frame. Skips now take the first verified entry point at or after the target.
- **Two quick changes do not restart the film**: A restart reads the clock, and the clock reads zero while one is in flight - measured at a 61ms window on a fast desktop. A second press inside it used to resume from zero. The target is now held until the new source is playing.

### 3. Sound
- **Audio track selection**: Japanese against an English dub, commentary against the feature. The server selects the track, because browser `audioTracks` support cannot be relied on.
- **Dialogue levelling**: Two presets for films that whisper and then explode. Measured gap between quiet and loud passages: 22 dB untouched, 10.5 dB on Boost, 5.5 dB on Night, at no measurable CPU cost. Single-pass `loudnorm` was tried and rejected - it made quiet dialogue *quieter* at six times the CPU - and `acompressor` clipped.
- **Per-show track memory**: The dub you picked for episode one is the dub for episode two. Choices are remembered per series rather than globally, which is what stopped "subtitles off" on one film from turning them off everywhere. Each menu says whose preference it is holding and offers to drop it.

### 4. Comprehensive Subtitle Engine
- **External Sidecar Subtitles**: Automatic discovery of `.srt`, `.vtt`, `.ass` files in media folders or `subs/` directories.
- **Pure-Python SRT Converter**: Converts SubRip (`.srt`) to WebVTT in-process with multi-encoding fallback (`utf-8-sig`, `utf-16`, `cp1252`, `latin-1`), functioning even if FFmpeg is not installed.
- **Embedded Subtitles**: Automatically extracts embedded text subtitles on-the-fly and caches them as WebVTT.
- **Fetched once, for the whole film**: Cue timings used to be re-based by the server for whatever point the stream had restarted at, which meant refetching the entire subtitle file on every seek — measured at eight fetches for eight seeks. The player's clock is already absolute, so one copy in the file's own timeline now serves the whole playback and a seek costs nothing.
- **Survives a failed fetch**: A `<track>` whose download fails is dead permanently — zero cues, and re-enabling it loads nothing, which is why toggling subtitles off and on could not always bring them back. A failed track is replaced rather than re-enabled, and retried at 2s, 6s and 15s before it says so.
- **Adjustable subtitle delay**: Cues are selected in the browser rather than left to the video element, so the offset can be nudged without refetching anything. Useful when the file itself drifts, which no server-side fix can repair.
- **Add one from the player**: A file with no subtitles, or with the wrong ones, is fixable without reaching the media folder. The Subtitles menu takes an `.srt`, `.vtt` or `.ass` from whatever device you are watching on and keeps it beside the video. It is offered even when the list is empty, which is when it is actually needed.
- **Or fetch one from OpenSubtitles**: Search by title, or by the file's own hash, which matches a release far more reliably than a name does. Results are listed with their release name and download count rather than picked for you, because the top hit is often for a different cut. Needs a free API key and the account it belongs to, set up on the admin page; downloads count against that account's daily quota, so nothing is fetched until you choose one.
- **Neither is taken on trust**: Both paths go through the same check before anything is written. A shell script named `subtitles.srt` is refused, the extension is decided by what the text actually is, the name on disk is built from the video's own path, and an existing file is never overwritten. Adding a subtitle needs an admin session, because it writes into a media folder.
- **Bitmap Burn-in**: Detects image-based subtitles (PGS / VobSub) and offers clean hardware-assisted video burn-in.

### 5. TV Remote & 10-Foot User Interface

- **Device profiles**: The interface sizes itself to the screen it is on. There is no reliable way to ask a browser "am I a television", so LiteJelly asks what actually changes the design: a wide screen that cannot hover is being driven by a remote from across a room. Type, spacing, focus rings and hit targets all scale from that, and a stray mouse movement corrects a wrong guess.
- **D-pad spatial navigation**: Arrow keys move to the nearest control in that direction, measured geometrically. A hero, rails of differing lengths and a grid share no common column count, so counting columns cannot work.
- **Overscan safe area**: Nothing readable or pressable sits in the outer 5% on a television, because many still clip the edge of the picture.
- **Home screen**: A suggestion with full-width artwork, then one rail per category. A flat alphabetical grid is a file browser, not a library. The large slot never repeats what is in Continue watching directly beneath it, and it does not call itself a recommendation, because nothing behind it knows anything about your taste.
- **Search that matches how a title is remembered**: Accents are folded, punctuation becomes a gap and words match in any order, so "pokemon" finds "Pokémon" and "dragon s2e3" finds the episode. `s02e03`, `s2e3` and `2x03` are the same query. A run-together copy of each title is kept so "shield" finds "S.H.I.E.L.D.".
- **Filter by what you have seen**: Unwatched, in progress, finished - and by genre, which comes from metadata already fetched. The old filters sorted by container, which told you about the file rather than about the film.
- **Series pages**: Poster beside backdrop, season tabs, and one row per episode carrying its own still, synopsis, runtime and air date.
- **"Continue Watching" Rail**: One row per series rather than one per episode, showing the next episode once you finish one.
- **Up Next**: The next episode is offered in the corner before the current one ends - at the credits where their position is known, otherwise thirty seconds out - so the picture never goes black first. The episode keeps playing behind it, and dismissing it leaves it dismissed.
- **Mark as watched**: A toggle on every episode row, for the one you watched somewhere else or abandoned halfway.
- **Player controls grouped by purpose**: Previous and next together, picture and sound settings together. Only the two controls that report a value carry a word; the rest are icons, with the chosen subtitle track named on the button and cut to fit.
- **Skip Intro / Skip Credits**: Taken from the file's own chapter markers when it has them, and from a shared database when it does not. The segments are also drawn on the seek bar, so you can see where the intro and the credits are before you reach them.
- **Artwork in its own shape**: Posters are portrait, episode stills are landscape. Forcing both into one box is what cropped posters to a slice and made every episode of a show look identical.
- **Offline typography**: Inter is served from the machine itself. Static weights, not the variable file, because variable fonts need a newer engine than the target television has.

### Metadata and artwork

LiteJelly reads what is already beside your media, with no API key and no
network:

| File | Used for |
| :--- | :--- |
| `Episode.S01E01.nfo` | Episode title, plot, rating, air date, season/episode numbers |
| `poster.jpg`, `folder.jpg`, `cover.jpg` | Card artwork, taken from the episode's folder or the show's |
| `Episode.S01E01-thumb.jpg` | Artwork for that one episode |
| `fanart.jpg`, `backdrop.jpg` | Background artwork |

A `.nfo` overrides what the filename guessed, field by field, so a file that
only contains a plot will not wipe an episode number the filename got right.
Where there is no artwork, a frame from the video is still generated as before.
Plots are fetched per item rather than shipped with the whole library, which
would otherwise dwarf the payload.

### Online metadata (optional)

Turn on **Library → Online metadata** to fill the gaps for media with no `.nfo`
or poster beside it. It is **off by default**, because it sends your show
titles to a third party, which is your decision rather than the default.

| Source | Used for | Account needed |
| :--- | :--- | :--- |
| [TVmaze](https://www.tvmaze.com) | Television: episode names, plots, ratings, posters, IMDb id | No |
| [AniList](https://anilist.co) | Anime: titles, scores, cover art | No |
| [AniSkip](https://www.aniskip.com) | Anime opening and ending times | No |
| [TheIntroDB](https://theintrodb.org) | Intros, recaps and credits for everything else | No |
| [TMDb](https://www.themoviedb.org) | Films, and shows TVmaze does not have | Free key |
| [OMDb](https://www.omdbapi.com) | The actual IMDb rating | Free key |

The first four work out of the box. The last two are the same pair Jellyfin
ships with, and both need a free key, which you paste into **Library → Online
metadata**:

- **TMDb** — [themoviedb.org](https://www.themoviedb.org/settings/api). Without
  it, films get nothing, since no free film source works without an account.
- **OMDb** — [omdbapi.com](https://www.omdbapi.com/apikey.aspx), emailed to
  you. Without it the rating shown is TVmaze's or AniList's own, not IMDb's.

Anything found locally still wins: a `.nfo` and a `poster.jpg` were put there
deliberately, and a fuzzy title match should not override them.

Lookups run in the background and are cached on disk for a month. Nothing in a
request ever waits on these services, so if one is slow or down the artwork
simply arrives later. Posters are downloaded and served locally rather than
hotlinked, both because the page's CSP is `img-src 'self'` and so the library
keeps working offline. Cached records carry a schema version, so an upgrade
that reads a new field refetches rather than serving a record that predates
it. Images no record mentions any more are removed on the next scan.

> Show data is provided by [TVmaze](https://www.tvmaze.com), used under
> [CC BY-SA](https://creativecommons.org/licenses/by-sa/4.0/). Film data is
> provided by [TMDb](https://www.themoviedb.org), which does not endorse or
> certify this product.
- **Rescan & Search**: Instant real-time video search, category format filters (`All`, `MP4`, `MKV`, `Other`), and multi-attribute sorting.
- **Display WakeLock**: Leverages the Screen Wake Lock API to prevent smart TV screens and phones from sleeping during playback.

---

## 📂 Repository Structure

```
lucid-fermi/
├── config.json             # Server and transcoding configuration
├── settings.json           # Written by the admin page; overrides config.json (gitignored)
├── credentials.json        # Admin username and password hash (gitignored)
├── server.py               # CLI entry point and server startup
├── ffmpeg.exe              # Optional portable FFmpeg binary
├── ffprobe.exe             # Optional portable FFprobe binary
│
├── litejelly/              # Core Python package (Zero pip dependencies)
│   ├── __init__.py         # Version info
│   ├── admin.py            # Admin access control: session cookies, CSRF guard
│   ├── auth.py             # Password hashing, credential storage, sessions, lockout
│   ├── chapters.py         # Chapter markers and the Skip intro / credits segments
│   ├── config.py           # Configuration loading, validation, and CLI overrides
│   ├── enrich.py           # Background metadata lookups, kept out of every request
│   ├── ffmpeg.py           # Media probe, playback planner, quality ladders, transcode commands
│   ├── library.py          # Media scanner, title cleanup, series grouping, SxxExx parser
│   ├── logs.py             # Verbosity, rotating log file, and reading it back
│   ├── metadata.py         # Kodi .nfo sidecars and local artwork discovery
│   ├── opensubtitles.py    # Subtitle search and download, and the account it needs
│   ├── paths.py            # Realpath containment and symlink traversal guards
│   ├── providers.py        # TVmaze, AniList, AniSkip, TheIntroDB, TMDb and OMDb clients with a versioned on-disk cache
│   ├── settings.py         # settings.json load/save and admin input validation
│   ├── store.py            # SQLite WAL progress store and continue watching
│   ├── subtitles.py        # Sidecar discovery, SRT->VTT parser, cue shifting, burn-in logic
│   ├── thumbnails.py       # Single-flight thumbnail worker pool with fallback seeking
│   ├── trickplay.py        # Sprite sheets of frames for the seek-bar preview
│   └── web.py              # HTTP server, REST API, ReadAhead ring buffer, streaming pump
│
├── static/                 # Frontend assets, no build step and no CDN
│   ├── index.html          # Semantic HTML5 layout with accessible templates
│   ├── style.css           # Device profiles, tile shapes, player OSD, @supports layer
│   ├── app.js              # State machine, spatial navigation, custom video controls
│   ├── admin.html          # Settings page, served at /admin (separate from the TV UI)
│   ├── admin.css           # Settings page styling, sharing the library's tokens
│   ├── admin.js            # Settings form, directory picker, save/validation handling
│   ├── fonts/              # Self-hosted Inter (SIL Open Font License 1.1)
│   ├── favicon.svg         # SVG vector favicon
│   └── site.webmanifest    # Web app manifest for mobile home screen installs
│
├── tests/                  # Automated test suite
│   ├── test_litejelly.py   # Unit tests for containment, ranges, subtitles, and titles
│   ├── test_admin.py       # Settings validation, admin access control, library rebuild
│   ├── test_audio.py       # Audio track listing, selection and per-show memory
│   ├── test_auth.py        # Password hashing, credential storage, sessions, lockout
│   ├── test_avsync.py      # Regression tests for the seeking and A/V sync fixes
│   ├── test_browse.py      # Watch-state chips, genres, and what the hero may show
│   ├── test_chapters.py    # Chapter parsing and skip-segment selection
│   ├── test_frontend.py    # Browser code: undefined calls, missing ids, browser floor
│   ├── test_grouping.py    # Categories, series identity, episode ordering
│   ├── test_http.py        # Live-server tests over a real socket
│   ├── test_hwaccel.py     # Encoder discovery, self-test, and the auto choice
│   ├── test_levelling.py   # Dialogue levelling modes and their effect on the plan
│   ├── test_logs.py        # Verbosity floor, rotation, log parsing and filtering
│   ├── test_metadata.py    # .nfo parsing and artwork discovery
│   ├── test_nextup.py      # Next episode selection and the resume rail
│   ├── test_opensubtitles.py # Search, download and the credential file, against a stub
│   ├── test_providers.py   # Online metadata parsing, caching and failure handling
│   ├── test_restart.py     # Restarting the pipe without losing your place
│   ├── test_subtitle_upload.py # What may be written into a media folder, and by whom
│   ├── test_subtitles.py   # One fetch per film, and recovery from a failed one
│   ├── test_thumbnails.py  # Background generation, caching, failure handling
│   └── test_trickplay.py   # Sprite sheets, tile geometry, queue limits
│
├── logs/                   # Rotating log files (gitignored)
│
└── tools/                  # Diagnostic and verification utilities
    ├── avsync_probe.py     # Diagnostic harness for measuring A/V synchronization drift
    ├── hero_fixture.ps1    # Throwaway library of films and episodes for browser checks
    ├── subtitle_fixture.ps1 # MKV carrying a real embedded subtitle stream
    ├── upload_fixture.ps1  # A video with no subtitles, and files to add to it
    ├── mutate_browse.ps1   # Breaks each browse assertion to prove the tests catch it
    ├── mutate_restart.ps1  # The same, for the restart race
    ├── mutate_subtitles.ps1 # The same, for the subtitle fetching rules
    ├── mutate_subtitle_upload.ps1 # The same, for what may be written to disk
    └── mutate_opensubtitles.ps1   # The same, for the search and download rules
```

---

## ⚡ Quick Start

### 1. Prerequisites
- Python 3.10 or higher.
- *(Optional but strongly recommended)*: **FFmpeg & FFprobe**.
  - Drop `ffmpeg.exe` and `ffprobe.exe` directly into the repository root, or ensure they are available on your system `PATH`.

### 2. Run the Server
```bash
# That's it. Everything else is set up in the browser.
python server.py

# Optional: choose the port or bind address
python server.py --host 0.0.0.0 --port 8000

# Optional: seed a folder on first run instead of using the admin page
python server.py --dir "D:\Movies"
```

On the first run the library is empty. Open **`http://127.0.0.1:<port>/admin`**,
add your media folders and save. Settings persist, so from then on
`python server.py` is all you need.

### 3. Open on Your TV or Mobile Device
Upon launch, LiteJelly prints the network URL:
```
============================================================
 LiteJelly 0.2.0
 Local:   http://127.0.0.1:8000
 Network: http://192.168.1.50:8000
 Admin:   http://127.0.0.1:8000/admin
 ffmpeg:  7.1-essentials (ffmpeg.exe)
 ffprobe: 7.1-essentials (ffprobe.exe)
 Videos:  66
 Media directories:
   - [shows] D:\TV Shows
============================================================
```
Type the **Network URL** (e.g., `http://192.168.1.50:8000`) into your TV's browser or mobile browser and bookmark it!

---

## ⚙️ Configuration

Most people never need to edit a file. `config.json` only holds what must be
known before the server can start listening:

```json
{
    "port": 8000,
    "host": "0.0.0.0"
}
```

Everything else — media folders, transcoding, ffmpeg paths — is set on the
admin page and saved to `settings.json` beside it. Where the two overlap,
`settings.json` wins; command line flags beat both. Delete `settings.json` at
any time to fall back to `config.json` and the built-in defaults.

If you prefer to configure by hand, any admin-page setting can also be written
directly into `config.json`:

```json
{
    "server_name": "LiteJelly",
    "media_dirs": [
        { "path": "D:\\Movies", "content_type": "movies" },
        { "path": "D:\\TV Shows", "content_type": "shows", "label": "TV" },
        "C:\\My Local Drive\\Media"
    ],
    "scan_interval": 60,
    "thumbnail_workers": 2,
    "allow_hevc_direct": false,
    "stream_buffer_mb": 8,
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "transcode": {
        "video_codec": "libx264",
        "audio_codec": "aac",
        "preset": "veryfast",
        "crf": 22,
        "max_video_bitrate": "3000k",
        "audio_bitrate": "160k",
        "resolution": "1280x720",
        "max_concurrent": 2
    }
}
```

A media directory may be a plain path string or an object with a
`content_type` of `mixed`, `movies`, `shows` or `anime`. The tag describes how
the folder should be grouped and presented; plain strings default to `mixed`.

---

## 🔧 Admin Page (`/admin`)

Settings live on their own page at `http://<server>:<port>/admin`, the way Plex
and Jellyfin separate configuration from the viewing experience. There is
deliberately no settings icon in the library UI: someone watching a film on a
TV has no reason to reach the transcoder configuration with a D-pad.

It is grouped into four tabs so the everyday controls are not buried among the
rare ones:

| Tab | Contains |
| :--- | :--- |
| **Library** | Media folders and their content types, scan interval, online metadata, manual rescan, clearing or forgetting a wrong metadata match, OpenSubtitles account |
| **Playback** | HEVC direct play, default transcode quality, hardware encoder and its self-test, scrub previews |
| **General** | Server name, port and bind address, admin account, status, settings backup and restore |
| **Logs** | Detail level and a viewer for the recent log |
| **Advanced** | x264 preset and CRF, bitrates, concurrency, stream buffer, log rotation, ffmpeg paths |

Two of those are worth calling out. The encoder self-test runs a real timed
encode rather than trusting `ffmpeg -encoders`, and reports the speed it
actually measured. And "forget this match" exists because a lookup that lands
on the wrong show is otherwise permanent: it clears the cached records for
that title so the next scan can try again.

Everything except `port` and `host` applies immediately; those two are saved
and reported as needing a restart.

### Signing in

The first time you open `/admin`, LiteJelly asks you to create an admin
username and password. **This can only be done on the machine running the
server.** Otherwise whoever reached a freshly started server first would claim
it, which is a race, not a security model.

After that the settings are reachable from any device on the network by
signing in — a TV, a laptop, your phone. The library itself needs no sign-in
and stays open to everyone on the LAN, as before.

Forgot the password? Run `python server.py --reset-admin` on the server to set
a new one.

**How it is protected.** Passwords are stored as a salted PBKDF2-SHA256 hash
in `credentials.json`, never in plain text, and that file is written
owner-readable only. Signing in issues an `HttpOnly`, `SameSite=Strict`
session cookie so the slow hash runs once rather than on every request. Eight
failed attempts lock an address out for fifteen minutes, which matters because
the hash is deliberately expensive: unlimited guessing would also be a way to
burn the server's CPU. Changing the password signs out every other device.

**Why any of this.** The admin API decides which directories the server hands
files out of, so a request that can change it can make the server share
anything on the machine. Writes additionally require a same-origin request, so
another website cannot post to it from your browser.

---

## 📋 Logs

LiteJelly writes to `logs/litejelly.log`, rotating at 2 MB and keeping three
old files by default. The console shows the same lines.

Activity, warnings and errors are always recorded — there is no setting that
hides a failure. The detail level only decides how much *extra* is kept:

| Level | Records |
| :--- | :--- |
| **Normal** (default) | Scans, playback decisions, requests, warnings, errors |
| **Debug** | Plus ffmpeg decisions, skipped files, probe failures |
| **Trace** | Everything, including per-chunk streaming detail |

Change it on **Logs** in the admin page; it applies immediately, without a
restart. `python server.py --verbose` forces debug for a single run without
changing the saved setting.

The terminal stays quiet by default and shows only warnings and errors. Tick
**Also print logs to the terminal** on the Logs tab to mirror everything there,
which is useful when running in the foreground.

The same tab shows the recent log with filtering by severity, so you can look
for errors without reading through every request. Rotation size and how many
old files to keep live under **Advanced → Log file** and need a restart.

---

## 🎮 TV Remote Navigation Guide

| Key / Button | Library View | Player View |
| :--- | :--- | :--- |
| **◀ Left** | Focus the nearest control to the left | Seek backward 10 seconds |
| **▶ Right** | Focus the nearest control to the right | Seek forward 10 seconds |
| **▲ Up** | Focus the nearest control above | Volume up |
| **▼ Down** | Focus the nearest control below | Volume down |
| **OK / Enter** | Open & play selected video | Toggle Play / Pause |
| **Back / Escape** | Clear search / exit | Return to library view |
| **Space** | Play selected video | Toggle Play / Pause |
| **N / P** | — | Next / previous episode |
| **S** | — | Skip intro or credits when offered |
| **C / Q / A** | — | Subtitles / quality / timing |
| **M** | — | Mute |
| **F** | — | Fullscreen |

Focus moves to whichever control is nearest in the direction pressed, measured
from where things actually are on screen. The home view mixes a hero, rails of
differing lengths and a grid, so "move one column" has no meaning there.

---

## 🗺️ Roadmap

Everything above is built and working. This is what is not, roughly in the
order it is worth doing. Each entry says what it buys and what it costs,
because on a phone-hosted server those are usually in tension.

### Playback

- **Pre-remux to MP4 on demand.** Cache a stream-copied MP4 beside the cache
  for files that only fail direct play on their container or audio codec, so
  the browser seeks them itself. Deliberately not built: for watch-once viewing
  it spends disk and a wait up front to improve a seek that the buffered
  stream already handles well. Worth revisiting only for files watched often.
- **A caps handshake for Safari.** MediaSource is unavailable on iPhone before
  17.1, so those fall back to the classic pipe and seek by restarting it. HLS
  would fix it and costs a segmenter, so it needs the capability negotiation
  first rather than a browser sniff.

### Library

- **Paging or streaming the library payload.** The whole library is sent in one
  response. That is fine for hundreds of files and will not be for thousands,
  on a device with this much memory. Absolute paths no longer go out with it,
  but the size does.
- **Cast as a way in.** Portraits are shown but do nothing. The data to filter
  a library by actor is already fetched and cached.
- **More than one viewer.** Progress, and the per-show track choices, are
  shared by everyone using the server. Two people watching the same series
  overwrite each other.

### Interface

- **Drive the browser code end to end, unattended.** Journeys are checked in a
  real browser by hand today, and the fixtures in `tools/` exist for it, but
  nothing runs them on its own. The static checks cannot see a journey.
- **A real suggestion.** The hero no longer claims to be one - it says plainly
  that a title is unstarted - but genres and watch history are both available
  to do better than ranking by artwork.
- **Subtitle appearance.** Size, colour and background are fixed. On a TV
  across a room the size in particular wants to be adjustable.
- **A coarse seek on the remote.** Up and down are volume now, so there is no
  one-press minute jump. Holding left or right is the workaround.

### Operations

- **Optional server statistics.** CPU, memory and active streams on the admin
  page. Genuinely useful on a phone that may be thermally throttling, and
  deliberately absent today rather than faked.

---

## 🧪 Testing & Verification

Run the automated test suite:
```bash
python -m unittest discover -s tests
```

No test touches the network, and none needs media beyond what it generates
itself. `tests/test_frontend.py` covers the browser code, which has no build
step to catch anything: it checks that every function called is defined, that
every element id looked up exists in the markup, that nothing newer than the
target television's engine is used without a fallback, and that no asset is
fetched from the internet. These are textual checks and cannot prove the
interface works - they catch the mistakes that have actually happened.

A textual check is only worth having if it fails when the thing it describes
breaks, so the ones guarding a fixed bug come with a script beside them that
breaks it deliberately and confirms the suite notices:
```bash
powershell -NoProfile -ExecutionPolicy Bypass -File tools/mutate_subtitles.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools/mutate_subtitle_upload.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools/mutate_opensubtitles.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools/mutate_restart.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools/mutate_browse.ps1
```
This has already earned its keep: one assertion passed happily while the
behaviour it claimed to pin was inverted, and was rewritten until it did not.

Nothing in the suite touches the network, including the OpenSubtitles tests,
which run against a stub. That covers the parts that are ours - the credential
file, the file hash, parsing, the refusal to follow a link that is not https -
and does not prove the live API still looks the way it did. That leg has to be
checked with a real key.

`tools/subtitle_fixture.ps1` and `tools/hero_fixture.ps1` generate throwaway
libraries - an MKV carrying a real embedded subtitle stream, a set of films and
episodes - for checking playback behaviour in a browser without needing a
media collection.

Run the A/V synchronization diagnostic tool:
```bash
python tools/avsync_probe.py .probe 25 32 47.5
```

---

## 📄 License

MIT License. Designed for personal and private home network media streaming.
