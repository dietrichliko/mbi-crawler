"""Crawler for WikiJS sites protected by OEAW SSO.

Login strategy
--------------
crawl4ai runs a headless Chromium browser.  On first navigation the wiki
redirects to the OEAW identity provider.  The ``setup()`` hook injects
JavaScript that fills the login form and submits it.

If the auto-login fails (wrong selectors, MFA, captcha), open the wiki URL
manually in a non-headless run (set ``headless: false`` in settings.yaml),
complete the SSO flow once, and the browser session will be reused for all
subsequent ``arun`` calls within the same ``AsyncWebCrawler`` context.

Discovery strategy
------------------
BFS link-following from ``start_urls``, staying within the same domain.
Depth is bounded by ``filters.max_depth``.

Future improvement
------------------
WikiJS exposes a GraphQL API at ``/graphql``.  After login you can POST::

    { pages { list { id path title } } }

to enumerate all pages without link-following.  Enable this by setting
``extra.graphql_endpoint`` in the site config and implementing
``_discover_via_graphql``.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urljoin, urlparse

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig  # type: ignore[import]

from .base import BaseCrawler

logger = logging.getLogger(__name__)

# Milliseconds to wait after clicking submit before checking the URL.
_POST_SUBMIT_WAIT_MS = 3000


class WikiJSCrawler(BaseCrawler):
    """Browser-based crawler for WikiJS with SSO authentication."""

    # ------------------------------------------------------------------
    # Setup (login)
    # ------------------------------------------------------------------

    async def setup(self, crawler: AsyncWebCrawler) -> None:
        """Perform SSO login via JavaScript injection into the Chromium browser."""
        if self.site_config.auth is None or self.site_config.auth.type == "none":
            return

        auth = self.site_config.auth
        username = os.environ.get(auth.username_env, "")
        password = os.environ.get(auth.password_env, "")

        if not username or not password:
            logger.warning(
                "[%s] Env vars %s / %s not set — proceeding without login.",
                self.site_config.name,
                auth.username_env,
                auth.password_env,
            )
            return

        logger.info("[%s] Starting SSO login …", self.site_config.name)
        login_url = auth.login_url or self.site_config.base_url

        # Escape values for safe JS string interpolation.
        js_user = username.replace("'", "\\'")
        js_pass = password.replace("'", "\\'")
        user_sel = auth.username_selector.replace("'", "\\'")
        pass_sel = auth.password_selector.replace("'", "\\'")
        submit_sel = auth.submit_selector.replace("'", "\\'")

        js_login = f"""
            (async () => {{
                // Give the IdP login page time to render.
                await new Promise(r => setTimeout(r, 1500));

                const fill = (selector, value) => {{
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    el.focus();
                    el.value = value;
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }};

                fill('{user_sel}', '{js_user}');
                fill('{pass_sel}', '{js_pass}');

                await new Promise(r => setTimeout(r, 300));

                const btn = document.querySelector('{submit_sel}');
                if (btn) btn.click();

                await new Promise(r => setTimeout(r, {_POST_SUBMIT_WAIT_MS}));
            }})();
        """

        result = await crawler.arun(
            url=login_url,
            config=CrawlerRunConfig(js_code=js_login, word_count_threshold=0),
        )

        final_url: str = getattr(result, "url", login_url) or login_url
        pattern = auth.post_login_url_pattern or ""

        if result.success and (not pattern or pattern in final_url):
            logger.info("[%s] Login succeeded (final URL: %s)", self.site_config.name, final_url)
        else:
            logger.warning(
                "[%s] Login may have failed — final URL: %s. "
                "Check selectors in config/sites/wiki_mbi.yaml or set headless: false.",
                self.site_config.name,
                final_url,
            )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_urls(self, crawler: AsyncWebCrawler) -> list[str]:
        """BFS link-following from start_urls, bounded by max_depth."""
        start = list(self.site_config.start_urls) or [self.site_config.base_url]
        base_netloc = urlparse(self.site_config.base_url).netloc
        filters = self.site_config.filters

        discovered: set[str] = set()
        frontier: list[str] = []

        for u in start:
            clean = self._clean(u)
            if clean not in discovered:
                discovered.add(clean)
                frontier.append(clean)

        for depth in range(filters.max_depth):
            if not frontier:
                break
            next_frontier: list[str] = []

            for url in frontier:
                result = await crawler.arun(url=url, config=self.make_run_config())
                if not result.success:
                    continue

                for link in result.links.get("internal", []):
                    href = (link.get("href") or "").strip()
                    if not href:
                        continue
                    href = urljoin(url, href)
                    parsed = urlparse(href)

                    if filters.same_domain_only and parsed.netloc != base_netloc:
                        continue

                    clean = self._clean(href)
                    if not clean or clean in discovered:
                        continue
                    if self._is_excluded(clean):
                        continue

                    discovered.add(clean)
                    next_frontier.append(clean)

            logger.debug(
                "[%s] BFS depth %d → %d new URLs",
                self.site_config.name, depth + 1, len(next_frontier),
            )
            frontier = next_frontier

        return sorted(discovered)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clean(self, url: str) -> str:
        url = url.split("#")[0]
        if self.site_config.filters.strip_query_params:
            url = url.split("?")[0]
        return url.rstrip("/") or url

    def _is_excluded(self, url: str) -> bool:
        import fnmatch
        path = urlparse(url).path
        return any(fnmatch.fnmatch(path, p) for p in self.site_config.filters.exclude_patterns)
