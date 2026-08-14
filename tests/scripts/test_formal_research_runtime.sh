#!/bin/sh
set -eu

asq_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
asq_runtime="$asq_repo_root/scripts/formal_research_runtime.sh"
asq_output=$("$asq_runtime" smoke 2>&1)

for asq_expected in \
    'FORMAL_RUNTIME_INSTALLATION=LOCKED_NON_EDITABLE' \
    'FORMAL_RUNTIME_IMPORTABLE=TRUE' \
    'OFFICIAL_AQUANT_EXPERIMENT_CLI=PASS' \
    'STOP_BEFORE_DATA=TRUE' \
    'STOP_BEFORE_STRATEGY=TRUE' \
    'STOP_BEFORE_METRICS=TRUE'
do
    printf '%s\n' "$asq_output" | grep -Fx "$asq_expected" >/dev/null || {
        printf 'missing formal-runtime smoke marker: %s\n' "$asq_expected" >&2
        exit 1
    }
done

printf '%s\n' "$asq_output" \
    | grep -E '^FORMAL_RUNTIME_MODULE_PATH=.*/venv/lib/python3\.11/site-packages/aquant/experiment_cli\.py$' \
    >/dev/null
printf '%s\n' "$asq_output" \
    | grep -E '^FORMAL_RUNTIME_WHEEL_SHA256=[0-9a-f]{64}$' \
    >/dev/null

printf '%s\n' 'formal_research_runtime: pass'
