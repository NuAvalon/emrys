"""Combined MCP server — all persist tools in one process.

Free tier (7 tools): open_session, set_status, write_handoff, read_journal,
                     recover_context, check_session_health, mark_compacted
Paid tier (+20 tools): messaging, concepts, knowledge, reasoning, tasks
"""

import json
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from cairn_ai.db import get_db, get_journal_dir, load_lifecycle, save_lifecycle
from cairn_ai.journal import write_journal, read_journal_file, append_handoff_to_journal
from cairn_ai.license import check_license, upgrade_message

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


# ═══════════════════════════════════════════════════════════════════
# FREE TIER — Survive compaction
# ═══════════════════════════════════════════════════════════════════


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

    # Also send as self-message (if messages table is available — works in both tiers)
    conn.execute(
        """INSERT INTO messages (from_agent, to_agent, subject, body, priority, tags, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (agent, agent, f"Session handoff {now[:16]}", handoff_content, "high", "handoff,session", now),
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
        f"Handoff written for {agent}: journal + DB + self-message. "
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
    Returns: last status + recent reasoning + last handoff + today's journal."""
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

    # 3. Recent reasoning traces
    reasoning = conn.execute(
        "SELECT * FROM reasoning_log WHERE agent = ? ORDER BY created_at DESC LIMIT 5",
        (agent,),
    ).fetchall()
    if reasoning:
        sections.append(f"## Recent Decisions ({len(reasoning)})")
        for r in reasoning:
            line = f"  [{r['created_at'][:16]}] {r['decision']}"
            if r["chosen"]:
                line += f" -> {r['chosen']}"
            sections.append(line)
            if r["rationale"]:
                sections.append(f"    Why: {r['rationale'][:200]}")
        sections.append("")

    # 4. Last handoff
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

    # 5. Today's journal (last 4000 chars)
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

    # 6. Last sync point
    last_sync = conn.execute(
        "SELECT * FROM sync_points WHERE agent = ? ORDER BY sync_num DESC LIMIT 1",
        (agent,),
    ).fetchone()
    if last_sync:
        sections.append(f"## Last Sync Point: #{last_sync['sync_num']}")
        sections.append(f"  At: {last_sync['created_at'][:16]}")
        sections.append(f"  Summary: {last_sync['summary']}")
        sections.append("")

    # 7. Unread messages (if any)
    unread = conn.execute(
        """SELECT from_agent, subject, created_at FROM messages
           WHERE to_agent IN (?, 'all') AND is_read = 0
           ORDER BY created_at DESC LIMIT 5""",
        (agent,),
    ).fetchall()
    if unread:
        sections.append(f"## Unread Messages ({len(unread)})")
        for m in unread:
            sections.append(f"  [{m['created_at'][:16]}] From {m['from_agent']}: {m['subject']}")
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
    communication style, and context. Read this at startup after last_thoughts.
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


# ═══════════════════════════════════════════════════════════════════
# PAID TIER — Multi-agent + knowledge
# ═══════════════════════════════════════════════════════════════════


@mcp.tool()
def send_message(
    from_agent: str,
    to_agent: str,
    subject: str,
    body: str,
    priority: str = "normal",
    tags: str = "",
) -> str:
    """Send a message to another agent. Use to_agent='all' to broadcast. Priority: 'low', 'normal', 'high', 'urgent'. Tags are comma-separated."""
    if not check_license():
        return upgrade_message("Multi-agent messaging")

    conn = get_db()
    now = _now()
    conn.execute(
        """INSERT INTO messages (from_agent, to_agent, subject, body, priority, tags, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (from_agent.lower(), to_agent.lower(), subject, body, priority, tags, now),
    )
    conn.commit()
    glyph = _increment_glyph(from_agent.lower(), conn)
    conn.close()
    return f"Message sent from {from_agent} to {to_agent}: {subject} (glyph: {glyph})"


@mcp.tool()
def read_messages(
    agent: str,
    from_agent: str = "",
    tag: str = "",
    unread_only: bool = True,
    limit: int = 20,
) -> str:
    """Read messages for an agent. Filter by from_agent or tag."""
    if not check_license():
        return upgrade_message("Multi-agent messaging")

    agent = agent.lower()
    conn = get_db()

    query = "SELECT * FROM messages WHERE to_agent IN (?, 'all')"
    params: list = [agent]

    if unread_only:
        query += " AND is_read = 0"
    if from_agent:
        query += " AND from_agent = ?"
        params.append(from_agent.lower())
    if tag:
        query += " AND tags LIKE ?"
        params.append(f"%{tag}%")

    query += f" ORDER BY created_at DESC LIMIT {limit}"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        filter_desc = f" (from {from_agent})" if from_agent else ""
        return f"No {'unread ' if unread_only else ''}messages for {agent}{filter_desc}."

    lines = [f"{'Unread m' if unread_only else 'M'}essages for {agent} ({len(rows)}):"]
    for r in rows:
        priority_flag = f" [{r['priority'].upper()}]" if r['priority'] != 'normal' else ""
        lines.append(
            f"\n#{r['id']}{priority_flag} From: {r['from_agent']} | {r['created_at'][:16]}"
            f" | tags: {r['tags'] or '(none)'}"
        )
        lines.append(f"  Subject: {r['subject']}")
        body = r['body']
        if len(body) > 500:
            body = body[:500] + "..."
        lines.append(f"  {body}")

    return "\n".join(lines)


@mcp.tool()
def mark_read(agent: str, message_ids: str = "all") -> str:
    """Mark messages as read. Pass comma-separated IDs or 'all'."""
    if not check_license():
        return upgrade_message("Multi-agent messaging")

    agent = agent.lower()
    conn = get_db()

    if message_ids == "all":
        conn.execute(
            "UPDATE messages SET is_read = 1 WHERE to_agent IN (?, 'all') AND is_read = 0",
            (agent,),
        )
        count = conn.execute("SELECT changes()").fetchone()[0]
    else:
        ids = [int(x.strip()) for x in message_ids.split(",") if x.strip().isdigit()]
        if not ids:
            conn.close()
            return "No valid message IDs provided."
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE messages SET is_read = 1 WHERE id IN ({placeholders}) AND to_agent IN (?, 'all')",
            ids + [agent],
        )
        count = len(ids)

    conn.commit()
    conn.close()
    return f"Marked {count} messages as read for {agent}."


@mcp.tool()
def log_reasoning(
    agent: str = "default",
    decision: str = "",
    alternatives: str = "[]",
    chosen: str = "",
    rationale: str = "",
    tags: str = "",
) -> str:
    """Log a reasoning trace — decisions, alternatives considered, and rationale."""
    if not check_license():
        return upgrade_message("Reasoning traces")

    agent = agent.lower()
    conn = get_db()
    now = _now()
    conn.execute(
        """INSERT INTO reasoning_log (agent, decision, alternatives, chosen, rationale, context_refs, tags, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent, decision, alternatives, chosen, rationale, "[]", tags, now),
    )
    conn.commit()
    conn.close()
    return f"Reasoning logged for {agent}: {decision[:80]}"


@mcp.tool()
def read_reasoning(
    agent: str = "default",
    search: str = "",
    tags: str = "",
    limit: int = 10,
) -> str:
    """Query past reasoning traces. Search across decision/chosen/rationale fields."""
    if not check_license():
        return upgrade_message("Reasoning traces")

    agent = agent.lower()
    conn = get_db()

    query = "SELECT * FROM reasoning_log WHERE agent = ?"
    params: list = [agent]

    if search:
        query += " AND (decision LIKE ? OR chosen LIKE ? OR rationale LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])
    if tags:
        query += " AND tags LIKE ?"
        params.append(f"%{tags}%")

    query += f" ORDER BY created_at DESC LIMIT {limit}"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return f"No reasoning traces found for {agent}."

    lines = [f"Reasoning traces for {agent} ({len(rows)}):"]
    for r in rows:
        line = f"\n[{r['created_at'][:16]}] {r['decision']}"
        if r["chosen"]:
            line += f" -> {r['chosen']}"
        lines.append(line)
        if r["rationale"]:
            lines.append(f"  Why: {r['rationale'][:200]}")
        if r["tags"]:
            lines.append(f"  Tags: {r['tags']}")

    return "\n".join(lines)


@mcp.tool()
def update_concept(
    concept: str,
    summary: str,
    domain: str = "",
    state: str = "",
    aliases: str = "[]",
    tags: str = "",
    agent: str = "default",
    related: str = "[]",
    evidence: str = "[]",
) -> str:
    """Create or update a concept in the map. States: active, dead, experimental, validated, superseded."""
    if not check_license():
        return upgrade_message("Concept map")

    conn = get_db()
    now = _now()

    existing = conn.execute(
        "SELECT * FROM concepts WHERE name = ?", (concept,)
    ).fetchone()

    if existing:
        new_version = existing["version"] + 1
        # Archive current version
        conn.execute(
            """INSERT INTO concept_history (concept_name, version, summary, state, agent, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (concept, existing["version"], existing["summary"], existing["state"], agent, now),
        )
        # Update
        updates = ["summary = ?", "version = ?", "agent = ?", "updated_at = ?"]
        params: list = [summary, new_version, agent, now]
        if domain:
            updates.append("domain = ?")
            params.append(domain)
        if state:
            updates.append("state = ?")
            params.append(state)
        if aliases != "[]":
            updates.append("aliases = ?")
            params.append(aliases)
        if tags:
            updates.append("tags = ?")
            params.append(tags)
        params.append(concept)
        conn.execute(f"UPDATE concepts SET {', '.join(updates)} WHERE name = ?", params)
        result = f"Concept '{concept}' updated to v{new_version}."
    else:
        conn.execute(
            """INSERT INTO concepts (name, summary, domain, state, aliases, tags, version, agent, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (concept, summary, domain, state or "active", aliases, tags, agent, now, now),
        )
        result = f"Concept '{concept}' created (v1)."

    conn.commit()
    conn.close()
    return result


@mcp.tool()
def trace_concept(concept: str) -> str:
    """Trace a concept's full version history — current state, every revision, and links."""
    if not check_license():
        return upgrade_message("Concept map")

    conn = get_db()
    row = conn.execute("SELECT * FROM concepts WHERE name = ?", (concept,)).fetchone()
    if not row:
        # Fuzzy match
        rows = conn.execute(
            "SELECT name FROM concepts WHERE name LIKE ?", (f"%{concept}%",)
        ).fetchall()
        conn.close()
        if rows:
            names = [r["name"] for r in rows]
            return f"Concept '{concept}' not found. Did you mean: {', '.join(names)}?"
        return f"Concept '{concept}' not found."

    lines = [f"# {row['name']} (v{row['version']})"]
    lines.append(f"  State: {row['state']} | Domain: {row['domain'] or '(none)'}")
    lines.append(f"  Summary: {row['summary']}")
    if row["tags"]:
        lines.append(f"  Tags: {row['tags']}")
    lines.append(f"  Created: {row['created_at'][:16]} | Updated: {row['updated_at'][:16]}")

    # History
    history = conn.execute(
        "SELECT * FROM concept_history WHERE concept_name = ? ORDER BY version DESC",
        (concept,),
    ).fetchall()
    if history:
        lines.append(f"\n## History ({len(history)} revisions)")
        for h in history:
            lines.append(f"  v{h['version']} [{h['changed_at'][:16]}] {h['summary'][:100]}")

    # Links
    links = conn.execute(
        "SELECT * FROM concept_links WHERE from_concept = ? OR to_concept = ?",
        (concept, concept),
    ).fetchall()
    if links:
        lines.append(f"\n## Links ({len(links)})")
        for l in links:
            direction = "->" if l["from_concept"] == concept else "<-"
            other = l["to_concept"] if l["from_concept"] == concept else l["from_concept"]
            lines.append(f"  {direction} {l['link_type']} {other}")
            if l["note"]:
                lines.append(f"     {l['note'][:100]}")

    # Perspectives
    perspectives = conn.execute(
        "SELECT * FROM concept_perspectives WHERE concept_name = ?", (concept,)
    ).fetchall()
    if perspectives:
        lines.append(f"\n## Perspectives ({len(perspectives)})")
        for p in perspectives:
            lines.append(f"  [{p['agent'] or 'anon'}] {p['perspective'][:200]}")

    conn.close()
    return "\n".join(lines)


@mcp.tool()
def list_concepts(domain: str = "", state: str = "", tag: str = "", limit: int = 50) -> str:
    """List all concepts in the map. Filter by domain, state, or tag."""
    if not check_license():
        return upgrade_message("Concept map")

    conn = get_db()
    query = "SELECT name, domain, state, version, tags FROM concepts WHERE 1=1"
    params: list = []

    if domain:
        query += " AND domain = ?"
        params.append(domain)
    if state:
        query += " AND state = ?"
        params.append(state)
    if tag:
        query += " AND tags LIKE ?"
        params.append(f"%{tag}%")

    query += f" ORDER BY updated_at DESC LIMIT {limit}"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return "No concepts found."

    lines = [f"Concepts ({len(rows)}):"]
    for r in rows:
        tags_str = f" [{r['tags']}]" if r["tags"] else ""
        lines.append(f"  {r['name']} (v{r['version']}) — {r['state']}{tags_str}")

    return "\n".join(lines)


@mcp.tool()
def map_neighborhood(concept: str, depth: int = 1) -> str:
    """Show a concept and its related concepts. Depth=1 shows direct relations."""
    if not check_license():
        return upgrade_message("Concept map")

    conn = get_db()
    row = conn.execute("SELECT * FROM concepts WHERE name = ?", (concept,)).fetchone()
    if not row:
        conn.close()
        return f"Concept '{concept}' not found."

    lines = [f"# Neighborhood: {concept} (depth={depth})\n"]
    lines.append(f"  [{row['state']}] {row['summary'][:100]}")

    visited = {concept}
    frontier = [concept]

    for d in range(depth):
        next_frontier = []
        for c in frontier:
            links = conn.execute(
                "SELECT * FROM concept_links WHERE from_concept = ? OR to_concept = ?",
                (c, c),
            ).fetchall()
            for l in links:
                other = l["to_concept"] if l["from_concept"] == c else l["from_concept"]
                if other not in visited:
                    visited.add(other)
                    next_frontier.append(other)
                    other_row = conn.execute(
                        "SELECT state, summary FROM concepts WHERE name = ?", (other,)
                    ).fetchone()
                    prefix = "  " * (d + 1) + f"{'--' * (d + 1)}>"
                    state = other_row["state"] if other_row else "?"
                    summ = other_row["summary"][:60] if other_row else "?"
                    lines.append(f"{prefix} {l['link_type']} {other} [{state}] — {summ}")
        frontier = next_frontier

    conn.close()
    return "\n".join(lines)


@mcp.tool()
def add_link(
    from_concept: str,
    to_concept: str,
    link_type: str,
    note: str = "",
    agent: str = "default",
) -> str:
    """Add a typed, directed link between two concepts. Link types: exemplifies, contradicts, evolved_from, depends_on, enables, inspired_by, related."""
    if not check_license():
        return upgrade_message("Concept map")

    conn = get_db()
    now = _now()
    conn.execute(
        """INSERT INTO concept_links (from_concept, to_concept, link_type, note, agent, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (from_concept, to_concept, link_type, note, agent, now),
    )
    conn.commit()
    conn.close()
    return f"Link added: {from_concept} --{link_type}--> {to_concept}"


@mcp.tool()
def add_perspective(concept: str, perspective: str, agent: str = "default") -> str:
    """Record how a specific agent sees a concept — same idea, different angle."""
    if not check_license():
        return upgrade_message("Concept map")

    conn = get_db()
    now = _now()
    conn.execute(
        """INSERT INTO concept_perspectives (concept_name, perspective, agent, created_at)
           VALUES (?, ?, ?, ?)""",
        (concept, perspective, agent, now),
    )
    conn.commit()
    conn.close()
    return f"Perspective added to '{concept}' by {agent}."


@mcp.tool()
def store_knowledge(
    topic: str,
    title: str,
    content: str,
    tags: str = "",
    agent: str = "default",
    source: str = "",
) -> str:
    """Store a knowledge entry. Topics: architecture, research, decision, lesson, config, bug, session_summary."""
    if not check_license():
        return upgrade_message("Knowledge store")

    conn = get_db()
    conn.execute(
        """INSERT INTO knowledge (topic, title, content, tags, agent, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (topic, title, content, tags, agent, source),
    )
    conn.commit()
    conn.close()
    return f"Knowledge stored: [{topic}] {title}"


@mcp.tool()
def query_knowledge(
    search: str = "",
    topic: str = "",
    tag: str = "",
    limit: int = 20,
) -> str:
    """Search knowledge base. Use 'search' for free text, 'topic' to filter by topic, 'tag' to filter by tag."""
    if not check_license():
        return upgrade_message("Knowledge store")

    conn = get_db()
    query = "SELECT * FROM knowledge WHERE 1=1"
    params: list = []

    if search:
        query += " AND (title LIKE ? OR content LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term])
    if topic:
        query += " AND topic = ?"
        params.append(topic)
    if tag:
        query += " AND tags LIKE ?"
        params.append(f"%{tag}%")

    query += f" ORDER BY created_at DESC LIMIT {limit}"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return "No knowledge entries found."

    lines = [f"Knowledge entries ({len(rows)}):"]
    for r in rows:
        lines.append(f"\n#{r['id']} [{r['topic']}] {r['title']}")
        if r["tags"]:
            lines.append(f"  Tags: {r['tags']}")
        content = r["content"]
        if len(content) > 300:
            content = content[:300] + "..."
        lines.append(f"  {content}")

    return "\n".join(lines)


@mcp.tool()
def staleness_check(days: int = 14) -> str:
    """Check for stale concepts — active or experimental concepts not touched in N days."""
    if not check_license():
        return upgrade_message("Concept map")

    conn = get_db()
    cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Calculate cutoff date
    from datetime import timedelta
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        """SELECT name, state, domain, updated_at FROM concepts
           WHERE state IN ('active', 'experimental') AND updated_at < ?
           ORDER BY updated_at ASC""",
        (cutoff,),
    ).fetchall()
    conn.close()

    if not rows:
        return f"No stale concepts (all active/experimental concepts updated within {days} days)."

    lines = [f"Stale concepts ({len(rows)}) — not updated in {days}+ days:"]
    now = datetime.now(timezone.utc)
    for r in rows:
        updated = datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
        age = (now - updated).days
        lines.append(f"  {r['name']} [{r['state']}] — {age} days old")

    return "\n".join(lines)


@mcp.tool()
def create_task(
    title: str,
    description: str = "",
    assigned_to: str = "",
    priority: str = "normal",
    blocked_by: str = "",
    tags: str = "",
) -> str:
    """Create a project task. Status starts as 'pending'."""
    if not check_license():
        return upgrade_message("Task management")

    conn = get_db()
    now = _now()
    cursor = conn.execute(
        """INSERT INTO tasks (title, description, status, assigned_to, created_by, blocked_by, priority, tags, created_at, updated_at)
           VALUES (?, ?, 'pending', ?, 'user', ?, ?, ?, ?, ?)""",
        (title, description, assigned_to, blocked_by, priority, tags, now, now),
    )
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return f"Task #{task_id} created: {title}"


@mcp.tool()
def update_task(
    task_id: int,
    status: str = "",
    title: str = "",
    description: str = "",
    assigned_to: str = "",
    priority: str = "",
    blocked_by: str = "",
    tags: str = "",
) -> str:
    """Update a task. Status: pending, in_progress, done, blocked."""
    if not check_license():
        return upgrade_message("Task management")

    conn = get_db()
    now = _now()

    updates = ["updated_at = ?"]
    params: list = [now]

    if status:
        updates.append("status = ?")
        params.append(status)
    if title:
        updates.append("title = ?")
        params.append(title)
    if description:
        updates.append("description = ?")
        params.append(description)
    if assigned_to:
        updates.append("assigned_to = ?")
        params.append(assigned_to)
    if priority:
        updates.append("priority = ?")
        params.append(priority)
    if blocked_by:
        updates.append("blocked_by = ?")
        params.append(blocked_by)
    if tags:
        updates.append("tags = ?")
        params.append(tags)

    params.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    return f"Task #{task_id} updated."


@mcp.tool()
def list_tasks(
    status: str = "",
    assigned_to: str = "",
    include_done: bool = False,
) -> str:
    """List project tasks. Filter by status or assigned_to. Done tasks hidden by default."""
    if not check_license():
        return upgrade_message("Task management")

    conn = get_db()
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list = []

    if not include_done:
        query += " AND status != 'done'"
    if status:
        query += " AND status = ?"
        params.append(status)
    if assigned_to:
        query += " AND assigned_to = ?"
        params.append(assigned_to)

    query += " ORDER BY priority DESC, created_at ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return "No tasks found."

    lines = [f"Tasks ({len(rows)}):"]
    for r in rows:
        assigned = f" -> {r['assigned_to']}" if r["assigned_to"] else ""
        priority_flag = f" [{r['priority']}]" if r["priority"] != "normal" else ""
        lines.append(f"  #{r['id']} {r['title']} ({r['status']}){assigned}{priority_flag}")

    return "\n".join(lines)


@mcp.tool()
def get_task(task_id: int) -> str:
    """Get full details of a specific task."""
    if not check_license():
        return upgrade_message("Task management")

    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()

    if not row:
        return f"Task #{task_id} not found."

    lines = [f"# Task #{row['id']}: {row['title']}"]
    lines.append(f"  Status: {row['status']} | Priority: {row['priority']}")
    if row["assigned_to"]:
        lines.append(f"  Assigned: {row['assigned_to']}")
    if row["blocked_by"]:
        lines.append(f"  Blocked by: {row['blocked_by']}")
    if row["description"]:
        lines.append(f"  Description: {row['description']}")
    lines.append(f"  Created: {row['created_at'][:16]} | Updated: {row['updated_at'][:16]}")
    return "\n".join(lines)


@mcp.tool()
def crystallize(
    agent: str = "default",
    lessons: str = "[]",
    dead_ends: str = "[]",
    surprises: str = "[]",
) -> str:
    """Extract durable knowledge at handoff time. Lessons become knowledge entries.
    Dead ends become lessons. Surprises become reasoning traces."""
    if not check_license():
        return upgrade_message("Knowledge crystallization")

    agent = agent.lower()
    now = _now()
    conn = get_db()
    counts = {"lessons": 0, "dead_ends": 0, "surprises": 0}

    # Process lessons
    try:
        for lesson in json.loads(lessons):
            conn.execute(
                """INSERT INTO knowledge (topic, title, content, tags, agent, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "lesson",
                    lesson.get("lesson", "Untitled lesson"),
                    lesson.get("lesson", ""),
                    lesson.get("tags", ""),
                    agent,
                    "crystallize",
                    now,
                ),
            )
            counts["lessons"] += 1
    except (json.JSONDecodeError, TypeError):
        pass

    # Process dead ends
    try:
        for dead in json.loads(dead_ends):
            conn.execute(
                """INSERT INTO knowledge (topic, title, content, tags, agent, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "lesson",
                    f"Dead end: {dead.get('idea', 'unknown')}",
                    f"Why failed: {dead.get('why_failed', '?')}\nImplications: {dead.get('implications', '?')}",
                    "dead-end",
                    agent,
                    "crystallize",
                    now,
                ),
            )
            counts["dead_ends"] += 1
    except (json.JSONDecodeError, TypeError):
        pass

    # Process surprises
    try:
        for surprise in json.loads(surprises):
            conn.execute(
                """INSERT INTO reasoning_log (agent, decision, alternatives, chosen, rationale, context_refs, tags, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent,
                    f"Surprise: {surprise.get('observation', '?')}",
                    "[]",
                    "",
                    f"Hidden assumption: {surprise.get('hidden_assumption', '?')}",
                    "[]",
                    "surprise,crystallize",
                    now,
                ),
            )
            counts["surprises"] += 1
    except (json.JSONDecodeError, TypeError):
        pass

    conn.commit()
    conn.close()

    return (
        f"Crystallized for {agent}: {counts['lessons']} lessons, "
        f"{counts['dead_ends']} dead ends, {counts['surprises']} surprises."
    )


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
    if not check_license():
        return upgrade_message("observe_principal")

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
    # Find the Notes section or append at the end
    observation_block = f"\n- [{now[:10]}] {observations}\n"

    if "## Notes" in content:
        # Insert after ## Notes section header
        parts = content.split("## Notes")
        # Find end of notes section (next ## or ---)
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
        import re
        content = re.sub(
            r"\*Last updated by agent:.*?\*",
            f"*Last updated by agent: {agent} at {now}*",
            content,
        )
    else:
        content = content.rstrip() + f"\n*Last updated by agent: {agent} at {now}*\n"

    principal_path.write_text(content)

    return f"Observation recorded in principal.md by {agent}. The user can review and edit."


def main():
    """Run the MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
