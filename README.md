# surface-water-cache-proxy

Lane for MINECRAFT-573 / MINECRAFT-571: one independently deployable,
loopback-only, SQLite-backed cache for Earth Platform's JRC Global Surface
Water occurrence layer.

Pinned source: https://github.com/pirut/earth-platform, tag `combined-73`
(= release "Earth Platform 0.1.0-alpha.73", commit `88b13c33954550c6ac660dceb743a4288a9aafac`).
Endpoint inventory below was read from that exact tag (`SurfaceWaterSource.java`,
`CubicSettings.java:surfaceWater()`), not `main`.

## Status

Pushed to `brooswit-intel/surface-water-cache-proxy` main. Reviewed once
(CHANGES_REQUESTED on 3 findings: health-check false-positive on 403/404,
unvalidated upstream origin, incomplete observability); all 3 addressed below
and awaiting re-review.

## Run

```
python3 -m unittest test_proxy -v     # 32 tests, local mock upstream only
python3 proxy.py                      # serves on 127.0.0.1:8075 by default
```

## Fixed HTTPS allowlisted origin

`SURFACE_WATER_CACHE_UPSTREAM`, when read from the environment (the
production-config path `run_server`/`__main__` use), is validated against the
exact approved origin before use: scheme must be `https`, host must be
exactly `storage.googleapis.com` with no port, no userinfo, and the path must
start with `/global-surface-water/downloads2021/occurrence/`. Anything else
raises `ValueError` at startup rather than silently proxying to an arbitrary
or private destination. Passing `upstream_base` directly to the constructor
(what every test does, to point at a local loopback mock) deliberately
bypasses this check — that is the test-only injection path, never how a real
deployment's config is read.

## Health check: dedicated TTL-0 probe, exact-success-only

`GET /__health` is a separate, TTL-0 endpoint, independent of the TTL -1 data
cache below: a fresh live probe on every call, never reading or writing the
data cache, never incrementing any `upstream_*`/`cache_*` counter (its own
`health_checks_total/healthy/unhealthy` counters are tracked separately).
It issues a `bytes=0-7` range request against a fixed representative file
(`DEFAULT_HEALTH_CHECK_FILE`, independently confirmed present upstream) -
not the bare base path, which isn't itself a real resource and would 403 on
a listing-disabled bucket, telling the probe nothing. **Healthy means exactly
HTTP 206 with a body matching the requested range** — a 403, 404, 5xx, any
other status, a mismatched/short body, or a connection failure are all
unhealthy, so a wrong route or an access rejection is never reported green.

## Cache lifetime (per John's 2026-11-24 decision on MINECRAFT-571, applies to every source lane)

Validated data has no time-based expiry (TTL -1): once a byte range is cached
as a successful response, it is served from SQLite regardless of elapsed
wall-clock time (verified by a test that advances the clock a year between
write and read-back). This is not the same as immutable:

- `POST /__invalidate[?path=<file>]` explicitly clears one file's cached
  ranges, or the whole cache when `path` is omitted - the escape hatch for a
  dataset revision, since nothing expires on its own.
- Failures, 403s, rate limits and truncated payloads still never enter the
  permanent cache in the first place (unchanged from before this decision);
  only a genuine 404 is cached, as a distinct "missing" sentinel.
- Byte caps/LRU eviction still bound disk usage despite no time expiry.

## Observability

`GET /__stats` reports, distinct from each other:
- `cache_hits`/`cache_misses`/`not_modified`/`evictions`/`missing_files`: the
  data-cache side.
- `upstream_attempts`/`upstream_fills`/`upstream_missing`/`upstream_forbidden`/
  `upstream_failed`/`upstream_bytes`/`upstream_latency_ms_total`: **every**
  real HTTP attempt made to upstream, by outcome, not just the ones that end
  in a successful fill — a retried 5xx, a truncated transfer, a
  Content-Range mismatch and a 403/404 are each one counted attempt, with
  their own bytes-received and latency.
- `health_checks_total`/`health_checks_healthy`/`health_checks_unhealthy`:
  the `/__health` probe's own counters, which never move any of the fields
  above.

## Upstream contract (from SurfaceWaterSource.java)

