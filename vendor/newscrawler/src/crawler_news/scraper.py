# pip install requests beautifulsoup4 lxml trafilatura
from __future__ import annotations
import os, re, typing as t
import trafilatura

from src.config import CRAWLER_VERBOSE as _VERBOSE
import requests
from bs4 import BeautifulSoup, Tag
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .scraper_fetch_html import fetch_html
from src.logger import get_logger
logger = get_logger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })
    retry = Retry(
        total=5,                # total retries
        connect=3,              # connection errors
        read=3,                 # read timeouts
        backoff_factor=1.2,     # 0, 1.2s, 2.4s, 4.8s, ...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

_SESSION = _build_session()
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 45  # was 15

def _load_html(source: str, timeout: int = 15) -> str:
    if source.startswith(("http://", "https://")):
        return fetch_html(source)
    import os
    if os.path.exists(source):
        with open(source, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    return source

def _clean_soup(soup: BeautifulSoup) -> None:
    # First pass: Remove elements that match privacy/cookie patterns by text content
    # (Must do this BEFORE removing by selector, as modals often have generic classes)
    to_remove = []
    for el in soup.find_all(True):
        if isinstance(el, Tag):
            # Never remove html or body tags
            if el.name in ['html', 'body']:
                continue

            text_sample = el.get_text(strip=True)[:200].lower()  # First 200 chars
            # Privacy/cookie modals typically start with these phrases
            privacy_starts = ("about your privacy", "manage consent", "cookie preferences",
                            "we use cookies", "this website uses cookies", "privacy settings",
                            "we and our partners")
            if any(text_sample.startswith(marker) for marker in privacy_starts):
                # Also check it's not tiny (avoid false positives)
                if len(el.get_text(strip=True)) > 100:
                    to_remove.append(el)

    for el in to_remove:
        el.decompose()

    # Second pass: remove obvious non-content by selectors
    # Don't remove structural tags (header, footer, nav, aside) if they're inside article tags
    structural_tags = ["header", "footer", "nav", "aside"]
    articles = soup.find_all("article")

    for sel in [
        "script", "style", "noscript", "iframe", "svg", "form", "input",
        "button", "figure", "figcaption",
        "[aria-hidden='true']",
        "[role='navigation']", "[role='banner']", "[role='contentinfo']", "[role='dialog']",
        ".advert, .ads, .ad, .promo, .share, .social, .subscribe, .newsletter",
        ".cookie, .consent, .paywall, .metered, .outbrain, .taboola",
        ".breadcrumbs, .breadcrumb, .comments, #comments",
    ]:
        for el in soup.select(sel):
            el.decompose()

    # Remove structural tags only if NOT inside article AND doesn't contain article
    for tag in structural_tags:
        for el in soup.find_all(tag):
            # Check if this element is inside an article
            is_inside_article = False
            for parent in el.parents:
                if parent.name == 'article':
                    is_inside_article = True
                    break
            # Check if this element contains an article
            contains_article = bool(el.find('article'))

            if not is_inside_article and not contains_article:
                el.decompose()

def _text_len(el: Tag) -> int:
    # count only meaningful text
    text = " ".join(x.strip() for x in el.stripped_strings)
    # penalize super short paragraphs
    short = sum(1 for p in el.find_all(["p", "li"]) if len(p.get_text(strip=True)) < 40)
    return max(0, len(text) - short * 40)

CANDIDATE_HINTS = re.compile(r"(article|content|post|story|main|body|read|text)", re.I)

def _find_best_node(soup: BeautifulSoup) -> t.Optional[Tag]:
    # 1) obvious containers
    candidates: list[Tag] = []
    candidates += soup.select("article")
    candidates += soup.select("[role='main']")
    candidates += [x for x in soup.find_all(True, id=CANDIDATE_HINTS)]
    candidates += [x for x in soup.find_all(True, class_=CANDIDATE_HINTS)]

    # 2) fallback: pick parents of <p> clusters
    if not candidates:
        parents = {}
        for p in soup.find_all("p"):
            pr = p.parent
            if isinstance(pr, Tag):
                parents.setdefault(pr, 0)
                parents[pr] += len(p.get_text(strip=True))
        candidates = sorted(parents, key=parents.get, reverse=True)[:10]

    # rank by text length, prefer deeper nodes with more paragraphs
    best = None
    best_score = 0
    for el in candidates:
        if not isinstance(el, Tag):
            continue
        # avoid tiny/utility nodes
        if len(el.find_all("p")) < 2 and len(el.get_text(strip=True)) < 400:
            continue
        score = _text_len(el) + 50 * len(el.find_all("p")) - 5 * (len(list(el.parents)))
        if score > best_score:
            best, best_score = el, score
    return best

def _extract_paragraphs(node: Tag) -> list[str]:
    # keep order: h1/h2/h3, then paragraphs and list items
    blocks: list[str] = []
    for el in node.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name in {"h1", "h2", "h3"}:
            txt = el.get_text(" ", strip=True)
            if txt and len(txt) > 5:
                blocks.append(txt)
        elif el.name == "p":
            txt = el.get_text(" ", strip=True)
            if txt:
                blocks.append(txt)
        elif el.name in {"li"} and not el.find_parent("nav"):
            txt = el.get_text(" ", strip=True)
            if txt:
                blocks.append("• " + txt)
    # de-dup near-duplicates (common with sticky headers)
    out: list[str] = []
    seen: set[str] = set()
    for b in blocks:
        key = b[:120]
        if key not in seen:
            out.append(b)
            seen.add(key)
    return out

def _extract_reuters_article(soup: BeautifulSoup) -> t.Optional[str]:
    """
    Extract article content from Reuters pages using their specific structure.
    Returns article text or None if content not found.

    Note: This should only be called for Reuters URLs to avoid unnecessary processing.
    """
    # Reuters uses data-testid="ArticleBody" for main content
    article_body = soup.find(attrs={'data-testid': 'ArticleBody'})
    if not article_body:
        return None

    # Get all text content from ArticleBody
    # Reuters wraps paragraphs in divs, not just p tags
    text_content = article_body.get_text('\n', strip=True)

    # Clean up common Reuters boilerplate
    lines = text_content.split('\n')
    cleaned_lines = []

    skip_patterns = [
        'Purchase Licensing',
        'Rights',
        ', opens new tab',
        'opens new tab',
        'Companies',
        'Follow',
        'Show more companies',
        'Sign up here',
        'here.',
        'Our Standards:',
        'The Thomson Reuters Trust Principles',
        'Suggested Topics:',
        'Share',
        'Facebook',
        'Linkedin',
        'Email',
        'Link',
        'Carbon Markets',
        'Sustainable Markets',
        'Grid & Infrastructure',
        'Climate Change',
        'Clean Energy'
    ]

    # Company name patterns (usually appear alone on a line)
    company_patterns = [
        'Corp',
        'Corporation',
        'Industries',
        'Limited',
        'Ltd',
        'Inc',
        'LLC',
        'PLC'
    ]

    for line in lines:
        line = line.strip()
        # Skip empty lines
        if not line:
            continue
        # Skip stock tickers like (XOM.N) or (SCIL.SI)
        if re.match(r'^\([A-Z]{2,5}\.[A-Z]{1,2}\)$', line):
            continue
        # Skip exact matches with boilerplate
        if any(pattern == line for pattern in skip_patterns):
            continue
        # Skip lines containing boilerplate patterns
        if any(pattern in line and len(line) < 50 for pattern in skip_patterns):
            continue
        # Skip standalone company names (short lines ending with company suffixes)
        if len(line) < 30 and any(line.endswith(suffix) for suffix in company_patterns):
            continue
        # Skip very short lines that don't end with punctuation (likely navigation)
        if len(line) < 15 and not line[-1] in '.!?)':
            continue

        cleaned_lines.append(line)

    return '\n\n'.join(cleaned_lines)

def _soup_extract(html: str) -> str:
    """BeautifulSoup-based extraction (fallback)."""
    soup = BeautifulSoup(html, "lxml")
    _clean_soup(soup)
    node = _find_best_node(soup) or soup.body or soup
    paragraphs = _extract_paragraphs(node)

    if not paragraphs or sum(len(p) for p in paragraphs) < 200:
        body = soup.body or soup
        raw_text = body.get_text(" ", strip=True)
        raw_text = re.sub(r'\s+', ' ', raw_text).strip()
        if len(raw_text) > 200:
            paragraphs = [raw_text]
            if _VERBOSE: logger.info(f"[scraper] fallback to raw body text ({len(raw_text)} chars)")

    return "\n\n".join(p for p in paragraphs if p)


def scrape_article(source: str) -> str:
    """
    Scrape main text content from a news article page or an HTML file/raw HTML.
    Returns a single text string.
    """
    html = _load_html(source)

    # Reuters-specific extraction (targeted, reliable — keep as-is)
    if 'reuters.com' in source.lower():
        soup = BeautifulSoup(html, "lxml")
        reuters_text = _extract_reuters_article(soup)
        if reuters_text:
            text = reuters_text
        else:
            text = _soup_extract(html)
    else:
        # Try trafilatura first
        traf = trafilatura.extract(html, include_comments=False, include_tables=False)
        if traf and len(traf) >= 300:
            if _VERBOSE: logger.info(f"[scraper] trafilatura extracted {len(traf)} chars")
            text = traf
        else:
            if _VERBOSE: logger.info(f"[scraper] trafilatura insufficient ({len(traf) if traf else 0} chars), falling back to BS4")
            text = _soup_extract(html)

    # normalize excessive whitespace
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    #Debug: Show if its a legit article...
    import unicodedata
    debugtext = unicodedata.normalize("NFC", text)
    letter_count = sum(ch.isalpha() for ch in debugtext)  # True for Unicode letters in any script
    if _VERBOSE: logger.info(f"Letter count: {letter_count:,}")

    return text

# --- minimal CLI ---
if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.info("Usage: python scraper.py <url|html-file|raw-html>")
        sys.exit(1)
    logger.info(scrape_article(sys.argv[1]))
