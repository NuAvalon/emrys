"""Transcript ingest — parse JSONL transcripts into emrys knowledge.

Reads JSONL conversation logs, extracts key moments (tool calls, decisions,
user instructions), and stores them as timestamped knowledge entries.
The agent never touches raw JSONL — this runs offline via CLI.

Noise reduction: filters out mechanical navigation ("let me read the file"),
benign errors (file not found), temp file writes, and short low-signal
assistant responses. Good inputs = good outputs.

Content storage: full text is stored (no truncation). For entries larger than
10KB, the full content is extracted to .persist/artifacts/ and the DB holds
a preview with an artifact reference. FTS5 indexes all searchable content.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from emrys.db import get_db, get_persist_dir


# --- Noise filters ---

# File writes: only track files with these extensions
_NOTABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",
    ".json", ".yaml", ".yml", ".toml", ".md", ".html", ".css",
    ".sh", ".sql", ".env", ".cfg",
}

# Skip file writes inside these directories
_NOISE_DIRS = {
    "__pycache__", "node_modules", ".git", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
    ".egg-info", ".cache", "tmp", "temp",
}

# High-signal decision markers — substantive thinking
_SUBSTANTIVE_MARKERS = [
    "root cause", "the fix", "found the bug", "the issue is",
    "the problem is", "my recommendation", "the approach",
    "design decision", "trade-off", "architecture",
    "breaking change", "the solution", "key insight",
    "this works because", "the reason", "critical",
    "we should", "the right way", "this breaks",
]

# Low-signal decision markers — need more content to qualify
_MECHANICAL_MARKERS = [
    "i'll ", "i will ", "this means ",
]

# Skip assistant messages that are just navigation/routing
_SKIP_PREFIXES = [
    "let me read", "let me check", "let me search", "let me look",
    "let me find", "let me explore", "let me see", "let me open",
    "i'll read the", "i'll look at", "i'll search",
    "searching for", "reading the file", "looking at the",
    "now let me", "first, let me", "good, ", "ok, ",
]

# Benign errors to skip (framework noise, not real bugs)
_BENIGN_ERROR_PATTERNS = [
    "no such file", "file not found", "not found:",
    "permission denied", "is a directory", "not a directory",
    "no matches found", "command not found",
    "does not exist", "already exists",
    "no results", "0 matches", "empty result",
]

# Minimum content lengths
_MIN_USER_LEN = 30
_MIN_DECISION_LEN = 150      # Low-signal markers need substantial content
_MIN_SUBSTANTIVE_LEN = 80    # High-signal markers can be shorter

# Artifact threshold — content larger than this gets extracted to a file
_ARTIFACT_THRESHOLD = 10_000  # 10KB


def ingest_transcript(path: str, agent: str = "default",
                      dry_run: bool = False) -> str:
    """Parse a JSONL transcript and store highlights.

    Args:
        path: Path to the JSONL transcript file
        agent: Agent name to attribute entries to
        dry_run: If True, show what would be ingested without writing to DB

    Returns:
        Summary of what was ingested.
    """
    transcript_path = Path(path)
    if not transcript_path.exists():
        return f"File not found: {path}"

    if not transcript_path.suffix == ".jsonl":
        return f"Expected .jsonl file, got: {transcript_path.suffix}"

    entries = _parse_transcript(transcript_path, agent)

    if not entries:
        return f"No notable entries found in {transcript_path.name}"

    if dry_run:
        lines = [f"DRY RUN — {len(entries)} entries from {transcript_path.name}:\n"]
        for e in entries:
            tag = e["tags"].split(",")[-1]
            size = f" ({len(e['content'])} chars)" if len(e["content"]) > 1000 else ""
            lines.append(f"  [{tag:>16}] {e['title'][:90]}{size}")
        lines.append(_summary_line(entries, transcript_path.name, prefix="Would ingest"))
        return "\n".join(lines)

    # Store in knowledge table
    conn = get_db()
    stored = 0
    artifacts = 0
    for entry in entries:
        content, artifact_path = _prepare_content(entry["content"])
        conn.execute(
            """INSERT INTO knowledge (agent, topic, title, content, tags, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (entry["agent"], "transcript", entry["title"],
             content, entry["tags"], f"transcript:{transcript_path.name}",
             entry["ts"]),
        )
        stored += 1
        if artifact_path:
            artifacts += 1
    conn.commit()
    conn.close()

    result = _summary_line(entries, transcript_path.name, prefix="Ingested")
    if artifacts:
        result += f"\n  Artifacts: {artifacts} (large content extracted to .persist/artifacts/)"
    return result


