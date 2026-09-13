"""Online metadata from free sources that need no API key.

Verified against the live services rather than their docs:

  TVmaze   /singlesearch/shows?q=NAME&embed[]=episodes&embed[]=cast returns the
           show, every episode and the billed cast in one request, with
           rating.average, externals.imdb and poster URLs. No key.
           CC BY-SA, so the UI credits it.
  AniList  GraphQL search returns idMal, averageScore and cover art. No key.
           404 means no match.
  AniSkip  /v2/skip-times/{malId}/{episode} returns opening and ending
           intervals, keyed by the MAL id AniList supplies. No key.

Everything is cached on disk and nothing here is ever called from a request
handler: a media server must not wait on someone else's website to draw a page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("litejelly.providers")

USER_AGENT = "LiteJelly/0.2 (+https://github.com/surajb2000/LiteJelly)"

TVMAZE_ROOT = "https://api.tvmaze.com"
ANILIST_URL = "https://graphql.anilist.co"
ANISKIP_ROOT = "https://api.aniskip.com"
INTRODB_ROOT = "https://api.theintrodb.org/v3"
TMDB_ROOT = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"
OMDB_ROOT = "https://www.omdbapi.com/"

_SECRET = re.compile(r"(api_?key=)[^&]+", re.IGNORECASE)


def redact(url: str) -> str:
    """Keys travel in the query string, so a logged URL would leak them."""
    return _SECRET.sub(r"\1***", url or "")

# TVmaze allows at least 20 calls per 10s; stay well inside it.
MIN_REQUEST_INTERVAL = 0.6
REQUEST_TIMEOUT = 15
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ARTWORK_BYTES = 8 * 1024 * 1024

CACHE_TTL = 30 * 24 * 3600
MISS_TTL = 3 * 24 * 3600

# Bump when the shape of a cached record changes. Without this, a record
# written before a field existed stays valid for its full month and the new
# field simply never appears, which is indistinguishable from a broken fetch.
CACHE_VERSION = 2

# How far past the end of the file a skip segment may reach before the data is
# assumed to describe a different cut.
END_TOLERANCE = 5.0

# Shortest segment worth offering a button for.
MIN_SKIP_SECONDS = 5.0

# Ordered: a marker drawn earlier sits left of a later one on the seek bar.
SKIP_LABELS = {
    "intro": "Skip intro",
    "recap": "Skip recap",
    "credits": "Skip credits",
    "preview": "Skip preview",
}

_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    idMal
    title { romaji english }
    averageScore
    description(asHtml: false)
    coverImage { large }
  }
}
"""


def strip_html(text: str) -> str:
    """TVmaze and AniList summaries arrive as HTML."""
    if not text:
        return ""
    cleaned = _TAGS.sub(" ", text)
    cleaned = (cleaned.replace("&nbsp;", " ").replace("&amp;", "&")
               .replace("&quot;", '"').replace("&#39;", "'")
               .replace("&lt;", "<").replace("&gt;", ">"))
    return _WHITESPACE.sub(" ", cleaned).strip()


