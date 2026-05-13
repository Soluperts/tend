"""Secret storage for tend — OS keyring first, $TEND_HOME/.env fallback.

The .env fallback exists because keyring on a headless Pi typically has no
backend (GNOME Keyring / KWallet need a desktop session). Surface the
fallback explicitly to the user in setup output.
"""

from __future__ import annotations

import os
import re
from typing import Literal

import keyring as _keyring
import keyring.errors as _keyring_errors

from tend import paths


_KNOWN: list[str] = [
    "ANTHROPIC_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "TEND_WEBHOOK_TOKEN",
]

_SERVICE = "tend"


def list_known_secrets() -> list[str]:
    return list(_KNOWN)


def set_secret(key: str, value: str) -> Literal["keyring", "dotenv"]:
    """Store `value` under (service='tend', username=key).

    Tries keyring first; on NoKeyringError or PasswordSetError, appends or
    replaces in `$TEND_HOME/.env` with mode 600. Returns the backend used.
    """
    try:
        _keyring.set_password(_SERVICE, key, value)
        return "keyring"
    except (
        _keyring_errors.NoKeyringError,
        _keyring_errors.PasswordSetError,
    ):
        _write_dotenv(key, value)
        return "dotenv"


def get_secret(key: str) -> str | None:
    """Look up `key` in env vars → keyring → $TEND_HOME/.env. None if absent."""
    v = os.environ.get(key)
    if v:
        return v
    try:
        v = _keyring.get_password(_SERVICE, key)
    except _keyring_errors.NoKeyringError:
        v = None
    if v:
        return v
    return _read_dotenv(key)


def secret_backend(key: str) -> Literal["env", "keyring", "dotenv"] | None:
    """Where the secret currently lives. None if unset."""
    if os.environ.get(key):
        return "env"
    try:
        if _keyring.get_password(_SERVICE, key):
            return "keyring"
    except _keyring_errors.NoKeyringError:
        pass
    if _read_dotenv(key) is not None:
        return "dotenv"
    return None


_LINE_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def _read_dotenv(key: str) -> str | None:
    p = paths.env_path()
    if not p.exists():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _LINE_RE.match(line.strip())
        if m and m.group(1) == key:
            return m.group(2)
    return None


def _write_dotenv(key: str, value: str) -> None:
    p = paths.env_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, str] = {}
    order: list[str] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = _LINE_RE.match(line.strip())
            if m:
                if m.group(1) not in existing:
                    order.append(m.group(1))
                existing[m.group(1)] = m.group(2)
    if key not in existing:
        order.append(key)
    existing[key] = value

    body = "\n".join(f"{k}={existing[k]}" for k in order) + "\n"
    tmp = p.with_suffix(".env.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(p)
    p.chmod(0o600)
