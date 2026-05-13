#!/usr/bin/env bash
# gws-recent-mail.sh — wrapper for gmail reads.
#
# Translates skill-friendly flags into real gws flags:
#   --json           -> --format json
#   --limit N        -> --max N        (only meaningful with +triage)
#   --from EMAIL     -> +triage --query "from:EMAIL"
#
# Usage:
#   gws-recent-mail.sh --triage --json
#   gws-recent-mail.sh --triage --query "label:important" --json
#   gws-recent-mail.sh --from sender@example.com --limit 3 --json

set -euo pipefail

mode=""
sender=""
extra=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --triage) mode="triage"; shift ;;
        --from) mode="from"; sender="$2"; shift 2 ;;
        --json) extra+=("--format" "json"); shift ;;
        --limit) extra+=("--max" "$2"); shift 2 ;;
        *) extra+=("$1"); shift ;;
    esac
done

case "$mode" in
    triage)
        exec gws gmail +triage "${extra[@]}"
        ;;
    from)
        if [[ -z "$sender" ]]; then
            echo "--from requires an email address" >&2
            exit 2
        fi
        exec gws gmail +triage --query "from:$sender" "${extra[@]}"
        ;;
    *)
        echo "must pass --triage or --from <email>" >&2
        exit 2
        ;;
esac
