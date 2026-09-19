#!/usr/bin/env bash
set -euo pipefail

output="$("$(dirname "$0")/desk" --json health --due)"
case "$output" in
  *'"status": "ALREADY_DONE"'*) exit 0 ;;
esac
printf '%s\n' "$output" | "$(dirname "$0")/../.venv/bin/python" -m crypto_desk.notifications health
