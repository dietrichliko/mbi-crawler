# mbi-crawler

A Python crawler that pulls pages from three sites into clean Markdown files
for use in a RAG (Retrieval-Augmented Generation) system:

| Site key    | Type    | URL                                                          |
|-------------|---------|--------------------------------------------------------------|
| `mbi_oeaw`  | oeaw    | https://www.oeaw.ac.at/mbi (public, sitemap-driven)         |
| `wiki_mbi`  | wikijs  | https://wiki.mbi.oeaw.ac.at (private, OEAW SSO login)       |
| `cern_cms`  | twiki   | https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBook      |

MBI = **Marietta Blau Institute** (part of the Austrian Academy of Sciences / OEAW).

## Stack

- Python 3.11+, `uv` for env / dependency management
- `crawl4ai ≥ 0.4` — headless Chromium fetch + HTML→Markdown
- `pydantic v2` — typed config validation
- `python-dotenv` — loads secrets from `.env` at startup
- `typer` + `rich` — CLI
- `ruff` + `mypy` — lint / type checking
- `pytest` + `pytest-asyncio` — tests

## First-time setup

```bash
uv sync                          # create .venv and install deps
uv run crawl4ai-setup            # install Playwright browsers (Chromium)
# or: uv run playwright install chromium
cp .env.example .env             # create local secrets file (gitignored)
```

## Running the crawler

```bash
# Crawl all enabled sites
uv run mbi-crawler crawl

# Crawl a specific site by key
uv run mbi-crawler crawl --site mbi_oeaw
uv run mbi-crawler crawl --site cern_cms

# Multiple sites in one run
uv run mbi-crawler crawl -s mbi_oeaw -s cern_cms

# Custom .env location
uv run mbi-crawler crawl --env-file /path/to/secrets.env

# Verbose / debug output
uv run mbi-crawler crawl -v

# List all configured sites with their status
uv run mbi-crawler list-sites
```

## Secrets / credentials

Secrets are loaded from `.env` (default) or a file passed via `--env-file`.
The YAML config stores only the **variable names**, never the values.

```
.env.example   ← template, committed to git
.env           ← actual secrets, gitignored
```

Example `.env`:
```ini
WIKI_USERNAME=your@email.oeaw.ac.at
WIKI_PASSWORD=yourpassword
```

The `auth.username_env` / `auth.password_env` fields in `wiki_mbi.yaml`
tell the crawler which keys to read from the environment after `.env` is loaded.

## Configuration

Config lives in two places that are **merged at startup**:

```
config/
├── settings.yaml          ← global settings (output dir, headless, …)
└── sites/
    ├── mbi_oeaw.yaml      ← one file per site
    ├── wiki_mbi.yaml
    └── cern_cms.yaml
```

All fields are validated by the Pydantic models in
`src/mbi_crawler/config/models.py`.  The most important per-site knobs:

| Field                         | Purpose                                       |
|-------------------------------|-----------------------------------------------|
| `enabled`                     | Skip site when `false`                        |
| `crawler_type`                | `oeaw` / `wikijs` / `twiki`                  |
| `base_url`                    | Crawl root and URL→path anchor                |
| `sitemap_url`                 | Sitemap XML (oeaw type only)                  |
| `filters.include_patterns`    | fnmatch on full URL — keep only matches       |
| `filters.exclude_patterns`    | fnmatch on URL path — drop matches            |
| `filters.max_depth`           | BFS depth limit (wikijs / twiki)             |
| `filters.strip_query_params`  | Drop `?…` before storing URLs                |
| `rate_limit.delay`            | Seconds between requests per concurrent slot  |
| `rate_limit.max_concurrent`   | Parallel crawl slots                          |
| `auth.username_env`           | `.env` key name for the SSO username         |
| `auth.password_env`           | `.env` key name for the SSO password         |

## WikiJS SSO credentials

The `wiki_mbi` site is **disabled by default**.  To enable it:

1. Ensure you are on the MBI private network (or VPN).
2. Fill in `.env`:
   ```ini
   WIKI_USERNAME=your@email.oeaw.ac.at
   WIKI_PASSWORD=yourpassword
   ```
