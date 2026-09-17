from vendor.newscrawler.browser_identity import (
    BROWSER_USER_AGENT,
    chromium_user_agent,
)


def test_requests_identity_looks_like_a_regular_browser():
    lowered = BROWSER_USER_AGENT.lower()
    assert "mozilla/5.0" in lowered
    assert "chrome/" in lowered
    assert "bot" not in lowered
    assert "example.com" not in lowered


def test_playwright_identity_tracks_its_chromium_major_version():
    ua = chromium_user_agent("151.0.7922.34")
    assert "Chrome/151.0.0.0" in ua
    assert "HeadlessChrome" not in ua


def test_invalid_browser_version_uses_requests_identity():
    assert chromium_user_agent("unknown") == BROWSER_USER_AGENT
