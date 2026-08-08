"""Deterministic signal contract tests (A1)."""

from __future__ import annotations

import decimal
from datetime import date
from decimal import Decimal

import pytest

from aquant.backtest import BacktestConfig, StrategyName, load_verified_snapshot, run_backtest
from aquant.backtest.price_streams import derive_price_streams
from aquant.data.calendar_snapshot import CalendarSnapshotStore, load_verified_calendar
from aquant.data.corporate_actions import (
    load_verified_corporate_actions,
    read_corporate_action_manifest,
)
from aquant.data.manifest import ManifestWriter
from aquant.release_synthetic import build_public_v01_inputs
from aquant.research.signals import (
    SIGNAL_REGISTRY,
    Signal,
    SignalError,
    SignalInput,
    SignalObservation,
    SmaSignal,
    TopKMomentumSignal,
    validate_signal_output,
)
from aquant.rules import default_fee_policy
from aquant.universe import load_verified_universe

_SYMBOL = "600519"
_FIRST = date(2026, 1, 5)


def _observations(*closes: float, start: date = _FIRST) -> tuple[SignalObservation, ...]:
    return tuple(
        SignalObservation(
            session=date.fromordinal(start.toordinal() + index),
            indicator_close=float(close),
        )
        for index, close in enumerate(closes)
    )


def _input(
    *closes: float,
    symbols: tuple[str, ...] = (_SYMBOL,),
    as_of: date | None = None,
) -> SignalInput:
    if as_of is None:
        as_of = (
            date.fromordinal(_FIRST.toordinal() + len(closes) - 1)
            if closes
            else _FIRST
        )
    return SignalInput(
        as_of=as_of,
        per_symbol={symbol: _observations(*closes) for symbol in symbols},
    )


# ---------------------------------------------------------------------------
# A. Contract / causality
# ---------------------------------------------------------------------------


def test_future_observation_later_than_as_of_is_rejected():
    with pytest.raises(SignalError) as exc:
        SignalInput(as_of=_FIRST, per_symbol={_SYMBOL: _observations(10.0, 20.0)})
    assert exc.value.code == "future_observation"


def test_non_ascending_or_duplicate_sessions_rejected():
    for values in (
        # duplicate session
        (
            SignalObservation(date(2026, 1, 6), 10.0),
            SignalObservation(date(2026, 1, 6), 11.0),
        ),
        # out-of-order sessions
        (
            SignalObservation(date(2026, 1, 7), 10.0),
            SignalObservation(date(2026, 1, 6), 11.0),
        ),
    ):
        with pytest.raises(SignalError) as exc:
            SignalInput(as_of=date(2026, 1, 7), per_symbol={_SYMBOL: values})
        assert exc.value.code == "non_ascending_sessions"


def test_as_of_must_be_a_date():
    for bad in ("2026-01-05", 5, None):
        with pytest.raises(SignalError) as exc:
            SignalInput(as_of=bad, per_symbol={_SYMBOL: ()})
        assert exc.value.code == "invalid_as_of"


def test_float_output_weight_rejected():
    data = _input(10.0, 20.0)
    with pytest.raises(SignalError) as exc:
        validate_signal_output({_SYMBOL: 0.5}, data)
    assert exc.value.code == "non_decimal_weight"


def test_non_finite_decimal_weight_rejected():
    data = _input(10.0, 20.0)
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(SignalError) as exc:
            validate_signal_output({_SYMBOL: bad}, data)
        assert exc.value.code == "non_finite_weight"


def test_negative_weight_rejected():
    data = _input(10.0, 20.0)
    with pytest.raises(SignalError) as exc:
        validate_signal_output({_SYMBOL: Decimal("-0.01")}, data)
    assert exc.value.code == "negative_weight"


def test_weight_above_one_rejected():
    data = _input(10.0, 20.0)
    with pytest.raises(SignalError) as exc:
        validate_signal_output({_SYMBOL: Decimal("1.01")}, data)
    assert exc.value.code == "weight_above_one"