class RateLimitedFetcher:
    """Minimal HTTP client: one request at a time, spaced, with 429 backoff."""

    def __init__(self, interval: float = MIN_REQUEST_INTERVAL):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def _wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.interval:
                time.sleep(self.interval - gap)
            self._last = time.monotonic()

    def fetch(self, url: str, data: bytes | None = None,
              content_type: str = "") -> bytes | None:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type

        for attempt in range(3):
            self._wait()
            request = urllib.request.Request(url, data=data, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    return response.read(MAX_RESPONSE_BYTES)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None  # A clean "no match", not a failure.
                if exc.code == 429 and attempt < 2:
                    # The documented remedy is simply to pause and retry.
                    time.sleep(2 ** attempt * 2)
                    continue
                log.debug("%s returned HTTP %s", redact(url), exc.code)
                return None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.debug("%s failed: %s", redact(url), exc)
                return None
        return None

    def fetch_json(self, url: str, data: bytes | None = None,
                   content_type: str = "") -> dict | None:
        raw = self.fetch(url, data, content_type)
        if not raw:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def fetch_json_list(self, url: str) -> list:
        """Some endpoints answer with an array, which fetch_json discards."""
        raw = self.fetch(url)
        if not raw:
            return []
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []


class MetadataCache:
    """JSON on disk. Misses are cached too, or every scan re-asks for nothing."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha1(key.lower().encode("utf-8")).hexdigest()[:20]
        return self.root / namespace / f"{digest}.json"

    def get(self, namespace: str, key: str):
        path = self._path(namespace, key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if payload.get("version") != CACHE_VERSION:
            return None
        age = time.time() - (payload.get("fetched_at") or 0)
        ttl = MISS_TTL if payload.get("miss") else CACHE_TTL
        if age > ttl:
            return None
        return payload.get("data") if not payload.get("miss") else {}

    def put(self, namespace: str, key: str, data, miss: bool = False) -> None:
        path = self._path(namespace, key)
        payload = {"fetched_at": time.time(), "key": key, "version": CACHE_VERSION,
                   "miss": miss, "data": data}
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(temporary, path)
            except OSError as exc:
                log.debug("Could not cache %s/%s: %s", namespace, key, exc)

    def clear(self) -> int:
        removed = 0
        for path in self.root.rglob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed


@dataclass
class SeriesInfo:
    source: str = ""
    title: str = ""
    summary: str = ""
    rating: float | None = None
    imdb_rating: float | None = None
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    imdb_id: str = ""
    mal_id: int | None = None
    poster_url: str = ""
    backdrop_url: str = ""
    # "s1e2" -> {"title", "summary", "rating", "image", "runtime"}
    episodes: dict = field(default_factory=dict)
    # [{"name", "character", "image"}]
    cast: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source, "title": self.title, "summary": self.summary,
            "rating": self.rating, "imdb_rating": self.imdb_rating,
            "year": self.year, "genres": self.genres,
            "imdb_id": self.imdb_id, "mal_id": self.mal_id,
            "poster_url": self.poster_url, "episodes": self.episodes,
            "backdrop_url": self.backdrop_url,
            "cast": self.cast,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SeriesInfo":
        info = cls()
        if not isinstance(data, dict):
            return info
        info.source = str(data.get("source") or "")
        info.title = str(data.get("title") or "")
        info.summary = str(data.get("summary") or "")
        info.rating = data.get("rating")
        info.imdb_rating = data.get("imdb_rating")
        info.year = data.get("year")
        info.genres = list(data.get("genres") or [])
        info.imdb_id = str(data.get("imdb_id") or "")
        info.mal_id = data.get("mal_id")
        info.poster_url = str(data.get("poster_url") or "")
        info.backdrop_url = str(data.get("backdrop_url") or "")
        info.episodes = dict(data.get("episodes") or {})
        info.cast = list(data.get("cast") or [])
        return info


def episode_key(season, episode) -> str:
    return f"s{int(season or 0)}e{int(episode or 0)}"


def parse_tvmaze(payload: dict) -> SeriesInfo:
    info = SeriesInfo(source="TVmaze")
    info.title = str(payload.get("name") or "")
    info.summary = strip_html(payload.get("summary") or "")
    rating = (payload.get("rating") or {}).get("average")
    if isinstance(rating, (int, float)):
        info.rating = round(float(rating), 1)
    premiered = str(payload.get("premiered") or "")
    if premiered[:4].isdigit():
        info.year = int(premiered[:4])
    info.genres = [str(g) for g in (payload.get("genres") or [])][:8]
    info.imdb_id = str((payload.get("externals") or {}).get("imdb") or "")
    image = payload.get("image") or {}
    info.poster_url = str(image.get("original") or image.get("medium") or "")

    for raw in ((payload.get("_embedded") or {}).get("episodes") or []):
        if not isinstance(raw, dict):
            continue
        season, number = raw.get("season"), raw.get("number")
        if season is None or number is None:
            continue
        episode_rating = (raw.get("rating") or {}).get("average")
        info.episodes[episode_key(season, number)] = {
            "title": str(raw.get("name") or ""),
            "summary": strip_html(raw.get("summary") or ""),
            "rating": round(float(episode_rating), 1)
            if isinstance(episode_rating, (int, float)) else None,
            "airdate": str(raw.get("airdate") or ""),
            "image": str((raw.get("image") or {}).get("original") or ""),
            "runtime": raw.get("runtime") if isinstance(raw.get("runtime"), int) else None,
        }

    # A long-running show can list a hundred guest actors; the billed few are
    # the ones anyone recognises.
    for member in ((payload.get("_embedded") or {}).get("cast") or [])[:12]:
        if not isinstance(member, dict):
            continue
        person = member.get("person") or {}
        character = member.get("character") or {}
        image = (person.get("image") or {})
        name = str(person.get("name") or "").strip()
        if not name:
            continue
        info.cast.append({
            "name": name,
            "character": str(character.get("name") or "").strip(),
            "image": str(image.get("medium") or image.get("original") or ""),
        })
    return info


def parse_anilist(payload: dict) -> SeriesInfo:
    media = ((payload.get("data") or {}).get("Media")) or {}
    info = SeriesInfo(source="AniList")
    titles = media.get("title") or {}
    info.title = str(titles.get("english") or titles.get("romaji") or "")
    info.summary = strip_html(media.get("description") or "")
    score = media.get("averageScore")
    if isinstance(score, (int, float)):
        # AniList scores out of 100; everything else here is out of 10.
        info.rating = round(float(score) / 10.0, 1)
    info.mal_id = media.get("idMal") if isinstance(media.get("idMal"), int) else None
    info.poster_url = str((media.get("coverImage") or {}).get("large") or "")
    return info


def parse_tmdb(payload: dict, kind: str = "movie") -> SeriesInfo | None:
    """First result of a TMDb search. Movies use title/release_date, TV uses
    name/first_air_date."""
    results = payload.get("results") or []
    if not results or not isinstance(results[0], dict):
        return None
    top = results[0]

    info = SeriesInfo(source="TMDb")
    info.title = str(top.get("title") or top.get("name") or "")
    info.summary = str(top.get("overview") or "").strip()
    score = top.get("vote_average")
    if isinstance(score, (int, float)) and score > 0:
        info.rating = round(float(score), 1)
    released = str(top.get("release_date") or top.get("first_air_date") or "")
    if released[:4].isdigit():
        info.year = int(released[:4])
    poster = top.get("poster_path")
    if isinstance(poster, str) and poster.startswith("/"):
        info.poster_url = f"{TMDB_IMAGE}{poster}"
    return info if info.title else None


def parse_omdb(payload: dict) -> tuple[float | None, str]:
    """OMDb's IMDb rating and id. Returns (rating, imdb_id)."""
    if not payload or str(payload.get("Response") or "").lower() != "true":
        return None, ""
    imdb_id = str(payload.get("imdbID") or "")
    raw = str(payload.get("imdbRating") or "").strip()
    if not raw or raw.upper() == "N/A":
        return None, imdb_id
    try:
        value = float(raw)
    except ValueError:
        return None, imdb_id
    return (round(value, 1) if 0 <= value <= 10 else None), imdb_id


def parse_aniskip(payload: dict, duration: float = 0.0) -> list[dict]:
    if not payload or not payload.get("found"):
        return []
    kinds = {"op": "intro", "ed": "credits"}
    segments = []
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        interval = result.get("interval") or {}
        try:
            start = float(interval.get("startTime"))
            end = float(interval.get("endTime"))
        except (TypeError, ValueError):
            continue
        if end <= start or end - start < MIN_SKIP_SECONDS:
            continue
        kind = kinds.get(str(result.get("skipType") or "").lower())
        if not kind:
            continue
        # These times describe AniSkip's copy of the episode. If they run past
        # the end of this file it is a different cut, and clamping would turn
        # "skip the opening" into "skip to the end".
        if duration and (start >= duration or end > duration + END_TOLERANCE):
            continue
        segments.append({"start": start, "end": min(end, duration or end),
                         "label": SKIP_LABELS[kind], "kind": kind})
    segments.sort(key=lambda item: item["start"])
    return segments


def parse_introdb(payload: dict, duration: float = 0.0) -> list[dict]:
    """TheIntroDB reports milliseconds, and uses null for the obvious edges.

    A null start means the segment opens the file; a null end means it runs to
    the end of it. The second case needs a real duration to be usable at all,
    so without one those segments are dropped rather than guessed at.
    """
    if not isinstance(payload, dict):
        return []

    segments = []
    for kind, label in SKIP_LABELS.items():
        for entry in payload.get(kind) or []:
            if not isinstance(entry, dict):
                continue
            # A null start is the beginning of the file, which is always
            # known. A null end is the end of it, which is only known if the
            # file has been probed, so those are dropped rather than guessed.
            start = _introdb_seconds(entry.get("start_ms"), 0.0)
            end = _introdb_seconds(entry.get("end_ms"), duration)
            if start is None or end is None or end <= 0:
                continue
            if end <= start or end - start < MIN_SKIP_SECONDS:
                continue
            # The database describes some release of this title. If the times
            # overshoot this file it is a different cut, and trimming them
            # would turn "skip the intro" into "skip the episode".
            if duration and (start >= duration or end > duration + END_TOLERANCE):
                continue
            segments.append({"start": start, "end": min(end, duration or end),
                             "label": label, "kind": kind})

    segments.sort(key=lambda item: item["start"])
    return segments


def _introdb_seconds(value, fallback: float) -> float | None:
    """None means "the natural edge": 0 for a start, the duration for an end."""
    if value is None:
        return fallback
    try:
        return max(0.0, float(value) / 1000.0)
    except (TypeError, ValueError):
        return None


def _introdb_key(imdb_id: str, season=None, episode=None) -> str:
    """A film is keyed by id alone; an episode needs its place in the run."""
    imdb_id = str(imdb_id or "").strip().lower()
    if not re.fullmatch(r"tt\d{7,9}", imdb_id):
        return ""
    if season is None or episode is None:
        return imdb_id
    try:
        return f"{imdb_id}-s{int(season)}e{int(episode)}"
    except (TypeError, ValueError):
        return ""


def artwork_digest(url: str) -> str:
    """Stable name for a downloaded image, so the client can ask for it."""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:20]


