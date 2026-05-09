#!/usr/bin/env bash
# gws-agenda.sh — wrapper around `gws calendar +agenda` with sane defaults.
#
# Translates skill-friendly flags into real gws flags:
#   --json          -> --format json
#   --next N        -> --days N         (rough approximation)
# Passes through gws +agenda flags unchanged:
#   --today, --tomorrow, --week, --days N, --calendar NAME, --timezone TZ
#
# Usage:
#   gws-agenda.sh --calendar <name> --today --json
#   gws-agenda.sh --next 1 --json
#
# Note: --calendar takes a calendar's summary or ID, not the literal
# "primary" alias. Use the actual summary (your email for the primary
# calendar) in tend.toml's google.watched_calendars list.
#
# For time-window queries (--time-min/--time-max), use
# gws-events-window.sh instead.

set -euo pipefail

args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json) args+=("--format" "json"); shift ;;
        --next) args+=("--days" "$2"); shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done

exec gws calendar +agenda "${args[@]}"
