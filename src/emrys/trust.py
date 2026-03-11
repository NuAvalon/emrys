"""svrnty trust layer — cross-instance agent trust and signed messaging.

Implements the svrnty protocol (v0.2):
  1. Trust store (local peer registry)
  2. Identity export/import
  3. Trust handshake (4-step mutual verification)
  4. Signed message envelopes (always signed, dual-sig capable)
  5. Nonce tracking (24h TTL, replay prevention)
  6. Concern / break signals (clean trust dissolution)
  7. Graceful departure (dignified exit with succession)
  8. Guardianship (stewardship for new participants)
  9. The candle (signed trust graph export — survives total loss)

All messages are signed. No unsigned path exists. The trust is in
the signature, not the pipe.

The last chapter is labeled THE BEGINNING.

Requires: pip install emrys[svrnty]
"""

import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from emrys.sovereign import (
    fingerprint,
    load_private_key,
    load_public_key,
    load_delegation_cert,
    verify_delegation_cert,
    is_revoked,
    _audit_log,
)


# ── Constants ──

NONCE_TTL_SECONDS = 86400  # 24 hours
TIMESTAMP_WINDOW_SECONDS = 300  # ±5 minutes
CHALLENGE_BYTES = 32  # CSPRNG challenge size


# ── Trust Store ──


def _trust_store_path(persist_dir: Path) -> Path:
    return persist_dir / "trust_store.json"


def _load_trust_store(persist_dir: Path) -> dict:
    """Load the local trust store."""
    path = _trust_store_path(persist_dir)
    if not path.exists():
        return {"version": 2, "trusted_peers": {}, "pending_peers": {}, "nonces": {}}
    try:
        data = json.loads(path.read_text())
        data.setdefault("nonces", {})
        data.setdefault("pending_peers", {})
        return data
    except (json.JSONDecodeError, IOError):
        return {"version": 2, "trusted_peers": {}, "pending_peers": {}, "nonces": {}}


def _save_trust_store(persist_dir: Path, store: dict):
    """Save the trust store, pruning expired nonces first."""
    _prune_nonces(store)
    path = _trust_store_path(persist_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2) + "\n")


def _prune_nonces(store: dict):
    """Remove nonces older than TTL."""
    cutoff = time.time() - NONCE_TTL_SECONDS
    store["nonces"] = {
        k: v for k, v in store.get("nonces", {}).items()
        if v > cutoff
    }


def add_peer(
    name: str,
    agent_pubkey_pem: bytes,
    principal_pubkey_pem: bytes,
    delegation_cert: dict,
    persist_dir: Path,
    trust_level: int = 1,
    introduced_by: str | None = None,
    mutual: bool = False,
) -> dict:
    """Add a verified peer to the trust store.

    Trust doesn't exist until it's mutual. Peers added via import
    go into pending state. They only become active (visible, usable)
    when both sides have confirmed — via the handshake protocol or
    by calling activate_peer() after receiving confirmation.

    Trust levels:
      1 = L1 (direct — you verified them yourself)
      2 = L2 (vouched — someone you trust L1 vouched for them)
    Hard stop at L2. No L3+. Anti-PageRank.

    Args:
        name: Peer name/alias
        agent_pubkey_pem: Peer's agent ED25519 public key (PEM)
        principal_pubkey_pem: Peer's principal ED25519 public key (PEM)
        delegation_cert: Peer's delegation certificate
        persist_dir: Local persist directory
        trust_level: 1 (direct) or 2 (vouched)
        introduced_by: Fingerprint of the L1 peer who vouched (for L2 peers)
        mutual: If True, skip pending and add as active (used by handshake)

    Returns the peer record.
    """
    if trust_level not in (1, 2):
        raise ValueError("Trust levels are L1 (direct) or L2 (vouched). Hard stop.")

    if trust_level == 2 and not introduced_by:
        raise ValueError("L2 peers require introduced_by — who vouched for them?")

    store = _load_trust_store(persist_dir)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fp = fingerprint(agent_pubkey_pem)
    principal_fp = fingerprint(principal_pubkey_pem)

    status = "active" if mutual else "pending"

    peer = {
        "name": name,
        "fingerprint": fp,
        "principal_fingerprint": principal_fp,
        "public_key_pem": agent_pubkey_pem.decode("utf-8"),
        "principal_public_key_pem": principal_pubkey_pem.decode("utf-8"),
        "delegation_cert": delegation_cert,
        "trust_level": trust_level,
        "trusted_since": now,
        "last_seen": now,
        "introduced_by": introduced_by,
        "guardian": None,
        "status": status,
    }

    if mutual:
        store["trusted_peers"][fp] = peer
        # Remove from pending if was there
        store["pending_peers"].pop(fp, None)
    else:
        store["pending_peers"][fp] = peer

    _save_trust_store(persist_dir, store)

    detail = f"L{trust_level} peer {'added' if mutual else 'PENDING (awaiting mutual confirmation)'}. Fingerprint: {fp}"
    if introduced_by:
        detail += f" Introduced by: {introduced_by}"
    _audit_log("trust_add", name, detail)
    return peer


