"""
Bild.de login steps.
Called by the paywall handler in scraper_fetch_html.py.

Single-page form: both email and password fields are visible at once.
"""
import re


def login(page, email, password):
    """Perform Bild login. Navigates to login page and fills the single-page form."""
    EMAIL_SEL  = "input#identifier, input[name='identifier'], input[type='email']"
    PASS_SEL   = "input#password, input[name='password'], input[type='password']"
    SUBMIT_SEL = "button[data-testid='password-button'][name='method'][value='password'], button[type='submit'][name='method'][value='password']"

    page.goto("https://signin.auth.bild.de/login", wait_until="domcontentloaded", timeout=60000)

    # consent (may appear before login form)
    for sel in [
        "button:has-text('Alle akzeptieren')",
        "[data-testid='uc-accept-all-button']",
        "button:has-text('Zustimmen')",
        "button:has-text('ALLE AKZEPTIEREN')",
    ]:
        try:
            page.locator(sel).first.click(timeout=3000)
            break
        except Exception:
            continue

    # fill email and password (both fields are visible on the same page)
    page.wait_for_selector(EMAIL_SEL, timeout=60000)
    page.fill(EMAIL_SEL, email)

    page.wait_for_selector(PASS_SEL, timeout=60000)
    page.fill(PASS_SEL, password)

    # submit — button is disabled until fields are filled, Playwright waits for it to be enabled
    page.locator(SUBMIT_SEL).first.click(timeout=15000)

    # wait for redirect back to bild.de
    page.wait_for_url(re.compile(r"^https://(www\.)?bild\.de/.*"), timeout=60000)
