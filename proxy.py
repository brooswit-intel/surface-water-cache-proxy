"""Loopback-only caching proxy for Earth Platform's JRC Global Surface Water occurrence layer.

Upstream contract (inventoried from Earth Platform 0.1.0-alpha.73 / tag combined-73,
geography/src/main/java/io/earthplatform/geography/globe/SurfaceWaterSource.java):
  base: https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/
  file: occurrence_{lon}{E|W}_{lat}{N|S}v1_4_2021.tif  (one classic tiled TIFF per
        10x10 degree square, 80N to 60S, 1/4000-degree pixels)
  the mod NEVER does a whole-file GET: it reads an 8-byte header, a directory at an
  offset found in that header, two block-offset/count tables, then individual
  256x256 tile blocks (LZW/deflate/raw) - all via HTTP Range, through
  geography/.../RangeFiles.java. A 404/403 upstream response for a square's file is
  treated by the mod as "no data for this square" (Index.MISSING, cached as a
  ".none" sentinel); any other non-2xx status or a short/absent body is a transient
  failure the mod retries and never caches. This proxy preserves that distinction:
  only a 404 is cached as genuine absence, a 403 is never cached (it can also mean a
  terms/auth problem, not missing data - see ticket MINECRAFT-573), and a truncated
  or malformed read is never cached as success.

Default upstream is exactly the mod's existing default (no behavior change for a
caller that never overrides it). SURFACE_WATER_CACHE_UPSTREAM overrides it, e.g. for
tests pointed at a local stand-in - the "endpoint override with unchanged defaults"
the Story asks for, scoped to this proxy's own egress.

Attribution (preserved from SurfaceWaterSource.ATTRIBUTION, never stripped by this
proxy): "JRC Global Surface Water v1.4 (Source: EC JRC/Google; Pekel et al. 2016,
Nature 540, 418-422), Copernicus Programme". Per the JRC/Copernicus Data Users Guide,
the data is provided free of charge without restriction of use, subject to that
citation/attribution - verified before any live upstream use (see MINECRAFT-573).
"""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_UPSTREAM = "https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/"
ATTRIBUTION = (
    "JRC Global Surface Water v1.4 (Source: EC JRC/Google; Pekel et al. 2016, "
    "Nature 540, 418-422), Copernicus Programme"
)

# occurrence_{lon}{E|W}_{lat}{N|S}v1_4_2021.tif - matches SurfaceWaterSource.name().
_FILENAME_RE = re.compile(r"^occurrence_(\d{1,3})([EW])_(\d{1,2})([NS])v1_4_2021\.tif$")


def _parse_file_path(path: str) -> str | None:
    """Validates the request names exactly one JRC surface-water occurrence file and
    nothing else; returns the normalized upstream path, or None to refuse (no
    arbitrary forwarding)."""
    parts = path.strip("/").split("/")
    if len(parts) != 1:
        return None
    name = parts[0]
    match = _FILENAME_RE.match(name)
    if match is None:
        return None
    lon_mag, ew, lat_mag, ns = match.groups()
    lon = int(lon_mag) * (1 if ew == "E" else -1)
    lat = int(lat_mag) * (1 if ns == "N" else -1)
    if lon % 10 != 0 or lat % 10 != 0 or not (-180 <= lon < 180) or not (-60 <= lat <= 80):
        return None
    return name


def _parse_range(range_header: str | None) -> tuple[int, int] | None:
    """Parses a single-range 'bytes=start-end' request header. The mod always sends
    exactly this form (never multi-range, never an open-ended suffix range) when
    reading a TIFF header, directory or tile block, so this proxy requires it rather
    than guessing a default length for a whole-file request it will never actually
    see in practice."""
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes="):]
    start_s, _, end_s = spec.partition("-")
    if not start_s or not end_s:
        return None
    try:
        start, end = int(start_s), int(end_s)
    except ValueError:
        return None
    if start < 0 or end < start:
        return None
    return start, end


@dataclass
class Stats:
    cache_hits: int = 0
    cache_misses: int = 0
    upstream_requests: int = 0
    bytes_downloaded: int = 0
    not_modified: int = 0
    evictions: int = 0
    missing_files: int = 0
    forbidden: int = 0
    failures: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def as_dict(self) -> dict:
        with self.lock:
            return {
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "upstream_requests": self.upstream_requests,
                "bytes_downloaded": self.bytes_downloaded,
                "not_modified": self.not_modified,
                "evictions": self.evictions,
                "missing_files": self.missing_files,
                "forbidden": self.forbidden,
                "failures": self.failures,
            }


