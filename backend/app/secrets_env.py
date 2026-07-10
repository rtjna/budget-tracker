"""Load secrets from DATA_DIR/secrets.env into the process environment.

Secrets live next to the financial data (outside the repository and outside
Claude's readable paths), never in the project tree. Lines are KEY=VALUE;
existing environment variables are never overridden.
"""

import os

from .db import DATA_DIR

SECRETS_PATH = DATA_DIR / "secrets.env"


def load_secrets() -> None:
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
