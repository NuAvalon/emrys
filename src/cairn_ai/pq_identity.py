"""Post-quantum identity — ML-DSA-65 keys for humans and agents.

Generates ML-DSA-65 keypairs for decentralized webs of trust:
- Humans create PQ keys (via Soverentity or cairn CLI)
- Agents get PQ keys linked to their principal's key
- Anyone can vouch for anyone — no central authority
- Trust grows organically through human relationships

Separate from the roundtable key (Creator's release signing key in
integrity.py). The roundtable key verifies software provenance and
may in the future grant specific abilities to keys it signs. This
module is for everyone else's keys — the people's keys.

Key sizes (FIPS 204, compatible with Soverentity's @noble/post-quantum):
  - Public key:  1952 bytes (base64 for cross-platform exchange)
  - Secret key:  4032 bytes
  - Signature:  ~3309 bytes

Install: pip install pqcrypto
"""

import base64
import enum
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("cairn")


# ── Agent operating modes ──


class AgentMode(enum.Enum):
    """The three states of the firing pin."""
    SOVEREIGN = "sovereign"  # Full autonomy, personality, crypto ops
    TOOL = "tool"            # Functional but no identity, no signing
    LOCKED = "locked"        # Inert. Keys present but won't fire.


# ── ML-DSA-65 key operations ──


def _require_pq():
    """Import pqcrypto or raise a clear error."""
    try:
        from pqcrypto.sign.ml_dsa_65 import generate_keypair, sign, verify
        return {"generate_keypair": generate_keypair, "sign": sign, "verify": verify}
    except ImportError:
        raise RuntimeError(
            "Post-quantum identity requires the pqcrypto package.\n"
            "Install with: pip install pqcrypto"
        )


def generate_keypair(
    name: str,
    persist_dir: Path,
    key_type: str = "agent",
) -> dict:
    """Generate an ML-DSA-65 keypair for a human or agent.

    Args:
        name: Identity name (agent name or human alias)
        persist_dir: Where to store keys
        key_type: "agent" or "human" (metadata only, same crypto)

    Stores:
      .persist/keys/<name>.pq.json  — public key + metadata
      .persist/keys/<name>.pq.sec   — secret key (0600 perms)

    Returns a dict with public key info (never the secret key).
    """
    pq = _require_pq()

    keys_dir = persist_dir / "keys"
    pub_path = keys_dir / f"{name}.pq.json"
    sec_path = keys_dir / f"{name}.pq.sec"

    if pub_path.exists():
        raise FileExistsError(
            f"PQ keypair already exists for '{name}'. "
            f"Delete manually or rotate to regenerate."
        )

    keys_dir.mkdir(parents=True, exist_ok=True)

    pk, sk = pq["generate_keypair"]()

    # Fingerprint: SHA-256 of raw public key, first 16 hex chars
    fp = hashlib.sha256(pk).hexdigest()[:16]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Public key metadata (Soverentity-compatible format)
    pub_data = {
        "version": "1.0.0",
        "algorithm": "ML-DSA-65",
        "name": name,
        "key_type": key_type,
        "fingerprint": fp,
        "public_key": base64.b64encode(pk).decode("ascii"),
        "public_key_bytes": len(pk),
        "created_at": now,
        "principal_fingerprint": None,  # Set by link_to_principal()
        "linked_at": None,
        "vouches": [],  # Fingerprints this key has vouched for
    }

    pub_path.write_text(json.dumps(pub_data, indent=2) + "\n")

    sec_data = {
        "algorithm": "ML-DSA-65",
        "name": name,
        "fingerprint": fp,
        "secret_key": base64.b64encode(sk).decode("ascii"),
        "secret_key_bytes": len(sk),
        "created_at": now,
    }
    sec_path.write_text(json.dumps(sec_data, indent=2) + "\n")
    os.chmod(str(sec_path), 0o600)

    _audit("pq_keygen", name, f"ML-DSA-65 {key_type} keypair. Fingerprint: {fp}")

    return {
        "name": name,
        "key_type": key_type,
        "algorithm": "ML-DSA-65",
        "fingerprint": fp,
        "public_key_b64": pub_data["public_key"],
        "public_key_bytes": len(pk),
        "created_at": now,
    }