_MISSING = object()  # sentinel body for a file genuinely absent upstream (404)


class RangeCache:
    """SQLite-backed, restart-persistent, bounded cache keyed by the exact
    (path, start, end) byte range requested - never a whole file, matching how the
    mod itself only ever asks for specific ranges within a (possibly huge) TIFF.

    A row is written only after a fetch both succeeded (200/206/404) and - for a
    body - was read to completion and matched any declared Content-Length/range
    size; a timeout, dropped connection, short body or non-404/2xx status never
    reaches `put`, so a transient failure can never be served as cached data later.
    A 404 (file genuinely absent) is cached as a zero-length sentinel row distinct
    from a present-but-empty range, so the two are never confused on read-back.
    """

    def __init__(self, db_path: str, max_bytes: int):
        self.db_path = db_path
        self.max_bytes = max_bytes
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS ranges (
                 path TEXT NOT NULL,
                 start INTEGER NOT NULL,
                 end INTEGER NOT NULL,
                 missing INTEGER NOT NULL,
                 etag TEXT NOT NULL,
                 body BLOB NOT NULL,
                 size INTEGER NOT NULL,
                 created_at REAL NOT NULL,
                 last_used_at REAL NOT NULL,
                 PRIMARY KEY (path, start, end)
               )"""
        )
        self._conn.commit()
        self._db_lock = threading.Lock()

    def get(self, path: str, start: int, end: int):
        with self._db_lock:
            row = self._conn.execute(
                "SELECT missing, etag, body FROM ranges WHERE path = ? AND start = ? AND end = ?",
                (path, start, end),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE ranges SET last_used_at = ? WHERE path = ? AND start = ? AND end = ?",
                (time.time(), path, start, end),
            )
            self._conn.commit()
        missing, etag, body = row
        return (_MISSING, etag) if missing else (body, etag)

    def put(self, path: str, start: int, end: int, body: bytes | None) -> str:
        missing = body is None
        payload = b"" if missing else body
        etag = hashlib.sha256(payload if not missing else path.encode()).hexdigest()
        now = time.time()
        with self._db_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ranges "
                "(path, start, end, missing, etag, body, size, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (path, start, end, int(missing), etag, payload, len(payload), now, now),
            )
            self._conn.commit()
        return etag

    def total_bytes(self) -> int:
        with self._db_lock:
            row = self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM ranges").fetchone()
            return row[0]

    def evict_to_fit(self, stats: Stats) -> None:
        if self.max_bytes <= 0:
            return
        target = self.max_bytes * 9 // 10
        with self._db_lock:
            total = self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM ranges").fetchone()[0]
            if total <= self.max_bytes:
                return
            rows = self._conn.execute(
                "SELECT path, start, end, size FROM ranges ORDER BY last_used_at ASC"
            ).fetchall()
            removed = 0
            for path, start, end, size in rows:
                if total - removed <= target:
                    break
                self._conn.execute(
                    "DELETE FROM ranges WHERE path = ? AND start = ? AND end = ?", (path, start, end)
                )
                removed += size
                with stats.lock:
                    stats.evictions += 1
            self._conn.commit()

    def invalidate(self, path: str | None) -> int:
        with self._db_lock:
            if path is None:
                removed = self._conn.execute("SELECT COUNT(*) FROM ranges").fetchone()[0]
                self._conn.execute("DELETE FROM ranges")
            else:
                removed = self._conn.execute(
                    "SELECT COUNT(*) FROM ranges WHERE path = ?", (path,)
                ).fetchone()[0]
                self._conn.execute("DELETE FROM ranges WHERE path = ?", (path,))
            self._conn.commit()
            return removed

    def close(self) -> None:
        self._conn.close()


class UpstreamError(Exception):
    pass


class UpstreamMissing(Exception):
    """Raised for a genuine 404: the file does not exist upstream at all."""


class UpstreamForbidden(Exception):
    """Raised for a 403: may be a terms/auth problem, not genuine absence - never cached."""


_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


def _content_range_matches(header: str | None, start: int, end: int) -> bool:
    """A 206 is only trustworthy if its Content-Range proves the server actually
    answered the exact requested extent - matching body length alone is not
    enough: a misbehaving/misconfigured origin (or an intermediary) could return
    the right number of bytes from the WRONG offset, which would then be cached
    and served back as if it were the requested block (silent data corruption)."""
    if header is None:
        return False
    match = _CONTENT_RANGE_RE.match(header)
    if match is None:
        return False
    got_start, got_end = int(match.group(1)), int(match.group(2))
    return got_start == start and got_end == end


def fetch_upstream_range(upstream_base: str, file_path: str, start: int, end: int,
                          timeout: float = 15.0, retries: int = 3) -> bytes:
    """Byte-range GET against the allowlisted upstream, matching RangeFiles.transfer():
    fixed retries with backoff; 404 raises UpstreamMissing (genuine absence, cacheable);
    403 raises UpstreamForbidden (never cached, never silently treated as absence);
    any other non-2xx, a dropped connection, or a body that doesn't match the
    requested/declared length is a transient failure, retried, then raised as
    UpstreamError - never returned as if it were data."""
    parsed = urllib.parse.urlsplit(upstream_base)
    full_path = parsed.path.rstrip("/") + "/" + file_path
    want = end - start + 1
    last_exc: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(0.05 * (2 ** attempt))
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=timeout)
        try:
            conn.request(
                "GET", full_path,
                headers={
                    "User-Agent": "earth-platform-surface-water-cache/1",
                    "Range": f"bytes={start}-{end}",
                },
            )
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 404:
                raise UpstreamMissing(f"HTTP 404 for {full_path}")
            if resp.status == 403:
                raise UpstreamForbidden(f"HTTP 403 for {full_path}")
            if resp.status not in (200, 206):
                last_exc = UpstreamError(f"HTTP {resp.status} for {full_path}")
                continue
            declared = resp.getheader("Content-Length")
            if declared is not None and int(declared) != len(body):
                last_exc = UpstreamError(
                    f"truncated transfer for {full_path}: declared {declared}, got {len(body)}"
                )
                continue
            if resp.status == 206 and not _content_range_matches(resp.getheader("Content-Range"), start, end):
                last_exc = UpstreamError(
                    f"Content-Range mismatch for {full_path}: wanted bytes {start}-{end}, "
                    f"got {resp.getheader('Content-Range')!r}"
                )
                continue
            if resp.status == 200 and (start != 0 or len(body) > want):
                # Upstream ignored the Range request and sent the whole file; slice
                # locally, exactly like RangeFiles.transfer() does for the Java client.
                body = body[start:start + want]
            if len(body) != want:
                last_exc = UpstreamError(
                    f"short range body for {full_path}: wanted {want} bytes, got {len(body)}"
                )
                continue
            return body
        except (UpstreamMissing, UpstreamForbidden):
            raise
        except OSError as net_err:
            last_exc = net_err
            continue
        finally:
            conn.close()
    raise UpstreamError(f"upstream range fetch failed after {retries} attempts: {last_exc}")


class _Flight(threading.Event):
    """An Event that also carries its own leader's result, so every waiter reads its
    own reference instead of racing the others over a shared dict."""
    result = None
    error: Exception | None = None


class _SingleFlight:
    def __init__(self):
        self._lock = threading.Lock()
        self._inflight: dict[tuple, _Flight] = {}

    def run(self, key: tuple, loader):
        with self._lock:
            flight = self._inflight.get(key)
            if flight is not None:
                is_leader = False
            else:
                flight = _Flight()
                self._inflight[key] = flight
                is_leader = True
        if is_leader:
            try:
                flight.result = loader()
            except Exception as exc:  # noqa: BLE001 - propagated to every waiter
                flight.error = exc
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                flight.set()
        else:
            flight.wait()
        if flight.error is not None:
            raise flight.error
        return flight.result


class SurfaceWaterCacheProxy:
    def __init__(self, db_path: str, upstream_base: str | None = None, max_bytes: int = 512 * 1024 * 1024):
        self.upstream_base = upstream_base or os.environ.get(
            "SURFACE_WATER_CACHE_UPSTREAM", DEFAULT_UPSTREAM
        )
        self.cache = RangeCache(db_path, max_bytes)
        self.stats = Stats()
        self._single_flight = _SingleFlight()

    def health_check(self) -> tuple[bool, str]:
        """TTL 0: a fresh, live reachability probe of the allowlisted upstream on
        every call - never reads or writes the data cache (per John's cache-lifetime
        decision on MINECRAFT-571, 2026-11-24: validated data has no time-based
        expiry, but the health endpoint is TTL 0 only and does no data cache I/O).
        A tiny bytes=0-0 range request against the upstream base itself, so this
        never depends on any one data square existing - any HTTP response at all
        (200/206/403/404) proves the host is reachable; only a connection failure
        or timeout means unhealthy."""
        parsed = urllib.parse.urlsplit(self.upstream_base)
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=5.0)
        try:
            conn.request("GET", parsed.path, headers={
                "User-Agent": "earth-platform-surface-water-cache/1",
                "Range": "bytes=0-0",
            })
            resp = conn.getresponse()
            resp.read()
            return True, f"reachable (HTTP {resp.status})"
        except OSError as net_err:
            return False, f"unreachable: {net_err}"
        finally:
            conn.close()

    def invalidate(self, path: str | None = None) -> int:
        """Explicit invalidation, required because TTL -1 (no time-based expiry) is
        not the same as immutable: clears one file's cached ranges (all offsets), or
        every cached range when path is None. Returns the number of rows removed."""
        return self.cache.invalidate(path)

    def serve_range(self, request_path: str, range_header: str | None, if_none_match: str | None):
        """Returns (status, headers_dict, body_bytes) for one client request. Only
        GET with an exact 'bytes=start-end' Range is accepted - that is the only
        request shape SurfaceWaterSource.java ever makes; anything else is refused,
        never silently forwarded."""
        file_path = _parse_file_path(request_path)
        if file_path is None:
            return 404, {}, b""
        parsed_range = _parse_range(range_header)
        if parsed_range is None:
            return 400, {}, b""
        start, end = parsed_range

        cached = self.cache.get(file_path, start, end)
        if cached is None:
            def load():
                try:
                    body = fetch_upstream_range(self.upstream_base, file_path, start, end)
                except UpstreamMissing:
                    with self.stats.lock:
                        self.stats.missing_files += 1
                    self.cache.put(file_path, start, end, None)
                    return _MISSING, self.cache.get(file_path, start, end)[1]
                with self.stats.lock:
                    self.stats.upstream_requests += 1
                    self.stats.bytes_downloaded += len(body)
                etag = self.cache.put(file_path, start, end, body)
                self.cache.evict_to_fit(self.stats)
                return body, etag

            with self.stats.lock:
                self.stats.cache_misses += 1
            try:
                body, etag = self._single_flight.run((file_path, start, end), load)
            except UpstreamForbidden:
                with self.stats.lock:
                    self.stats.forbidden += 1
                return 403, {}, b""
            except UpstreamError:
                with self.stats.lock:
                    self.stats.failures += 1
                return 502, {}, b""
        else:
            with self.stats.lock:
                self.stats.cache_hits += 1
            body, etag = cached

        if body is _MISSING:
            return 404, {"ETag": etag}, b""

        if if_none_match is not None and if_none_match == etag:
            with self.stats.lock:
                self.stats.not_modified += 1
            return 304, {"ETag": etag}, b""

        headers = {
            "ETag": etag,
            "Content-Range": f"bytes {start}-{end}/*",
            "Content-Type": "application/octet-stream",
        }
        return 206, headers, body

    def close(self):
        self.cache.close()


def make_handler(proxy: SurfaceWaterCacheProxy):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet; /__stats covers verification
            pass

        def _send_json(self, status, payload: dict):
            import json
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/__stats":
                self._send_json(200, proxy.stats.as_dict())
                return

            if self.path == "/__health":
                # TTL 0: a fresh live probe every call, never the data cache.
                ok, detail = proxy.health_check()
                self._send_json(200 if ok else 503, {"healthy": ok, "detail": detail})
                return

            status, headers, body = proxy.serve_range(
                self.path,
                self.headers.get("Range"),
                self.headers.get("If-None-Match"),
            )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):
            if self.path == "/__invalidate" or self.path.startswith("/__invalidate?"):
                query = urllib.parse.urlsplit(self.path).query
                params = urllib.parse.parse_qs(query)
                path = params.get("path", [None])[0]
                removed = proxy.invalidate(path)
                self._send_json(200, {"invalidated": removed, "path": path})
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def run_server(db_path: str, port: int, upstream_base: str | None = None, max_bytes: int = 512 * 1024 * 1024):
    proxy = SurfaceWaterCacheProxy(db_path, upstream_base, max_bytes)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(proxy))
    return server, proxy


if __name__ == "__main__":
    port = int(os.environ.get("SURFACE_WATER_CACHE_PORT", "8075"))
    db = os.environ.get("SURFACE_WATER_CACHE_DB", "surface-water-cache.sqlite3")
    server, proxy = run_server(db, port)
    print(f"surface water cache proxy on http://127.0.0.1:{port} -> upstream {proxy.upstream_base}")
    print(f"attribution: {ATTRIBUTION}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        proxy.close()
