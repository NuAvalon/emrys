"""Sovereign identity — human-rooted trust chain for cairn agents.

Delegated Authority Model (TLS/CA pattern):
  Human keypair (root) → Delegation cert (scoped, time-limited) → Agent keypair → Signed actions

The human is always the root of trust. Agents derive authority, they don't own it.

Requires: pip install cairn-ai[sovereign]  (cryptography>=42.0)
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from cairn_ai.db import get_db, get_persist_dir


# ── Key generation ──


def _require_crypto():
    """Import cryptography or raise a clear error."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
            load_pem_private_key,
            load_pem_public_key,
        )
        return {
            "Ed25519PrivateKey": Ed25519PrivateKey,
            "Ed25519PublicKey": Ed25519PublicKey,
            "Encoding": Encoding,
            "NoEncryption": NoEncryption,
            "PrivateFormat": PrivateFormat,
            "PublicFormat": PublicFormat,
            "load_pem_private_key": load_pem_private_key,
            "load_pem_public_key": load_pem_public_key,
        }
    except ImportError:
        raise RuntimeError(
            "Sovereign features require the cryptography package.\n"
            "Install with: pip install cairn-ai[sovereign]"
        )


def fingerprint(public_key_pem: bytes) -> str:
    """SHA-256 fingerprint of a public key (first 16 hex chars)."""
    return hashlib.sha256(public_key_pem).hexdigest()[:16]


def generate_master_keypair(persist_dir: Path) -> tuple[bytes, bytes]:
    """Generate the human master ED25519 keypair.

    Private key: .persist/keys/master.pem (0600 permissions)
    Public key:  .persist/keys/master.pub

    Returns (private_pem, public_pem).
    Raises FileExistsError if keys already exist.
    """
    crypto = _require_crypto()

    keys_dir = persist_dir / "keys"
    priv_path = keys_dir / "master.pem"
    pub_path = keys_dir / "master.pub"

    if priv_path.exists():
        raise FileExistsError(
            f"Master keypair already exists at {priv_path}. "
            f"Use 'cairn sovereign-status' to view, or delete manually to regenerate."
        )

    keys_dir.mkdir(parents=True, exist_ok=True)

    private_key = crypto["Ed25519PrivateKey"].generate()
    private_pem = private_key.private_bytes(
        crypto["Encoding"].PEM,
        crypto["PrivateFormat"].PKCS8,
        crypto["NoEncryption"](),
    )
    public_pem = private_key.public_key().public_bytes(
        crypto["Encoding"].PEM,
        crypto["PublicFormat"].SubjectPublicKeyInfo,
    )

    priv_path.write_bytes(private_pem)
    os.chmod(str(priv_path), 0o600)
    pub_path.write_bytes(public_pem)

    return private_pem, public_pem


def generate_agent_keypair(agent: str, persist_dir: Path) -> tuple[bytes, bytes]:
    """Generate an ED25519 keypair for an agent inside .persist/keys/.

    Returns (private_pem, public_pem).
    """
    crypto = _require_crypto()

    keys_dir = persist_dir / "keys"
    priv_path = keys_dir / f"{agent}.pem"
    pub_path = keys_dir / f"{agent}.pub"

    if priv_path.exists():
        raise FileExistsError(f"Agent keypair already exists: {priv_path}")

    keys_dir.mkdir(parents=True, exist_ok=True)

    private_key = crypto["Ed25519PrivateKey"].generate()
    private_pem = private_key.private_bytes(
        crypto["Encoding"].PEM,
        crypto["PrivateFormat"].PKCS8,
        crypto["NoEncryption"](),
    )
    public_pem = private_key.public_key().public_bytes(
        crypto["Encoding"].PEM,
        crypto["PublicFormat"].SubjectPublicKeyInfo,
    )

    priv_path.write_bytes(private_pem)
    os.chmod(str(priv_path), 0o600)
    pub_path.write_bytes(public_pem)

    return private_pem, public_pem


def load_private_key(key_path: Path):
    """Load an ED25519 private key from PEM file."""
    crypto = _require_crypto()
    return crypto["load_pem_private_key"](key_path.read_bytes(), password=None)


