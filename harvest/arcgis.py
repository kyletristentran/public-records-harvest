"""ArcGIS REST client on top of the harvest core.

Consolidates the two shapes these clients grow into: small point queries against a
parcel layer, and bulk OID-windowed harvest of a whole county.

The traps, all of which are enforced here rather than documented and re-hit:

1. **ArcGIS returns HTTP 200 with an `error` body.** Status alone is not success.
2. **`resultRecordCount` truncates silently.** A broad query returns the first N
   with no flag. `query()` raises `TruncatedResponse` when the server sets
   `exceededTransferLimit` or returns exactly the cap, unless you pass `allow_partial`.
3. **`objectIds=` longer than `maxRecordCount` truncates too.** `fetch_by_oids`
   reads the server's own `maxRecordCount`, clamps to it, and raises when a chunk
   comes back short.
4. **Never point-in-polygon a geocoded address.** `centroid_of` exists so spatial
   work runs at the parcel centroid; `query(geometry=...)` marks its results
   `low_confidence` so the distinction survives into the data.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from .core import Fetcher, FetchError, Request, TruncatedResponse

WGS84 = 4326


def _payload(f: Fetcher, url: str, params: dict, *, post: bool = False) -> dict:
    resp = (f.post(url, data=params) if post else f.get(url, params=params))
    try:
        out = resp.json()
    except Exception as exc:
        raise FetchError(f"{url}: response was not JSON ({exc})") from exc
    if isinstance(out, dict) and "error" in out:
        raise FetchError(f"ArcGIS error from {url}: {out['error']}")
    return out


def layer_info(f: Fetcher, layer_url: str) -> dict:
    return _payload(f, layer_url, {"f": "json"})


def max_record_count(f: Fetcher, layer_url: str, fallback: int = 1000) -> int:
    try:
        return int(layer_info(f, layer_url).get("maxRecordCount") or fallback)
    except FetchError:
        return fallback


def count(f: Fetcher, layer_url: str, where: str = "1=1") -> int:
    out = _payload(f, f"{layer_url}/query",
                   {"where": where, "returnCountOnly": "true", "f": "json"})
    return int(out.get("count", 0))


def query(f: Fetcher, layer_url: str, *, where: str | None = None,
          geometry: tuple[float, float] | None = None, out_fields: str = "*",
          limit: int = 2000, geom: bool = False, centroid: bool = False,
          distance: float = 0, allow_partial: bool = False) -> list[dict]:
    """One page of features. Raises rather than returning [] on failure.

    `allow_partial=False` (the default) turns silent truncation into an exception —
    the single highest-value behaviour change over the code this replaces.
    """
    p: dict[str, Any] = {"outFields": out_fields, "f": "json",
                         "resultRecordCount": limit,
                         "returnGeometry": "true" if (geom or centroid) else "false"}
    if where:
        p["where"] = where
    if geometry:
        lon, lat = geometry
        p.update({"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
                  "inSR": WGS84, "spatialRel": "esriSpatialRelIntersects"})
        if distance:
            p.update({"distance": distance, "units": "esriSRUnit_Meter"})
    if centroid:
        p.update({"returnCentroid": "true", "outSR": WGS84})

    out = _payload(f, f"{layer_url}/query", p)
    feats = out.get("features", [])
    if not allow_partial and (out.get("exceededTransferLimit") or len(feats) >= limit):
        raise TruncatedResponse(
            f"{layer_url}: {len(feats)} features at the {limit}-record cap "
            f"(exceededTransferLimit={out.get('exceededTransferLimit')}). "
            "Narrow the WHERE clause or use fetch_all()."
        )
    return feats


def fetch_oids(f: Fetcher, layer_url: str, where: str = "1=1",
               bbox: Sequence[float] | None = None) -> list[int]:
    """The cheap index-only read that makes bulk harvest resumable.

    OID windowing beats resultOffset paging: the server re-scans from row 0 on
    every offset request, so late pages of a 2.4M-feature layer cost far more than
    early ones, and some services silently cap the maximum offset.
    """
    params: dict[str, Any] = {"where": where, "returnIdsOnly": "true", "f": "json"}
    if bbox:
        params.update(geometry=",".join(str(v) for v in bbox),
                      geometryType="esriGeometryEnvelope", inSR=WGS84,
                      spatialRel="esriSpatialRelIntersects")
    out = _payload(f, f"{layer_url}/query", params, post=True)
    oids = out.get("objectIds") or []
    if not oids:
        raise FetchError(f"{layer_url}: no ObjectIDs for where={where!r}")
    return sorted(int(o) for o in oids)


def fetch_by_oids(f: Fetcher, layer_url: str, oids: Sequence[int], *,
                  out_fields: str = "*", geometry: bool = True,
                  fmt: str = "geojson", allow_short: bool = False) -> dict:
    """POST one OID chunk. Short response = silent loss, so it raises by default."""
    out = _payload(f, f"{layer_url}/query", {
        "objectIds": ",".join(map(str, oids)), "outFields": out_fields,
        "returnGeometry": "true" if geometry else "false",
        "outSR": WGS84, "geometryPrecision": 6, "f": fmt,
    }, post=True)
    got = len(out.get("features") or [])
    if got < len(oids) and not allow_short:
        raise TruncatedResponse(
            f"{layer_url}: chunk returned {got}/{len(oids)} features "
            f"(oid {oids[0]}..{oids[-1]}). Clamp to maxRecordCount or pass allow_short."
        )
    return out


def fetch_all(f: Fetcher, layer_url: str, *, where: str = "1=1", out_fields: str = "*",
              geometry: bool = True, chunk: int | None = None,
              bbox: Sequence[float] | None = None, log=lambda *_: None) -> Iterable[dict]:
    """Every feature matching `where`, chunk by chunk, clamped to the server's own max."""
    oids = fetch_oids(f, layer_url, where=where, bbox=bbox)
    cap = max_record_count(f, layer_url, fallback=chunk or 1000)
    size = min(chunk or cap, cap)
    log(f"{len(oids):,} OIDs, chunk={size} (server max {cap})")
    for i in range(0, len(oids), size):
        window = oids[i:i + size]
        yield fetch_by_oids(f, layer_url, window, out_fields=out_fields, geometry=geometry)


