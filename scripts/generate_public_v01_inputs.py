"""Generate public-only, deterministic synthetic inputs for v0.1 release replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aquant.release_synthetic import build_public_v01_inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_public_v01_inputs(args.release_root)
    print(
        json.dumps(
            {
                "calendar_id": result.calendar_id,
                "fixture_version": result.fixture_version,
                "status": "ok",
                "universe_id": result.universe_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
