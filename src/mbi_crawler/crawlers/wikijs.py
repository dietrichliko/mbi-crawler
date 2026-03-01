"""Crawler for WikiJS sites protected by OEAW SSO.

Login strategy
--------------
crawl4ai runs a headless Chromium browser.  On first navigation the wiki
redirects to the OEAW identity provider.  The ``setup()`` hook uses
Playwright's native ``page.fill()`` / ``page.click()`` / ``wait_for_url()``
to fill the SSO form — this correctly triggers React / Vue synthetic events
and waits for the full redirect chain to complete.

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

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

from .base import BaseCrawler

logger = logging.getLogger(__name__)

# Seconds the browser window stays open for a manual (headless: false) login.
_MANUAL_LOGIN_TIMEOUT_S = 90

# Persistent session so the login cookies are reused across all arun() calls.
_SESSION_ID = "wiki_session"

# JavaScript injected before crawl4ai captures the page HTML.
# It does three things:
#   1. Finds "Zuletzt bearbeitet von …" text (searching from the bottom of the
#      visible text) and stores it in a body data-attribute so we can retrieve
#      it after HTML capture without a second request.
#   2. Removes the Vuetify navigation sidebar (.v-navigation-drawer).
#   3. Removes the app-bar / header bar (.v-app-bar / header).
#   4. Removes any <button> whose visible text is exactly "Bearbeiten" or "Edit".
_DOM_CLEANUP_JS = r"""
(() => {
    try {
        // 1. Extract last-modified footer text (search from bottom up).
        const lines = (document.body.innerText || '').split('\n');
        let lastMod = '';
        for (let i = lines.length - 1; i >= 0; i--) {
            const t = lines[i].trim();
            if (t.includes('Zuletzt bearbeitet') || t.includes('Last edited')) {
                lastMod = t;
                break;
            }
        }
        document.body.setAttribute('data-crawl-last-modified', lastMod);

        // 2. Remove navigation sidebar (Vuetify).
        document.querySelectorAll('.v-navigation-drawer').forEach(el => el.remove());

        // 3. Remove the fixed app bar / top header (Vuetify).
        document.querySelectorAll('.v-app-bar, .v-toolbar').forEach(el => el.remove());

        // 4. Remove "Bearbeiten" / "Edit" action buttons.
        document.querySelectorAll('button').forEach(btn => {
            const t = btn.textContent.trim().toLowerCase();
            if (t === 'bearbeiten' || t === 'edit') btn.remove();
        });
    } catch (e) { /* ignore — never break the crawl */ }
})();
"""

# File extensions that cannot be rendered by Playwright — downloaded via httpx.
_BINARY_EXTENSIONS = frozenset({
    ".pdf", ".pptx", ".ppt", ".potx", ".pot",
    ".docx", ".doc", ".dotx", ".dot",
    ".xlsx", ".xls", ".xltx", ".xlt",
    ".odt", ".ods", ".odp", ".odg",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mp3", ".avi", ".mov", ".mkv", ".webm",
})


class WikiJSCrawler(BaseCrawler):
    """Browser-based crawler for WikiJS with SSO authentication."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Binary attachment URLs collected during BFS; downloaded separately.
        self._binary_urls: list[str] = []

    # ------------------------------------------------------------------
    # Setup (login)
    # ------------------------------------------------------------------

    async def setup(self, crawler: AsyncWebCrawler) -> None:
        """Perform SSO login via Playwright into the Chromium browser."""
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

        if not self.app_config.headless:
            # ── Manual login (headless: false) ─────────────────────────────
            # Keep the browser window open so the user can complete the SSO
            # flow themselves.  We do NOT inject JS — no risk of interfering
            # with the SSO redirect chain.  After _MANUAL_LOGIN_TIMEOUT_S the
            # page state (cookies, session) is captured as-is.
            logger.info(
                "[%s] headless: false — complete the SSO login in the browser window "
                "(%d s timeout).",
                self.site_config.name,
                _MANUAL_LOGIN_TIMEOUT_S,
            )
            result = await crawler.arun(
                url=login_url,
                config=CrawlerRunConfig(
                    delay_before_return_html=float(_MANUAL_LOGIN_TIMEOUT_S),
                    word_count_threshold=0,
                    session_id=_SESSION_ID,
                ),
            )
            final_title = (getattr(result, "metadata", {}) or {}).get("title", "")
            if result.success and "login" not in final_title.lower():
                logger.info("[%s] Login succeeded (page title: %s)", self.site_config.name, final_title)
            else:
                logger.warning(
                    "[%s] Login may have failed (title: %r).",
                    self.site_config.name,
                    final_title,
                )
        else:
            # ── Automated login (headless: true) via Playwright API ─────────
            # Step 1: Navigate to the wiki — crawl4ai creates the session and
            # follows the SSO redirect so the page lands on the IdP login form.
            await crawler.arun(
                url=login_url,
                config=CrawlerRunConfig(
                    word_count_threshold=0,
                    session_id=_SESSION_ID,
                    page_timeout=30000,
                    delay_before_return_html=2.0,
                ),
            )

            # Step 2: Obtain the raw Playwright page from the crawl4ai session.
            page = await self._get_playwright_page(crawler)
            if page is None:
                logger.warning(
                    "[%s] Could not get Playwright page — automated login unavailable.",
                    self.site_config.name,
                )
                return

            # Step 3: Fill the form using Playwright's native API.
            # Playwright's fill() triggers the synthetic events that React / Vue
            # SPAs expect, unlike direct DOM value assignment.
            try:
                success = await self._playwright_fill_login(page, auth, username, password)
            except Exception:
                logger.warning(
                    "[%s] Login automation raised an exception.",
                    self.site_config.name,
                    exc_info=True,
                )
                success = False

            if success:
                logger.info("[%s] Login succeeded (URL: %s)", self.site_config.name, page.url)
            else:
                logger.warning(
                    "[%s] Login may have failed (URL: %s). "
                    "Try headless: false in settings.yaml to debug the SSO flow.",
                    self.site_config.name,
                    page.url,
                )

    async def _playwright_fill_login(self, page: Any, auth: Any, username: str, password: str) -> bool:
        """Fill and submit the SSO login form using Playwright's native API.

        Handles WikiJS's two-stage flow: the wiki shows its own login page with
        an SSO provider button; clicking it redirects to the actual IdP form.
        Also handles both single-page and two-step (username→next→password) IdP flows.
        """
        base_netloc = urlparse(self.site_config.base_url).netloc

        # --- Click SSO provider button if we're still on the wiki login page ----
        # WikiJS shows its own /login page with provider buttons (rendered by the
        # Vue SPA after an async API call).  We wait for the network to settle,
        # then click the configured SSO button to trigger the IdP redirect.
        if base_netloc in page.url:
            sso_sel = auth.sso_button_selector
            # Wait for the SPA to finish rendering before interacting.
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # non-fatal

            if sso_sel:
                # SSO button configured — click it to trigger the IdP redirect.
                logger.debug(
                    "[%s] On wiki login page — clicking SSO button (%r)",
                    self.site_config.name, sso_sel,
                )
                try:
                    await page.wait_for_selector(sso_sel, timeout=8000, state="visible")
                    await page.click(sso_sel)
                    # Wait for redirect away from the wiki domain (to the IdP).
                    def _left_wiki(url: str) -> bool:
                        return base_netloc not in url

                    await page.wait_for_url(_left_wiki, timeout=15000)
                    logger.debug(
                        "[%s] Redirected to IdP: %s", self.site_config.name, page.url
                    )
                except Exception:
                    logger.warning(
                        "[%s] Could not click SSO button %r (URL: %s) — "
                        "update auth.sso_button_selector in the site YAML.",
                        self.site_config.name, sso_sel, page.url,
                    )
                    await self._log_page_buttons(page)
                    return False
            else:
                # No SSO button — login form is already on this page.
                logger.debug(
                    "[%s] On wiki login page, no sso_button_selector — filling form directly",
                    self.site_config.name,
                )

        # --- Fill username field (try each comma-separated selector) --------------
        # Fill username — try each comma-separated selector in turn.
        username_filled = False
        for sel in auth.username_selector.split(","):
            sel = sel.strip()
            if not sel:
                continue
            try:
                await page.wait_for_selector(sel, timeout=5000, state="visible")
                await page.fill(sel, username)
                username_filled = True
                logger.debug("[%s] Filled username with selector %r", self.site_config.name, sel)
                break
            except Exception:
                continue

        if not username_filled:
            logger.warning(
                "[%s] Could not find username field — selectors tried: %s",
                self.site_config.name,
                auth.username_selector,
            )
            await self._log_page_inputs(page)
            return False

        # Check whether the password field is already visible (one-page form)
        # or requires clicking submit first (two-step flow).
        password_visible = False
        try:
            await page.wait_for_selector(auth.password_selector, timeout=2000, state="visible")
            password_visible = True
        except Exception:
            pass

        if not password_visible:
            # Two-step SSO: advance to the password screen by clicking submit.
            logger.debug(
                "[%s] Password field not visible — submitting username first",
                self.site_config.name,
            )
            await page.click(auth.submit_selector)
            try:
                await page.wait_for_selector(auth.password_selector, timeout=10000, state="visible")
            except Exception:
                logger.warning(
                    "[%s] Password field did not appear after submitting username (URL: %s)",
                    self.site_config.name,
                    page.url,
                )
                return False

        # Fill password and submit.
        await page.fill(auth.password_selector, password)
        await page.click(auth.submit_selector)

        # Wait for the login to complete.  The correct condition depends on
        # where we are before clicking submit:
        #   • On the IdP (different domain) → wait to land back on the wiki.
        #   • On the wiki's own /login form → wait for URL to leave /login.
        # Using "base_netloc in url" in the second case would return immediately
        # (we're already on the wiki domain) and the /login check would fail.
        currently_on_wiki = base_netloc in page.url
        def _left_login(url: str) -> bool:
            return "login" not in url.lower()

        def _back_on_wiki(url: str) -> bool:
            return base_netloc in url

        try:
            if currently_on_wiki:
                await page.wait_for_url(_left_login, timeout=20000)
            else:
                await page.wait_for_url(_back_on_wiki, timeout=20000)
        except Exception:
            logger.debug(
                "[%s] wait_for_url timed out — checking current URL: %s",
                self.site_config.name,
                page.url,
            )

        return "login" not in page.url.lower()

    # ------------------------------------------------------------------
    # Config override — keep the same browser tab (session) across all calls
    # ------------------------------------------------------------------

    def make_run_config(self) -> CrawlerRunConfig:
        """Return a run config that reuses the authenticated browser session."""
        extra = self.site_config.extra or {}
        return CrawlerRunConfig(
            word_count_threshold=int(extra.get("word_count_threshold", 10)),
            css_selector=extra.get("css_selector"),
            excluded_selector=extra.get("excluded_selector"),
            session_id=_SESSION_ID,
            js_code=_DOM_CLEANUP_JS,
            # Give Vue a moment to finish rendering before we run the JS.
            delay_before_return_html=float(extra.get("content_delay", 1.0)),
        )

    def make_discovery_config(self) -> CrawlerRunConfig:
        """Discovery config — no CSS filtering, but still in the same session.

        A 3-second delay gives Vue time to render the navigation sidebar before
        crawl4ai extracts links from the page.
        """
        return CrawlerRunConfig(
            word_count_threshold=0,
            session_id=_SESSION_ID,
            delay_before_return_html=3.0,
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_urls(self, crawler: AsyncWebCrawler) -> list[str]:
        """Enumerate pages via GraphQL API, falling back to BFS link-following."""
        extra = self.site_config.extra or {}
        if extra.get("graphql_endpoint"):
            urls = await self._discover_via_graphql(crawler)
            if urls:
                logger.info("[%s] GraphQL discovered %d pages", self.site_config.name, len(urls))
                return sorted(urls)
            logger.warning(
                "[%s] GraphQL discovery returned nothing — falling back to BFS",
                self.site_config.name,
            )
        return await self._bfs_discover(crawler)

    async def _discover_via_graphql(self, crawler: Any) -> list[str]:
        """Fetch all page paths from the WikiJS GraphQL API using the browser session.

        The JS ``fetch()`` runs inside the already-authenticated Chromium page so
        it carries the session cookies automatically.  The JSON result is injected
        into a hidden ``<pre>`` element so crawl4ai can wait for it.
        """
        base_url = self.site_config.base_url.rstrip("/")

        # Create the result element first so wait_for can find it even on error.
        js_code = """
            (async () => {
                let el = document.getElementById('__gql_pages__');
                if (!el) {
                    el = document.createElement('pre');
                    el.id = '__gql_pages__';
                    el.style.display = 'none';
                    document.body.appendChild(el);
                }
                try {
                    const resp = await fetch('/graphql', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: '{"query":"{ pages { list { path } } }"}'
                    });
                    const data = await resp.json();
                    el.textContent = JSON.stringify(data);
                } catch (err) {
                    el.textContent = '{"error":true}';
                }
            })();
        """

        # Wait until the element has been populated (content length > 2 means not empty).
        wait_js = (
            "js:() => {"
            " const el = document.getElementById('__gql_pages__');"
            " return el && el.textContent.length > 2;"
            "}"
        )

        result = await crawler.arun(
            url=base_url,
            config=CrawlerRunConfig(
                session_id=_SESSION_ID,
                js_code=js_code,
                wait_for=wait_js,
                word_count_threshold=0,
                page_timeout=20000,
            ),
        )

        if not result.success:
            logger.warning("[%s] GraphQL arun failed", self.site_config.name)
            return []

        raw_html = getattr(result, "cleaned_html", "") or getattr(result, "html", "") or ""
        match = re.search(r'id="__gql_pages__"[^>]*>(.+?)</pre>', raw_html, re.DOTALL)
        if not match:
            logger.warning("[%s] GraphQL result element not found in HTML", self.site_config.name)
            return []

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.warning("[%s] Could not parse GraphQL JSON response", self.site_config.name)
            return []

        if data.get("error") or not data.get("data"):
            logger.warning(
                "[%s] GraphQL returned an error — API may be disabled",
                self.site_config.name,
            )
            return []

        pages = (data.get("data") or {}).get("pages", {}).get("list", [])
        urls: list[str] = []
        for page in pages:
            path = (page.get("path") or "").strip("/")
            if not path:
                continue
            url = f"{base_url}/{path}"
            if not self._is_excluded(url):
                urls.append(url)

        return urls

    async def _bfs_discover(self, crawler: Any) -> list[str]:
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
                result = await crawler.arun(url=url, config=self.make_discovery_config())
                if not result.success:
                    continue

                title = (getattr(result, "metadata", {}) or {}).get("title", "")
                if "login" in title.lower():
                    logger.warning(
                        "[%s] BFS hit login page — session not authenticated; stopping BFS",
                        self.site_config.name,
                    )
                    frontier = []
                    break

                internal_links = result.links.get("internal", [])
                logger.debug(
                    "[%s] BFS %s → %d internal links",
                    self.site_config.name, url, len(internal_links),
                )

                for link in internal_links:
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

                    # Binary files cannot be navigated to by Playwright;
                    # collect them for a separate httpx download pass.
                    if Path(urlparse(clean).path).suffix.lower() in _BINARY_EXTENSIONS:
                        if clean not in self._binary_urls:
                            self._binary_urls.append(clean)
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
    # Run loop (overrides base to add binary download pass)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Crawl wiki pages, then download binary attachments."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        rate = self.site_config.rate_limit
        sem = asyncio.Semaphore(rate.max_concurrent)

        async with AsyncWebCrawler(config=self.make_browser_config()) as crawler:
            await self.setup(crawler)
            urls = await self.discover_urls(crawler)
            logger.info("[%s] Discovered %d pages, %d binaries",
                        self.site_config.name, len(urls), len(self._binary_urls))

            async def _fetch(url: str) -> None:
                async with sem:
                    page = await self.crawl_page(crawler, url)
                    if page:
                        out = self.writer.write(page, self.site_config)
                        logger.debug("Wrote %s → %s", url, out)
                await asyncio.sleep(rate.delay)

            await asyncio.gather(*[_fetch(u) for u in urls])

            # Extract authenticated cookies before the browser closes.
            cookies = await self._get_session_cookies(crawler)

        # Download binary attachments outside the browser context.
        if self._binary_urls:
            if not cookies:
                logger.warning("[%s] No session cookies — binary downloads may fail", self.site_config.name)
            await self._download_all_binaries(cookies, sem, rate.delay)

        manifest = self.writer.write_manifest(self.site_config)
        logger.info("[%s] Done — %d pages, %d binaries, manifest: %s",
                    self.site_config.name, len(self.writer._manifest),
                    len(self._binary_urls), manifest)

    # ------------------------------------------------------------------
    # Binary download helpers
    # ------------------------------------------------------------------

    async def _log_page_buttons(self, page: Any) -> None:
        """Log all visible buttons and links so the user can find the right selector."""
        try:
            elements: list[dict[str, Any]] = await page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('a, button').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return;
                    out.push({
                        tag:     el.tagName,
                        text:    (el.innerText || '').trim().slice(0, 80),
                        href:    el.getAttribute('href') || null,
                        classes: el.className || null,
                        type:    el.getAttribute('type') || null,
                    });
                });
                return out;
            }""")
            logger.warning(
                "[%s] Visible buttons/links on %s — use one of these to set sso_button_selector:",
                self.site_config.name, page.url,
            )
            for el in elements:
                logger.warning(
                    "  <%s> text=%r href=%r class=%r type=%r",
                    el.get("tag"), el.get("text"), el.get("href"),
                    el.get("classes"), el.get("type"),
                )
        except Exception:
            logger.debug("Could not dump page elements", exc_info=True)

    async def _log_page_inputs(self, page: Any) -> None:
        """Log all input elements so the user can identify the right username selector."""
        try:
            inputs: list[dict[str, Any]] = await page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('input').forEach(el => {
                    const r = el.getBoundingClientRect();
                    out.push({
                        type:        el.getAttribute('type') || null,
                        name:        el.getAttribute('name') || null,
                        id:          el.getAttribute('id') || null,
                        placeholder: el.getAttribute('placeholder') || null,
                        classes:     el.className || null,
                        visible:     r.width > 0 && r.height > 0,
                    });
                });
                return out;
            }""")
            logger.warning(
                "[%s] Input fields on %s — update username_selector / password_selector in the site YAML:",
                self.site_config.name, page.url,
            )
            for inp in inputs:
                logger.warning(
                    "  <input> type=%r name=%r id=%r placeholder=%r visible=%r class=%r",
                    inp.get("type"), inp.get("name"), inp.get("id"),
                    inp.get("placeholder"), inp.get("visible"), inp.get("classes"),
                )
        except Exception:
            logger.debug("Could not dump input elements", exc_info=True)

    async def _get_playwright_page(self, crawler: Any) -> Any:
        """Return the raw Playwright Page for the named crawl4ai session, or None.

        crawl4ai ≥ 0.5 stores sessions in:
            crawler.crawler_strategy.browser_manager.sessions
        as a dict of ``session_id → (BrowserContext, Page, timestamp)``.
        """
        try:
            bm = getattr(crawler.crawler_strategy, "browser_manager", None)
            sessions: dict[str, Any] = getattr(bm, "sessions", {}) if bm else {}
            entry = sessions.get(_SESSION_ID)
            if entry is None:
                return None
            # entry is (BrowserContext, Page, float)
            if isinstance(entry, tuple) and len(entry) >= 2:
                return entry[1]
            # Fallback: older crawl4ai stored a session object with a .page attr.
            return getattr(entry, "page", None)
        except Exception:
            logger.debug("Could not retrieve Playwright page", exc_info=True)
            return None

    async def _get_session_cookies(self, crawler: Any) -> dict[str, str]:
        """Extract all cookies (including HttpOnly) from the live Playwright session."""
        try:
            page = await self._get_playwright_page(crawler)
            if page is None:
                return {}
            raw = await page.context.cookies()
            return {c["name"]: c["value"] for c in raw}
        except Exception:
            logger.debug("Could not extract browser cookies", exc_info=True)
            return {}

    async def _download_all_binaries(
        self, cookies: dict[str, str], sem: asyncio.Semaphore, delay: float
    ) -> None:
        logger.info("[%s] Downloading %d binary attachments …",
                    self.site_config.name, len(self._binary_urls))

        async def _dl(url: str) -> None:
            async with sem:
                dest = await self._download_binary(url, cookies)
                if dest:
                    self.writer.write_binary(url, dest, self.site_config)
                    logger.debug("Downloaded %s → %s", url, dest)
            await asyncio.sleep(delay)

        await asyncio.gather(*[_dl(u) for u in self._binary_urls])

    async def _download_binary(self, url: str, cookies: dict[str, str]) -> Path | None:
        """Download one binary file with httpx and save it under ``files/``."""
        rel = urlparse(url).path.lstrip("/")
        dest = self.output_dir / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(
                cookies=cookies, follow_redirects=True, timeout=60
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                dest.write_bytes(resp.content)
            return dest
        except Exception:
            logger.warning("Failed to download binary %s", url, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Page result — extract last-modified metadata
    # ------------------------------------------------------------------

    def _build_page_result(self, url: str, result: Any) -> Any:
        page = super()._build_page_result(url, result)
        last_modified = self._extract_last_modified(result)
        if last_modified:
            return page.model_copy(update={"last_modified": last_modified})
        return page

    def _extract_last_modified(self, result: Any) -> str | None:
        """Parse the data-crawl-last-modified attribute injected by _DOM_CLEANUP_JS."""
        import html as html_lib

        raw_html = getattr(result, "html", "") or ""
        match = re.search(r'data-crawl-last-modified="([^"]*)"', raw_html)
        if not match:
            return None
        text = html_lib.unescape(match.group(1)).strip()
        return text or None

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
