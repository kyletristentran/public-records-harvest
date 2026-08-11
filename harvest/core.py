"""The harvest core: one fetch path for every scraper in the stack.

Design in one paragraph
-----------------------
`Transport` is the only thing that touches a socket, and it is swappable — tests
run the entire stack against a recorded/fake transport with no network. `Fetcher`
wraps a transport with the four policies hand-rolled scrapers tend to re-invent
badly: an on-disk cache, a per-host rate limit, retry with exponential backoff and
jitter, and a provenance ledger. Sources declare their own politeness in
`registry.py` rather than each caller passing `delay=` and `workers=` by hand.

Two rules encoded here, both learned the expensive way:

1. **A failed fetch is never an empty result.** The client this replaced caught
   every exception and returned `[]`, so a timeout and "no parcels match" were the
   same value. `Fetcher.get` raises; callers opt into a default.
2. **Every response is recorded.** Any derived cell can be traced back to the
   request that produced it, with a timestamp and a body hash, without re-fetching.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse


class HarvestError(RuntimeError):
    """Base for everything this package raises."""


class FetchError(HarvestError):
    """Transport failed after every retry, or the server returned an error body."""


class TruncatedResponse(HarvestError):
    """The server returned fewer records than were asked for — silent data loss."""


# --------------------------------------------------------------------------- wire

@dataclass(frozen=True)
class Request:
    url: str
    method: str = "GET"
    params: Mapping[str, Any] | None = None
    data: Mapping[str, Any] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: tuple[float, float] = (15.0, 180.0)

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc.lower()

    def key(self) -> str:
        """Stable cache key. Params and body are sorted so ordering can't split the cache."""
        blob = json.dumps(
            [self.method.upper(), self.url,
             sorted((self.params or {}).items()),
             sorted((self.data or {}).items())],
            sort_keys=True, default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    from_cache: bool = False
    elapsed_ms: int = 0

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8", "replace"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Transport(Protocol):
    def send(self, req: Request) -> Response: ...


class RequestsTransport:
    """The real one. Kept deliberately thin — no policy lives here."""

    def __init__(self, session=None):
        import requests                                    # imported lazily: tests don't need it
        self._s = session or requests.Session()

    def send(self, req: Request) -> Response:
        t0 = time.time()
        r = self._s.request(
            req.method, req.url,
            params=dict(req.params) if req.params else None,
            data=dict(req.data) if req.data else None,
            headers=dict(req.headers), timeout=req.timeout,
        )
        return Response(status=r.status_code, body=r.content, headers=dict(r.headers),
                        elapsed_ms=int((time.time() - t0) * 1000))


# ------------------------------------------------------------------------ policies

class RateLimiter:
    """Per-host token bucket, shared across threads.

    The old code slept inside each worker, so N workers meant N times the intended
    rate against the host. A single bucket makes the limit mean what it says no
    matter how the callers are parallelised.
    """

    def __init__(self, default_rps: float = 2.0):
        self.default_rps = default_rps
        self._rps: dict[str, float] = {}
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()
        self._sleep = time.sleep                            # swappable for tests
        self._now = time.monotonic

    def configure(self, host: str, rps: float) -> None:
        self._rps[host.lower()] = rps

    def acquire(self, host: str) -> float:
        host = host.lower()
        rps = self._rps.get(host, self.default_rps)
        gap = 1.0 / rps if rps > 0 else 0.0
        with self._lock:
            now = self._now()
            due = max(now, self._next.get(host, 0.0))
            self._next[host] = due + gap
        wait = due - self._now()
        if wait > 0:
            self._sleep(wait)
        return max(wait, 0.0)


class Cache:
    """Content-addressed body store plus a small metadata sidecar.

    Bodies are addressed by request key, not by URL, so a POST with an
    `objectIds=` list caches as cleanly as a GET.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, key: str) -> tuple[Path, Path]:
        d = self.root / key[:2]
        return d / f"{key}.body", d / f"{key}.meta.json"

    def get(self, key: str) -> Response | None:
        body_p, meta_p = self._paths(key)
        if not (body_p.exists() and meta_p.exists()):
            return None
        meta = json.loads(meta_p.read_text())
        return Response(status=meta["status"], body=body_p.read_bytes(),
                        headers=meta.get("headers", {}), from_cache=True)

    def put(self, key: str, req: Request, resp: Response) -> None:
        body_p, meta_p = self._paths(key)
        body_p.parent.mkdir(parents=True, exist_ok=True)
        body_p.write_bytes(resp.body)
        meta_p.write_text(json.dumps({
            "url": req.url, "method": req.method, "status": resp.status,
            "headers": {k: v for k, v in resp.headers.items()
                        if k.lower() in ("etag", "last-modified", "content-type")},
            "sha256": hashlib.sha256(resp.body).hexdigest(),
            "bytes": len(resp.body),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=1))

    def validators(self, key: str) -> dict[str, str]:
        """ETag / Last-Modified for a conditional re-fetch."""
        _, meta_p = self._paths(key)
        if not meta_p.exists():
            return {}
        h = json.loads(meta_p.read_text()).get("headers", {})
        out = {}
        if h.get("ETag"):          out["If-None-Match"] = h["ETag"]
        if h.get("Last-Modified"): out["If-Modified-Since"] = h["Last-Modified"]
        return out


class Ledger:
    """Append-only JSONL record of every fetch. This is the provenance trail."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, **row) -> None:
        row.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        line = json.dumps(row, default=str)
        with self._lock, self.path.open("a") as f:
            f.write(line + "\n")

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]


