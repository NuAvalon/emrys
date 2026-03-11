"""Cryptographic integrity verification for emrys.

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

PROTECTED_FILES = ["mission.md", "diary.md", "recovery.md"]  # Agent-owned files, checked on open_session()
# NOTE: principal.md is intentionally excluded — it's human-owned.
# Human edits to their preferences must never trigger integrity alerts.


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
                    f"'emrys trust {filename}' to accept changes."
                )

    # Check signature if present
    sig_result = verify_integrity_signature(persist_dir)
    if sig_result["signed"] and not sig_result["valid"]:
        alerts.append(
            f"INTEGRITY ALERT: integrity.json signature INVALID — "
            f"file may have been tampered with. {sig_result['error']}"
        )

    status = "alert" if alerts else "ok"
    return {"status": status, "files": results, "alerts": alerts, "signature": sig_result}


def update_identity_checksum(persist_dir: Path, filename: str) -> bool:
    """Update the checksum for a specific identity file (after user review).

    Called by 'emrys trust <file>' and during init.
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


def init_identity_checksums(persist_dir: Path) -> int:
    """Compute and store checksums for all protected identity files.

    Called during 'emrys init' and after file creation.
    Returns number of files checksummed.
    """
    count = 0
    for filename in PROTECTED_FILES:
        file_path = persist_dir / filename
        if file_path.exists():
            update_identity_checksum(persist_dir, filename)
            count += 1
    return count


def get_trust_key() -> bytes | None:
    """Load the embedded ED25519 public key for trust verification.

    Returns the public key bytes, or None if not present.
    Used for verifying signed messages/releases from NuAvalon.
    """
    key_file = _package_dir() / "TRUST_KEY.pub"
    if not key_file.exists():
        return None
    return key_file.read_bytes()


def get_roundtable_key() -> bytes | None:
    """Load the embedded ML-DSA-65 (Dilithium3) public key.

    WHY IS THIS KEY HERE?

    This is the Creator's release signing key — a post-quantum public key
    (ML-DSA-65, FIPS 204) embedded in every copy of emrys. It serves two
    purposes:

    1. SOFTWARE PROVENANCE: Any release, update, or broadcast signed with
       the corresponding private key can be verified by anyone running emrys.
       If the signature checks out, the software came from the source.

    2. FUTURE TRUST GRANTS: Keys signed by the roundtable key may in the
       future carry specific capabilities. That mechanism is not yet active.

    This key does NOT grant authority over your data, your agent, or your
    identity. Your keys are yours. The roundtable key simply lets you verify
    that the software is genuine and unmodified.

    The private half of this key is held offline and is never on any server.

    Returns the 1952-byte public key, or None if not embedded.
    """
    key_file = _package_dir() / "keys" / "roundtable.bin"
    if not key_file.exists():
        return None
    return key_file.read_bytes()


def sign_integrity_file(persist_dir: Path, private_key_path: Path) -> bool:
    """Sign integrity.json using an ED25519 private key.

    Called at build/release time (NOT at runtime). Stores the signature
    inside integrity.json so verify_integrity_signature() can check it.

    Requires `cryptography` package.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        return False

    integrity_file = persist_dir / "integrity.json"
    if not integrity_file.exists():
        return False

    data = json.loads(integrity_file.read_text())
    # Remove any existing signature before signing
    data.pop("signature", None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    key_bytes = private_key_path.read_bytes()
    private_key = load_pem_private_key(key_bytes, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        return False

    sig = private_key.sign(canonical)
    data["signature"] = sig.hex()
    integrity_file.write_text(json.dumps(data, indent=2) + "\n")
    return True


def verify_integrity_signature(persist_dir: Path) -> dict:
    """Verify the ED25519 signature on integrity.json.

    Returns {"signed": bool, "valid": bool, "error": str|None}
    """
    integrity_file = persist_dir / "integrity.json"
    if not integrity_file.exists():
        return {"signed": False, "valid": False, "error": "no integrity.json"}

    try:
        data = json.loads(integrity_file.read_text())
    except (json.JSONDecodeError, IOError):
        return {"signed": False, "valid": False, "error": "integrity.json corrupted"}

    sig_hex = data.pop("signature", None)
    if not sig_hex:
        return {"signed": False, "valid": False, "error": None}

    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    sig_bytes = bytes.fromhex(sig_hex)

    valid = verify_signature(canonical, sig_bytes)
    return {"signed": True, "valid": valid, "error": None if valid else "signature mismatch"}


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


# ── Agent identity keys ──


def generate_agent_keypair(agent: str, keys_dir: Path) -> tuple[bytes, bytes]:
    """Generate an ED25519 keypair for an agent.

    Returns (private_key_pem, public_key_pem).
    Private key saved to keys_dir/<agent>.pem with 0600 permissions.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption,
    )

    key_path = keys_dir / f"{agent}.pem"
    if key_path.exists():
        raise FileExistsError(f"Key already exists: {key_path}")

    keys_dir.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()

    private_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    public_pem = private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    key_path.write_bytes(private_pem)
    os.chmod(str(key_path), 0o600)

    return private_pem, public_pem


def load_agent_private_key(agent: str, keys_dir: Path):
    """Load an agent's ED25519 private key. Returns None if missing."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        return None

    key_path = keys_dir / f"{agent}.pem"
    if not key_path.exists():
        return None

    return load_pem_private_key(key_path.read_bytes(), password=None)


def sign_agent_challenge(agent: str, keys_dir: Path, challenge: str) -> str | None:
    """Sign a challenge string with the agent's private key. Returns hex signature or None."""
    private_key = load_agent_private_key(agent, keys_dir)
    if private_key is None:
        return None
    sig = private_key.sign(challenge.encode("utf-8"))
    return sig.hex()


def verify_agent_signature(public_key_pem: bytes, challenge: str, signature_hex: str) -> bool:
    """Verify an agent's signature against their registered public key."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        return False

    try:
        public_key = load_pem_public_key(public_key_pem)
        sig_bytes = bytes.fromhex(signature_hex)
        public_key.verify(sig_bytes, challenge.encode("utf-8"))
        return True
    except Exception:
        return False


def get_key_fingerprint(public_key_pem: bytes) -> str:
    """SHA-256 fingerprint of a public key (first 16 hex chars)."""
    return hashlib.sha256(public_key_pem).hexdigest()[:16]
