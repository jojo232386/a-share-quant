"""Machine-readable command line entry point for Gate E release replay."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from aquant.gate_e.replay import (
    GateEReplayError,
    audit_candidate,
    build_trust_from_candidate,
    create_gate_e_replay,
    create_gate_e_replay_b,
    replay_environment_b,
    run_candidate_a,
    verify_candidate_trust,
)

GATE_E_COMMANDS = (
    "candidate-a",
    "audit-candidate",
    "build-trust",
    "verify-trust",
    "replay-b",
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise GateEReplayError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="aquant-gate-e")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SafeArgumentParser,
    )

    candidate = subparsers.add_parser("candidate-a")
    candidate.add_argument("--config", required=True)
    candidate.add_argument("--wheel", required=True)
    candidate.add_argument("--wheelhouse", required=True)
    candidate.add_argument("--workspace", required=True)

    audit = subparsers.add_parser("audit-candidate")
    audit.add_argument("--evidence", required=True)
    audit.add_argument("--artifact", required=True)

    build = subparsers.add_parser("build-trust")
    build.add_argument("--evidence", required=True)
    build.add_argument("--approved-review", required=True)

    verify = subparsers.add_parser("verify-trust")
    verify.add_argument("--trust", required=True)
    verify.add_argument("--evidence", required=True)
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--approved-review", required=True)

    replay = subparsers.add_parser("replay-b")
    replay.add_argument("--trust-anchor-commit", required=True)
    replay.add_argument("--approval-commit", required=True)
    replay.add_argument("--trust-path", required=True)
    replay.add_argument("--wheel", required=True)
    replay.add_argument("--wheelhouse", required=True)
    replay.add_argument("--workspace-a", required=True)
    replay.add_argument("--workspace-b", required=True)
    return parser


def parse_gate_e_arguments(
    argv: Sequence[str] | None,
) -> argparse.Namespace:
    """Parse only the five reviewed Gate E commands."""
    return _parser().parse_args(argv)


def _write_json(stream, payload: dict[str, object]) -> None:
    stream.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one Gate E controller command with sanitized machine output."""
    try:
        args = parse_gate_e_arguments(argv)
        raw_output: bytes | None = None
        if args.command == "candidate-a":
            replay = create_gate_e_replay(
                config=Path(args.config),
                project_wheel=Path(args.wheel),
                wheelhouse=Path(args.wheelhouse),
                workspace_a=Path(args.workspace),
            )
            result = run_candidate_a(replay)
            payload = {
                "candidate": "A",
                "evidence": result.evidence_path.name,
                "run_id": result.run_id,
                "stage_count": len(result.progress),
                "status": "candidate",
                "trusted": False,
            }
        elif args.command == "audit-candidate":
            payload = audit_candidate(
                evidence_path=Path(args.evidence),
                artifact=Path(args.artifact),
            )
        elif args.command == "build-trust":
            raw_output = build_trust_from_candidate(
                evidence_path=Path(args.evidence),
                approved_review=Path(args.approved_review),
            )
            payload = {}
        elif args.command == "verify-trust":
            payload = verify_candidate_trust(
                trust=Path(args.trust),
                evidence_path=Path(args.evidence),
                artifact=Path(args.artifact),
                approved_review=Path(args.approved_review),
            )
        elif args.command == "replay-b":
            replay = create_gate_e_replay_b(
                project_wheel=Path(args.wheel),
                wheelhouse=Path(args.wheelhouse),
                workspace_a=Path(args.workspace_a),
                workspace_b=Path(args.workspace_b),
            )
            result = replay_environment_b(
                replay,
                trust_anchor_commit=args.trust_anchor_commit,
                approval_commit=args.approval_commit,
                trust_path=Path(args.trust_path),
            )
            payload = {
                "candidate": "B",
                "run_id": result.run_id,
                "status": "verified_replay",
                "trusted": True,
            }
        else:  # pragma: no cover - parser narrows this
            raise GateEReplayError("invalid_arguments")
    except Exception as exc:
        _write_json(
            sys.stderr,
            {
                "error_code": getattr(exc, "code", "operation_failed"),
                "error_type": type(exc).__name__,
                "status": "error",
            },
        )
        return 1
    if raw_output is not None:
        sys.stdout.write(raw_output.decode("utf-8"))
        return 0
    _write_json(sys.stdout, payload)
    return 0

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
