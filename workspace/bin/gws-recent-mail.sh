#!/usr/bin/env bash
# gws-recent-mail.sh — wrapper for gmail reads.
#
# Usage:
#   gws-recent-mail.sh --triage --json
#   gws-recent-mail.sh --from sender@example.com --limit 3 --json

set -euo pipefail

mode=""
if [[ "${1:-}" == "--triage" ]]; then
    shift
    exec gws gmail +triage "$@"
elif [[ "${1:-}" == "--from" ]]; then
    shift
    sender="$1"; shift
    exec gws gmail list --query "from:$sender" "$@"
else
    exec gws gmail list "$@"
fi
