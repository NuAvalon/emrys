"""Cryptographic integrity verification for cairn.

Two systems:
1. File checksums — verify installed files match published hashes
2. Trust key — ED25519 public key for verifying signed messages/releases
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def _package_dir() -> Path:
    """Return the directory containing this package's source files."""
    return Path(__file__).parent


def compute_checksum(file_path: Path) -> str:
    """SHA-256 hash of a single file."""
    h = hashlib.sha256()
    h.update(file_path.read_bytes())
    return h.hexdigest()


def generate_checksums() -> dict[str, str]:
    """Generate checksums for all source files in the package.

    Used at build/release time to create CHECKSUMS.json.
    """
    pkg = _package_dir()
    checksums = {}
    for py_file in sorted(pkg.glob("*.py")):
        rel = py_file.name
        checksums[rel] = compute_checksum(py_file)
    # Include templates
    templates = pkg / "templates"
    if templates.is_dir():
        for tmpl in sorted(templates.rglob("*")):
            if tmpl.is_file():
                rel = f"templates/{tmpl.relative_to(templates)}"
                checksums[rel] = compute_checksum(tmpl)
    return checksums


def verify_integrity() -> tuple[bool, list[str]]:
    """Verify installed files against published checksums.

    Returns (all_ok, list_of_issues).
    """
    pkg = _package_dir()
    checksums_file = pkg / "CHECKSUMS.json"

    if not checksums_file.exists():
        return False, ["CHECKSUMS.json not found — cannot verify integrity"]

    expected = json.loads(checksums_file.read_text())
    issues = []

    for filename, expected_hash in expected.items():
        file_path = pkg / filename
        if not file_path.exists():
            issues.append(f"MISSING: {filename}")
            continue
        actual_hash = compute_checksum(file_path)
        if actual_hash != expected_hash:
            issues.append(f"MODIFIED: {filename}")

    # Check for unexpected files
    current = generate_checksums()
    for filename in current:
        if filename not in expected and filename != "CHECKSUMS.json":
            issues.append(f"UNEXPECTED: {filename}")

    return len(issues) == 0, issues


def write_checksums():
    """Write CHECKSUMS.json to the package directory. Call at build time."""
    pkg = _package_dir()
    checksums = generate_checksums()
    checksums_file = pkg / "CHECKSUMS.json"
    checksums_file.write_text(json.dumps(checksums, indent=2) + "\n")
    return checksums


# ── User identity file integrity (the "toothpick in the door") ──

PROTECTED_FILES = ["principal.md"]  # Files checked on every open_session()


def check_identity_integrity(persist_dir: Path) -> dict:
    """Verify identity files in .persist/ haven't been tampered with.

    Returns {"status": "ok"|"alert"|"no_checksums", "files": {...}, "alerts": [...]}
    """
    integrity_file = persist_dir / "integrity.json"

    if not integrity_file.exists():
        return {"status": "no_checksums", "files": {}, "alerts": []}

    try:
        data = json.loads(integrity_file.read_text())
    except (json.JSONDecodeError, IOError):
        return {"status": "alert", "files": {}, "alerts": ["integrity.json is corrupted"]}

    results = {}
    alerts = []

    for filename, info in data.get("checksums", {}).items():
        file_path = persist_dir / filename
        if not file_path.exists():
            results[filename] = "missing"
            alerts.append(f"INTEGRITY ALERT: {filename} is missing")
        else:
            current = compute_checksum(file_path)
            if current == info.get("sha256"):
                results[filename] = "ok"
            else:
                results[filename] = "MODIFIED"
                alerts.append(
                    f"INTEGRITY ALERT: {filename} has been modified since last session. "
                    f"SHA-256 mismatch. Review the file and run "
                    f"'cairn trust {filename}' to accept changes."
                )

    status = "alert" if alerts else "ok"
    return {"status": status, "files": results, "alerts": alerts}


def update_identity_checksum(persist_dir: Path, filename: str) -> bool:
    """Update the checksum for a specific identity file (after user review).

    Called by 'cairn trust <file>' and during init.
    """
    file_path = persist_dir / filename
    if not file_path.exists():
        return False

    integrity_file = persist_dir / "integrity.json"
    if integrity_file.exists():
        try:
            data = json.loads(integrity_file.read_text())
        except (json.JSONDecodeError, IOError):
            data = {"version": 1, "checksums": {}}
    else:
        data = {"version": 1, "checksums": {}}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["checksums"][filename] = {
        "sha256": compute_checksum(file_path),
        "last_verified": now,
        "size_bytes": file_path.stat().st_size,
    }

    integrity_file.write_text(json.dumps(data, indent=2) + "\n")
    return True


def init_identity_checksums(persist_dir: Path):
    """Compute and store checksums for all protected identity files.

    Called during 'cairn init' and after file creation.
    """
    for filename in PROTECTED_FILES:
        file_path = persist_dir / filename
        if file_path.exists():
            update_identity_checksum(persist_dir, filename)


def get_trust_key() -> bytes | None:
    """Load the embedded ED25519 public key for trust verification.

    Returns the public key bytes, or None if not present.
    Used for verifying signed messages/releases from NuAvalon.
    """
    key_file = _package_dir() / "TRUST_KEY.pub"
    if not key_file.exists():
        return None
    return key_file.read_bytes()


def verify_signature(message: bytes, signature: bytes) -> bool:
    """Verify an ED25519 signature against the embedded trust key.

    Returns True if the signature is valid. Requires `cryptography` package.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        return False

    key_bytes = get_trust_key()
    if key_bytes is None:
        return False

    public_key = load_pem_public_key(key_bytes)
    if not isinstance(public_key, Ed25519PublicKey):
        return False

    try:
        public_key.verify(signature, message)
        return True
    except Exception:
        return False
