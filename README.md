# public-records-harvest

[![CI](https://github.com/kyletristentran/public-records-harvest/actions/workflows/ci.yml/badge.svg)](https://github.com/kyletristentran/public-records-harvest/actions/workflows/ci.yml)

One fetch path for public-records scrapers — on-disk cache, per-host rate limiting,
retry with backoff, and a provenance ledger, under a swappable transport. Plus an
ArcGIS REST client that turns the format's silent failure modes into exceptions.

```python
from harvest import build, registry, arcgis

f = registry.apply(build(".harvest", user_agent="my-research/1.0 (you@example.com)"))

arcgis.doctor(f, LAYER)                        # liveness + schema drift, before a long pull
arcgis.count(f, LAYER, where="CITY='ONTARIO'")
for chunk in arcgis.fetch_all(f, LAYER, where="USE_CODE LIKE '3%'"):
    ...                                        # clamped to the server's maxRecordCount
```

## Why

Scrapers that grow organically tend to share four defects. This package exists to
make each one impossible rather than documented:

**A failed fetch is not an empty result.** The client this replaced caught every
exception and returned `[]`, so a timeout and "nothing matched" were the same value.
Rows silently landed in the unresolved bucket and the run looked clean. Everything
here raises; callers opt into a default explicitly.

**Truncation is loud.** ArcGIS caps results at `maxRecordCount` and tells you only
via `exceededTransferLimit`, which is easy to never read. An over-long `objectIds=`
list truncates the same way. Both raise `TruncatedResponse`.

**An HTTP 200 can be an error.** ArcGIS returns failures in the body with a 200
status, so `raise_for_status()` alone lets them through.

**Rate limits belong to the host, not the thread.** A `time.sleep()` inside each
worker means N workers hit the host N times faster than intended. Here it's one
token bucket per host, shared across threads.

## Provenance and replay

Every response is recorded to `ledger.jsonl` with a timestamp, body hash and byte
count, and cached by request hash. That makes a whole run replayable with no network:

```python
f = build(".harvest", user_agent="…", offline=True)   # a cache miss raises
```

Which is also how the derived data stays auditable — any value traces back to the
request that produced it without re-fetching.

## Layout

```
harvest/core.py       Transport, Fetcher, Cache, RateLimiter, Ledger, RetryPolicy
harvest/arcgis.py     layer_info · count · query · fetch_oids · fetch_by_oids
                      fetch_all · centroid_of · doctor
harvest/registry.py   per-host politeness, declared once
adapters/             signature-compatible shim for migrating an existing client
```

`fetch_all` uses OID windowing rather than `resultOffset` paging: the server re-scans
from row 0 on every offset request, so late pages of a multi-million-feature layer
cost far more than early ones, and some services silently cap the maximum offset.
Asking once for the ObjectID list and requesting explicit windows makes every request
O(chunk) and makes a crashed run resumable.

## Tests

```bash
uv sync --extra dev && uv run pytest -q      # 20 tests, no network, no fixtures
```

The entire suite runs against a scripted fake transport, including the rate limiter
(virtual clock) and every ArcGIS failure mode.

## Scope

Public records only — this is built for open government endpoints (SEC EDGAR, Census,
county assessor GIS, regional planning layers). It has no auth support by design.
Check a site's `robots.txt` and terms before pointing it anywhere new, and set a
User-Agent with a real contact address: some hosts require one.

MIT licensed.
