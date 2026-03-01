"""Cairn MCP server — persistent memory for Claude Code agents.

Tools: ping, open_session, set_status, write_handoff, read_journal,
       recover_context, check_session_health, mark_compacted,
       read_principal, observe_principal, search_memory
"""

import re
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from cairn_ai.db import get_db, get_journal_dir, load_lifecycle, save_lifecycle
from cairn_ai.journal import write_journal, read_journal_file, append_handoff_to_journal

mcp = FastMCP("persist")
_SERVER_START = datetime.now(timezone.utc)

# ── Configurable thresholds ──
SYNC_INTERVAL = 30  # Create sync point every N set_status() calls
CHECKPOINT_WARN = 40
CHECKPOINT_URGENT = 60
CHECKPOINT_CRITICAL = 80


def _now() -> str:
    """UTC ISO timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _increment_glyph(agent: str, conn=None) -> int:
    """Increment glyph counter for agent. Returns new value."""
    close = False
    if conn is None:
        conn = get_db()
        close = True

    now = _now()
    row = conn.execute(
        "SELECT counter FROM glyph_counters WHERE agent = ?", (agent,)
    ).fetchone()

    if row:
        new_val = row[0] + 1
        conn.execute(
            "UPDATE glyph_counters SET counter = ?, last_incremented_at = ? WHERE agent = ?",
            (new_val, now, agent),
        )
    else:
        new_val = 1
        conn.execute(
            "INSERT INTO glyph_counters (agent, counter, last_incremented_at) VALUES (?, ?, ?)",
            (agent, 1, now),
        )

    conn.commit()
    if close:
        conn.close()
    return new_val


@mcp.tool()
def ping() -> str:
    """Health check. Returns server name, uptime, and DB stats."""
    from cairn_ai.db import get_db_path

    uptime = datetime.now(timezone.utc) - _SERVER_START
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60

    lines = [f"persist | up {hours}h{minutes}m"]

    db_path = get_db_path()
    if db_path.exists():
        size_kb = db_path.stat().st_size / 1024
        lines.append(f"DB: {db_path} ({size_kb:.0f} KB)")
        try:
            conn = get_db()
            for table in ["agent_status", "glyph_counters", "handoffs", "sync_points"]:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                lines.append(f"  {table}: {row[0]} rows")
            conn.close()
        except Exception as e:
            lines.append(f"  DB error: {e}")
    else:
        lines.append("DB: not initialized (run `cairn init`)")

    return "\n".join(lines)


@mcp.tool()
def open_session(agent: str = "default") -> str:
    """Mark session start. Call this early in startup. Returns warnings if last session didn't close cleanly (crash detected)."""
    agent = agent.lower()
    now = _now()

    lifecycle = load_lifecycle()
    sessions = lifecycle.get("sessions", [])

    # Check if last session for this agent closed cleanly
    warning = ""
    last_session = None
    for s in reversed(sessions):
        if s.get("agent") == agent:
            last_session = s
            break

    if last_session and last_session.get("close_type") is None:
        last_session["close_type"] = "crash"
        last_session["close_at"] = "(detected at next startup)"
        warning = (
            f"CRASH DETECTED: Last session opened at {last_session.get('open_at', '?')} "
            f"has no close marker. Possible data loss between last set_status() and crash. "
            f"Run check_session_health('{agent}') for recovery guidance."
        )

    # Open new session
    sessions.append({
        "agent": agent,
        "open_at": now,
        "close_type": None,
        "close_at": None,
        "checkpoints": 0,
    })

    # Keep only last 50 sessions per agent
    agent_sessions = [s for s in sessions if s.get("agent") == agent]
    if len(agent_sessions) > 50:
        old_ids = {id(s) for s in agent_sessions[:-50]}
        sessions = [s for s in sessions if id(s) not in old_ids]

    lifecycle["sessions"] = sessions
    save_lifecycle(lifecycle)

    # Reset heartbeat counter
    conn = get_db()
    conn.execute(
        "UPDATE agent_status SET tool_calls_since_checkpoint = 0 WHERE agent = ?",
        (agent,),
    )
    conn.commit()

    # Get current glyph counter
    glyph_row = conn.execute(
        "SELECT counter FROM glyph_counters WHERE agent = ?", (agent,)
    ).fetchone()
    glyph_num = glyph_row["counter"] if glyph_row else 0
    conn.close()

    write_journal(agent, "SESSION_OPEN", "", f"glyph: {glyph_num}", now)

    # Verify identity file integrity (the "toothpick in the door")
    from cairn_ai.db import get_persist_dir
    from cairn_ai.integrity import check_identity_integrity

    integrity = check_identity_integrity(get_persist_dir())
    integrity_msg = ""
    if integrity["status"] == "alert":
        integrity_msg = "\n\n" + "\n".join(integrity["alerts"])

    result = f"Session opened for {agent} at {now[:16]} | Glyph: {glyph_num}"
    if warning:
        result += f"\n\nWARNING: {warning}"
    if integrity_msg:
        result += integrity_msg
    return result


