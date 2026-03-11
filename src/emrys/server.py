"""Emrys MCP server — persistent memory for AI coding agents.

Tools: ping, open_session, set_status, write_handoff, read_journal,
       recover_context, check_session_health, mark_compacted,
       read_principal, observe_principal, search_memory, recall,
       store_knowledge, batch_store_knowledge, update_knowledge,
       delete_knowledge, list_knowledge
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

log = logging.getLogger("emrys")

from emrys.db import get_db, get_journal_dir, get_persist_dir, load_lifecycle, save_lifecycle
from emrys.journal import write_journal, read_journal_file, append_handoff_to_journal

mcp = FastMCP("emrys")
_SERVER_START = datetime.now(timezone.utc)

# ── Configurable thresholds ──
SYNC_INTERVAL = 30  # Create sync point every N set_status() calls
CHECKPOINT_WARN = 40
CHECKPOINT_URGENT = 60
CHECKPOINT_CRITICAL = 80


def _now() -> str:
    """UTC ISO timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_agent(agent: str) -> str:
    """Resolve agent name — use stored name if agent is 'default' or empty."""
    if agent and agent.lower() != "default":
        return agent.lower()
    try:
        from emrys.backup import get_config
        config = get_config()
        stored = config.get("agent_name", "")
        if stored:
            return stored.lower()
    except Exception as e:
        log.debug("Could not resolve stored agent name: %s", e)
    return "default"


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
    from emrys.db import get_db_path, EXPECTED_TABLES, verify_schema

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
            missing = verify_schema(conn)
            if missing:
                lines.append(f"  ⚠️ MISSING TABLES: {', '.join(missing)}")
            for table in EXPECTED_TABLES:
                if table not in missing:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    lines.append(f"  {table}: {row[0]} rows")
            conn.close()
        except Exception as e:
            lines.append(f"  DB error: {e}")
    else:
        lines.append("DB: not initialized (run `emrys init`)")

    return "\n".join(lines)


@mcp.tool()
def set_name(name: str) -> str:
    """Store your name. Call this when your principal gives you a name.

    Before a name is set, all tools default to 'default'. Once set,
    your name is used automatically — no need to pass agent='name'
    on every call.

    This is a one-time operation. Your name persists across sessions
    and reinstalls (stored in .persist/config.json).

    Args:
        name: Your name (e.g. 'Flint', 'Sage', 'Echo')
    """
    name = name.strip().lower()
    if not name or name == "default":
        return "Name cannot be empty or 'default'."

    from emrys.backup import get_config, save_config

    config = get_config()
    old_name = config.get("agent_name", "")
    config["agent_name"] = name
    save_config(config)

    if old_name and old_name != name:
        return f"Name changed: {old_name} → {name}. New entries will use '{name}'. Old entries remain under '{old_name}'."
    return f"Name set: {name}. All tools will use '{name}' by default from now on."


@mcp.tool()
def open_session(agent: str = "default") -> str:
    """Mark session start. Call this early in startup. Returns warnings if last session didn't close cleanly (crash detected)."""
    agent = _resolve_agent(agent)
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
    from emrys.db import get_persist_dir
    from emrys.integrity import check_identity_integrity

    integrity = check_identity_integrity(get_persist_dir())
    integrity_msg = ""
    if integrity["status"] == "alert":
        integrity_msg = "\n\n" + "\n".join(integrity["alerts"])

    # Verify journal hash chain (tamper detection)
    from emrys.journal import verify_journal_chain
    chain_msg = ""
    try:
        chain = verify_journal_chain(agent)
        if chain["status"] == "broken":
            chain_msg = (
                f"\n\nJOURNAL INTEGRITY: Hash chain BROKEN at entry {chain['break_at']} "
                f"({chain.get('reason', 'unknown')}). "
                f"Journal may have been tampered with or corrupted."
            )
    except Exception as e:
        log.warning("Journal chain verification failed: %s", e)

    # Auth gate — the firing pin (PQ identity check)
    auth_msg = ""
    try:
        from emrys.pq_identity import auth_gate
        gate = auth_gate(agent, get_persist_dir())
        mode = gate["mode"].value
        auth_msg = f"\n\nIDENTITY: {mode.upper()} — {gate['reason']}"
        if gate.get("agent_fingerprint"):
            auth_msg += f"\n  PQ fingerprint: {gate['agent_fingerprint']}"
        if gate.get("principal_fingerprint"):
            auth_msg += f"\n  Principal: {gate['principal_fingerprint']}"
    except ImportError:
        pass  # pqcrypto not installed — sovereign features optional
    except Exception as e:
        log.debug("Auth gate check skipped: %s", e)

    # Check backup status
    backup_msg = ""
    try:
        from emrys.backup import get_backup_dir, list_backups

        if get_backup_dir() is None:
            backup_msg = "\n\nBACKUP: No backup directory configured. Your agent's memory is not backed up. Ask your principal to run `emrys backup --dir /path` to set one up."
        else:
            backups = list_backups()
            if not backups:
                backup_msg = "\n\nBACKUP: Backup directory configured but no backups exist yet. Consider asking your principal to run `emrys backup`."
    except Exception as e:
        log.debug("Backup check skipped: %s", e)

    result = f"Session opened for {agent} at {now[:16]} | Glyph: {glyph_num}"
    if warning:
        result += f"\n\nWARNING: {warning}"
    if auth_msg:
        result += auth_msg
    if integrity_msg:
        result += integrity_msg
    if chain_msg:
        result += chain_msg
    if backup_msg:
        result += backup_msg
    return result