Unlike Terrarium elevation (whole-file GET), the mod **never** does a
whole-file GET here: it reads an 8-byte TIFF header, a directory at an offset
named in that header, two block-offset/count tables, then individual 256x256
tile blocks (LZW/deflate/raw) — all via HTTP byte-range GET
(`geography/.../RangeFiles.java`), one classic tiled TIFF per 10x10 degree
square, 80N to 60S. This proxy mirrors that: it caches **exact byte ranges**,
never a whole file — files can be hundreds of MB and the mod only ever touches
a handful of specific ranges per square it actually visits.

- 404 for a square's file → genuine absence (`Index.MISSING` on the mod side)
  → cached as a zero-length "missing" sentinel, distinct from a present-but-
  empty range.
- 403 → **never** cached as missing or success; it can mean a terms/access
  problem, not genuine absence (see MINECRAFT-573/ MINECRAFT-571's explicit
  instruction to distinguish genuine absence from transient/authorization
  failure). Surfaced to the caller as 403, retried on the next request.
- Any other non-2xx status, a dropped connection, or a body whose length
  doesn't match the requested range / declared `Content-Length` is a transient
  failure: retried 3x with backoff, never written to the cache under any
  circumstance.

## Contract

- Loopback-only bind (`127.0.0.1`, hardcoded in `run_server`).
- Fixed, allowlisted upstream:
  `https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/`
  by default (unchanged from `SurfaceWaterSource.JRC_GSW`). Override via
  `SURFACE_WATER_CACHE_UPSTREAM` — the default is untouched unless a caller
  sets it.
- Request path is validated against the exact
  `occurrence_{lon}{E|W}_{lat}{N|S}v1_4_2021.tif` filename grid the mod itself
  generates (`SurfaceWaterSource.name()`) and refused otherwise — no
  arbitrary URL forwarding.
- Only a single `bytes=start-end` `Range` request is accepted — the one shape
  the mod ever sends; a request without `Range` is refused (400) rather than
  guessing a whole-file response no real caller needs.
- SQLite file cache, keyed by `(path, start, end)`, restart-persistent.
- A stable SHA-256 `ETag` per cached range supports `If-None-Match` → 304.
- Concurrent cache misses for the same exact range single-flight to exactly
  one upstream request.
- Bounded storage: `max_bytes`, LRU eviction down to 90% on overflow.
- If upstream ignores the Range header and returns 200 with the whole file,
  the proxy slices the requested range out locally before caching/serving it
  — the same fallback `RangeFiles.transfer()` uses on the Java side.

## Attribution / terms (verified before any live use)

`ATTRIBUTION` in `proxy.py` carries, unmodified, the same string the mod
itself embeds (`SurfaceWaterSource.ATTRIBUTION`): "JRC Global Surface Water
v1.4 (Source: EC JRC/Google; Pekel et al. 2016, Nature 540, 418-422),
Copernicus Programme". Per the JRC/Copernicus Data Users Guide, this data is
provided free of charge without restriction of use, subject to that citation
— no quota, paywall or republishing restriction was found that would block
caching. This proxy forwards/preserves attribution; it never strips it.

## Mod-side wiring (earth-platform fork, local branch off `combined-73`)

`CubicSettings.surfaceWater()` has exactly one call site constructing
`SurfaceWaterSource`, hardcoded to `SurfaceWaterSource.JRC_GSW` — simpler than
Terrarium's two independent call sites. Needs the same per-service override
pattern already established for elevation: a `surfaceWaterBase()` helper read
from a system property (e.g. `-Dearthplatform.earth.surfaceWaterBase`),
defaulting to the unchanged real endpoint when unset, pointed at this proxy's
`127.0.0.1:8075` for the A/B.

## What this is not (yet)

- Not tested against the live GCS endpoint: pending explicit terms-verified
  live-use sign-off per the ticket, all tests run against a local mock
  upstream serving the identical range-request contract (genuine 404, 403,
  dropped connection, truncated `Content-Length`, Range-ignored-200 fallback).
- Mod-side `surfaceWaterBase()` wiring is designed above but not yet
  committed/pushed — see Status.
- No Zippy/in-game A/B run: out of scope for this agent per the ticket (no
  Zippy/profile/save/release/merge action); Tayne owns that handoff.
