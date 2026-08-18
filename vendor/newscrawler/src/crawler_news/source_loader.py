#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Source & Blacklist Loader (singleton)
-------------------------------------
Loads news source configurations and blacklisted URLs once, caches them.

Usage:
    from src.crawler_news.source_loader import sources

    sources.load_sources("input/germany_medias.json")
    sources.load_blacklist()

    rules = sources.get_site_rules("https://example.com/article")

    if sources.is_brightdata_enabled(url):
        use_brightdata()

    if sources.is_url_blacklisted(url):
        skip_url()
"""

import json
from urllib.parse import urlparse
from typing import Optional, Dict, Any, Set, List
from pathlib import Path
from src.logger import get_logger

logger = get_logger(__name__)


class SourceLoader:
    """
    Singleton loader for news source configs and blacklisted URLs.
    """
    _instance = None
    _sources = None
    _blacklist_urls = None

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

    def load_blacklist(self, blacklist_path: str = "input/Blacklist/blacklist.jsonl",
                       source_url: Optional[str] = None) -> Set[str]:
        """
        Load blacklisted URLs, but only for domains that are in our sources.
        This prevents loading irrelevant blacklist entries.

        Args:
            blacklist_path: Path to the blacklist JSONL file
            source_url: Optional. If provided, only return blacklist for this specific source's domain.
                       If None/empty, return blacklist for all sources.

        Returns:
            Set of blacklisted URLs (normalized)

        Raises:
            RuntimeError: If sources have not been loaded yet (must call load_sources() first)
        """
        if self._blacklist_urls is None:
            self._blacklist_urls = set()

            # First, ensure sources are loaded
            if self._sources is None:
                raise RuntimeError("Sources must be loaded before loading blacklist. Call load_sources() first.")

            # Get all source domains
            source_domains = set()
            for source in self._sources:
                url = source.get("url", "")
                if url:
                    domain = urlparse(url).netloc.lower()
                    source_domains.add(domain)

            # Load blacklist, filtering by source domains
            try:
                if not Path(blacklist_path).exists():
                    logger.info("[sources] No blacklist file found at %s", blacklist_path)
                    return self._blacklist_urls

                with open(blacklist_path, 'r', encoding='utf-8') as f:
                    total_count = 0
                    filtered_count = 0

                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                            url = entry.get('url')
                            if not url:
                                continue

                            total_count += 1

                            # Only add if URL's domain is in our sources
                            url_domain = urlparse(url).netloc.lower()
                            if url_domain in source_domains:
                                # Normalize URL (import on-demand to avoid circular dependency)
                                from src.crawler_news.crawler import normalize_url
                                self._blacklist_urls.add(normalize_url(url))
                                filtered_count += 1

                        except json.JSONDecodeError:
                            continue

                logger.info("[sources] Loaded %d/%d blacklist URLs (filtered to source domains)", filtered_count, total_count)

            except Exception as e:
                logger.info("[sources] Failed to load blacklist: %s", e)

        # If source_url is specified, filter to only that domain
        if source_url:
            source_domain = urlparse(source_url).netloc.lower()
            return {url for url in self._blacklist_urls
                    if urlparse(url).netloc.lower() == source_domain}

        return self._blacklist_urls

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
                }

        return None  # URL not in our sources

    def is_brightdata_enabled(self, url: str) -> bool:
        """Quick check if URL's site has brightdata enabled."""
        rules = self.get_site_rules(url)
        return rules.get("brightdata", False) if rules else False

    def is_url_blacklisted(self, url: str) -> bool:
        """Check if URL is in blacklist."""
        if self._sources is None:
            return False

        if self._blacklist_urls is None:
            self.load_blacklist()

        # Import on-demand to avoid circular dependency
        from src.crawler_news.crawler import normalize_url
        return normalize_url(url) in self._blacklist_urls

    def clear_cache(self):
        """Clear cached data (useful for testing or reload)."""
        self._sources = None
        self._blacklist_urls = None
        logger.info("[sources] Cache cleared")


# Convenience singleton instance
sources = SourceLoader()
