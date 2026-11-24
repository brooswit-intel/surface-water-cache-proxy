# surface-water-cache-proxy

Lane for MINECRAFT-573 / MINECRAFT-571: one independently deployable,
loopback-only, SQLite-backed cache for Earth Platform's JRC Global Surface
Water occurrence layer.

Pinned source: https://github.com/pirut/earth-platform, tag `combined-73`
(= release "Earth Platform 0.1.0-alpha.73", commit `88b13c33954550c6ac660dceb743a4288a9aafac`).
Endpoint inventory below was read from that exact tag (`SurfaceWaterSource.java`,
`CubicSettings.java:surfaceWater()`), not `main`.

## Status

`brooswit-intel/surface-water-cache-proxy` does not exist yet (confirmed by a
fresh read-only check and by Wroosbit's source→issue→repo→owner matrix on
MINECRAFT-571). Tayne owns creating it and granting this agent's GitHub
account (`booswrit`) repo-only Write. This tree is ready to push verbatim once
that lands; work continues locally in the meantime.

## Run

```
python3 -m unittest test_proxy -v     # 20 tests, local mock upstream only
python3 proxy.py                      # serves on 127.0.0.1:8075 by default
```

## Cache lifetime (per John's 2026-11-24 decision on MINECRAFT-571, applies to every source lane)

Validated data has no time-based expiry (TTL -1): once a byte range is cached
as a successful response, it is served from SQLite regardless of elapsed
wall-clock time (verified by a test that advances the clock a year between
write and read-back). This is not the same as immutable:

- `GET /__health` is a separate, TTL-0 endpoint: a fresh live reachability
  probe (`bytes=0-0` against the upstream base) on every call, never reading
  or writing the data cache.
- `POST /__invalidate[?path=<file>]` explicitly clears one file's cached
  ranges, or the whole cache when `path` is omitted - the escape hatch for a
  dataset revision, since nothing expires on its own.
- Failures, 403s, rate limits and truncated payloads still never enter the
  permanent cache in the first place (unchanged from before this decision);
  only a genuine 404 is cached, as a distinct "missing" sentinel.
- Byte caps/LRU eviction still bound disk usage despite no time expiry.

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
