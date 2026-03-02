#!/usr/bin/env bash
# scripts/release.sh — bump version, generate changelog, commit, and tag.
#
# Usage:
#   ./scripts/release.sh 1.2.3
#
# What it does:
#   1. Validates the working tree is clean.
#   2. Updates `version` in pyproject.toml.
#   3. Regenerates CHANGELOG.md using git-cliff (full history).
#   4. Creates a single "chore(release)" commit containing both changes.
#   5. Creates an annotated git tag vX.Y.Z.
#
# Pushing that tag triggers the GitHub Actions release workflow which
# builds the wheel, publishes a GitHub Release, and pushes a Docker image.
#
# Requires:  git-cliff  (installed via `uv sync --all-groups`)

set -euo pipefail

# ── Arguments ─────────────────────────────────────────────────────────────────

VERSION="${1:?Usage: ./scripts/release.sh <version>  (e.g. 1.2.3, no 'v' prefix)}"
TAG="v${VERSION}"

# Rough semver check.
if ! [[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+].+)?$ ]]; then
    echo "Error: '${VERSION}' does not look like a semver version (e.g. 1.2.3)." >&2
    exit 1
fi

# ── Pre-flight checks ─────────────────────────────────────────────────────────

if ! uv run git-cliff --version &>/dev/null; then
    echo "Error: git-cliff not found in the uv environment." >&2
    echo "Run:  uv sync --all-groups" >&2
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Error: working tree is dirty — commit or stash all changes first." >&2
    git status --short
    exit 1
fi

if git tag --list | grep -qx "${TAG}"; then
    echo "Error: tag ${TAG} already exists." >&2
    exit 1
fi

echo "Preparing release ${TAG} …"
echo

# ── 1. Bump version in pyproject.toml ────────────────────────────────────────

python3 - <<PYEOF
import re, pathlib
p = pathlib.Path("pyproject.toml")
old = p.read_text()
if not re.search(r'^version = ".*"', old, flags=re.MULTILINE):
    raise SystemExit("Could not find 'version = ...' in pyproject.toml")
new = re.sub(r'^version = ".*"', f'version = "${VERSION}"', old, flags=re.MULTILINE)
if not new.endswith("\n"):
    new += "\n"
p.write_text(new)
PYEOF

echo "  ✓ pyproject.toml  →  version = \"${VERSION}\""

# ── 2. Regenerate full CHANGELOG.md ──────────────────────────────────────────

uv run git-cliff --tag "${TAG}" --output CHANGELOG.md
tail -c 20 CHANGELOG.md | xxd

echo "  ✓ CHANGELOG.md updated"
echo

# ── 3. Show the changelog entry for this release and ask for confirmation ─────

echo "Changelog entry for ${TAG}:"
echo "─────────────────────────────────────────────────────────────────────────"
uv run git-cliff --tag "${TAG}" --unreleased --strip all
echo "─────────────────────────────────────────────────────────────────────────"
echo

read -r -p "Does this look good? [y/N] " CONFIRM
if [[ "${CONFIRM}" != "y" && "${CONFIRM}" != "Y" ]]; then
    echo "Aborted. Reverting pyproject.toml and CHANGELOG.md."
    git checkout -- pyproject.toml CHANGELOG.md
    exit 1
fi
echo

# ── 4. Commit both files ──────────────────────────────────────────────────────

git add pyproject.toml CHANGELOG.md
git commit --message "$(cat <<EOF
chore(release): prepare ${TAG}

Co-Authored-By: Claude Sonnet <noreply@anthropic.com>
EOF
)"

echo "  ✓ release commit created"

# ── 5. Annotated tag ──────────────────────────────────────────────────────────

git tag -a "${TAG}" -m "Release ${TAG}"

echo "  ✓ tag ${TAG} created"

# ── 6. Push commit and tag ────────────────────────────────────────────────────

echo
git push origin main "${TAG}"

echo
echo "Release ${TAG} pushed. The GitHub Actions workflow will take it from here."
echo