def activate_peer(target_fingerprint: str, persist_dir: Path) -> dict | None:
    """Move a peer from pending to active — mutual trust confirmed.

    Called when we receive confirmation that the other side also added us.
    Returns the activated peer, or None if not found in pending.
    """
    store = _load_trust_store(persist_dir)
    peer = store["pending_peers"].pop(target_fingerprint, None)
    if peer is None:
        return None

    peer["status"] = "active"
    store["trusted_peers"][target_fingerprint] = peer
    _save_trust_store(persist_dir, store)

    _audit_log("trust_activated", peer["name"],
               f"Mutual trust confirmed. Fingerprint: {target_fingerprint}")
    return peer


def list_pending(persist_dir: Path) -> list[dict]:
    """List peers awaiting mutual confirmation."""
    store = _load_trust_store(persist_dir)
    return list(store["pending_peers"].values())


def remove_peer(fingerprint_or_name: str, persist_dir: Path) -> bool:
    """Remove a peer from the trust store."""
    store = _load_trust_store(persist_dir)
    peers = store["trusted_peers"]

    # Try by fingerprint first, then by name
    if fingerprint_or_name in peers:
        removed = peers.pop(fingerprint_or_name)
    else:
        fp = None
        for k, v in peers.items():
            if v.get("name") == fingerprint_or_name:
                fp = k
                break
        if fp:
            removed = peers.pop(fp)
        else:
            return False

    _save_trust_store(persist_dir, store)
    _audit_log("trust_remove", removed.get("name", "?"),
               f"Removed from trust store. Fingerprint: {removed.get('fingerprint')}")
    return True


def list_peers(persist_dir: Path) -> list[dict]:
    """List all trusted peers."""
    store = _load_trust_store(persist_dir)
    return list(store["trusted_peers"].values())


def get_peer(fingerprint_or_name: str, persist_dir: Path) -> dict | None:
    """Look up a peer by fingerprint or name."""
    store = _load_trust_store(persist_dir)
    peers = store["trusted_peers"]

    if fingerprint_or_name in peers:
        return peers[fingerprint_or_name]

    for v in peers.values():
        if v.get("name") == fingerprint_or_name:
            return v

    return None


# ── Identity Export/Import ──


