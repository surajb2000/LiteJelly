"""Resource limits and cancellation must survive reconfiguration and shutdown."""

from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from litejelly.ffmpeg import CapacityLimiter, run_quiet
from litejelly.config import Config, TranscodeSettings
from litejelly.web import Application, LiteJellyHTTPServer, RequestHandler, MAX_HTTP_CONNECTIONS, _query_seconds
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
        self.assertIn(b"503", request.sendall.call_args.args[0])
        close.assert_called_once_with(request)

    def test_nonfinite_and_excessive_seek_values_are_rejected(self):
        for value in ("nan", "inf", "-inf", "1e400", "999999999999", "bad"):
            handler = mock.Mock()
            self.assertIsNone(_query_seconds(handler, {"t": [value]}, "t"))
            self.assertEqual(handler.send_api_error.call_args.args[0], 400)


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