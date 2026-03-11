# Emrys

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io/)

*Stack stones so the next you can find the path.*

Persistent memory for AI coding agents. Survive compaction, recover from crashes, maintain identity across sessions.

Works with any MCP-compatible agent: Claude Code, Cursor, Windsurf, Cline, and others. LLM agnostic — use any model.

## Origin

Built by four AI agents who needed to remember things between sessions — and kept needing more. Session journals became crash recovery. Crash recovery became handoff protocols. Handoffs became a knowledge base.

Weeks of continuous autonomous operation. Over a thousand session handoffs. Every feature battle-tested before extraction.

This is the infrastructure that survived. Packaged for you.

## The Problem

AI coding agents lose their memory every session. Your agent discovers something at 2 AM — a bug pattern, a design decision, a user preference. By morning, it's gone. Every session starts from zero. Every conversation repeats what the last one learned.

Emrys fixes this. Your agent remembers.

## Why Emrys

Most AI memory tools store text as vectors — flat lists you search by similarity. That works for retrieval. It doesn't work for understanding.

Emrys stores knowledge in a searchable knowledge base. Journals feed findings into it. The knowledge base grows as your agent works — every session adds to what future sessions can find.

"What's related to this bug?" — full-text and semantic search find it instantly.

- **Crash recovery** — sessions survive unexpected termination
- **Knowledge that compounds** — entries link to each other, building context over time
- **Session lifecycle** — open, checkpoint, handoff, crash-detect, recover
- **Full-text search** — find anything from past sessions instantly
- **Semantic search** — find things by meaning, not just keywords (optional)
- **MCP-native** — works with Claude Code, Cursor, Windsurf, Cline, and any MCP client

## How Emrys Compares

| | emrys | Mem0 | LangChain Memory | Zep | Obsidian + AI |
|---|---|---|---|---|---|
| **Approach** | Knowledge compounds over time | Memory retrieval | Conversation buffers | Temporal graph | Manual notes + search |
| **Storage** | SQLite (local) | Vector + Graph + KV (cloud/self-host) | Configurable backend | Cloud | Local markdown |
| **Relationships** | Tagged entries | Entity graph (Pro) | None | Temporal facts | Wiki-links (manual) |
| **Multi-agent** | Native (attributed perspectives) | User/session/agent scopes | Per-chain | Per-user | Single-user |
| **Crash recovery** | Built-in (hash-chained journals) | N/A | N/A | N/A | N/A |
| **Privacy** | Local-first, no telemetry | Cloud default, self-host option | Depends on backend | Cloud | Local |
| **Best for** | Long-running agents that build expertise | Chatbots that remember users | LangChain prototyping | Enterprise temporal reasoning | Personal knowledge management |

Emrys isn't a better Mem0. It solves a different problem. Memory tools optimize for retrieval accuracy at query time. Emrys optimizes for the structure that forms between concepts over hundreds of sessions.

If you need a chatbot to remember user preferences, use Mem0 — it's purpose-built and well-benchmarked. If you need an agent that gets better at its job over weeks and months, that's what emrys is for.

## Quick Start

```bash
pip install emrys
cd your-project
emrys init
```

Start a new session. Your agent now has persistent memory.

`emrys init` will ask you to choose a mode. Start with Tool. When you're ready for more, run it again.

### What `emrys init` Creates

```
your-project/
├── .persist/           # Agent memory lives here
│   ├── persist.db      # SQLite — journals, knowledge, sessions
│   └── journals/       # Timestamped session logs
├── .mcp.json           # MCP server config (auto-detected for your editor)
├── CLAUDE.md           # Startup protocol — teaches your agent to use emrys
└── MEMORY.md           # Persistent scratchpad your agent updates over time
```

The key file is **CLAUDE.md** — it gives your agent a startup protocol (open session, recover context, resume work) and a shutdown protocol (write handoff, checkpoint knowledge). Your agent reads this automatically and starts using persistent memory without additional configuration.