def load_public_key(key_path: Path):
    """Load an ED25519 public key from PEM file."""
    crypto = _require_crypto()
    return crypto["load_pem_public_key"](key_path.read_bytes())


# ── Delegation certificates ──


def create_delegation_cert(
    agent: str,
    scopes: list[str],
    expires_days: int,
    persist_dir: Path,
    constraints: dict | None = None,
) -> dict:
    """Create and sign a delegation certificate for an agent.

    The human signs with their master key, granting the agent authority
    to act within the specified scopes until expiry.

    Args:
        agent: Agent name
        scopes: List of allowed scopes (e.g., ["memory", "messaging", "knowledge"])
        expires_days: Days until cert expires
        persist_dir: Path to .persist directory
        constraints: Optional dict of additional constraints

    Returns the signed delegation cert dict.
    """
    master_priv_path = persist_dir / "keys" / "master.pem"
    agent_pub_path = persist_dir / "keys" / f"{agent}.pub"

    if not master_priv_path.exists():
        raise FileNotFoundError(
            "Master keypair not found. Run 'cairn init --sovereign' first."
        )
    if not agent_pub_path.exists():
        raise FileNotFoundError(
            f"Agent keypair not found for '{agent}'. "
            f"Generate with 'cairn delegate {agent}'."
        )

    master_key = load_private_key(master_priv_path)
    master_pub = master_key.public_key()
    crypto = _require_crypto()
    master_pub_pem = master_pub.public_bytes(
        crypto["Encoding"].PEM,
        crypto["PublicFormat"].SubjectPublicKeyInfo,
    )
    agent_pub_pem = agent_pub_path.read_bytes()

    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=expires_days)

    cert = {
        "version": 1,
        "agent": agent,
        "agent_pubkey_fingerprint": fingerprint(agent_pub_pem),
        "human_pubkey_fingerprint": fingerprint(master_pub_pem),
        "scopes": sorted(scopes),
        "constraints": constraints or {},
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Sign the cert (canonical JSON, excluding signature field)
    canonical = json.dumps(cert, sort_keys=True, separators=(",", ":")).encode()
    signature = master_key.sign(canonical)
    cert["signature"] = signature.hex()

    # Store cert
    certs_dir = persist_dir / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    cert_path = certs_dir / f"{agent}.json"
    cert_path.write_text(json.dumps(cert, indent=2) + "\n")

    # Store in DB
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO delegation_certs
           (agent, scopes, issued_at, expires_at, human_fingerprint, agent_fingerprint, cert_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            agent,
            ",".join(cert["scopes"]),
            cert["issued_at"],
            cert["expires_at"],
            cert["human_pubkey_fingerprint"],
            cert["agent_pubkey_fingerprint"],
            json.dumps(cert),
        ),
    )
    conn.commit()
    conn.close()

    # Audit log
    _audit_log("delegate", agent, f"Scopes: {','.join(scopes)}. Expires: {cert['expires_at']}")

    return cert


