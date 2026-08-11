"""Drop-in replacement for a hand-rolled `arcgis.py`, backed by the harvest core.

Written to migrate an existing pipeline without touching its call sites. If your code
has a module exposing `query(base, ua, ...)` and `centroid_of(...)`, migration is a
one-line import swap:

    from . import arcgis        ->  from adapters import legacy_arcgis as arcgis

The call signatures are unchanged, so nothing else moves. What changes
underneath: caching, a global per-host rate limit, retry with backoff, a provenance
ledger, and — the one behavioural difference — **failures raise instead of
returning an empty list**.

That last one is the point of the exercise. The original swallowed every exception,
so a timeout against a county layer and "no parcel matches this address" were
the same value: `[]`. Rows silently landed in the unresolved bucket and the run looked
clean. If you need the old behaviour while migrating, set `LEGACY_SWALLOW = True`
and grep the ledger afterwards for what it hid.
"""
from __future__ import annotations

import os
from pathlib import Path

from harvest import arcgis as _ag, registry
from harvest.core import Fetcher, HarvestError, build

LEGACY_SWALLOW = False          # True = pre-migration behaviour: errors become []

_FETCHER: Fetcher | None = None


def configure(fetcher: Fetcher) -> None:
    """Inject a fetcher built by the caller (tests, or a project that wants its own root)."""
    global _FETCHER
    _FETCHER = fetcher


def _f(ua: str) -> Fetcher:
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = registry.apply(
            build(Path(os.environ.get("HARVEST_ROOT", ".harvest")), user_agent=ua)
        )
    return _FETCHER


def query(base, ua, *, where=None, geometry=None, out="*", count=2000,
          geom=False, centroid=False, distance=0, timeout=70):
    """Same signature as the original. Raises on failure unless LEGACY_SWALLOW."""
    try:
        return _ag.query(_f(ua), base, where=where, geometry=geometry, out_fields=out,
                         limit=count, geom=geom, centroid=centroid, distance=distance,
                         allow_partial=True)          # callers here page by hand
    except HarvestError:
        if LEGACY_SWALLOW:
            return []
        raise


def centroid_of(base, ua, apn_field, apns):
    """Original returned the MEAN centroid across all matched parcels.

    Preserved for signature compatibility, but prefer `centroids_of` — averaging
    across a multi-parcel site lands the point between the parcels, which on a
    corner site is the middle of the road, and every zoning read taken there is wrong.
    """
    try:
        return _ag.centroid_of(_f(ua), base, apn_field, apns, per_parcel=False) or (None, None)
    except HarvestError:
        if LEGACY_SWALLOW:
            return None, None
        raise


def centroids_of(base, ua, apn_field, apns):
    """One centroid per matched parcel — the shape multi-parcel sites actually need."""
    return _ag.centroid_of(_f(ua), base, apn_field, apns, per_parcel=True)