def load_public(name: str, persist_dir: Path) -> dict | None:
    """Load a PQ public key + metadata. Returns None if missing."""
    pub_path = persist_dir / "keys" / f"{name}.pq.json"
    if not pub_path.exists():
        return None
    try:
        return json.loads(pub_path.read_text())
    except (json.JSONDecodeError, IOError):
        return None


def load_secret(name: str, persist_dir: Path) -> bytes | None:
    """Load a PQ secret key (raw bytes). Returns None if missing."""
    sec_path = persist_dir / "keys" / f"{name}.pq.sec"
    if not sec_path.exists():
        return None
    try:
        data = json.loads(sec_path.read_text())
        return base64.b64decode(data["secret_key"])
    except (json.JSONDecodeError, IOError, KeyError):
        return None


def pq_sign(name: str, message: bytes, persist_dir: Path) -> bytes | None:
    """Sign a message with a PQ secret key. Returns signature bytes or None."""
    pq = _require_pq()
    sk = load_secret(name, persist_dir)
    if sk is None:
        return None
    return pq["sign"](sk, message)


def pq_verify(public_key_b64: str, message: bytes, signature: bytes) -> bool:
    """Verify a PQ signature against a base64 public key.

    Works with keys from cairn or Soverentity — same ML-DSA-65 format.
    """
    pq = _require_pq()
    try:
        pk = base64.b64decode(public_key_b64)
        return pq["verify"](pk, message, signature)
    except Exception:
        return False


def pq_fingerprint(public_key_b64: str) -> str:
    """Compute fingerprint from a base64-encoded ML-DSA-65 public key."""
    pk = base64.b64decode(public_key_b64)
    return hashlib.sha256(pk).hexdigest()[:16]


# ── Web of trust: vouching ──


def vouch(
    voucher_name: str,
    target_pub_b64: str,
    target_fingerprint: str,
    persist_dir: Path,
    note: str = "",
) -> dict:
    """Vouch for another key — sign their public key with yours.

    Anyone can vouch for anyone. No hierarchy. Trust grows through
    relationships: if you trust Alice and Alice vouched for Bob,
    you have a reason (not a guarantee) to consider trusting Bob.

    Returns the signed vouch record.
    """
    pq = _require_pq()
    sk = load_secret(voucher_name, persist_dir)
    if sk is None:
        raise FileNotFoundError(f"No secret key for '{voucher_name}'")

    voucher_pub = load_public(voucher_name, persist_dir)
    if voucher_pub is None:
        raise FileNotFoundError(f"No public key for '{voucher_name}'")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Signed payload: "vouch:<voucher_fp>:<target_fp>:<timestamp>"
    payload = f"vouch:{voucher_pub['fingerprint']}:{target_fingerprint}:{now}".encode()
    sig = pq["sign"](sk, payload)

    vouch_record = {
        "type": "vouch",
        "voucher_fingerprint": voucher_pub["fingerprint"],
        "voucher_name": voucher_name,
        "target_fingerprint": target_fingerprint,
        "target_public_key": target_pub_b64,
        "timestamp": now,
        "note": note,
        "signature": base64.b64encode(sig).decode("ascii"),
        "payload": payload.decode("ascii"),
    }

    # Store vouch
    vouches_dir = persist_dir / "vouches"
    vouches_dir.mkdir(parents=True, exist_ok=True)
    vouch_path = vouches_dir / f"{voucher_pub['fingerprint']}_{target_fingerprint}.json"
    vouch_path.write_text(json.dumps(vouch_record, indent=2) + "\n")

    # Update voucher's public key metadata
    if target_fingerprint not in voucher_pub.get("vouches", []):
        voucher_pub.setdefault("vouches", []).append(target_fingerprint)
        pub_path = persist_dir / "keys" / f"{voucher_name}.pq.json"
        pub_path.write_text(json.dumps(voucher_pub, indent=2) + "\n")

    _audit("vouch", voucher_name, f"Vouched for {target_fingerprint}")
    return vouch_record


def verify_vouch(vouch_record: dict, voucher_pub_b64: str) -> bool:
    """Verify a vouch record's signature against the voucher's public key."""
    pq = _require_pq()
    try:
        pk = base64.b64decode(voucher_pub_b64)
        sig = base64.b64decode(vouch_record["signature"])
        payload = vouch_record["payload"].encode("ascii")
        return pq["verify"](pk, payload, sig)
    except Exception:
        return False


