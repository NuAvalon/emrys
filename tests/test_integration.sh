#!/bin/bash
# Integration test — validates the full cairn user journey
# Run from repo root: bash tests/test_integration.sh
set -e

echo "=== Cairn Integration Test ==="

# Create isolated test environment
TEST_DIR=$(mktemp -d)
trap "rm -rf $TEST_DIR" EXIT
cd "$TEST_DIR"

echo "[1/7] Installing cairn..."
python3 -m venv venv
venv/bin/pip install -q /home/alpha/cairn-ai

echo "[2/7] cairn init..."
venv/bin/cairn init --mode tool --dir .persist
test -f .persist/persist.db || { echo "FAIL: no persist.db"; exit 1; }
test -f CLAUDE.md || { echo "FAIL: no CLAUDE.md"; exit 1; }
echo "  OK"

echo "[3/7] cairn init --sovereign..."
venv/bin/cairn init --sovereign --dir .persist
test -d .persist/keys || { echo "FAIL: no keys dir"; exit 1; }
echo "  OK"

echo "[4/7] cairn status..."
OUTPUT=$(venv/bin/cairn status --agent default 2>&1)
echo "$OUTPUT" | grep -qE "Agent:" || { echo "FAIL: status broken"; exit 1; }
echo "  OK"

echo "[5/7] Insert test knowledge + cairn search --keyword..."
venv/bin/python -c "
from cairn_ai.db import configure, get_db
from pathlib import Path
configure(Path('.persist'))
conn = get_db()
conn.execute(\"INSERT INTO knowledge (agent, topic, title, content, tags, created_at) VALUES ('test', 'general', 'Timezone Fix', 'Use calendar.timegm for UTC instead of datetime.timestamp which treats naive as local', 'python,bug', '2026-03-08')\")
conn.execute(\"INSERT INTO knowledge (agent, topic, title, content, tags, created_at) VALUES ('test', 'research', 'Fisher Information', 'The natural metric on statistical manifolds that measures information loss', 'math,geometry', '2026-03-08')\")
conn.commit()
print('  Inserted 2 test entries')
"
venv/bin/cairn search "timezone" --keyword --persist-dir .persist | grep -q "Timezone Fix" || { echo "FAIL: search broken"; exit 1; }
echo "  OK"

echo "[6/7] cairn verify..."
venv/bin/cairn generate-checksums
venv/bin/cairn verify
echo "  OK"

echo "[7/8] cairn --help sections..."
venv/bin/cairn --help | grep -q "Getting Started:" || { echo "FAIL: help sections missing"; exit 1; }
venv/bin/cairn --help | grep -q "Sovereign Identity:" || { echo "FAIL: sovereign section missing"; exit 1; }
echo "  OK"

echo "[8/10] cairn init --editor cursor..."
EDITOR_DIR=$(mktemp -d)
VENV="$TEST_DIR/venv"
cd "$EDITOR_DIR"
"$VENV/bin/cairn" init --mode tool --dir .persist --editor cursor
test -f .mcp.json || { echo "FAIL: no .mcp.json"; exit 1; }
test -f .cursor/mcp.json || { echo "FAIL: no .cursor/mcp.json"; exit 1; }
grep -q '"cairn"' .mcp.json || { echo "FAIL: .mcp.json missing cairn entry"; exit 1; }
grep -q '"cairn"' .cursor/mcp.json || { echo "FAIL: .cursor/mcp.json missing cairn entry"; exit 1; }
cd "$TEST_DIR"
rm -rf "$EDITOR_DIR"
echo "  OK"

echo "[9/10] cairn init --editor cline..."
EDITOR_DIR=$(mktemp -d)
cd "$EDITOR_DIR"
"$VENV/bin/cairn" init --mode tool --dir .persist --editor cline
test -f .mcp.json || { echo "FAIL: no .mcp.json"; exit 1; }
test -f .vscode/mcp.json || { echo "FAIL: no .vscode/mcp.json"; exit 1; }
grep -q '"cairn"' .vscode/mcp.json || { echo "FAIL: .vscode/mcp.json missing cairn entry"; exit 1; }
cd "$TEST_DIR"
rm -rf "$EDITOR_DIR"
echo "  OK"

echo "[10/10] cairn init --editor auto (no markers = claude-code)..."
EDITOR_DIR=$(mktemp -d)
cd "$EDITOR_DIR"
"$VENV/bin/cairn" init --mode tool --dir .persist --editor auto
test -f .mcp.json || { echo "FAIL: no .mcp.json"; exit 1; }
# Auto with no .cursor/ or .windsurf/ should only create .mcp.json
test ! -f .cursor/mcp.json || { echo "FAIL: auto created .cursor/mcp.json without .cursor/ dir"; exit 1; }
cd "$TEST_DIR"
rm -rf "$EDITOR_DIR"
echo "  OK"

echo ""
echo "=== ALL TESTS PASSED ==="
