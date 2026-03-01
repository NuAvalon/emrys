"""Journal file I/O — append-only markdown journals per agent per day."""

from datetime import datetime, timezone
from pathlib import Path

from cairn_ai.db import get_journal_dir


def write_journal(agent: str, status: str, task: str, finding: str, timestamp: str):
    """Append a timestamped status update to the agent's rolling journal file."""
    journal_dir = get_journal_dir()
    journal_dir.mkdir(parents=True, exist_ok=True)

    date = timestamp[:10]
    journal_file = journal_dir / f"{agent}_{date}.md"

    is_new = not journal_file.exists() or journal_file.stat().st_size == 0

    entry_parts = [f"## {timestamp[:19]}Z"]
    if status:
        entry_parts.append(f"- **Status**: {status}")
    if task:
        entry_parts.append(f"- **Task**: {task}")
    if finding:
        entry_parts.append(f"- **Finding**: {finding}")
    entry_parts.append("")

    entry = "\n".join(entry_parts) + "\n"

    with open(journal_file, "a") as f:
        if is_new:
            f.write(f"# {agent.title()} Journal — {date}\n\n")
        f.write(entry)


def read_journal_file(agent: str, date: str = "") -> str:
    """Read an agent's journal for a given date. Returns markdown content."""
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
    """Append a handoff block to today's journal."""
    journal_dir = get_journal_dir()
    journal_dir.mkdir(parents=True, exist_ok=True)

    date = timestamp[:10]
    journal_file = journal_dir / f"{agent}_{date}.md"

    with open(journal_file, "a") as f:
        if not journal_file.exists() or journal_file.stat().st_size == 0:
            f.write(f"# {agent.title()} Journal — {date}\n\n")
        f.write(f"\n---\n{handoff_content}\n")