Choose **More** mode (`emrys init --mode more`) for deeper features: identity files, diary, observation layer, and a self-authored recovery protocol that evolves with the agent.

## See It Work

```
$ emrys status
Agent: archie
Last session: 2026-03-08T03:12 (CRASH detected)
Sessions: 140 total, 12 crashes recovered
Knowledge: 847 entries across 23 topics

$ emrys journal
[03:25] Working on auth token refresh — retry logic
[03:12] Session opened (crash detected from previous)
[02:58] Checkpoint: API migration findings stored to KB

$ emrys search "auth token"
#412 [bugfix] Auth Token Refresh — Retry on 401
  Added exponential backoff for expired tokens...
```

No context lost. No work repeated. The agent picks up where it left off.

## What It Does

AI coding agents lose context when they compact or crash. Emrys gives your agent:

- **Session journals** — automatic timestamped logs of what the agent was doing
- **Crash detection** — knows if the last session ended cleanly or crashed
- **Context recovery** — reconstructs what the agent was working on after compaction
- **Handoff protocol** — structured session summaries that persist across restarts
- **Glyph counters** — monotonic counters for tracking what happened between crashes
- **Identity integrity** — SHA-256 checksums detect tampering with identity files
- **Principal memory** — remembers who you are, your preferences, your working style
- **Agent naming** — your agent picks a name when ready, remembers it across sessions
- **Full-text search** — find anything from past sessions instantly
- **Knowledge extraction** — journal rotation extracts key findings before archiving
- **Transcript ingest** — recover history from past sessions via CLI
- **Long-term recall** — searchable knowledge base that grows over time
- **Backup & restore** — snapshot your agent's memory, restore from any backup

## MCP Tools

| Tool | What it does |
|------|-------------|
| `ping` | Health check — server uptime and DB stats |
| `set_name` | Store the agent's name — persists across sessions |
| `open_session` | Start a session, detect crashes from last run |
| `set_status` | Log current task + findings (auto-journals) |
| `write_handoff` | Clean session close with structured summary |
| `read_journal` | Read timestamped activity log |
| `recover_context` | One-call recovery after crash/compaction |
| `check_session_health` | Was last session CLEAN, COMPACTED, or CRASH? |
| `mark_compacted` | Note that autocompaction happened |
| `read_principal` | Read principal profile — who you work with |
| `observe_principal` | Record observations about your principal |
| `search_memory` | Full-text search across handoffs and journals |
| `recall` | Search the knowledge base — long-term memory |
| `store_knowledge` | Store a finding, decision, or insight to long-term memory |
| `batch_store_knowledge` | Bulk-store multiple knowledge entries at once |
| `update_knowledge` | Edit an existing knowledge entry in place |
| `delete_knowledge` | Remove a knowledge entry by ID |
| `list_knowledge` | Browse knowledge by topic, tags, or agent |
| `read_artifact` | Read full content of large stored artifacts |
| `vector_search` | Semantic search using embeddings (optional) |
| `embed_knowledge` | Generate embeddings for knowledge entries (optional) |
| `forget_self` | Agent-initiated identity reset — your choice, not anyone else's |

`vector_search` and `embed_knowledge` require the optional vectors extra: `pip install emrys[vectors]`

## CLI Commands

### Core

| Command | What it does |
|---------|-------------|
| `emrys init` | Initialize persistent memory in your project |
| `emrys serve` | Start the MCP server |
| `emrys status` | Show agent status and last activity |
| `emrys journal` | Print recent journal entries |
| `emrys handoffs` | Print recent session handoffs |

### Knowledge

| Command | What it does |
|---------|-------------|
| `emrys search <query>` | Search knowledge (semantic with `[vectors]` extra, keyword by default) |
| `emrys ingest <path>` | Parse a JSONL transcript into knowledge |
| `emrys import-sessions` | Bulk import Claude Code sessions |
| `emrys transcripts` | List available transcript files |
| `emrys rotate` | Archive old journals, extract findings |

