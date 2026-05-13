#!/usr/bin/env bash
# gws-events-window.sh — query events on a specific calendar within a
# time window. Wraps `gws calendar events list --params '{...}'`.
#
# Usage:
#   gws-events-window.sh --calendar <id> --time-min <iso> --time-max <iso> [--json]
#
# Defaults --calendar to "primary" if not given. The --json flag is
# accepted for skill-script compatibility (output is always JSON).
#
# Output shape: a JSON object with an "items" array (the raw
# Calendar API events.list response).

set -euo pipefail

calendar_id="primary"
time_min=""
time_max=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --calendar) calendar_id="$2"; shift 2 ;;
        --time-min) time_min="$2"; shift 2 ;;
        --time-max) time_max="$2"; shift 2 ;;
        --json) shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$time_min" || -z "$time_max" ]]; then
    echo "--time-min and --time-max are required" >&2
    exit 2
fi

params="$(python3 -c "import json,sys; print(json.dumps({'calendarId': sys.argv[1], 'timeMin': sys.argv[2], 'timeMax': sys.argv[3], 'singleEvents': True, 'orderBy': 'startTime'}))" "$calendar_id" "$time_min" "$time_max")"

exec gws calendar events list --params "$params" --format json
