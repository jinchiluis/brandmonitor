"""Content lifecycle: enrichment, rechecks, retries, and public-page extraction."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bodies import (NO_ARTICLE_TEXT, PAYWALL_DECLARED, BodyResult,
                        _extract_pdf, _fetch_subscriber, fetch_body,
                        normalize_page_published_at, run_body_fetch)
from src.collect import run_collection, store_hints
from src.db import get_watermark, migrate, session, start_run
from vendor.newscrawler.crawler import ArticleHint


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps([
        {"url": "https://trade.test/", "organization": "Trade", "content_mode": "full_text"},
        {"url": "https://major.test/", "organization": "Major", "content_mode": "title_only"},
    ]), encoding="utf-8")
    return db, sources


def discover(project, url="https://trade.test/article", date="2026-09-01T00:00:00+00:00",
             title="Headline", full_text=True, kind="news"):
    db, _ = project
    slug = url.split("/")[2]
    with session(db) as conn:
        run = start_run(conn, kind, "a", "b")
        return store_hints(conn, run, slug, [ArticleHint(
            url, datetime.fromisoformat(date) if date else None, title, "sitemap")],
            source_kind=kind, full_text=full_text)


def rows(project):
    with session(project[0]) as conn:
        return list(conn.execute("SELECT * FROM raw_item ORDER BY id"))


def run(project, **kwargs):
    return run_body_fetch(project[1], db_path=project[0], **kwargs)


def success(text="The article body", title="Headline"):
    return BodyResult("ok", text=text, title=title)


def test_enrichment_preserves_hint_and_assessment_and_rerun_skips_network(project, monkeypatch):
    discover(project)
    original = dict(rows(project)[0])
    with session(project[0]) as conn:
        conn.execute("INSERT INTO assessment (raw_item_id, client_slug, prompt_version, "
                     "profile_version, created_at, payload) VALUES (?, 'client', 'v1', 'v1', 'now', '{}')",
                     (original["id"],))
    calls = []
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: calls.append(url) or success())
    first, second = run(project), run(project)
    assert (first["stored"], second["attempted"]) == (1, 0)
    assert len(calls) == 1
    items = rows(project)
    assert dict(items[0]) == original
    assert json.loads(items[1]["payload"])["body_text"] == "The article body"
    assert items[1]["version"] == 2
    with session(project[0]) as conn:
        assert conn.execute("SELECT raw_item_id FROM assessment").fetchone()[0] == original["id"]


def test_lastmod_churn_rechecks_without_content_versions(project, monkeypatch):
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    discover(project)
    run(project)
    assert discover(project, date="2026-09-09T00:00:00+00:00") == 0
    summary = run(project)
    assert (summary["attempted"], summary["stored"]) == (1, 0)
    assert len(rows(project)) == 2
    assert rows(project)[-1]["published_at"] == "2026-09-01T00:00:00+00:00"


def test_real_change_and_reversion_are_both_preserved(project, monkeypatch):
    discover(project)
    for body in ("Original body", "Corrected body", "Original body"):
        monkeypatch.setattr("src.bodies.fetch_body", lambda url, body=body: success(body))
        assert run(project, refresh=True)["stored"] == 1
    assert [r["version"] for r in rows(project)] == [1, 2, 3, 4]


def test_whitespace_is_not_a_content_change(project, monkeypatch):
    discover(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success("Body text\n\nMore text"))
    run(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success("Body  text More\ttext"))
    assert run(project, refresh=True)["stored"] == 0


def test_body_title_change_versions_even_when_text_does_not(project, monkeypatch):
    discover(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    run(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success(title="Corrected title"))
    assert run(project, refresh=True)["stored"] == 1
    assert rows(project)[-1]["title"] == "Corrected title"


def test_failed_fetch_is_visible_and_retries_without_rediscovery(project, monkeypatch):
    discover(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: BodyResult("failed", error="timeout"))
    summary = run(project)
    assert summary["remaining"]["failed"] == 1
    assert len(rows(project)) == 1
    with session(project[0]) as conn:
        failure = conn.execute("SELECT * FROM body_fetch").fetchone()
        assert (failure["status"], failure["attempts"], failure["error"]) == ("failed", 1, "timeout")
        assert conn.execute("SELECT status FROM run WHERE id=?", (summary["run_id"],)).fetchone()[0] == "failed"
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    assert run(project)["stored"] == 1


def test_unavailable_is_explicitly_retryable_and_never_overwrites_a_body(project, monkeypatch):
    discover(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    run(project)
    before = [dict(r) for r in rows(project)]
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: BodyResult("unavailable", error="paywall"))
    unavailable = run(project, refresh=True)
    assert unavailable["unavailable"] == 1
    with session(project[0]) as conn:
        assert conn.execute(
            "SELECT status FROM run WHERE id=?", (unavailable["run_id"],)
        ).fetchone()[0] == "ok"
    assert run(project)["attempted"] == 0
    assert [dict(r) for r in rows(project)] == before
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    assert run(project, retry_unavailable=True)["ok"] == 1


def test_backfills_legacy_rows_but_never_fetches_title_only_sources(project, monkeypatch):
    discover(project, full_text=False)
    discover(project, url="https://major.test/article", full_text=False)
    calls = []
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: calls.append(url) or success())
    assert run(project)["stored"] == 1
    assert calls == ["https://trade.test/article"]


def test_title_gate_log_fetches_only_kept_title_only_urls(project, monkeypatch, tmp_path):
    discover(project, url="https://major.test/kept", full_text=False)
    discover(project, url="https://major.test/dropped", full_text=False)
    log_dir = tmp_path / "title_gate" / "client"
    log_dir.mkdir(parents=True)
    decisions = [
        {"client": "client", "source": "major.test",
         "external_id": "https://major.test/kept", "decision": "keep"},
        {"client": "client", "source": "major.test",
         "external_id": "https://major.test/dropped", "decision": "drop"},
    ]
    (log_dir / "2026-09-11.jsonl").write_text(
        "\n".join(json.dumps(row) for row in decisions) + "\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr("src.bodies.fetch_body",
                        lambda url: calls.append(url) or success("Selected body"))

    summary = run(project, title_gate_client="client",
                  title_gate_log_root=tmp_path / "title_gate")

    assert summary["attempted"] == 1 and summary["stored"] == 1
    assert calls == ["https://major.test/kept"]
    with session(project[0]) as conn:
        queued = list(conn.execute(
            "SELECT external_id, status, hint_payload FROM body_fetch ORDER BY external_id"))
    assert [(row["external_id"], row["status"]) for row in queued] == [
        ("https://major.test/kept", "ok")]
    route = json.loads(queued[0]["hint_payload"])["title_gate_routes"]["client"]
    assert route["client"] == "client"


def test_title_gate_fetch_retries_its_queue_after_the_jsonl_is_gone(
        project, monkeypatch, tmp_path):
    discover(project, url="https://major.test/retry", full_text=False)
    log_dir = tmp_path / "title_gate" / "client"
    log_dir.mkdir(parents=True)
    decision = {"client": "client", "source": "major.test",
                "external_id": "https://major.test/retry", "decision": "keep"}
    log = log_dir / "2026-09-11.jsonl"
    log.write_text(json.dumps(decision) + "\n", encoding="utf-8")
    monkeypatch.setattr("src.bodies.fetch_body",
                        lambda url: BodyResult("failed", error="timeout"))
    first = run(project, title_gate_client="client",
                title_gate_log_root=tmp_path / "title_gate")
    log.unlink()
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success("Recovered body"))

    second = run(project, title_gate_client="client",
                 title_gate_log_root=tmp_path / "title_gate")

    assert first["failed"] == 1
    assert second["ok"] == 1 and second["stored"] == 1


def test_backfill_respects_narrowed_sitemap_sections(project, monkeypatch):
    entries = json.loads(project[1].read_text())
    entries[0]["allowed_dirs"] = ["events"]
    project[1].write_text(json.dumps(entries))
    discover(project, url="https://trade.test/events/story", full_text=False)
    discover(project, url="https://trade.test/recipes/cake", full_text=False)
    calls = []
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: calls.append(url) or success())
    assert run(project)["attempted"] == 1
    assert calls == ["https://trade.test/events/story"]


def test_backfill_respects_excluded_dirs_for_sitemaps_and_feeds(project, monkeypatch):
    discover(project, url="https://trade.test/paid/sitemap-story")
    discover(project, url="https://trade.test/paid/feed-story")
    with session(project[0]) as conn:
        conn.execute(
            "UPDATE raw_item SET payload=json_set(payload, '$.discovered_via', 'rss') "
            "WHERE external_id='https://trade.test/paid/feed-story'"
        )
    entries = json.loads(project[1].read_text())
    entries[0]["excluded_dirs"] = ["paid"]
    project[1].write_text(json.dumps(entries))
    calls = []
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: calls.append(url) or success())
    assert run(project)["attempted"] == 0
    assert calls == []


def test_backfill_respects_source_specific_url_fragments(project, monkeypatch):
    discover(project, url="https://trade.test/news/acme-newsuebersicht-123.html")
    entries = json.loads(project[1].read_text())
    entries[0]["excluded_url_substrings"] = ["newsuebersicht"]
    project[1].write_text(json.dumps(entries))
    calls = []
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: calls.append(url) or success())
    assert run(project)["attempted"] == 0
    assert calls == []


def test_backfill_respects_source_specific_title_fragments(project, monkeypatch):
    discover(project, url="https://trade.test/news/old-article-123.html",
             title="Acme: Newsübersicht")
    entries = json.loads(project[1].read_text())
    entries[0]["excluded_title_substrings"] = ["Newsübersicht"]
    project[1].write_text(json.dumps(entries))
    calls = []
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: calls.append(url) or success())
    assert run(project)["attempted"] == 0
    assert calls == []


def test_batch_limit_and_old_failures_do_not_starve_pending_items(project, monkeypatch):
    discover(project, url="https://trade.test/a")
    discover(project, url="https://trade.test/b")
    calls = []
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: calls.append(url) or BodyResult("failed", error="timeout"))
    assert run(project, limit=1)["deferred"] == 1
    assert run(project, limit=1)["deferred"] == 1
    assert calls == ["https://trade.test/a", "https://trade.test/b"]


def test_collection_runs_bodies_and_keeps_discovery_watermark_independent(project, monkeypatch):
    monkeypatch.setattr("src.collect.collect_source", lambda entry, *args: ([ArticleHint(
        entry["url"] + "article", None, "Headline", "rss")], None))
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: BodyResult("failed", error="timeout"))
    summary = run_collection(project[1], db_path=project[0], workers=1)
    assert summary["stored"] == 2
    assert summary["bodies"]["failed"] == 1
    with session(project[0]) as conn:
        assert get_watermark(conn, "collection:news") == summary["end"]
    # The feed may no longer carry that article: the saved body task survives.
    monkeypatch.setattr("src.collect.collect_source", lambda *args: ([], None))
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    again = run_collection(project[1], db_path=project[0], workers=1)
    assert again["found"] == 0
    assert again["bodies"]["stored"] == 1


def test_body_tracks_are_separate(project, monkeypatch):
    discover(project, kind="regulatory")
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    assert run(project, kind="news")["attempted"] == 0
    assert run(project, kind="regulatory")["stored"] == 1
    assert {r["source_kind"] for r in rows(project)} == {"regulatory"}


PARAGRAPHS = [
    "Die neue Verordnung betrifft den Versand von Paketen nach Deutschland. "
    "Die zuständige Behörde hat dazu eine ausführliche Mitteilung veröffentlicht.",
    "Unternehmen müssen ihre Prozesse prüfen und die angekündigten Änderungen "
    "beobachten. Weitere Einzelheiten sollen im kommenden Monat veröffentlicht werden.",
    "Der Branchenverband begrüßt die Klarstellung und fordert eine angemessene "
    "Übergangsfrist. Die Regelung betrifft sowohl Händler als auch Paketdienste.",
]
ARTICLE = '<html><head><title>Neue Regeln</title></head><body><nav>Menu</nav>' \
          '<article><h1>Neue Regeln für Händler</h1>' + ''.join(f'<p>{p}</p>' for p in PARAGRAPHS) + \
          '</article><footer>Navigation</footer></body></html>'


def respond(monkeypatch, html=ARTICLE, status=200, media="text/html"):
    response = requests.Response()
    response.status_code = status
    response.url = "https://trade.test/article"
    response.headers["Content-Type"] = media
    response._content = html.encode("utf-8")
    response._content_consumed = True
    monkeypatch.setattr("src.bodies.requests.get", lambda *a, **kw: response)


def test_real_extractor_preserves_article_and_german_characters(monkeypatch):
    respond(monkeypatch)
    result = fetch_body("https://trade.test/article")
    assert result.status == "ok"
    assert result.title == "Neue Regeln für Händler"
    assert all(p in result.text for p in PARAGRAPHS)
    assert "Navigation" not in result.text


def test_an_article_wrapped_in_one_container_still_extracts(monkeypatch):
    """A lone <article> holding a single wrapper div used to extract to nothing.

    trafilatura's load_html rejects markup whose first 50 characters lack "html"
    unless its root has 2+ children, so handing it a bare <article> fragment
    returned None. The page was then retired as "no article text even after
    rendering" - verkehrsrundschau.de lost 32 stories that way.
    """
    wrapped = ARTICLE.replace('<article>', '<article><div class="section">').replace(
        '</article>', '</div></article>')
    respond(monkeypatch, wrapped)
    result = fetch_body("https://trade.test/article")
    assert result.status == "ok"
    assert all(p in result.text for p in PARAGRAPHS)


def test_a_lone_article_hides_its_ids_from_the_extractor(monkeypatch):
    """Inside a known boundary, an id must not be able to re-anchor the body.

    trafilatura treats ids like #article-content-wrapper as "the body starts here".
    verkehrsrundschau.de wraps only its standfirst in one and leaves the rest of the
    story outside it, so the extractor kept the lead and dropped ~40% of every
    article - a body that still passed the length gate, so nothing flagged it.
    Asserted on the markup handed to the extractor because reproducing the scoring
    that picks the anchor needs a full-size page, not a fixture.
    """
    import trafilatura
    seen = []
    real = trafilatura.extract
    monkeypatch.setattr(trafilatura, "extract",
                        lambda html, **kw: seen.append(html) or real(html, **kw))
    anchored = ARTICLE.replace(
        '<article>', '<article><div id="article-content-wrapper" class="lead">').replace(
        '</article>', '</div></article>')
    respond(monkeypatch, anchored)
    assert fetch_body("https://trade.test/article").status == "ok"
    assert 'id="article-content-wrapper"' not in seen[0]
    assert 'class="lead"' in seen[0], "classes still carry junk signals; only ids re-anchor"


def test_page_title_fills_a_missing_discovery_title(monkeypatch):
    respond(monkeypatch, ARTICLE.replace('<h1>Neue Regeln für Händler</h1>', ''))
    result = fetch_body("https://trade.test/article")
    assert result.status == "ok"
    assert result.title == "Neue Regeln"


@pytest.mark.parametrize("html,status,media,expected", [
    (ARTICLE.replace('<head>', '<head><script type="application/ld+json">'
                     '{"@graph":[{"isAccessibleForFree":false}]}</script>'), 200, "text/html", "unavailable"),
    (ARTICLE, 404, "text/html", "unavailable"),
    (ARTICLE, 403, "text/html", "failed"),
    (ARTICLE, 429, "text/html", "failed"),
    (ARTICLE, 500, "text/html", "failed"),
    (ARTICLE, 200, "image/jpeg", "unavailable"),
    ('<html><body><div id="app"></div></body></html>', 200, "text/html", "failed"),
    (ARTICLE.replace("<title>Neue Regeln", "<title>Just a moment"), 200, "text/html", "failed"),
])
def test_error_pages_and_teasers_are_not_saved_as_bodies(monkeypatch, html, status, media, expected):
    respond(monkeypatch, html, status, media)
    result = fetch_body("https://trade.test/article")
    assert result.status == expected
    assert result.text is None
    assert result.error


def test_timeout_is_retryable(monkeypatch):
    def timeout(*args, **kwargs):
        raise requests.Timeout("slow site")
    monkeypatch.setattr("src.bodies.requests.get", timeout)
    result = fetch_body("https://trade.test/article")
    assert result.status == "failed" and "Timeout" in result.error


def test_homepages_are_not_fetched_as_articles(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("homepage should be rejected before fetching")
    monkeypatch.setattr("src.bodies.requests.get", unexpected)
    assert fetch_body("https://trade.test/").status == "unavailable"


def test_quotation_in_slider_is_part_of_source_material(monkeypatch):
    quote = "Die Regelung ist wichtig und muss alle betroffenen Unternehmen einbeziehen. " \
            "Wir fordern eine ausreichende Übergangsfrist und eine transparente Umsetzung."
    html = ARTICLE.replace('</article>', '<div class="slider quoteslider"><div class="slide">'
                           f'<blockquote><p>{quote}</p></blockquote></div></div></article>')
    respond(monkeypatch, html)
    result = fetch_body("https://trade.test/article")
    assert result.status == "ok"
    assert quote in result.text


def test_dense_tag_cloud_is_removed_without_removing_article_links(monkeypatch):
    tags = " , ".join(
        f'<span class="tagcloud-prio-{i % 3 + 1}"><a href="/tag/{i}">Tag {i}</a></span>'
        for i in range(8)
    )
    unbounded = ARTICLE.replace("<article>", "<main>").replace("</article>", "</main>")
    unbounded = unbounded.replace(
        "</body>", f'<div class="global-taxonomy"><p>{tags}</p></div></body>')
    respond(monkeypatch, unbounded)

    result = fetch_body("https://trade.test/article")

    assert result.status == "ok"
    assert all(p in result.text for p in PARAGRAPHS)
    assert "Tag 0" not in result.text


def test_one_tagcloud_link_in_article_is_not_removed(monkeypatch):
    linked = ARTICLE.replace(
        "</article>",
        '<p><span class="tagcloud-prio-1"><a href="/tag/kep">KEP-Dienste</a></span> '
        "bleiben Gegenstand des Artikels.</p></article>")
    respond(monkeypatch, linked)

    result = fetch_body("https://trade.test/article")

    assert result.status == "ok"
    assert "KEP-Dienste" in result.text


def test_source_tiers_are_explicit():
    """The tier split is a decision, not a default, so it is pinned here.

    BVL moved to title_only on 2026-09-09: 0 brand hits across 620 URLs. DVZ moved
    on 2026-09-11 when its missing news sitemap exposed a substantial daily stream
    from a mixed/paywalled source. Both are fetched on match instead of in bulk.
    """
    root = Path(__file__).resolve().parent.parent
    news = json.loads((root / "input/germany_medias.json").read_text(encoding="utf-8"))
    assert sum(e["content_mode"] == "full_text" for e in news) == 14
    assert sum(e["content_mode"] == "title_only" for e in news) == 10
    assert {e["access"] for e in news} == {"free", "mixed"}
    assert sum(e["access"] == "mixed" for e in news) == 10
    logins = {e["organization"]: e["paywall_login"] for e in news
              if "paywall_login" in e}
    assert logins == {
        "DER SPIEGEL": "spiegel.de",
        "DIE ZEIT": "zeit.de",
        "WELT": "welt.de",
    }
    configured = json.loads(
        (root / "vendor/newscrawler/paywall/paywalls.json").read_text(encoding="utf-8"))
    assert set(logins.values()) <= set(configured)
    dvz = next(e for e in news if e["organization"] == "DVZ")
    assert dvz["content_mode"] == "title_only"
    assert dvz["extra_sitemap_urls"] == ["https://www.dvz.de/news-sitemap.xml"]
    regulatory = json.loads((root / "input/regulatory_sources.json").read_text(encoding="utf-8"))
    assert all(e["content_mode"] == "full_text" for e in regulatory)
    assert all(e["access"] == "free" and "paywall_login" not in e
               for e in regulatory)
    presscorner = next(e for e in regulatory
                       if e["organization"] == "EU Commission Press Corner")
    assert presscorner["feed_urls"] == [
        "https://ec.europa.eu/commission/presscorner/api/rss?search?language=en"
        "&policyarea=23&pagesize=100",
    ]


def test_page_date_replaces_a_restamped_discovery_date(project, monkeypatch):
    """A sitemap <lastmod> is a change signal; the page's own date is the age.

    BVL stamps every URL with one regeneration timestamp and etailment's migration
    restamped a 2001 archive to 2026, so both make old pages look new. Discovery
    said 2026-09-01 here; the article says 2019.
    """
    discover(project, date="2026-09-01T00:00:00+00:00")
    monkeypatch.setattr("src.bodies.fetch_body",
                        lambda url: BodyResult("ok", text="The article body",
                                               title="Headline",
                                               published_at="2019-12-09"))
    run(project)
    latest = rows(project)[-1]
    assert latest["published_at"] == "2019-12-09"
    assert json.loads(latest["payload"])["published_at_source"] == "page"


def test_discovery_date_is_kept_and_marked_when_the_page_states_none(project, monkeypatch):
    discover(project, date="2026-09-01T00:00:00+00:00")
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    run(project)
    latest = rows(project)[-1]
    assert latest["published_at"] == "2026-09-01T00:00:00+00:00"
    assert json.loads(latest["payload"])["published_at_source"] == "sitemap"


def test_page_published_at_reads_nested_json_ld():
    from bs4 import BeautifulSoup

    from src.bodies import _page_published_at

    soup = BeautifulSoup(
        '<script type="application/ld+json">'
        '{"@graph":[{"@type":"WebSite"},{"@type":"Article","datePublished":"2026-07-19"}]}'
        "</script>", "lxml")
    assert _page_published_at(soup) == "2026-07-19"


def test_page_published_at_falls_back_to_meta_and_then_none():
    from bs4 import BeautifulSoup

    from src.bodies import _page_published_at

    meta = BeautifulSoup(
        '<meta property="article:published_time" content="2026-06-09T10:00:00+02:00">', "lxml")
    assert _page_published_at(meta) == "2026-06-09T10:00:00+02:00"
    assert _page_published_at(BeautifulSoup("<p>no date here</p>", "lxml")) is None


@pytest.mark.parametrize(("raw", "expected"), [
    ("Do, 09/10/2026 - 14:33", "2026-09-10T14:33:00+02:00"),
    ("So, 11/16/2025 - 06:00", "2025-11-16T06:00:00+01:00"),
    ("2026-09-10T13:51:00+0200", "2026-09-10T13:51:00+02:00"),
    ("2019-12-09", "2019-12-09"),
    ("Mo, 09/10/2026 - 14:33", None),  # 10 September 2026 was Thursday
    ("not a date", None),
])
def test_page_dates_are_normalized_or_rejected(raw, expected):
    assert normalize_page_published_at(raw) == expected


def test_logistik_date_is_normalized_and_raw_value_is_retained(monkeypatch):
    raw = "Do, 09/10/2026 - 14:33"
    markup = ARTICLE.replace(
        "<head>", '<head><script type="application/ld+json">'
        f'{{"@type":"NewsArticle","datePublished":"{raw}"}}'
        "</script>")
    respond(monkeypatch, markup)

    result = fetch_body("https://trade.test/article")

    assert result.published_at == "2026-09-10T14:33:00+02:00"
    assert result.published_at_raw == raw


def test_invalid_page_date_keeps_discovery_date_and_raw_evidence(project, monkeypatch):
    discover(project, date="2026-09-01T00:00:00+00:00")
    monkeypatch.setattr(
        "src.bodies.fetch_body",
        lambda url: BodyResult("ok", text="The article body", title="Headline",
                               published_at_raw="not a date"))

    run(project)

    latest = rows(project)[-1]
    payload = json.loads(latest["payload"])
    assert latest["published_at"] == "2026-09-01T00:00:00+00:00"
    assert payload["published_at_source"] == "sitemap"
    assert payload["published_at_raw"] == "not a date"


def test_a_lone_time_element_dates_a_page_but_disagreeing_ones_do_not():
    from bs4 import BeautifulSoup

    from src.bodies import _page_published_at

    # bevh: one <time itemprop="datePublished">, nothing in JSON-LD or meta.
    lone = BeautifulSoup(
        '<h2><time itemprop="datePublished" datetime="2026-09-09">09.09.2026</time></h2>', "lxml")
    assert _page_published_at(lone) == "2026-09-09"
    # DSLV: epoch seconds in the datetime attribute.
    epoch = BeautifulSoup('<li><time datetime="1789036740">10. September 2026</time></li>', "lxml")
    assert _page_published_at(epoch) == "2026-09-10T12:39:00+02:00"
    # Verbraucherzentrale class action: filed, served, status. None is publication.
    record = BeautifulSoup(
        '<time datetime="2025-03-04T12:00:00Z">04. März 2025</time>'
        '<time datetime="2025-04-16T12:00:00Z">16. April 2025</time>'
        '<time datetime="2026-06-30T12:00:00Z">30. Juni 2026</time>', "lxml")
    assert _page_published_at(record) is None
    # Durations and empty attributes are not dates and do not spoil a lone one;
    # two stamps on the same day agree.
    noisy = BeautifulSoup(
        '<time datetime="PT5M">5 min</time><time datetime="">heute</time>'
        '<time datetime="2026-09-09T08:00:00+02:00">morgens</time>'
        '<time datetime="2026-09-09T17:30:00+02:00">abends</time>', "lxml")
    assert _page_published_at(noisy) == "2026-09-09T08:00:00+02:00"
    # A structured statement always outranks <time>, even when it is older.
    stated = BeautifulSoup(
        '<script type="application/ld+json">'
        '{"@type":"Article","datePublished":"2015-06-24T22:00:00Z","dateModified":"2026-09-10T09:21:29Z"}'
        '</script><time datetime="2026-09-10">heute</time>', "lxml")
    assert _page_published_at(stated) == "2015-06-24T22:00:00+00:00"


def test_hint_dates_carry_their_provenance_into_the_stored_body(project, monkeypatch):
    db, _ = project
    with session(db) as conn:
        run_id = start_run(conn, "news", "a", "b")
        store_hints(conn, run_id, "trade.test", [
            ArticleHint("https://trade.test/feed-story", datetime(2026, 9, 1), "F", "rss",
                        date_source="feed"),
            ArticleHint("https://trade.test/lastmod-story", datetime(2026, 9, 1), None,
                        "sitemap", date_source="lastmod"),
            ArticleHint("https://trade.test/undated", None, "U", "frontpage"),
        ], full_text=True)
    labels = {r["external_id"]: json.loads(r["payload"])["published_at_source"]
              for r in rows(project)}
    assert labels == {"https://trade.test/feed-story": "feed",
                      "https://trade.test/lastmod-story": "lastmod",
                      "https://trade.test/undated": None}
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    run(project)
    enriched = {r["external_id"]: json.loads(r["payload"]) for r in rows(project)
                if json.loads(r["payload"]).get("body_text")}
    assert {k: v["published_at_source"] for k, v in enriched.items()} == labels


def test_legacy_discovery_label_is_read_as_its_method_without_a_new_version(project, monkeypatch):
    discover(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    run(project)
    with session(project[0]) as conn:
        conn.execute("UPDATE raw_item SET payload=json_set(payload, "
                     "'$.published_at_source', 'discovery') WHERE version=2")
    # Nothing about the page changed, so relabelling alone must not version it.
    assert run(project, refresh=True)["stored"] == 0
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success("A changed body"))
    assert run(project, refresh=True)["stored"] == 1
    assert json.loads(rows(project)[-1]["payload"])["published_at_source"] == "sitemap"


def test_failures_are_retired_after_the_attempt_cap_and_reopened_explicitly(project, monkeypatch):
    monkeypatch.setattr("src.bodies.BODY_FETCH_MAX_ATTEMPTS", 3)
    discover(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: BodyResult("failed", error="timeout"))
    assert [run(project)["failed"] for _ in range(2)] == [1, 1]
    third = run(project)
    assert (third["failed"], third["unavailable"]) == (0, 1)
    with session(project[0]) as conn:
        task = conn.execute("SELECT * FROM body_fetch").fetchone()
    assert (task["status"], task["attempts"]) == ("unavailable", 3)
    assert task["error"] == "gave up after 3 attempts: timeout"
    assert run(project)["attempted"] == 0
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: success())
    assert run(project, retry_unavailable=True)["ok"] == 1
    with session(project[0]) as conn:
        assert conn.execute("SELECT attempts FROM body_fetch").fetchone()[0] == 0


def test_a_changed_hint_reopens_a_retired_url_with_a_fresh_budget(project, monkeypatch):
    monkeypatch.setattr("src.bodies.BODY_FETCH_MAX_ATTEMPTS", 2)
    discover(project)
    monkeypatch.setattr("src.bodies.fetch_body", lambda url: BodyResult("failed", error="timeout"))
    run(project)
    run(project)
    with session(project[0]) as conn:
        assert conn.execute("SELECT status FROM body_fetch").fetchone()[0] == "unavailable"
    discover(project, date="2026-09-09T00:00:00+00:00")
    with session(project[0]) as conn:
        task = conn.execute("SELECT status, attempts FROM body_fetch").fetchone()
    assert (task["status"], task["attempts"]) == ("pending", 0)


def test_corrected_page_date_versions_an_unchanged_body(project, monkeypatch):
    discover(project)
    monkeypatch.setattr(
        "src.bodies.fetch_body",
        lambda url: BodyResult("ok", text="The article body", title="Headline",
                               published_at="2026-09-01T12:00:00+02:00",
                               published_at_raw="Di, 09/01/2026 - 12:00"))
    run(project)
    monkeypatch.setattr(
        "src.bodies.fetch_body",
        lambda url: BodyResult("ok", text="The article body", title="Headline",
                               published_at="2026-09-01T12:05:00+02:00",
                               published_at_raw="Di, 09/01/2026 - 12:05"))

    summary = run(project, refresh=True)

    assert summary["stored"] == 1
    assert rows(project)[-1]["published_at"] == "2026-09-01T12:05:00+02:00"


# ── the fetch escalation ladder ───────────────────────────────────────────

def test_pdf_bytes_are_extracted_not_rejected(monkeypatch):
    """Regulators publish press releases as PDF; those used to be discarded."""
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    from io import BytesIO
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    pdf = buffer.getvalue()
    assert pdf[:5] == b"%PDF-"

    # A blank page carries no text layer, which is the scanned-document case:
    # permanent, so unavailable rather than a retryable failure.
    result = _extract_pdf(pdf, "https://reg.test/release.pdf")
    assert result.status == "unavailable"
    assert "no text layer" in result.error


def test_a_corrupt_pdf_stays_retryable():
    """A truncated download can succeed next time, so it must not be final."""
    result = _extract_pdf(b"%PDF-1.4 truncated", "https://reg.test/x.pdf")
    assert result.status == "failed"
    assert "PDF parse failed" in result.error


def test_pdf_is_detected_by_bytes_when_the_header_lies(monkeypatch):
    """Servers mislabel. HTML sent as application/pdf must still extract."""
    respond(monkeypatch, ARTICLE, 200, "application/pdf")
    result = fetch_body("https://trade.test/article")
    assert result.status == "ok"


def test_browser_escalates_only_on_a_js_only_page(monkeypatch):
    """The expensive rung must not fire on ordinary failures."""
    calls = []
    monkeypatch.setattr("src.bodies._fetch_rendered",
                        lambda url: calls.append(url) or BodyResult(
                            "ok", text="Rendered body text " * 40, title="Rendered"))

    respond(monkeypatch, '<html><body><div id="app"></div></body></html>')
    assert fetch_body("https://trade.test/article").status == "ok"
    assert calls == ["https://trade.test/article"], "JS-only page should escalate"

    calls.clear()
    respond(monkeypatch, ARTICLE, 404, "text/html")
    fetch_body("https://trade.test/article")
    assert calls == [], "a 404 is not something a browser fixes"


def test_a_failed_browser_keeps_the_cheaper_diagnosis(monkeypatch):
    monkeypatch.setattr("src.bodies._fetch_rendered",
                        lambda url: BodyResult("failed", error="browser returned nothing"))
    respond(monkeypatch, '<html><body><div id="app"></div></body></html>')
    result = fetch_body("https://trade.test/article")
    assert result.status == "failed", "still retryable"
    assert NO_ARTICLE_TEXT in result.error
    assert "browser" in result.error


def test_a_subscriber_login_page_is_not_stored_as_an_article(monkeypatch):
    """DVZ answers 200 with its login page and no paywall markup at all.

    Nothing above catches it, so 103 words of "Jetzt 4 Wochen kostenlos testen"
    was being stored as article text and would have reached the LLM as one.
    """
    login = ARTICLE.replace("<title>Neue Regeln", "<title>Login - DVZ").replace(
        "<h1>Neue Regeln für Händler</h1>", "<h1>Login - DVZ</h1>")
    respond(monkeypatch, login)
    result = fetch_body("https://trade.test/article")
    assert result.status == "unavailable"
    assert result.error == "subscriber login page"
    assert result.text is None


def test_an_article_may_still_mention_a_subscription(monkeypatch):
    """The phrase test must not swallow real reporting about subscription models."""
    from src.bodies import _is_login_wall
    long_article = "Das Unternehmen bietet ein Probeabo an. " * 60
    assert not _is_login_wall("Temu startet Abo-Modell", long_article)
    assert _is_login_wall(None, "Login für Abonnenten. Jetzt 4 Wochen kostenlos testen.")


def test_a_blank_browser_document_stays_retryable(monkeypatch):
    """A swallowed navigation error must not look like a page without an article.

    fetch_html_with_playwright ignores goto() failures and returns whatever the
    browser shows - 39 bytes of empty document for an unreachable host. Treating
    that as "not an article" would permanently retire a URL over a DNS blip.
    """
    monkeypatch.setattr("src.bodies.fetch_html_with_playwright", None, raising=False)
    monkeypatch.setattr(
        "vendor.newscrawler.crawler_playwright.fetch_html_with_playwright",
        lambda url, timeout_ms=0: b"<html><head></head><body></body></html>")
    respond(monkeypatch, '<html><body><div id="app"></div></body></html>')
    result = fetch_body("https://trade.test/article")
    assert result.status == "failed", "a transport failure must stay retryable"
    assert "blank document" in result.error


def test_a_page_empty_even_after_rendering_stops_being_retried(monkeypatch):
    """Rendered and still empty means it is not an article: a listing, a form.

    Without this every such URL costs a browser launch on every run forever.
    Measured on real data: dslv.org/positionen/stellungnahmen (a listing) and a
    dvz.de webinar form both render fine and contain no article.
    """
    monkeypatch.setattr("src.bodies._fetch_rendered",
                        lambda url: BodyResult("failed", error=NO_ARTICLE_TEXT))
    respond(monkeypatch, '<html><body><div id="app"></div></body></html>')
    result = fetch_body("https://trade.test/article")
    assert result.status == "unavailable", "must not be retried again"
    assert "even after rendering" in result.error


def test_browser_escalation_can_be_switched_off(monkeypatch):
    calls = []
    monkeypatch.setattr("src.bodies.BODY_FETCH_BROWSER", False)
    monkeypatch.setattr("src.bodies._fetch_rendered", lambda url: calls.append(url))
    respond(monkeypatch, '<html><body><div id="app"></div></body></html>')
    assert fetch_body("https://trade.test/article").status == "failed"
    assert calls == []


def test_paywall_login_is_skipped_without_credentials(monkeypatch):
    """An unsubscribed site must stay a cheap skip, never a browser launch."""
    paywalled = ARTICLE.replace(
        "<head>", '<head><script type="application/ld+json">'
                  '{"isAccessibleForFree": false}</script>')
    launched = []
    monkeypatch.setattr("src.bodies._fetch_subscriber",
                        lambda url: launched.append(url) or None)
    respond(monkeypatch, paywalled)
    result = fetch_body("https://trade.test/article")
    assert result.status == "unavailable"
    assert result.error == PAYWALL_DECLARED, "error must stay clean when no login applies"


def test_public_article_never_checks_subscriber_credentials(monkeypatch):
    def unexpected(url):
        pytest.fail("a public article must not reach the subscriber rung")

    monkeypatch.setattr("src.bodies._fetch_subscriber", unexpected)
    respond(monkeypatch, ARTICLE)
    assert fetch_body("https://trade.test/article").status == "ok"


def test_subscriber_rung_loads_dotenv_before_checking_credentials(monkeypatch):
    import vendor.newscrawler.paywall.handler as handler

    email_env, password_env = "TEST_SUBSCRIBER_EMAIL", "TEST_SUBSCRIBER_PASSWORD"
    monkeypatch.delenv(email_env, raising=False)
    monkeypatch.delenv(password_env, raising=False)
    loaded = []

    def fake_load(path):
        loaded.append(Path(path))
        os.environ[email_env] = "subscriber@example.test"
        os.environ[password_env] = "secret"
        return True

    monkeypatch.setattr("src.bodies.load_dotenv", fake_load)
    monkeypatch.setattr(handler, "get_paywall_cfg", lambda url: {
        "email_env": email_env, "password_env": password_env,
    })
    monkeypatch.setattr(handler, "fetch_paywall_article", lambda url, cfg: ARTICLE)

    result = _fetch_subscriber("https://trade.test/article")

    assert result is not None and result.status == "ok"
    assert len(loaded) == 1 and loaded[0].name == ".env"


def test_subscriber_login_recovers_a_paywalled_article(monkeypatch):
    paywalled = ARTICLE.replace(
        "<head>", '<head><script type="application/ld+json">'
                  '{"isAccessibleForFree": false}</script>')
    monkeypatch.setattr("src.bodies._fetch_subscriber",
                        lambda url: BodyResult("ok", text="Full subscriber text " * 40,
                                               title="Full article"))
    respond(monkeypatch, paywalled)
    result = fetch_body("https://trade.test/article")
    assert result.status == "ok"
    assert result.title == "Full article"
