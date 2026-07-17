#!/bin/sh
set -eu
script_path=$(readlink -f "$0")
root=$(CDPATH= cd -- "$(dirname -- "$script_path")/.." && pwd)
exec "$root/scripts/desk" --json health --due

