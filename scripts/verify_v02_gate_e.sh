#!/bin/sh
set -eu

: "${AQUANT_GATE_E_CLI:?AQUANT_GATE_E_CLI must name the installed Gate E CLI}"
case "$AQUANT_GATE_E_CLI" in
    /*) ;;
    *) exit 64 ;;
esac
[ -x "$AQUANT_GATE_E_CLI" ] || exit 64

unset PYTHONPATH

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$project_root"
git ls-files --error-unmatch -- uv.lock >/dev/null
git diff --quiet -- uv.lock
git diff --cached --quiet -- uv.lock

exec "$AQUANT_GATE_E_CLI" "$@"
