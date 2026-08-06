"""Fresh-interpreter probe for deterministic portfolio identities."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from portfolio_gate_c_support import make_portfolio_case

from aquant.portfolio import export_portfolio_run, run_verified_portfolio


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        run = run_verified_portfolio(
            **make_portfolio_case(
                root / "inputs",
                symbols=("600001", "600000"),
            )
        )
        artifact = export_portfolio_run(run, root / "outputs")
        artifact_sha256 = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(
                artifact.iterdir(),
                key=lambda item: item.name,
            )
        }
    print(
        json.dumps(
            {
                "artifact_sha256": artifact_sha256,
                "implementation_digest": run.identity.implementation_digest,
                "input_closure_digest": run.identity.input_closure_digest,
                "run_id": run.identity.run_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