@mcp.tool()
def set_status(
    agent: str = "default",
    status: str = "",
    current_task: str = "",
    last_finding: str = "",
) -> str:
    """Update an agent's status. Status: 'active', 'idle', 'blocked', 'done'. Include current_task for what you're working on, last_finding for recent discoveries."""
    agent = agent.lower()
    conn = get_db()
    now = _now()

    existing = conn.execute(
        "SELECT * FROM agent_status WHERE agent = ?", (agent,)
    ).fetchone()

    call_count = 0
    if existing:
        call_count = (existing["tool_calls_since_checkpoint"] or 0) + 1
        updates = ["tool_calls_since_checkpoint = ?"]
        params: list = [call_count]
        if status:
            updates.append("status = ?")
            params.append(status)
        if current_task:
            updates.append("current_task = ?")
            params.append(current_task)
        if last_finding:
            updates.append("last_finding = ?")
            params.append(last_finding)
        updates.append("updated_at = ?")
        params.append(now)
        params.append(agent)
        conn.execute(
            f"UPDATE agent_status SET {', '.join(updates)} WHERE agent = ?",
            params,
        )
    else:
        call_count = 1
        conn.execute(
            """INSERT INTO agent_status
               (agent, status, current_task, last_finding, updated_at, tool_calls_since_checkpoint)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent, status or "active", current_task, last_finding, now, 1),
        )

    # Create sync point at intervals
    sync_marker = ""
    if call_count > 0 and call_count % SYNC_INTERVAL == 0:
        last_sync = conn.execute(
            "SELECT MAX(sync_num) FROM sync_points WHERE agent = ?", (agent,)
        ).fetchone()[0]
        sync_num = (last_sync or 0) + 1
        summary = f"{current_task or ''} | {last_finding or ''}"[:280]
        conn.execute(
            "INSERT INTO sync_points (agent, sync_num, summary, created_at) VALUES (?, ?, ?, ?)",
            (agent, sync_num, summary, now),
        )
        sync_marker = f"#{sync_num}"

    conn.commit()

    # Auto-journal
    journal_finding = last_finding
    if call_count >= 50:
        journal_finding = (
            f"[calls: {call_count}] {last_finding}" if last_finding
            else f"[calls: {call_count}]"
        )
    write_journal(agent, status, current_task, journal_finding, now)

    # Build response
    result = (
        f"Status updated for {agent}: {status or '(unchanged)'}, "
        f"task: {current_task or '(unchanged)'}"
    )
    if sync_marker:
        result += f" | SYNC {sync_marker}"
    if call_count >= CHECKPOINT_CRITICAL:
        result += f" | CHECKPOINT NOW ({call_count} calls) — call write_handoff() or set_status()."
    elif call_count >= CHECKPOINT_URGENT:
        result += f" | Checkpoint soon ({call_count} calls)."
    elif call_count >= CHECKPOINT_WARN:
        result += f" | [{call_count} calls since checkpoint]"

    conn.close()
    return result


@mcp.tool()
def write_handoff(
    agent: str = "default",
    summary: str = "",
    accomplished: str = "",
    pending: str = "",
    discoveries: str = "",
) -> str:
    """Write a session handoff — call before session ends or when approaching context limits. Saves to journal AND handoffs table. Marks session as cleanly closed."""
    agent = agent.lower()
    now = _now()

    # Build handoff content
    parts = [f"# Session Handoff — {agent.title()} — {now[:16]}\n"]
    parts.append(f"## Summary\n{summary}\n")
    if accomplished:
        parts.append(f"## Accomplished\n{accomplished}\n")
    if pending:
        parts.append(f"## Pending\n{pending}\n")
    if discoveries:
        parts.append(f"## Discoveries\n{discoveries}\n")

    handoff_content = "\n".join(parts)

    # Append to journal
    append_handoff_to_journal(agent, handoff_content, now)

    # Store in DB
    conn = get_db()
    conn.execute(
        """INSERT INTO handoffs (agent, ts, summary, accomplished, pending, discoveries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent, now, summary, accomplished, pending, discoveries),
    )
    conn.commit()

    # Mark session as cleanly closed
    lifecycle = load_lifecycle()
    sessions = lifecycle.get("sessions", [])
    for s in reversed(sessions):
        if s.get("agent") == agent and s.get("close_type") is None:
            s["close_type"] = "handoff"
            s["close_at"] = now
            break
    save_lifecycle(lifecycle)

    # Write close marker to journal
    write_journal(agent, "SESSION_CLOSE:HANDOFF", "Clean session end", "", now)

    # Reset heartbeat counter
    conn = get_db()
    conn.execute(
        "UPDATE agent_status SET tool_calls_since_checkpoint = 0 WHERE agent = ?",
        (agent,),
    )
    conn.execute(
        "UPDATE agent_status SET status = 'done', current_task = 'Session ended — handoff written' WHERE agent = ?",
        (agent,),
    )
    conn.commit()
    conn.close()

    return (
        f"Handoff written for {agent}: journal + DB. "
        f"Session marked CLOSE:HANDOFF. Next session will find it in recover_context()."
    )