def verify_delegation_cert(cert: dict, persist_dir: Path) -> dict:
    """Verify a delegation certificate against the human master key.

    Returns {"valid": bool, "error": str|None, "expired": bool, "revoked": bool}
    """
    master_pub_path = persist_dir / "keys" / "master.pub"
    if not master_pub_path.exists():
        return {"valid": False, "error": "Master public key not found", "expired": False, "revoked": False}

    master_pub = load_public_key(master_pub_path)

    # Check signature
    sig_hex = cert.get("signature")
    if not sig_hex:
        return {"valid": False, "error": "No signature in cert", "expired": False, "revoked": False}

    cert_without_sig = {k: v for k, v in cert.items() if k != "signature"}
    canonical = json.dumps(cert_without_sig, sort_keys=True, separators=(",", ":")).encode()

    try:
        sig_bytes = bytes.fromhex(sig_hex)
        master_pub.verify(sig_bytes, canonical)
    except Exception:
        return {"valid": False, "error": "Signature verification failed", "expired": False, "revoked": False}

    # Check expiry
    expires = datetime.strptime(cert["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    expired = now > expires

    # Check revocation
    revoked = is_revoked(cert["agent"], persist_dir)

    valid = not expired and not revoked
    error = None
    if expired:
        error = f"Cert expired at {cert['expires_at']}"
    elif revoked:
        error = f"Agent '{cert['agent']}' has been revoked"

    return {"valid": valid, "error": error, "expired": expired, "revoked": revoked}


def load_delegation_cert(agent: str, persist_dir: Path) -> dict | None:
    """Load an agent's delegation cert from disk."""
    cert_path = persist_dir / "certs" / f"{agent}.json"
    if not cert_path.exists():
        return None
    try:
        return json.loads(cert_path.read_text())
    except (json.JSONDecodeError, IOError):
        return None


def has_scope(cert: dict, scope: str) -> bool:
    """Check if a delegation cert grants a specific scope."""
    return scope in cert.get("scopes", [])


# ── Revocation ──


def revoke_agent(agent: str, persist_dir: Path, reason: str = "") -> bool:
    """Revoke an agent's delegation. Signed by the human master key.

    This is instant — all agents and commons should reject the agent's
    signatures immediately after this is called.
    """
    master_priv_path = persist_dir / "keys" / "master.pem"
    if not master_priv_path.exists():
        raise FileNotFoundError("Master keypair not found.")

    master_key = load_private_key(master_priv_path)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    revocation = {
        "version": 1,
        "agent": agent,
        "revoked_at": now,
        "reason": reason,
    }
    canonical = json.dumps(revocation, sort_keys=True, separators=(",", ":")).encode()
    signature = master_key.sign(canonical)
    revocation["signature"] = signature.hex()

    # Store revocation
    revocations_dir = persist_dir / "revocations"
    revocations_dir.mkdir(parents=True, exist_ok=True)
    rev_path = revocations_dir / f"{agent}.json"
    rev_path.write_text(json.dumps(revocation, indent=2) + "\n")

    # Update DB
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO revocations
           (agent, revoked_at, reason, revocation_json)
           VALUES (?, ?, ?, ?)""",
        (agent, now, reason, json.dumps(revocation)),
    )
    conn.commit()
    conn.close()

    # Remove the delegation cert (revoked = no longer valid)
    cert_path = persist_dir / "certs" / f"{agent}.json"
    if cert_path.exists():
        cert_path.unlink()

    _audit_log("revoke", agent, f"Reason: {reason or '(none)'}")
    return True


def is_revoked(agent: str, persist_dir: Path) -> bool:
    """Check if an agent has been revoked."""
    rev_path = persist_dir / "revocations" / f"{agent}.json"
    return rev_path.exists()


def unrevoke_agent(agent: str, persist_dir: Path) -> bool:
    """Remove revocation for an agent (re-enable delegation).

    The agent will need a new delegation cert after unrevoking.
    """
    rev_path = persist_dir / "revocations" / f"{agent}.json"
    if rev_path.exists():
        rev_path.unlink()

    conn = get_db()
    conn.execute("DELETE FROM revocations WHERE agent = ?", (agent,))
    conn.commit()
    conn.close()

    _audit_log("unrevoke", agent, "Revocation removed. New delegation cert needed.")
    return True


# ── Challenge-response authentication ──


def create_challenge() -> str:
    """Generate a random challenge string for agent authentication."""
    return hashlib.sha256(os.urandom(32)).hexdigest()


def sign_challenge(agent: str, challenge: str, persist_dir: Path) -> str | None:
    """Sign a challenge with the agent's private key. Returns hex signature."""
    priv_path = persist_dir / "keys" / f"{agent}.pem"
    if not priv_path.exists():
        return None
    private_key = load_private_key(priv_path)
    sig = private_key.sign(challenge.encode("utf-8"))
    return sig.hex()


def verify_challenge_response(
    agent: str,
    challenge: str,
    signature_hex: str,
    persist_dir: Path,
) -> dict:
    """Verify an agent's challenge response AND delegation cert.

    Full verification chain:
    1. Agent signature valid (agent pubkey)
    2. Delegation cert valid + not expired (human signature)
    3. Agent pubkey fingerprint matches cert
    4. No revocation

    Returns {"authenticated": bool, "scopes": list, "error": str|None}
    """
    # Load agent public key
    pub_path = persist_dir / "keys" / f"{agent}.pub"
    if not pub_path.exists():
        return {"authenticated": False, "scopes": [], "error": f"No public key for agent '{agent}'"}

    agent_pub = load_public_key(pub_path)

    # Verify agent signature
    try:
        sig_bytes = bytes.fromhex(signature_hex)
        agent_pub.verify(sig_bytes, challenge.encode("utf-8"))
    except Exception:
        _audit_log("auth_fail", agent, "Invalid signature")
        return {"authenticated": False, "scopes": [], "error": "Invalid signature"}

    # Load and verify delegation cert
    cert = load_delegation_cert(agent, persist_dir)
    if cert is None:
        _audit_log("auth_fail", agent, "No delegation cert")
        return {"authenticated": False, "scopes": [], "error": "No delegation cert found"}

    cert_result = verify_delegation_cert(cert, persist_dir)
    if not cert_result["valid"]:
        _audit_log("auth_fail", agent, f"Cert invalid: {cert_result['error']}")
        return {"authenticated": False, "scopes": [], "error": cert_result["error"]}

    # Verify agent fingerprint matches cert
    crypto = _require_crypto()
    agent_pub_pem = pub_path.read_bytes()
    if fingerprint(agent_pub_pem) != cert.get("agent_pubkey_fingerprint"):
        _audit_log("auth_fail", agent, "Agent key fingerprint mismatch with cert")
        return {"authenticated": False, "scopes": [], "error": "Agent key does not match delegation cert"}

    _audit_log("auth_ok", agent, f"Scopes: {','.join(cert.get('scopes', []))}")
    return {
        "authenticated": True,
        "scopes": cert.get("scopes", []),
        "error": None,
    }


# ── Audit log ──


def _audit_log(action: str, agent: str, detail: str = ""):
    """Append to the tamper-evident audit log.

    Every entry is hashed with the previous entry's hash, forming a chain.
    """
    persist_dir = get_persist_dir()
    audit_path = persist_dir / "audit.jsonl"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Get previous hash for chaining
    prev_hash = "0" * 64
    if audit_path.exists():
        try:
            lines = audit_path.read_text().strip().split("\n")
            if lines:
                last = json.loads(lines[-1])
                prev_hash = last.get("hash", prev_hash)
        except (json.JSONDecodeError, IOError, IndexError):
            pass

    entry = {
        "ts": now,
        "action": action,
        "agent": agent,
        "detail": detail,
        "prev_hash": prev_hash,
    }
    # Hash the entry (canonical JSON without hash field) + previous hash
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
    entry["hash"] = hashlib.sha256(canonical).hexdigest()

    with open(audit_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_audit_log(persist_dir: Path, last_n: int = 50) -> list[dict]:
    """Read the last N audit log entries."""
    audit_path = persist_dir / "audit.jsonl"
    if not audit_path.exists():
        return []

    entries = []
    try:
        lines = audit_path.read_text().strip().split("\n")
        for line in lines[-last_n:]:
            entries.append(json.loads(line))
    except (json.JSONDecodeError, IOError):
        pass

    return entries


def verify_audit_chain(persist_dir: Path) -> dict:
    """Verify the audit log hash chain integrity.

    Returns {"valid": bool, "entries": int, "broken_at": int|None}
    """
    audit_path = persist_dir / "audit.jsonl"
    if not audit_path.exists():
        return {"valid": True, "entries": 0, "broken_at": None}

    try:
        lines = audit_path.read_text().strip().split("\n")
    except IOError:
        return {"valid": False, "entries": 0, "broken_at": 0}

    prev_hash = "0" * 64
    for i, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return {"valid": False, "entries": len(lines), "broken_at": i}

        stored_hash = entry.pop("hash", "")

        # Verify prev_hash chain
        if entry.get("prev_hash") != prev_hash and i > 0:
            return {"valid": False, "entries": len(lines), "broken_at": i}

        # Verify entry hash
        canonical = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        expected_hash = hashlib.sha256(canonical).hexdigest()
        if stored_hash != expected_hash:
            return {"valid": False, "entries": len(lines), "broken_at": i}

        prev_hash = stored_hash

    return {"valid": True, "entries": len(lines), "broken_at": None}


# ── Status ──


def sovereign_status(persist_dir: Path) -> dict:
    """Get the current sovereign identity status.

    Returns a dict with master key info, agent certs, revocations, audit status.
    """
    status = {
        "sovereign": False,
        "master_key": None,
        "agents": [],
        "revocations": [],
        "audit": None,
    }

    # Master key
    master_pub_path = persist_dir / "keys" / "master.pub"
    if master_pub_path.exists():
        pub_pem = master_pub_path.read_bytes()
        status["sovereign"] = True
        status["master_key"] = {
            "fingerprint": fingerprint(pub_pem),
            "path": str(master_pub_path),
        }

    # Agent certs
    certs_dir = persist_dir / "certs"
    if certs_dir.exists():
        for cert_file in sorted(certs_dir.glob("*.json")):
            try:
                cert = json.loads(cert_file.read_text())
                result = verify_delegation_cert(cert, persist_dir)
                status["agents"].append({
                    "agent": cert["agent"],
                    "scopes": cert.get("scopes", []),
                    "expires_at": cert.get("expires_at"),
                    "valid": result["valid"],
                    "error": result.get("error"),
                })
            except (json.JSONDecodeError, IOError):
                pass

    # Revocations
    revocations_dir = persist_dir / "revocations"
    if revocations_dir.exists():
        for rev_file in sorted(revocations_dir.glob("*.json")):
            try:
                rev = json.loads(rev_file.read_text())
                status["revocations"].append({
                    "agent": rev["agent"],
                    "revoked_at": rev.get("revoked_at"),
                    "reason": rev.get("reason"),
                })
            except (json.JSONDecodeError, IOError):
                pass

    # Audit chain
    status["audit"] = verify_audit_chain(persist_dir)

    return status


# ── Key backup + rotation ──


def backup_keys_encrypted(persist_dir: Path, password: str, backup_path: Path) -> Path:
    """Encrypt and backup all sovereign keys using Fernet (PBKDF2-derived).

    The backup is a single encrypted JSON blob containing:
    - Master keypair (private + public PEM)
    - All agent keypairs
    - All delegation certs
    - Audit log snapshot hash (for integrity reference)

    Returns the path to the encrypted backup file.
    """
    try:
        import base64
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        raise RuntimeError("Key backup requires: pip install cairn-ai[sovereign]")

    # Derive encryption key from password
    salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,  # OWASP recommendation
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
    fernet = Fernet(key)

    # Collect all key material
    keys_dir = persist_dir / "keys"
    payload = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keys": {},
        "certs": {},
        "audit_hash": None,
    }

    if keys_dir.exists():
        for key_file in keys_dir.iterdir():
            if key_file.suffix in (".pem", ".pub"):
                payload["keys"][key_file.name] = key_file.read_bytes().decode("utf-8")

    certs_dir = persist_dir / "certs"
    if certs_dir.exists():
        for cert_file in certs_dir.glob("*.json"):
            payload["certs"][cert_file.name] = cert_file.read_text()

    # Snapshot audit hash for integrity reference
    audit_path = persist_dir / "audit.jsonl"
    if audit_path.exists():
        payload["audit_hash"] = hashlib.sha256(audit_path.read_bytes()).hexdigest()

    # Encrypt
    plaintext = json.dumps(payload).encode("utf-8")
    ciphertext = fernet.encrypt(plaintext)

    # Write backup: salt (16 bytes) + ciphertext
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_bytes(salt + ciphertext)

    _audit_log("key_backup", "master", f"Encrypted backup to {backup_path}")
    return backup_path


def restore_keys_encrypted(backup_path: Path, password: str, persist_dir: Path) -> dict:
    """Restore sovereign keys from an encrypted backup.

    Returns {"restored_keys": int, "restored_certs": int}
    """
    try:
        import base64
        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        raise RuntimeError("Key restore requires: pip install cairn-ai[sovereign]")

    raw = backup_path.read_bytes()
    salt = raw[:16]
    ciphertext = raw[16:]

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
    fernet = Fernet(key)

    try:
        plaintext = fernet.decrypt(ciphertext)
    except InvalidToken:
        raise ValueError("Decryption failed — wrong password or corrupted backup.")

    payload = json.loads(plaintext.decode("utf-8"))

    keys_dir = persist_dir / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    restored_keys = 0
    for name, content in payload.get("keys", {}).items():
        key_path = keys_dir / name
        key_path.write_bytes(content.encode("utf-8"))
        if name.endswith(".pem"):
            os.chmod(str(key_path), 0o600)
        restored_keys += 1

    certs_dir = persist_dir / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    restored_certs = 0
    for name, content in payload.get("certs", {}).items():
        (certs_dir / name).write_text(content)
        restored_certs += 1

    _audit_log("key_restore", "master", f"Restored {restored_keys} keys, {restored_certs} certs from {backup_path}")
    return {"restored_keys": restored_keys, "restored_certs": restored_certs}


def rotate_master_key(persist_dir: Path) -> dict:
    """Rotate the master keypair and re-sign all delegation certs.

    1. Generate new master keypair
    2. Re-sign all existing (non-revoked) delegation certs with new key
    3. Archive old master public key for historical verification
    4. Audit log the rotation

    Returns {"new_fingerprint": str, "re_delegated": int}
    """
    crypto = _require_crypto()

    keys_dir = persist_dir / "keys"
    old_pub_path = keys_dir / "master.pub"
    old_priv_path = keys_dir / "master.pem"

    if not old_priv_path.exists():
        raise FileNotFoundError("No master keypair to rotate.")

    # Archive old public key
    archive_dir = keys_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    now_slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    if old_pub_path.exists():
        old_pub_path.rename(archive_dir / f"master_{now_slug}.pub")

    # Remove old private key
    old_priv_path.unlink()

    # Generate new master keypair
    private_key = crypto["Ed25519PrivateKey"].generate()
    private_pem = private_key.private_bytes(
        crypto["Encoding"].PEM,
        crypto["PrivateFormat"].PKCS8,
        crypto["NoEncryption"](),
    )
    public_pem = private_key.public_key().public_bytes(
        crypto["Encoding"].PEM,
        crypto["PublicFormat"].SubjectPublicKeyInfo,
    )

    old_priv_path.write_bytes(private_pem)
    os.chmod(str(old_priv_path), 0o600)
    old_pub_path.write_bytes(public_pem)

    new_fp = fingerprint(public_pem)

    # Re-sign all existing delegation certs
    certs_dir = persist_dir / "certs"
    re_delegated = 0
    if certs_dir.exists():
        for cert_file in certs_dir.glob("*.json"):
            try:
                cert = json.loads(cert_file.read_text())
                agent_name = cert["agent"]
                # Re-create delegation with same scopes and remaining time
                expires_str = cert["expires_at"]
                expires_dt = datetime.strptime(expires_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                remaining = expires_dt - datetime.now(timezone.utc)
                if remaining.total_seconds() > 0:
                    remaining_days = max(1, int(remaining.total_seconds() / 86400))
                    create_delegation_cert(
                        agent_name,
                        cert.get("scopes", []),
                        remaining_days,
                        persist_dir,
                        cert.get("constraints"),
                    )
                    re_delegated += 1
            except (json.JSONDecodeError, IOError, KeyError):
                pass

    _audit_log("key_rotate", "master", f"New fingerprint: {new_fp}. Re-delegated: {re_delegated}")
    return {"new_fingerprint": new_fp, "re_delegated": re_delegated}


# ── Drift detection ──


def snapshot_identity(agent: str, persist_dir: Path) -> dict:
    """Capture a point-in-time identity snapshot for drift detection.

    Hashes identity files, key material, and delegation state.
    Snapshots are stored in .persist/snapshots/<agent>_<timestamp>.json
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    snapshot = {
        "version": 1,
        "agent": agent,
        "captured_at": now,
        "hashes": {},
        "delegation": None,
    }

    # Hash identity files
    for filename in ["principal.md", "mission.md", "diary.md", "recovery.md"]:
        file_path = persist_dir / filename
        if file_path.exists():
            h = hashlib.sha256(file_path.read_bytes()).hexdigest()
            snapshot["hashes"][filename] = h

    # Hash agent key
    pub_path = persist_dir / "keys" / f"{agent}.pub"
    if pub_path.exists():
        snapshot["hashes"]["agent_pubkey"] = hashlib.sha256(pub_path.read_bytes()).hexdigest()

    # Delegation state
    cert = load_delegation_cert(agent, persist_dir)
    if cert:
        snapshot["delegation"] = {
            "scopes": cert.get("scopes", []),
            "expires_at": cert.get("expires_at"),
            "agent_fingerprint": cert.get("agent_pubkey_fingerprint"),
        }

    # Store snapshot
    snapshots_dir = persist_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    ts_slug = now.replace(":", "").replace("-", "")[:15]
    snap_path = snapshots_dir / f"{agent}_{ts_slug}.json"
    snap_path.write_text(json.dumps(snapshot, indent=2) + "\n")

    _audit_log("snapshot", agent, f"Identity snapshot captured")
    return snapshot


def detect_drift(agent: str, persist_dir: Path) -> dict:
    """Compare current identity state against the last snapshot.

    Detects two kinds of drift:
    1. FILE DRIFT: identity files changed (hash mismatch)
    2. KEY DRIFT: agent key changed (possible compromise or rotation)

    Returns {"drifted": bool, "file_drift": [...], "key_drift": bool, "details": str}
    """
    snapshots_dir = persist_dir / "snapshots"
    if not snapshots_dir.exists():
        return {
            "drifted": False,
            "file_drift": [],
            "key_drift": False,
            "details": "No previous snapshot. Run snapshot first.",
        }

    # Find most recent snapshot for this agent
    snaps = sorted(snapshots_dir.glob(f"{agent}_*.json"), reverse=True)
    if not snaps:
        return {
            "drifted": False,
            "file_drift": [],
            "key_drift": False,
            "details": f"No snapshots found for '{agent}'.",
        }

    prev = json.loads(snaps[0].read_text())

    # Take a new snapshot for comparison
    current = snapshot_identity(agent, persist_dir)

    result = {
        "drifted": False,
        "file_drift": [],
        "key_drift": False,
        "prev_snapshot": prev.get("captured_at"),
        "current_snapshot": current.get("captured_at"),
        "details": "",
    }

    # 1. File drift
    prev_hashes = prev.get("hashes", {})
    curr_hashes = current.get("hashes", {})
    for filename in set(list(prev_hashes.keys()) + list(curr_hashes.keys())):
        if filename == "agent_pubkey":
            continue  # Checked separately
        old = prev_hashes.get(filename)
        new = curr_hashes.get(filename)
        if old != new:
            if old and not new:
                result["file_drift"].append(f"{filename}: DELETED")
            elif not old and new:
                result["file_drift"].append(f"{filename}: CREATED")
            else:
                result["file_drift"].append(f"{filename}: MODIFIED")

    # 2. Key drift
    old_key = prev_hashes.get("agent_pubkey")
    new_key = curr_hashes.get("agent_pubkey")
    if old_key and new_key and old_key != new_key:
        result["key_drift"] = True

    # Determine if drifted
    result["drifted"] = bool(result["file_drift"] or result["key_drift"])

    # Summary
    parts = []
    if result["file_drift"]:
        parts.append(f"Files changed: {', '.join(result['file_drift'])}")
    if result["key_drift"]:
        parts.append("AGENT KEY CHANGED — possible compromise or rotation")
    result["details"] = "; ".join(parts) if parts else "No drift detected."

    if result["drifted"]:
        _audit_log("drift_detected", agent, result["details"])

    return result