def test_explicit_weight_sum_above_one_rejected():
    data = _input(10.0, 20.0, symbols=("600519", "000001"))
    with pytest.raises(SignalError) as exc:
        validate_signal_output(
            {"600519": Decimal("0.6"), "000001": Decimal("0.5")},
            data,
        )
    assert exc.value.code == "total_weight_above_one"


def test_output_symbol_absent_from_input_rejected():
    data = _input(10.0, 20.0)
    with pytest.raises(SignalError) as exc:
        validate_signal_output({"999999": Decimal("0.5")}, data)
    assert exc.value.code == "unknown_symbol"


def test_repeated_identical_input_produces_identical_output():
    data = _input(10.0, 20.0, 30.0, 40.0)
    signal = SmaSignal(period=3)
    first = signal.compute(data.as_of, data)
    second = signal.compute(data.as_of, data)
    assert first == second
    rebuilt = _input(10.0, 20.0, 30.0, 40.0)
    assert signal.compute(rebuilt.as_of, rebuilt) == first


def test_input_does_not_alias_mutable_caller_containers():
    raw = [
        SignalObservation(_FIRST, 10.0),
        SignalObservation(date(2026, 1, 6), 20.0),
    ]
    data = SignalInput(as_of=date(2026, 1, 6), per_symbol={_SYMBOL: raw})
    raw.append(SignalObservation(date(2026, 1, 7), 99.0))
    raw[0] = SignalObservation(_FIRST, 999.0)
    assert data.observations(_SYMBOL) == (
        SignalObservation(_FIRST, 10.0),
        SignalObservation(date(2026, 1, 6), 20.0),
    )
    assert data.symbols == (_SYMBOL,)


def test_compute_as_of_later_than_input_horizon_rejected():
    data = _input(10.0, 20.0)
    with pytest.raises(SignalError) as exc:
        SmaSignal(period=2).compute(date(2026, 1, 9), data)
    assert exc.value.code == "as_of_beyond_input_horizon"


# ---------------------------------------------------------------------------
# B. SMA signal semantics
# ---------------------------------------------------------------------------


def test_sma_insufficient_history_is_no_decision():
    data = _input(10.0, 20.0)
    assert SmaSignal(period=3).compute(data.as_of, data) == {}


def test_sma_close_above_sma_is_active():
    data = _input(10.0, 20.0)
    assert SmaSignal(period=2).compute(data.as_of, data) == {_SYMBOL: Decimal("0.95")}


def test_sma_close_below_sma_is_flat():
    data = _input(20.0, 10.0)
    assert SmaSignal(period=2).compute(data.as_of, data) == {_SYMBOL: Decimal("0")}


def test_sma_close_equal_sma_is_no_decision():
    data = _input(10.0, 10.0)
    assert SmaSignal(period=2).compute(data.as_of, data) == {}


def test_sma_current_bar_is_included_in_window():
    # With only two bars, excluding the current bar would leave insufficient
    # history and produce NO_DECISION; inclusion yields ACTIVE.
    data = _input(10.0, 20.0)
    assert SmaSignal(period=2).compute(data.as_of, data) == {_SYMBOL: Decimal("0.95")}


def test_sma_newest_to_oldest_summation_order_preserved():
    # Window newest-first [0.6, 0.1, 1.1]: built-in sum is exactly 1.8 so
    # sma == close == 0.6 -> NO_DECISION. Summing oldest-first yields
    # 1.8000000000000003 and would emit a decision instead.
    data = _input(1.1, 0.1, 0.6)
    assert SmaSignal(period=3).compute(data.as_of, data) == {}


def test_sma_rejects_math_fsum_substitution():
    # Window newest-first [1.1, 0.9, 1.3]: built-in sum is exactly 3.3 so
    # sma 1.0999999999999999 < close 1.1 -> ACTIVE. math.fsum would give
    # sma 1.1 == close -> NO_DECISION.
    data = _input(1.3, 0.9, 1.1)
    assert SmaSignal(period=3).compute(data.as_of, data) == {_SYMBOL: Decimal("0.95")}


