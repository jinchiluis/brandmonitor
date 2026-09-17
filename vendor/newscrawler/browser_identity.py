"""Shared browser identity for publisher-facing requests.

Plain HTTP requests cannot inherit Playwright's browser identity, so they use a
current, reduced Chrome User-Agent.  Browser sessions derive the same shape from
the Chromium version they actually launch instead of claiming a stale version.
"""

from __future__ import annotations


_UA_PREFIX = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
)
_UA_SUFFIX = " Safari/537.36"

# Used by requests-based discovery and body fetching. Chrome's reduced UA keeps
# only the major version meaningful; update this when the deployed browser moves
# materially ahead.
BROWSER_USER_AGENT = _UA_PREFIX + "Chrome/152.0.0.0" + _UA_SUFFIX


def chromium_user_agent(browser_version: str) -> str:
    """Return a normal Chrome UA matching a launched Chromium's major version."""
    major = browser_version.partition(".")[0]
    if not major.isascii() or not major.isdecimal():
        return BROWSER_USER_AGENT
    return _UA_PREFIX + f"Chrome/{major}.0.0.0" + _UA_SUFFIX
