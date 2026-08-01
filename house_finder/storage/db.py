"""SQLite connection handling and the migration runner."""

from __future__ import annotations

import importlib
import logging
import pkgutil
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from house_finder import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "houses.db"


def db_path(settings: dict | None = None) -> Path:
    """Resolve the configured database path."""
    if settings:
        configured = (settings.get("paths") or {}).get("db")
        if configured:
            path = Path(configured)
            return path if path.is_absolute() else PROJECT_ROOT / path
    return DEFAULT_DB_PATH


def connect(settings: dict | None = None, path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database with sane pragmas and Row access."""
    target = Path(path) if path else db_path(settings)
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT,
            applied_at TEXT NOT NULL
        )
        """
    )
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}


def _discover_migrations() -> list:
    """Import every migration module, ordered by VERSION."""
    from house_finder.storage import migrations as migrations_pkg

    found = []
    for mod_info in pkgutil.iter_modules(migrations_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{migrations_pkg.__name__}.{mod_info.name}")
        if hasattr(module, "up") and hasattr(module, "VERSION"):
            found.append(module)
    return sorted(found, key=lambda m: m.VERSION)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply any pending migrations. Returns the versions applied."""
    applied = _applied_versions(conn)
    newly_applied = []
    for module in _discover_migrations():
        if module.VERSION in applied:
            continue
        logger.info("db: applying migration %d (%s)", module.VERSION, module.NAME)
        module.up(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (module.VERSION, module.NAME, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        newly_applied.append(module.VERSION)
    return newly_applied


def open_and_migrate(
    settings: dict | None = None, path: Path | str | None = None
) -> sqlite3.Connection:
    """Open the database and bring the schema up to date."""
    conn = connect(settings, path)
    migrate(conn)
    return conn


def start_run(conn: sqlite3.Connection) -> int:
    """Record the start of a pipeline run and return its id."""
    cur = conn.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
        (datetime.now(UTC).isoformat(),),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str = "ok", **counts) -> None:
    """Close out a run row with its result counts."""
    fields = {
        "finished_at": datetime.now(UTC).isoformat(),
        "status": status,
        **{
            k: v
            for k, v in counts.items()
            if k
            in {
                "listings_fetched",
                "listings_kept",
                "listings_new",
                "listings_ranked",
                "cost_gbp",
                "error",
            }
        },
    }
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE runs SET {assignments} WHERE run_id = ?",  # noqa: S608 - keys are whitelisted
        (*fields.values(), run_id),
    )
    conn.commit()


def backup(settings: dict, conn: sqlite3.Connection | None = None) -> Path | None:
    """Copy the database into the backups directory, then prune old copies."""
    source = db_path(settings)
    if not source.exists():
        return None

    backups_dir = (settings.get("paths") or {}).get("backups", "data/backups")
    target_dir = Path(backups_dir)
    if not target_dir.is_absolute():
        target_dir = PROJECT_ROOT / target_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    stamp = date.today().isoformat()
    target = target_dir / f"houses-{stamp}.db"

    if conn is not None:
        # Checkpoint the WAL so the copied file is complete.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            logger.debug("db: wal checkpoint before backup failed: %s", exc)
    shutil.copy2(source, target)

    keep_days = int((settings.get("backups") or {}).get("keep_days", 7))
    cutoff = date.today() - timedelta(days=keep_days)
    for old in target_dir.glob("houses-*.db"):
        try:
            stamp_part = old.stem.split("houses-", 1)[1]
            if date.fromisoformat(stamp_part) < cutoff:
                old.unlink()
        except (ValueError, IndexError, OSError):
            continue

    return target
