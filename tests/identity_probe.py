"""Fresh-interpreter probe for deterministic backtest identities."""

from __future__ import annotations

import json
from decimal import Decimal

import pandas as pd

from aquant.backtest import BacktestConfig, StrategyName, run_synthetic_backtest


def main() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2026-07-13",
                    "2026-07-14",
                    "2026-07-15",
                    "2026-07-16",
                ]
            ),
            "open": [10.0, 11.0, 12.0, 13.0],
            "high": [10.5, 11.5, 12.5, 13.5],
            "low": [9.5, 10.5, 11.5, 12.5],
            "close": [10.0, 11.0, 12.0, 13.0],
            "volume": [10_000] * 4,
            "amount": [100_000.0] * 4,
        }
    )
    result = run_synthetic_backtest(
        frame,
        config=BacktestConfig(
            strategy=StrategyName.SMA,
            initial_cash=10_000.0,
            target_weight=Decimal("0.95"),
            sma_period=2,
            random_seed=7,
        ),
    )
    print(
        json.dumps(
            {
                "implementation_digest": result.implementation_digest,
                "run_id": result.run_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
