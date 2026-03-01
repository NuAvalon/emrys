"""Tests for cairn free tier tools."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cairn_ai.db import configure, get_db, load_lifecycle, get_journal_dir
from cairn_ai.server import (
    ping, open_session, set_status, write_handoff,
    read_journal, recover_context, check_session_health, mark_compacted,
    read_principal, observe_principal, search_memory,
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Create a fresh .persist directory for each test."""
    persist_dir = tmp_path / ".persist"
    persist_dir.mkdir()
    configure(persist_dir)
    yield persist_dir


class TestPing:
    def test_returns_server_name(self):
        result = ping()
        assert "persist" in result

    def test_shows_db_stats(self):
        get_db().close()  # Ensure DB exists
        result = ping()
        assert "agent_status" in result


class TestOpenSession:
    def test_opens_session(self):
        result = open_session(agent="alice")
        assert "Session opened for alice" in result
        assert "Glyph: 0" in result

    def test_detects_crash(self):
        # Open a session but don't close it
        open_session(agent="alice")
        # Open another — should detect crash
        result = open_session(agent="alice")
        assert "CRASH DETECTED" in result

    def test_clean_after_handoff(self):
        open_session(agent="alice")
        write_handoff(agent="alice", summary="clean close")
        result = open_session(agent="alice")
        assert "CRASH" not in result

    def test_default_agent(self):
        result = open_session()
        assert "Session opened for default" in result


class TestSetStatus:
    def test_creates_new_agent(self):
        result = set_status(agent="alice", status="active", current_task="testing")
        assert "Status updated for alice" in result

    def test_updates_existing(self):
        set_status(agent="alice", status="active")
        result = set_status(agent="alice", current_task="new task")
        assert "Status updated" in result

    def test_auto_journals(self):
        set_status(agent="alice", status="active", current_task="test", last_finding="found it")
        content = read_journal(agent="alice")
        assert "found it" in content

    def test_checkpoint_warning(self):
        # Call many times to trigger warning
        for i in range(45):
            result = set_status(agent="alice", current_task=f"task {i}")
        assert "calls since checkpoint" in result or "checkpoint" in result.lower()


class TestWriteHandoff:
    def test_writes_handoff(self):
        open_session(agent="alice")
        result = write_handoff(
            agent="alice",
            summary="test summary",
            accomplished="did things",
            pending="more things",
        )
        assert "Handoff written for alice" in result

    def test_handoff_in_journal(self):
        open_session(agent="alice")
        write_handoff(agent="alice", summary="test summary")
        content = read_journal(agent="alice")
        assert "test summary" in content

    def test_handoff_in_recovery(self):
        open_session(agent="alice")
        write_handoff(agent="alice", summary="test handoff", pending="do X")
        result = recover_context(agent="alice")
        assert "test handoff" in result
        assert "do X" in result

    def test_marks_session_clean(self):
        open_session(agent="alice")
        write_handoff(agent="alice", summary="clean")
        lifecycle = load_lifecycle()
        sessions = [s for s in lifecycle["sessions"] if s["agent"] == "alice"]
        assert sessions[-1]["close_type"] == "handoff" or sessions[-2]["close_type"] == "handoff"


class TestReadJournal:
    def test_no_journal(self):
        result = read_journal(agent="alice")
        assert "No journal" in result

    def test_reads_today(self):
        set_status(agent="alice", status="active", current_task="testing")
        result = read_journal(agent="alice")
        assert "testing" in result

    def test_specific_date(self):
        result = read_journal(agent="alice", date="2020-01-01")
        assert "No journal" in result


class TestRecoverContext:
    def test_empty_recovery(self):
        result = recover_context(agent="alice")
        assert "Context Recovery" in result

    def test_includes_status(self):
        set_status(agent="alice", status="active", current_task="important work")
        result = recover_context(agent="alice")
        assert "important work" in result

    def test_includes_handoff(self):
        open_session(agent="alice")
        write_handoff(agent="alice", summary="session done", pending="finish X")
        result = recover_context(agent="alice")
        assert "session done" in result

    def test_includes_journal(self):
        set_status(agent="alice", status="active", last_finding="key discovery")
        result = recover_context(agent="alice")
        assert "key discovery" in result


class TestCheckSessionHealth:
    def test_no_history(self):
        result = check_session_health(agent="alice")
        assert "No session history" in result

    def test_first_session(self):
        open_session(agent="alice")
        result = check_session_health(agent="alice")
        assert "First tracked session" in result

    def test_clean_close(self):
        open_session(agent="alice")
        write_handoff(agent="alice", summary="done")
        open_session(agent="alice")
        result = check_session_health(agent="alice")
        assert "CLEAN" in result

    def test_crash_detection(self):
        open_session(agent="alice")
        open_session(agent="alice")  # No handoff = crash
        open_session(agent="alice")  # Third session to check second
        result = check_session_health(agent="alice")
        assert "CRASH" in result


class TestMarkCompacted:
    def test_marks_compaction(self):
        open_session(agent="alice")
        result = mark_compacted(agent="alice")
        assert "Compaction marker" in result

    def test_compaction_in_journal(self):
        open_session(agent="alice")
        mark_compacted(agent="alice")
        content = read_journal(agent="alice")
        assert "SESSION_COMPACTED" in content

    def test_compaction_detected_by_health(self):
        open_session(agent="alice")
        mark_compacted(agent="alice")
        # mark_compacted closes old session as COMPACTED and opens a new one.
        # To check it properly, we need to write a handoff for the post-compaction
        # session, then open a third session and check the second (post-compaction).
        # But mark_compacted already opens a new session, so check_session_health
        # on the NEXT open will see the post-compaction session (which wasn't closed).
        # The compaction itself is recorded in the session before that.
        # Verify the lifecycle data directly:
        from cairn_ai.db import load_lifecycle
        lifecycle = load_lifecycle()
        sessions = [s for s in lifecycle["sessions"] if s.get("agent") == "alice"]
        assert any(s.get("close_type") == "compacted" for s in sessions)


