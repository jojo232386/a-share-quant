#!/bin/sh
# Build the clean candidate as a wheel, install it non-editably beside locked
# dependencies, and invoke the official aquant-experiment console entry point.
set -eu

usage() {
    printf '%s\n' "usage: $0 smoke | run [--] <aquant-experiment arguments...>" >&2
    exit 2
}

asq_runtime_mode=${1:-}
[ -n "$asq_runtime_mode" ] || usage
shift
if [ "${1:-}" = "--" ]; then
    shift
fi
case $asq_runtime_mode in
    smoke) [ "$#" -eq 0 ] || usage ;;
    run) [ "$#" -gt 0 ] || usage ;;
    *) usage ;;
esac

asq_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$asq_runtime_mode" = "run" ]; then
    [ -z "$(git -C "$asq_repo_root" status --porcelain=v1 --untracked-files=all)" ] || {
        printf '%s\n' 'error: formal runtime requires a clean committed repository' >&2
        exit 1
    }
fi

asq_runtime_root=$(mktemp -d "${TMPDIR:-/tmp}/asq-formal-runtime.XXXXXX")
trap 'rm -rf "$asq_runtime_root"' EXIT HUP INT TERM
asq_wheelhouse="$asq_runtime_root/wheelhouse"
asq_runtime_venv="$asq_runtime_root/venv"
mkdir -p "$asq_wheelhouse" "$asq_runtime_root/empty"

(
    cd "$asq_repo_root"
    uv build --wheel --no-sources --out-dir "$asq_wheelhouse" >/dev/null
)
asq_wheel_count=$(find "$asq_wheelhouse" -type f -name '*.whl' | wc -l | tr -d ' ')
[ "$asq_wheel_count" -eq 1 ] || {
    printf '%s\n' 'error: formal runtime must build exactly one wheel' >&2
    exit 1
}
asq_wheel_path=$(find "$asq_wheelhouse" -type f -name '*.whl')
unzip -tq "$asq_wheel_path" >/dev/null
unzip -Z1 "$asq_wheel_path" | grep -Fx 'aquant/experiment_cli.py' >/dev/null
asq_entry_points=$(unzip -Z1 "$asq_wheel_path" | awk '/\.dist-info\/entry_points\.txt$/')
[ "$(printf '%s\n' "$asq_entry_points" | wc -l | tr -d ' ')" -eq 1 ] || {
    printf '%s\n' 'error: wheel must contain exactly one entry_points.txt' >&2
    exit 1
}
unzip -p "$asq_wheel_path" "$asq_entry_points" \
    | grep -Fx 'aquant-experiment = aquant.experiment_cli:main' >/dev/null
asq_wheel_sha256=$(shasum -a 256 "$asq_wheel_path" | awk '{print $1}')

UV_PROJECT_ENVIRONMENT="$asq_runtime_venv" \
    uv sync --frozen --no-dev --no-editable --no-install-project >/dev/null
uv pip install \
    --python "$asq_runtime_venv/bin/python" \
    --no-deps \
    --strict \
    "$asq_wheel_path" >/dev/null
uv pip check --python "$asq_runtime_venv/bin/python" >/dev/null

ASQ_FORMAL_RUNTIME_VENV="$asq_runtime_venv" \
ASQ_FORMAL_REPO_ROOT="$asq_repo_root" \
    "$asq_runtime_venv/bin/python" - <<'PY'
import importlib.metadata
import json
import os
from pathlib import Path

import aquant.experiment_cli

runtime_root = Path(os.environ["ASQ_FORMAL_RUNTIME_VENV"]).resolve()
repo_root = Path(os.environ["ASQ_FORMAL_REPO_ROOT"]).resolve()
module_path = Path(aquant.experiment_cli.__file__).resolve()
if not module_path.is_relative_to(runtime_root):
    raise SystemExit("formal module was not imported from the isolated runtime")
if module_path.is_relative_to(repo_root):
    raise SystemExit("formal module resolved to the editable source tree")
distribution = importlib.metadata.distribution("a-share-quant")
direct_url_text = distribution.read_text("direct_url.json")
if direct_url_text is not None:
    direct_url = json.loads(direct_url_text)
    if direct_url.get("dir_info", {}).get("editable") is True:
        raise SystemExit("formal wheel was installed editably")
print(f"FORMAL_RUNTIME_MODULE_PATH={module_path}")
PY

asq_git_head=$(git -C "$asq_repo_root" rev-parse HEAD)
asq_git_tree=$(git -C "$asq_repo_root" rev-parse 'HEAD^{tree}')
printf 'FORMAL_RUNTIME_GIT_HEAD=%s\n' "$asq_git_head" >&2
printf 'FORMAL_RUNTIME_GIT_TREE=%s\n' "$asq_git_tree" >&2
printf 'FORMAL_RUNTIME_WHEEL_SHA256=%s\n' "$asq_wheel_sha256" >&2
printf '%s\n' 'FORMAL_RUNTIME_INSTALLATION=LOCKED_NON_EDITABLE' >&2

run_official_cli() {
    "$asq_runtime_venv/bin/aquant-experiment" "$@"
}

if [ "$asq_runtime_mode" = "smoke" ]; then
    (
        cd "$asq_runtime_root/empty"
        run_official_cli --help
    ) >"$asq_runtime_root/cli-help.txt"
    grep -F 'usage: aquant-experiment' "$asq_runtime_root/cli-help.txt" >/dev/null
    [ -z "$(find "$asq_runtime_root/empty" -mindepth 1 -print -quit)" ] || {
        printf '%s\n' 'error: CLI smoke wrote into the empty working directory' >&2
        exit 1
    }
    printf '%s\n' 'FORMAL_RUNTIME_IMPORTABLE=TRUE'
    printf '%s\n' 'OFFICIAL_AQUANT_EXPERIMENT_CLI=PASS'
    printf '%s\n' 'STOP_BEFORE_DATA=TRUE'
    printf '%s\n' 'STOP_BEFORE_STRATEGY=TRUE'
    printf '%s\n' 'STOP_BEFORE_METRICS=TRUE'
    exit 0
fi

run_official_cli "$@"