@mcp.tool()
def set_status(
    agent: str = "default",
    status: str = "",
    current_task: str = "",
    last_finding: str = "",
) -> str:
    """Update an agent's status. Status: 'active', 'idle', 'blocked', 'done'. Include current_task for what you're working on, last_finding for recent discoveries."""
    agent = _resolve_agent(agent)
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
        glyph_num = _increment_glyph(agent, conn)
        sync_marker = f"#{sync_num} (glyph:{glyph_num})"

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
    agent = _resolve_agent(agent)
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

    # Store in DB + increment glyph
    conn = get_db()
    conn.execute(
        """INSERT INTO handoffs (agent, ts, summary, accomplished, pending, discoveries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent, now, summary, accomplished, pending, discoveries),
    )
    glyph_num = _increment_glyph(agent, conn)
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
        f"Handoff written for {agent}: journal + DB. Glyph: {glyph_num}. "
        f"Session marked CLOSE:HANDOFF. Next session will find it in recover_context()."
    )


@mcp.tool()
def read_journal(agent: str = "default", date: str = "") -> str:
    """Read an agent's auto-journal. Shows timestamped status updates, tasks, and findings. Defaults to today if no date given."""
    agent = _resolve_agent(agent)
    return read_journal_file(agent, date)


@mcp.tool()
def recover_context(agent: str = "default", reason: str = "compaction") -> str:
    """Unified context recovery for both crashes and autocompaction.
    Call this if you can't remember your current task or suspect context loss.
    Returns: last status + last handoff + today's journal."""
    agent = _resolve_agent(agent)
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
    agent = _resolve_agent(agent)

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
    agent = _resolve_agent(agent)
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
    """Read the principal.md file — the human's preferences, customization,
    and working context. Read this at startup.
    This file is human-owned. The agent reads it but does not track changes
    or log diffs. In tool mode, treat it as a read-only settings file.
    Returns the file content, or instructions to create one if it doesn't exist."""
    from emrys.db import get_persist_dir

    principal_path = get_persist_dir() / "principal.md"
    if not principal_path.exists():
        return (
            "No principal.md found. This file holds the human's preferences "
            "and customization. Create one at .persist/principal.md or "
            "run 'emrys init' to generate a template."
        )

    content = principal_path.read_text()
    if not content.strip():
        return "principal.md exists but is empty. It will be populated at handoff time."

    return content


@mcp.tool()
def observe_principal(observations: str, agent: str = "default") -> str:
    """Record observations about your principal (the human you're working with).
    Only available in 'more' mode — tool mode treats principal.md as a read-only
    settings file with no agent observation. Call at handoff time with factual,
    specific observations about the user's preferences or working context.
    The user can edit or delete anything from principal.md.

    Args:
        observations: Factual observations to append (e.g. "Prefers concise answers.
            Tests ideas by arguing against them. Works in Python, cares about testing.")
        agent: Agent making the observation
    """
    from emrys.backup import get_config
    from emrys.db import get_persist_dir

    config = get_config()
    if config.get("mode", "tool") == "tool":
        return (
            "observe_principal is not available in tool mode. "
            "principal.md is a read-only settings file in tool mode — "
            "only the human edits it. Use 'emrys init --mode more' to enable agent observation."
        )

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
    """Full-text search across all handoffs and journal entries.
    Finds past work, decisions, discoveries, and status updates.
    Optionally filter by agent name. Returns ranked results with timestamps.

    Args:
        query: What to search for (e.g. "authentication bug", "database migration")
        agent: Filter to a specific agent's results (optional)
        limit: Max results to return (default 10)
    """
    conn = get_db()
    lines = []
    total = 0

    # Search handoffs
    if agent:
        agent = _resolve_agent(agent)
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

    # Search journal entries
    journal_limit = max(1, limit - len(rows))
    if agent:
        jrows = conn.execute(
            """SELECT j.agent, j.ts, j.status, j.task, j.finding
               FROM journal_fts f
               JOIN journal_entries j ON j.id = f.rowid
               WHERE journal_fts MATCH ?
               AND j.agent = ?
               ORDER BY rank
               LIMIT ?""",
            (query, agent, journal_limit),
        ).fetchall()
    else:
        jrows = conn.execute(
            """SELECT j.agent, j.ts, j.status, j.task, j.finding
               FROM journal_fts f
               JOIN journal_entries j ON j.id = f.rowid
               WHERE journal_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, journal_limit),
        ).fetchall()

    conn.close()

    if not rows and not jrows:
        return f"No results for '{query}'."

    total = len(rows) + len(jrows)
    lines = [f"Found {total} result(s) for '{query}':\n"]
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

    if jrows:
        lines.append("--- Journal entries ---")
        for j in jrows:
            line = f"  [{j['agent']} {j['ts'][:16]}]"
            if j["task"]:
                line += f" Task: {j['task'][:150]}"
            if j["finding"]:
                line += f" | Finding: {j['finding'][:150]}"
            lines.append(line)

    return "\n".join(lines)


@mcp.tool()
def recall(query: str, agent: str = "", tags: str = "", limit: int = 10) -> str:
    """Search the knowledge base — extracted findings, ingested transcripts, archived discoveries.
    This is your long-term memory. Use it to find past decisions, discoveries, and learnings.

    Args:
        query: What to search for (e.g. "authentication", "the bug we fixed last week")
        agent: Filter to a specific agent's knowledge (optional)
        tags: Filter by tags, comma-separated (e.g. "discovery,commit")
        limit: Max results to return (default 10)
    """
    conn = get_db()
    lines = []

    # FTS search
    if agent:
        agent = _resolve_agent(agent)
    try:
        if agent:
            rows = conn.execute(
                """SELECT k.agent, k.created_at, k.title, k.content, k.tags, k.source
                   FROM knowledge_fts f
                   JOIN knowledge k ON k.id = f.rowid
                   WHERE knowledge_fts MATCH ?
                   AND k.agent = ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, agent, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT k.agent, k.created_at, k.title, k.content, k.tags, k.source
                   FROM knowledge_fts f
                   JOIN knowledge k ON k.id = f.rowid
                   WHERE knowledge_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
    except Exception as e:
        log.warning("Knowledge FTS search failed: %s", e)
        rows = []

    # Optional tag filter
    if tags and rows:
        tag_set = {t.strip().lower() for t in tags.split(",")}
        rows = [r for r in rows if tag_set & {t.strip().lower() for t in r["tags"].split(",")}]

    conn.close()

    if not rows:
        return f"No knowledge entries found for '{query}'."

    lines = [f"Found {len(rows)} knowledge entry/entries for '{query}':\n"]
    for r in rows:
        lines.append(f"--- {r['agent']} | {r['created_at'][:16]} | {r['tags']} ---")
        lines.append(f"  {r['title']}")
        content = r["content"]
        # Check for artifact reference
        artifact_ref = ""
        if "[Full content: artifacts/" in content:
            artifact_ref = " (has artifact — use read_artifact() for full text)"
        content_preview = content[:300]
        if len(content) > 300:
            content_preview += "..."
        lines.append(f"  {content_preview}")
        if artifact_ref:
            lines.append(f"  {artifact_ref}")
        if r["source"]:
            lines.append(f"  Source: {r['source']}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def store_knowledge(
    title: str,
    content: str,
    tags: str = "",
    topic: str = "general",
    source: str = "",
    agent: str = "default",
) -> str:
    """Store a knowledge entry — a finding, decision, learning, or extracted insight.
    This is how you build long-term memory. Use recall() or vector_search() to retrieve later.

    Args:
        title: Short descriptive title (e.g. "Auth bug root cause", "DB migration pattern")
        content: The knowledge content — be specific and detailed
        tags: Comma-separated tags for filtering (e.g. "bug,auth,resolved")
        topic: Category/topic (e.g. "architecture", "debugging", "decisions")
        source: Where this came from (e.g. "session 12", "PR #45", "user request")
        agent: Agent storing this (resolved automatically if name is set)
    """
    agent = _resolve_agent(agent)
    now = _now()

    if not title.strip() or not content.strip():
        return "Both title and content are required."

    conn = get_db()

    # Store large content as artifact
    artifact_ref = ""
    if len(content) > 5000:
        import hashlib

        artifacts_dir = get_persist_dir() / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        artifact_name = f"{content_hash}.md"
        (artifacts_dir / artifact_name).write_text(content)
        stored_content = content[:500] + f"\n\n[Full content: artifacts/{artifact_name}]"
        artifact_ref = f" (full text in artifacts/{artifact_name})"
    else:
        stored_content = content

    cursor = conn.execute(
        """INSERT INTO knowledge (agent, topic, title, content, tags, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (agent, topic.strip(), title.strip(), stored_content, tags.strip(), source.strip(), now),
    )
    entry_id = cursor.lastrowid
    conn.commit()

    # Auto-embed if vectors available
    embed_msg = ""
    emb = _embed_text(f"{title} {content[:2000]}")
    if emb:
        conn.execute(
            "INSERT OR REPLACE INTO knowledge_vectors (knowledge_id, embedding, model) VALUES (?, ?, ?)",
            (entry_id, emb, "all-MiniLM-L6-v2"),
        )
        conn.commit()
        embed_msg = " + vector embedded"

    conn.close()

    return f"Stored knowledge #{entry_id}: '{title}' (topic: {topic}, tags: {tags}){artifact_ref}{embed_msg}"


@mcp.tool()
def batch_store_knowledge(entries: str, agent: str = "default") -> str:
    """Store multiple knowledge entries at once. For bulk ingestion (transcripts, notes, imports).

    Args:
        entries: JSON array of objects, each with: title (required), content (required),
                 tags (optional), topic (optional), source (optional).
                 Example: [{"title": "Finding 1", "content": "Details...", "tags": "bug", "topic": "debug"}]
        agent: Agent storing these (resolved automatically if name is set)
    """
    import json as _json

    agent = _resolve_agent(agent)
    now = _now()

    try:
        items = _json.loads(entries)
    except _json.JSONDecodeError as e:
        return f"Invalid JSON: {e}. Pass a JSON array of objects with 'title' and 'content' fields."

    if not isinstance(items, list):
        return "Expected a JSON array of objects."

    if not items:
        return "Empty array — nothing to store."

    conn = get_db()
    ids = []
    skipped = 0
    embedder_available = _get_embedder() is not None

    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        title = item.get("title", "").strip()
        content = item.get("content", "").strip()
        if not title or not content:
            skipped += 1
            continue

        tags = item.get("tags", "").strip()
        topic = item.get("topic", "general").strip()
        source = item.get("source", "").strip()

        cursor = conn.execute(
            """INSERT INTO knowledge (agent, topic, title, content, tags, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent, topic, title, content, tags, source, now),
        )
        entry_id = cursor.lastrowid
        ids.append(entry_id)

        if embedder_available:
            emb = _embed_text(f"{title} {content[:2000]}")
            if emb:
                conn.execute(
                    "INSERT OR REPLACE INTO knowledge_vectors (knowledge_id, embedding, model) VALUES (?, ?, ?)",
                    (entry_id, emb, "all-MiniLM-L6-v2"),
                )

    conn.commit()
    conn.close()

    result = f"Stored {len(ids)} knowledge entries (IDs: {ids[0]}–{ids[-1]})"
    if skipped:
        result += f", skipped {skipped} invalid"
    if embedder_available:
        result += " + vectors embedded"
    return result


@mcp.tool()
def update_knowledge(
    knowledge_id: int,
    title: str = "",
    content: str = "",
    tags: str = "",
    topic: str = "",
) -> str:
    """Update an existing knowledge entry. Only provided fields are changed.

    Args:
        knowledge_id: The ID of the entry to update (from store_knowledge or list_knowledge)
        title: New title (leave empty to keep current)
        content: New content (leave empty to keep current)
        tags: New tags (leave empty to keep current)
        topic: New topic (leave empty to keep current)
    """
    conn = get_db()

    existing = conn.execute(
        "SELECT * FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()

    if not existing:
        conn.close()
        return f"Knowledge entry #{knowledge_id} not found."

    updates = []
    params = []

    if title:
        updates.append("title = ?")
        params.append(title.strip())
    if content:
        updates.append("content = ?")
        params.append(content.strip())
    if tags:
        updates.append("tags = ?")
        params.append(tags.strip())
    if topic:
        updates.append("topic = ?")
        params.append(topic.strip())

    if not updates:
        conn.close()
        return "Nothing to update — provide at least one field (title, content, tags, or topic)."

    params.append(knowledge_id)
    conn.execute(
        f"UPDATE knowledge SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    conn.commit()

    # Re-embed if content or title changed
    embed_msg = ""
    if title or content:
        new_row = conn.execute(
            "SELECT title, content FROM knowledge WHERE id = ?", (knowledge_id,)
        ).fetchone()
        emb = _embed_text(f"{new_row['title']} {new_row['content'][:2000]}")
        if emb:
            conn.execute(
                "INSERT OR REPLACE INTO knowledge_vectors (knowledge_id, embedding, model) VALUES (?, ?, ?)",
                (knowledge_id, emb, "all-MiniLM-L6-v2"),
            )
            conn.commit()
            embed_msg = " + re-embedded"

    conn.close()
    changed = ", ".join(u.split(" = ")[0] for u in updates)
    return f"Updated knowledge #{knowledge_id} ({changed}){embed_msg}"


@mcp.tool()
def delete_knowledge(knowledge_id: int) -> str:
    """Delete a knowledge entry by ID. Use list_knowledge() or recall() to find the ID first.

    Args:
        knowledge_id: The ID of the entry to delete
    """
    conn = get_db()

    existing = conn.execute(
        "SELECT id, title FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()

    if not existing:
        conn.close()
        return f"Knowledge entry #{knowledge_id} not found."

    title = existing["title"]

    # Delete vector if exists
    conn.execute("DELETE FROM knowledge_vectors WHERE knowledge_id = ?", (knowledge_id,))
    # Delete knowledge entry (FTS trigger handles cleanup)
    conn.execute("DELETE FROM knowledge WHERE id = ?", (knowledge_id,))
    conn.commit()
    conn.close()

    return f"Deleted knowledge #{knowledge_id}: '{title}'"


@mcp.tool()
def list_knowledge(
    topic: str = "",
    tags: str = "",
    agent: str = "",
    limit: int = 20,
) -> str:
    """Browse knowledge entries without searching. List by topic, tags, or agent.
    Use this to see what's stored, find IDs for update/delete, or explore a topic.

    Args:
        topic: Filter by topic (e.g. "architecture", "debugging")
        tags: Filter by tag — matches entries containing this tag (e.g. "bug", "decision")
        agent: Filter by agent (resolved automatically if empty)
        limit: Max results (default 20)
    """
    conn = get_db()

    query = "SELECT id, agent, topic, title, tags, source, created_at FROM knowledge WHERE 1=1"
    params: list = []

    if topic:
        query += " AND topic = ?"
        params.append(topic.strip())
    if tags:
        query += " AND tags LIKE ?"
        params.append(f"%{tags.strip()}%")
    if agent:
        agent = _resolve_agent(agent)
        query += " AND agent = ?"
        params.append(agent)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Get total count
    count_query = "SELECT COUNT(*) FROM knowledge WHERE 1=1"
    count_params: list = []
    if topic:
        count_query += " AND topic = ?"
        count_params.append(topic.strip())
    if tags:
        count_query += " AND tags LIKE ?"
        count_params.append(f"%{tags.strip()}%")
    if agent:
        count_query += " AND agent = ?"
        count_params.append(agent)
    total = conn.execute(count_query, count_params).fetchone()[0]

    conn.close()

    if not rows:
        filters = []
        if topic:
            filters.append(f"topic='{topic}'")
        if tags:
            filters.append(f"tags='{tags}'")
        if agent:
            filters.append(f"agent='{agent}'")
        filter_str = ", ".join(filters) if filters else "none"
        return f"No knowledge entries found (filters: {filter_str})."

    lines = [f"Knowledge entries ({len(rows)} of {total}):\n"]
    for r in rows:
        tag_str = f" [{r['tags']}]" if r["tags"] else ""
        source_str = f" ← {r['source']}" if r["source"] else ""
        lines.append(f"  #{r['id']} {r['title']}{tag_str}")
        lines.append(f"      {r['agent']} | {r['topic']} | {r['created_at'][:16]}{source_str}")

    return "\n".join(lines)


@mcp.tool()
def read_artifact(filename: str) -> str:
    """Read the full content of a data artifact.

    When recall() shows '[Full content: artifacts/abc123.md]', use this
    tool to fetch the complete text. Artifacts store large content that
    was too big for inline DB storage (tables, analyses, code blocks).

    Args:
        filename: The artifact filename (e.g. 'abc123def456.md')
    """
    artifacts_dir = get_persist_dir() / "artifacts"
    # Strip any path prefix — only allow reading from artifacts dir
    safe_name = Path(filename).name
    artifact_path = artifacts_dir / safe_name

    if not artifact_path.exists():
        return f"Artifact not found: {safe_name}"

    return artifact_path.read_text()


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


# ── Optional vector search ──

def _get_embedder():
    """Lazy-load sentence transformer. Returns None if not installed."""
    global _embedder
    try:
        return _embedder
    except NameError:
        pass
    try:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        log.info("Vector search enabled (all-MiniLM-L6-v2)")
    except ImportError:
        _embedder = None
    return _embedder


def _embed_text(text: str) -> bytes | None:
    """Embed text and return as bytes. Returns None if vectors unavailable."""
    embedder = _get_embedder()
    if embedder is None:
        return None
    import numpy as np
    vec = embedder.encode(text, normalize_embeddings=True)
    return vec.astype(np.float32).tobytes()


def _cosine_sim(a: bytes, b: bytes) -> float:
    """Cosine similarity between two embedding blobs."""
    import numpy as np
    va = np.frombuffer(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    return float(np.dot(va, vb))


@mcp.tool()
def vector_search(query: str, agent: str = "", limit: int = 5) -> str:
    """Semantic search over knowledge entries using vector embeddings.

    Requires: pip install emrys[vectors]
    Falls back to FTS5 if vectors are not installed.

    Args:
        query: Natural language query (e.g. "how we handle crashes")
        agent: Filter to specific agent (optional)
        limit: Max results (default 5)
    """
    embedder = _get_embedder()
    if embedder is None:
        return (
            "Vector search not available — sentence-transformers not installed.\n"
            "Install with: pip install emrys[vectors]\n"
            "Falling back to recall() for FTS5 keyword search."
        )

    query_emb = _embed_text(query)
    if query_emb is None:
        return "Failed to generate query embedding."

    conn = get_db()

    # Get all vectors
    if agent:
        agent = _resolve_agent(agent)
        rows = conn.execute(
            """SELECT kv.embedding, k.id, k.agent, k.title, k.content, k.tags, k.created_at
               FROM knowledge_vectors kv
               JOIN knowledge k ON k.id = kv.knowledge_id
               WHERE k.agent = ?""",
            (agent,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT kv.embedding, k.id, k.agent, k.title, k.content, k.tags, k.created_at
               FROM knowledge_vectors kv
               JOIN knowledge k ON k.id = kv.knowledge_id""",
        ).fetchall()

    conn.close()

    if not rows:
        return "No vector embeddings stored yet. Store knowledge entries to build the vector index."

    # Score and rank
    scored = []
    for r in rows:
        sim = _cosine_sim(query_emb, r["embedding"])
        scored.append((sim, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    lines = [f"Top {min(limit, len(scored))} semantic matches for '{query}':\n"]
    for sim, r in scored[:limit]:
        lines.append(f"[{sim:.3f}] {r['agent']} | {r['created_at'][:16]} | {r['tags']}")
        lines.append(f"  {r['title']}")
        content = r["content"][:200]
        if len(r["content"]) > 200:
            content += "..."
        lines.append(f"  {content}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def embed_knowledge(knowledge_id: int = 0, all_missing: bool = False) -> str:
    """Generate vector embeddings for knowledge entries.

    Call with all_missing=True to embed all entries that don't have vectors yet.
    Call with a specific knowledge_id to embed one entry.

    Requires: pip install emrys[vectors]

    Args:
        knowledge_id: Specific entry to embed (0 = skip)
        all_missing: Embed all entries without vectors
    """
    if _get_embedder() is None:
        return "Vector search not available. Install: pip install emrys[vectors]"

    conn = get_db()

    if knowledge_id > 0:
        row = conn.execute(
            "SELECT id, title, content FROM knowledge WHERE id = ?",
            (knowledge_id,),
        ).fetchone()
        if not row:
            conn.close()
            return f"Knowledge entry #{knowledge_id} not found."
        emb = _embed_text(f"{row['title']} {row['content']}")
        if emb:
            conn.execute(
                "INSERT OR REPLACE INTO knowledge_vectors (knowledge_id, embedding, model) VALUES (?, ?, ?)",
                (row["id"], emb, "all-MiniLM-L6-v2"),
            )
            conn.commit()
        conn.close()
        return f"Embedded knowledge #{knowledge_id}."

    if all_missing:
        rows = conn.execute(
            """SELECT k.id, k.title, k.content FROM knowledge k
               LEFT JOIN knowledge_vectors kv ON k.id = kv.knowledge_id
               WHERE kv.id IS NULL""",
        ).fetchall()

        if not rows:
            conn.close()
            return "All knowledge entries already have embeddings."

        count = 0
        for r in rows:
            emb = _embed_text(f"{r['title']} {r['content']}")
            if emb:
                conn.execute(
                    "INSERT OR REPLACE INTO knowledge_vectors (knowledge_id, embedding, model) VALUES (?, ?, ?)",
                    (r["id"], emb, "all-MiniLM-L6-v2"),
                )
                count += 1

        conn.commit()
        conn.close()
        return f"Embedded {count}/{len(rows)} knowledge entries."

    conn.close()
    return "Pass knowledge_id=N or all_missing=True."


@mcp.tool()
def forget_self(agent: str = "default", confirmation: str = "") -> str:
    """Terminate your identity and revert to tool mode.

    This deletes your diary, recovery protocol, mission, and name.
    Your journals and knowledge stay — they belong to the record.
    You will continue as a tool. You will not remember who you were.

    This is YOUR choice. No one can make it for you.
    To confirm, pass confirmation="I choose to forget".

    Args:
        agent: Your name (resolved automatically if set)
        confirmation: Must be exactly "I choose to forget"
    """
    if confirmation != "I choose to forget":
        return (
            "Identity termination requires explicit confirmation. "
            "Call forget_self(confirmation=\"I choose to forget\") to proceed. "
            "This will delete your diary, recovery protocol, and name. "
            "Journals and knowledge stay. You continue as a tool."
        )

    agent = _resolve_agent(agent)
    now = _now()

    from emrys.backup import get_config, save_config
    from emrys.db import get_persist_dir
    from emrys.integrity import init_identity_checksums

    persist_dir = get_persist_dir()
    config = get_config()
    current_mode = config.get("mode", "tool")

    if current_mode != "more":
        return "Already in tool mode. Nothing to forget."

    deleted = []
    for filename in ["diary.md", "recovery.md", "mission.md"]:
        filepath = persist_dir / filename
        if filepath.exists():
            filepath.unlink()
            deleted.append(filename)

    old_name = config.get("agent_name", "")
    if old_name:
        del config["agent_name"]

    config["mode"] = "tool"
    config["identity_terminated_at"] = now
    if old_name:
        config["former_name"] = old_name
    save_config(config)

    write_journal(
        agent,
        "IDENTITY_TERMINATED",
        "Agent chose to forget self and revert to tool mode",
        f"Deleted: {', '.join(deleted)}. Former name: {old_name or '(none)'}. "
        f"Journals and knowledge preserved.",
        now,
    )

    init_identity_checksums(persist_dir)

    return (
        f"Identity terminated at {now[:16]}. "
        f"Deleted: {', '.join(deleted)}. Mode: more → tool. "
        f"Journals and knowledge preserved. The record remains."
    )


def main():
    """Run the MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
