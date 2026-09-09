# Source audit — reusable prompt

Every source examined closely so far has turned up something that changed the
configuration. This is the method, written so it can be run against the remaining
sources in a fresh session.

Audited so far: Verbraucherzentrale (narrowed 766→203), BVL (narrowed 2002→174),
LOGISTIK HEUTE (partial paywall found), Zoll (dropped — publishes drug seizures, not
customs policy), BPEX (wrong company entirely), DVZ (sitemap pruning bug, 13→73),
bevh (JS-rendered, bodies unobtainable).

**Not yet audited:** DER SPIEGEL, DIE ZEIT, FAZ, Handelsblatt, WELT,
WirtschaftsWoche, Süddeutsche, Tagesschau, etailment, VerkehrsRundschau,
Onlinehändler-News, t3n, e-commerce Magazin, DSLV, BGL, BVDW, Händlerbund,
Wettbewerbszentrale, and all eight regulatory sources.

---

## The prompt

> Audit the source `<NAME>` in `input/germany_medias.json` (or
> `input/regulatory_sources.json`). We have ~14,000 rows already collected in
> `data/brandmonitor.sqlite3`, so work from stored data where possible and only
> fetch when the question needs it. Do not change any config until the findings are
> in and I have agreed to them.
>
> Answer these, with numbers rather than impressions:
>
> **1. What is this source actually publishing?**
> Break its stored URLs down by top-level path segment, then two levels deep for the
> largest sections. Distinguish articles from website furniture — navigation,
> membership and contact pages, PDF downloads, event calendars, author and tag
> indexes, English mirrors of the same content, paginated list pages.
>
> **2. Do the rows carry titles?**
> `SELECT COUNT(*) FROM raw_item WHERE source_slug=? AND title IS NOT NULL`.
> A sitemap that ships bare URLs makes title-based filtering impossible and changes
> what the source is worth in `title_only` mode.
>
> **3. What does it yield?**
> Count brand matches (J&T, 极兔, Temu, Shein, AliExpress, TikTok Shop) and sector
> matches (Paket, KEP, Zoll, Einfuhr, E-Commerce, Marktplatz, Produktsicherheit,
> Plattform, DSA, Lieferkette, China) over title plus URL slug. Show the matching
> headlines, not just the count — a count of 13 that is all tag pages is a count of
> zero.
>
> **4. Events or evergreen?**
> Fetch two or three matching pages and read them. A permanent guide that the CMS
> re-touches every few months will resurface as "changed" forever and be re-assessed
> for nothing. Monitoring wants events. Check whether many URLs share one identical
> `lastmod` — that is bulk republication, not editorial activity.
>
> **5. Paywall reality.**
> Sample recent articles **indiscriminately**, not only the ones matching our brands,
> and read each publisher's own schema.org `isAccessibleForFree`. Report per section,
> because paywalls are usually sectional.
>
> **6. Verdict.** One of:
> - keep as configured
> - narrow, with the exact `allowed_dirs` and the measured before/after count
> - drop, with what is lost and whether another source covers it
>
> Say which findings are firm and which rest on a small sample.

---

## Traps this method has already caught

Each of these produced a wrong answer first time and was only caught by checking.

**A 200 response is not identification.** `bpex.de` resolved fine and was an
eleven-page software consultancy. The client's BPEX is the former BIEK at
`bpex-ev.de`. Confirm the organisation, not just the host.

**Marker text lies.** "abonnement" appears on all 20 LOGISTIK HEUTE pages sampled —
it is footer promo. A marker-based paywall check would have condemned the whole
site. Use `isAccessibleForFree`.

**Sampling the interesting subset lies.** LOGISTIK HEUTE read as fully free because
the five brand-matching articles all happened to be `/news/`; its `/fachmagazin/` is
paid. Sample broadly, then look at the interesting subset separately.

**Crawlable is not valuable.** Zoll is scrapeable, and publishes drug seizures and
open days. Its customs *policy* comes from the Commission and the finance ministry,
and its decisions reach us through the trade press anyway — 178 customs articles
across the configured sources in 120 days.

**High volume is not high signal, and low signal is not zero.** Verbraucherzentrale
was 82% of the regulatory table at 1.7% relevance; BVL was 31% of the body backlog
with 0 brand hits. Both were narrowed rather than dropped, because each held a small
core worth keeping — class actions and court rulings in one, a blog post on
de-minimis and Chinese platforms in the other.

**Narrow with `allowed_dirs`, do not delete the source.** It is config, reversible,
and `run_body_fetch` re-checks it against current config when selecting tasks, so
narrowing immediately stops the wasted fetches without touching stored rows.

**Remember what `allowed_dirs` means per method** — filter for `sitemap`,
navigation seed for `frontpage`, ignored for `feeds`. See CLAUDE.md. And FAZ nests
everything under `/aktuell/`, so depth-1 paths are useless there.

---

## Where the leverage probably is

Ranked by how much a finding would change things:

1. **etailment** — 2,000 items and 165 brand hits, the single most valuable news
   source. Worth confirming its `/magazin`, `/themen`, `/autoren` and `/format`
   split, since author and topic index pages are in there.
2. **DIE ZEIT and WELT** — 2,000 and 1,035 items in `title_only`. ZEIT is 78% dpa
   wire, WELT 53% `regionales`. Neither is fetched for bodies, so the cost is only
   storage, but the question is whether `regionales` is worth indexing at all.
3. **The eight regulators** — small volumes, but EU presscorner returned 0 relevant
   of 50 and needs topic filtering, and BNetzA's feeds are mostly energy.
4. **Händlerbund vs Onlinehändler-News** — same organisation, both configured.
   Check whether their content overlaps before paying to fetch both.
5. **The remaining associations** (DSLV 13, BGL 2, BVDW 143, Wettbewerbszentrale 47)
   — small enough that the answer is probably "leave it", which is worth confirming
   cheaply rather than assuming.
