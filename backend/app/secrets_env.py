"""Secret lookup: environment → macOS Keychain → DATA_DIR/secrets.env.

The Keychain is the preferred store on the Mac (encrypted at rest, never a
plaintext file). The secrets.env fallback exists for Docker/Linux, where
there is no Keychain. Secrets never live in the project tree.

Store a secret in the Keychain (prompts for the value, hidden input):
    security add-generic-password -s budget-tracker -a ANTHROPIC_API_KEY -w
"""

import os
import subprocess
import sys

from .db import DATA_DIR

SECRETS_PATH = DATA_DIR / "secrets.env"
KEYCHAIN_SERVICE = "budget-tracker"


def load_secrets() -> None:
    """Load DATA_DIR/secrets.env into the environment (existing vars win)."""
    if not SECRETS_PATH.exists():
        return
    for line in SECRETS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def _from_keychain(name: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_secret(name: str) -> str | None:
    return os.environ.get(name) or _from_keychain(name)