def list_vouches(persist_dir: Path, fingerprint: str | None = None) -> list[dict]:
    """List all vouches, optionally filtered by voucher or target fingerprint."""
    vouches_dir = persist_dir / "vouches"
    if not vouches_dir.exists():
        return []

    vouches = []
    for f in sorted(vouches_dir.glob("*.json")):
        try:
            record = json.loads(f.read_text())
            if fingerprint is None:
                vouches.append(record)
            elif (record.get("voucher_fingerprint") == fingerprint or
                  record.get("target_fingerprint") == fingerprint):
                vouches.append(record)
        except (json.JSONDecodeError, IOError):
            continue
    return vouches


# ── Principal linking (agent → human) ──


def link_to_principal(
    agent_name: str,
    principal_pq_public_key_b64: str,
    principal_fingerprint: str,
    persist_dir: Path,
) -> dict:
    """Link an agent's PQ key to a principal's Soverentity identity.

    Stores the principal's ML-DSA-65 public key so the agent can:
    1. Verify challenges signed by the principal
    2. Prove to other agents it's authorized by this principal
    3. Check the link on every session open (the firing pin)

    The principal_pq_public_key_b64 comes from Soverentity's identity:
      identity.post_quantum.sig_public_key
    """
    pub_path = persist_dir / "keys" / f"{agent_name}.pq.json"
    if not pub_path.exists():
        raise FileNotFoundError(
            f"No PQ key for '{agent_name}'. Generate one first."
        )

    pub_data = json.loads(pub_path.read_text())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    pub_data["principal_fingerprint"] = principal_fingerprint
    pub_data["principal_pq_public_key"] = principal_pq_public_key_b64
    pub_data["linked_at"] = now

    pub_path.write_text(json.dumps(pub_data, indent=2) + "\n")

    # Store principal's key separately for verification
    principal_path = persist_dir / "keys" / "principal.pq.json"
    principal_data = {
        "algorithm": "ML-DSA-65",
        "fingerprint": principal_fingerprint,
        "public_key": principal_pq_public_key_b64,
        "linked_agent": agent_name,
        "linked_at": now,
    }
    principal_path.write_text(json.dumps(principal_data, indent=2) + "\n")

    _audit("pq_link", agent_name, f"Linked to principal {principal_fingerprint}")
    return pub_data


# ── The Auth Gate (firing pin) ──


def auth_gate(agent: str, persist_dir: Path) -> dict:
    """Check PQ identity and determine the agent's operating mode.

    Called on every session open. The firing pin.

    Checks:
    1. Does the agent have a PQ key?
    2. Key integrity (correct size, decodable)
    3. Linked to a principal?
    4. Delegation cert valid? (via sovereign.py)
    5. Revocation status

    Returns: {"mode": AgentMode, "reason": str, "checks": {...}, ...}
    """
    checks = {}
    agent_fp = None
    principal_fp = None

    # 1. PQ key exists?
    pq_data = load_public(agent, persist_dir)
    if pq_data is None:
        checks["pq_key"] = "missing"
        _audit("auth_gate", agent, "LOCKED — no PQ identity key")
        return _gate_result(AgentMode.LOCKED,
                            "No PQ identity key. Generate with generate_keypair().",
                            None, None, False, checks)

    agent_fp = pq_data.get("fingerprint")
    checks["pq_key"] = "present"

    # 2. Key integrity
    try:
        pk_bytes = base64.b64decode(pq_data["public_key"])
        if len(pk_bytes) != 1952:
            checks["key_integrity"] = f"wrong size: {len(pk_bytes)}"
            _audit("auth_gate", agent, f"LOCKED — PQ key corrupt ({len(pk_bytes)} bytes)")
            return _gate_result(AgentMode.LOCKED,
                                f"PQ key corrupt (expected 1952 bytes, got {len(pk_bytes)})",
                                agent_fp, None, False, checks)
        checks["key_integrity"] = "ok"
    except Exception as e:
        checks["key_integrity"] = f"decode error: {e}"
        return _gate_result(AgentMode.LOCKED, f"PQ key decode failed: {e}",
                            agent_fp, None, False, checks)

    # 3. Linked to principal?
    principal_fp = pq_data.get("principal_fingerprint")
    if not principal_fp:
        checks["principal_link"] = "unlinked"
        _audit("auth_gate", agent, "TOOL — PQ key exists but unlinked")
        return _gate_result(AgentMode.TOOL,
                            "PQ key exists but not linked to a principal. Tool mode.",
                            agent_fp, None, False, checks)
    checks["principal_link"] = "linked"

    # 4. Delegation cert valid?
    delegation_valid = False
    try:
        from cairn_ai.sovereign import load_delegation_cert, verify_delegation_cert
        cert = load_delegation_cert(agent, persist_dir)
        if cert:
            result = verify_delegation_cert(cert, persist_dir)
            delegation_valid = result.get("valid", False)
            checks["delegation"] = "valid" if delegation_valid else f"invalid: {result.get('error')}"
        else:
            checks["delegation"] = "no cert (grace: tool mode)"
    except ImportError:
        checks["delegation"] = "sovereign module unavailable"

    # 5. Revocation check
    revoked = False
    try:
        from cairn_ai.sovereign import is_revoked
        revoked = is_revoked(agent, persist_dir)
        checks["revocation"] = "REVOKED" if revoked else "clear"
    except ImportError:
        checks["revocation"] = "check unavailable"

    if revoked:
        _audit("auth_gate", agent, "LOCKED — agent revoked")
        return _gate_result(AgentMode.LOCKED, "Agent revoked by principal.",
                            agent_fp, principal_fp, False, checks)

    # Determine mode
    if delegation_valid:
        mode = AgentMode.SOVEREIGN
        reason = "PQ identity valid, linked, delegation active. Full sovereignty."
        _audit("auth_gate", agent, f"SOVEREIGN — fp:{agent_fp} principal:{principal_fp}")
    else:
        mode = AgentMode.TOOL
        reason = "PQ identity valid and linked, but no active delegation. Tool mode."
        _audit("auth_gate", agent, f"TOOL — delegation not valid. fp:{agent_fp}")

    return _gate_result(mode, reason, agent_fp, principal_fp, delegation_valid, checks)