def centroid_of(f: Fetcher, layer_url: str, apn_field: str, apns: Sequence[str],
                *, per_parcel: bool = True) -> list[tuple[float, float]] | tuple[float, float] | None:
    """Parcel centroids in EPSG:4326 — the call that replaces the geocoded point.

    `per_parcel=True` returns one centroid per matched parcel. **Do not average
    them across a multi-parcel site**: on a site that wraps a corner the mean lands
    in the road, and every downstream zoning read is then taken at the wrong point.
    """
    if not apns:
        return [] if per_parcel else None
    ins = ",".join("'%s'" % a for a in apns[:40])
    feats = query(f, layer_url, where=f"{apn_field} IN ({ins})", out_fields=apn_field,
                  limit=max(len(apns), 40), centroid=True, allow_partial=True)
    pts: list[tuple[float, float]] = []
    for ft in feats:
        c = ft.get("centroid") or {}
        if c.get("x") is not None:
            pts.append((round(c["y"], 6), round(c["x"], 6)))
            continue
        rings = (ft.get("geometry") or {}).get("rings")      # older MapServers ignore returnCentroid
        if rings:
            r = rings[0]
            pts.append((round(sum(p[1] for p in r) / len(r), 6),
                        round(sum(p[0] for p in r) / len(r), 6)))
    if per_parcel:
        return pts
    if not pts:
        return None
    return (round(sum(p[0] for p in pts) / len(pts), 6),
            round(sum(p[1] for p in pts) / len(pts), 6))


def doctor(f: Fetcher, layer_url: str, want_fields: Sequence[str] = ()) -> dict:
    """Cheap liveness + schema-drift check. Run before committing to a long pull."""
    r: dict[str, Any] = {"layer_url": layer_url}
    try:
        info = layer_info(f, layer_url)
        have = {fld["name"] for fld in info.get("fields", [])}
        r.update(name=info.get("name"), geometry_type=info.get("geometryType"),
                 max_record_count=info.get("maxRecordCount"),
                 supports_pagination=(info.get("advancedQueryCapabilities") or {}).get("supportsPagination"),
                 missing_fields=sorted(set(want_fields) - have),
                 count=count(f, layer_url))
        r["ok"] = not r["missing_fields"] and r["count"] > 0
    except Exception as exc:
        r.update(ok=False, error=str(exc))
    return r
