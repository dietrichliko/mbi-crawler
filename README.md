# mbi-crawler

A focused web crawler that converts institute websites into clean Markdown files for use in a [Retrieval-Augmented Generation (RAG)](https://en.wikipedia.org/wiki/Retrieval-augmented_generation) pipeline.

Built for the [Marietta Blau Institute (MBI)](https://www.oeaw.ac.at/mbi) at the Austrian Academy of Sciences, with support for three distinct site types out of the box.

## Supported sites

| Site | Type | Notes |
|------|------|-------|
| [MBI / OEAW](https://www.oeaw.ac.at/mbi) | `oeaw` | Public institutional site; sitemap-driven discovery |
| MBI WikiJS (`wiki.mbi.oeaw.ac.at`) | `wikijs` | Private network; OEAW SSO login required |
| [CERN CMS WorkBook](https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBook) | `twiki` | Public TWiki; BFS within the CMSPublic namespace |

## How it works

1. **Discovery** — each crawler type finds pages in the way that suits its site: sitemap parsing for OEAW, GraphQL-ready BFS for WikiJS, namespace-confined BFS for TWiki.
2. **Fetch & convert** — [crawl4ai](https://github.com/unclecode/crawl4ai) runs a headless Chromium browser, fetches each page, and converts the rendered HTML to clean Markdown.
3. **Write** — each page is saved as a `.md` file with YAML frontmatter that records the source URL, title, and crawl timestamp, making it straightforward to trace any RAG chunk back to its origin.

---

## Installation

### Docker (recommended)

Pre-built images are published to the GitHub Container Registry on every release.
No local Python setup is required.

```bash
docker pull ghcr.io/dietrichliko/mbi-crawler:latest
```

### From a GitHub Release

Download the wheel from the [Releases page](https://github.com/dietrichliko/mbi-crawler/releases) and install it with pip or uv:

```bash
# pip
pip install mbi_crawler-<version>-py3-none-any.whl

# uv tool (creates an isolated environment automatically)
uv tool install ./mbi_crawler-<version>-py3-none-any.whl

# After installation with either method:
mbi-crawler --help
```

You will also need Chromium:

```bash
playwright install chromium --with-deps
```

### From source (development)

```bash
git clone https://github.com/dietrichliko/mbi-crawler.git
cd mbi-crawler

uv sync                   # create .venv and install all dependencies
uv run crawl4ai-setup     # install Playwright Chromium
cp .env.example .env      # create local secrets file (gitignored)
```

---

## Usage

### Docker

Mount your local `output/` directory to persist results, and optionally mount a custom `config/` directory to override site settings:

```bash
# Crawl the public MBI site
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  ghcr.io/dietrichliko/mbi-crawler:latest crawl --site mbi_oeaw

# Crawl CERN CMS WorkBook
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  ghcr.io/dietrichliko/mbi-crawler:latest crawl --site cern_cms

# Crawl all enabled sites
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  ghcr.io/dietrichliko/mbi-crawler:latest crawl

# Pass credentials for the private WikiJS site via Docker's --env-file
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  --env-file .env \
  ghcr.io/dietrichliko/mbi-crawler:latest crawl --site wiki_mbi

# Override config (e.g. to enable wiki_mbi or tune rate limits)
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/config:/app/config" \
  --env-file .env \
  ghcr.io/dietrichliko/mbi-crawler:latest crawl

# List configured sites
docker run --rm ghcr.io/dietrichliko/mbi-crawler:latest list-sites
```

### CLI (installed directly)

```bash
# Check what sites are configured
mbi-crawler list-sites

# Crawl the public MBI site
mbi-crawler crawl --site mbi_oeaw

# Crawl CERN CMS WorkBook
mbi-crawler crawl --site cern_cms

# Crawl multiple sites in one run
mbi-crawler crawl -s mbi_oeaw -s cern_cms

# Verbose output for debugging
mbi-crawler crawl --site mbi_oeaw -v

# Use a custom secrets file (default: .env)
mbi-crawler crawl --env-file /path/to/secrets.env
```

---

## WikiJS (private network)

The `wiki_mbi` site is disabled by default because it requires:

- Access to the MBI private network or VPN
- OEAW SSO credentials

**Step 1 — create your `.env` file:**

```bash
cp .env.example .env
# edit .env and fill in your credentials
```

```ini
# .env
WIKI_USERNAME=your@email.oeaw.ac.at
WIKI_PASSWORD=yourpassword
```

The `.env` file is listed in `.gitignore` and will never be committed.
The YAML config only stores the *names* of these variables (`WIKI_USERNAME`, `WIKI_PASSWORD`), not the values.

**Step 2 — enable the site:**

Set `enabled: true` in `config/sites/wiki_mbi.yaml`.

**Step 3 — run:**

```bash
# CLI
mbi-crawler crawl --site wiki_mbi

# Docker (credentials via --env-file)
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/config:/app/config" \
  --env-file .env \
  ghcr.io/dietrichliko/mbi-crawler:latest crawl --site wiki_mbi
```

The crawler automates the SSO login in a headless browser. If the login fails (changed form selectors, MFA prompt, etc.), set `headless: false` in `config/settings.yaml` to watch and complete the flow manually.

---

## Output

Each site writes into its own subdirectory under `output/`:

```
output/
└── mbi_oeaw/
    ├── pages/
    │   ├── index.md
    │   └── research/
    │       └── index.md
    └── crawl_manifest.json
```

Every Markdown file includes a frontmatter header:

```yaml
---
url: https://www.oeaw.ac.at/mbi/research/
title: Research - Marietta Blau Institute
site: mbi_oeaw
crawled_at: 2026-02-28T10:30:00+00:00
path: pages/research/index.md
---
```

`crawl_manifest.json` is a machine-readable index of all crawled pages — useful as the starting point for a RAG ingestion pipeline.

---

## Configuration

Crawler behaviour is controlled by YAML files, with no code changes needed for most tuning:

```
config/
├── settings.yaml          # global: output directory, headless mode, …
└── sites/
    ├── mbi_oeaw.yaml      # include/exclude patterns, rate limits, …
    ├── wiki_mbi.yaml      # SSO selectors, env var names, BFS depth, …
    └── cern_cms.yaml      # TWiki namespace, depth, rate limits, …
```

Key per-site options:

| Option | Description |
|--------|-------------|
| `enabled` | Set to `false` to skip a site |
| `filters.include_patterns` | fnmatch patterns on the full URL to keep |
| `filters.exclude_patterns` | fnmatch patterns on the URL path to drop |
| `filters.max_depth` | BFS depth limit for link-following crawlers |
| `rate_limit.delay` | Seconds between requests per concurrent slot |
| `rate_limit.max_concurrent` | Number of parallel crawl slots |
| `auth.username_env` | Name of the `.env` key holding the username |
| `auth.password_env` | Name of the `.env` key holding the password |

---

## Development

### Setup

```bash
git clone https://github.com/dietrichliko/mbi-crawler.git
cd mbi-crawler

uv sync --all-groups        # install deps + dev tools (ruff, mypy, pytest, git-cliff, pre-commit)
uv run crawl4ai-setup       # install Playwright Chromium
cp .env.example .env        # create local secrets file

uv run pre-commit install                            # install pre-push / pre-commit hooks
uv run pre-commit install --hook-type commit-msg    # install commit-msg hook
```

### Daily commands

```bash
uv run ruff check src/ tests/     # lint
uv run ruff format src/ tests/    # format
uv run mypy src/                  # type check
uv run pytest                     # run tests
```

### Commit conventions

This project follows [Conventional Commits](https://www.conventionalcommits.org/).
The `commit-msg` pre-commit hook enforces the format automatically:

```
feat: add GraphQL discovery for WikiJS
fix(twiki): correctly strip TWiki action suffixes
docs: expand WikiJS SSO setup guide
chore: bump crawl4ai to 0.5
```

### Creating a release

```bash
# 1. Bump version, regenerate CHANGELOG.md, commit, and tag locally:
./scripts/release.sh 1.2.3

# 2. Review the release commit and changelog, then push:
git push origin main v1.2.3
```

The [release workflow](.github/workflows/release.yaml) then automatically:

1. Builds the Python wheel and sdist.
2. Generates per-release notes with [git-cliff](https://git-cliff.org).
3. Creates a [GitHub Release](https://github.com/dietrichliko/mbi-crawler/releases) with the wheel attached.
4. Builds and pushes a Docker image to `ghcr.io/dietrichliko/mbi-crawler`.

### Adding a new site

1. Create `config/sites/my_site.yaml` with `crawler_type: oeaw`, `wikijs`, or `twiki`.
2. Adjust `filters` and `rate_limit` for the target site.
3. If none of the three crawler types fit, subclass `BaseCrawler` in `src/mbi_crawler/crawlers/my_site.py` and register it in `src/mbi_crawler/cli.py`.

---

## Authors

- Dietrich Liko &lt;Dietrich.Liko@oeaw.ac.at&gt;
- Claude Sonnet (Anthropic)

## License

[MIT](LICENSE)
