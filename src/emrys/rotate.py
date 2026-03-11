"""Journal rotation — archive old journals, extract findings into knowledge table."""

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from emrys.db import get_db, get_journal_dir


def rotate_journals(agent: str = "", days: int = 7, dry_run: bool = True) -> str:
    """Rotate old journal files. Extracts key findings into knowledge table,
    then moves raw journals to archive/ (cold storage, never deleted).

    Args:
        agent: Filter to specific agent (empty = all agents)
        days: Keep journals newer than this many days (default 7)
        dry_run: If True (default), preview what would happen. Set False to execute.

    Returns:
        Summary of what was (or would be) rotated.
    """
    journal_dir = get_journal_dir()
    if not journal_dir.exists():
        return "No journals directory found."

    archive_dir = journal_dir / "archive"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    # Find journals older than cutoff
    pattern = f"{agent}_*.md" if agent else "*.md"
    old_journals = []
    for jf in sorted(journal_dir.glob(pattern)):
        if jf.parent != journal_dir:
            continue  # Skip archive/
        # Extract date from filename: agent_YYYY-MM-DD.md
        parts = jf.stem.rsplit("_", 1)
        if len(parts) != 2:
            continue
        file_date = parts[1]
        if file_date < cutoff_str:
            old_journals.append(jf)

    if not old_journals:
        return f"No journals older than {days} days to rotate."

    lines = []
    extracted_count = 0

    for jf in old_journals:
        content = jf.read_text()
        findings = _extract_findings(content, jf.stem)

        if dry_run:
            lines.append(f"  Would archive: {jf.name} ({len(content)} chars, {len(findings)} findings)")
            for f in findings:
                lines.append(f"    → {f['title'][:80]}")
        else:
            # Store findings in knowledge table
            if findings:
                conn = get_db()
                for f in findings:
                    conn.execute(
                        """INSERT INTO knowledge (agent, ts, title, content, tags, source)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (f["agent"], f["ts"], f["title"], f["content"],
                         f["tags"], f"journal:{jf.name}"),
                    )
                conn.commit()
                conn.close()
                extracted_count += len(findings)

            # Move to archive
            archive_dir.mkdir(exist_ok=True)
            jf.rename(archive_dir / jf.name)
            lines.append(f"  Archived: {jf.name} ({len(findings)} findings extracted)")

    prefix = "DRY RUN — " if dry_run else ""
    header = f"{prefix}Journal rotation: {len(old_journals)} file(s), {days}-day retention\n"
    if not dry_run:
        header += f"Extracted {extracted_count} findings into knowledge table.\n"
    return header + "\n".join(lines)


def _extract_findings(content: str, file_stem: str) -> list[dict]:
    """Extract notable findings from a journal file.

    Looks for:
    - Lines with **Finding**: ... (set_status findings)
    - Handoff discoveries sections
    - Session summaries from handoffs
    """
    # Parse agent name from filename
    parts = file_stem.rsplit("_", 1)
    agent = parts[0] if len(parts) == 2 else "default"
    file_date = parts[1] if len(parts) == 2 else ""

    findings = []

    # Extract findings from journal entries
    finding_pattern = re.compile(
        r"## (\d{4}-\d{2}-\d{2}T[\d:]+)Z?\n.*?- \*\*Finding\*\*: (.+?)(?=\n(?:## |<!-- |$))",
        re.DOTALL,
    )
    for match in finding_pattern.finditer(content):
        ts = match.group(1)
        finding_text = match.group(2).strip()
        # Skip trivial findings
        if len(finding_text) < 20 or finding_text.startswith("glyph:"):
            continue
        findings.append({
            "agent": agent,
            "ts": ts,
            "title": finding_text[:120],
            "content": finding_text,
            "tags": "extracted,journal",
        })

    # Extract handoff summaries
    handoff_pattern = re.compile(
        r"# Session Handoff.*?\n\n## Summary\n(.+?)(?=\n## |\n---|\Z)",
        re.DOTALL,
    )
    for match in handoff_pattern.finditer(content):
        summary = match.group(1).strip()
        if len(summary) > 20:
            findings.append({
                "agent": agent,
                "ts": file_date,
                "title": f"Session summary: {summary[:100]}",
                "content": summary,
                "tags": "extracted,handoff-summary",
            })

    # Extract handoff discoveries
    discovery_pattern = re.compile(
        r"## Discoveries\n(.+?)(?=\n## |\n---|\Z)",
        re.DOTALL,
    )
    for match in discovery_pattern.finditer(content):
        disc = match.group(1).strip()
        if len(disc) > 20:
            findings.append({
                "agent": agent,
                "ts": file_date,
                "title": f"Discovery: {disc[:100]}",
                "content": disc,
                "tags": "extracted,discovery",
            })

    return findings
