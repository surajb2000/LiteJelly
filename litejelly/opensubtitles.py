"""OpenSubtitles search and download.

The REST API needs three things: a consumer API key, and an account to get a
download token with. Downloads are quota-limited per day, so nothing here
fetches speculatively - a search costs nothing but a request, and a file is
only pulled when someone picks one.

Credentials live in their own 0600 file rather than in settings.json, for the
same reason the admin password does: settings.json is exported and imported
from the admin page, and a third-party password has no business travelling
with it.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("litejelly.opensubtitles")

CREDENTIALS_FILE = "opensubtitles.json"
API_ROOT = "https://api.opensubtitles.com/api/v1"
# They ask for an identifying User-Agent and reject the urllib default.
USER_AGENT = "LiteJelly v0.2"
REQUEST_TIMEOUT = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# A token is good for about a day; this is refreshed well before that.
TOKEN_LIFETIME = 20 * 3600
MIN_REQUEST_INTERVAL = 0.4
MAX_RESULTS = 25


class OpenSubtitlesError(Exception):
    """Something the admin or the viewer needs to be told about."""


@dataclass
class Account:
    api_key: str = ""
    username: str = ""
    password: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.username and self.password)

    def to_public_dict(self) -> dict:
        """Never includes the password, and never the key itself."""
        return {
            "configured": self.configured,
            "username": self.username,
            "has_key": bool(self.api_key),
            "has_password": bool(self.password),
        }


def credentials_path(app_dir: Path) -> Path:
    return app_dir / CREDENTIALS_FILE


def load_account(app_dir: Path) -> Account:
    path = credentials_path(app_dir)
    if not path.is_file():
        return Account()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read %s: %s", CREDENTIALS_FILE, exc)
        return Account()
    if not isinstance(data, dict):
        return Account()
    return Account(
        api_key=str(data.get("api_key") or ""),
        username=str(data.get("username") or ""),
        password=str(data.get("password") or ""),
    )


def save_account(app_dir: Path, account: Account) -> None:
    """Write atomically, readable only by the owner where that is supported."""
    path = credentials_path(app_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps({
        "api_key": account.api_key,
        "username": account.username,
        "password": account.password,
    }, indent=2)

    handle = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows and some filesystems do not support this.


def movie_hash(path: Path) -> str:
    """OpenSubtitles' own hash: the size plus the first and last 64 KB.

    It matches a release far more reliably than a title does, because it is
    computed from the file itself rather than from what someone named it.
    """
    chunk = 65536
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size < chunk * 2:
        return ""

    value = size
    try:
        with path.open("rb") as handle:
            for offset in (0, size - chunk):
                handle.seek(offset)
                buffer = handle.read(chunk)
                if len(buffer) < chunk:
                    return ""
                for piece in struct.unpack(f"<{chunk // 8}Q", buffer):
                    value = (value + piece) & 0xFFFFFFFFFFFFFFFF
    except OSError:
        return ""
    return f"{value:016x}"


@dataclass
class Candidate:
    file_id: int
    language: str
    release: str
    downloads: int
    hearing_impaired: bool
    from_trusted: bool
    uploader: str

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "language": self.language,
            "release": self.release,
            "downloads": self.downloads,
            "hearing_impaired": self.hearing_impaired,
            "from_trusted": self.from_trusted,
            "uploader": self.uploader,
        }


class OpenSubtitles:
    """One account's worth of searching and downloading."""

    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self.account = load_account(app_dir)
        self._token = ""
        self._token_at = 0.0
        self._lock = threading.Lock()
        self._last_call = 0.0

    def set_account(self, account: Account) -> None:
        with self._lock:
            self.account = account
            self._token = ""
            self._token_at = 0.0
        save_account(self.app_dir, account)

    # -- plumbing ---------------------------------------------------------

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - gap)
        self._last_call = time.monotonic()

    def _call(self, method: str, path: str, payload: dict | None = None,
              token: str = "") -> dict:
        if not self.account.api_key:
            raise OpenSubtitlesError("No OpenSubtitles API key has been set")

        url = API_ROOT + path
        headers = {
            "Api-Key": self.account.api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        self._pace()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            raise OpenSubtitlesError(_describe_http(exc)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise OpenSubtitlesError(f"Could not reach OpenSubtitles: {exc}") from exc

        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            # Seen in practice from the edge in front of the API rather than
            # from the API itself, so it says what arrived, not why.
            raise OpenSubtitlesError(
                "OpenSubtitles sent a reply that was not JSON") from exc
        if not isinstance(parsed, dict):
            raise OpenSubtitlesError("OpenSubtitles sent something unexpected")
        return parsed

    def _valid_token(self) -> str:
        with self._lock:
            fresh = self._token and (time.time() - self._token_at) < TOKEN_LIFETIME
            if fresh:
                return self._token
        if not self.account.configured:
            raise OpenSubtitlesError(
                "OpenSubtitles needs an API key, a username and a password")

        body = self._call("POST", "/login", {
            "username": self.account.username,
            "password": self.account.password,
        })
        token = str(body.get("token") or "")
        if not token:
            raise OpenSubtitlesError("OpenSubtitles refused the sign-in")
        with self._lock:
            self._token = token
            self._token_at = time.time()
        return token

    # -- the two things it is for -----------------------------------------

    def sign_in(self) -> dict:
        """Used by the admin page to prove the details work."""
        with self._lock:
            self._token = ""
            self._token_at = 0.0
        self._valid_token()
        return {"username": self.account.username}

    def search(self, query: str, language: str, file_hash: str = "",
               season: int | None = None, episode: int | None = None) -> list[Candidate]:
        if not self.account.api_key:
            raise OpenSubtitlesError("No OpenSubtitles API key has been set")

        params: list[tuple[str, str]] = [("languages", language or "en")]
        if file_hash:
            params.append(("moviehash", file_hash))
        if query:
            params.append(("query", query))
        if season is not None:
            params.append(("season_number", str(season)))
        if episode is not None:
            params.append(("episode_number", str(episode)))

        body = self._call("GET", "/subtitles?" + urllib.parse.urlencode(params))
        rows = body.get("data")
        if not isinstance(rows, list):
            return []

        found: list[Candidate] = []
        for row in rows[:MAX_RESULTS]:
            attributes = row.get("attributes") if isinstance(row, dict) else None
            if not isinstance(attributes, dict):
                continue
            files = attributes.get("files")
            if not isinstance(files, list) or not files:
                continue
            first = files[0] if isinstance(files[0], dict) else {}
            try:
                file_id = int(first.get("file_id"))
            except (TypeError, ValueError):
                continue
            uploader = attributes.get("uploader")
            found.append(Candidate(
                file_id=file_id,
                language=str(attributes.get("language") or ""),
                release=str(attributes.get("release") or first.get("file_name") or ""),
                downloads=_as_int(attributes.get("download_count")),
                hearing_impaired=bool(attributes.get("hearing_impaired")),
                from_trusted=bool(attributes.get("from_trusted")),
                uploader=str((uploader or {}).get("name") or "")
                if isinstance(uploader, dict) else "",
            ))
        return found

    def download(self, file_id: int) -> tuple[bytes, str]:
        """The subtitle bytes and the name OpenSubtitles gave them."""
        token = self._valid_token()
        body = self._call("POST", "/download", {"file_id": int(file_id)}, token=token)
        link = str(body.get("link") or "")
        if not link:
            message = str(body.get("message") or "OpenSubtitles returned no link")
            raise OpenSubtitlesError(message)
        # The link is chosen by the API, not by us, but it still gets checked:
        # anything that is not plain https would be fetching something else.
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme != "https" or not parsed.hostname:
            raise OpenSubtitlesError("OpenSubtitles returned a link we will not follow")

        self._pace()
        request = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                data = response.read(MAX_RESPONSE_BYTES)
        except (urllib.error.URLError, OSError) as exc:
            raise OpenSubtitlesError(f"Could not download it: {exc}") from exc

        remaining = body.get("remaining")
        if isinstance(remaining, int) and remaining <= 0:
            log.info("OpenSubtitles downloads for today are used up")
        return data, str(body.get("file_name") or "")

    def quota(self) -> dict:
        """What is left today, as far as the last download told us."""
        return {"configured": self.account.configured}


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _describe_http(exc: urllib.error.HTTPError) -> str:
    """Turn the API's status codes into something worth reading.

    Their own message is the useful part - a wrong key answers 403 with "You
    cannot consume this service", which says far more than the status does -
    so it is quoted when there is one.
    """
    detail = ""
    try:
        body = json.loads(exc.read(8192).decode("utf-8", errors="replace"))
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or "").strip()
    except (ValueError, OSError, AttributeError):
        detail = ""

    if exc.code == 401:
        base = "OpenSubtitles rejected the API key or the sign-in"
    elif exc.code == 403:
        base = "OpenSubtitles refused the request; check the API key"
    elif exc.code == 406:
        base = "Today's OpenSubtitles download quota is used up"
    elif exc.code == 429:
        base = "OpenSubtitles is rate limiting; try again in a moment"
    elif exc.code >= 500:
        base = "OpenSubtitles is having trouble; try again later"
    else:
        base = f"OpenSubtitles returned HTTP {exc.code}"
    return f"{base} ({detail})" if detail else base
