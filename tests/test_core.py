"""Core unit tests for emrys — db, ingest, search, journal."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from emrys import db


@pytest.fixture
def persist_dir(tmp_path):
    """Create a temporary persist directory with initialized DB."""
    persist = tmp_path / ".persist"
    persist.mkdir()
    (persist / "journals").mkdir()
    db.configure(persist)
    yield persist


@pytest.fixture
def db_conn(persist_dir):
    """Get a DB connection to the test database."""
    conn = db.get_db()
    yield conn
    conn.close()


# ── DB tests ──

class TestDB:
    def test_init_creates_tables(self, db_conn):
        tables = {row[0] for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        assert "knowledge" in tables
        assert "journal_entries" in tables
        assert "glyph_counters" in tables

    def test_store_and_retrieve_knowledge(self, db_conn):
        db_conn.execute(
            """INSERT INTO knowledge (agent, topic, title, content, tags, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("test", "architecture", "Test entry", "Some content",
             "test,unit", "test", datetime.now(timezone.utc).isoformat()),
        )
        db_conn.commit()

        row = db_conn.execute(
            "SELECT title, content FROM knowledge WHERE agent = 'test'"
        ).fetchone()
        assert row is not None
        assert row["title"] == "Test entry"
        assert row["content"] == "Some content"

    def test_fts_search(self, db_conn):
        db_conn.execute(
            """INSERT INTO knowledge (agent, topic, title, content, tags, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("test", "research", "Gradient boosting results",
             "The model achieved 0.72 AUC on validation set",
             "ml,results", "test", datetime.now(timezone.utc).isoformat()),
        )
        db_conn.commit()

        rows = db_conn.execute(
            """SELECT k.title FROM knowledge_fts f
               JOIN knowledge k ON k.id = f.rowid
               WHERE knowledge_fts MATCH 'gradient'"""
        ).fetchall()
        assert len(rows) == 1
        assert "Gradient" in rows[0]["title"]


# ── Ingest tests ──

class TestIngest:
    def _make_jsonl(self, tmp_path, records):
        path = tmp_path / "test_session.jsonl"
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return str(path)

    def test_extracts_user_instructions(self, tmp_path, persist_dir):
        from emrys.ingest import ingest_transcript

        path = self._make_jsonl(tmp_path, [
            {
                "timestamp": "2026-01-15T10:00:00Z",
                "type": "user",
                "message": {
                    "role": "human",
                    "content": "Please fix the bug in the authentication module, it crashes on empty passwords"
                }
            }
        ])
        result = ingest_transcript(path, agent="test", dry_run=True)
        assert "user-instruction" in result

    def test_extracts_decisions(self, tmp_path, persist_dir):
        from emrys.ingest import ingest_transcript

        path = self._make_jsonl(tmp_path, [
            {
                "timestamp": "2026-01-15T10:05:00Z",
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": (
                        "The root cause is that the password validation function "
                        "doesn't check for None before calling .strip(). The fix is "
                        "to add an early return for falsy values. This works because "
                        "empty strings and None should both be rejected at the boundary."
                    )
                }
            }
        ])
        result = ingest_transcript(path, agent="test", dry_run=True)
        assert "decision" in result

    def test_skips_mechanical_messages(self, tmp_path, persist_dir):
        from emrys.ingest import ingest_transcript

        path = self._make_jsonl(tmp_path, [
            {
                "timestamp": "2026-01-15T10:00:00Z",
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "Let me read the file to understand the current implementation."
                }
            }
        ])
        result = ingest_transcript(path, agent="test", dry_run=True)
        assert "No notable entries" in result

    def test_skips_benign_errors(self, tmp_path, persist_dir):
        from emrys.ingest import ingest_transcript

        path = self._make_jsonl(tmp_path, [
            {
                "timestamp": "2026-01-15T10:00:00Z",
                "type": "user",
                "message": {
                    "role": "human",
                    "content": [
                        {"type": "tool_result", "content": "Error: no such file or directory: /tmp/nonexistent.py"}
                    ]
                }
            }
        ])
        result = ingest_transcript(path, agent="test", dry_run=True)
        assert "No notable entries" in result


# ── Search tests ──

class TestSearchFTS:
    def test_keyword_search_returns_results(self, db_conn, persist_dir):
        from emrys.search import search_fts

        db_conn.execute(
            """INSERT INTO knowledge (agent, topic, title, content, tags, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("test", "lesson", "Database migration strategy",
             "Always run migrations in a transaction to ensure atomicity",
             "db,lesson", "test", datetime.now(timezone.utc).isoformat()),
        )
        db_conn.commit()

        results = search_fts("migration")
        assert len(results) >= 1
        assert "migration" in results[0]["title"].lower()

    def test_keyword_search_no_results(self, db_conn, persist_dir):
        from emrys.search import search_fts

        results = search_fts("xyznonexistent")
        assert len(results) == 0


# ── Journal tests ──

class TestJournal:
    def test_write_and_read_journal(self, persist_dir):
        from emrys.journal import write_journal, read_journal_file

        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        today = now.strftime("%Y-%m-%d")

        write_journal(
            agent="test", status="active",
            task="Running tests", finding="All passing",
            timestamp=ts,
        )

        content = read_journal_file(agent="test", date=today)
        assert "Running tests" in content
        assert "All passing" in content
