"""Abstract base class shared by all site crawlers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

from ..config.models import AppConfig, SiteConfig
from ..output.models import PageResult
from ..output.writer import OutputWriter

logger = logging.getLogger(__name__)


def _extract_markdown(result: Any) -> str:
    """Extract a plain Markdown string from a crawl4ai result.

    Handles both the pre-0.4 API (``result.markdown: str``) and the
    newer API (``result.markdown_v2.raw_markdown``).
    """
    # New API: MarkdownGenerationResult object.
    for attr in ("markdown_v2", "markdown"):
        obj = getattr(result, attr, None)
        if obj is None:
            continue
        raw = getattr(obj, "raw_markdown", None)
        if raw is not None:
            return str(raw)
        if isinstance(obj, str):
            return obj
    return ""


class BaseCrawler(ABC):
    """Common crawl-loop logic.  Subclasses implement :meth:`discover_urls`."""

    def __init__(self, site_config: SiteConfig, app_config: AppConfig) -> None:
        self.site_config = site_config
        self.app_config = app_config
        self.output_dir = Path(app_config.output_base_dir) / site_config.output_subdir
        self.writer = OutputWriter(self.output_dir)
        self._visited: set[str] = set()

    # ------------------------------------------------------------------
    # crawl4ai configuration helpers
    # ------------------------------------------------------------------

    def make_browser_config(self) -> BrowserConfig:
        return BrowserConfig(headless=self.app_config.headless)

    def make_run_config(self) -> CrawlerRunConfig:
        extra = self.site_config.extra or {}
        css_selector: str | None = extra.get("css_selector")
        excluded_selector: str | None = extra.get("excluded_selector")
        word_count_threshold: int = int(extra.get("word_count_threshold", 10))
        return CrawlerRunConfig(
            word_count_threshold=word_count_threshold,
            css_selector=css_selector,
            excluded_selector=excluded_selector,
        )

    def make_discovery_config(self) -> CrawlerRunConfig:
        """Config for BFS link-discovery: no CSS filtering so full-page links are returned."""
        return CrawlerRunConfig(word_count_threshold=10)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def discover_urls(self, crawler: AsyncWebCrawler) -> list[str]:
        """Return the full list of page URLs to crawl for this site."""
        ...

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------

    @abstractmethod
    async def setup(self, crawler: Any) -> None:
        """Called once before discovery.  Override for login flows etc."""
        ...

    # ------------------------------------------------------------------
    # Core page-fetch
    # ------------------------------------------------------------------

    async def crawl_page(
        self,
        crawler: AsyncWebCrawler,
        url: str,
    ) -> PageResult | None:
        """Fetch *url*, convert to Markdown, return a :class:`PageResult`."""
        if url in self._visited:
            return None
        self._visited.add(url)

        try:
            result = await crawler.arun(url=url, config=self.make_run_config())
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

    def _build_page_result(self, url: str, result: Any) -> PageResult:
        """Convert a crawl4ai result into a :class:`PageResult`."""
        markdown = _extract_markdown(result)
        metadata: dict[str, Any] = getattr(result, "metadata", {}) or {}
        title = metadata.get("title", "") or metadata.get("og:title", "")
        internal = [lnk.get("href", "") for lnk in result.links.get("internal", [])]
        external = [lnk.get("href", "") for lnk in result.links.get("external", [])]
        return PageResult(
            url=url,
            title=str(title),
            markdown=markdown,
            links_internal=[h for h in internal if h],
            links_external=[h for h in external if h],
        )

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Discover URLs, crawl each one, write output files."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        rate = self.site_config.rate_limit
        sem = asyncio.Semaphore(rate.max_concurrent)

        async with AsyncWebCrawler(config=self.make_browser_config()) as crawler:
            await self.setup(crawler)
            urls = await self.discover_urls(crawler)
            logger.info("[%s] Discovered %d URLs", self.site_config.name, len(urls))

            async def _fetch(url: str) -> None:
                async with sem:
                    page = await self.crawl_page(crawler, url)
                    if page:
                        out = self.writer.write(page, self.site_config)
                        logger.debug("Wrote %s → %s", url, out)
                    await asyncio.sleep(rate.delay)

            await asyncio.gather(*[_fetch(u) for u in urls])

        manifest = self.writer.write_manifest(self.site_config)
        logger.info(
            "[%s] Done — %d pages, manifest: %s",
            self.site_config.name,
            len(self.writer._manifest),
            manifest,
        )