class MetadataProviders:
    """Looks things up once, remembers the answer, and never blocks a request."""

    def __init__(self, cache_dir: Path, fetcher: RateLimitedFetcher | None = None,
                 tmdb_key: str = "", omdb_key: str = ""):
        self.cache = MetadataCache(cache_dir / "metadata")
        self.artwork_dir = cache_dir / "artwork"
        self.fetcher = fetcher or RateLimitedFetcher()
        self.tmdb_key = (tmdb_key or "").strip()
        self.omdb_key = (omdb_key or "").strip()

    # -- series -----------------------------------------------------------
    def cached_series(self, title: str, anime: bool = False) -> SeriesInfo | None:
        """What we already know. Never goes near the network.

        Scanning calls this, so a scan cannot be slowed by a third party.
        """
        if not title.strip():
            return None
        cached = self.cache.get("anilist" if anime else "tvmaze", title)
        return SeriesInfo.from_dict(cached) if cached else None

    def has_looked_up(self, title: str, anime: bool = False) -> bool:
        """True once a lookup happened, hit or miss, so it is not repeated."""
        if not title.strip():
            return False
        return self.cache.get("anilist" if anime else "tvmaze", title) is not None

    def cached_skip_times(self, mal_id: int, episode: int,
                          duration: float = 0.0) -> list[dict]:
        if not mal_id or not episode:
            return []
        cached = self.cache.get("aniskip", f"{mal_id}-{episode}")
        return parse_aniskip(cached, duration) if cached else []

    def has_looked_up_skip(self, mal_id: int, episode: int) -> bool:
        """True once asked, hit or miss, so a client knows to stop waiting."""
        if not mal_id or not episode:
            return False
        return self.cache.get("aniskip", f"{mal_id}-{episode}") is not None

    def cached_intro_times(self, imdb_id: str, season=None, episode=None,
                           duration: float = 0.0) -> list[dict]:
        key = _introdb_key(imdb_id, season, episode)
        if not key:
            return []
        cached = self.cache.get("introdb", key)
        return parse_introdb(cached, duration) if cached else []

    def has_looked_up_intro(self, imdb_id: str, season=None,
                            episode=None) -> bool:
        key = _introdb_key(imdb_id, season, episode)
        if not key:
            return False
        return self.cache.get("introdb", key) is not None

    def artwork_path(self, url: str) -> Path | None:
        """Where a poster would be, if it has already been downloaded."""
        if not url:
            return None
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            suffix = ".jpg"
        digest = artwork_digest(url)
        target = self.artwork_dir / f"{digest}{suffix}"
        try:
            return target if target.is_file() and target.stat().st_size else None
        except OSError:
            return None

    def cached_image(self, digest: str) -> Path | None:
        """Look a downloaded image up by digest alone.

        The name is derived from a sha1, so a caller cannot walk out of the
        cache directory with it.
        """
        if not re.fullmatch(r"[0-9a-f]{20}", str(digest or "")):
            return None
        for suffix in (".jpg", ".jpeg", ".png", ".webp"):
            target = self.artwork_dir / f"{digest}{suffix}"
            try:
                if target.is_file() and target.stat().st_size:
                    return target
            except OSError:
                continue
        return None

    def series(self, title: str, anime: bool = False) -> SeriesInfo | None:
        if not title.strip():
            return None
        namespace = "anilist" if anime else "tvmaze"
        cached = self.cache.get(namespace, title)
        if cached is not None:
            return SeriesInfo.from_dict(cached) if cached else None

        info = self._fetch_anilist(title) if anime else self._fetch_tvmaze(title)
        if info is None and not anime:
            # TMDb covers shows TVmaze does not, but only with a key.
            info = self._fetch_tmdb(title, kind="tv")
        if info is None:
            self.cache.put(namespace, title, None, miss=True)
            return None
        self._attach_imdb_rating(info)
        self.cache.put(namespace, title, info.to_dict())
        return info

    def movie(self, title: str, year: int | None = None) -> SeriesInfo | None:
        """Films have no keyless source, so this needs a TMDb key."""
        if not title.strip() or not self.tmdb_key:
            return None
        key = f"{title}|{year or ''}"
        cached = self.cache.get("tmdb-movie", key)
        if cached is not None:
            return SeriesInfo.from_dict(cached) if cached else None

        info = self._fetch_tmdb(title, year, kind="movie")
        if info is None:
            self.cache.put("tmdb-movie", key, None, miss=True)
            return None
        self._attach_imdb_rating(info)
        self.cache.put("tmdb-movie", key, info.to_dict())
        return info

    def cached_movie(self, title: str, year: int | None = None) -> SeriesInfo | None:
        if not title.strip():
            return None
        cached = self.cache.get("tmdb-movie", f"{title}|{year or ''}")
        return SeriesInfo.from_dict(cached) if cached else None

    def has_looked_up_movie(self, title: str, year: int | None = None) -> bool:
        if not title.strip():
            return False
        return self.cache.get("tmdb-movie", f"{title}|{year or ''}") is not None

    def _fetch_tmdb(self, title: str, year: int | None = None,
                    kind: str = "movie") -> SeriesInfo | None:
        if not self.tmdb_key:
            return None
        path = "movie" if kind == "movie" else "tv"
        url = (f"{TMDB_ROOT}/search/{path}"
               f"?api_key={urllib.parse.quote(self.tmdb_key)}"
               f"&query={urllib.parse.quote(title)}&include_adult=false")
        if year:
            url += f"&year={int(year)}"
        payload = self.fetcher.fetch_json(url)
        return parse_tmdb(payload, kind) if payload else None

    def _attach_imdb_rating(self, info: SeriesInfo) -> None:
        """OMDb is the only free way to the actual IMDb score, and needs a key."""
        if not self.omdb_key or info.imdb_rating is not None:
            return
        query = (f"i={urllib.parse.quote(info.imdb_id)}" if info.imdb_id
                 else f"t={urllib.parse.quote(info.title)}"
                      + (f"&y={info.year}" if info.year else ""))
        url = f"{OMDB_ROOT}?apikey={urllib.parse.quote(self.omdb_key)}&{query}"
        payload = self.fetcher.fetch_json(url)
        if not payload:
            return
        rating, imdb_id = parse_omdb(payload)
        if rating is not None:
            info.imdb_rating = rating
        if imdb_id and not info.imdb_id:
            info.imdb_id = imdb_id

    def _fetch_tvmaze(self, title: str) -> SeriesInfo | None:
        url = (f"{TVMAZE_ROOT}/singlesearch/shows"
               f"?q={urllib.parse.quote(title)}&embed[]=episodes&embed[]=cast")
        payload = self.fetcher.fetch_json(url)
        if not payload or not payload.get("name"):
            return None
        info = parse_tvmaze(payload)
        self._attach_tvmaze_backdrop(info, payload.get("id"))
        return info

    def _attach_tvmaze_backdrop(self, info: SeriesInfo, show_id) -> None:
        """A landscape backdrop, which the show record itself does not carry.

        Worth one extra request per show: the alternative is stretching a
        portrait poster across a wide hero, which is what it was built to
        avoid. Cached with the rest of the series record.
        """
        if not show_id:
            return
        fetch = getattr(self.fetcher, "fetch_json_list", None)
        payload = fetch(f"{TVMAZE_ROOT}/shows/{int(show_id)}/images") if fetch else []
        if not isinstance(payload, list):
            return
        best = ""
        for image in payload:
            if not isinstance(image, dict) or image.get("type") != "background":
                continue
            url = ((image.get("resolutions") or {}).get("original") or {}).get("url")
            if not url:
                continue
            # A show can carry several; the one marked main is the chosen art.
            if image.get("main"):
                best = str(url)
                break
            if not best:
                best = str(url)
        info.backdrop_url = best

    def _fetch_anilist(self, title: str) -> SeriesInfo | None:
        body = json.dumps({"query": ANILIST_QUERY,
                           "variables": {"search": title}}).encode("utf-8")
        payload = self.fetcher.fetch_json(ANILIST_URL, body, "application/json")
        if not payload or not ((payload.get("data") or {}).get("Media")):
            return None
        return parse_anilist(payload)

    # -- skip times -------------------------------------------------------
    def skip_times(self, mal_id: int, episode: int, duration: float = 0.0) -> list[dict]:
        if not mal_id or not episode:
            return []
        key = f"{mal_id}-{episode}"
        cached = self.cache.get("aniskip", key)
        if cached is not None:
            return parse_aniskip(cached, duration) if cached else []

        url = (f"{ANISKIP_ROOT}/v2/skip-times/{int(mal_id)}/{int(episode)}"
               f"?types=op&types=ed&episodeLength=0")
        payload = self.fetcher.fetch_json(url)
        if not payload or not payload.get("found"):
            self.cache.put("aniskip", key, None, miss=True)
            return []
        self.cache.put("aniskip", key, payload)
        return parse_aniskip(payload, duration)

    def intro_times(self, imdb_id: str, season=None, episode=None,
                    duration: float = 0.0) -> list[dict]:
        """TheIntroDB: live-action intros, recaps and credits. No key needed.

        Looked up by the IMDb id TVmaze already gave us, so this costs no
        extra provider and cannot match the wrong show by title.
        """
        key = _introdb_key(imdb_id, season, episode)
        if not key:
            return []
        cached = self.cache.get("introdb", key)
        if cached is not None:
            return parse_introdb(cached, duration) if cached else []

        params = {"imdb_id": imdb_id}
        if season is not None and episode is not None:
            params["season"] = int(season)
            params["episode"] = int(episode)
        if duration:
            params["duration_ms"] = int(duration * 1000)
        url = f"{INTRODB_ROOT}/media?" + urllib.parse.urlencode(params)

        payload = self.fetcher.fetch_json(url)
        if not payload or not any(payload.get(kind) for kind in SKIP_LABELS):
            self.cache.put("introdb", key, None, miss=True)
            return []
        self.cache.put("introdb", key, payload)
        return parse_introdb(payload, duration)

    # -- artwork ----------------------------------------------------------
    def artwork(self, url: str) -> Path | None:
        """Download a poster once and keep it.

        The page's CSP is img-src 'self', so a remote URL simply would not
        render; it has to be served from here.
        """
        if not url.startswith("https://"):
            return None
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            suffix = ".jpg"
        digest = artwork_digest(url)
        target = self.artwork_dir / f"{digest}{suffix}"
        if target.is_file() and target.stat().st_size > 0:
            return target

        raw = self.fetcher.fetch(url)
        if not raw or len(raw) > MAX_ARTWORK_BYTES:
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(raw)
            os.replace(temporary, target)
        except OSError as exc:
            log.debug("Could not save artwork: %s", exc)
            return None
        return target
