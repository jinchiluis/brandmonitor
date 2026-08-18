"""Per-scrape wire-byte accounting.

Usage:
    bandwidth.start()                # reset counter for this thread
    bandwidth.attach(page)           # call after every ctx.new_page()
    ...
    n = bandwidth.consumed()         # bytes received on the wire

Sums encodedDataLength from Chrome's Network.loadingFinished events via CDP.
This matches DevTools "transferred" column.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_local = threading.local()


def start() -> None:
    _local.value = 0
    _local.proxy = None
    _local.proxy_session = None
    _local.last_error = None


def consumed() -> int:
    return int(getattr(_local, "value", 0) or 0)


def set_proxy(name: str | None, session: str | None = None) -> None:
    _local.proxy = name
    _local.proxy_session = session


def proxy() -> str | None:
    return getattr(_local, "proxy", None)


def proxy_session() -> str | None:
    """The sticky session used for the proxied fetch, if any.

    Lets the run-log probe re-query the same exit IP for rotating ISP fetches
    (paywall fetches instead carry their session in the paywall config)."""
    return getattr(_local, "proxy_session", None)


def note_error(msg: str | None) -> None:
    """Record a swallowed fetch failure cause (e.g. a Playwright `net::ERR_…`).

    The fetcher catches transport errors and returns empty HTML, so the cause
    is otherwise lost by the time `scrape_server` raises `ScrapeError("empty
    result")`. Stashing it on the same per-scrape threadlocal lets that path
    fold the real cause into the run-log `detail` (read in the worker thread)."""
    _local.last_error = msg


def last_error() -> str | None:
    return getattr(_local, "last_error", None)



def add(n: int) -> None:
    """Record wire bytes for a non-Playwright (httpx/requests) proxy fetch.

    Playwright pages are counted automatically via CDP (`attach`); plain HTTP
    fetches aren't, so the proxy httpx path calls this so its billed bandwidth
    shows up in `consumed()` (and therefore the run log's wire_bytes / cost)."""
    _add(n)


def _add(n: int) -> None:
    _local.value = getattr(_local, "value", 0) + int(n or 0)


def attach(page: Any) -> None:
    """Subscribe to Network.loadingFinished on this page's CDP session."""
    try:
        client = page.context.new_cdp_session(page)
        client.send("Network.enable")
        client.on("Network.loadingFinished", lambda e: _add(e.get("encodedDataLength", 0)))
    except Exception as e:
        logger.debug("bandwidth.attach failed: %s", e)
