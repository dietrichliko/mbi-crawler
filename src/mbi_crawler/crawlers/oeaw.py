"""Crawler for the OEAW / MBI public website.

Discovery strategy
------------------
1. Fetch the sitemap (supports sitemap-index → child sitemaps recursively).
2. Filter the URL list with include/exclude patterns from the site config.
3. Crawl each URL sequentially within the rate-limit budget.

No login required.  robots.txt is respected by keeping the delay ≥ 1 s.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx
from crawl4ai import AsyncWebCrawler

from .base import BaseCrawler

logger = logging.getLogger(__name__)

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class OEAWCrawler(BaseCrawler):
    """Sitemap-driven crawler for oeaw.ac.at / MBI."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Populated during BFS: DE page URL → EN alternate URL (language switcher).
        self._en_alternates: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Run loop — extends base to also crawl EN language alternates
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        rate = self.site_config.rate_limit
        sem = asyncio.Semaphore(rate.max_concurrent)

        async with AsyncWebCrawler(config=self.make_browser_config()) as crawler:
            await self.setup(crawler)
            urls = await self.discover_urls(crawler)
            logger.info("[%s] Discovered %d URLs", self.site_config.name, len(urls))
            logger.info(
                "[%s] Found %d EN alternates", self.site_config.name, len(self._en_alternates)
            )

            async def _fetch_de(url: str) -> None:
                async with sem:
                    page = await self.crawl_page(crawler, url)
                    if page:
                        out = self.writer.write(page, self.site_config)
                        logger.debug("Wrote DE %s → %s", url, out)
                await asyncio.sleep(rate.delay)

            await asyncio.gather(*[_fetch_de(u) for u in urls])

            async def _fetch_en(de_url: str, en_url: str) -> None:
                async with sem:
                    page = await self.crawl_page(crawler, en_url)
                    if page:
                        out = self.writer.write(page, self.site_config, lang="en", path_url=de_url)
                        logger.debug("Wrote EN %s → %s", en_url, out)
                await asyncio.sleep(rate.delay)

            await asyncio.gather(*[_fetch_en(de, en) for de, en in self._en_alternates.items()])

        manifest = self.writer.write_manifest(self.site_config)
        logger.info(
            "[%s] Done — %d pages, manifest: %s",
            self.site_config.name,
            len(self.writer._manifest),
            manifest,
        )

    async def discover_urls(self, crawler: AsyncWebCrawler) -> list[str]:
        if self.site_config.sitemap_url:
            raw = await self._fetch_sitemap(self.site_config.sitemap_url)
            logger.info("[%s] Sitemap yielded %d raw URLs", self.site_config.name, len(raw))
            filtered = self._filter(raw)
            if filtered:
                return filtered
            logger.info(
                "[%s] Sitemap had no matching URLs — falling back to BFS", self.site_config.name
            )

        return await self._bfs_discover(crawler)

    # ------------------------------------------------------------------
    # BFS fallback
    # ------------------------------------------------------------------

    async def _bfs_discover(self, crawler: AsyncWebCrawler) -> list[str]:
        """Parallel BFS from start_urls, constrained by include/exclude filters.

        Each depth level fetches all frontier URLs concurrently (up to
        ``max_concurrent`` slots).  Results are cached in ``_bfs_cache`` so
        the subsequent crawl pass can skip re-fetching them.
        """
        start = list(self.site_config.start_urls) or [self.site_config.base_url]
        filters = self.site_config.filters
        base_netloc = urlparse(self.site_config.base_url).netloc
        rate = self.site_config.rate_limit
        sem = asyncio.Semaphore(rate.max_concurrent)

        discovered: set[str] = set()
        frontier: list[str] = []
        for u in start:
            norm = self._normalise(u)
            if norm not in discovered:
                discovered.add(norm)
                frontier.append(norm)

        async def _fetch_links(url: str) -> list[str]:
            async with sem:
                result = await crawler.arun(url=url, config=self.make_discovery_config())
            await asyncio.sleep(rate.delay)
            if not result.success:  # pyright: ignore[reportAttributeAccessIssue]
                return []
            # Capture the EN language-switcher alternate for this DE page.
            for link in result.links.get("internal", []):  # pyright: ignore[reportAttributeAccessIssue]
                if (
                    link.get("text", "").strip() == "EN"
                    and "select your language" in link.get("title", "").lower()
                ):
                    en_norm = self._normalise(link.get("href", ""))
                    if en_norm:
                        self._en_alternates[url] = en_norm
                    break
            links: list[str] = []
            for link in result.links.get("internal", []):  # pyright: ignore[reportAttributeAccessIssue]
                href = (link.get("href") or "").strip()
                if not href:
                    continue
                full = urljoin(url, href)
                parsed = urlparse(full)
                if filters.same_domain_only and parsed.netloc != base_netloc:
                    continue
                norm = self._normalise(full)
                if norm in discovered:
                    continue
                if filters.include_patterns and not any(
                    fnmatch.fnmatch(norm, p) for p in filters.include_patterns
                ):
                    continue
                if any(fnmatch.fnmatch(parsed.path, p) for p in filters.exclude_patterns):
                    continue
                links.append(norm)
            return links

        for depth in range(filters.max_depth):
            if not frontier:
                break
            link_lists = await asyncio.gather(*[_fetch_links(u) for u in frontier])
            next_frontier: list[str] = []
            for links in link_lists:
                for link in links:
                    if link not in discovered:
                        discovered.add(link)
                        next_frontier.append(link)
            logger.debug(
                "[%s] BFS depth %d → %d new pages",
                self.site_config.name,
                depth + 1,
                len(next_frontier),
            )
            frontier = next_frontier

        logger.info("[%s] BFS discovered %d URLs", self.site_config.name, len(discovered))
        return sorted(discovered)

    @staticmethod
    def _normalise(url: str) -> str:
        """Strip query string and fragment; normalise http → https."""
        if url.startswith("http://"):
            url = "https://" + url[7:]
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")

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
            # Sitemap may use http:// even for HTTPS-only sites — normalise.
            if url.startswith("http://"):
                url = "https://" + url[7:]
            if filters.strip_query_params:
                url = url.split("?")[0].split("#")[0]

            parsed = urlparse(url)
            path = parsed.path

            # Include filter — match against full URL.
            if filters.include_patterns and not any(
                fnmatch.fnmatch(url, p) for p in filters.include_patterns
            ):
                continue

            # Exclude filter — match against path (simpler patterns in YAML).
            if any(fnmatch.fnmatch(path, p) for p in filters.exclude_patterns):
                continue

            if url not in seen:
                seen.add(url)
                result.append(url)

        logger.info("[%s] After filtering: %d URLs", self.site_config.name, len(result))
        return result
