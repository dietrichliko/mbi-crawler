"""Crawler for the OEAW / MBI public website.

Discovery strategy
------------------
1. Fetch the sitemap (supports sitemap-index → child sitemaps recursively).
2. Filter the URL list with include/exclude patterns from the site config.
3. Crawl each URL sequentially within the rate-limit budget.

No login required.  robots.txt is respected by keeping the delay ≥ 1 s.
"""

from __future__ import annotations

import fnmatch
import logging
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from crawl4ai import AsyncWebCrawler  # type: ignore[import]

from ..config.models import SiteConfig, AppConfig
from .base import BaseCrawler

logger = logging.getLogger(__name__)

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class OEAWCrawler(BaseCrawler):
    """Sitemap-driven crawler for oeaw.ac.at / MBI."""

    async def discover_urls(self, crawler: AsyncWebCrawler) -> list[str]:
        urls: list[str] = []

        if self.site_config.sitemap_url:
            urls = await self._fetch_sitemap(self.site_config.sitemap_url)
            logger.info("[%s] Sitemap yielded %d raw URLs", self.site_config.name, len(urls))

        if not urls:
            urls = list(self.site_config.start_urls) or [self.site_config.base_url]

        return self._filter(urls)

    # ------------------------------------------------------------------
    # Sitemap helpers
    # ------------------------------------------------------------------

    async def _fetch_sitemap(self, url: str) -> list[str]:
        """Recursively fetch sitemap / sitemap-index XML, return all page URLs."""
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                xml_text = resp.text
        except Exception:
            logger.exception("Could not fetch sitemap: %s", url)
            return []

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            logger.warning("Could not parse sitemap XML from %s", url)
            return []

        # Sitemap index → recurse into child sitemaps.
        child_sitemaps = root.findall("sm:sitemap/sm:loc", _SITEMAP_NS)
        if child_sitemaps:
            all_urls: list[str] = []
            for node in child_sitemaps:
                child_url = (node.text or "").strip()
                if child_url:
                    all_urls.extend(await self._fetch_sitemap(child_url))
            return all_urls

        # Regular sitemap → collect <url><loc> entries.
        return [
            (loc.text or "").strip()
            for loc in root.findall("sm:url/sm:loc", _SITEMAP_NS)
            if loc.text
        ]

    # ------------------------------------------------------------------
    # URL filtering
    # ------------------------------------------------------------------

    def _filter(self, urls: list[str]) -> list[str]:
        filters = self.site_config.filters
        seen: set[str] = set()
        result: list[str] = []

        for raw_url in urls:
            url = raw_url.strip()
            if filters.strip_query_params:
                url = url.split("?")[0].split("#")[0]

            parsed = urlparse(url)
            path = parsed.path

            # Include filter — match against full URL.
            if filters.include_patterns:
                if not any(fnmatch.fnmatch(url, p) for p in filters.include_patterns):
                    continue

            # Exclude filter — match against path (simpler patterns in YAML).
            if any(fnmatch.fnmatch(path, p) for p in filters.exclude_patterns):
                continue

            if url not in seen:
                seen.add(url)
                result.append(url)

        logger.info("[%s] After filtering: %d URLs", self.site_config.name, len(result))
        return result
