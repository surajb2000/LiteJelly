"""Resource limits and cancellation must survive reconfiguration and shutdown."""

from __future__ import annotations

import subprocess
import io
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from litejelly.ffmpeg import CapacityLimiter, FFmpegTools, MediaInfo, run_quiet
from litejelly.config import Config, TranscodeSettings
from litejelly.web import Application, LiteJellyHTTPServer, ReadAhead, RequestHandler, MAX_HTTP_CONNECTIONS, _query_seconds
from litejelly.trickplay import TrickplayService
from litejelly.enrich import Enricher


class CapacityTests(unittest.TestCase):
    def test_lower_limit_preserves_occupied_slots(self):
        limiter = CapacityLimiter(2)
        self.assertTrue(limiter.acquire(blocking=False))
        self.assertTrue(limiter.acquire(blocking=False))
        limiter.resize(1)
        self.assertFalse(limiter.acquire(blocking=False))
        limiter.release()
        self.assertFalse(limiter.acquire(blocking=False))
        limiter.release()
        self.assertTrue(limiter.acquire(blocking=False))
        limiter.release()

    def test_close_wakes_waiters_without_granting_a_slot(self):
        limiter = CapacityLimiter(1)
        self.assertTrue(limiter.acquire())
        results = []
        waiter = threading.Thread(target=lambda: results.append(limiter.acquire(timeout=2)))
        waiter.start()
        limiter.close()
        waiter.join(timeout=1)
        self.assertEqual(results, [False])
        self.assertFalse(waiter.is_alive())
        limiter.release()


