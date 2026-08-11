"""Declarative source registry — politeness and contact policy live with the source.

Hand-rolled scrapers tend to hardcode their own `delay=1.5, workers=2` and their own
User-Agent string. When a host tightens its limits you then have to find
every caller. Here the host's policy is stated once, and `apply(fetcher)` pushes it
into the rate limiter.

`rps` is requests per second per host, applied globally across threads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .core import Fetcher


@dataclass(frozen=True)
class Source:
    key: str
    base: str
    rps: float = 2.0
    kind: str = "arcgis"          # arcgis | http | json
    fields: tuple[str, ...] = ()
    note: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.base).netloc.lower()


# Public records only. No paid feeds, no credentialed endpoints, nothing behind a login.
SOURCES: dict[str, Source] = {s.key: s for s in [
    Source("sec.submissions", "https://data.sec.gov", rps=8, kind="json",
           note="Any UA accepted here."),
    Source("sec.archives", "https://www.sec.gov/Archives/edgar/data", rps=8, kind="http",
           note="403s unless the User-Agent contains a contact address. /cgi-bin/ is "
                "disallowed by robots.txt — never fetch the company browse UI."),
    Source("census.geocoder", "https://geocoding.geo.census.gov/geocoder", rps=4, kind="json",
           note="Batch endpoint takes up to 10k rows per POST; prefer it over per-row calls."),
    Source("census.tiger", "https://tigerweb.geo.census.gov/arcgis/rest/services", rps=4),
    Source("nominatim", "https://nominatim.openstreetmap.org", rps=1, kind="json",
           note="OSM policy is a hard 1 rps with a real UA. Fallback only, never bulk."),

    # County assessors — Southern California. Full layer URLs belong in your own
    # project config; only the host policy belongs here.
    Source("assessor.la", "https://public.gis.lacounty.gov/public/rest/services", rps=3,
           fields=("APN", "SitusAddress", "UseCode", "SQFTmain1", "YearBuilt1"),
           note="Owner name is redacted county-wide (CA Gov Code 7928.205)."),
    Source("assessor.orange", "https://services2.arcgis.com", rps=3),
    Source("assessor.sandiego", "https://gis-public.sandiegocounty.gov/arcgis/rest/services", rps=3),
    Source("assessor.sanbernardino", "https://arcgis.sbcounty.gov/arcgis/rest/services", rps=3),
    Source("assessor.riverside", "https://gis1.countyofriverside.us/arcgis/rest/services", rps=3),
    Source("assessor.ventura", "https://maps.ventura.org/arcgis/rest/services", rps=3,
           note="Zoning layer here is a ~2018 roll — stale. Parcels are current; zoning is not."),

    Source("scag.ldx", "https://maps.scag.ca.gov/scaggis/rest/services", rps=3,
           note="Local Data Exchange 2024: parcel-level zoning for 197 jurisdictions. "
                "Codes are each jurisdiction's own and are NOT comparable across cities."),
    Source("laplanning", "https://planning.lacity.gov", rps=2),

    # Operator/marketing sites: set the base per project, keep the rate conservative.
    Source("operator.site", "", rps=0.7, kind="http",
           note="robots.txt asks Crawl-delay 10; 0.7 rps one-off for research is the "
                "compromise, well under a search crawler. Re-check robots before any re-run."),
]}


def apply(fetcher: Fetcher, extra: dict[str, float] | None = None) -> Fetcher:
    """Push every source's rate policy into the fetcher's limiter."""
    for s in SOURCES.values():
        if s.host:
            fetcher.limiter.configure(s.host, s.rps)
    for host, rps in (extra or {}).items():
        fetcher.limiter.configure(host, rps)
    return fetcher


def for_url(url: str) -> Source | None:
    host = urlparse(url).netloc.lower()
    return next((s for s in SOURCES.values() if s.host and s.host == host), None)
