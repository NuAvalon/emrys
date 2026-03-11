"""SQLite database helpers and schema initialization."""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("emrys")

SCHEMA_VERSION = 4  # Bump when schema changes. Add migration in _MIGRATIONS.

# Default paths — overridden by init() or config
_persist_dir: Path | None = None
_db_path: Path | None = None
_journal_dir: Path | None = None


def get_persist_dir() -> Path:
    """Return the .persist directory, auto-detecting from CWD if not configured."""
    global _persist_dir
    if _persist_dir is not None:
        return _persist_dir
    # Walk up from CWD looking for .persist/
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        candidate = parent / ".persist"
        if candidate.is_dir():
            _persist_dir = candidate
            return _persist_dir
    # Default to CWD/.persist
    _persist_dir = cwd / ".persist"
    return _persist_dir


def get_db_path() -> Path:
    """Return path to persist.db."""
    global _db_path
    if _db_path is not None:
        return _db_path
    _db_path = get_persist_dir() / "persist.db"
    return _db_path


def get_journal_dir() -> Path:
    """Return path to the journals directory."""
    global _journal_dir
    if _journal_dir is not None:
        return _journal_dir
    _journal_dir = get_persist_dir() / "journals"
    return _journal_dir


def configure(persist_dir: Path):
    """Explicitly set the .persist directory. Call before any DB operations."""
    global _persist_dir, _db_path, _journal_dir
    _persist_dir = Path(persist_dir)
    _db_path = _persist_dir / "persist.db"
    _journal_dir = _persist_dir / "journals"