class ProbeConcurrencyTests(unittest.TestCase):
    def test_concurrent_cold_requests_share_one_probe(self):
        """Three overlapping cold requests previously launched three probe jobs."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"fixture")
            tools = FFmpegTools(Path(directory))
            self.addCleanup(tools.close)
            entered = threading.Event()
            release = threading.Event()
            lock = threading.Lock()
            arrivals = []
            results = []

            class ObservedLock:
                def __enter__(self):
                    lock.acquire()
                    arrivals.append(threading.get_ident())
                    if len(arrivals) == 3:
                        entered.set()

                def __exit__(self, *args):
                    lock.release()

            tools._probe_lock = ObservedLock()
            expected = MediaInfo(probed=True, duration=60)

            def probe(_path):
                release.wait(5)
                return expected

            with mock.patch.object(tools, "_probe_uncached", side_effect=probe) as uncached:
                workers = [threading.Thread(target=lambda: results.append(tools.probe(path)),
                                            daemon=True) for _index in range(3)]
                try:
                    for worker in workers:
                        worker.start()
                    self.assertTrue(entered.wait(3))
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(timeout=3)
                self.assertTrue(all(not worker.is_alive() for worker in workers))
                self.assertEqual(len(results), 3)
                self.assertTrue(all(result is expected for result in results))
                self.assertEqual(uncached.call_count, 1)

    def test_distinct_files_probe_concurrently_and_changes_invalidate_cache(self):
        with TemporaryDirectory() as directory:
            tools = FFmpegTools(Path(directory))
            self.addCleanup(tools.close)
            paths = [Path(directory) / name for name in ("first.mkv", "second.mkv")]
            for path in paths:
                path.write_bytes(b"fixture")
            barrier = threading.Barrier(2)

            def probe(_path):
                barrier.wait(timeout=3)
                return MediaInfo(probed=True)

            with mock.patch.object(tools, "_probe_uncached", side_effect=probe) as uncached:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(tools.probe, paths))
                self.assertEqual(uncached.call_count, 2)
                self.assertIs(tools.probe(paths[0]), results[0])
                paths[0].write_bytes(b"replacement media")
                uncached.side_effect = None
                uncached.return_value = MediaInfo(probed=True, duration=120)
                self.assertEqual(tools.probe(paths[0]).duration, 120)
                self.assertEqual(uncached.call_count, 3)

    def test_failed_probe_releases_pending_entry_and_allows_retry(self):
        with TemporaryDirectory() as directory:
            tools = FFmpegTools(Path(directory))
            self.addCleanup(tools.close)
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"fixture")
            expected = MediaInfo(probed=True)
            with mock.patch.object(tools, "_probe_uncached",
                                   side_effect=[RuntimeError("failed probe"), expected]) as uncached:
                with self.assertRaisesRegex(RuntimeError, "failed probe"):
                    tools.probe(path)
                self.assertEqual(tools._pending_probes, {})
                self.assertIs(tools.probe(path), expected)
                self.assertEqual(uncached.call_count, 2)

    def test_shutdown_releases_waiters_and_does_not_cache_retired_result(self):
        with TemporaryDirectory() as directory:
            tools = FFmpegTools(Path(directory))
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"fixture")
            started = threading.Event()
            release = threading.Event()

            def probe(_path):
                started.set()
                release.wait(5)
                return MediaInfo(probed=True)

            with mock.patch.object(tools, "_probe_uncached", side_effect=probe) as uncached:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    owner = pool.submit(tools.probe, path)
                    try:
                        self.assertTrue(started.wait(3))
                        with tools._probe_lock:
                            pending = next(iter(tools._pending_probes.values()))
                        waiter = pool.submit(pending.result)
                        tools.close()
                        self.assertFalse(waiter.result(timeout=1).probed)
                        self.assertFalse(tools.probe(path).probed)
                    finally:
                        release.set()
                        tools.close()
                    self.assertFalse(owner.result(timeout=3).probed)
                self.assertEqual(tools._probe_cache, {})
                self.assertEqual(tools._pending_probes, {})
                self.assertEqual(uncached.call_count, 1)


class ReconfigurationTests(unittest.TestCase):
    def test_shutdown_is_idempotent_and_rejects_late_streams(self):
        with TemporaryDirectory() as directory:
            app = Application(Config(app_dir=Path(directory)))
            app.shutdown()
            app.shutdown()
            self.assertFalse(app.track_stream(mock.Mock()))
            with self.assertRaises(RuntimeError):
                app.apply_config(app.config)

    def test_reconfigure_preserves_slots_and_metadata_refresh(self):
        with TemporaryDirectory() as directory:
            config = Config(app_dir=Path(directory), online_metadata=True,
                            transcode=TranscodeSettings(max_concurrent=1))
            app = Application(config)
            original_tools = app.tools
            limiter = original_tools.transcode_sem
            self.assertTrue(limiter.acquire(blocking=False))
            try:
                app.apply_config(config)
                self.assertIs(app.tools.transcode_sem, limiter)
                self.assertFalse(limiter.acquire(blocking=False))
                self.assertTrue(original_tools._stop.is_set())
                self.assertTrue(callable(app.enricher.on_updated))
                with mock.patch.object(app.library, "request_scan") as request:
                    app.enricher.on_updated()
                    request.assert_called_once_with(force=True)
            finally:
                limiter.release()
                app.shutdown()

    def test_a_failed_rebuild_leaves_the_running_services_alone(self):
        """The stream limit used to be resized before the rest of the build could fail."""
        with TemporaryDirectory() as directory:
            config = Config(app_dir=Path(directory), transcode=TranscodeSettings(max_concurrent=1))
            app = Application(config)
            running = (app.config, app.tools, app.thumbnails, app.trickplay)
            built = []
            real_tools = FFmpegTools

            def track(*args, **kwargs):
                tools = real_tools(*args, **kwargs)
                built.append(tools)
                return tools

            changed = Config(app_dir=Path(directory), transcode=TranscodeSettings(max_concurrent=4))
            try:
                with mock.patch("litejelly.web.FFmpegTools", side_effect=track), \
                     mock.patch("litejelly.web.TrickplayService", side_effect=RuntimeError("broken")):
                    with self.assertRaises(RuntimeError):
                        app.apply_config(changed)
                self.assertEqual((app.config, app.tools, app.thumbnails, app.trickplay), running)
                self.assertTrue(app.tools.transcode_sem.acquire(blocking=False))
                self.assertFalse(app.tools.transcode_sem.acquire(blocking=False),
                                 "the live limit must still be the old one")
                app.tools.transcode_sem.release()
                self.assertFalse(app.tools._stop.is_set())
                self.assertTrue(built and built[0]._stop.is_set(), "half-built tools must be closed")
            finally:
                app.shutdown()

    def test_closed_trickplay_service_cannot_queue_more_work(self):
        with TemporaryDirectory() as directory:
            service = TrickplayService(mock.Mock(available=True), Path(directory))
            service.close()
            self.assertFalse(service.request(Path(directory) / "video.mkv", 600))
            service._ensure_worker()
            self.assertIsNone(service._worker)

    def test_stopped_enricher_discards_work_and_detaches_callback(self):
        service = Enricher(mock.Mock(), on_updated=mock.Mock())
        service._queue.put_nowait(("movie", "queued", 2024))
        service.stop()
        self.assertFalse(service.enqueue_movie("new", 2025))
        self.assertIsNone(service.on_updated)
        self.assertEqual(service._queue.unfinished_tasks, 0)


class RequestLimitsTests(unittest.TestCase):
    def test_busy_server_rejects_without_spawning_another_thread(self):
        server = LiteJellyHTTPServer(("127.0.0.1", 0), RequestHandler, mock.Mock())
        self.addCleanup(server.server_close)
        for _index in range(MAX_HTTP_CONNECTIONS):
            self.assertTrue(server._request_slots.acquire(blocking=False))
        request = mock.Mock()
        with mock.patch.object(server, "shutdown_request") as close:
            server.process_request(request, ("127.0.0.1", 1))
        reply = request.sendall.call_args.args[0]
        head, _, body = reply.partition(b"\r\n\r\n")
        self.assertIn(b"503", head)
        self.assertIn(b"Content-Length: " + str(len(body)).encode(), head)
        self.assertEqual(json.loads(body)["status"], 503, "every error answers in JSON")
        close.assert_called_once_with(request)

    def test_nonfinite_and_excessive_seek_values_are_rejected(self):
        for value in ("nan", "inf", "-inf", "1e400", "999999999999", "bad"):
            handler = mock.Mock()
            self.assertIsNone(_query_seconds(handler, {"t": [value]}, "t"))
            self.assertEqual(handler.send_api_error.call_args.args[0], 400)


class ReadAheadTests(unittest.TestCase):
    def test_chunk_order_and_eof_preserve_every_byte_within_capacity(self):
        payload = bytes(range(251)) * 1000
        reader = ReadAhead(io.BytesIO(payload), capacity=2048, chunk=256)
        self.addCleanup(reader.close)
        chunks = []
        while True:
            chunk = reader.read(timeout=2)
            if not chunk:
                break
            chunks.append(chunk)
            self.assertLessEqual(reader.buffered, 2048)
        reader._thread.join(timeout=2)
        self.assertFalse(reader._thread.is_alive())
        self.assertEqual(b"".join(chunks), payload)
        self.assertEqual(reader.buffered, 0)

    def test_close_releases_a_producer_waiting_on_a_full_buffer(self):
        overflow = threading.Event()

        class Stream(io.BytesIO):
            def read(self, size):
                chunk = super().read(size)
                if self.tell() >= 24:
                    overflow.set()
                return chunk

        reader = ReadAhead(Stream(b"x" * 64), capacity=16, chunk=8)
        self.addCleanup(reader.close)
        try:
            self.assertTrue(overflow.wait(2))
            self.assertEqual(reader.buffered, 16)
        finally:
            reader.close()
            reader._thread.join(timeout=2)
        self.assertFalse(reader._thread.is_alive())
        self.assertEqual(reader.buffered, 0)
        self.assertEqual(reader.read(), b"")

    def test_close_releases_an_empty_reader_before_the_pipe_finishes(self):
        release = threading.Event()
        stream = mock.Mock()
        stream.read.side_effect = lambda size: (release.wait(5), b"")[1]
        reader = ReadAhead(stream, capacity=16, chunk=8)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(reader.read, timeout=3)
                reader.close()
                self.assertEqual(waiting.result(timeout=1), b"")
        finally:
            reader.close()
            release.set()
            reader._thread.join(timeout=2)
        self.assertFalse(reader._thread.is_alive())

    def test_pipe_error_preserves_bytes_already_queued(self):
        stream = mock.Mock()
        stream.read.side_effect = [b"first", b"second", OSError("closed pipe")]
        reader = ReadAhead(stream, capacity=64, chunk=8)
        self.addCleanup(reader.close)
        self.assertEqual(reader.read(timeout=2), b"first")
        self.assertEqual(reader.read(timeout=2), b"second")
        self.assertEqual(reader.read(timeout=2), b"")
        reader._thread.join(timeout=2)
        self.assertFalse(reader._thread.is_alive())


class ProcessTests(unittest.TestCase):
    def test_cancellation_terminates_and_reaps_the_process(self):
        cancel = threading.Event()
        started = threading.Event()
        children = []
        failures = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            """Signal when the controlled child exists, without relying on a sleep."""
            process = real_popen(*args, **kwargs)
            children.append(process)
            started.set()
            return process

        def run():
            """Capture the expected cancellation result from the worker thread."""
            try:
                run_quiet([sys.executable, "-c", "import threading; threading.Event().wait(60)"],
                          timeout=30, cancel_event=cancel)
            except subprocess.SubprocessError as error:
                failures.append(str(error))

        with mock.patch("litejelly.ffmpeg.subprocess.Popen", side_effect=spawn):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                cancel.set()
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, ["Media job cancelled"])
                self.assertIsNotNone(children[0].poll())
            finally:
                cancel.set()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=3)
                worker.join(timeout=3)

    def test_cancelled_job_never_spawns(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch("litejelly.ffmpeg.subprocess.Popen") as spawn:
            with self.assertRaises(subprocess.SubprocessError):
                run_quiet(["unused"], cancel_event=cancel)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
