#!/usr/bin/env bash
# gws-find-slot.sh — find the largest free slot on a given day in a window.
#
# Usage:
#   gws-find-slot.sh --day 2026-05-09 --window morning --duration 60 --json
#
# Reads watched calendars from tend.toml; returns largest contiguous free slot.

set -euo pipefail

day=""
window=""
duration=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --day) day="$2"; shift 2 ;;
        --window) window="$2"; shift 2 ;;
        --duration) duration="$2"; shift 2 ;;
        --json) shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

case "$window" in
    morning) start_h=7; end_h=12 ;;
    afternoon) start_h=12; end_h=17 ;;
    evening) start_h=17; end_h=21 ;;
    *) echo "window must be morning|afternoon|evening" >&2; exit 2 ;;
esac

# Pull events for the day from the primary calendar (gws default).
events_json="$(gws calendar +agenda \
    --time-min "${day}T${start_h}:00:00+00:00" \
    --time-max "${day}T${end_h}:00:00+00:00" \
    --json)"

# Largest free slot calculation: emit the day window minus any overlaps.
# For v1, use a simple python helper. (jq alone gets unwieldy.)
python3 - "$events_json" "$day" "$start_h" "$end_h" "$duration" <<'PY'
import json, sys
events = json.loads(sys.argv[1])
day = sys.argv[2]
sh = int(sys.argv[3])
eh = int(sys.argv[4])
need_min = int(sys.argv[5])
import datetime as dt
start = dt.datetime.fromisoformat(f"{day}T{sh:02d}:00:00+00:00")
end = dt.datetime.fromisoformat(f"{day}T{eh:02d}:00:00+00:00")
busy = []
for ev in events:
    try:
        s = dt.datetime.fromisoformat(ev["start"]["dateTime"])
        e = dt.datetime.fromisoformat(ev["end"]["dateTime"])
        busy.append((max(s, start), min(e, end)))
    except (KeyError, ValueError):
        continue
busy.sort()
free = []
cursor = start
for s, e in busy:
    if s > cursor:
        free.append((cursor, s))
    cursor = max(cursor, e)
if cursor < end:
    free.append((cursor, end))
free.sort(key=lambda t: -(t[1] - t[0]).total_seconds())
out = [{"start": s.isoformat(), "end": e.isoformat(),
        "minutes": int((e - s).total_seconds() / 60)} for s, e in free]
out = [r for r in out if r["minutes"] >= need_min]
print(json.dumps(out[:3]))  # top three by length
PY
