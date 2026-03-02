"""Pydantic models for YAML configuration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RateLimitConfig(BaseModel):
    """Controls request frequency."""

    delay: float = 1.0
    """Seconds to wait between requests (per concurrent slot)."""
    max_concurrent: int = 3
    """Maximum number of concurrent crawl tasks."""


class AuthConfig(BaseModel):
    """Authentication settings for sites that require login."""

    type: Literal["none", "sso", "basic"] = "none"
    login_url: str | None = None
    """URL to navigate to for triggering the login flow."""
    username_env: str = "CRAWLER_USERNAME"
    """Environment variable name holding the username/email."""
    password_env: str = "CRAWLER_PASSWORD"
    """Environment variable name holding the password."""
    # Optional selector for an SSO provider button on the app's own login page
    # (e.g. WikiJS shows a "Sign in with OEAW" button before redirecting to IdP).
    # If set, this element is clicked first and we wait for the IdP page to load.
    sso_button_selector: str | None = None
    # CSS selectors for the login form — adjust per IdP.
    username_selector: str = "input[type='email'], input[name='username']"
    password_selector: str = "input[type='password']"
    submit_selector: str = "button[type='submit']"
    post_login_url_pattern: str | None = None
    """Substring that must appear in the URL after a successful login."""


class FilterConfig(BaseModel):
    """URL filtering rules applied during crawl."""

    include_patterns: list[str] = Field(default_factory=list)
    """fnmatch patterns matched against the full URL.  Empty = include all."""
    exclude_patterns: list[str] = Field(default_factory=list)
    """fnmatch patterns matched against the URL path.  Matched URLs are skipped."""
    max_depth: int = 10
    """Maximum BFS depth for link-following crawlers (oeaw uses sitemap, ignores this)."""
    same_domain_only: bool = True
    """Discard links that leave the site's domain."""
    strip_query_params: bool = True
    """Remove ?query=strings before storing/matching URLs."""


class SiteConfig(BaseModel):
    """Full configuration for a single site."""

    name: str
    enabled: bool = True
    crawler_type: Literal["oeaw", "wikijs", "twiki"]
    base_url: str
    """Canonical base URL.  Used as crawl root and for URL→path conversion."""
    start_urls: list[str] = Field(default_factory=list)
    """Override crawl entry points.  Defaults to [base_url] when empty."""
    sitemap_url: str | None = None
    """Sitemap XML URL (used by the oeaw crawler type)."""
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    auth: AuthConfig | None = None
    filters: FilterConfig = Field(default_factory=FilterConfig)
    output_subdir: str = ""
    """Subdirectory under AppConfig.output_base_dir for this site's output."""
    extra: dict[str, Any] = Field(default_factory=dict)
    """Crawler-specific settings that don't fit the common schema."""

    def model_post_init(self, __context: Any) -> None:  # noqa: ANN401
        if not self.output_subdir:
            self.output_subdir = self.name


class AppConfig(BaseModel):
    """Top-level application configuration."""

    output_base_dir: str = "./output"
    respect_robots_txt: bool = True
    headless: bool = True
    sites: dict[str, SiteConfig] = Field(default_factory=dict)
