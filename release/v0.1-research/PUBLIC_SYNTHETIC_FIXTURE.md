# Public v0.1 synthetic fixture

This directory's frozen inputs are generated locally with
`scripts/generate_public_v01_inputs.py`. They are deterministic engineering
fixtures, not downloaded A-share prices, official trading-calendar evidence, or
company-action records.

The fixture retains ten six-digit identifiers only to exercise the project's
supported stock and ETF branches. It does not represent a historical universe,
investment recommendation, profitability result, or live-trading capability.

The fixture spans 2018-01-02 through 2026-07-24. Its calendar has exactly 2,074
sessions: it is derived from weekdays and deterministically removes 160
synthetic non-sessions. Those removals are not claims about official exchange
closures. Across the ten instruments, the deterministic snapshots contain 28
intentional missing bars to exercise conservative no-bar handling. Four
synthetic cash-only corporate-action snapshots contain one event each; the
remaining six are deliberately empty.

The generator declares both market and corporate-action provenance as
`synthetic_public_fixture`; it does not call AKShare or any network service.
