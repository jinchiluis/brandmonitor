"""
Manager Magazin login steps.
Called by the paywall handler in handler.py.

Uses the same Spiegel gruppenkonto system at /manager/anmelden.html.
Two-step form: email first, then password.
"""


def login(page, email, password):
    """Perform Manager Magazin login via gruppenkonto.spiegel.de/manager/."""
    USERNAME_SEL = 'input#username, input[name="loginform:username"], input[name*="username"]'
    PASSWORD_SEL = 'input#password, input[name="loginform:password"], input[name*="password"]'

    page.goto(
        "https://gruppenkonto.spiegel.de/manager/anmelden.html"
        "?targetUrl=https%3A%2F%2Fwww.manager-magazin.de%2F&requestAccessToken=true",
        wait_until="domcontentloaded",
        timeout=60000,
    )

    # consent (may appear before login form)
    for sel in [
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Zustimmen')",
        "button:has-text('Accept all')",
    ]:
        try:
            page.locator(sel).first.click(timeout=3000)
            break
        except Exception:
            continue

    # email step
    page.wait_for_selector(USERNAME_SEL, timeout=60000)
    page.fill(USERNAME_SEL, email)
    try:
        page.locator("form").first.press("Enter")
    except Exception:
        page.locator("button[type='submit']").first.click()
    page.wait_for_load_state("domcontentloaded")

    # password step
    page.wait_for_selector(PASSWORD_SEL, timeout=60000)
    page.fill(PASSWORD_SEL, password)
    try:
        page.locator("form").first.press("Enter")
    except Exception:
        page.locator("button[type='submit']").first.click()
    page.wait_for_load_state("domcontentloaded")