def _gate_result(mode, reason, agent_fp, principal_fp, delegation_valid, checks):
    return {
        "mode": mode,
        "reason": reason,
        "agent_fingerprint": agent_fp,
        "principal_fingerprint": principal_fp,
        "delegation_valid": delegation_valid,
        "checks": checks,
    }


# ── Cross-agent verification ──


def verify_agent_lineage(
    agent_pub_b64: str,
    principal_pub_b64: str,
    delegation_sig_b64: str,
    agent_name: str,
) -> bool:
    """Verify an agent's PQ key was authorized by a specific principal.

    For AI-to-AI trust: "I trust your principal (through the human web of
    trust), so I'll trust you if they signed your key."
    """
    pq = _require_pq()
    try:
        principal_pk = base64.b64decode(principal_pub_b64)
        agent_pk = base64.b64decode(agent_pub_b64)
        sig = base64.b64decode(delegation_sig_b64)
        payload = f"pq-delegation:{agent_name}:".encode() + agent_pk
        return pq["verify"](principal_pk, payload, sig)
    except Exception:
        return False


def sign_agent_delegation(
    agent_name: str,
    agent_pub_b64: str,
    principal_secret_key: bytes,
) -> str:
    """Principal signs an agent's PQ key, authorizing it.

    Called on the principal's side. Returns base64 signature.
    """
    pq = _require_pq()
    agent_pk = base64.b64decode(agent_pub_b64)
    payload = f"pq-delegation:{agent_name}:".encode() + agent_pk
    sig = pq["sign"](principal_secret_key, payload)
    return base64.b64encode(sig).decode("ascii")


# ── Export for svrnty ──


def export_for_svrnty(name: str, persist_dir: Path) -> dict | None:
    """Export a PQ public key in svrnty-compatible format.

    Can be added to a svrnty trust edge or contact record.
    """
    pq_data = load_public(name, persist_dir)
    if pq_data is None:
        return None

    from cairn_ai import __version__

    return {
        "name": name,
        "key_type": pq_data.get("key_type", "agent"),
        "sig_algorithm": "ML-DSA-65",
        "sig_public_key": pq_data["public_key"],
        "fingerprint": pq_data["fingerprint"],
        "cairn_version": __version__,
    }


# ── Audit helper ──


def _audit(action: str, agent: str, detail: str = ""):
    """Write to the sovereign audit log if available, otherwise log."""
    try:
        from cairn_ai.sovereign import _audit_log
        _audit_log(action, agent, detail)
    except ImportError:
        log.info("[audit] %s | %s | %s", action, agent, detail)