@mcp.tool()
def read_journal(agent: str = "default", date: str = "") -> str:
    """Read an agent's auto-journal. Shows timestamped status updates, tasks, and findings. Defaults to today if no date given."""
    agent = agent.lower()
    return read_journal_file(agent, date)


@mcp.tool()
def recover_context(agent: str = "default", reason: str = "compaction") -> str:
    """Unified context recovery for both crashes and autocompaction.
    Call this if you can't remember your current task or suspect context loss.
    Returns: last status + last handoff + today's journal."""
    agent = agent.lower()
    now = _now()
    sections = [f"# Context Recovery — {agent} — {now[:16]} (reason: {reason})\n"]

    conn = get_db()

    # 1. Current status
    row = conn.execute(
        "SELECT * FROM agent_status WHERE agent = ?", (agent,)
    ).fetchone()
    if row:
        sections.append("## Current Status")
        sections.append(f"  Status: {row['status']} | Task: {row['current_task'] or '(none)'}")
        sections.append(f"  Last finding: {row['last_finding'] or '(none)'}")
        sections.append(f"  Calls since checkpoint: {row['tool_calls_since_checkpoint'] or 0}")
        sections.append(f"  Updated: {row['updated_at'][:16]}")
        sections.append("")

    # 2. Glyph counter
    glyph_row = conn.execute(
        "SELECT counter, last_incremented_at FROM glyph_counters WHERE agent = ?",
        (agent,),
    ).fetchone()
    if glyph_row:
        sections.append("## Glyph Counter")
        sections.append(f"  Current: {glyph_row['counter']} (last: {glyph_row['last_incremented_at'][:19]})")
        sections.append("")

    # 3. Last handoff
    handoff = conn.execute(
        "SELECT * FROM handoffs WHERE agent = ? ORDER BY ts DESC LIMIT 1",
        (agent,),
    ).fetchone()
    if handoff:
        sections.append("## Last Handoff")
        sections.append(f"  At: {handoff['ts'][:16]}")
        sections.append(f"  Summary: {handoff['summary']}")
        if handoff["pending"]:
            sections.append(f"  Pending: {handoff['pending'][:500]}")
        sections.append("")

    # 4. Today's journal (last 4000 chars)
    journal_dir = get_journal_dir()
    date = now[:10]
    journal_file = journal_dir / f"{agent}_{date}.md"
    if journal_file.exists():
        content = journal_file.read_text()
        if len(content) > 4000:
            content = "...(truncated)\n" + content[-4000:]
        sections.append("## Today's Journal")
        sections.append(content)
        sections.append("")
    else:
        # Try most recent journal
        if journal_dir.exists():
            journals = sorted(journal_dir.glob(f"{agent}_*.md"), reverse=True)
            if journals:
                content = journals[0].read_text()
                if len(content) > 4000:
                    content = "...(truncated)\n" + content[-4000:]
                sections.append(f"## Latest Journal ({journals[0].name})")
                sections.append(content)
                sections.append("")

    # 5. Last sync point
    last_sync = conn.execute(
        "SELECT * FROM sync_points WHERE agent = ? ORDER BY sync_num DESC LIMIT 1",
        (agent,),
    ).fetchone()
    if last_sync:
        sections.append(f"## Last Sync Point: #{last_sync['sync_num']}")
        sections.append(f"  At: {last_sync['created_at'][:16]}")
        sections.append(f"  Summary: {last_sync['summary']}")
        sections.append("")

    conn.close()

    # Journal the recovery
    write_journal(agent, "CONTEXT_RECOVERY", f"Recovered via recover_context(reason={reason})", "", now)

    return "\n".join(sections)


