"""CLI behaviour. We invoke main(argv) directly so tests don't need a shell."""

import json

import pytest

from tend.sessions import SessionStore


@pytest.fixture
def store_with_rows(tmp_path, monkeypatch):
    """Set up a populated SessionStore at a temp path; patch the CLI's root."""
    root = tmp_path / "tend-home"
    store = SessionStore(root=root)
    store.start(session_id="aaaa1111", worker="coding", request="rename foo", cwd=None)
    store.complete("aaaa1111", status="done", spoken_summary="all renamed")
    store.start(session_id="bbbb2222", worker="coding", request="fix bug", cwd=None)
    # Tail content
    transcript = store.transcript_path("bbbb2222")
    transcript.write_bytes(
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": "hello"}]}}).encode()
        + b"\n"
        + json.dumps({"type": "result", "subtype": "success",
                      "total_cost_usd": 0.01}).encode()
        + b"\n"
    )
    monkeypatch.setattr("tend.cli._default_root", lambda: root)
    return store, root


def test_cli_sessions_list_shows_recent(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "list"])
    out = capsys.readouterr().out
    assert "aaaa1111"[:8] in out
    assert "bbbb2222"[:8] in out
    assert "rename foo" in out
    assert "running" in out and "done" in out


def test_cli_sessions_list_filter_status(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "list", "--status", "running"])
    out = capsys.readouterr().out
    assert "bbbb2222"[:8] in out
    assert "aaaa1111"[:8] not in out


def test_cli_sessions_list_json(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert {r["session_id"] for r in rows} == {"aaaa1111", "bbbb2222"}


def test_cli_sessions_show_resolves_prefix(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "show", "aaaa"])
    out = capsys.readouterr().out
    assert "aaaa1111" in out
    assert "rename foo" in out
    assert "done" in out


def test_cli_sessions_show_unknown_returns_nonzero(store_with_rows):
    from tend.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["sessions", "show", "zzzz"])
    assert exc.value.code == 1


def test_cli_sessions_tail_pretty_prints(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "tail", "bbbb2222"])
    out = capsys.readouterr().out
    assert "[assistant]" in out
    assert "hello" in out
    assert "[result]" in out


def test_cli_sessions_tail_raw(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "tail", "bbbb2222", "--raw"])
    out = capsys.readouterr().out
    # Raw passes through the jsonl lines verbatim
    assert '"type": "assistant"' in out


def test_cli_sessions_cat_emits_bytes(capsys, store_with_rows):
    from tend.cli import main
    main(["sessions", "cat", "bbbb2222"])
    out = capsys.readouterr().out
    assert "assistant" in out


def test_cli_snapshot_writes_file(tmp_path, monkeypatch, capsys):
    """`tend snapshot` writes ~/.tend/claude-env.md with all expected sections."""
    from tend import cli

    root = tmp_path / "tend-home"
    root.mkdir()
    monkeypatch.setattr(cli, "_default_root", lambda: root)

    # Stub out claude_env_data so we don't shell out in tests.
    monkeypatch.setattr(cli, "_claude_version", lambda: "claude 1.2.3")
    monkeypatch.setattr(cli, "_mcp_list_markdown",
                        lambda: "| sheets | mcp__sheets__ | user | running |")

    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "skills" / "demo").mkdir(parents=True)
    (fake_home / ".claude" / "agents").mkdir(parents=True)
    (fake_home / ".claude" / "commands").mkdir(parents=True)
    (fake_home / ".claude" / "agents" / "agent-x.md").write_text("x")
    monkeypatch.setattr(cli, "_claude_home", lambda: fake_home / ".claude")

    cli.main(["snapshot"])
    out = capsys.readouterr().out
    target = root / "claude-env.md"
    assert target.exists()
    assert "Wrote" in out
    text = target.read_text()
    assert "MCP servers" in text
    assert "Skills" in text
    assert "demo" in text
    assert "Agents" in text
    assert "agent-x" in text
    assert "Suggested tend.toml additions" in text
    assert "claude 1.2.3" in text


def test_cli_snapshot_handles_missing_claude(monkeypatch, tmp_path, capsys):
    from tend import cli

    monkeypatch.setattr(cli, "_default_root", lambda: tmp_path / "tend-home")
    monkeypatch.setattr("shutil.which", lambda _: None)

    with pytest.raises(SystemExit) as exc:
        cli.main(["snapshot"])
    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "claude" in err.lower()
