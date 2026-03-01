"""Crawler for TWiki sites (e.g. CERN CMS WorkBook).

Discovery strategy
------------------
Fetches ``WebTopicList`` in the configured TWiki namespace to obtain all topic
names in a single request, then crawls each page with ``?skin=text`` for
clean, boilerplate-free Markdown output.

Steps:
1. Derive the namespace URL from the first ``start_url``, e.g.
   ``https://twiki.cern.ch/twiki/bin/view/CMSPublic``.
2. Fetch ``<namespace>/WebTopicList`` and extract all topic links.
3. Filter by namespace prefix, exclude patterns, and skip non-view paths.
4. Crawl each topic URL with ``?skin=text`` appended for clean content.

No authentication required for the public CMS WorkBook.  If you need to
crawl a protected TWiki web, add an ``auth`` block to the site config (same
approach as wikijs.py).
"""

from __future__ import annotations

import fnmatch
import logging
from urllib.parse import urljoin, urlparse

from crawl4ai import AsyncWebCrawler

from ..output.models import PageResult
from .base import BaseCrawler

logger = logging.getLogger(__name__)

# TWiki action path segments that do NOT represent readable content pages.
_SKIP_ACTIONS = frozenset(
    ["edit", "attach", "rdiff", "diff", "oops", "search", "manage",
     "rename", "preview", "rest", "login", "logon", "viewfile"]
)


class TWikiCrawler(BaseCrawler):
    """TWiki crawler: WebTopicList discovery + ``?skin=text`` content fetch."""

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_urls(self, crawler: AsyncWebCrawler) -> list[str]:
        """Fetch WebTopicList to enumerate all topics without BFS."""
        start = list(self.site_config.start_urls) or [self.site_config.base_url]
        filters = self.site_config.filters

        ns_prefix = self._namespace_prefix(start[0])
        parsed_start = urlparse(start[0])
        base_netloc = parsed_start.netloc
        base_scheme = parsed_start.scheme

        topic_list_url = f"{base_scheme}://{base_netloc}{ns_prefix}/WebTopicList"
        logger.info("[%s] Fetching topic list: %s", self.site_config.name, topic_list_url)

        result = await crawler.arun(url=topic_list_url, config=self.make_discovery_config())
        if not result.success:
            logger.warning(
                "[%s] WebTopicList fetch failed (%s) — no pages discovered",
                self.site_config.name,
                getattr(result, "error_message", ""),
            )
            return []

        seen: set[str] = set()
        discovered: list[str] = []
        all_links = result.links.get("internal", []) + result.links.get("external", [])

        for link in all_links:
            href = (link.get("href") or "").strip()
            if not href:
                continue
            full = urljoin(topic_list_url, href)
            parsed = urlparse(full)
            if parsed.netloc != base_netloc:
                continue
            norm = self._normalize(full)
            if not self._is_view_url(norm):
                continue
            if not parsed.path.startswith(ns_prefix):
                continue
            if self._is_excluded(norm, filters.exclude_patterns):
                continue
            if norm in seen:
                continue
            seen.add(norm)
            discovered.append(norm)

        discovered.sort()
        logger.info(
            "[%s] Discovered %d TWiki pages from WebTopicList",
            self.site_config.name, len(discovered),
        )
        return discovered

    # ------------------------------------------------------------------
    # Content fetch — ?skin=text strips navigation/boilerplate
    # ------------------------------------------------------------------

    async def crawl_page(
        self, crawler: AsyncWebCrawler, url: str
    ) -> PageResult | None:
        """Fetch *url* with ``?skin=text`` for clean, boilerplate-free content."""
        if url in self._visited:
            return None
        self._visited.add(url)

        sep = "&" if "?" in url else "?"
        fetch_url = f"{url}{sep}skin=text"

        try:
            result = await crawler.arun(url=fetch_url, config=self.make_run_config())
        except Exception:
            logger.exception("Exception while fetching %s", url)
            return None

        status = getattr(result, "status_code", None)
        if status and status >= 400:
            logger.warning("HTTP %d — skipping: %s", status, url)
            return None

        if not result.success:
            logger.warning("Failed [%s]: %s", url, getattr(result, "error_message", ""))
            return None

        return self._build_page_result(url, result)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _namespace_prefix(url: str) -> str:
        """Return the TWiki namespace path prefix, e.g. ``/twiki/bin/view/CMSPublic``."""
        parsed = urlparse(url)
        parts = parsed.path.split("/")
        try:
            view_idx = parts.index("view")
            if len(parts) > view_idx + 1:
                return "/".join(parts[: view_idx + 2])
        except ValueError:
            pass
        return parsed.path

    @staticmethod
    def _normalize(url: str) -> str:
        """Strip query string, fragment, and trailing slash."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")

    @staticmethod
    def _is_view_url(url: str) -> bool:
        """Return True only for ``/twiki/bin/view/`` content pages."""
        parsed = urlparse(url)
        path = parsed.path
        if "/twiki/bin/view/" not in path:
            return False
        for action in _SKIP_ACTIONS:
            if f"/bin/{action}/" in path or path.endswith(f"/bin/{action}"):
                return False
        return True

    @staticmethod
    def _is_excluded(url: str, patterns: list[str]) -> bool:
        path = urlparse(url).path
        return any(fnmatch.fnmatch(path, p) for p in patterns)
