"""SQLite database helpers and schema initialization."""

import json
import sqlite3
from pathlib import Path

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

    # --- PAID TIER ---

    # Messages (multi-agent communication)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            priority TEXT DEFAULT 'normal',
            tags TEXT DEFAULT '',
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_agent, is_read)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_agent)
    """)

    # Concept map
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concepts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            summary TEXT NOT NULL,
            domain TEXT DEFAULT '',
            state TEXT DEFAULT 'active',
            aliases TEXT DEFAULT '[]',
            tags TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            agent TEXT DEFAULT '',
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS concept_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            concept_name TEXT NOT NULL,
            version INTEGER NOT NULL,
            summary TEXT NOT NULL,
            state TEXT DEFAULT '',
            agent TEXT DEFAULT '',
            changed_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS concept_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_concept TEXT NOT NULL,
            to_concept TEXT NOT NULL,
            link_type TEXT NOT NULL,
            note TEXT DEFAULT '',
            agent TEXT DEFAULT '',
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS concept_perspectives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            concept_name TEXT NOT NULL,
            perspective TEXT NOT NULL,
            agent TEXT DEFAULT '',
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)

    # Knowledge store
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT DEFAULT '',
            agent TEXT DEFAULT '',
            source TEXT DEFAULT '',
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)

    # Reasoning traces
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reasoning_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            decision TEXT NOT NULL,
            alternatives TEXT NOT NULL DEFAULT '[]',
            chosen TEXT NOT NULL DEFAULT '',
            rationale TEXT NOT NULL DEFAULT '',
            context_refs TEXT NOT NULL DEFAULT '[]',
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reasoning_agent ON reasoning_log(agent)
    """)

    # Task management
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            assigned_to TEXT DEFAULT '',
            created_by TEXT NOT NULL DEFAULT 'user',
            blocked_by TEXT DEFAULT '',
            priority TEXT DEFAULT 'normal',
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
    """)

    conn.commit()


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
