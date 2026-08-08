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

expect_pass() {
    if ! (cd "$repo" && "$checker" "$@") >"$tmp_root/expect-pass.out" 2>&1; then
        printf 'expected success from checker %s\n' "$*" >&2
        cat "$tmp_root/expect-pass.out" >&2
        exit 1
    fi
}

expect_fail() {
    if (cd "$repo" && "$checker" "$@") >"$tmp_root/expect-fail.out" 2>&1; then
        printf 'expected failure from checker %s\n' "$*" >&2
        exit 1
    fi
}

# `github.event.before` stops being reachable after an amend, rebase, or
# force-push, because CI clones only what a ref still points at.
missing=deadbeef00000000000000000000000000000000
if git -C "$repo" rev-parse --verify --quiet "${missing}^{commit}" >/dev/null; then
    printf '%s\n' 'test setup error: the placeholder base unexpectedly exists' >&2
    exit 1
fi

# Without the opt-in the strict contract is unchanged: a base CI cannot resolve
# is still a hard error rather than a silent pass.
expect_fail "$missing" "$good"

# With the opt-in an unreachable base falls back to the whole submitted tree
# instead of failing the build for valid code.
expect_pass --fallback-whole-tree "$missing" "$good"

# The fallback must not become an escape hatch: a committed whitespace error in
# the submitted tree still fails when the base is unreachable.
expect_fail --fallback-whole-tree "$missing" "$bad"

# The opt-in must not weaken an ordinary fast-forward push either.
expect_fail --fallback-whole-tree "$base" "$bad"
expect_pass --fallback-whole-tree "$base" "$good"

# Drop the untracked leftover so the branch checkouts below are conflict-free.
rm -f "$repo/notes.md"

# A rewritten or sibling tip that is still reachable must be compared through
# its merge base rather than rejected.
git -C "$repo" checkout -q -b sibling "$base"
printf '%s\n' sibling >"$repo/sibling.md"
git -C "$repo" add sibling.md
git -C "$repo" commit -qm sibling-commit
sibling=$(git -C "$repo" rev-parse HEAD)
if git -C "$repo" merge-base --is-ancestor "$sibling" "$good"; then
    printf '%s\n' 'test setup error: the sibling commit must not be an ancestor' >&2
    exit 1
fi
expect_pass --fallback-whole-tree "$sibling" "$good"
expect_fail --fallback-whole-tree "$sibling" "$bad"

# A reachable base must keep using the incremental range.  This branch carries
# an older committed whitespace error, so a silent whole-tree scan would fail
# here even though the pushed range is clean.
git -C "$repo" checkout -q -b legacy-whitespace "$base"
printf '%s  \n' legacy >"$repo/legacy.md"
git -C "$repo" add legacy.md
git -C "$repo" commit -qm legacy-whitespace
legacy_base=$(git -C "$repo" rev-parse HEAD)
printf '%s\n' follow-up >"$repo/follow-up.md"
git -C "$repo" add follow-up.md
git -C "$repo" commit -qm clean-follow-up
legacy_head=$(git -C "$repo" rev-parse HEAD)
expect_pass --fallback-whole-tree "$legacy_base" "$legacy_head"

# ...and the fallback really does widen the range when the base is gone.
expect_fail --fallback-whole-tree "$missing" "$legacy_head"

printf '%s\n' 'check_committed_whitespace: pass'