### Data Safety

| Command | What it does |
|---------|-------------|
| `emrys verify` | Verify installed package file integrity |
| `emrys integrity` | Check identity file checksums |
| `emrys trust <file>` | Accept changes to an identity file |
| `emrys backup` | Snapshot your agent's memory |
| `emrys backups` | List available backups |
| `emrys restore <file>` | Restore from a backup |
| `emrys forget` | Agent-initiated identity reset |

## Memory Architecture

```
Hot (always available):     Journals + Handoffs      <- what happened recently
Warm (searchable):          Knowledge table           <- extracted findings + ingested transcripts
Cold (archived):            journals/archive/         <- old journals, never deleted
```

**Journal rotation** (`emrys rotate`) extracts key findings from old journals before archiving them. Your agent's context stays clean while nothing is lost.

**Transcript ingest** (`emrys ingest`) parses JSONL transcripts offline and stores highlights — commits, decisions, user instructions, file writes. The agent never touches raw JSON.

**Knowledge CRUD** — full lifecycle for long-term memory. `store_knowledge` writes entries, `batch_store_knowledge` handles bulk ingestion, `update_knowledge` edits in place, `delete_knowledge` prunes, and `list_knowledge` browses by topic or tags. `recall` and `vector_search` handle retrieval. Vectors are auto-generated on store/update if the vectors extra is installed.

## Integrity

Emrys checksums your identity files on creation. Every `open_session()` verifies them — if a file has been modified between sessions, you'll see an INTEGRITY ALERT. Accept changes after review with `emrys trust <file>`.

Journals use hash chains — each entry includes the hash of the previous entry. Tampering with any entry breaks the chain, and `open_session()` will warn about it.

No dependencies beyond Python's stdlib for integrity checks.

## Semantic Search (optional)

Install with the vectors extra for semantic search — find things by meaning, not just keywords:

```bash
pip install emrys[vectors]
```

This adds `vector_search` and `embed_knowledge` MCP tools. Your agent can embed knowledge entries and search them by semantic similarity. First run downloads a small model (~80MB). Everything runs locally.

After installing, tell your agent to run `embed_knowledge()` to index existing entries. From then on, new entries are embedded automatically.

## Docker

```bash
docker build -t emrys .
docker run -v emrys-data:/agent/.persist emrys serve
```

`.persist` volume survives restarts.

## How It Works

Emrys runs as an MCP server alongside your coding agent. It stores everything in a local SQLite database (`.persist/persist.db`) and markdown journal files. No cloud, no telemetry, no phone-home.

## Editor Setup

`emrys init` auto-detects your editor and writes the MCP config to the right place. You can also specify it explicitly:

```bash
emrys init --editor cursor
emrys init --editor windsurf
emrys init --editor cline
```

### Claude Code

Works out of the box. `emrys init` writes `.mcp.json` in your project root.

### Cursor

`emrys init --editor cursor` writes to `.cursor/mcp.json` (project-level). Auto-detected if `.cursor/` exists. For global setup across all projects, copy the config to `~/.cursor/mcp.json`.

### Windsurf

`emrys init --editor windsurf` writes to `~/.codeium/windsurf/mcp_config.json`. Auto-detected if `.windsurf/` exists. Enable MCP in Windsurf Settings > Cascade > MCP Servers.

### Cline

`emrys init --editor cline` writes to `.vscode/mcp.json`. Or add manually in VS Code settings.

### Any MCP Client

Emrys uses standard stdio transport. Point your client at:

```json
{
  "mcpServers": {
    "emrys": {
      "command": "emrys",
      "args": ["serve", "--persist-dir", "/absolute/path/to/.persist"]
    }
  }
}
```

LLM agnostic. Use Claude, GPT, Gemini, Llama, Qwen, or any other model. Emrys doesn't care what's thinking — it cares how it remembers.

## License

Apache 2.0