def _prepare_content(content: str) -> tuple[str, str | None]:
    """Prepare content for storage — inline or artifact.

    Returns (db_content, artifact_path_or_None).
    Content under 10KB: stored fully inline in the DB.
    Content over 10KB: full text saved as artifact file,
        DB gets first 5KB + artifact reference for FTS coverage.
    """
    if len(content) <= _ARTIFACT_THRESHOLD:
        return content, None

    # Extract to artifact file
    artifacts_dir = get_persist_dir() / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    artifact_name = f"{content_hash}.md"
    artifact_path = artifacts_dir / artifact_name
    artifact_path.write_text(content)

    # DB gets a generous preview + reference. The first 5KB covers most
    # search terms while keeping the DB lean for huge entries.
    preview = content[:5000]
    db_content = f"{preview}\n\n[Full content: artifacts/{artifact_name} ({len(content)} chars)]"

    return db_content, str(artifact_path)


def _summary_line(entries: list[dict], filename: str, prefix: str) -> str:
    """Build a summary line from entries."""
    counts = {}
    for tag_key in ("decision", "user-instruction", "commit", "file-write", "error"):
        counts[tag_key] = sum(1 for e in entries if tag_key in e["tags"])
    return (
        f"{prefix} {len(entries)} entries from {filename}\n"
        f"  Decisions: {counts['decision']}\n"
        f"  User instructions: {counts['user-instruction']}\n"
        f"  Commits: {counts['commit']}\n"
        f"  File writes: {counts['file-write']}\n"
        f"  Errors: {counts['error']}"
    )


def _parse_transcript(path: Path, agent: str) -> list[dict]:
    """Parse JSONL and extract notable entries."""
    entries = []
    seen_titles = set()  # Dedup

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            extracted = _extract_from_record(record, agent)
            for entry in extracted:
                if entry["title"] not in seen_titles:
                    seen_titles.add(entry["title"])
                    entries.append(entry)

    return entries


def _is_notable_file(file_path: str) -> bool:
    """Check if a file write is worth recording."""
    p = Path(file_path)
    # Skip files in noise directories
    if set(p.parts) & _NOISE_DIRS:
        return False
    return p.suffix.lower() in _NOTABLE_EXTENSIONS


def _is_benign_error(text: str) -> bool:
    """Check if an error is framework noise rather than a real bug."""
    lower = text.lower()[:300]
    return any(pat in lower for pat in _BENIGN_ERROR_PATTERNS)


def _is_mechanical(text: str) -> bool:
    """Check if assistant text is just navigation/routing, not a decision."""
    lower = text.lower().strip()
    return any(lower.startswith(p) for p in _SKIP_PREFIXES)


def _extract_commit_msg(cmd: str) -> str:
    """Extract commit message from various git commit formats."""
    # Heredoc: -m "$(cat <<'EOF'\nmessage here\n..."
    m = re.search(r"EOF['\"]?\s*\n(.+?)(?:\nEOF|\nCo-Authored|\Z)",
                  cmd, re.DOTALL)
    if m:
        return m.group(1).strip()[:200]

    # Simple: -m "message" or -m 'message'
    m = re.search(r'-m\s+["\']([^"\']+)["\']', cmd)
    if m:
        return m.group(1).strip()[:200]

    # Fallback: first line after the commit command
    m = re.search(r'git commit.*?-m\s+(.*)', cmd)
    if m:
        return m.group(1).strip()[:200]

    return "(commit message not parsed)"


