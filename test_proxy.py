import http.server
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest import mock

import proxy as sw


class _FakeUpstream(http.server.BaseHTTPRequestHandler):
    """A local stand-in for storage.googleapis.com. Each test installs behavior via
    class-level state so every request can be scripted exactly."""

    FILES: dict[str, bytes] = {}
    FORBIDDEN: set[str] = set()
    DROP_ONCE: set[str] = set()  # paths whose very first request gets a dropped connection
    TRUNCATE: set[str] = set()  # paths whose response lies about Content-Length
    WRONG_RANGE: set[str] = set()  # paths whose Content-Range lies about which bytes these are
    requests: list[tuple[str, str]] = []

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.lstrip("/")
        rng = self.headers.get("Range")
        type(self).requests.append((path, rng))

        if path in type(self).DROP_ONCE:
            type(self).DROP_ONCE.discard(path)
            self.close_connection = True
            return  # no response at all: simulates a dropped connection

        if path in type(self).FORBIDDEN:
            self.send_response(403)
            self.end_headers()
            return

        body = type(self).FILES.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return

        start, end = 0, len(body) - 1
        if rng and rng.startswith("bytes="):
            s, _, e = rng[len("bytes="):].partition("-")
            start, end = int(s), int(e)
        chunk = body[start:end + 1]
        declared = len(chunk) - 1 if path in type(self).TRUNCATE else len(chunk)
        content_range_start, content_range_end = (start, end)
        if path in type(self).WRONG_RANGE:
            # Misbehaving/misconfigured origin: the right byte count, from the WRONG offset.
            content_range_start, content_range_end = start + 1000, end + 1000
        self.send_response(206)
        self.send_header("Content-Length", str(declared))
        self.send_header("Content-Range", f"bytes {content_range_start}-{content_range_end}/{len(body)}")
        self.end_headers()
        self.wfile.write(chunk)


