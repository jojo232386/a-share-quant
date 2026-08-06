# Engineering Contraction Guardrails

## Purpose

PR-0 records the smallest machine-checkable compatibility surface for future engineering contraction. It does not refactor production code, delete files, merge CLI commands or change A-share financial semantics.

The contract is stored in `tests/contracts/import_contract.json` and verified by `tests/contracts/test_import_contract.py`. It is generated from the accepted `main` baseline recorded in that manifest.

## Current compatibility surface

The contract protects these import paths:

- `aquant`, `aquant.data`, `aquant.backtest`, `aquant.rules`, `aquant.risk`;
- `aquant.portfolio`, `aquant.reporting`, `aquant.research`, `aquant.gate_e`;
- the entry-point modules `aquant.cli`, `aquant.backtest_cli`, `aquant.report_cli`, `aquant.experiment_cli`, `aquant.release_cli`, `aquant.portfolio_cli` and `aquant.gate_e.cli`.

The contract intentionally has no top-level symbol list. In particular, it does not create or freeze an `aquant.__version__` symbol.

The seven console scripts and their current targets are:

| Command | Target |
|---|---|
| `aquant-data` | `aquant.cli:main` |
| `aquant-backtest` | `aquant.backtest_cli:main` |
| `aquant-report` | `aquant.report_cli:main` |
| `aquant-experiment` | `aquant.experiment_cli:main` |
| `aquant-release` | `aquant.release_cli:main` |
| `aquant-portfolio` | `aquant.portfolio_cli:main` |
| `aquant-gate-e` | `aquant.gate_e.cli:main` |

The contract test reads `[project.scripts]` from `pyproject.toml`; this table is the authority. Each target is imported and its `main` attribute is checked for callability without starting a task, reading market data or accessing private configuration.

## Protected capabilities

The contract records five capability boundaries:

1. A-share execution rules: supported instruments, T+1, lot size, price limits, suspension rejection and fees.
2. Data quality and provenance: fail-closed validation, snapshots, manifests and corporate actions.
3. Backtrader adapter boundary: generic engine mechanics remain separated from A-share execution semantics.
4. Portfolio ledger and run identity: shared cash, accounting, availability and deterministic identities.
5. Gate E audit boundary: research-only trust, approval, replay, release and evidence relationships.

Each capability points to repository-relative source and test evidence in the manifest. These references document the boundary; PR-0 does not copy or reimplement those business tests.

## What later contraction may change

Internal implementation may be refactored. Private helpers may be merged, moved or deleted after caller and regression checks. Internal CLI implementations may be unified while keeping the seven command names and their target entry points working. Internal release/replay plumbing and temporary Gate E helpers may change only under their existing audit protections.

## What later contraction must not change silently

- A protected module path must not disappear accidentally.
- Existing command names and target entry points must continue to work unless a separate compatibility-change PR explicitly documents migration impact.
- A financial-semantic change must not be presented as ordinary engineering contraction.
- T+1, lot constraints, price limits, suspension/no-bar handling, fees, data rejection, provenance, portfolio identity and Gate E relationships require behavior evidence when touched.
- The Gate E fixed objects and `v0.2-gate-e-public-audit` are outside ordinary contraction scope.
- `pyproject.toml`, lock files and dependency changes are separate concerns, not implicit contraction edits.

## Updating the contract deliberately

A future PR may change the manifest only when the compatibility surface intentionally changes. That PR must show the exact manifest diff, explain migration impact, preserve or update the relevant tests, and state whether any protected capability or audit boundary is affected. The baseline SHA in the manifest must be updated only when the project deliberately adopts a new accepted baseline; it must never be changed to hide a drift.

The contract is a boundary, not a list of every internal symbol. It should stay small enough to support safe contraction while making public entry points and protected domain/audit capabilities explicit.
