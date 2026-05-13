#!/usr/bin/env bash
# tend-schedule-event.sh — convenience wrapper for `tend schedule add --event`.
#
# Usage:
#   tend-schedule-event.sh --when <iso> --event <kind> --payload <json> --name <unique>

set -euo pipefail
exec tend schedule add "$@"
