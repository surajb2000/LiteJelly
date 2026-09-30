"""A minimal Chrome DevTools client using only the standard library.

Exists because some bugs only happen in real Chrome (hardware decode, GPU
compositing) and nothing in this environment can drive it otherwise. It
launches its own throwaway profile, so the user's profile is never touched.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class Tab:
    def __init__(self, ws_url: str):
        rest = ws_url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        header = b""
        while b"\r\n\r\n" not in header:
            header += self.sock.recv(1)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(header.decode(errors="replace"))
        self.next_id = 0

    def _send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        head = bytearray([0x81])
        if len(data) < 126:
            head.append(0x80 | len(data))
        elif len(data) < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", len(data))
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", len(data))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(head) + mask + masked)

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("devtools socket closed")
            buf += chunk
        return buf

    def _recv(self) -> str:
        message = b""
        while True:
            b1, b2 = self._exact(2)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._exact(8))[0]
            message += self._exact(length)
            if b1 & 0x80:
                return message.decode(errors="replace")

    def call(self, method: str, **params):
        self.next_id += 1
        wanted = self.next_id
        self._send(json.dumps({"id": wanted, "method": method, "params": params}))
        while True:
            reply = json.loads(self._recv())
            if reply.get("id") == wanted:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def js(self, expression: str, timeout: float = 60):
        result = self.call("Runtime.evaluate", expression=expression,
                           awaitPromise=True, returnByValue=True,
                           timeout=int(timeout * 1000))
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"])[:600])
        return result.get("result", {}).get("value")


def launch(url: str, port: int = 9333, size=(1280, 800), extra=()) -> tuple[subprocess.Popen, Tab]:
    profile = tempfile.mkdtemp(prefix="lj-chrome-")
    proc = subprocess.Popen([
        CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
        f"--window-position=0,0", f"--window-size={size[0]},{size[1]}", *extra, url])
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return proc, Tab(pages[0]["webSocketDebuggerUrl"])
        except OSError:
            pass
        time.sleep(0.3)
    proc.kill()
    raise RuntimeError("Chrome did not expose a debugging target")
