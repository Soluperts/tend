# SPDX-License-Identifier: MIT
"""Regression: importing the CLI must not touch the OS keyring.

On macOS, eager keyring access at import time triggers a Keychain Access
prompt before tend can print --help, and if the user cancels (or the import
runs in a non-interactive shell), it crashes with KeychainDenied(-128).
That's a hostile first-run experience for anyone who just `pip install
tend-assistant`'d.

This test guards against any future module-level keyring call sneaking back
in via a transitive import. The deferred load-into-env still happens — but
only when something explicitly accesses `tend.config.settings` (the daemon
entry point and `tend doctor` do; `tend --help` and CLI subcommand imports
do not).
"""

from __future__ import annotations

import importlib
import sys


def _drop_tend_modules() -> None:
    for name in list(sys.modules):
        if name == "tend" or name.startswith("tend."):
            del sys.modules[name]


def test_importing_tend_cli_does_not_call_keyring(monkeypatch):
    calls: list[tuple] = []

    import keyring

    def _spy(*args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr(keyring, "get_password", _spy)

    _drop_tend_modules()
    try:
        importlib.import_module("tend.cli")
    finally:
        _drop_tend_modules()

    assert calls == [], (
        f"keyring.get_password was called during `import tend.cli` "
        f"with: {calls}. This regresses the macOS first-run UX — `tend --help` "
        f"will trigger a Keychain Access prompt before any output. Move the "
        f"offending settings access into a command body or behind the lazy "
        f"`tend.config.__getattr__('settings')` path."
    )


def test_importing_tend_config_does_not_call_keyring(monkeypatch):
    """The config module itself must not touch keyring at import."""
    calls: list[tuple] = []

    import keyring

    def _spy(*args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr(keyring, "get_password", _spy)

    _drop_tend_modules()
    try:
        importlib.import_module("tend.config")
    finally:
        _drop_tend_modules()

    assert calls == [], (
        f"keyring.get_password called during `import tend.config`: {calls}"
    )


def test_accessing_settings_singleton_does_call_keyring(monkeypatch):
    """The lazy load IS expected to fire when something reads `settings`.

    This locks in the intentional behavior: deferred-but-still-present.
    """
    calls: list[tuple] = []

    import keyring

    def _spy(service, key, *args, **kwargs):
        calls.append((service, key))
        return None

    monkeypatch.setattr(keyring, "get_password", _spy)

    _drop_tend_modules()
    try:
        import tend.config

        _ = tend.config.settings  # triggers lazy build
    finally:
        _drop_tend_modules()

    assert calls, (
        "Accessing `tend.config.settings` did NOT call keyring — the lazy "
        "load wiring has regressed. Real users with secrets in Keychain "
        "won't see them on daemon startup."
    )
