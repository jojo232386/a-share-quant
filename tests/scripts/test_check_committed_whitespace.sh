#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
checker="$repo_root/scripts/check_committed_whitespace.sh"
tmp_root=$(mktemp -d "${TMPDIR:-/tmp}/aquant-whitespace.XXXXXX")
trap 'rm -rf "$tmp_root"' EXIT HUP INT TERM

repo="$tmp_root/repo"
git init -q "$repo"
git -C "$repo" config user.name ci-test
git -C "$repo" config user.email ci-test@example.invalid

printf '%s\n' clean >"$repo/notes.md"
git -C "$repo" add notes.md
git -C "$repo" commit -qm baseline
base=$(git -C "$repo" rev-parse HEAD)

printf '%s  \n' bad >"$repo/notes.md"
git -C "$repo" commit -qam whitespace-error
bad=$(git -C "$repo" rev-parse HEAD)
if (cd "$repo" && "$checker" "$base" "$bad") >"$tmp_root/whitespace.out" 2>&1; then
    printf '%s\n' 'expected committed trailing whitespace to fail' >&2
    exit 1
fi

printf '%s\n' clean-again >"$repo/notes.md"
git -C "$repo" commit -qam whitespace-fixed
good=$(git -C "$repo" rev-parse HEAD)
(cd "$repo" && "$checker" "$base" "$good")
(cd "$repo" && "$checker" 0000000000000000000000000000000000000000 "$good")

# A sanitized-history branch can be unrelated to the legacy public branch.
git -C "$repo" checkout -q --orphan clean-history
git -C "$repo" rm -q -rf .
printf '%s\n' public-safe >"$repo/public.md"
git -C "$repo" add public.md
git -C "$repo" commit -qm clean-history
unrelated=$(git -C "$repo" rev-parse HEAD)
(cd "$repo" && "$checker" "$base" "$unrelated")

# An uncommitted whitespace error must not affect a submitted-range check.
printf '%s  \n' local-only >"$repo/notes.md"
(cd "$repo" && "$checker" "$base" "$good")

if (cd "$repo" && "$checker" deadbeef "$good") >"$tmp_root/unknown-base.out" 2>&1; then
    printf '%s\n' 'expected an unknown base to fail' >&2
    exit 1
fi

printf '%s\n' 'check_committed_whitespace: pass'
