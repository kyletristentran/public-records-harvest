"""The migrated legacy client: same signature, no longer silent on failure."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import legacy_arcgis as adapter                     # noqa: E402
from harvest.core import Fetcher, FetchError, Response, RetryPolicy  # noqa: E402
from tests.test_core import Fake, ok                             # noqa: E402


def wire(plan):
    f = Fetcher(Fake(plan), retry=RetryPolicy(attempts=2, backoff=0.0, jitter=0.0))
    f._sleep = lambda *_: None
    f.limiter._sleep = lambda *_: None
    adapter.configure(f)
    adapter.LEGACY_SWALLOW = False
    return f


def test_signature_is_unchanged():
    wire([ok({"features": [{"attributes": {"APN": "8208-026-027"}}]})])
    feats = adapter.query("https://gis.test/Layer/0", "ua/1.0",
                          where="APN='8208-026-027'", out="APN", count=40)
    assert feats[0]["attributes"]["APN"] == "8208-026-027"


def test_network_failure_no_longer_looks_like_no_matches():
    wire([TimeoutError("connection reset")] * 2)
    with pytest.raises(FetchError):
        adapter.query("https://gis.test/Layer/0", "ua/1.0", where="APN='1'")


def test_legacy_swallow_reproduces_the_old_behaviour():
    wire([TimeoutError("connection reset")] * 2)
    adapter.LEGACY_SWALLOW = True
    try:
        assert adapter.query("https://gis.test/Layer/0", "ua/1.0", where="APN='1'") == []
    finally:
        adapter.LEGACY_SWALLOW = False


def test_mean_centroid_still_available_but_per_parcel_is_preferred():
    plan = [ok({"features": [{"centroid": {"x": -118.0, "y": 34.0}},
                             {"centroid": {"x": -118.02, "y": 34.02}}]})]
    wire(plan)
    assert adapter.centroid_of("https://gis.test/L/0", "ua", "APN", ["1", "2"]) == (34.01, -118.01)
    wire(plan)
    assert adapter.centroids_of("https://gis.test/L/0", "ua", "APN", ["1", "2"]) == [
        (34.0, -118.0), (34.02, -118.02)]
