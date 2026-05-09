#!/usr/bin/env bash
# gws-agenda.sh — wrapper around `gws calendar +agenda` with sane defaults.
#
# Usage:
#   gws-agenda.sh --calendar <name> --today --json
#   gws-agenda.sh --calendar <name> --time-min <iso> --time-max <iso> --json
#   gws-agenda.sh --next 1 --json

set -euo pipefail
exec gws calendar +agenda "$@"
