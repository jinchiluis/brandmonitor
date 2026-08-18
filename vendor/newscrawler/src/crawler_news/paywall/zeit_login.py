"""
Zeit.de login steps.
Called by the paywall handler in scraper_fetch_html.py.
"""
import re


def login(page, email, password):
    """Perform Zeit.de login. Single-step Keycloak form with Friendly Captcha (proof-of-work).

    Friendly Captcha runs automatically in JS — no user interaction needed.
    We just wait for the hidden frc-captcha-response field to be populated.
    """
    # Navigate directly to the Keycloak login page (stable URL, no session-specific nonce/state)
    page.goto(
        "https://login.zeit.de/realms/zeit-online-public/protocol/openid-connect/auth"
        "?client_id=member&response_type=code&scope=openid"
        "&redirect_uri=https%3A%2F%2Fwww.zeit.de%2Findex",
        wait_until="domcontentloaded",
        timeout=60000,
    )

    # Fill credentials
    page.wait_for_selector("input#username", timeout=60000)
    page.fill("input#username", email)
    page.fill("input#password", password)

    # Friendly Captcha is proof-of-work — completes automatically via JS.
    # The hidden frc-captcha-response field starts empty/dotted and gets a real token when done.
    page.wait_for_function(
        """() => {
            const el = document.querySelector('input[name="frc-captcha-response"]');
            if (!el) return false;
            const v = el.value;
            return v && !v.startsWith('.') && v.length > 10;
        }""",
        timeout=60000,
    )

    # Submit
    page.locator("input#kc-login").click()

    # Wait for redirect back to zeit.de after successful login
    page.wait_for_url(re.compile(r"^https://(www\.)?zeit\.de/.*"), timeout=60000)
