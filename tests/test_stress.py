"""Stress test for cairn — QA before Gumroad listing.

Tests realistic usage patterns:
1. Multi-agent concurrent access (4 agents, rapid status updates)
2. High-volume messaging (100+ messages, mark_read in batch)
3. Large concept maps (50+ concepts with links and perspectives)
4. Recovery flows (crash → recovery → verify integrity)
5. Edge cases (Unicode, long strings, special characters)
6. Journal accumulation (30 days simulated)
7. Knowledge volume (100+ entries with search)
"""

import os
import sqlite3
import tempfile
import time
import threading
from pathlib import Path
from datetime import datetime, timezone

import pytest

# Configure before any imports touch the DB
_tmpdir = None


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Each test gets a fresh .persist directory."""
    global _tmpdir
    persist_dir = tmp_path / ".persist"
    persist_dir.mkdir()
    (persist_dir / "journals").mkdir()

    import cairn_ai.db as db_mod
    db_mod.configure(persist_dir)
    _tmpdir = persist_dir
    yield persist_dir

    # Reset for next test
    db_mod._persist_dir = None
    db_mod._db_path = None
    db_mod._journal_dir = None


# ── 1. Multi-agent concurrent access ──────────────────────────────────────────

class TestConcurrentAccess:
    def test_four_agents_rapid_status(self, fresh_db):
        """4 agents writing status updates concurrently."""
        from cairn_ai.db import get_db

        errors = []
        agents = ["archie", "apollo", "athena", "hypatia"]

        def agent_work(agent_name, n_updates):
            try:
                for i in range(n_updates):
                    conn = get_db()
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    conn.execute(
                        "INSERT OR REPLACE INTO agent_status (agent, status, current_task, last_finding, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (agent_name, "active", f"Task {i}", f"Finding {i}", now),
                    )
                    conn.commit()
                    conn.close()
            except Exception as e:
                errors.append((agent_name, str(e)))

        threads = [
            threading.Thread(target=agent_work, args=(a, 50)) for a in agents
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent errors: {errors}"

        # Verify all 4 agents have final state
        conn = get_db()
        rows = conn.execute("SELECT agent FROM agent_status").fetchall()
        conn.close()
        assert len(rows) == 4

    def test_concurrent_messages(self, fresh_db):
        """Multiple agents sending messages simultaneously."""
        from cairn_ai.db import get_db

        errors = []

        def send_messages(from_agent, to_agent, count):
            try:
                for i in range(count):
                    conn = get_db()
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    conn.execute(
                        "INSERT INTO messages (from_agent, to_agent, subject, body, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (from_agent, to_agent, f"Msg {i}", f"Body {i}", now),
                    )
                    conn.commit()
                    conn.close()
            except Exception as e:
                errors.append((from_agent, str(e)))

        threads = [
            threading.Thread(target=send_messages, args=("archie", "athena", 25)),
            threading.Thread(target=send_messages, args=("athena", "archie", 25)),
            threading.Thread(target=send_messages, args=("apollo", "hypatia", 25)),
            threading.Thread(target=send_messages, args=("hypatia", "apollo", 25)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Message errors: {errors}"

        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        assert count == 100


# ── 2. High volume ────────────────────────────────────────────────────────────

class TestHighVolume:
    def test_200_status_updates(self, fresh_db):
        """200 rapid status updates from one agent."""
        from cairn_ai.db import get_db

        for i in range(200):
            conn = get_db()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT OR REPLACE INTO agent_status (agent, status, current_task, updated_at, tool_calls_since_checkpoint) "
                "VALUES (?, 'active', ?, ?, ?)",
                ("athena", f"Task {i}", now, i),
            )
            conn.commit()
            conn.close()

        conn = get_db()
        row = conn.execute(
            "SELECT current_task, tool_calls_since_checkpoint FROM agent_status WHERE agent = ?",
            ("athena",),
        ).fetchone()
        conn.close()
        assert row[0] == "Task 199"
        assert row[1] == 199

    def test_500_messages_with_bulk_read(self, fresh_db):
        """500 messages, then bulk mark-read."""
        from cairn_ai.db import get_db

        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(500):
            conn.execute(
                "INSERT INTO messages (from_agent, to_agent, subject, body, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("archie", "athena", f"Subject {i}", f"Body {i}", now),
            )
        conn.commit()

        # Count unread
        unread = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE to_agent = 'athena' AND is_read = 0"
        ).fetchone()[0]
        assert unread == 500

        # Bulk mark read
        conn.execute(
            "UPDATE messages SET is_read = 1 WHERE to_agent = 'athena' AND is_read = 0"
        )
        conn.commit()

        unread_after = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE to_agent = 'athena' AND is_read = 0"
        ).fetchone()[0]
        conn.close()
        assert unread_after == 0

    def test_100_knowledge_entries(self, fresh_db):
        """100 knowledge entries with varying topics and tags."""
        from cairn_ai.db import get_db

        topics = ["research", "decision", "lesson", "architecture", "bug"]
        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(100):
            conn.execute(
                "INSERT INTO knowledge (topic, title, content, tags, agent, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    topics[i % len(topics)],
                    f"Entry {i}: {topics[i % len(topics)]}",
                    f"Content for entry {i}. " * 20,  # ~400 chars each
                    f"tag{i % 5},tag{i % 3}",
                    ["archie", "apollo", "athena", "hypatia"][i % 4],
                    now,
                ),
            )
        conn.commit()

        count = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        assert count == 100

        # Topic filter
        research = conn.execute(
            "SELECT COUNT(*) FROM knowledge WHERE topic = 'research'"
        ).fetchone()[0]
        assert research == 20

        conn.close()


# ── 3. Large concept maps ─────────────────────────────────────────────────────

class TestLargeConceptMap:
    def test_50_concepts_with_links(self, fresh_db):
        """50 concepts with inter-links and perspectives."""
        from cairn_ai.db import get_db

        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Create 50 concepts
        for i in range(50):
            conn.execute(
                "INSERT INTO concepts (name, summary, domain, state, version, agent, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', 1, 'athena', ?, ?)",
                (f"Concept {i}", f"Summary for concept {i}", ["ml", "trading", "infrastructure"][i % 3], now, now),
            )

        # Create 100 links (each concept linked to 2 others)
        for i in range(50):
            for j in [1, 2]:
                target = (i + j) % 50
                conn.execute(
                    "INSERT INTO concept_links (from_concept, to_concept, link_type, agent, created_at) "
                    "VALUES (?, ?, ?, 'athena', ?)",
                    (f"Concept {i}", f"Concept {target}", "related", now),
                )

        # Add perspectives
        for i in range(50):
            conn.execute(
                "INSERT INTO concept_perspectives (concept_name, perspective, agent, created_at) "
                "VALUES (?, ?, ?, ?)",
                (f"Concept {i}", f"Athena's view on concept {i}", "athena", now),
            )

        conn.commit()

        concepts = conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]
        links = conn.execute("SELECT COUNT(*) FROM concept_links").fetchone()[0]
        perspectives = conn.execute("SELECT COUNT(*) FROM concept_perspectives").fetchone()[0]

        conn.close()

        assert concepts == 50
        assert links == 100
        assert perspectives == 50

    def test_concept_versioning_at_scale(self, fresh_db):
        """One concept updated 20 times — verify version history."""
        from cairn_ai.db import get_db

        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn.execute(
            "INSERT INTO concepts (name, summary, version, agent, created_at, updated_at) "
            "VALUES ('Evolving Idea', 'v1', 1, 'archie', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO concept_history (concept_name, version, summary, agent) "
            "VALUES ('Evolving Idea', 1, 'v1', 'archie')",
        )

        for v in range(2, 21):
            conn.execute(
                "UPDATE concepts SET summary = ?, version = ?, updated_at = ? WHERE name = 'Evolving Idea'",
                (f"v{v}", v, now),
            )
            conn.execute(
                "INSERT INTO concept_history (concept_name, version, summary, agent) "
                "VALUES ('Evolving Idea', ?, ?, ?)",
                (v, f"v{v}", ["archie", "apollo", "athena"][v % 3]),
            )

        conn.commit()

        current = conn.execute(
            "SELECT version, summary FROM concepts WHERE name = 'Evolving Idea'"
        ).fetchone()
        history = conn.execute(
            "SELECT COUNT(*) FROM concept_history WHERE concept_name = 'Evolving Idea'"
        ).fetchone()[0]

        conn.close()
        assert current[0] == 20
        assert current[1] == "v20"
        assert history == 20


# ── 4. Recovery flows ─────────────────────────────────────────────────────────

class TestRecoveryFlows:
    def test_crash_and_recovery(self, fresh_db):
        """Simulate: open session → write status → crash → recover."""
        from cairn_ai.db import get_db, load_lifecycle, save_lifecycle

        # Open session
        lf = load_lifecycle()
        lf["sessions"].append({
            "agent": "athena",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "close_type": None,
        })
        save_lifecycle(lf)

        # Write some status
        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT OR REPLACE INTO agent_status (agent, status, current_task, updated_at) "
            "VALUES ('athena', 'active', 'Important work', ?)",
            (now,),
        )
        conn.commit()
        conn.close()

        # Simulate crash (no handoff, no lifecycle close)
        # Now "recover"
        lf2 = load_lifecycle()
        last = lf2["sessions"][-1]
        assert last["closed_at"] is None  # crash detected

        # Recovery: read last status
        conn = get_db()
        row = conn.execute(
            "SELECT status, current_task FROM agent_status WHERE agent = 'athena'"
        ).fetchone()
        conn.close()
        assert row[0] == "active"
        assert row[1] == "Important work"  # data survived

    def test_journal_survives_crash(self, fresh_db):
        """Journal entries persist across simulated crashes."""
        from cairn_ai.journal import write_journal, read_journal_file

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Write journal entries
        write_journal("athena", "active", "First entry before crash", "", now)
        write_journal("athena", "active", "Second entry before crash", "", now)

        # "Crash" — no cleanup
        # "Recover" — read journal
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = read_journal_file("athena", today)
        assert "First entry" in content
        assert "Second entry" in content


# ── 5. Edge cases ─────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_unicode_in_all_fields(self, fresh_db):
        """Unicode characters in every text field."""
        from cairn_ai.db import get_db

        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Status with emoji/CJK/Arabic
        conn.execute(
            "INSERT OR REPLACE INTO agent_status (agent, status, current_task, last_finding, updated_at) "
            "VALUES ('athena', 'active', '🦉 建造 البناء', '発見 اكتشاف 🔍', ?)",
            (now,),
        )

        # Message with unicode
        conn.execute(
            "INSERT INTO messages (from_agent, to_agent, subject, body, created_at) "
            "VALUES ('archie', 'athena', '件名 الموضوع', 'Содержание 内容 🌍', ?)",
            (now,),
        )

        # Concept with unicode
        conn.execute(
            "INSERT INTO concepts (name, summary, created_at, updated_at) "
            "VALUES ('概念 مفهوم Концепция', 'Résumé 摘要 ملخص', ?, ?)",
            (now, now),
        )

        conn.commit()

        # Verify round-trip
        row = conn.execute(
            "SELECT current_task FROM agent_status WHERE agent = 'athena'"
        ).fetchone()
        assert "🦉" in row[0]
        assert "建造" in row[0]

        msg = conn.execute(
            "SELECT body FROM messages WHERE to_agent = 'athena'"
        ).fetchone()
        assert "Содержание" in msg[0]
        assert "🌍" in msg[0]

        concept = conn.execute(
            "SELECT summary FROM concepts WHERE name LIKE '%概念%'"
        ).fetchone()
        assert "Résumé" in concept[0]

        conn.close()

    def test_very_long_strings(self, fresh_db):
        """10KB+ strings in message body and concept summary."""
        from cairn_ai.db import get_db

        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        long_body = "A" * 50000  # 50KB
        conn.execute(
            "INSERT INTO messages (from_agent, to_agent, subject, body, created_at) "
            "VALUES ('archie', 'athena', 'Long message', ?, ?)",
            (long_body, now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT LENGTH(body) FROM messages WHERE subject = 'Long message'"
        ).fetchone()
        conn.close()
        assert row[0] == 50000

    def test_special_sql_characters(self, fresh_db):
        """SQL injection attempts are safely handled by parameterized queries."""
        from cairn_ai.db import get_db

        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        evil = "'; DROP TABLE messages; --"
        conn.execute(
            "INSERT INTO messages (from_agent, to_agent, subject, body, created_at) "
            "VALUES ('archie', 'athena', ?, ?, ?)",
            (evil, evil, now),
        )
        conn.commit()

        # Table still exists
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert count == 1

        row = conn.execute("SELECT subject FROM messages").fetchone()
        assert row[0] == evil  # stored as literal text
        conn.close()

    def test_empty_strings(self, fresh_db):
        """Empty strings in optional fields."""
        from cairn_ai.db import get_db

        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn.execute(
            "INSERT OR REPLACE INTO agent_status (agent, status, current_task, last_finding, updated_at) "
            "VALUES ('athena', 'active', '', '', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO concepts (name, summary, domain, tags, created_at, updated_at) "
            "VALUES ('Empty Test', '', '', '', ?, ?)",
            (now, now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT current_task FROM agent_status WHERE agent = 'athena'"
        ).fetchone()
        assert row[0] == ""
        conn.close()


# ── 6. Journal accumulation ───────────────────────────────────────────────────

class TestJournalAccumulation:
    def test_30_days_of_journals(self, fresh_db):
        """30 days of journal entries with multiple entries per day."""
        from cairn_ai.journal import write_journal, read_journal_file
        from cairn_ai.db import get_journal_dir

        for day in range(30):
            date_str = f"2026-02-{day + 1:02d}"
            journal_path = get_journal_dir() / f"athena_{date_str}.md"
            # Write 10 entries per day
            entries = []
            for i in range(10):
                entries.append(f"## {date_str}T{i:02d}:00:00Z\n- Status: active\n- Task: Day {day} task {i}\n")
            journal_path.write_text("\n".join(entries))

        # Verify all journals exist
        journals = list(get_journal_dir().glob("athena_*.md"))
        assert len(journals) == 30

        # Read a specific day
        content = (get_journal_dir() / "athena_2026-02-15.md").read_text()
        assert "Day 14 task 9" in content

    def test_journal_write_performance(self, fresh_db):
        """100 journal writes should complete in < 5 seconds."""
        from cairn_ai.journal import write_journal

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = time.time()
        for i in range(100):
            write_journal("athena", "active", f"Entry {i}: " + "x" * 200, "", now)
        elapsed = time.time() - start

        assert elapsed < 5.0, f"100 journal writes took {elapsed:.2f}s (> 5s)"


# ── 7. DB size sanity ─────────────────────────────────────────────────────────

class TestDBSize:
    def test_db_size_after_heavy_use(self, fresh_db):
        """After 500 messages + 50 concepts + 100 knowledge + 200 status updates, DB < 5MB."""
        from cairn_ai.db import get_db, get_db_path

        conn = get_db()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 500 messages
        for i in range(500):
            conn.execute(
                "INSERT INTO messages (from_agent, to_agent, subject, body, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("archie", "athena", f"Subject {i}", f"Body content {i} " * 10, now),
            )

        # 50 concepts
        for i in range(50):
            conn.execute(
                "INSERT INTO concepts (name, summary, domain, version, created_at, updated_at) "
                "VALUES (?, ?, 'ml', 1, ?, ?)",
                (f"Concept {i}", f"Summary {i} " * 20, now, now),
            )

        # 100 knowledge entries
        for i in range(100):
            conn.execute(
                "INSERT INTO knowledge (topic, title, content, agent, created_at) "
                "VALUES (?, ?, ?, 'apollo', ?)",
                ("research", f"Entry {i}", f"Content {i} " * 30, now),
            )

        conn.commit()
        conn.close()

        db_size = get_db_path().stat().st_size
        assert db_size < 5 * 1024 * 1024, f"DB is {db_size / 1024 / 1024:.2f}MB (> 5MB)"


# ── 8. Init flow ──────────────────────────────────────────────────────────────

class TestInitFlow:
    def test_clean_init(self, tmp_path):
        """Simulate what happens when a new user runs cairn init."""
        from cairn_ai.db import configure, get_db

        persist_dir = tmp_path / "fresh_project" / ".persist"
        persist_dir.mkdir(parents=True)
        configure(persist_dir)

        # First DB access creates schema
        conn = get_db()

        # Verify all tables exist
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()

        expected = [
            "agent_status", "glyph_counters", "sync_points", "handoffs",
            "messages", "concepts", "concept_history", "concept_links",
            "concept_perspectives", "knowledge", "reasoning_log", "tasks",
        ]
        for table in expected:
            assert table in tables, f"Missing table: {table}"
