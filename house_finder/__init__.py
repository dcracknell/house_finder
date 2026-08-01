"""UK house-finder pipeline.

Fetches property listings from configured sources, normalises them into one
shared schema, filters and ranks them against a buyer/renter profile, stores
them in SQLite, and regenerates an Excel workbook and interactive map dashboard.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

__version__ = "0.1.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
PROFILE_PATH = CONFIG_DIR / "profile.json"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"
RANKER_PATH = CONFIG_DIR / "ranker.yaml"

# "${SMTP_HOST}" style placeholders in settings.yaml are resolved from the
# environment so secrets never live in the config file itself.
_ENV_PLACEHOLDER = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def _expand_env(value: Any) -> Any:
    """Recursively replace ${VAR} placeholders with their environment values."""
    if isinstance(value, str):
        match = _ENV_PLACEHOLDER.match(value.strip())
        if match:
            return os.environ.get(match.group(1), "")
        return value
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_settings(path: Path | None = None) -> dict:
    """Load config/settings.yaml with ${ENV_VAR} placeholders resolved."""
    path = path or SETTINGS_PATH
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return _expand_env(raw)


def load_profile(path: Path | None = None) -> dict:
    """Load config/profile.json (the buyer/renter search criteria)."""
    path = path or PROFILE_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_sources(path: Path | None = None) -> dict:
    """Load config/sources.yaml (which portals and enrichment APIs are enabled)."""
    path = path or SOURCES_PATH
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return _expand_env(raw)


def load_ranker_config(path: Path | None = None) -> dict:
    """Load config/ranker.yaml (scoring rubric and prompt templates)."""
    path = path or RANKER_PATH
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve_path(settings: dict, key: str, default: str) -> Path:
    """Resolve a settings['paths'][key] entry to an absolute path."""
    configured = (settings.get("paths") or {}).get(key, default)
    path = Path(configured)
    return path if path.is_absolute() else PROJECT_ROOT / path