# ------------------------------------------------------------------------- fetcher

@dataclass
class RetryPolicy:
    attempts: int = 5
    backoff: float = 2.0
    jitter: float = 0.25
    retry_status: tuple[int, ...] = (408, 429, 500, 502, 503, 504)

    def delay(self, attempt: int) -> float:
        base = self.backoff * (2 ** attempt)
        return base * (1 + random.uniform(-self.jitter, self.jitter))


class Fetcher:
    """Transport + cache + rate limit + retry + ledger. The only fetch path."""

    def __init__(self, transport: Transport, *, cache: Cache | None = None,
                 limiter: RateLimiter | None = None, ledger: Ledger | None = None,
                 retry: RetryPolicy | None = None, user_agent: str = "kt-harvest/0.1",
                 offline: bool = False):
        self.transport = transport
        self.cache = cache
        self.limiter = limiter or RateLimiter()
        self.ledger = ledger
        self.retry = retry or RetryPolicy()
        self.user_agent = user_agent
        self.offline = offline            # replay-only: a cache miss is an error
        self._sleep = time.sleep

    def send(self, req: Request, *, revalidate: bool = False, use_cache: bool = True) -> Response:
        key = req.key()
        cached = self.cache.get(key) if (self.cache and use_cache) else None
        if cached is not None and not revalidate:
            self._log(req, cached, key, attempt=0, cached=True)
            return cached
        if self.offline:
            raise FetchError(f"offline: no cached response for {req.url}")

        headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip", **dict(req.headers)}
        if cached is not None and revalidate and self.cache:
            headers.update(self.cache.validators(key))
        req = Request(req.url, req.method, req.params, req.data, headers, req.timeout)

        last: Exception | None = None
        for attempt in range(self.retry.attempts):
            self.limiter.acquire(req.host)
            try:
                resp = self.transport.send(req)
                if resp.status == 304 and cached is not None:
                    self._log(req, cached, key, attempt, cached=True, note="304 not-modified")
                    return cached
                if resp.status in self.retry.retry_status:
                    raise FetchError(f"HTTP {resp.status} from {req.url}")
                if resp.status >= 400:
                    raise FetchError(f"HTTP {resp.status} from {req.url}")   # not retryable
                if self.cache and use_cache:
                    self.cache.put(key, req, resp)
                self._log(req, resp, key, attempt)
                return resp
            except FetchError as exc:
                last = exc
                if "HTTP 4" in str(exc) and "408" not in str(exc) and "429" not in str(exc):
                    break                                   # client error: retrying won't help
            except Exception as exc:                        # transport-level: always transient
                last = exc
            if attempt < self.retry.attempts - 1:
                self._sleep(self.retry.delay(attempt))
        self._log(req, None, key, self.retry.attempts - 1, error=str(last))
        raise FetchError(f"{req.url} failed after {self.retry.attempts} attempts: {last}")

    def get(self, url: str, **kw) -> Response:
        opts = {k: kw.pop(k) for k in ("revalidate", "use_cache") if k in kw}
        return self.send(Request(url, "GET", **kw), **opts)

    def post(self, url: str, **kw) -> Response:
        opts = {k: kw.pop(k) for k in ("revalidate", "use_cache") if k in kw}
        return self.send(Request(url, "POST", **kw), **opts)

    def _log(self, req, resp, key, attempt, cached=False, error=None, note=None):
        if not self.ledger:
            return
        self.ledger.record(url=req.url, method=req.method, key=key, attempt=attempt,
                           status=(resp.status if resp else None),
                           bytes=(len(resp.body) if resp else 0),
                           from_cache=cached, elapsed_ms=(resp.elapsed_ms if resp else None),
                           error=error, note=note)


def build(root: Path | str = ".harvest", *, user_agent: str, offline: bool = False,
          transport: Transport | None = None) -> Fetcher:
    """Conventional wiring: cache + ledger under `root`, real transport unless given."""
    root = Path(root)
    return Fetcher(transport or RequestsTransport(),
                   cache=Cache(root / "cache"), ledger=Ledger(root / "ledger.jsonl"),
                   user_agent=user_agent, offline=offline)