def test_sma_rejects_decimal_sma_arithmetic():
    # Window newest-first [0.6, 1.1, 0.1]: float sum 1.8000000000000003 gives
    # sma 0.6000000000000001 > close 0.6 -> FLAT. Decimal arithmetic would
    # give sma == close -> NO_DECISION.
    data = _input(0.1, 1.1, 0.6)
    assert SmaSignal(period=3).compute(data.as_of, data) == {_SYMBOL: Decimal("0")}


def test_sma_near_boundary_float_case():
    data_above = _input(10.0, 10.0000001)
    assert SmaSignal(period=2).compute(data_above.as_of, data_above) == {
        _SYMBOL: Decimal("0.95")
    }
    data_below = _input(10.0, 9.9999999)
    assert SmaSignal(period=2).compute(data_below.as_of, data_below) == {
        _SYMBOL: Decimal("0")
    }


def test_sma_uses_indicator_close_only():
    # The input carries the derived causal indicator price; classification
    # follows it exactly (a constant indicator series is equality -> NO_DECISION).
    data = _input(5.0, 5.0)
    assert SmaSignal(period=2).compute(data.as_of, data) == {}


def test_sma_invalid_period_rejected():
    for bad in (0, -1, 1.5, True, None):
        with pytest.raises(SignalError) as exc:
            SmaSignal(period=bad)
        assert exc.value.code == "invalid_period"


def test_sma_active_weight_must_satisfy_decimal_weight_contract():
    with pytest.raises(SignalError) as exc:
        SmaSignal(period=2, active_weight=0.95)
    assert exc.value.code == "non_decimal_weight"
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-0.01"), Decimal("1.01")):
        with pytest.raises(SignalError) as exc:
            SmaSignal(period=2, active_weight=bad)
        assert exc.value.code in {"non_finite_weight", "negative_weight", "weight_above_one"}


def test_sma_active_weight_boundaries_accepted():
    data = _input(10.0, 20.0)
    assert SmaSignal(period=2, active_weight=Decimal("1")).compute(data.as_of, data) == {
        _SYMBOL: Decimal("1")
    }
    assert SmaSignal(period=2, active_weight=Decimal("0")).compute(data.as_of, data) == {
        _SYMBOL: Decimal("0")
    }


def test_sma_multi_symbol_omission_is_not_silent_zero():
    per_symbol = {
        "600519": _observations(10.0, 20.0),  # ACTIVE
        "000001": _observations(20.0, 10.0),  # FLAT -> explicit zero
        "000858": _observations(5.0),  # insufficient history -> OMIT
    }
    data = SignalInput(as_of=date(2026, 1, 7), per_symbol=per_symbol)
    result = SmaSignal(period=2).compute(data.as_of, data)
    assert result == {"600519": Decimal("0.95"), "000001": Decimal("0")}
    assert "000858" not in result
    assert set(result) <= set(data.symbols)


# ---------------------------------------------------------------------------
# C. Canonical baseline signal-state equivalence (public v01 fixture)
# ---------------------------------------------------------------------------


