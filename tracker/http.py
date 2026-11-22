"""Small, polite HTTP helper (standard library only).

* Limits how many requests hit the same host at once and spaces them out.
* Retries rate limits (429), server errors and timeouts with backoff.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

USER_AGENT = "Mozilla/5.0 (compatible; internship-tracker/1.0)"

PER_HOST_CONCURRENCY = 2
PER_HOST_GAP_SECONDS = 0.4

_host_sems: dict[str, threading.Semaphore] = defaultdict(
    lambda: threading.Semaphore(PER_HOST_CONCURRENCY)
)
_host_next_time: dict[str, float] = defaultdict(float)
_pace_lock = threading.Lock()
# Hosts we can't reach at all (DNS failure / blocked by a network policy).
# Remembered so we fail fast instead of retrying hundreds of times.
_dead_hosts: set[str] = set()


class FetchError(Exception):
    """A request failed for good (after retries)."""


def _unreachable(err: Exception) -> bool:
    text = str(getattr(err, "reason", err))
    return any(s in text for s in (
        "Tunnel connection failed", "Name or service not known", "nodename nor servname",
        "No address associated", "getaddrinfo failed", "Temporary failure in name resolution",
    ))


# Some platforms rate-limit per visitor across all their customers, so pace
# them as one shared "host".
_SHARED_LANES = {"myworkdayjobs.com": 0.6, "oraclecloud.com": 0.5}


def _lane(host: str) -> tuple[str, float]:
    for suffix, gap in _SHARED_LANES.items():
        if host.endswith(suffix):
            return suffix, gap
    return host, PER_HOST_GAP_SECONDS


def _wait_turn(lane: str, gap: float) -> None:
    with _pace_lock:
        now = time.monotonic()
        start = max(now, _host_next_time[lane])
        _host_next_time[lane] = start + gap
    delay = start - now
    if delay > 0:
        time.sleep(delay)


def _slow_down(lane: str, seconds: float) -> None:
    """After a rate-limit response, push back everyone using this lane."""
    with _pace_lock:
        _host_next_time[lane] = max(_host_next_time[lane], time.monotonic() + seconds)


def fetch(
    url: str,
    *,
    json_body: dict | None = None,
    headers: dict | None = None,
    as_json: bool = True,
    timeout: float = 45,
    retries: int = 5,
):
    """GET (or POST when json_body is given) and return parsed JSON or text."""
    host = urllib.parse.urlparse(url).netloc
    if host in _dead_hosts:
        raise FetchError(f"host unreachable: {host}")
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json" if as_json else "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"

    lane, gap = _lane(host)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        retry_after = None
        with _host_sems[lane]:
            _wait_turn(lane, gap)
            try:
                req = urllib.request.Request(url, data=data, headers=hdrs)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    charset = resp.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                return json.loads(text) if as_json else text
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code not in (408, 425, 429, 500, 502, 503, 504):
                    raise FetchError(f"HTTP {e.code}") from e
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra and ra.isdigit():
                    retry_after = min(int(ra), 90)
                elif e.code == 429:
                    retry_after = min(15 * (2 ** attempt), 90)
                if e.code == 429:
                    _slow_down(lane, retry_after or 15)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                last_err = e
                if _unreachable(e):
                    _dead_hosts.add(host)
                    raise FetchError(f"host unreachable: {host}") from e
            except OSError as e:  # e.g. connection reset
                last_err = e
        if attempt < retries:
            sleep = retry_after if retry_after is not None else (2 ** attempt) * 1.5 + random.random()
            time.sleep(sleep)
    reason = getattr(last_err, "reason", None) or last_err
    raise FetchError(str(reason)[:200])