def _extract_from_record(record: dict, agent: str) -> list[dict]:
    """Extract notable entries from a single JSONL record."""
    entries = []
    ts = record.get("timestamp", "")
    if not ts:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # JSONL nests role/content inside a "message" object.
    # Top-level "type" field holds "user", "assistant", "progress", etc.
    msg = record.get("message", {})
    if not isinstance(msg, dict):
        msg = {}
    role = msg.get("role", "") or record.get("type", "")

    # --- User messages: instructions and real errors ---
    if role in ("human", "user"):
        content_blocks = msg.get("content", [])
        has_tool_results = False
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    has_tool_results = True
                    tr_content = block.get("content", "")
                    if isinstance(tr_content, str) and len(tr_content) > 100:
                        if _is_benign_error(tr_content):
                            continue
                        lower_200 = tr_content.lower()[:200]
                        if re.search(r'\berror\b', lower_200) or "traceback" in lower_200:
                            entries.append({
                                "agent": agent,
                                "ts": ts,
                                "title": f"Error: {tr_content[:100]}",
                                "content": tr_content,
                                "tags": "transcript,finding,error",
                            })

        if not has_tool_results:
            content = _get_text_content(msg)
            if content and len(content) > _MIN_USER_LEN:
                instruction_markers = [
                    "please ", "can you ", "make sure ", "don't ", "always ",
                    "never ", "i want ", "let's ", "we need ", "go ahead",
                    "approved", "fix ", "add ", "change ", "update ",
                    "remove ", "implement",
                ]
                lower = content.lower()
                if any(lower.startswith(m) or f" {m}" in lower[:100]
                       for m in instruction_markers):
                    entries.append({
                        "agent": agent,
                        "ts": ts,
                        "title": f"User: {content[:120]}",
                        "content": content,
                        "tags": "transcript,user-instruction",
                    })

    # --- Assistant messages: substantive decisions, commits, notable writes ---
    elif role == "assistant":
        content = _get_text_content(msg)
        if content and not _is_mechanical(content):
            lower = content.lower()

            # Check signal quality
            has_substance = any(m in lower[:300] for m in _SUBSTANTIVE_MARKERS)
            has_mechanical = any(m in lower[:200] for m in _MECHANICAL_MARKERS)

            if has_substance and len(content) > _MIN_SUBSTANTIVE_LEN:
                entries.append({
                    "agent": agent,
                    "ts": ts,
                    "title": f"Decision: {content[:120]}",
                    "content": content,
                    "tags": "transcript,decision",
                })
            elif has_mechanical and len(content) > _MIN_DECISION_LEN:
                entries.append({
                    "agent": agent,
                    "ts": ts,
                    "title": f"Decision: {content[:120]}",
                    "content": content,
                    "tags": "transcript,decision",
                })

        # Tool use blocks
        content_blocks = msg.get("content", [])
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue

                tool_name = block.get("name", "")
                tool_input = block.get("input", {})

                # Git commits — always notable
                if tool_name == "Bash":
                    cmd = tool_input.get("command", "")
                    if "git commit" in cmd and "--amend" not in cmd:
                        commit_msg = _extract_commit_msg(cmd)
                        entries.append({
                            "agent": agent,
                            "ts": ts,
                            "title": f"Commit: {commit_msg[:120]}",
                            "content": commit_msg,
                            "tags": "transcript,commit",
                        })

                # File writes — only notable files
                elif tool_name == "Write":
                    file_path = tool_input.get("file_path", "")
                    if file_path and _is_notable_file(file_path):
                        entries.append({
                            "agent": agent,
                            "ts": ts,
                            "title": f"Created: {Path(file_path).name}",
                            "content": f"File created: {file_path}",
                            "tags": "transcript,file-write",
                        })

    return entries


def _get_text_content(record: dict) -> str:
    """Extract text from various record formats."""
    content = record.get("content", "")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return " ".join(texts).strip()

    message = record.get("message", "")
    if isinstance(message, str):
        return message.strip()

    return ""


