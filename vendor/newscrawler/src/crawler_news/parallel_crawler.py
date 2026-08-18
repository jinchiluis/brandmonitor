# parallel_crawler.py
import os, json, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

from src.logger import get_logger
logger = get_logger(__name__)

from src.crawler_news.crawler import crawl_site


def _crawl_one_source(src: Dict[str, Any],
                      start, end,
                      max_per_source: int) -> Dict[str, Any]:
    site_url = (src.get("url") or "").strip()
    if not site_url:
        return {"url": None, "recs": [], "error": "missing url"}

    try:
        recs, fetch_title_count = crawl_site(
            site_url,
            start,
            end,
            max_per_source=max_per_source,
        )
        return {"url": site_url, "recs": [r.__dict__ for r in recs], "fetch_title_count": fetch_title_count, "error": None}
    except Exception as e:
        return {"url": site_url, "recs": [], "error": f"{e}\n{traceback.format_exc()}"}

def crawl_window_parallel(
    sources: List[Dict[str, Any]],
    country: str,
    out_path: str,
    start, end,
    *,
    max_per_source: int,
    workers: int,
) -> int:
    """
    Runs site crawls in parallel threads and appends results to JSONL.

    Returns: total_written (number of articles crawled)
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    total_written = 0
    workers = max(1, int(workers or 1))

    logger.info(f"[PARALLEL] Spawning {workers} workers for {len(sources)} sources...")
    with ThreadPoolExecutor(max_workers=workers) as ex, open(out_path, "a", encoding="utf-8") as out_f:
        futures = [
            ex.submit(_crawl_one_source, src, start, end, max_per_source)
            for src in sources
        ]
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if res["error"]:
                logger.error(f"[{i}/{len(futures)}] crawl failed {res['url']}: {res['error']}")
                continue
            for r in res["recs"]:
                out_f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total_written += len(res["recs"])
            ftc = res.get("fetch_title_count", 0)
            suffix = f" ({ftc} fetched title from html)" if ftc else ""
            logger.info(f"[{i}/{len(futures)}] {res['url']} -> {len(res['recs'])} articles{suffix}")

    logger.info(f"[PARALLEL] Done. Appended {total_written} articles to {out_path}")
    return total_written