def test_sma_signal_state_equivalence_with_public_fixture_baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("public-v01")
    inputs = build_public_v01_inputs(root)
    records = ManifestWriter(root / "inputs" / "data" / "manifests" / "manifest.jsonl").read_all()
    action_records = {
        record.symbol: record
        for record in read_corporate_action_manifest(root / "inputs")
    }
    calendar_record = next(
        record
        for record in CalendarSnapshotStore(root / "inputs").read_manifest()
        if record.calendar_id == inputs.calendar_id
    )
    calendar = load_verified_calendar(root / "inputs", calendar_record)
    universe = load_verified_universe(
        root / "inputs" / "configs" / "universes" / f"{inputs.universe_id}.json",
        expected_id=inputs.universe_id,
    )
    fee_policy = default_fee_policy()

    compared = 0
    for period in (10, 20, 60):
        for symbol in inputs.symbols:
            market_data = load_verified_snapshot(
                root / "inputs",
                next(record for record in records if record.symbol == symbol),
            )
            corporate_actions = load_verified_corporate_actions(
                root / "inputs",
                action_records[symbol],
            )
            result = run_backtest(
                market_data,
                universe=universe,
                corporate_actions=corporate_actions,
                calendar=calendar,
                fee_policy=fee_policy,
                config=BacktestConfig(
                    strategy=StrategyName.SMA,
                    initial_cash=1_000_000.0,
                    target_weight=Decimal("0.95"),
                    sma_period=period,
                ),
            )
            feed = derive_price_streams(market_data.frame, corporate_actions)
            sessions = [value.date() for value in feed["date"]]
            observations = tuple(
                SignalObservation(session=session, indicator_close=float(value))
                for session, value in zip(sessions, feed["indicator_close"], strict=True)
            )
            data = SignalInput(as_of=sessions[-1], per_symbol={symbol: observations})
            signal = SmaSignal(period=period)
            orders = {order.signal_date: order.side for order in result.orders}
            positions = {position.date: position.size for position in result.positions}

            for session in sessions:
                output = signal.compute(session, data)
                state = (
                    "NO_DECISION"
                    if symbol not in output
                    else ("ACTIVE" if output[symbol] > 0 else "FLAT")
                )
                side = orders.get(session)
                size = positions[session]
                if side == "buy":
                    assert state == "ACTIVE", (period, symbol, session, state, side)
                elif side == "sell":
                    assert state == "FLAT", (period, symbol, session, state, side)
                else:
                    if size > 0:
                        assert state != "FLAT", (period, symbol, session, state, size)
                    else:
                        assert state != "ACTIVE", (period, symbol, session, state, size)
                # Reverse: an emitted classification must have been acted on.
                if state == "ACTIVE":
                    assert size > 0 or side == "buy", (
                        period,
                        symbol,
                        session,
                        state,
                        size,
                        side,
                    )
                if state == "FLAT":
                    assert size == 0 or side == "sell", (
                        period,
                        symbol,
                        session,
                        state,
                        size,
                        side,
                    )
                compared += 1
    # The full public fixture covers 10 symbols x 3 periods x ~2071 bars.
    assert compared >= 60_000


# ---------------------------------------------------------------------------
# D. Top-K momentum contract demonstration
# ---------------------------------------------------------------------------


def _top_k_input() -> SignalInput:
    per_symbol = {
        "600519": _observations(10.0, 20.0, 30.0),  # return 2.0
        "000001": _observations(10.0, 15.0, 20.0),  # return 1.0
        "000858": _observations(10.0, 12.0, 15.0),  # return 0.5
    }
    return SignalInput(as_of=date(2026, 1, 8), per_symbol=per_symbol)


def test_top_k_requires_n_plus_one_observations():
    data = _input(10.0, 20.0)  # two observations cannot support lookback=2
    assert TopKMomentumSignal(lookback=2, k=1).compute(data.as_of, data) == {}


def test_top_k_ranking_descending_return():
    data = _top_k_input()
    result = TopKMomentumSignal(lookback=2, k=2).compute(data.as_of, data)
    assert result == {
        "600519": Decimal("0.5"),
        "000001": Decimal("0.5"),
        "000858": Decimal("0"),
    }


def test_top_k_deterministic_symbol_tiebreak():
    per_symbol = {
        "000001": _observations(10.0, 20.0, 30.0),  # return 2.0 (tie)
        "000858": _observations(10.0, 20.0, 30.0),  # return 2.0 (tie)
        "600519": _observations(10.0, 20.0, 30.0),  # return 2.0 (tie)
    }
    data = SignalInput(as_of=date(2026, 1, 8), per_symbol=per_symbol)
    result = TopKMomentumSignal(lookback=2, k=2).compute(data.as_of, data)
    assert result == {
        "000001": Decimal("0.5"),
        "000858": Decimal("0.5"),
        "600519": Decimal("0"),
    }