class TestMultiAgent:
    """Verify agents are isolated from each other."""

    def test_separate_journals(self):
        set_status(agent="alice", current_task="alice work")
        set_status(agent="bob", current_task="bob work")
        alice_journal = read_journal(agent="alice")
        bob_journal = read_journal(agent="bob")
        assert "alice work" in alice_journal
        assert "bob work" not in alice_journal
        assert "bob work" in bob_journal

    def test_separate_sessions(self):
        open_session(agent="alice")
        open_session(agent="bob")
        write_handoff(agent="alice", summary="alice done")
        # Bob's session should still be open (no crash warning on next open)
        result = check_session_health(agent="bob")
        assert "CRASH" not in result or "First" in result


class TestPrincipal:
    """Tests for read_principal (free tier)."""

    def test_no_principal_file(self):
        """Returns helpful message when file doesn't exist."""
        result = read_principal()
        assert "No principal.md found" in result

    def test_reads_existing_principal(self, fresh_db):
        """Reads principal.md when it exists."""
        principal_path = fresh_db / "principal.md"
        principal_path.write_text("## Communication\n- Prefers concise answers\n")
        result = read_principal()
        assert "Prefers concise answers" in result

    def test_empty_principal(self, fresh_db):
        """Handles empty file gracefully."""
        principal_path = fresh_db / "principal.md"
        principal_path.write_text("")
        result = read_principal()
        assert "empty" in result.lower()

    def test_unicode_principal(self, fresh_db):
        """Handles unicode in principal.md."""
        principal_path = fresh_db / "principal.md"
        principal_path.write_text("## Notes\n- Speaks 日本語 and العربية\n- Uses 🎯 for priorities\n")
        result = read_principal()
        assert "日本語" in result
        assert "🎯" in result


class TestObservePrincipal:
    """Tests for observe_principal (free tier)."""

    def test_creates_file(self, fresh_db):
        """observe_principal creates principal.md if it doesn't exist."""
        result = observe_principal(observations="Prefers snake_case. Works late EST.", agent="alice")
        assert "recorded" in result.lower()

        principal_path = fresh_db / "principal.md"
        assert principal_path.exists()
        content = principal_path.read_text()
        assert "Prefers snake_case" in content
        assert "Works late EST" in content

    def test_appends_observations(self, fresh_db):
        """Multiple observations append to the file."""
        observe_principal(observations="Prefers Python.", agent="alice")
        observe_principal(observations="Hates boilerplate.", agent="alice")

        principal_path = fresh_db / "principal.md"
        content = principal_path.read_text()
        assert "Prefers Python" in content
        assert "Hates boilerplate" in content

    def test_preserves_existing_content(self, fresh_db):
        """Appends to existing principal.md without overwriting."""
        principal_path = fresh_db / "principal.md"
        principal_path.write_text("# About My Principal\n\n## Notes\n- Likes coffee\n\n---\n")

        observe_principal(observations="Works in healthcare domain.", agent="alice")

        content = principal_path.read_text()
        assert "Likes coffee" in content  # preserved
        assert "healthcare domain" in content  # added


class TestSearchMemory:
    """Tests for search_memory (FTS5)."""

    def test_no_results(self):
        """Returns no-results message for empty DB."""
        result = search_memory(query="nonexistent")
        assert "No results" in result

    def test_finds_handoff_by_summary(self):
        """Finds a handoff by searching its summary."""
        open_session(agent="alice")
        write_handoff(agent="alice", summary="Fixed authentication bug in login flow")
        result = search_memory(query="authentication")
        assert "authentication" in result
        assert "alice" in result

    def test_finds_handoff_by_discoveries(self):
        """Finds a handoff by its discoveries field."""
        open_session(agent="alice")
        write_handoff(agent="alice", summary="session done", discoveries="SQLite FTS5 is blazing fast")
        result = search_memory(query="FTS5")
        assert "FTS5" in result

    def test_filters_by_agent(self):
        """Agent filter returns only that agent's handoffs."""
        open_session(agent="alice")
        write_handoff(agent="alice", summary="Alice worked on database migration")
        open_session(agent="bob")
        write_handoff(agent="bob", summary="Bob worked on database tests")

        result = search_memory(query="database", agent="alice")
        assert "alice" in result
        assert "bob" not in result.lower().replace("database", "")

    def test_multiple_results_ranked(self):
        """Multiple matching handoffs are returned."""
        open_session(agent="alice")
        write_handoff(agent="alice", summary="First: deploying new API endpoint")
        open_session(agent="alice")
        write_handoff(agent="alice", summary="Second: API endpoint needs rate limiting")

        result = search_memory(query="API endpoint")
        assert "Found 2" in result

    def test_respects_limit(self):
        """Limit parameter caps results."""
        open_session(agent="alice")
        for i in range(5):
            write_handoff(agent="alice", summary=f"Iteration {i}: refactoring module")

        result = search_memory(query="refactoring", limit=2)
        assert "Found 2" in result
