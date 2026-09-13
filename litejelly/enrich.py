"""Background metadata enrichment.

Lookups happen here and nowhere else. Scanning and playback read only what is
already cached on disk, so a slow or unreachable third-party service can delay
artwork appearing but can never delay the library loading or a video starting.

The worker fills the cache and then asks for a rescan, which re-reads it.
"""

from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger("litejelly.enrich")

# An anime series can run to hundreds of episodes; fetch skip times for a
# sensible prefix rather than issuing a request for every one.
MAX_SKIP_PREFETCH = 60


class Enricher:
    def __init__(self, providers, on_updated=None, workers: int = 1):
        self.providers = providers
        self.on_updated = on_updated
        self._queue: queue.Queue = queue.Queue()
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._worker_count = max(1, workers)
        self._stop = threading.Event()
        self._pending_updates = False

    # -- public API -------------------------------------------------------
    def enqueue_series(self, title: str, anime: bool) -> bool:
        return self._enqueue(("series", title, anime))

    def enqueue_skip(self, mal_id: int, episode: int) -> bool:
        if not mal_id or not episode:
            return False
        return self._enqueue(("skip", int(mal_id), int(episode)))

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            threads = list(self._threads)
            self._threads = []
        for _ in threads:
            self._queue.put(None)
        for thread in threads:
            thread.join(timeout=3)

    def pending(self) -> int:
        return self._queue.qsize()

    # -- internals --------------------------------------------------------
    def _enqueue(self, job) -> bool:
        key = repr(job)
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
        self._ensure_workers()
        self._queue.put(job)
        return True

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._threads or self._stop.is_set():
                return
            for index in range(self._worker_count):
                thread = threading.Thread(target=self._loop, daemon=True,
                                          name=f"enrich-{index}")
                self._threads.append(thread)
                thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                return
            try:
                self._run(job)
            except Exception:
                log.exception("Metadata lookup failed for %r", job)
            finally:
                self._queue.task_done()

            # Tell the library once the burst is done, not once per lookup.
            if self._queue.empty() and self._pending_updates:
                self._pending_updates = False
                if self.on_updated:
                    try:
                        self.on_updated()
                    except Exception:
                        log.exception("Rescan after enrichment failed")

    def _run(self, job) -> None:
        kind = job[0]
        if kind == "series":
            _, title, anime = job
            info = self.providers.series(title, anime)
            if info is None:
                log.debug("No online match for %r", title)
                return
            log.info("Found %s on %s", info.title or title, info.source)
            if info.poster_url:
                self.providers.artwork(info.poster_url)
            self._pending_updates = True
        elif kind == "skip":
            _, mal_id, episode = job
            if self.providers.skip_times(mal_id, episode):
                self._pending_updates = True
