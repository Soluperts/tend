"""Tend CLI — read-only inspection of worker sessions.

`tend sessions list/show/tail/cat` reads the on-disk SessionStore. Runs as
a separate process from the daemon; no IPC.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from tend.sessions import SessionStore


def _default_root() -> Path:
    return Path.home() / ".tend"


def _store() -> SessionStore:
    return SessionStore(root=_default_root())


def _ago(ts_ms: int) -> str:
    delta_s = int((time.time() * 1000 - ts_ms) / 1000)
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86400:
        return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86400}d ago"


def cmd_sessions_list(args) -> None:
    store = _store()
    rows = store.list_recent(limit=args.limit)
    if args.status:
        rows = [r for r in rows if r.status == args.status]
    if args.json:
        print(json.dumps([r.__dict__ for r in rows], indent=2, default=str))
        return
    if not rows:
        print("No sessions yet.")
        return
    fmt = "{status:<8} {worker:<12} {started:<10} {sid:<12} {request}"
    print(fmt.format(status="STATUS", worker="WORKER",
                     started="STARTED", sid="SESSION", request="REQUEST"))
    for r in rows:
        print(fmt.format(
            status=r.status, worker=r.worker,
            started=_ago(r.started_at), sid=r.session_id[:10],
            request=(r.request or "")[:60],
        ))


def _resolve_session(store: SessionStore, prefix: str):
    rows = store.list_recent(limit=10000)
    return next((r for r in rows if r.session_id.startswith(prefix)), None)


def cmd_sessions_show(args) -> None:
    store = _store()
    match = _resolve_session(store, args.session_id)
    if not match:
        print(f"No session matching '{args.session_id}'", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(match.__dict__, indent=2, default=str))
        return
    for k, v in match.__dict__.items():
        print(f"{k:<22} {v}")


def cmd_sessions_tail(args) -> None:
    store = _store()
    match = _resolve_session(store, args.session_id)
    if not match:
        print(f"No session matching '{args.session_id}'", file=sys.stderr)
        sys.exit(1)
    path = Path(match.transcript_path)
    if not path.exists():
        print(f"Transcript not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r") as f:
        for line in f:
            _print_event(line, args.raw)
        if not getattr(args, "follow", False):
            return
        while True:
            line = f.readline()
            if line:
                _print_event(line, args.raw)
            else:
                time.sleep(0.2)


def cmd_sessions_cat(args) -> None:
    store = _store()
    match = _resolve_session(store, args.session_id)
    if not match:
        print(f"No session matching '{args.session_id}'", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(Path(match.transcript_path).read_text())


def _print_event(line: str, raw: bool) -> None:
    if raw:
        sys.stdout.write(line)
        return
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        sys.stdout.write(line)
        return
    t = ev.get("type", "?")
    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text":
                print(f"[assistant] {block.get('text', '')}")
            elif block.get("type") == "tool_use":
                print(f"[tool_use] {block.get('name')}({json.dumps(block.get('input', {}))[:120]})")
    elif t == "user":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                snippet = content if isinstance(content, str) else json.dumps(content)
                print(f"[tool_result] {snippet[:200]}")
    elif t == "result":
        cost = ev.get("total_cost_usd")
        print(f"[result] subtype={ev.get('subtype')} cost={cost}")
    else:
        print(f"[{t}] {json.dumps(ev)[:200]}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tend")
    sub = p.add_subparsers(dest="cmd", required=True)

    sessions = sub.add_parser("sessions").add_subparsers(dest="action", required=True)

    pl = sessions.add_parser("list"); pl.set_defaults(func=cmd_sessions_list)
    pl.add_argument("--limit", type=int, default=10)
    pl.add_argument("--status", choices=["running", "done", "failed", "killed"])
    pl.add_argument("--json", action="store_true")

    ps = sessions.add_parser("show"); ps.set_defaults(func=cmd_sessions_show)
    ps.add_argument("session_id")
    ps.add_argument("--json", action="store_true")

    pt = sessions.add_parser("tail"); pt.set_defaults(func=cmd_sessions_tail)
    pt.add_argument("session_id")
    pt.add_argument("--follow", action="store_true")
    pt.add_argument("--raw", action="store_true",
                    help="Print raw JSONL instead of pretty-printed events")

    pc = sessions.add_parser("cat"); pc.set_defaults(func=cmd_sessions_cat)
    pc.add_argument("session_id")

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
