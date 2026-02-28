"""Typer CLI entry point for mbi-crawler."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.logging import RichHandler

from .config.loader import load_config
from .crawlers.oeaw import OEAWCrawler
from .crawlers.twiki import TWikiCrawler
from .crawlers.wikijs import WikiJSCrawler

app = typer.Typer(
    name="mbi-crawler",
    help="Crawl Marietta Blau Institute (MBI/OEAW), WikiJS, and CERN TWiki sites into Markdown for RAG.",
    add_completion=False,
)

_CRAWLER_MAP = {
    "oeaw": OEAWCrawler,
    "wikijs": WikiJSCrawler,
    "twiki": TWikiCrawler,
}

_DEFAULT_SETTINGS = Path("config/settings.yaml")
_DEFAULT_SITES_DIR = Path("config/sites")
_DEFAULT_ENV_FILE = Path(".env")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, markup=True)],
    )


@app.command()
def crawl(
    config: Path = typer.Option(_DEFAULT_SETTINGS, help="Path to settings YAML."),
    sites_dir: Optional[Path] = typer.Option(_DEFAULT_SITES_DIR, help="Per-site YAML directory."),
    env_file: Path = typer.Option(_DEFAULT_ENV_FILE, help="Path to .env file with secrets."),
    site: Optional[list[str]] = typer.Option(
        None, "--site", "-s", help="Site key(s) to crawl.  Repeatable.  Default: all enabled."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Crawl one or more configured sites and write Markdown output."""
    _setup_logging(verbose)

    if env_file.exists():
        load_dotenv(env_file)
        logging.debug("Loaded secrets from %s", env_file)
    else:
        logging.debug("No .env file found at %s — relying on existing environment", env_file)

    app_config = load_config(config, sites_dir)

    targets = {
        name: sc
        for name, sc in app_config.sites.items()
        if sc.enabled and (not site or name in site)
    }

    if not targets:
        typer.echo("No enabled sites matched.  Use `list-sites` to see available sites.", err=True)
        raise typer.Exit(1)

    async def run_all() -> None:
        for name, site_cfg in targets.items():
            cls = _CRAWLER_MAP.get(site_cfg.crawler_type)
            if cls is None:
                logging.warning("Unknown crawler_type '%s' for site '%s'", site_cfg.crawler_type, name)
                continue
            logging.info("Starting crawl: [bold]%s[/bold]", name)
            crawler = cls(site_cfg, app_config)
            await crawler.run()

    asyncio.run(run_all())


@app.command(name="list-sites")
def list_sites(
    config: Path = typer.Option(_DEFAULT_SETTINGS),
    sites_dir: Optional[Path] = typer.Option(_DEFAULT_SITES_DIR),
) -> None:
    """List all configured sites and their status."""
    _setup_logging(verbose=False)
    app_config = load_config(config, sites_dir)

    if not app_config.sites:
        typer.echo("No sites configured.")
        return

    typer.echo(f"{'KEY':<20} {'TYPE':<10} {'STATUS':<10} URL")
    typer.echo("-" * 70)
    for name, sc in app_config.sites.items():
        status = "enabled" if sc.enabled else "disabled"
        typer.echo(f"{name:<20} {sc.crawler_type:<10} {status:<10} {sc.base_url}")
