"""Shared HTTP helpers: a browser-like session, retries, and polite delays."""

from __future__ import annotations

import logging
import threading
import time

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30

_session: requests.Session | None = None
_session_lock = threading.Lock()

# Per-host timestamp of the last request, so delays are enforced per host
# rather than globally (a slow portal must not throttle the free gov APIs).
_last_request_at: dict[str, float] = {}
_delay_lock = threading.Lock()


def get_session() -> requests.Session:
    """Return a shared session with browser-like default headers."""
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-GB,en;q=0.9",
                }
            )
            _session = s
        return _session


def polite_delay(host: str, delay_seconds: float) -> None:
    """Sleep just long enough that requests to `host` stay `delay_seconds` apart."""
    if delay_seconds <= 0:
        return
    with _delay_lock:
        last = _last_request_at.get(host)
        now = time.monotonic()
        if last is not None:
            wait = delay_seconds - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        _last_request_at[host] = now


class TransientHTTPError(Exception):
    """A request failed in a way that is worth retrying."""


class BlockedError(Exception):
    """The host refused us outright (403/451). Retrying will not help."""


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """How long the server asked us to wait, capped so a run cannot hang."""
    value = (resp.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 300.0))
    except ValueError:
        return None


@retry(
    retry=retry_if_exception_type(TransientHTTPError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def get(url: str, **kwargs) -> requests.Response:
    """GET with retry on transient failures (timeouts, 5xx, 429)."""
    timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
    try:
        resp = get_session().get(url, timeout=timeout, **kwargs)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise TransientHTTPError(str(exc)) from exc

    if resp.status_code in (403, 451):
        raise BlockedError(
            f"HTTP {resp.status_code} from {url} - this host is refusing "
            "automated requests. Retrying will not help: slow the source "
            "down, or turn it off in config/sources.yaml."
        )
    if resp.status_code == 429 or resp.status_code >= 500:
        wait = _retry_after_seconds(resp)
        if wait:
            logger.warning(
                "http: %s asked us to wait %.0fs before retrying (HTTP %s)",
                url, wait, resp.status_code,
            )
            time.sleep(wait)
        raise TransientHTTPError(f"HTTP {resp.status_code} from {url}")
    resp.raise_for_status()
    return resp


def get_once(url: str, **kwargs) -> requests.Response | None:
    """GET with no retries, returning None on any failure.

    For probes where a miss is the normal case and retrying wastes time.
    """
    timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
    try:
        resp = get_session().get(url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        logger.debug("get_once: %s failed: %s", url, exc)
        return None
    if resp.status_code >= 400:
        logger.debug("get_once: %s returned HTTP %s", url, resp.status_code)
        return None
    return resp
