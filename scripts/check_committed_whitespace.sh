#!/bin/sh
# Check whitespace errors in a committed Git range.  This deliberately avoids
# inspecting the working tree: CI must validate the submitted revision, not
# whichever files happen to exist on the runner.
set -eu

usage() {
    printf '%s\n' "usage: $0 <base-commit-or-zero-sha> <head-commit>" >&2
    exit 2
}

[ "$#" -eq 2 ] || usage

base=$1
head=$2
zero_sha=0000000000000000000000000000000000000000

git rev-parse --verify --quiet "${head}^{commit}" >/dev/null || {
    printf '%s\n' "error: head is not a reachable commit: $head" >&2
    exit 2
}

if [ "$base" = "$zero_sha" ]; then
    # A branch's first push has no usable predecessor.  Compare its committed
    # tree with Git's empty tree so every submitted file is still checked.
    empty_tree=$(git hash-object -t tree /dev/null)
    exec git diff --check "$empty_tree" "$head"
fi

git rev-parse --verify --quiet "${base}^{commit}" >/dev/null || {
    printf '%s\n' "error: base is not a reachable commit: $base" >&2
    exit 2
}

# A clean-history rebuild can intentionally have no merge base with the legacy
# default branch.  In that case the submitted tree itself is the review range.
if ! merge_base=$(git merge-base "$base" "$head"); then
    empty_tree=$(git hash-object -t tree /dev/null)
    exec git diff --check "$empty_tree" "$head"
fi

# Check the submitted branch since its merge base with the target.  This stays
# correct when a related PR base branch advances.
exec git diff --check "$merge_base" "$head"
