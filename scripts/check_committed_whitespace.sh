#!/bin/sh
# Check whitespace errors in a committed Git range.  This deliberately avoids
# inspecting the working tree: CI must validate the submitted revision, not
# whichever files happen to exist on the runner.
set -eu

usage() {
    printf '%s\n' "usage: $0 [--fallback-whole-tree] <base-commit-or-zero-sha> <head-commit>" >&2
    exit 2
}

# When the base cannot be resolved, escalate to a whole-tree scan instead of
# erroring out.  This is opt-in because a mistyped base should stay a hard
# failure for direct callers.
fallback_whole_tree=0

while [ "$#" -gt 0 ]; do
    case $1 in
        --fallback-whole-tree) fallback_whole_tree=1; shift ;;
        --) shift; break ;;
        -*) usage ;;
        *) break ;;
    esac
done

[ "$#" -eq 2 ] || usage

base=$1
head=$2
zero_sha=0000000000000000000000000000000000000000

# Compare the submitted tree with Git's empty tree so every committed file is
# checked.  This is the strictest range available: it can only report more
# whitespace errors than a two-commit diff, never fewer.
check_whole_tree() {
    empty_tree=$(git hash-object -t tree /dev/null)
    exec git diff --check "$empty_tree" "$head"
}

git rev-parse --verify --quiet "${head}^{commit}" >/dev/null || {
    printf '%s\n' "error: head is not a reachable commit: $head" >&2
    exit 2
}

if [ "$base" = "$zero_sha" ]; then
    # A branch's first push has no usable predecessor.
    check_whole_tree
fi

if ! git rev-parse --verify --quiet "${base}^{commit}" >/dev/null; then
    if [ "$fallback_whole_tree" -eq 0 ]; then
        printf '%s\n' "error: base is not a reachable commit: $base" >&2
        exit 2
    fi
    # An amend, rebase, or force-push orphans the previous branch tip, so CI's
    # fresh clone never receives it.  Scan the whole submitted tree rather than
    # the unavailable range: the check is escalated, not skipped, so genuine
    # committed whitespace errors still fail.
    printf '%s\n' "notice: base is unreachable ($base); checking the whole submitted tree" >&2
    check_whole_tree
fi

# A clean-history rebuild can intentionally have no merge base with the legacy
# default branch.  In that case the submitted tree itself is the review range.
if ! merge_base=$(git merge-base "$base" "$head"); then
    check_whole_tree
fi

# Check the submitted branch since its merge base with the target.  This stays
# correct when a related PR base branch advances, and when the pushed tip is a
# sibling of the previous tip rather than a descendant.
exec git diff --check "$merge_base" "$head"
