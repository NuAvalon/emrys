"""License validation for paid tier features."""

import hashlib
from pathlib import Path

from cairn_ai.db import get_persist_dir

_UPGRADE_MSG = (
    "This feature requires cairn Pro. "
    "Visit https://nuavalon.dev/cairn for details, "
    "or run `cairn license <key>` to activate."
)


def check_license() -> bool:
    """Check .persist/license for a valid paid license key.

    Returns True if a valid license exists. The license file contains
    a single key string. Validation is local — no phone-home.
    """
    license_file = get_persist_dir() / "license"
    if not license_file.exists():
        return False

    key = license_file.read_text().strip()
    if not key:
        return False

    # Simple local validation: key must match format and checksum
    # Format: CP-XXXX-XXXX-XXXX-XXXX (20 chars + 4 dashes)
    parts = key.split("-")
    if len(parts) != 5 or parts[0] != "CP":
        return False

    # Last segment is a checksum of the first four
    payload = "-".join(parts[:4])
    expected = hashlib.sha256(payload.encode()).hexdigest()[:4].upper()
    return parts[4] == expected


def upgrade_message(feature: str = "") -> str:
    """Return a friendly upgrade message for unlicensed paid features."""
    if feature:
        return f"{feature} requires Cairn Pro. Visit https://nuavalon.dev/cairn for details."
    return _UPGRADE_MSG
