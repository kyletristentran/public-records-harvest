# public-records-harvest

A shared fetch layer for public-records scrapers. It provides an on-disk cache, per-host rate limiting, retry with backoff and a provenance ledger, all behind a transport that can be swapped out. It also includes an ArcGIS REST client that raises exceptions in the cases where ArcGIS normally fails without reporting an error.

```python
from harvest import build, registry, arcgis

f = registry.apply(build(".harvest", user_agent="my-research/1.0 (you@example.com)"))

arcgis.doctor(f, LAYER)                        # liveness + schema drift, before a long pull
arcgis.count(f, LAYER, where="CITY='ONTARIO'")
for chunk in arcgis.fetch_all(f, LAYER, where="USE_CODE LIKE '3%'"):
    ...                                        # clamped to the server's maxRecordCount
```

## Problems it addresses

The package addresses four defects that are common in scrapers built up over time.

- **Failed fetches returned as empty results.** The client this package replaced caught every exception and returned `[]`, so a timeout and a query with no matches produced the same value. Rows ended up in the unresolved bucket and the run appeared to succeed. Every function here raises on failure, and a caller that wants a default has to ask for one.
- **Truncated results.** ArcGIS caps a response at `maxRecordCount` and reports the cap only through `exceededTransferLimit`, which is easy to overlook. An `objectIds=` list that is too long is cut off in the same way. Both cases raise `TruncatedResponse`.
- **Errors returned with HTTP 200.** ArcGIS places some failures in the response body under a 200 status, so `raise_for_status()` does not catch them. The client inspects the body.
- **Rate limits applied per thread.** When each worker calls `time.sleep()`, N workers reach the host N times faster than intended. This package keeps one token bucket per host and shares it across threads.

## Provenance and replay

Every response is cached by request hash and recorded in `ledger.jsonl` with a timestamp, a body hash and a byte count. A complete run can therefore be replayed without network access:

```python
f = build(".harvest", user_agent="…", offline=True)   # a cache miss raises
```

The ledger also keeps derived data auditable, because any value can be traced to the request that produced it without fetching again.

## Layout

```
harvest/core.py       Transport, Fetcher, Cache, RateLimiter, Ledger, RetryPolicy
harvest/arcgis.py     layer_info · count · query · fetch_oids · fetch_by_oids
                      fetch_all · centroid_of · doctor
harvest/registry.py   per-host politeness, declared once
adapters/             signature-compatible shim for migrating an existing client
```

`fetch_all` pages by ObjectID window instead of `resultOffset`. With offset paging the server scans from row 0 on every request, so the later pages of a layer with several million features cost much more than the early ones, and some services cap the maximum offset without reporting it. `fetch_all` requests the ObjectID list once and then asks for explicit windows. Each request then costs O(chunk), and a run that crashes can resume where it stopped.

## Tests

```bash
uv sync --extra dev && uv run pytest -q      # 20 tests, no network, no fixtures
```

The suite runs against a scripted fake transport. It covers the rate limiter, using a virtual clock, and every ArcGIS failure mode listed above.

## Scope

The package is intended for open government endpoints such as SEC EDGAR, the Census, county assessor GIS and regional planning layers. It does not support authentication. Check a site's `robots.txt` and terms of use before adding a new host, and set a User-Agent that includes a real contact address, because some hosts require one.

## License

MIT