class ProxyTest(unittest.TestCase):
    FILE = "occurrence_20E_50Nv1_4_2021.tif"

    def setUp(self):
        _FakeUpstream.FILES = {}
        _FakeUpstream.FORBIDDEN = set()
        _FakeUpstream.DROP_ONCE = set()
        _FakeUpstream.TRUNCATE = set()
        _FakeUpstream.WRONG_RANGE = set()
        _FakeUpstream.requests = []
        self.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.upstream_base = f"http://127.0.0.1:{self.upstream.server_address[1]}/"

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "cache.sqlite3")
        # health_check_file=self.FILE: reuse the same mock file the data tests
        # already populate, so health-check tests don't need a second fixture.
        self.proxy = sw.SurfaceWaterCacheProxy(
            self.db_path, upstream_base=self.upstream_base, max_bytes=1024, health_check_file=self.FILE
        )

    def tearDown(self):
        self.proxy.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def test_default_upstream_is_unchanged_mod_default(self):
        self.assertEqual(
            sw.DEFAULT_UPSTREAM,
            "https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/",
        )
        unset = sw.SurfaceWaterCacheProxy(self.db_path + ".2")
        self.assertEqual(unset.upstream_base, sw.DEFAULT_UPSTREAM)
        unset.close()

    # --- fixed HTTPS allowlisted origin ------------------------------------

    def test_production_upstream_accepts_the_real_allowlisted_origin(self):
        with mock.patch.dict(os.environ, {"SURFACE_WATER_CACHE_UPSTREAM": sw.DEFAULT_UPSTREAM}):
            p = sw.SurfaceWaterCacheProxy(self.db_path + ".ok")
            self.assertEqual(p.upstream_base, sw.DEFAULT_UPSTREAM)
            p.close()

    def test_production_upstream_rejects_non_https_scheme(self):
        bad = sw.DEFAULT_UPSTREAM.replace("https://", "http://")
        with mock.patch.dict(os.environ, {"SURFACE_WATER_CACHE_UPSTREAM": bad}):
            with self.assertRaises(ValueError):
                sw.SurfaceWaterCacheProxy(self.db_path + ".bad1")

    def test_production_upstream_rejects_alternate_host(self):
        bad = "https://evil.example.com/global-surface-water/downloads2021/occurrence/"
        with mock.patch.dict(os.environ, {"SURFACE_WATER_CACHE_UPSTREAM": bad}):
            with self.assertRaises(ValueError):
                sw.SurfaceWaterCacheProxy(self.db_path + ".bad2")

    def test_production_upstream_rejects_userinfo(self):
        bad = "https://attacker@storage.googleapis.com/global-surface-water/downloads2021/occurrence/"
        with mock.patch.dict(os.environ, {"SURFACE_WATER_CACHE_UPSTREAM": bad}):
            with self.assertRaises(ValueError):
                sw.SurfaceWaterCacheProxy(self.db_path + ".bad3")

    def test_production_upstream_rejects_changed_path(self):
        bad = "https://storage.googleapis.com/some-other-bucket/path/"
        with mock.patch.dict(os.environ, {"SURFACE_WATER_CACHE_UPSTREAM": bad}):
            with self.assertRaises(ValueError):
                sw.SurfaceWaterCacheProxy(self.db_path + ".bad4")

    def test_direct_constructor_injection_bypasses_production_validation(self):
        # This is exactly what every other test in this file relies on: a local
        # loopback mock passed directly as upstream_base, never through the
        # SURFACE_WATER_CACHE_UPSTREAM env var production-config path.
        p = sw.SurfaceWaterCacheProxy(self.db_path + ".mock", upstream_base="http://127.0.0.1:1/")
        self.assertEqual(p.upstream_base, "http://127.0.0.1:1/")
        p.close()

    # --- path / range validation --------------------------------------------

    def test_rejects_non_file_and_malformed_names(self):
        for bad in ["/", "/not-a-tif.tif", "/occurrence_999E_50Nv1_4_2021.tif", "/a/b"]:
            status, _, _ = self.proxy.serve_range(bad, "bytes=0-7", None)
            self.assertEqual(status, 404, bad)

    def test_requires_range_header(self):
        _FakeUpstream.FILES[self.FILE] = b"0123456789"
        status, _, _ = self.proxy.serve_range("/" + self.FILE, None, None)
        self.assertEqual(status, 400)

    # --- cache correctness ---------------------------------------------------

    def test_cold_then_warm_byte_range(self):
        body = bytes(range(256)) * 4  # 1024 distinguishable bytes
        _FakeUpstream.FILES[self.FILE] = body

        status, headers, chunk = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status, 206)
        self.assertEqual(chunk, body[0:8])
        self.assertEqual(self.proxy.stats.as_dict()["upstream_fills"], 1)
        self.assertEqual(self.proxy.stats.as_dict()["cache_misses"], 1)
        self.assertEqual(self.proxy.stats.as_dict()["cache_hits"], 0)
        self.assertEqual(len(_FakeUpstream.requests), 1)

        status, headers2, chunk2 = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status, 206)
        self.assertEqual(chunk2, body[0:8])
        self.assertEqual(self.proxy.stats.as_dict()["upstream_fills"], 1, "warm read must not re-hit upstream")
        self.assertEqual(self.proxy.stats.as_dict()["cache_hits"], 1)
        self.assertEqual(len(_FakeUpstream.requests), 1)
        self.assertEqual(headers["ETag"], headers2["ETag"])

    def test_different_ranges_are_different_cache_keys(self):
        body = bytes(range(256)) * 4
        _FakeUpstream.FILES[self.FILE] = body
        self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.proxy.serve_range("/" + self.FILE, "bytes=8-15", None)
        self.assertEqual(self.proxy.stats.as_dict()["upstream_fills"], 2)
        self.assertEqual(len(_FakeUpstream.requests), 2)

    def test_conditional_not_modified(self):
        _FakeUpstream.FILES[self.FILE] = b"abcdefgh"
        status, headers, _ = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        etag = headers["ETag"]
        status2, _, body2 = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", etag)
        self.assertEqual(status2, 304)
        self.assertEqual(body2, b"")

    def test_genuine_404_is_cached_as_missing_not_retried_as_success(self):
        # No file registered at all -> upstream genuinely has nothing for this square.
        status, _, body = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")
        self.assertEqual(self.proxy.stats.as_dict()["missing_files"], 1)
        self.assertEqual(self.proxy.stats.as_dict()["upstream_missing"], 1)
        first_hit_count = len(_FakeUpstream.requests)

        # Second read must come from the cached missing-sentinel, not hit upstream again.
        status2, _, _ = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status2, 404)
        self.assertEqual(len(_FakeUpstream.requests), first_hit_count, "a cached 404 must not re-fetch")

    def test_403_is_never_cached_as_missing_or_success(self):
        _FakeUpstream.FORBIDDEN.add(self.FILE)
        status, _, _ = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status, 403)
        self.assertEqual(self.proxy.stats.as_dict()["upstream_forbidden"], 1)
        self.assertEqual(self.proxy.stats.as_dict()["missing_files"], 0)

        # Lifting the 403 must immediately succeed - nothing negative was cached.
        _FakeUpstream.FORBIDDEN.discard(self.FILE)
        _FakeUpstream.FILES[self.FILE] = b"realdata"
        status2, _, body2 = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, b"realdata")

    def test_dropped_connection_is_retried_and_never_cached_as_failure(self):
        _FakeUpstream.FILES[self.FILE] = b"goodbyte"
        _FakeUpstream.DROP_ONCE.add(self.FILE)
        status, _, body = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status, 206, "a transient drop must be retried, not surfaced as the final result")
        self.assertEqual(body, b"goodbyte")
        stats = self.proxy.stats.as_dict()
        self.assertEqual(stats["upstream_failed"], 1, "the dropped first attempt must still be counted")
        self.assertEqual(stats["upstream_fills"], 1)
        self.assertEqual(stats["upstream_attempts"], 2)

    def test_truncated_response_is_never_cached(self):
        _FakeUpstream.FILES[self.FILE] = b"12345678"
        _FakeUpstream.TRUNCATE.add(self.FILE)
        status, _, _ = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status, 502)
        self.assertEqual(self.proxy.stats.as_dict()["cache_hits"], 0)
        self.assertEqual(self.proxy.stats.as_dict()["upstream_failed"], 3, "all 3 retries must be counted")

        # Fixing the upstream must succeed on the very next read - nothing bad was cached.
        _FakeUpstream.TRUNCATE.discard(self.FILE)
        status2, _, body2 = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, b"12345678")

    def test_wrong_content_range_is_never_cached_as_success(self):
        # A misbehaving/misconfigured origin can return the right number of bytes
        # from the WRONG offset in a 206. Content-Length matching alone can't
        # catch this - only validating Content-Range against the exact requested
        # start/end can. If this were cached, a later request for the real range
        # would silently be served the wrong file's bytes.
        body = bytes(range(256)) * 8
        _FakeUpstream.FILES[self.FILE] = body
        _FakeUpstream.WRONG_RANGE.add(self.FILE)
        status, _, _ = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status, 502)
        self.assertEqual(self.proxy.stats.as_dict()["cache_hits"], 0)
        self.assertEqual(self.proxy.stats.as_dict()["cache_misses"], 1)
        self.assertEqual(self.proxy.stats.as_dict()["upstream_fills"], 0, "a Content-Range mismatch must never count as a successful upstream fill")
        self.assertGreaterEqual(self.proxy.stats.as_dict()["upstream_failed"], 1)

        # Fixing the upstream must succeed on the very next read - nothing wrong was cached.
        _FakeUpstream.WRONG_RANGE.discard(self.FILE)
        status2, _, body2 = self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.assertEqual(status2, 206)
        self.assertEqual(body2, body[0:8])

    def test_concurrent_miss_is_deduplicated_single_flight(self):
        body = b"x" * 64
        _FakeUpstream.FILES[self.FILE] = body
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            status, _, chunk = self.proxy.serve_range("/" + self.FILE, "bytes=0-63", None)
            results.append((status, chunk))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(_FakeUpstream.requests), 1, "8 concurrent misses on the same range must be one fetch")
        self.assertEqual(self.proxy.stats.as_dict()["upstream_fills"], 1)
        self.assertTrue(all(status == 206 and chunk == body for status, chunk in results))

    def test_restart_persists_cache_across_proxy_instances(self):
        body = b"persisted"
        _FakeUpstream.FILES[self.FILE] = body
        self.proxy.serve_range("/" + self.FILE, "bytes=0-8", None)
        self.proxy.close()

        reopened = sw.SurfaceWaterCacheProxy(self.db_path, upstream_base=self.upstream_base, max_bytes=1024)
        try:
            _FakeUpstream.requests.clear()
            status, _, chunk = reopened.serve_range("/" + self.FILE, "bytes=0-8", None)
            self.assertEqual(status, 206)
            self.assertEqual(chunk, body)
            self.assertEqual(len(_FakeUpstream.requests), 0, "restart must reuse the on-disk cache, not refetch")
        finally:
            reopened.close()

    def test_bounded_storage_evicts_least_recently_used(self):
        # max_bytes=1024 in setUp; write enough distinct ranges to force eviction.
        body = bytes(range(256))
        _FakeUpstream.FILES[self.FILE] = body * 4
        for start in range(0, 512, 64):
            end = start + 63
            self.proxy.serve_range("/" + self.FILE, f"bytes={start}-{end}", None)
        self.assertLessEqual(self.proxy.cache.total_bytes(), 1024)

    def test_good_data_survives_restart_and_large_clock_advance_with_zero_upstream_calls(self):
        # John's 2026-11-24 cache-lifetime decision on MINECRAFT-571: validated data
        # defaults to TTL -1 (no time-based expiry). Advance time by a year between
        # the write and the read-back; a warm hit must still come from the cache.
        body = b"forever-valid"
        _FakeUpstream.FILES[self.FILE] = body
        self.proxy.serve_range("/" + self.FILE, "bytes=0-12", None)
        self.proxy.close()

        real_time = sw.time.time
        try:
            sw.time.time = lambda: real_time() + 365 * 24 * 3600
            reopened = sw.SurfaceWaterCacheProxy(self.db_path, upstream_base=self.upstream_base, max_bytes=1024)
            try:
                _FakeUpstream.requests.clear()
                status, _, chunk = reopened.serve_range("/" + self.FILE, "bytes=0-12", None)
                self.assertEqual(status, 206)
                self.assertEqual(chunk, body)
                self.assertEqual(len(_FakeUpstream.requests), 0, "TTL -1 data must never expire by elapsed time")
            finally:
                reopened.close()
        finally:
            sw.time.time = real_time

    def test_explicit_invalidation_forces_a_fresh_upstream_fetch(self):
        _FakeUpstream.FILES[self.FILE] = b"version-one"
        self.proxy.serve_range("/" + self.FILE, "bytes=0-10", None)
        self.assertEqual(self.proxy.stats.as_dict()["upstream_fills"], 1)

        removed = self.proxy.invalidate(self.FILE)
        self.assertEqual(removed, 1)

        _FakeUpstream.FILES[self.FILE] = b"version-two"
        status, _, chunk = self.proxy.serve_range("/" + self.FILE, "bytes=0-10", None)
        self.assertEqual(status, 206)
        self.assertEqual(chunk, b"version-two")
        self.assertEqual(self.proxy.stats.as_dict()["upstream_fills"], 2)

    def test_invalidate_all_clears_every_cached_range(self):
        _FakeUpstream.FILES[self.FILE] = b"x" * 32
        self.proxy.serve_range("/" + self.FILE, "bytes=0-7", None)
        self.proxy.serve_range("/" + self.FILE, "bytes=8-15", None)
        removed = self.proxy.invalidate(None)
        self.assertEqual(removed, 2)
        self.assertEqual(self.proxy.cache.total_bytes(), 0)

    # --- observability: attempts/outcomes/bytes/latency ----------------------

    def test_upstream_attempt_accounting_tracks_bytes_and_latency(self):
        body = b"timed-bytes"
        _FakeUpstream.FILES[self.FILE] = body
        self.proxy.serve_range("/" + self.FILE, f"bytes=0-{len(body) - 1}", None)
        stats = self.proxy.stats.as_dict()
        self.assertEqual(stats["upstream_attempts"], 1)
        self.assertEqual(stats["upstream_fills"], 1)
        self.assertGreaterEqual(stats["upstream_bytes"], len(body))
        self.assertGreater(stats["upstream_latency_ms_total"], 0.0)

    # --- health check: dedicated TTL-0 probe, exact-200/206-only success -----

    def test_health_check_succeeds_only_on_exact_matching_response(self):
        _FakeUpstream.FILES[self.FILE] = b"irrelevant-to-data-cache-but-needed-for-health"
        ok, detail = self.proxy.health_check()
        self.assertTrue(ok, detail)

    def test_health_check_treats_403_as_unhealthy_not_green(self):
        _FakeUpstream.FORBIDDEN.add(self.FILE)
        ok, detail = self.proxy.health_check()
        self.assertFalse(ok, "a 403 on the health probe must never read as healthy")

    def test_health_check_treats_404_as_unhealthy_not_green(self):
        # self.FILE deliberately left unregistered -> fake upstream answers 404.
        ok, detail = self.proxy.health_check()
        self.assertFalse(ok, "a 404 on the health probe must never read as healthy")

    def test_health_check_never_touches_data_cache_or_upstream_fetch_counters(self):
        _FakeUpstream.FILES[self.FILE] = b"irrelevant-to-health-" + b"x" * 20
        before = self.proxy.stats.as_dict()
        ok, detail = self.proxy.health_check()
        self.assertTrue(ok, detail)
        after = self.proxy.stats.as_dict()
        for key in ("cache_hits", "cache_misses", "missing_files",
                    "upstream_attempts", "upstream_fills", "upstream_missing",
                    "upstream_forbidden", "upstream_failed", "upstream_bytes"):
            self.assertEqual(before[key], after[key], f"health check must not change {key}")
        self.assertEqual(after["health_checks_total"], 1)
        self.assertEqual(after["health_checks_healthy"], 1)
        self.assertEqual(self.proxy.cache.total_bytes(), 0, "health check must never write to the data cache")

    def test_warm_tile_serves_from_cache_while_health_reports_upstream_down_then_recovers(self):
        # John's acceptance requirement (MINECRAFT-571 comment 55734): warm the
        # ordinary cache, make upstream unavailable, verify the cached read still
        # hits SQLite while the separate health endpoint truthfully reports the
        # outage - then restore upstream and confirm a fresh probe succeeds.
        body = b"warmed-before-outage"
        _FakeUpstream.FILES[self.FILE] = body
        status, _, chunk = self.proxy.serve_range("/" + self.FILE, f"bytes=0-{len(body) - 1}", None)
        self.assertEqual(status, 206)
        self.assertEqual(chunk, body)

        self.upstream.shutdown()
        self.upstream.server_close()

        _FakeUpstream.requests.clear()
        status2, _, chunk2 = self.proxy.serve_range("/" + self.FILE, f"bytes=0-{len(body) - 1}", None)
        self.assertEqual(status2, 206, "a warm tile must still be served from SQLite while upstream is down")
        self.assertEqual(chunk2, body)
        self.assertEqual(len(_FakeUpstream.requests), 0)

        ok, detail = self.proxy.health_check()
        self.assertFalse(ok, "health must truthfully report the outage, never substitute a stale success")

        # Restart upstream on the same address and confirm a fresh probe recovers.
        self.upstream = http.server.ThreadingHTTPServer(self.upstream.server_address, _FakeUpstream)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        ok2, detail2 = self.proxy.health_check()
        self.assertTrue(ok2, detail2)

    def test_health_check_reports_unreachable_host(self):
        unreachable = sw.SurfaceWaterCacheProxy(
            self.db_path + ".unreachable", upstream_base="http://127.0.0.1:1/", max_bytes=1024
        )
        try:
            ok, detail = unreachable.health_check()
            self.assertFalse(ok, detail)
        finally:
            unreachable.close()

    # --- live HTTP server wiring ---------------------------------------------

    def test_live_http_server_health_and_invalidate_endpoints(self):
        body = b"B" * 16
        _FakeUpstream.FILES[self.FILE] = body
        server, live_proxy = sw.run_server(
            self.db_path + ".live2", 0, upstream_base=self.upstream_base
        )
        live_proxy.health_check_file = self.FILE
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/__health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            import json
            self.assertTrue(json.loads(resp.read())["healthy"])

            conn.request("GET", "/" + self.FILE, headers={"Range": "bytes=0-15"})
            conn.getresponse().read()

            conn.request("POST", "/__invalidate?path=" + self.FILE)
            resp2 = conn.getresponse()
            self.assertEqual(resp2.status, 200)
            self.assertEqual(json.loads(resp2.read())["invalidated"], 1)
        finally:
            server.shutdown()
            live_proxy.close()

    def test_live_http_server_range_and_stats_endpoint(self):
        body = b"A" * 32
        _FakeUpstream.FILES[self.FILE] = body
        server, live_proxy = sw.run_server(self.db_path + ".live", 0, upstream_base=self.upstream_base)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/" + self.FILE, headers={"Range": "bytes=0-7"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.read(), body[0:8])

            conn.request("GET", "/__stats")
            resp2 = conn.getresponse()
            self.assertEqual(resp2.status, 200)
            import json
            stats = json.loads(resp2.read())
            self.assertEqual(stats["upstream_fills"], 1)
        finally:
            server.shutdown()
            live_proxy.close()


if __name__ == "__main__":
    unittest.main()