3. Set `enabled: true` in `config/sites/wiki_mbi.yaml`.
4. Run: `uv run mbi-crawler crawl --site wiki_mbi`

The crawler uses JavaScript injection to fill the SSO login form in a
headless browser.  If the IdP redesigns its form, update the `auth.*`
selectors in `wiki_mbi.yaml`.  For troubleshooting, set `headless: false`
in `config/settings.yaml` so you can watch (and manually complete) the login.

## Output format

```
output/
├── mbi_oeaw/
│   ├── pages/
│   │   ├── index.md
│   │   └── research/
│   │       └── index.md
│   └── crawl_manifest.json
├── wiki_mbi/
│   └── …
└── cern_cms/
    └── …
```

Every `.md` file starts with YAML frontmatter that links back to the source:

```yaml
---
url: https://www.oeaw.ac.at/mbi/research/
title: Research - Marietta Blau Institute
site: mbi_oeaw
crawled_at: 2026-02-28T10:30:00+00:00
path: pages/research/index.md
---
```

`crawl_manifest.json` contains a JSON array of every crawled page with its
URL and local path — useful for building a RAG index.

## Project structure

```
src/mbi_crawler/
├── cli.py                   Typer CLI (crawl / list-sites commands)
├── config/
│   ├── models.py            Pydantic AppConfig / SiteConfig / …
│   └── loader.py            YAML loading + deep-merge
├── crawlers/
│   ├── base.py              BaseCrawler (run loop, page fetch, output)
│   ├── oeaw.py              Sitemap-driven OEAW crawler
│   ├── wikijs.py            WikiJS crawler with SSO browser automation
│   └── twiki.py             TWiki BFS crawler (namespace-confined)
├── output/
│   ├── models.py            PageResult dataclass
│   └── writer.py            Markdown + manifest writer, URL→path logic
└── utils/
    └── __init__.py          (extend as needed)
tests/
└── test_output_writer.py    URL-to-path unit tests
config/
├── settings.yaml
└── sites/{mbi_oeaw,wiki_mbi,cern_cms}.yaml
.env.example                 Secret variable names and placeholders
```

## Dev commands

```bash
uv run ruff check src/       # lint
uv run ruff format src/      # auto-format
uv run mypy src/             # type check
uv run pytest                # run tests
```

## Per-site notes

### mbi_oeaw
- The OEAW sitemap is an **index** pointing to sub-sitemaps (pages, posts, …).
  The `OEAWCrawler` follows them recursively and filters by `include_patterns`.
- WordPress feeds (`/feed/`), JSON API (`/wp-json/`), and admin paths are
  excluded by default.  Add more patterns in `config/sites/mbi_oeaw.yaml`.
- If the sitemap is unavailable, set `sitemap_url: null` and list explicit
  `start_urls` instead.

### wiki_mbi
- Requires private network access.  Disabled by default.
- Discovery is BFS link-following.  WikiJS also exposes a **GraphQL API**
  at `/graphql` that can enumerate all pages in one query — see the
  `extra.graphql_endpoint` note in `wiki_mbi.yaml` for a future improvement.
- If OEAW adds MFA or changes the IdP, the `setup()` method in
  `src/mbi_crawler/crawlers/wikijs.py` will need updating.

### cern_cms
- BFS is **namespace-confined**: only URLs under
  `/twiki/bin/view/CMSPublic/` are followed.
- TWiki action pages (edit, attach, diff, search, …) are filtered at the
  `_is_view_url` level in the crawler, not via YAML patterns.
- Rate limit is conservative (2 s delay, 1 concurrent) — CERN servers are
  shared infrastructure.

## Adding a new site

1. Create `config/sites/my_site.yaml` with `crawler_type: oeaw | wikijs | twiki`.
2. Tune `filters`, `rate_limit`, and (if needed) `auth`.
3. If the existing crawler types don't fit, add a new subclass of `BaseCrawler`
   in `src/mbi_crawler/crawlers/my_site.py` and register it in
   `src/mbi_crawler/cli.py` → `_CRAWLER_MAP`.
