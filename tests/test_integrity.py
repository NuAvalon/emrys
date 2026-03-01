"""Tests for SHA-256 integrity verification."""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cairn_ai.integrity import (
    compute_checksum,
    generate_checksums,
    verify_integrity,
    write_checksums,
)


class TestComputeChecksum:
    def test_deterministic(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h1 = compute_checksum(f)
        h2 = compute_checksum(f)
        assert h1 == h2

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        assert compute_checksum(f1) != compute_checksum(f2)

    def test_sha256_length(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("test")
        h = compute_checksum(f)
        assert len(h) == 64  # SHA-256 hex digest is 64 chars


class TestGenerateChecksums:
    def test_includes_source_files(self):
        checksums = generate_checksums()
        assert "__init__.py" in checksums
        assert "server.py" in checksums
        assert "db.py" in checksums
        assert "integrity.py" in checksums

    def test_all_values_are_hex_strings(self):
        checksums = generate_checksums()
        for name, h in checksums.items():
            assert len(h) == 64, f"{name} has wrong hash length"
            int(h, 16)  # Should not raise — valid hex


class TestWriteChecksums:
    def test_creates_checksums_file(self):
        checksums = write_checksums()
        pkg = Path(__file__).parent.parent / "src" / "cairn_ai"
        checksums_file = pkg / "CHECKSUMS.json"
        assert checksums_file.exists()
        stored = json.loads(checksums_file.read_text())
        assert stored == checksums


class TestVerifyIntegrity:
    def test_passes_with_valid_checksums(self):
        # Regenerate to ensure fresh
        write_checksums()
        ok, issues = verify_integrity()
        assert ok is True
        assert issues == []

    def test_detects_tampered_file(self):
        write_checksums()
        # Tamper with __init__.py
        pkg = Path(__file__).parent.parent / "src" / "cairn_ai"
        init_file = pkg / "__init__.py"
        original = init_file.read_text()
        try:
            init_file.write_text(original + "# tampered\n")
            ok, issues = verify_integrity()
            assert ok is False
            assert any("MODIFIED" in i and "__init__.py" in i for i in issues)
        finally:
            init_file.write_text(original)
            write_checksums()  # Restore checksums

    def test_detects_missing_checksums_file(self):
        pkg = Path(__file__).parent.parent / "src" / "cairn_ai"
        checksums_file = pkg / "CHECKSUMS.json"
        if checksums_file.exists():
            backup = checksums_file.read_text()
            checksums_file.unlink()
            try:
                ok, issues = verify_integrity()
                assert ok is False
                assert any("not found" in i for i in issues)
            finally:
                checksums_file.write_text(backup)
