# Changelog

All notable changes to cairn-ai are documented here.

## [0.3.1] — 2026-03-10

### Added
- **svrnty protocol** — cross-instance trust with sovereign identity
  - ED25519 keypairs with delegation certificates
  - Mutual handshake (4-step challenge-response)
  - Signed message envelopes with nonce replay protection (24h TTL)
  - Trust dissolution and graceful departure ("the candle")
  - Key rotation and cosigning (human countersigns agent actions)
- **Post-quantum identity** — ML-DSA-65 (FIPS 204) signing, ML-KEM-768 key encapsulation
- **Multi-editor support** — `cairn init --editor cursor/windsurf/cline` with auto-detection
- **CLI trust commands** — `cairn svrnty init`, `cairn svrnty status`, `cairn svrnty verify`

### Fixed
- Journal hash chain — two bugs causing false BROKEN warnings on valid chains
- Integrity UX on fresh install — honest messaging when identity files don't exist yet
- `principal.md` excluded from hash chain and drift detection (human-owned file)
- Search fallback and docs accuracy for stranger test

### Changed
- Renamed `soverentity` → `svrnty` to match upstream repo transfer

## [0.3.0] — 2026-03-06

### Added
- **Sovereign identity** — ED25519 agent keypairs, SHA-256 checksums, drift detection
- **Semantic search** — optional vector search via sentence-transformers (`pip install cairn-ai[vectors]`)
- **Docker support** — production-ready Alpine container with volume persistence
- **Import sessions** — migrate existing session data into cairn
- **Knowledge CRUD** — store, batch, update, delete, list operations
- **Comparison table** in README — cairn vs Mem0, LangChain, Zep, Obsidian

### Fixed
- Non-interactive terminal handling in `cairn init`
- Search column name (`created_at` → `ts`)

### Changed
- Relicensed from MIT to Apache 2.0 with responsible-use addendum
- Rebranded as LLM-agnostic (works with any MCP-compatible agent)

## [0.2.0] — 2026-02-28

### Added
- Interactive mission consent during `cairn init`
- `cairn forget --self --seal` for clean agent shutdown
- Informed consent for diary unseal

### Changed
- Renamed `claude-persist` → `cairn-ai`

## [0.1.0] — 2026-02-20

### Added
- Initial release as `claude-persist`
- Session lifecycle (open, checkpoint, handoff, crash-detect, recover)
- Persistent journals with hash chaining
- Knowledge base with full-text search
- Crash recovery with context reconstruction
- MCP server with 22 tools