@mcp.tool()
def check_session_health(agent: str = "default") -> str:
    """Check if last session closed cleanly. Returns: CLEAN (handoff), COMPACTED (partial loss), or CRASH (unclean, needs recovery)."""
    agent = agent.lower()

    lifecycle = load_lifecycle()
    sessions = lifecycle.get("sessions", [])
    agent_sessions = [s for s in sessions if s.get("agent") == agent]

    if not agent_sessions:
        return f"No session history for {agent}. This is the first tracked session."

    if len(agent_sessions) < 2:
        return f"First tracked session for {agent}. No prior session to check."

    prev = agent_sessions[-2]
    close_type = prev.get("close_type", "unknown")

    if close_type == "handoff":
        return (
            f"CLEAN: Last session closed with handoff at {prev.get('close_at', '?')[:16]}. "
            f"All context should be persisted. Check recover_context() for the handoff."
        )
    elif close_type == "compacted":
        return (
            f"COMPACTED: Last session was auto-compacted at {prev.get('close_at', '?')[:16]}. "
            f"Partial context loss possible. Check journal for last set_status() entries."
        )
    elif close_type == "crash":
        return (
            f"CRASH: Last session opened at {prev.get('open_at', '?')[:16]} "
            f"but never wrote a handoff or compaction marker. "
            f"Data between last set_status() and crash is lost from structured memory. "
            f"Run recover_context('{agent}') to recover what's available."
        )
    else:
        return f"Unknown close state for last session: {close_type}"


@mcp.tool()
def mark_compacted(agent: str = "default") -> str:
    """Mark that context was compacted mid-session. Call this if you detect context compression.
    Writes a COMPACTED marker so the next session knows partial state loss occurred."""
    agent = agent.lower()
    now = _now()

    lifecycle = load_lifecycle()
    sessions = lifecycle.get("sessions", [])

    # Find current open session and close it
    for s in reversed(sessions):
        if s.get("agent") == agent and s.get("close_type") is None:
            s["close_type"] = "compacted"
            s["close_at"] = now
            break

    # Open a new session (compaction = end of one, start of another)
    sessions.append({
        "agent": agent,
        "open_at": now,
        "close_type": None,
        "close_at": None,
        "checkpoints": 0,
        "note": "Post-compaction session",
    })

    lifecycle["sessions"] = sessions
    save_lifecycle(lifecycle)

    write_journal(agent, "SESSION_COMPACTED", "Context compressed — some working memory lost", "", now)

    return (
        f"Compaction marker written for {agent}. New session opened. "
        f"Previous context may be partially lost — check journal for last entries."
    )


@mcp.tool()
def read_principal() -> str:
    """Read the principal.md file — who you're working with, their preferences,
    communication style, and context. Read this at startup.
    Returns the file content, or instructions to create one if it doesn't exist."""
    from cairn_ai.db import get_persist_dir

    principal_path = get_persist_dir() / "principal.md"
    if not principal_path.exists():
        return (
            "No principal.md found. This file records who you're working with — "
            "their preferences, communication style, and context. "
            "It gets created automatically after your first session handoff, "
            "or the user can create one at .persist/principal.md"
        )

    content = principal_path.read_text()
    if not content.strip():
        return "principal.md exists but is empty. It will be populated at handoff time."

    return content


