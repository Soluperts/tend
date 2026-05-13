"""Tests for tend.paths — single source of truth for filesystem paths."""

from __future__ import annotations

from pathlib import Path


def test_tend_home_default(monkeypatch):
    monkeypatch.delenv("TEND_HOME", raising=False)
    from tend.paths import tend_home
    assert tend_home() == Path.home() / ".config" / "tend"


def test_tend_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_HOME", str(tmp_path))
    from tend.paths import tend_home
    assert tend_home() == tmp_path


def test_tend_home_env_not_expanded(monkeypatch):
    """$TEND_HOME with a leading ~ stays literal — we don't expand."""
    monkeypatch.setenv("TEND_HOME", "~/custom")
    from tend.paths import tend_home
    assert tend_home() == Path("~/custom")
