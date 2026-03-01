"""Load and merge YAML configuration files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig

logger = logging.getLogger(__name__)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(
    settings_path: Path,
    sites_dir: Path | None = None,
) -> AppConfig:
    """Load global settings and merge per-site YAML files.

    Args:
        settings_path: Path to ``config/settings.yaml``.
        sites_dir:     Directory containing per-site ``*.yaml`` files.
                       Each file's stem becomes the site key.
    """
    data: dict[str, Any] = {}

    if settings_path.exists():
        with settings_path.open() as f:
            data = yaml.safe_load(f) or {}
    else:
        logger.warning("Settings file not found: %s — using defaults", settings_path)

    sites_data: dict[str, Any] = dict(data.get("sites", {}))

    if sites_dir and sites_dir.exists():
        for site_file in sorted(sites_dir.glob("*.yaml")):
            with site_file.open() as f:
                raw = yaml.safe_load(f) or {}
            site_key = raw.get("name", site_file.stem)
            existing = sites_data.get(site_key, {})
            sites_data[site_key] = _deep_merge(existing, raw)
            # Ensure 'extra' is a dict, not None
            if "extra" not in sites_data[site_key] or sites_data[site_key]["extra"] is None:
                sites_data[site_key]["extra"] = {}
            logger.debug("Loaded site config: %s from %s", site_key, site_file.name)

    data["sites"] = sites_data
    return AppConfig.model_validate(data)