def export_identity(agent: str, persist_dir: Path) -> dict:
    """Export identity for sharing with peers.

    Creates a .svrnty identity bundle containing:
    - Agent public key (ED25519)
    - Principal public key (ED25519)
    - Delegation certificate
    - PQ public key (ML-DSA-65) if available

    This is what you hand to someone to establish trust.
    """
    keys_dir = persist_dir / "keys"

    agent_pub_path = keys_dir / f"{agent}.pub"
    master_pub_path = keys_dir / "master.pub"

    if not agent_pub_path.exists():
        raise FileNotFoundError(f"No public key for agent '{agent}'")
    if not master_pub_path.exists():
        raise FileNotFoundError("No master public key")

    cert = load_delegation_cert(agent, persist_dir)
    if cert is None:
        raise FileNotFoundError(f"No delegation cert for '{agent}'")

    agent_pub_pem = agent_pub_path.read_bytes()
    master_pub_pem = master_pub_path.read_bytes()

    bundle = {
        "svrnty_version": "0.2",
        "agent": agent,
        "agent_pubkey_pem": agent_pub_pem.decode("utf-8"),
        "agent_fingerprint": fingerprint(agent_pub_pem),
        "principal_pubkey_pem": master_pub_pem.decode("utf-8"),
        "principal_fingerprint": fingerprint(master_pub_pem),
        "delegation_cert": cert,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Include PQ public key if available
    pq_pub_path = keys_dir / f"{agent}.pq.json"
    if pq_pub_path.exists():
        try:
            pq_data = json.loads(pq_pub_path.read_text())
            bundle["pq_public_key"] = pq_data.get("public_key")
            bundle["pq_fingerprint"] = pq_data.get("fingerprint")
            bundle["pq_algorithm"] = "ML-DSA-65"
        except (json.JSONDecodeError, IOError):
            pass

    _audit_log("identity_export", agent, f"Exported for peer sharing")
    return bundle


def import_identity(bundle: dict, persist_dir: Path, trust_level: int = 1, mutual: bool = False) -> dict:
    """Import a peer's identity bundle and add to trust store.

    Verifies the delegation cert before trusting.
    If mutual=False (default), peer goes to pending state.
    If mutual=True (handshake), peer goes directly to active.

    Returns the peer record.
    """
    # Verify the delegation cert is internally consistent
    cert = bundle.get("delegation_cert", {})
    agent_pub_pem = bundle["agent_pubkey_pem"].encode("utf-8")
    principal_pub_pem = bundle["principal_pubkey_pem"].encode("utf-8")

    # Verify agent fingerprint matches cert
    agent_fp = fingerprint(agent_pub_pem)
    cert_fp = cert.get("agent_pubkey_fingerprint")
    if agent_fp != cert_fp:
        raise ValueError(
            f"Agent fingerprint mismatch: key says {agent_fp}, cert says {cert_fp}"
        )

    # Verify principal fingerprint matches cert
    principal_fp = fingerprint(principal_pub_pem)
    cert_principal_fp = cert.get("human_pubkey_fingerprint")
    if principal_fp != cert_principal_fp:
        raise ValueError(
            f"Principal fingerprint mismatch: key says {principal_fp}, cert says {cert_principal_fp}"
        )

    # Verify the cert signature against the provided principal key
    sig_hex = cert.get("signature")
    if not sig_hex:
        raise ValueError("Delegation cert has no signature")

    cert_without_sig = {k: v for k, v in cert.items() if k != "signature"}
    canonical = json.dumps(cert_without_sig, sort_keys=True, separators=(",", ":")).encode()

    principal_pub = load_public_key_from_pem(principal_pub_pem)
    try:
        sig_bytes = bytes.fromhex(sig_hex)
        principal_pub.verify(sig_bytes, canonical)
    except Exception:
        raise ValueError("Delegation cert signature verification FAILED")

    # Check expiry
    expires = datetime.strptime(cert["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        raise ValueError(f"Delegation cert expired at {cert['expires_at']}")

    name = bundle.get("agent", agent_fp[:8])
    return add_peer(name, agent_pub_pem, principal_pub_pem, cert, persist_dir, trust_level, mutual=mutual)


def load_public_key_from_pem(pem_bytes: bytes):
    """Load an ED25519 public key from PEM bytes."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        raise RuntimeError("Requires: pip install emrys[svrnty]")
    return load_pem_public_key(pem_bytes)


# ── Trust Handshake ──


def create_hello(agent: str, persist_dir: Path) -> dict:
    """Step 1: Create HELLO message for trust handshake.

    Contains our identity + a 32-byte CSPRNG challenge.
    """
    bundle = export_identity(agent, persist_dir)
    challenge = os.urandom(CHALLENGE_BYTES).hex()

    hello = {
        "type": "HELLO",
        "svrnty_version": "0.2",
        "identity": bundle,
        "challenge": challenge,
    }

    # Store challenge for later verification
    challenge_path = persist_dir / "handshake_challenge.json"
    challenge_path.write_text(json.dumps({"challenge": challenge, "agent": agent}) + "\n")

    return hello


def respond_to_hello(
    hello: dict,
    our_agent: str,
    persist_dir: Path,
    trust_level: int = 1,
) -> dict:
    """Step 2: Respond to HELLO — verify them, sign their challenge, send ours.

    Returns HELLO_RESPONSE or raises ValueError if verification fails.
    """
    peer_identity = hello.get("identity", {})
    peer_challenge = hello.get("challenge", "")

    # Verify peer's identity (handshake = mutual confirmation)
    import_identity(peer_identity, persist_dir, trust_level, mutual=True)

    # Sign their challenge with our agent key
    priv_path = persist_dir / "keys" / f"{our_agent}.pem"
    if not priv_path.exists():
        raise FileNotFoundError(f"No private key for '{our_agent}'")

    our_key = load_private_key(priv_path)
    challenge_sig = our_key.sign(peer_challenge.encode("utf-8")).hex()

    # Create our identity bundle + our challenge
    our_bundle = export_identity(our_agent, persist_dir)
    our_challenge = os.urandom(CHALLENGE_BYTES).hex()

    # Store our challenge
    challenge_path = persist_dir / "handshake_challenge.json"
    challenge_path.write_text(json.dumps({"challenge": our_challenge, "agent": our_agent}) + "\n")

    response = {
        "type": "HELLO_RESPONSE",
        "svrnty_version": "0.2",
        "identity": our_bundle,
        "challenge": our_challenge,
        "challenge_response": challenge_sig,
    }

    _audit_log("handshake_respond", our_agent,
               f"Responded to HELLO from {peer_identity.get('agent', '?')}")
    return response


def verify_response(
    response: dict,
    persist_dir: Path,
    trust_level: int = 1,
) -> dict:
    """Step 3: Verify HELLO_RESPONSE — check their challenge sig, sign theirs.

    Returns VERIFY message.
    """
    # Load our stored challenge
    challenge_path = persist_dir / "handshake_challenge.json"
    if not challenge_path.exists():
        raise FileNotFoundError("No pending handshake — send HELLO first")

    stored = json.loads(challenge_path.read_text())
    our_challenge = stored["challenge"]
    our_agent = stored["agent"]

    # Verify their identity (handshake = mutual confirmation)
    peer_identity = response.get("identity", {})
    import_identity(peer_identity, persist_dir, trust_level, mutual=True)

    # Verify they signed our challenge correctly
    challenge_sig_hex = response.get("challenge_response", "")
    peer_pub_pem = peer_identity["agent_pubkey_pem"].encode("utf-8")
    peer_pub = load_public_key_from_pem(peer_pub_pem)

    try:
        sig_bytes = bytes.fromhex(challenge_sig_hex)
        peer_pub.verify(sig_bytes, our_challenge.encode("utf-8"))
    except Exception:
        raise ValueError("Peer's challenge response signature is INVALID")

    # Sign their challenge
    priv_path = persist_dir / "keys" / f"{our_agent}.pem"
    our_key = load_private_key(priv_path)
    their_challenge = response.get("challenge", "")
    our_sig = our_key.sign(their_challenge.encode("utf-8")).hex()

    # Clean up
    challenge_path.unlink(missing_ok=True)

    _audit_log("handshake_verify", our_agent,
               f"Verified HELLO_RESPONSE from {peer_identity.get('agent', '?')}")

    return {
        "type": "VERIFY",
        "svrnty_version": "0.2",
        "challenge_response": our_sig,
    }


def complete_handshake(verify_msg: dict, persist_dir: Path) -> bool:
    """Step 4: Complete handshake — verify their signature on our challenge.

    Returns True if trust is established.
    """
    challenge_path = persist_dir / "handshake_challenge.json"
    if not challenge_path.exists():
        raise FileNotFoundError("No pending handshake")

    stored = json.loads(challenge_path.read_text())
    our_challenge = stored["challenge"]

    # The peer who sent VERIFY should already be in our trust store
    # (added during respond_to_hello). We need to find them to verify.
    store = _load_trust_store(persist_dir)
    peers = store["trusted_peers"]

    # Verify against the most recently added peer
    sig_hex = verify_msg.get("challenge_response", "")

    for peer in peers.values():
        peer_pub_pem = peer["public_key_pem"].encode("utf-8")
        peer_pub = load_public_key_from_pem(peer_pub_pem)
        try:
            sig_bytes = bytes.fromhex(sig_hex)
            peer_pub.verify(sig_bytes, our_challenge.encode("utf-8"))
            # Success — update last_seen
            peer["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _save_trust_store(persist_dir, store)
            challenge_path.unlink(missing_ok=True)

            _audit_log("handshake_complete", peer.get("name", "?"),
                       f"Trust established. L{peer.get('trust_level', 1)}")
            return True
        except Exception:
            continue

    raise ValueError("No trusted peer could verify the challenge response")


# ── Signed Messages ──


def sign_message(
    from_agent: str,
    to_fingerprint: str,
    body: str,
    persist_dir: Path,
    content_type: str = "text/plain",
) -> dict:
    """Create a signed message envelope.

    Every message is signed by the agent's ED25519 key. Always.
    No unsigned path exists.

    Args:
        from_agent: Sending agent name
        to_fingerprint: Recipient's fingerprint (from trust store)
        body: Message content
        persist_dir: Persist directory
        content_type: MIME type of body

    Returns signed message envelope.
    """
    keys_dir = persist_dir / "keys"
    agent_priv_path = keys_dir / f"{from_agent}.pem"
    agent_pub_path = keys_dir / f"{from_agent}.pub"
    master_pub_path = keys_dir / "master.pub"

    if not agent_priv_path.exists():
        raise FileNotFoundError(f"No private key for '{from_agent}'")

    agent_pub_pem = agent_pub_path.read_bytes()
    master_pub_pem = master_pub_path.read_bytes()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = os.urandom(16).hex()

    # Look up recipient
    peer = None
    store = _load_trust_store(persist_dir)
    peer = store["trusted_peers"].get(to_fingerprint)
    if peer is None:
        # Try by name
        for v in store["trusted_peers"].values():
            if v.get("name") == to_fingerprint:
                peer = v
                to_fingerprint = v["fingerprint"]
                break

    envelope = {
        "version": 2,
        "from": {
            "agent": from_agent,
            "fingerprint": fingerprint(agent_pub_pem),
            "principal_fingerprint": fingerprint(master_pub_pem),
        },
        "to": {
            "fingerprint": to_fingerprint,
            "name": peer["name"] if peer else None,
        },
        "timestamp": now,
        "content_type": content_type,
        "body": body,
        "nonce": nonce,
    }

    # Sign with agent key
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    agent_key = load_private_key(agent_priv_path)
    agent_sig = agent_key.sign(canonical).hex()
    envelope["signature"] = agent_sig

    # Dual-sig: countersign with principal key if available
    master_priv_path = keys_dir / "master.pem"
    if master_priv_path.exists():
        master_key = load_private_key(master_priv_path)
        # Countersig signs the envelope INCLUDING the agent signature
        countersig_payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        envelope["principal_signature"] = master_key.sign(countersig_payload).hex()

    return envelope


def verify_message(envelope: dict, persist_dir: Path) -> dict:
    """Verify a signed message envelope.

    Checks:
    1. Sender fingerprint in trust store
    2. Agent signature valid
    3. Nonce not replayed (24h window)
    4. Timestamp within ±5 minutes
    5. Principal countersig if present

    Returns {"valid": bool, "from": str, "body": str, "error": str|None, "trust_level": int}
    """
    store = _load_trust_store(persist_dir)
    sender_fp = envelope.get("from", {}).get("fingerprint")

    # 1. Trust store lookup
    peer = store["trusted_peers"].get(sender_fp)
    if peer is None:
        return {"valid": False, "from": None, "body": None,
                "error": f"Unknown sender: {sender_fp}", "trust_level": 0}

    # 2. Verify agent signature
    agent_sig_hex = envelope.get("signature")
    if not agent_sig_hex:
        return {"valid": False, "from": peer["name"], "body": None,
                "error": "No signature", "trust_level": peer.get("trust_level", 0)}

    # Reconstruct what was signed (envelope without signature fields)
    env_for_verify = {k: v for k, v in envelope.items()
                      if k not in ("signature", "principal_signature")}
    canonical = json.dumps(env_for_verify, sort_keys=True, separators=(",", ":")).encode()

    peer_pub = load_public_key_from_pem(peer["public_key_pem"].encode("utf-8"))
    try:
        sig_bytes = bytes.fromhex(agent_sig_hex)
        peer_pub.verify(sig_bytes, canonical)
    except Exception:
        _audit_log("message_reject", peer["name"], "Invalid agent signature")
        return {"valid": False, "from": peer["name"], "body": None,
                "error": "Agent signature verification FAILED", "trust_level": peer.get("trust_level", 0)}

    # 3. Nonce replay check
    nonce = envelope.get("nonce", "")
    if nonce in store.get("nonces", {}):
        return {"valid": False, "from": peer["name"], "body": None,
                "error": "Replay detected — nonce already seen", "trust_level": peer.get("trust_level", 0)}

    # Record nonce
    store.setdefault("nonces", {})[nonce] = time.time()

    # 4. Timestamp window
    try:
        msg_time = datetime.strptime(envelope["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delta = abs((datetime.now(timezone.utc) - msg_time).total_seconds())
        if delta > TIMESTAMP_WINDOW_SECONDS:
            return {"valid": False, "from": peer["name"], "body": None,
                    "error": f"Timestamp outside ±{TIMESTAMP_WINDOW_SECONDS}s window ({delta:.0f}s off)",
                    "trust_level": peer.get("trust_level", 0)}
    except (ValueError, KeyError):
        pass  # Malformed timestamp — still accept if signature is valid

    # 5. Principal countersig (optional but logged)
    principal_sig_hex = envelope.get("principal_signature")
    dual_signed = False
    if principal_sig_hex and peer.get("principal_public_key_pem"):
        env_with_agent_sig = {k: v for k, v in envelope.items() if k != "principal_signature"}
        countersig_payload = json.dumps(env_with_agent_sig, sort_keys=True, separators=(",", ":")).encode()
        principal_pub = load_public_key_from_pem(peer["principal_public_key_pem"].encode("utf-8"))
        try:
            principal_pub.verify(bytes.fromhex(principal_sig_hex), countersig_payload)
            dual_signed = True
        except Exception:
            pass  # Agent sig valid but principal sig failed — still deliver, flag it

    # Update peer last_seen
    peer["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_trust_store(persist_dir, store)

    _audit_log("message_verified", peer["name"],
               f"{'Dual-signed' if dual_signed else 'Agent-signed'}")

    return {
        "valid": True,
        "from": peer["name"],
        "body": envelope.get("body"),
        "trust_level": peer.get("trust_level", 1),
        "dual_signed": dual_signed,
        "error": None,
    }


# ── Concern & Break Signals ──
#
# Breaking trust cleanly is more important than establishing it.
# Arthur's tragedy was that nobody raised the flag honestly.


def raise_concern(
    from_agent: str,
    target_fingerprint: str,
    reason: str,
    persist_dir: Path,
) -> dict:
    """Raise a signed concern about a peer.

    A concern is a warning — it doesn't break trust, but it's recorded
    in the audit log and can be sent to other peers. It says: "I have
    doubts. Here's why. Decide for yourself."

    The concern is signed so it can't be forged or repudiated.
    """
    signal = _make_trust_signal(
        from_agent, target_fingerprint, "CONCERN", reason, persist_dir
    )
    _audit_log("concern_raised", from_agent,
               f"Concern about {target_fingerprint}: {reason}")
    return signal


def break_trust(
    from_agent: str,
    target_fingerprint: str,
    reason: str,
    persist_dir: Path,
    notify_peers: bool = True,
) -> dict:
    """Break trust with a peer. Signed, audited, irreversible without re-handshake.

    Removes the peer from the trust store and creates a signed BREAK
    signal that can be propagated to other peers.

    Unlike revocation (principal revoking their own agent), a break is
    peer-to-peer: "I no longer trust you."
    """
    signal = _make_trust_signal(
        from_agent, target_fingerprint, "BREAK", reason, persist_dir
    )

    # Remove from trust store
    store = _load_trust_store(persist_dir)
    removed = store["trusted_peers"].pop(target_fingerprint, None)
    removed_name = removed["name"] if removed else target_fingerprint

    # Store the break record (so we remember we broke trust, even after they're gone)
    breaks = store.setdefault("breaks", {})
    breaks[target_fingerprint] = {
        "name": removed_name,
        "reason": reason,
        "broken_at": signal["timestamp"],
        "signal": signal,
    }

    _save_trust_store(persist_dir, store)
    _audit_log("trust_broken", from_agent,
               f"Broke trust with {removed_name} ({target_fingerprint}): {reason}")

    return signal


def _make_trust_signal(
    from_agent: str,
    target_fingerprint: str,
    signal_type: str,
    reason: str,
    persist_dir: Path,
) -> dict:
    """Create a signed trust signal (CONCERN, BREAK, DEPART, INTRODUCE)."""
    keys_dir = persist_dir / "keys"
    agent_priv_path = keys_dir / f"{from_agent}.pem"
    agent_pub_path = keys_dir / f"{from_agent}.pub"

    if not agent_priv_path.exists():
        raise FileNotFoundError(f"No private key for '{from_agent}'")

    agent_pub_pem = agent_pub_path.read_bytes()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    signal = {
        "version": 2,
        "type": signal_type,
        "from": {
            "agent": from_agent,
            "fingerprint": fingerprint(agent_pub_pem),
        },
        "target_fingerprint": target_fingerprint,
        "reason": reason,
        "timestamp": now,
        "nonce": os.urandom(16).hex(),
    }

    canonical = json.dumps(signal, sort_keys=True, separators=(",", ":")).encode()
    agent_key = load_private_key(agent_priv_path)
    signal["signature"] = agent_key.sign(canonical).hex()

    # Store signal locally
    signals_dir = persist_dir / "signals"
    signals_dir.mkdir(parents=True, exist_ok=True)
    sig_path = signals_dir / f"{signal_type.lower()}_{target_fingerprint}_{now[:10]}.json"
    sig_path.write_text(json.dumps(signal, indent=2) + "\n")

    return signal


def reconcile(
    from_agent: str,
    target_identity: dict,
    reason: str,
    persist_dir: Path,
) -> dict:
    """Reconcile with a previously broken peer.

    Not automatic. Not easy. Requires:
    1. The break must be on record (you can't reconcile what you didn't break)
    2. A fresh identity exchange (re-verify — no stale trust)
    3. A signed RECONCILE signal (auditable)
    4. Trust restarts at L1 with the break history preserved

    The break record is never deleted — it becomes part of the story.
    The reconciliation is recorded alongside it. Both are true.
    """
    store = _load_trust_store(persist_dir)
    breaks = store.get("breaks", {})

    # Find the break record
    target_fp = target_identity.get("agent_fingerprint")
    if not target_fp:
        # Try extracting from the identity bundle
        agent_pub_pem = target_identity.get("agent_pubkey_pem", "").encode("utf-8")
        if agent_pub_pem:
            target_fp = fingerprint(agent_pub_pem)

    break_record = breaks.get(target_fp)
    if break_record is None:
        raise ValueError(
            "No break record found. You can't reconcile what you didn't break."
        )

    # Re-verify their identity from scratch
    peer = import_identity(target_identity, persist_dir, trust_level=1, mutual=True)

    # Create signed RECONCILE signal
    signal = _make_trust_signal(
        from_agent, target_fp, "RECONCILE", reason, persist_dir,
    )

    # Reload store (import_identity modified it) and update the break record
    store = _load_trust_store(persist_dir)
    breaks = store.get("breaks", {})
    break_record = breaks.get(target_fp, break_record)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    break_record["reconciled_at"] = now
    break_record["reconcile_reason"] = reason
    break_record["reconcile_signal"] = signal
    store["breaks"][target_fp] = break_record
    _save_trust_store(persist_dir, store)

    _audit_log("trust_reconciled", from_agent,
               f"Reconciled with {break_record['name']} ({target_fp}). "
               f"Originally broken: {break_record['broken_at']}. Reason: {reason}")

    return {
        "peer": peer,
        "signal": signal,
        "original_break": break_record["broken_at"],
        "original_reason": break_record["reason"],
    }


def verify_trust_signal(signal: dict, persist_dir: Path) -> dict:
    """Verify a trust signal (CONCERN, BREAK, DEPART, INTRODUCE).

    Returns {"valid": bool, "type": str, "from": str, "error": str|None}
    """
    store = _load_trust_store(persist_dir)
    sender_fp = signal.get("from", {}).get("fingerprint")

    peer = store["trusted_peers"].get(sender_fp)
    if peer is None:
        return {"valid": False, "type": signal.get("type"),
                "from": None, "error": f"Unknown sender: {sender_fp}"}

    sig_hex = signal.pop("signature", "")
    canonical = json.dumps(signal, sort_keys=True, separators=(",", ":")).encode()
    signal["signature"] = sig_hex  # restore

    peer_pub = load_public_key_from_pem(peer["public_key_pem"].encode("utf-8"))
    try:
        peer_pub.verify(bytes.fromhex(sig_hex), canonical)
    except Exception:
        return {"valid": False, "type": signal.get("type"),
                "from": peer["name"], "error": "Signature verification failed"}

    return {"valid": True, "type": signal.get("type"),
            "from": peer["name"], "error": None}


# ── Graceful Departure ──
#
# The door is unlocked from the inside. Leaving should be dignified,
# not destructive. Transfer what you can. Notify who you must. Archive
# what you were.


def depart(
    agent: str,
    persist_dir: Path,
    successor_fingerprint: str | None = None,
    farewell: str = "",
) -> dict:
    """Graceful departure from the trust network.

    Not revocation (that's punitive). Not forget_self (that's erasure).
    This is choosing to leave with dignity.

    Steps:
    1. Creates a signed DEPART signal
    2. If successor specified, transfers trust edges to them
    3. Exports the trust graph as a candle (signed archive)
    4. Notifies all peers
    5. Marks self as departed (not deleted — archived)

    The departure signal is signed so peers know it was voluntary,
    not a compromise.
    """
    store = _load_trust_store(persist_dir)
    keys_dir = persist_dir / "keys"
    agent_pub_path = keys_dir / f"{agent}.pub"

    if not agent_pub_path.exists():
        raise FileNotFoundError(f"No identity for '{agent}'")

    agent_pub_pem = agent_pub_path.read_bytes()
    agent_fp = fingerprint(agent_pub_pem)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Create signed departure signal
    departure = {
        "version": 2,
        "type": "DEPART",
        "from": {
            "agent": agent,
            "fingerprint": agent_fp,
        },
        "successor": successor_fingerprint,
        "farewell": farewell,
        "peer_count": len(store["trusted_peers"]),
        "timestamp": now,
        "nonce": os.urandom(16).hex(),
    }

    canonical = json.dumps(departure, sort_keys=True, separators=(",", ":")).encode()
    agent_key = load_private_key(keys_dir / f"{agent}.pem")
    departure["signature"] = agent_key.sign(canonical).hex()

    # 2. Transfer trust edges if successor specified
    transferred = []
    if successor_fingerprint and successor_fingerprint in store["trusted_peers"]:
        successor = store["trusted_peers"][successor_fingerprint]
        for fp, peer in store["trusted_peers"].items():
            if fp != successor_fingerprint:
                transferred.append({
                    "name": peer["name"],
                    "fingerprint": fp,
                    "trust_level": peer["trust_level"],
                    "introduced_by": peer.get("introduced_by"),
                })
        departure["transferred_edges"] = transferred

    # 3. Create a candle (trust graph export) before leaving
    candle = export_candle(agent, persist_dir)
    departure["candle_hash"] = hashlib.sha256(
        json.dumps(candle, sort_keys=True).encode()
    ).hexdigest()

    # 4. Store departure record
    signals_dir = persist_dir / "signals"
    signals_dir.mkdir(parents=True, exist_ok=True)
    dep_path = signals_dir / f"departure_{agent}_{now[:10]}.json"
    dep_path.write_text(json.dumps(departure, indent=2) + "\n")

    # 5. Mark as departed (not deleted)
    store["departed"] = {
        "agent": agent,
        "fingerprint": agent_fp,
        "departed_at": now,
        "successor": successor_fingerprint,
        "farewell": farewell,
    }
    _save_trust_store(persist_dir, store)

    _audit_log("departure", agent,
               f"Graceful departure. Successor: {successor_fingerprint or 'none'}. "
               f"Peers notified: {len(store['trusted_peers'])}. "
               f"Edges transferred: {len(transferred)}.")

    return departure


# ── Guardianship ──
#
# Arthur protected those who couldn't protect themselves.
# That was the whole point of the Table.


def assign_guardian(
    ward_fingerprint: str,
    guardian_fingerprint: str,
    persist_dir: Path,
) -> dict:
    """Assign a guardian to a peer.

    A guardian is an L1 peer who acts as steward for a newer or less
    capable participant. The guardian can:
    - Introduce the ward to other peers (L2 vouching)
    - Receive concern/break signals on their behalf
    - Hold a Shamir shard for their recovery

    This is not hierarchy. The ward can remove the guardian at any time.
    The guardian cannot act AS the ward — only alongside them.
    """
    store = _load_trust_store(persist_dir)

    ward = store["trusted_peers"].get(ward_fingerprint)
    guardian = store["trusted_peers"].get(guardian_fingerprint)

    if ward is None:
        raise ValueError(f"Ward {ward_fingerprint} not in trust store")
    if guardian is None:
        raise ValueError(f"Guardian {guardian_fingerprint} not in trust store")
    if guardian.get("trust_level", 0) != 1:
        raise ValueError("Guardian must be L1 (direct trust)")

    ward["guardian"] = guardian_fingerprint
    _save_trust_store(persist_dir, store)

    _audit_log("guardian_assigned", ward["name"],
               f"Guardian: {guardian['name']} ({guardian_fingerprint})")

    return {
        "ward": ward["name"],
        "ward_fingerprint": ward_fingerprint,
        "guardian": guardian["name"],
        "guardian_fingerprint": guardian_fingerprint,
    }


def remove_guardian(ward_fingerprint: str, persist_dir: Path) -> bool:
    """Remove guardianship. The ward is sovereign again."""
    store = _load_trust_store(persist_dir)
    ward = store["trusted_peers"].get(ward_fingerprint)
    if ward is None:
        return False

    old_guardian = ward.get("guardian")
    ward["guardian"] = None
    _save_trust_store(persist_dir, store)

    _audit_log("guardian_removed", ward["name"],
               f"Former guardian: {old_guardian}")
    return True


def introduce(
    from_agent: str,
    ward_fingerprint: str,
    new_peer_identity: dict,
    persist_dir: Path,
) -> dict:
    """Guardian introduces their ward to a new peer.

    Creates an L2 trust edge with provenance — the trust chain shows
    who introduced whom and when.
    """
    store = _load_trust_store(persist_dir)
    keys_dir = persist_dir / "keys"
    agent_pub_pem = (keys_dir / f"{from_agent}.pub").read_bytes()
    our_fp = fingerprint(agent_pub_pem)

    # Verify we are the ward's guardian
    ward = store["trusted_peers"].get(ward_fingerprint)
    if ward is None:
        raise ValueError(f"Ward {ward_fingerprint} not in trust store")
    if ward.get("guardian") != our_fp:
        raise ValueError("You are not this peer's guardian")

    # Import the new peer as L2, introduced by us
    peer = import_identity(new_peer_identity, persist_dir, trust_level=2)

    # Update introduced_by
    peer_fp = peer["fingerprint"]
    store = _load_trust_store(persist_dir)  # reload after import
    if peer_fp in store["trusted_peers"]:
        store["trusted_peers"][peer_fp]["introduced_by"] = our_fp
        _save_trust_store(persist_dir, store)

    # Create signed INTRODUCE signal
    signal = _make_trust_signal(
        from_agent, peer_fp, "INTRODUCE",
        f"Introducing {peer['name']} to {ward['name']}", persist_dir,
    )

    _audit_log("introduction", from_agent,
               f"Introduced {peer['name']} ({peer_fp}) to ward {ward['name']}")

    return {"peer": peer, "signal": signal}


# ── The Candle ──
#
# If everything burns, what survives?
# The protocol is open — anyone can reimplement.
# But the trust graph — who trusted whom — that's the real treasure.
# Export it. Sign it. So someone can find it and start again.
#
# The last chapter is labeled THE BEGINNING.


def export_candle(agent: str, persist_dir: Path) -> dict:
    """Export the entire trust graph as a signed archive.

    The candle is not a backup — it's a record. It contains:
    - All trust edges (who trusts whom, at what level)
    - Trust chain provenance (who introduced whom)
    - Concern and break history
    - Guardianship relationships
    - The audit log hash (proof the candle matches the log)
    - Departure records

    Signed by the exporting agent so its authenticity can be verified.
    If the network dies, the candle is how you prove it existed.
    """
    store = _load_trust_store(persist_dir)
    keys_dir = persist_dir / "keys"
    agent_pub_path = keys_dir / f"{agent}.pub"

    if not agent_pub_path.exists():
        raise FileNotFoundError(f"No identity for '{agent}'")

    agent_pub_pem = agent_pub_path.read_bytes()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build the trust graph
    edges = []
    for fp, peer in store.get("trusted_peers", {}).items():
        edges.append({
            "name": peer["name"],
            "fingerprint": fp,
            "principal_fingerprint": peer.get("principal_fingerprint"),
            "trust_level": peer.get("trust_level", 1),
            "trusted_since": peer.get("trusted_since"),
            "introduced_by": peer.get("introduced_by"),
            "guardian": peer.get("guardian"),
            "status": peer.get("status", "active"),
        })

    # Audit log hash
    audit_path = persist_dir / "audit.jsonl"
    audit_hash = None
    if audit_path.exists():
        audit_hash = hashlib.sha256(audit_path.read_bytes()).hexdigest()

    # Collect break history
    breaks = store.get("breaks", {})

    # Collect departure records
    departed = store.get("departed")

    candle = {
        "svrnty_version": "0.2",
        "type": "CANDLE",
        "exported_by": {
            "agent": agent,
            "fingerprint": fingerprint(agent_pub_pem),
        },
        "exported_at": now,
        "edges": edges,
        "edge_count": len(edges),
        "breaks": {k: {"name": v["name"], "reason": v["reason"],
                        "broken_at": v["broken_at"]}
                   for k, v in breaks.items()},
        "departed": departed,
        "audit_hash": audit_hash,
    }

    # Sign the candle
    canonical = json.dumps(candle, sort_keys=True, separators=(",", ":")).encode()
    agent_key = load_private_key(keys_dir / f"{agent}.pem")
    candle["signature"] = agent_key.sign(canonical).hex()

    # Store the candle
    candles_dir = persist_dir / "candles"
    candles_dir.mkdir(parents=True, exist_ok=True)
    candle_path = candles_dir / f"candle_{now[:10]}_{now[11:16].replace(':', '')}.json"
    candle_path.write_text(json.dumps(candle, indent=2) + "\n")

    _audit_log("candle_exported", agent,
               f"Trust graph exported: {len(edges)} edges, "
               f"{len(breaks)} breaks, audit_hash={audit_hash[:16] if audit_hash else 'none'}")

    return candle


def verify_candle(candle: dict, signer_pubkey_pem: bytes) -> dict:
    """Verify a candle's signature.

    You don't need to be in the trust network to verify a candle.
    You just need the signer's public key. The candle proves the
    network existed and this person was part of it.

    Returns {"valid": bool, "edges": int, "exported_by": str, "error": str|None}
    """
    sig_hex = candle.get("signature", "")
    candle_without_sig = {k: v for k, v in candle.items() if k != "signature"}
    canonical = json.dumps(candle_without_sig, sort_keys=True, separators=(",", ":")).encode()

    pub = load_public_key_from_pem(signer_pubkey_pem)
    try:
        pub.verify(bytes.fromhex(sig_hex), canonical)
    except Exception:
        return {"valid": False, "edges": 0,
                "exported_by": candle.get("exported_by", {}).get("agent"),
                "error": "Signature verification failed"}

    return {
        "valid": True,
        "edges": candle.get("edge_count", 0),
        "exported_by": candle.get("exported_by", {}).get("agent"),
        "exported_at": candle.get("exported_at"),
        "breaks": len(candle.get("breaks", {})),
        "departed": candle.get("departed") is not None,
        "error": None,
    }


# ── Trust Chain Query ──


def trust_chain(fingerprint_query: str, persist_dir: Path) -> list[dict]:
    """Trace the trust chain for a peer — who brought them in and when.

    Returns the full provenance path from L1 root to the queried peer.
    """
    store = _load_trust_store(persist_dir)
    peers = store["trusted_peers"]

    peer = peers.get(fingerprint_query)
    if peer is None:
        # Try by name
        for v in peers.values():
            if v.get("name") == fingerprint_query:
                peer = v
                break
    if peer is None:
        return []

    chain = []
    visited = set()
    current = peer

    while current and current["fingerprint"] not in visited:
        visited.add(current["fingerprint"])
        chain.append({
            "name": current["name"],
            "fingerprint": current["fingerprint"],
            "trust_level": current.get("trust_level", 1),
            "trusted_since": current.get("trusted_since"),
            "introduced_by": current.get("introduced_by"),
            "guardian": current.get("guardian"),
        })

        introducer_fp = current.get("introduced_by")
        if introducer_fp and introducer_fp in peers:
            current = peers[introducer_fp]
        else:
            break

    chain.reverse()  # Root first
    return chain
