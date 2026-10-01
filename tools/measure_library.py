"""Measure library scanning and the /api/library payload at a given size.

Builds a throwaway library of one-byte "videos" (episodes and films, with a
share of them watched), starts a real server on a free loopback port, and
reports scan time, the per-scan change check, and the size and latency of the
library response with and without gzip. Nothing outside a temporary directory
is touched.

Usage:  python tools/measure_library.py [count ...]     (default: 1000 5000 10000)

These are local wall-clock numbers on this machine's disk, not a phone or a
network share, and a one-byte file stats faster than a real one.
"""

from __future__ import annotations

import http.client
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litejelly.config import load_config  # noqa: E402
from litejelly.web import Application, create_server  # noqa: E402


def build_library(root: Path, count: int) -> Path:
    """Two thirds episodes across shows of 50, one third films."""
    media = root / "media"
    episodes = count * 2 // 3
    for index in range(episodes):
        show, number = divmod(index, 50)
        folder = media / "shows" / f"Show {show:04d}" / "Season 01"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"Show.{show:04d}.S01E{number + 1:02d}.1080p.WEB-DL.mkv").write_bytes(b"x")
    films = media / "films"
    films.mkdir(parents=True, exist_ok=True)
    for index in range(count - episodes):
        (films / f"Film Number {index:05d} ({1950 + index % 70}).mkv").write_bytes(b"x")
    return media


def fetch(port: int, headers: dict | None = None) -> tuple[int, bytes, float, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        started = time.perf_counter()
        connection.request("GET", "/api/library", headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, body, time.perf_counter() - started, dict(response.getheaders())
    finally:
        connection.close()


def measure(count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="litejelly-measure-") as directory:
        root = Path(directory)
        media = build_library(root, count)
        (root / "config.json").write_text(json.dumps({
            "host": "127.0.0.1", "port": 0, "media_dirs": [str(media)]}), encoding="utf-8")
        config, _ = load_config(root)
        app = Application(config)
        try:
            started = time.perf_counter()
            videos = app.library.scan(force=True)
            scan = time.perf_counter() - started
            started = time.perf_counter()
            app.library._directory_signature(app.library.media_dirs)
            signature = time.perf_counter() - started
            for video in videos[: len(videos) // 3]:
                app.progress.save(video.id, 600, 1800)

            server = create_server(app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                fetch(port)
                plain = [fetch(port) for _ in range(5)]
                zipped = [fetch(port, {"Accept-Encoding": "gzip"}) for _ in range(5)]
            finally:
                server.shutdown()
                server.server_close()
            return {
                "files": len(videos), "scan_s": scan, "signature_s": signature,
                "status": plain[-1][0], "payload_kb": len(plain[-1][1]) / 1024,
                "response_ms": statistics.median(run[2] for run in plain) * 1000,
                "gzip_kb": len(zipped[-1][1]) / 1024,
                "gzip_ms": statistics.median(run[2] for run in zipped) * 1000,
                "encoding": zipped[-1][3].get("Content-Encoding", "none"),
            }
        finally:
            app.shutdown()


def main(argv: list[str]) -> int:
    counts = [int(value) for value in argv] or [1000, 5000, 10000]
    print(f"{'files':>6} {'scan s':>7} {'check s':>8} {'JSON KB':>8} {'GET ms':>7} "
          f"{'gzip KB':>8} {'gzip ms':>8}")
    for count in counts:
        result = measure(count)
        print(f"{result['files']:>6} {result['scan_s']:>7.2f} {result['signature_s']:>8.2f} "
              f"{result['payload_kb']:>8.0f} {result['response_ms']:>7.1f} "
              f"{result['gzip_kb']:>8.0f} {result['gzip_ms']:>8.1f}  ({result['encoding']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
