#!/bin/sh
set -eu

command -v uv >/dev/null 2>&1 || {
  printf '%s\n' '{"error_code":"uv_not_found","error_type":"EnvironmentError","status":"error"}' >&2
  exit 1
}

if ! uv lock --check >/dev/null 2>&1; then
  printf '%s\n' '{"error_code":"lock_check_failed","error_type":"EnvironmentError","status":"error"}' >&2
  exit 1
fi
exec uv run --no-sync aquant-release verify --project-root .