def get_db() -> sqlite3.Connection:
    """Get a database connection with schema initialized."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    """Create all tables if they don't exist."""

    # --- FREE TIER ---

    # Agent status tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_status (
            agent TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'idle',
            current_task TEXT DEFAULT '',
            last_finding TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            tool_calls_since_checkpoint INTEGER DEFAULT 0
        )
    """)

    # Glyph counters (monotonic per-agent for crash recovery)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glyph_counters (
            agent TEXT PRIMARY KEY,
            counter INTEGER NOT NULL DEFAULT 0,
            last_incremented_at TEXT NOT NULL DEFAULT ''
        )
    """)

    # Sync points for crash/compaction recovery
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            sync_num INTEGER NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sync_agent ON sync_points(agent, sync_num)
    """)

    # Handoffs (structured, separate from journal)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS handoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL DEFAULT 'default',
            ts TEXT NOT NULL,
            summary TEXT NOT NULL,
            accomplished TEXT DEFAULT '',
            pending TEXT DEFAULT '',
            discoveries TEXT DEFAULT ''
        )
    """)

    # Journal entries (DB mirror of file-based journals, for FTS)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            ts TEXT NOT NULL,
            status TEXT DEFAULT '',
            task TEXT DEFAULT '',
            finding TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_journal_agent ON journal_entries(agent)
    """)

    # Full-text search index over handoffs
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS handoffs_fts USING fts5(
            summary, accomplished, pending, discoveries,
            content='handoffs', content_rowid='id'
        )
    """)

    # FTS index over journal entries
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
            task, finding,
            content='journal_entries', content_rowid='id'
        )
    """)

    # Knowledge entries (mind palace seed — extracted findings, tagged)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL DEFAULT 'default',
            topic TEXT NOT NULL DEFAULT 'general',
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT DEFAULT '',
            source TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_knowledge_agent ON knowledge(agent)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_knowledge_tags ON knowledge(tags)
    """)

    # FTS index over knowledge
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            title, content, tags,
            content='knowledge', content_rowid='id'
        )
    """)

    # Triggers to keep FTS in sync
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS handoffs_ai AFTER INSERT ON handoffs BEGIN
            INSERT INTO handoffs_fts(rowid, summary, accomplished, pending, discoveries)
            VALUES (new.id, new.summary, new.accomplished, new.pending, new.discoveries);
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS journal_ai AFTER INSERT ON journal_entries BEGIN
            INSERT INTO journal_fts(rowid, task, finding)
            VALUES (new.id, new.task, new.finding);
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
            INSERT INTO knowledge_fts(rowid, title, content, tags)
            VALUES (new.id, new.title, new.content, new.tags);
        END
    """)

    # FTS sync triggers for UPDATE and DELETE on knowledge
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags)
            VALUES ('delete', old.id, old.title, old.content, old.tags);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags)
            VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts(rowid, title, content, tags)
            VALUES (new.id, new.title, new.content, new.tags);
        END
    """)

    conn.commit()

    # Run schema migrations
    _run_migrations(conn)


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version. Returns 0 if untracked."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] if row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def _run_migrations(conn: sqlite3.Connection):
    """Check schema version and apply any pending migrations."""
    # Create version table if missing
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)

    current = _get_schema_version(conn)
    if current >= SCHEMA_VERSION:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Run all migrations from current+1 to SCHEMA_VERSION
    # On fresh DB (current=0), this runs ALL migrations to create any new tables
    for v in range(current + 1, SCHEMA_VERSION + 1):
        if v in _MIGRATIONS:
            log.info("Running migration to v%d", v)
            _MIGRATIONS[v](conn)
        conn.execute(
            "INSERT INTO schema_version (version, migrated_at) VALUES (?, ?)",
            (v, now),
        )

    conn.commit()
    if current == 0:
        log.info("Schema initialized at v%d", SCHEMA_VERSION)
    else:
        log.info("Schema migrated v%d → v%d", current, SCHEMA_VERSION)


def _migrate_to_v2(conn: sqlite3.Connection):
    """v1 → v2: Add knowledge_vectors table for optional vector search."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_vectors (
            id INTEGER PRIMARY KEY,
            knowledge_id INTEGER NOT NULL UNIQUE,
            embedding BLOB NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (knowledge_id) REFERENCES knowledge(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_kv_knowledge ON knowledge_vectors(knowledge_id)
    """)


def _migrate_to_v3(conn: sqlite3.Connection):
    """v2 -> v3: Add agent_keys table for cryptographic agent identity."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_keys (
            agent TEXT PRIMARY KEY,
            public_key_pem TEXT NOT NULL,
            fingerprint TEXT NOT NULL DEFAULT '',
            registered_at TEXT NOT NULL,
            last_auth_at TEXT DEFAULT NULL
        )
    """)


def _migrate_to_v4(conn: sqlite3.Connection):
    """v3 -> v4: Add sovereign identity tables (delegation certs, revocations)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delegation_certs (
            agent TEXT PRIMARY KEY,
            scopes TEXT NOT NULL DEFAULT '',
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            human_fingerprint TEXT NOT NULL DEFAULT '',
            agent_fingerprint TEXT NOT NULL DEFAULT '',
            cert_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS revocations (
            agent TEXT PRIMARY KEY,
            revoked_at TEXT NOT NULL,
            reason TEXT DEFAULT '',
            revocation_json TEXT NOT NULL DEFAULT '{}'
        )
    """)


# Migration registry: version -> callable(conn)
_MIGRATIONS: dict[int, callable] = {
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
}


EXPECTED_TABLES = [
    "agent_status", "glyph_counters", "sync_points",
    "handoffs", "journal_entries", "knowledge",
    "schema_version", "knowledge_vectors", "agent_keys",
    "delegation_certs", "revocations",
]


def verify_schema(conn: sqlite3.Connection) -> list[str]:
    """Return list of missing tables. Empty = all good."""
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return [t for t in EXPECTED_TABLES if t not in existing]


def get_lifecycle_path() -> Path:
    """Return path to session_lifecycle.json."""
    return get_persist_dir() / "session_lifecycle.json"


def load_lifecycle() -> dict:
    """Load session lifecycle tracking data."""
    lf = get_lifecycle_path()
    if lf.exists():
        try:
            return json.loads(lf.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {"sessions": []}


def save_lifecycle(data: dict):
    """Save session lifecycle data."""
    lf = get_lifecycle_path()
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(json.dumps(data, indent=2))
