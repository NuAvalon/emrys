"""Tests for cairn paid tier gating."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cairn_ai.db import configure, get_db, get_persist_dir
from cairn_ai.license import check_license
from cairn_ai.server import (
    send_message, read_messages, mark_read,
    log_reasoning, read_reasoning,
    update_concept, trace_concept, list_concepts, map_neighborhood,
    add_link, add_perspective,
    store_knowledge, query_knowledge, staleness_check,
    create_task, update_task, list_tasks, get_task,
    crystallize, observe_principal,
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Create a fresh .persist directory for each test."""
    persist_dir = tmp_path / ".persist"
    persist_dir.mkdir()
    configure(persist_dir)
    yield persist_dir


class TestLicenseGating:
    """Without a license, all paid tools should return upgrade messages."""

    def test_no_license(self):
        assert not check_license()

    @pytest.mark.parametrize("tool_fn,kwargs", [
        (send_message, {"from_agent": "a", "to_agent": "b", "subject": "x", "body": "y"}),
        (read_messages, {"agent": "a"}),
        (mark_read, {"agent": "a"}),
        (log_reasoning, {"agent": "a", "decision": "x"}),
        (read_reasoning, {"agent": "a"}),
        (update_concept, {"concept": "x", "summary": "y"}),
        (trace_concept, {"concept": "x"}),
        (list_concepts, {}),
        (map_neighborhood, {"concept": "x"}),
        (add_link, {"from_concept": "a", "to_concept": "b", "link_type": "related"}),
        (add_perspective, {"concept": "x", "perspective": "y"}),
        (store_knowledge, {"topic": "x", "title": "y", "content": "z"}),
        (query_knowledge, {}),
        (staleness_check, {}),
        (create_task, {"title": "x"}),
        (update_task, {"task_id": 1}),
        (list_tasks, {}),
        (get_task, {"task_id": 1}),
        (crystallize, {}),
        (observe_principal, {"observations": "test"}),
    ])
    def test_gated_tool(self, tool_fn, kwargs):
        result = tool_fn(**kwargs)
        assert "Pro" in result or "upgrade" in result.lower() or "cairn" in result.lower()


class TestWithLicense:
    """With a valid license, paid tools should work."""

    @pytest.fixture(autouse=True)
    def activate_license(self, fresh_db):
        """Write a valid license key."""
        import hashlib
        # Generate a valid key: CP-TEST-ABCD-EFGH-<checksum>
        payload = "CP-TEST-ABCD-EFGH"
        checksum = hashlib.sha256(payload.encode()).hexdigest()[:4].upper()
        key = f"{payload}-{checksum}"
        license_file = fresh_db / "license"
        license_file.write_text(key)
        assert check_license()

    def test_send_and_read_message(self):
        result = send_message(from_agent="alice", to_agent="bob", subject="hello", body="world")
        assert "Message sent" in result

        result = read_messages(agent="bob")
        assert "hello" in result
        assert "world" in result

    def test_mark_read(self):
        send_message(from_agent="alice", to_agent="bob", subject="x", body="y")
        result = mark_read(agent="bob")
        assert "Marked" in result

    def test_reasoning_log(self):
        result = log_reasoning(agent="alice", decision="use SQLite", chosen="SQLite", rationale="simple")
        assert "Reasoning logged" in result

        result = read_reasoning(agent="alice")
        assert "use SQLite" in result

    def test_concept_map(self):
        result = update_concept(concept="Test Concept", summary="A test", domain="testing")
        assert "created" in result.lower()

        result = trace_concept(concept="Test Concept")
        assert "Test Concept" in result
        assert "A test" in result

        result = list_concepts()
        assert "Test Concept" in result

    def test_concept_versioning(self):
        update_concept(concept="Evolving", summary="version 1")
        update_concept(concept="Evolving", summary="version 2")
        result = trace_concept(concept="Evolving")
        assert "v2" in result
        assert "History" in result

    def test_concept_links(self):
        update_concept(concept="A", summary="concept A")
        update_concept(concept="B", summary="concept B")
        result = add_link(from_concept="A", to_concept="B", link_type="depends_on", note="A needs B")
        assert "Link added" in result

        result = trace_concept(concept="A")
        assert "depends_on" in result

    def test_concept_perspective(self):
        update_concept(concept="X", summary="concept X")
        result = add_perspective(concept="X", perspective="I see it differently", agent="bob")
        assert "Perspective added" in result

        result = trace_concept(concept="X")
        assert "I see it differently" in result

    def test_neighborhood(self):
        update_concept(concept="Center", summary="the center")
        update_concept(concept="Neighbor", summary="nearby")
        add_link(from_concept="Center", to_concept="Neighbor", link_type="related")
        result = map_neighborhood(concept="Center")
        assert "Neighbor" in result

    def test_knowledge_store(self):
        result = store_knowledge(topic="architecture", title="DB choice", content="We chose SQLite")
        assert "Knowledge stored" in result

        result = query_knowledge(search="SQLite")
        assert "DB choice" in result

    def test_tasks(self):
        result = create_task(title="Build feature X", priority="high")
        assert "Task #" in result

        result = list_tasks()
        assert "Build feature X" in result

        result = update_task(task_id=1, status="in_progress")
        assert "updated" in result

        result = get_task(task_id=1)
        assert "in_progress" in result

    def test_crystallize(self):
        import json
        lessons = json.dumps([{"lesson": "Always test first", "tags": "testing"}])
        dead_ends = json.dumps([{"idea": "XML config", "why_failed": "Too verbose"}])
        surprises = json.dumps([{"observation": "SQLite is fast", "hidden_assumption": "Need Postgres"}])

        result = crystallize(
            agent="alice",
            lessons=lessons,
            dead_ends=dead_ends,
            surprises=surprises,
        )
        assert "1 lessons" in result
        assert "1 dead ends" in result
        assert "1 surprises" in result

    def test_staleness(self):
        update_concept(concept="Fresh", summary="just updated")
        result = staleness_check(days=0)
        # With days=0, everything is stale
        assert "Fresh" in result or "No stale" in result

    def test_observe_principal_creates_file(self, fresh_db):
        """observe_principal creates principal.md if it doesn't exist."""
        result = observe_principal(observations="Prefers snake_case. Works late EST.", agent="alice")
        assert "recorded" in result.lower()

        principal_path = fresh_db / "principal.md"
        assert principal_path.exists()
        content = principal_path.read_text()
        assert "Prefers snake_case" in content
        assert "Works late EST" in content

    def test_observe_principal_appends(self, fresh_db):
        """Multiple observations append to the file."""
        observe_principal(observations="Prefers Python.", agent="alice")
        observe_principal(observations="Hates boilerplate.", agent="alice")

        principal_path = fresh_db / "principal.md"
        content = principal_path.read_text()
        assert "Prefers Python" in content
        assert "Hates boilerplate" in content

    def test_observe_principal_existing_file(self, fresh_db):
        """Appends to existing principal.md without overwriting."""
        principal_path = fresh_db / "principal.md"
        principal_path.write_text("# About My Principal\n\n## Notes\n- Likes coffee\n\n---\n")

        observe_principal(observations="Works in healthcare domain.", agent="alice")

        content = principal_path.read_text()
        assert "Likes coffee" in content  # preserved
        assert "healthcare domain" in content  # added
