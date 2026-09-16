#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Source Loader (singleton)
-------------------------
Loads news source configurations once, caches them.

Usage:
    from vendor.newscrawler.source_loader import sources

    sources.load_sources("input/germany_medias.json")

    rules = sources.get_site_rules("https://example.com/article")

    if sources.is_brightdata_enabled(url):
        use_brightdata()
"""

import json
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List
from src.logger import get_logger

logger = get_logger(__name__)


class SourceLoader:
    """
    Singleton loader for news source configs.
    """
    _instance = None
    _sources = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load_sources(self, config_path: str) -> List[Dict[str, Any]]:
        """
        Load source configurations once, cache them.
        Accepts either:
          - a JSON array file: [ {...}, {...}, ... ]
          - NDJSON: one JSON object per line
        Returns a list of dicts that have at least a 'url' field.

        Raises:
            FileNotFoundError: If config_path does not exist
            ValueError: If config file is invalid
        """
        if self._sources is None:
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()

            sources: List[Dict[str, Any]] = []
            if not content:
                self._sources = sources
                return self._sources

            # Try JSON array first
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    sources = parsed
                elif isinstance(parsed, dict):
                    # single object file
                    sources = [parsed]
                else:
                    raise ValueError("Unsupported JSON top-level type")
            except Exception:
                # Fallback: NDJSON
                sources = []
                with open(config_path, 'r', encoding='utf-8') as f2:
                    for line in f2:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            sources.append(json.loads(line))
                        except Exception:
                            # skip bad lines, but continue
                            continue

            self._sources = sources
            logger.info("[sources] Loaded %d sources from %s", len(self._sources), config_path)
        return self._sources

    def get_site_rules(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Get source_rules for a given URL.
        Returns dict with: sitemap, feeds, frontpage, brightdata, allowed_dirs, etc.
        Returns None if URL is not in our sources OR if sources haven't been loaded.
        """
        if self._sources is None:
            return None

        url_domain = urlparse(url).netloc.lower()

        for source in self._sources:
            source_url = source.get("url", "")
            if not source_url:
                continue

            source_domain = urlparse(source_url).netloc.lower()

            # Match exact domain or subdomain
            if url_domain == source_domain or url_domain.endswith(f".{source_domain}"):
                return {
                    "sitemap": source.get("sitemap", False),
                    "feeds": source.get("feeds", False),
                    "gov": source.get("gov", False),
                    "frontpage": source.get("frontpage", False),
                    "brightdata": source.get("brightdata", False),
                    "allowed_dirs": source.get("allowed_dirs", []),
                    # Explicit feed URLs for publishers whose feed is neither
                    # advertised on the homepage nor at a conventional path.
                    "feed_urls": source.get("feed_urls", []),
                    # Additive sitemap roots for publishers whose robots.txt is
                    # incomplete. Unlike the fallback guesses, these are used
                    # even when robots.txt declares another sitemap.
                    "extra_sitemap_urls": source.get("extra_sitemap_urls", []),
                }

        return None  # URL not in our sources

    def is_brightdata_enabled(self, url: str) -> bool:
        """Quick check if URL's site has brightdata enabled."""
        rules = self.get_site_rules(url)
        return rules.get("brightdata", False) if rules else False

    def clear_cache(self):
        """Clear cached data (useful for testing or reload)."""
        self._sources = None
        logger.info("[sources] Cache cleared")


# Convenience singleton instance
sources = SourceLoader()
