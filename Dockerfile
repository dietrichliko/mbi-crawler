# ── Stage 1: build the wheel ─────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# uv is used only for the build step.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ src/

RUN uv build --wheel

# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.11-slim

LABEL org.opencontainers.image.title="mbi-crawler" \
      org.opencontainers.image.description="RAG crawler for Marietta Blau Institute (MBI/OEAW), WikiJS, and CERN TWiki" \
      org.opencontainers.image.source="https://github.com/dietrichliko/mbi-crawler" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install the built wheel.
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Install Playwright Chromium + all required system libraries.
# --with-deps handles apt packages (fonts, nss, etc.) automatically.
RUN playwright install chromium --with-deps

# Bake in the default configuration.
# Users can override individual site configs by mounting a volume at /app/config.
COPY config/ /app/config/

# Crawl output — always mount a host directory here to persist results.
VOLUME ["/app/output"]

# Run as a non-root user for safety.
RUN useradd --system --no-create-home crawler
USER crawler

ENTRYPOINT ["mbi-crawler"]
CMD ["--help"]