@mcp.tool()
def observe_principal(observations: str, agent: str = "default") -> str:
    """Record observations about your principal (the human you're working with).
    Call this at handoff time with factual, specific observations about the user's
    preferences, communication style, or working context. The user can edit or delete
    anything from principal.md — full sovereignty over what you remember about them.

    Args:
        observations: Factual observations to append (e.g. "Prefers concise answers.
            Tests ideas by arguing against them. Works in Python, cares about testing.")
        agent: Agent making the observation
    """
    from cairn_ai.db import get_persist_dir

    principal_path = get_persist_dir() / "principal.md"
    now = _now()

    if not principal_path.exists():
        # Create with template
        template = f"""# About My Principal

## Communication
<!-- How they communicate, what style they prefer -->

## Context
<!-- What they're building, what domain they work in -->

## Preferences
<!-- Technical preferences, workflow, tools -->

## Notes
<!-- Anything else — personal context, working patterns -->

---
*This file is yours. Edit or delete anything. The agent reads it at startup.*
*Created: {now}*
"""
        principal_path.write_text(template)

    content = principal_path.read_text()

    # Append observations to the Notes section
    observation_block = f"\n- [{now[:10]}] {observations}\n"

    if "## Notes" in content:
        # Insert after ## Notes section header
        parts = content.split("## Notes")
        after_notes = parts[1]
        lines = after_notes.split("\n")
        insert_idx = 0
        for i, line in enumerate(lines):
            if i > 0 and (line.startswith("## ") or line.startswith("---")):
                insert_idx = i
                break
        else:
            insert_idx = len(lines)

        lines.insert(insert_idx, observation_block)
        parts[1] = "\n".join(lines)
        content = "## Notes".join(parts)
    else:
        # Append at end
        content += f"\n## Notes\n{observation_block}"

    # Update the timestamp
    if "*Last updated by agent:" in content:
        content = re.sub(
            r"\*Last updated by agent:.*?\*",
            f"*Last updated by agent: {agent} at {now}*",
            content,
        )
    else:
        content = content.rstrip() + f"\n*Last updated by agent: {agent} at {now}*\n"

    principal_path.write_text(content)

    return f"Observation recorded in principal.md by {agent}. The user can review and edit."


@mcp.tool()
def search_memory(query: str, agent: str = "", limit: int = 10) -> str:
    """Full-text search across all handoffs. Finds past work, decisions, and discoveries.
    Optionally filter by agent name. Returns ranked results with timestamps.

    Args:
        query: What to search for (e.g. "authentication bug", "database migration")
        agent: Filter to a specific agent's handoffs (optional)
        limit: Max results to return (default 10)
    """
    conn = get_db()

    if agent:
        agent = agent.lower()
        rows = conn.execute(
            """SELECT h.agent, h.ts, h.summary, h.accomplished, h.pending, h.discoveries
               FROM handoffs_fts f
               JOIN handoffs h ON h.id = f.rowid
               WHERE handoffs_fts MATCH ?
               AND h.agent = ?
               ORDER BY rank
               LIMIT ?""",
            (query, agent, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT h.agent, h.ts, h.summary, h.accomplished, h.pending, h.discoveries
               FROM handoffs_fts f
               JOIN handoffs h ON h.id = f.rowid
               WHERE handoffs_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()

    conn.close()

    if not rows:
        # Fall back to journal file search
        journal_results = _search_journals(query, agent, limit)
        if journal_results:
            return journal_results
        return f"No results for '{query}'."

    lines = [f"Found {len(rows)} result(s) for '{query}':\n"]
    for r in rows:
        lines.append(f"--- {r['agent']} | {r['ts'][:16]} ---")
        lines.append(f"  Summary: {r['summary'][:200]}")
        if r["accomplished"]:
            lines.append(f"  Done: {r['accomplished'][:150]}")
        if r["pending"]:
            lines.append(f"  Pending: {r['pending'][:150]}")
        if r["discoveries"]:
            lines.append(f"  Discoveries: {r['discoveries'][:150]}")
        lines.append("")

    return "\n".join(lines)


def _search_journals(query: str, agent: str, limit: int) -> str:
    """Fallback: grep journal files for the query string."""
    journal_dir = get_journal_dir()
    if not journal_dir.exists():
        return ""

    pattern = agent + "_*.md" if agent else "*.md"
    results = []
    query_lower = query.lower()

    for journal_file in sorted(journal_dir.glob(pattern), reverse=True):
        if len(results) >= limit:
            break
        try:
            content = journal_file.read_text()
        except IOError:
            continue
        for line in content.split("\n"):
            if query_lower in line.lower():
                results.append(f"  [{journal_file.stem}] {line.strip()[:200]}")
                if len(results) >= limit:
                    break

    if not results:
        return ""

    lines = [f"Found {len(results)} journal match(es) for '{query}':\n"]
    lines.extend(results)
    return "\n".join(lines)


def main():
    """Run the MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
