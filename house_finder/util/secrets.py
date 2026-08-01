"""Helpers for deciding whether an optional API key is really configured."""

from __future__ import annotations

import os

# Values that mean "not set" even though the variable technically exists -
# CI and .env.example both produce these.
_PLACEHOLDERS = frozenset(
    {
        "",
        "none",
        "null",
        "todo",
        "changeme",
        "replace_me",
        "your_key_here",
        "xxx",
        "placeholder",
    }
)


def looks_configured_secret(value: str | None) -> bool:
    """True if `value` looks like a real secret rather than a placeholder."""
    if value is None:
        return False
    cleaned = value.strip().strip("\"'")
    if cleaned.lower() in _PLACEHOLDERS:
        return False
    # "${SMTP_HOST}" survives when the variable was never exported.
    if cleaned.startswith("${") and cleaned.endswith("}"):
        return False
    return len(cleaned) >= 8


def has_secrets(*names: str) -> bool:
    """True if every named environment variable holds a real-looking secret."""
    return all(looks_configured_secret(os.environ.get(n)) for n in names)


def resolve_enabled(flag, *secret_names: str) -> bool:
    """Resolve a config `enabled` value that may be true/false or "auto".

    "auto" means: on if and only if every named secret is configured.
    """
    if isinstance(flag, str) and flag.strip().lower() == "auto":
        return has_secrets(*secret_names)
    return bool(flag)
