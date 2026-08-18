"""
Welt.de login steps.
Called by the paywall handler in scraper_fetch_html.py.
"""
import re

from playwright.sync_api import Error as PlaywrightError


def login(page, email, password):
    """Perform Welt login. Navigates to login page and handles the form."""
    EMAIL_SEL  = "input#identifier, input[name='identifier'], input[type='email']"
    PASS_SEL   = "input#password, input[name='password'], input[type='password']"
    PWBTN_SEL  = "button[data-testid='password-button'][name='method'][value='password']"
    SUBMIT_SEL = f"{PWBTN_SEL}, button[data-testid='password-button'], button[type='submit']"

    page.goto("https://signin.auth.welt.de/login", wait_until="domcontentloaded", timeout=60000)

    # consent
    for sel in [
        "button:has-text('ALLE AKZEPTIEREN')",
        "[data-testid='uc-accept-all-button']",
        "button:has-text('Zustimmen')",
    ]:
        try:
            page.locator(sel).first.click(timeout=3000)
            break
        except Exception:
            continue

    # email
    page.wait_for_selector(EMAIL_SEL, timeout=60000)
    page.fill(EMAIL_SEL, email)
    page.locator(EMAIL_SEL).first.press("Tab")  # fire validation

    # reveal password field
    try:
        page.wait_for_selector(PASS_SEL, timeout=8000)
    except Exception:
        try:
            page.locator(PWBTN_SEL).first.click(timeout=6000)
        except Exception:
            try:
                page.locator("form").first.press("Enter")
            except Exception:
                try:
                    page.locator(SUBMIT_SEL).first.click(timeout=6000)
                except Exception:
                    pass
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_selector(PASS_SEL, timeout=60000)

    # password
    page.fill(PASS_SEL, password)
    page.locator(PASS_SEL).first.press("Tab")

    # submit (prefer password-method button to avoid magic-link path)
    try:
        page.locator(PWBTN_SEL).first.click(timeout=8000)
    except Exception:
        page.locator(SUBMIT_SEL).first.click(timeout=8000)

    # wait for redirect back to welt.de — this is the real success signal
    page.wait_for_url(re.compile(r"^https://(www\.)?welt\.de/.*"), timeout=60000)

    # finish client-side session bootstrap
    try:
        page.goto("https://www.welt.de/meinewelt/einstellungen", wait_until="domcontentloaded", timeout=60000)
    except PlaywrightError as e:
        if "ERR_FAILED" not in str(e):
            raise
