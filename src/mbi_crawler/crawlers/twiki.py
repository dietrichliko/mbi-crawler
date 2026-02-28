"""Crawler for TWiki sites (e.g. CERN CMS WorkBook).

Discovery strategy
------------------
BFS link-following from ``start_urls``, restricted to:

* Same host as the start URL.
* URLs that match the TWiki ``/twiki/bin/view/`` path pattern.
* The same TWiki *namespace* (web) as the start page, e.g. ``CMSPublic``.
  This prevents the crawl from wandering into unrelated TWiki webs.

Only ``/bin/view/`` action URLs are collected; edit, attach, diff, search,
and other action paths are skipped.

Query parameters are stripped from discovered URLs because TWiki uses them
for sorting/pagination, not for unique content.

No authentication required for the public CMS WorkBook.  If you need to
crawl a protected TWiki web, add an ``auth`` block to the site config (same
approach as wikijs.py).
"""

from __future__ import annotations

import fnmatch
import logging
from urllib.parse import urljoin, urlparse

from crawl4ai import AsyncWebCrawler  # type: ignore[import]

from .base import BaseCrawler

logger = logging.getLogger(__name__)

# TWiki action path segments that do NOT represent readable content pages.
_SKIP_ACTIONS = frozenset(
    ["edit", "attach", "rdiff", "diff", "oops", "search", "manage",
     "rename", "preview", "rest", "login", "logon", "viewfile"]
)


class TWikiCrawler(BaseCrawler):
    """BFS crawler for TWiki sites, namespace-confined."""

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_urls(self, crawler: AsyncWebCrawler) -> list[str]:
        """BFS over TWiki view links, staying in one namespace."""
        start = list(self.site_config.start_urls) or [self.site_config.base_url]
        filters = self.site_config.filters

        # Derive the allowed namespace prefix from the first start URL.
        ns_prefix = self._namespace_prefix(start[0])
        base_netloc = urlparse(start[0]).netloc
        logger.info("[%s] Namespace prefix: %s", self.site_config.name, ns_prefix)

        discovered: set[str] = set()
        frontier: list[str] = []

        for u in start:
            norm = self._normalize(u)
            if norm not in discovered:
                discovered.add(norm)
                frontier.append(norm)

        for depth in range(filters.max_depth):
            if not frontier:
                break
            next_frontier: list[str] = []

            for url in frontier:
                result = await crawler.arun(url=url, config=self.make_run_config())
                if not result.success:
                    continue

                # TWiki pages link to both internal and external targets.
                all_links = (
                    result.links.get("internal", []) + result.links.get("external", [])
                )
                for link in all_links:
                    href = (link.get("href") or "").strip()
                    if not href:
                        continue

                    full = urljoin(url, href)
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
                    if norm in discovered:
                        continue

                    discovered.add(norm)
                    next_frontier.append(norm)

            logger.debug(
                "[%s] BFS depth %d → %d new pages",
                self.site_config.name, depth + 1, len(next_frontier),
            )
            frontier = next_frontier

        logger.info("[%s] Discovered %d TWiki pages", self.site_config.name, len(discovered))
        return sorted(discovered)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _namespace_prefix(url: str) -> str:
        """Return the TWiki namespace path prefix, e.g. ``/twiki/bin/view/CMSPublic``.

        If the URL is not a TWiki view URL the full path is returned as-is.
        """
        parsed = urlparse(url)
        parts = parsed.path.split("/")
        try:
            view_idx = parts.index("view")
            # prefix = everything up to and including the namespace segment
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
        # Reject non-view action paths embedded after the namespace.
        for action in _SKIP_ACTIONS:
            if f"/bin/{action}/" in path or path.endswith(f"/bin/{action}"):
                return False
        return True

    @staticmethod
    def _is_excluded(url: str, patterns: list[str]) -> bool:
        path = urlparse(url).path
        return any(fnmatch.fnmatch(path, p) for p in patterns)
