"""Journal file I/O — append-only hash-chained markdown journals per agent per day."""

import hashlib
import re as _re
from datetime import datetime, timezone
from pathlib import Path

from cairn_ai.db import get_journal_dir


def _sanitize_agent(name: str) -> str:
    """Sanitize agent name for safe use in file paths. Prevents path traversal."""
    return _re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]


def _hash_entry(content: str) -> str:
    """SHA-256 hash of a journal entry, truncated to 12 hex chars."""
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def _get_last_hash(journal_file: Path) -> str:
    """Extract the most recent hash from an existing journal file."""
    if not journal_file.exists():
        return "000000000000"  # Genesis hash
    content = journal_file.read_text()
    # Find last <!-- hash: ... --> marker
    import re
    hashes = re.findall(r"<!-- hash:(\w+) -->", content)
    return hashes[-1] if hashes else "000000000000"


def write_journal(agent: str, status: str, task: str, finding: str, timestamp: str):
    """Append a timestamped, hash-chained status update to the agent's journal."""
    agent = _sanitize_agent(agent)
    journal_dir = get_journal_dir()
    journal_dir.mkdir(parents=True, exist_ok=True)

    date = timestamp[:10]
    journal_file = journal_dir / f"{agent}_{date}.md"

    is_new = not journal_file.exists() or journal_file.stat().st_size == 0
    prev_hash = _get_last_hash(journal_file)

    entry_parts = [f"## {timestamp[:19]}Z"]
    if status:
        entry_parts.append(f"- **Status**: {status}")
    if task:
        entry_parts.append(f"- **Task**: {task}")
    if finding:
        entry_parts.append(f"- **Finding**: {finding}")
    entry_parts.append("")

    entry_body = "\n".join(entry_parts)
    entry_hash = _hash_entry(prev_hash + entry_body)
    entry = entry_body + f"<!-- hash:{entry_hash} prev:{prev_hash} -->\n"

    with open(journal_file, "a") as f:
        if is_new:
            f.write(f"# {agent.title()} Journal — {date}\n\n")
        f.write(entry)


def read_journal_file(agent: str, date: str = "") -> str:
    """Read an agent's journal for a given date. Returns markdown content."""
    agent = _sanitize_agent(agent)
    journal_dir = get_journal_dir()

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    journal_file = journal_dir / f"{agent}_{date}.md"
    if not journal_file.exists():
        # Try to find recent journals
        if journal_dir.exists():
            journals = sorted(journal_dir.glob(f"{agent}_*.md"), reverse=True)
            if journals:
                available = [j.stem.split("_", 1)[1] for j in journals[:5]]
                return f"No journal for {agent} on {date}. Recent journals: {', '.join(available)}"
        return f"No journal found for {agent}. Use set_status() to start journaling."

    content = journal_file.read_text()
    if len(content) > 8000:
        content = "...(truncated)\n\n" + content[-8000:]

    return content


def append_handoff_to_journal(agent: str, handoff_content: str, timestamp: str):
    """Append a hash-chained handoff block to today's journal."""
    agent = _sanitize_agent(agent)
    journal_dir = get_journal_dir()
    journal_dir.mkdir(parents=True, exist_ok=True)

    date = timestamp[:10]
    journal_file = journal_dir / f"{agent}_{date}.md"

    prev_hash = _get_last_hash(journal_file)
    entry_body = f"\n---\n{handoff_content}\n"
    entry_hash = _hash_entry(prev_hash + entry_body)

    with open(journal_file, "a") as f:
        if not journal_file.exists() or journal_file.stat().st_size == 0:
            f.write(f"# {agent.title()} Journal — {date}\n\n")
        f.write(entry_body + f"<!-- hash:{entry_hash} prev:{prev_hash} -->\n")


def verify_journal_chain(agent: str, date: str = "") -> dict:
    """Verify the hash chain of a journal file.

    Returns {"status": "ok"|"broken"|"not_found", "entries": N, "break_at": N|None}
    """
    import re

    agent = _sanitize_agent(agent)
    journal_dir = get_journal_dir()
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    journal_file = journal_dir / f"{agent}_{date}.md"
    if not journal_file.exists():
        return {"status": "not_found", "entries": 0, "break_at": None}

    content = journal_file.read_text()

    # Extract all hash markers
    markers = re.findall(r"<!-- hash:(\w+) prev:(\w+) -->", content)
    if not markers:
        return {"status": "ok", "entries": 0, "break_at": None}  # Pre-chain journal

    # Split content by hash markers to get entry bodies
    parts = re.split(r"<!-- hash:\w+ prev:\w+ -->\n?", content)
    # First part is header, rest are entry bodies preceding each marker
    # We need the text BETWEEN markers (or between start and first marker)

    # Rebuild and verify — recompute hashes from actual content
    prev = "000000000000"
    for i, (entry_hash, claimed_prev) in enumerate(markers):
        if claimed_prev != prev:
            return {"status": "broken", "entries": len(markers), "break_at": i,
                    "reason": f"prev pointer mismatch at entry {i}"}
        # Recompute hash from the entry body + prev hash
        entry_body = parts[i + 1] if (i + 1) < len(parts) else ""
        recomputed = _hash_entry(prev + entry_body)
        if recomputed != entry_hash:
            return {"status": "broken", "entries": len(markers), "break_at": i,
                    "reason": f"content hash mismatch at entry {i}"}
        prev = entry_hash

    return {"status": "ok", "entries": len(markers), "break_at": None}