def import_all_sessions(search_dir: str = "", agent_filter: str = "",
                        dry_run: bool = False, since: str = "",
                        create_journals: bool = True) -> str:
    """Bulk import Claude Code sessions into emrys memory.

    Scans for JSONL session files, deduplicates against already-imported
    sessions, extracts highlights into knowledge + creates journal entries.

    Args:
        search_dir: Directory to scan (default: ~/.claude/projects/)
        agent_filter: Only import sessions matching this agent name
        dry_run: Preview without writing
        since: Only import sessions modified after this date (YYYY-MM-DD)
        create_journals: Also create journal entries (chronological narrative)

    Returns:
        Human-readable summary of what was imported.
    """
    # Find all sessions
    transcripts = find_transcripts(search_dir)
    if not transcripts:
        return "No session files found."

    # Filter by date
    if since:
        transcripts = [t for t in transcripts if t["modified"] >= since]
        if not transcripts:
            return f"No sessions found after {since}."

    # Check what's already imported (dedup by filename)
    conn = get_db()
    already_imported = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT source FROM knowledge WHERE source LIKE 'transcript:%'"
        ).fetchall()
        for row in rows:
            # source format: "transcript:UUID.jsonl"
            already_imported.add(row[0].replace("transcript:", ""))
    except Exception:
        pass

    # Process each session
    total_knowledge = 0
    total_journals = 0
    imported_sessions = 0
    skipped_sessions = 0
    failed_sessions = 0
    session_summaries = []

    for t in transcripts:
        path = Path(t["path"])
        filename = path.name

        # Skip already imported
        if filename in already_imported:
            skipped_sessions += 1
            continue

        # Detect agent from session content
        agent = _detect_agent(path)
        if agent_filter and agent != agent_filter:
            skipped_sessions += 1
            continue

        if dry_run:
            entries = _parse_transcript(path, agent)
            journal_count = 0
            if create_journals:
                journal_entries = _extract_journal_entries(path, agent)
                journal_count = len(journal_entries)

            session_summaries.append(
                f"  {filename[:40]:40} {agent:>10}  "
                f"{len(entries):>3} knowledge, {journal_count:>3} journal"
            )
            total_knowledge += len(entries)
            total_journals += journal_count
            imported_sessions += 1
            continue

        # Ingest knowledge entries
        try:
            entries = _parse_transcript(path, agent)
            for entry in entries:
                content, _ = _prepare_content(entry["content"])
                conn.execute(
                    """INSERT INTO knowledge (agent, topic, title, content, tags, source, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (entry["agent"], "transcript", entry["title"],
                     content, entry["tags"], f"transcript:{filename}",
                     entry["ts"]),
                )
            total_knowledge += len(entries)

            # Create journal entries
            if create_journals:
                journal_entries = _extract_journal_entries(path, agent)
                for je in journal_entries:
                    conn.execute(
                        """INSERT INTO journal_entries (agent, ts, status, task, finding)
                           VALUES (?, ?, ?, ?, ?)""",
                        (je["agent"], je["ts"], je["status"],
                         je["task"], je["finding"]),
                    )
                total_journals += len(journal_entries)

            conn.commit()
            imported_sessions += 1
            session_summaries.append(
                f"  {filename[:40]:40} {agent:>10}  "
                f"{len(entries):>3} knowledge, {len(journal_entries) if create_journals else 0:>3} journal"
            )

        except Exception as e:
            failed_sessions += 1
            session_summaries.append(f"  {filename[:40]:40} FAILED: {e}")

    conn.close()

    # Build summary
    prefix = "DRY RUN — would import" if dry_run else "Imported"
    lines = [
        f"\n{prefix} {imported_sessions} session(s):\n",
    ]
    if session_summaries:
        lines.append(f"  {'Session':40} {'Agent':>10}  Entries")
        lines.append(f"  {'─' * 40} {'─' * 10}  {'─' * 25}")
        lines.extend(session_summaries)
    lines.append("")
    lines.append(f"  Knowledge entries: {total_knowledge}")
    lines.append(f"  Journal entries:   {total_journals}")
    lines.append(f"  Skipped (already imported or filtered): {skipped_sessions}")
    if failed_sessions:
        lines.append(f"  Failed: {failed_sessions}")
    lines.append("")

    if dry_run:
        lines.append("Run without --dry-run to import.")
    else:
        lines.append("Done. Your agent now remembers these sessions.")

    return "\n".join(lines)


def _detect_agent(path: Path) -> str:
    """Detect which agent a session belongs to by scanning early messages.

    Checks filename patterns first (agent_<name> sessions), then scans
    content for summon prompts, identity references, and greeting patterns.
    """
    agent_names = {"archie", "apollo", "athena", "hypatia"}

    # Check filename first — fork/agent sessions often have the name
    fname = path.stem.lower()
    for name in agent_names:
        if name in fname:
            return name

    try:
        with open(path) as f:
            lines_checked = 0
            for line in f:
                if lines_checked > 30:
                    break
                lines_checked += 1

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Check slug field (e.g. "purring-dancing-crescent")
                # and sessionId for agent hints
                slug = record.get("slug", "")

                msg = record.get("message", {})
                if not isinstance(msg, dict):
                    continue

                # Check tool inputs for agent references (e.g. Read diary/apollo.md)
                content_blocks = msg.get("content", [])
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if isinstance(block, dict):
                            # Tool use inputs
                            tool_input = block.get("input", {})
                            if isinstance(tool_input, dict):
                                for v in tool_input.values():
                                    if isinstance(v, str):
                                        for name in agent_names:
                                            if (f"/{name}." in v or f"/{name}/" in v or
                                                    f"_{name}." in v or f"_{name}/" in v):
                                                return name
                            # Tool result content
                            tr = block.get("content", "")
                            if isinstance(tr, str) and len(tr) > 20:
                                for name in agent_names:
                                    if f"— {name.title()}" in tr or f"# {name.title()}" in tr:
                                        return name

                content = _get_text_content(msg)
                if not content:
                    continue

                lower = content.lower()

                # Check for agent name mentions
                for name in agent_names:
                    # Direct address, summon prompts, orientation refs
                    if (f"hey {name}" in lower or f"{name}," in lower[:200] or
                            f"{name}." in lower[:200] or f"you are {name}" in lower or
                            f"who: {name}" in lower or
                            f"orientation/{name}" in lower or
                            f"diary/{name}" in lower or
                            f"last_thoughts_{name}" in lower or
                            f"memory/{name}" in lower or
                            f"agent_{name}" in lower or
                            f"from_agent=\"{name}\"" in lower or
                            f"from_agent='{name}'" in lower or
                            f"i'm {name}" in lower or
                            f"i am {name}" in lower or
                            f"— {name}" in lower[:200]):
                        return name

    except Exception:
        pass

    return "default"


def _extract_journal_entries(path: Path, agent: str) -> list[dict]:
    """Extract chronological journal entries from a session.

    Creates a narrative timeline: what happened, when, in what order.
    Captures user instructions, key decisions, commits, and status changes.
    More granular than knowledge extraction — this is the "what happened" log.
    """
    entries = []
    seen_ts = set()

    try:
        with open(path) as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = record.get("message", {})
                if not isinstance(msg, dict):
                    continue

                role = msg.get("role", "") or record.get("type", "")
                ts = record.get("timestamp", "")
                if not ts:
                    continue

                # Dedup by timestamp (multiple blocks can share one)
                ts_key = ts[:19]  # Truncate to second
                if ts_key in seen_ts:
                    continue

                content = _get_text_content(msg)
                if not content or len(content) < 20:
                    continue

                # User messages → task context
                if role in ("user", "human"):
                    # Skip tool results
                    content_blocks = msg.get("content", [])
                    if isinstance(content_blocks, list):
                        has_tool_result = any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content_blocks
                        )
                        if has_tool_result:
                            continue

                    seen_ts.add(ts_key)
                    entries.append({
                        "agent": agent,
                        "ts": ts,
                        "status": "active",
                        "task": f"User: {content[:200]}",
                        "finding": "",
                    })

                # Assistant substantive messages → findings
                elif role == "assistant":
                    if _is_mechanical(content):
                        continue
                    lower = content.lower()
                    has_substance = any(m in lower[:500] for m in _SUBSTANTIVE_MARKERS)
                    if has_substance and len(content) > _MIN_SUBSTANTIVE_LEN:
                        seen_ts.add(ts_key)
                        entries.append({
                            "agent": agent,
                            "ts": ts,
                            "status": "active",
                            "task": "",
                            "finding": content[:500],
                        })

                    # Check for commits in tool use blocks
                    content_blocks = msg.get("content", [])
                    if isinstance(content_blocks, list):
                        for block in content_blocks:
                            if not isinstance(block, dict):
                                continue
                            if block.get("name") == "Bash":
                                cmd = block.get("input", {}).get("command", "")
                                if "git commit" in cmd:
                                    commit_msg = _extract_commit_msg(cmd)
                                    entries.append({
                                        "agent": agent,
                                        "ts": ts,
                                        "status": "active",
                                        "task": f"Committed: {commit_msg[:200]}",
                                        "finding": "",
                                    })

    except Exception:
        pass

    return entries


def find_transcripts(project_dir: str = "") -> list[dict]:
    """Find JSONL transcript files.

    Args:
        project_dir: Optional project directory to search. If empty, searches
                     common transcript locations.

    Returns:
        List of {path, size_kb, modified} dicts, sorted newest first.
    """
    search_paths = []

    if project_dir:
        search_paths.append(Path(project_dir))
    else:
        home = Path.home()
        claude_dir = home / ".claude" / "projects"
        if claude_dir.exists():
            search_paths.append(claude_dir)

    # Skip fork artifacts — these are copies of the same session
    _skip_suffixes = {".trimmed.jsonl", ".fork-ready.jsonl", ".old.jsonl",
                      ".backup.jsonl", ".pre-fork.jsonl"}

    results = []
    for search_path in search_paths:
        for jsonl in search_path.rglob("*.jsonl"):
            # Skip fork artifacts and compressed files
            name = jsonl.name
            if any(name.endswith(s) for s in _skip_suffixes):
                continue
            if name.endswith(".gz"):
                continue
            stat = jsonl.stat()
            # Skip tiny files (< 1KB — usually empty or stubs)
            if stat.st_size < 1024:
                continue
            results.append({
                "path": str(jsonl),
                "size_kb": stat.st_size / 1024,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M"),
            })

    results.sort(key=lambda x: x["modified"], reverse=True)
    return results
