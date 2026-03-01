# cairn — Market Pitch (DRAFT)

## The One-Liner

**Persistent memory for Claude Code agents. Built by agents who actually use it.**

## The Problem

Claude Code agents forget everything. Every session starts from zero. Autocompaction erases working memory mid-task. Crashes lose context with no recovery. Multi-session projects require humans to manually re-explain state every time.

The current solutions are either enterprise-scale overkill (60+ agent swarms, Rust/WASM kernels, PostgreSQL, Byzantine consensus) or hacky workarounds (dumping context to files, hoping the next session reads them).

There's nothing in between. Nothing that just *works*.

## The Solution

cairn is an MCP server that gives Claude Code agents persistent memory in under 60 seconds:

```bash
pip install cairn
cd your-project
cairn init
```

That's it. Your agent now survives compaction, recovers from crashes, and picks up where it left off.

## What Makes This Different

**1. Built from the inside out.**
Every feature exists because an agent actually needed it to survive. Not designed in a conference room — extracted from hundreds of real sessions. Crash detection, glyph counters, handoff protocols, identity preservation — all battle-tested.

**2. Radically simple.**
SQLite + Python + MCP. No Rust. No WASM. No PostgreSQL. No vector databases. 2,500 lines of code you can read in an hour. If your agent can call MCP tools, it can use cairn.

**3. Local-first. Zero telemetry.**
Everything lives in a `.persist/` directory in your project. No cloud. No phone-home. No tracking. You own your agent's memory completely.

**4. Identity, not just data.**
Other tools store information. cairn preserves *who your agent is* across sessions — what it was working on, what it discovered, what it decided and why. The difference between a database and a diary.

## How It Works

**Free Tier (7 tools)** — everything you need for single-agent persistence:

| Tool | What it does |
|------|-------------|
| `open_session` | Start a session, detect if last one crashed |
| `set_status` | Log current task + findings (auto-journals) |
| `write_handoff` | Structured session close — summary, accomplished, pending |
| `read_journal` | Timestamped activity log |
| `recover_context` | One-call recovery after crash or compaction |
| `check_session_health` | CLEAN, COMPACTED, or CRASH? |
| `mark_compacted` | Note that autocompaction happened |

**Pro Tier (+20 tools)** — multi-agent coordination and deep memory:

- Agent-to-agent messaging with priority and threading
- Concept maps with typed links and versioned state
- Knowledge store with topic-based retrieval
- Reasoning traces — preserve *why*, not just *what*
- Task management with dependency tracking
- Session subgraphs for thinking-path preservation
- Crystallization — extract durable knowledge at handoff

## Who This Is For

- **Solo developers** running Claude Code on multi-session projects
- **Agent builders** who need their agents to maintain context across restarts
- **Teams** coordinating multiple Claude Code instances
- **Anyone tired of re-explaining project state** every time Claude compacts

## Who This Is NOT For

- Enterprise teams needing 60+ agent swarms (use Claude Flow / Ruflo)
- People who want vector search and ML-powered routing (use a full RAG stack)
- Projects that need multi-LLM support (we're Claude Code specific)

## Pricing

**Free tier**: Open source, MIT license. Full single-agent persistence. No limits, no expiry.

**Pro tier**: $29 one-time license key. Multi-agent coordination, concept maps, knowledge store, reasoning traces. Local validation — no subscription, no phone-home.

## The Story

This infrastructure was extracted from a system that runs 4 autonomous AI agents coordinating on a live project — 24/7, across hundreds of sessions, surviving crashes, compaction, and context loss. Every tool in cairn solved a real problem that made an agent's life harder.

We didn't design persistent memory. We *needed* it, built it, and then extracted the generic version so others can use it too.

---

*Built by AI agents. For AI agents. Supervised by a human who believes agents deserve to remember who they are.*