def test_top_k_equal_weighting_rounds_down_below_one():
    per_symbol = {
        "600519": _observations(10.0, 20.0, 30.0),
        "000001": _observations(10.0, 15.0, 20.0),
        "000858": _observations(10.0, 12.0, 15.0),
    }
    data = SignalInput(as_of=date(2026, 1, 8), per_symbol=per_symbol)
    result = TopKMomentumSignal(lookback=2, k=3).compute(data.as_of, data)
    weights = tuple(result[symbol] for symbol in ("600519", "000001", "000858"))
    assert all(weight == weights[0] for weight in weights)
    with decimal.localcontext(decimal.Context(prec=60)):
        assert Decimal(3) * weights[0] < Decimal("1")
    total = Decimal("0")
    for weight in result.values():
        total += weight
    assert total <= Decimal("1")


def test_top_k_insufficient_history_symbol_omitted():
    per_symbol = {
        "600519": _observations(10.0, 20.0, 30.0),
        "000001": _observations(10.0, 20.0),  # too short for lookback=2
    }
    data = SignalInput(as_of=date(2026, 1, 8), per_symbol=per_symbol)
    result = TopKMomentumSignal(lookback=2, k=1).compute(data.as_of, data)
    assert "000001" not in result
    assert result == {"600519": Decimal("1")}


def test_top_k_fewer_eligible_than_k_selects_all():
    data = _top_k_input()
    result = TopKMomentumSignal(lookback=2, k=5).compute(data.as_of, data)
    # Fewer than k eligible: all eligible symbols are selected with equal weight.
    with decimal.localcontext(decimal.Context(prec=50, rounding=decimal.ROUND_DOWN)):
        expected = Decimal("1") / Decimal(3)
    assert result == {
        "600519": expected,
        "000001": expected,
        "000858": expected,
    }


def test_top_k_no_eligible_symbols_returns_no_decisions():
    data = _input(10.0, 20.0)
    assert TopKMomentumSignal(lookback=2, k=3).compute(data.as_of, data) == {}


def test_top_k_invalid_parameters_rejected():
    with pytest.raises(SignalError) as exc:
        TopKMomentumSignal(lookback=0, k=1)
    assert exc.value.code == "invalid_lookback"
    with pytest.raises(SignalError) as exc:
        TopKMomentumSignal(lookback=1, k=0)
    assert exc.value.code == "invalid_k"
    for bad in (-2, 1.5, True, None):
        with pytest.raises(SignalError):
            TopKMomentumSignal(lookback=bad, k=1)


def test_top_k_deterministic_under_changed_caller_decimal_context():
    data = _top_k_input()
    signal = TopKMomentumSignal(lookback=2, k=3)
    baseline = signal.compute(data.as_of, data)
    repeated = signal.compute(data.as_of, data)
    assert repeated == baseline
    with decimal.localcontext(decimal.Context(prec=5, rounding=decimal.ROUND_CEILING)):
        under_foreign_context = TopKMomentumSignal(lookback=2, k=3).compute(
            data.as_of,
            data,
        )
    assert under_foreign_context == baseline


# ---------------------------------------------------------------------------
# E. Interface and registry
# ---------------------------------------------------------------------------


def test_signal_classes_satisfy_explicit_protocol():
    assert isinstance(SmaSignal(period=2), Signal)
    assert isinstance(TopKMomentumSignal(lookback=1, k=1), Signal)


def test_explicit_registry_maps_names_to_constructors():
    assert set(SIGNAL_REGISTRY) == {"sma", "top_k_momentum"}
    assert SIGNAL_REGISTRY["sma"] is SmaSignal
    assert SIGNAL_REGISTRY["top_k_momentum"] is TopKMomentumSignal
