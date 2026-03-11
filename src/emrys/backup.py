"""Backup and restore for emrys persist data.

Copies persist.db (and optionally journals) to a backup directory.
Keeps timestamped snapshots so you can roll back if needed.
"""

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from emrys.db import get_db_path, get_journal_dir, get_persist_dir

# Config lives in .persist/config.json — survives DB corruption
_CONFIG_FILENAME = "config.json"


def get_config() -> dict:
    """Load emrys config from .persist/config.json."""
    config_path = get_persist_dir() / _CONFIG_FILENAME
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_config(config: dict):
    """Save emrys config to .persist/config.json."""
    config_path = get_persist_dir() / _CONFIG_FILENAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def get_backup_dir() -> Path | None:
    """Get configured backup directory, or None if not set."""
    config = get_config()
    backup_dir = config.get("backup_dir", "")
    if backup_dir:
        return Path(backup_dir)
    return None


def set_backup_dir(backup_dir: str):
    """Set the backup directory in config."""
    config = get_config()
    config["backup_dir"] = str(Path(backup_dir).resolve())
    save_config(config)


def create_backup(backup_dir: str = "", include_journals: bool = False,
                  label: str = "") -> str:
    """Create a timestamped backup of persist.db.

    Args:
        backup_dir: Override backup directory (uses config if empty)
        include_journals: Also back up journal files
        label: Optional label for the backup (e.g. "pre-upgrade")

    Returns:
        Summary of what was backed up.
    """
    db_path = get_db_path()
    if not db_path.exists():
        return "No persist.db found. Run `emrys init` first."

    # Determine backup location
    if backup_dir:
        target_dir = Path(backup_dir)
    else:
        target_dir = get_backup_dir()
        if target_dir is None:
            # Fall back to .persist/backups/
            target_dir = get_persist_dir() / "backups"

    target_dir.mkdir(parents=True, exist_ok=True)

    # Timestamp for this backup
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""

    # Get DB stats before backup
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    stats = {}
    for table in ("handoffs", "journal_entries", "knowledge", "agent_status"):
        try:
            row = conn.execute(f"SELECT COUNT(*) as n FROM {table}").fetchone()
            stats[table] = row["n"]
        except sqlite3.OperationalError:
            stats[table] = 0
    conn.close()

    # Copy DB (using SQLite backup API for safety — no partial copies)
    backup_name = f"persist_{ts}{suffix}.db"
    backup_path = target_dir / backup_name

    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    src.close()
    dst.close()

    # Write manifest
    manifest = {
        "timestamp": now.isoformat(),
        "label": label,
        "db_file": backup_name,
        "db_size_kb": backup_path.stat().st_size / 1024,
        "stats": stats,
        "journals": [],
    }

    # Optionally copy journals
    if include_journals:
        journal_dir = get_journal_dir()
        if journal_dir.exists():
            journal_backup = target_dir / f"journals_{ts}{suffix}"
            journal_backup.mkdir(exist_ok=True)
            copied = 0
            for jf in journal_dir.iterdir():
                if jf.is_file():
                    shutil.copy2(jf, journal_backup / jf.name)
                    manifest["journals"].append(jf.name)
                    copied += 1

    manifest_path = target_dir / f"manifest_{ts}{suffix}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    lines = [
        f"Backup created: {backup_path}",
        f"  Size: {manifest['db_size_kb']:.0f} KB",
        f"  Handoffs: {stats.get('handoffs', 0)}",
        f"  Journal entries: {stats.get('journal_entries', 0)}",
        f"  Knowledge entries: {stats.get('knowledge', 0)}",
    ]
    if include_journals and manifest["journals"]:
        lines.append(f"  Journals: {len(manifest['journals'])} files")
    if label:
        lines.append(f"  Label: {label}")

    return "\n".join(lines)


def list_backups(backup_dir: str = "") -> list[dict]:
    """List available backups with their manifests.

    Returns list of {timestamp, label, db_file, db_size_kb, stats} dicts.
    """
    if backup_dir:
        target_dir = Path(backup_dir)
    else:
        target_dir = get_backup_dir()
        if target_dir is None:
            target_dir = get_persist_dir() / "backups"

    if not target_dir.exists():
        return []

    backups = []
    for manifest_file in sorted(target_dir.glob("manifest_*.json"), reverse=True):
        try:
            manifest = json.loads(manifest_file.read_text())
            db_file = target_dir / manifest["db_file"]
            manifest["exists"] = db_file.exists()
            manifest["manifest_path"] = str(manifest_file)
            backups.append(manifest)
        except (json.JSONDecodeError, KeyError):
            continue

    return backups


def restore_backup(backup_path: str) -> str:
    """Restore persist.db from a backup.

    Creates a safety backup of the current DB before restoring.

    Args:
        backup_path: Path to the backup .db file

    Returns:
        Summary of what was restored.
    """
    source = Path(backup_path)
    if not source.exists():
        return f"Backup not found: {backup_path}"

    db_path = get_db_path()

    # Safety: back up current DB before overwriting
    if db_path.exists():
        safety = db_path.with_suffix(".pre-restore.db")
        shutil.copy2(db_path, safety)

    # Restore using SQLite backup API
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(db_path))
    src.backup(dst)
    src.close()
    dst.close()

    # Get restored stats
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    stats = {}
    for table in ("handoffs", "journal_entries", "knowledge"):
        try:
            row = conn.execute(f"SELECT COUNT(*) as n FROM {table}").fetchone()
            stats[table] = row["n"]
        except sqlite3.OperationalError:
            stats[table] = 0
    conn.close()

    return (
        f"Restored from: {source.name}\n"
        f"  Safety backup: {db_path.with_suffix('.pre-restore.db').name}\n"
        f"  Handoffs: {stats.get('handoffs', 0)}\n"
        f"  Knowledge: {stats.get('knowledge', 0)}\n"
        f"  Journal entries: {stats.get('journal_entries', 0)}"
    )
