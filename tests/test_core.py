"""Every test runs against a fake transport — no network, no fixtures to refresh."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harvest import arcgis                                      # noqa: E402
from harvest.core import (Cache, Fetcher, FetchError, Ledger, RateLimiter,  # noqa: E402
                          Request, Response, RetryPolicy, TruncatedResponse)


class Fake:
    """Scripted transport. `plan` is a list of Responses or Exceptions, popped in order."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.seen: list[Request] = []

    def send(self, req):
        self.seen.append(req)
        item = self.plan.pop(0) if self.plan else Response(200, b"{}")
        if isinstance(item, Exception):
            raise item
        return item


def ok(payload, status=200, headers=None):
    return Response(status, json.dumps(payload).encode(), headers or {})


def fetcher(plan, tmp_path=None, **kw):
    f = Fetcher(Fake(plan),
                cache=Cache(tmp_path / "cache") if tmp_path else None,
                ledger=Ledger(tmp_path / "ledger.jsonl") if tmp_path else None,
                retry=RetryPolicy(attempts=3, backoff=0.0, jitter=0.0), **kw)
    f._sleep = lambda *_: None
    f.limiter._sleep = lambda *_: None
    return f


# ------------------------------------------------------------------ retry / errors

def test_retries_then_succeeds():
    f = fetcher([Response(503, b""), ok({"ok": True})])
    assert f.get("https://x.test/a").json() == {"ok": True}
    assert len(f.transport.seen) == 2


def test_gives_up_and_raises_instead_of_returning_empty():
    """The behaviour change that matters: the old client returned [] here."""
    f = fetcher([TimeoutError("boom")] * 3)
    with pytest.raises(FetchError):
        f.get("https://x.test/a")


def test_client_error_is_not_retried():
    f = fetcher([Response(404, b"nope"), ok({"never": "reached"})])
    with pytest.raises(FetchError):
        f.get("https://x.test/missing")
    assert len(f.transport.seen) == 1


# -------------------------------------------------------------------------- cache

def test_second_call_is_served_from_cache(tmp_path):
    f = fetcher([ok({"n": 1})], tmp_path)
    a = f.get("https://x.test/a")
    b = f.get("https://x.test/a")
    assert (a.from_cache, b.from_cache) == (False, True)
    assert b.json() == {"n": 1}
    assert len(f.transport.seen) == 1


def test_param_order_does_not_split_the_cache(tmp_path):
    f = fetcher([ok({"n": 1})], tmp_path)
    f.get("https://x.test/q", params={"a": 1, "b": 2})
    f.get("https://x.test/q", params={"b": 2, "a": 1})
    assert len(f.transport.seen) == 1


def test_conditional_revalidate_uses_304(tmp_path):
    f = fetcher([ok({"n": 1}, headers={"ETag": "W/\"v1\""}), Response(304, b"")], tmp_path)
    f.get("https://x.test/a")
    again = f.get("https://x.test/a", revalidate=True)
    assert again.from_cache and again.json() == {"n": 1}
    assert f.transport.seen[-1].headers["If-None-Match"] == 'W/"v1"'


def test_offline_replays_cache_and_refuses_a_miss(tmp_path):
    warm = fetcher([ok({"n": 1})], tmp_path)
    warm.get("https://x.test/a")
    cold = fetcher([], tmp_path, offline=True)
    assert cold.get("https://x.test/a").json() == {"n": 1}
    with pytest.raises(FetchError):
        cold.get("https://x.test/never-fetched")


# ---------------------------------------------------------------------- provenance

def test_ledger_records_every_fetch(tmp_path):
    f = fetcher([Response(503, b""), ok({"n": 1})], tmp_path)
    f.get("https://x.test/a")
    f.get("https://x.test/a")
    rows = f.ledger.rows()
    assert [r["from_cache"] for r in rows][-1] is True
    assert all(r["key"] == rows[0]["key"] for r in rows)
    assert rows[-1]["bytes"] > 0


# --------------------------------------------------------------------- rate limit

def test_one_bucket_per_host_regardless_of_threads():
    """The old code slept per worker, so N workers meant N times the rate."""
    clock, slept = [0.0], []
    rl = RateLimiter(default_rps=2.0)           # one request every 0.5s
    rl._now = lambda: clock[0]
    rl._sleep = lambda s: (slept.append(round(s, 3)), clock.__setitem__(0, clock[0] + s))
    for _ in range(4):
        rl.acquire("a.test")
    assert slept == [0.5, 0.5, 0.5]             # first is free, then spaced
    rl.configure("b.test", 100)
    assert rl.acquire("b.test") == 0.0          # a different host is independent


# ------------------------------------------------------------------------- arcgis

def test_arcgis_error_inside_a_200_is_an_error():
    f = fetcher([ok({"error": {"code": 400, "message": "Invalid where"}})] * 3)
    with pytest.raises(FetchError):
        arcgis.count(f, "https://gis.test/Layer/0")


def test_query_raises_on_silent_truncation():
    f = fetcher([ok({"features": [{"a": 1}] * 2000, "exceededTransferLimit": True})])
    with pytest.raises(TruncatedResponse):
        arcgis.query(f, "https://gis.test/Layer/0", where="1=1", limit=2000)


def test_query_allows_truncation_when_asked():
    f = fetcher([ok({"features": [{"a": 1}] * 2000, "exceededTransferLimit": True})])
    assert len(arcgis.query(f, "https://gis.test/Layer/0", limit=2000, allow_partial=True)) == 2000


def test_short_oid_chunk_raises():
    f = fetcher([ok({"features": [{"i": 1}, {"i": 2}]})])
    with pytest.raises(TruncatedResponse):
        arcgis.fetch_by_oids(f, "https://gis.test/Layer/0", [1, 2, 3])


def test_fetch_all_clamps_to_server_max_record_count():
    f = fetcher([
        ok({"objectIds": list(range(1, 6))}),          # fetch_oids
        ok({"maxRecordCount": 2, "fields": []}),       # layer_info -> cap 2
        ok({"features": [{"i": 1}, {"i": 2}]}),
        ok({"features": [{"i": 3}, {"i": 4}]}),
        ok({"features": [{"i": 5}]}),
    ])
    got = [len(c["features"]) for c in arcgis.fetch_all(f, "https://gis.test/Layer/0", chunk=1000)]
    assert got == [2, 2, 1]                            # asked for 1000, server said 2


def test_centroid_per_parcel_is_not_averaged():
    """Averaging across a multi-parcel site puts the point in the road."""
    f = fetcher([ok({"features": [
        {"centroid": {"x": -118.0, "y": 34.0}},
        {"centroid": {"x": -118.010, "y": 34.010}},
    ]})])
    pts = arcgis.centroid_of(f, "https://gis.test/Layer/0", "APN", ["1", "2"])
    assert pts == [(34.0, -118.0), (34.01, -118.01)]


def test_centroid_falls_back_to_ring_mean_on_old_mapservers():
    f = fetcher([ok({"features": [{"geometry": {"rings": [[[-118.0, 34.0], [-118.0, 34.2],
                                                           [-117.8, 34.2], [-117.8, 34.0]]]}}]})])
    pts = arcgis.centroid_of(f, "https://gis.test/Layer/0", "APN", ["1"])
    assert pts == [(34.1, -117.9)]
