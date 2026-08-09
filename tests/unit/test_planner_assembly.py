"""Planner-local signal assembly contract tests."""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

import pytest

import aquant.planner.assembly as assembly
from aquant.planner import (
    SIGNAL_SPECS,
    PlannerError,
    SignalCardinality,
    SignalSpec,
    build_signal,
)
from aquant.research.signals import SIGNAL_REGISTRY, SmaSignal, TopKMomentumSignal

_ONE_SYMBOL = frozenset({"600519"})
_TWO_SYMBOLS = frozenset({"600519", "000001"})


def assert_code(exc_info: pytest.ExceptionInfo[PlannerError], code: str) -> None:
    assert exc_info.value.code == code
    assert str(exc_info.value) == code


def test_signal_specs_are_the_single_registry_aligned_truth_source() -> None:
    assert type(SIGNAL_SPECS) is MappingProxyType
    assert set(SIGNAL_SPECS) == set(SIGNAL_REGISTRY)
    assert all(key == spec.name for key, spec in SIGNAL_SPECS.items())
    with pytest.raises(TypeError):
        SIGNAL_SPECS["unexpected"] = SIGNAL_SPECS["sma"]  # type: ignore[index]

    assert type(
        build_signal(
            name="sma",
            config={"period": 20},
            eligible_symbols=_ONE_SYMBOL,
        )
    ) is SIGNAL_REGISTRY["sma"]
    assert type(
        build_signal(
            name="top_k_momentum",
            config={"lookback": 20, "k": 3},
            eligible_symbols=_TWO_SYMBOLS,
        )
    ) is SIGNAL_REGISTRY["top_k_momentum"]


def test_sma_builder_preserves_default_and_explicit_active_weight() -> None:
    default = build_signal(
        name="sma",
        config={"period": 20},
        eligible_symbols=_ONE_SYMBOL,
    )
    custom = build_signal(
        name="sma",
        config={"period": 10, "active_weight": Decimal("0.8")},
        eligible_symbols=_ONE_SYMBOL,
    )

    assert type(default) is SmaSignal
    assert default.period == 20
    assert default.active_weight == Decimal("0.95")
    assert type(custom) is SmaSignal
    assert custom.period == 10
    assert custom.active_weight == Decimal("0.8")


def test_top_k_builder_preserves_parameters() -> None:
    signal = build_signal(
        name="top_k_momentum",
        config={"lookback": 15, "k": 2},
        eligible_symbols=_ONE_SYMBOL,
    )

    assert type(signal) is TopKMomentumSignal
    assert signal.lookback == 15
    assert signal.k == 2


@pytest.mark.parametrize("name", ["unknown", "", 1, []])
def test_unknown_signal_name_fails_closed(name: object) -> None:
    with pytest.raises(PlannerError) as exc:
        build_signal(name=name, config={}, eligible_symbols=_ONE_SYMBOL)  # type: ignore[arg-type]
    assert_code(exc, "unknown_signal_spec")


@pytest.mark.parametrize(
    "config",
    [
        [],
        {},
        {"active_weight": Decimal("0.8")},
        {"period": 20, "unexpected": 1},
        {"period": 20, "active_weight": Decimal("0.8"), "unexpected": 1},
        {"period": 0},
        {"period": -1},
        {"period": 1.5},
        {"period": True},
        {"period": "20"},
        {"period": 20, "active_weight": 0.8},
        {"period": 20, "active_weight": Decimal("0")},
        {"period": 20, "active_weight": Decimal("-0.1")},
        {"period": 20, "active_weight": Decimal("1.1")},
        {"period": 20, "active_weight": Decimal("NaN")},
    ],
)
def test_sma_invalid_config_fails_closed_without_raw_config(config: object) -> None:
    with pytest.raises(PlannerError) as exc:
        build_signal(name="sma", config=config, eligible_symbols=_ONE_SYMBOL)  # type: ignore[arg-type]
    assert_code(exc, "invalid_signal_config")
    assert repr(config) not in str(exc.value)


@pytest.mark.parametrize(
    "config",
    [
        [],
        {},
        {"lookback": 20},
        {"k": 3},
        {"lookback": 20, "k": 3, "unexpected": 1},
        {"lookback": 0, "k": 3},
        {"lookback": -1, "k": 3},
        {"lookback": 1.5, "k": 3},
        {"lookback": True, "k": 3},
        {"lookback": "20", "k": 3},
        {"lookback": 20, "k": 0},
        {"lookback": 20, "k": -1},
        {"lookback": 20, "k": 1.5},
        {"lookback": 20, "k": True},
        {"lookback": 20, "k": "3"},
    ],
)
def test_top_k_invalid_config_fails_closed_without_raw_config(config: object) -> None:
    with pytest.raises(PlannerError) as exc:
        build_signal(name="top_k_momentum", config=config, eligible_symbols=_ONE_SYMBOL)  # type: ignore[arg-type]
    assert_code(exc, "invalid_signal_config")
    assert repr(config) not in str(exc.value)


@pytest.mark.parametrize(
    "eligible_symbols",
    [
        set({"600519"}),
        frozenset(),
        frozenset({""}),
        frozenset({1}),
    ],
)
def test_eligible_symbols_require_an_exact_nonempty_frozenset_of_strings(
    eligible_symbols: object,
) -> None:
    with pytest.raises(PlannerError) as exc:
        build_signal(
            name="sma",
            config={"period": 20},
            eligible_symbols=eligible_symbols,  # type: ignore[arg-type]
        )
    assert_code(exc, "invalid_eligible_symbols")


def test_single_symbol_spec_rejects_multiple_eligible_symbols_before_construction() -> None:
    with pytest.raises(PlannerError) as exc:
        build_signal(
            name="sma",
            config={"period": 20},
            eligible_symbols=_TWO_SYMBOLS,
        )
    assert_code(exc, "unsupported_cardinality")


def test_multi_symbol_spec_accepts_one_or_more_eligible_symbols() -> None:
    assert type(
        build_signal(
            name="top_k_momentum",
            config={"lookback": 20, "k": 3},
            eligible_symbols=_ONE_SYMBOL,
        )
    ) is TopKMomentumSignal
    assert type(
        build_signal(
            name="top_k_momentum",
            config={"lookback": 20, "k": 3},
            eligible_symbols=_TWO_SYMBOLS,
        )
    ) is TopKMomentumSignal


def test_registry_spec_parity_fails_closed_before_builder_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assembly, "SIGNAL_SPECS", MappingProxyType({}))

    with pytest.raises(PlannerError) as exc:
        assembly.build_signal(
            name="sma",
            config={"period": 20},
            eligible_symbols=_ONE_SYMBOL,
        )
    assert_code(exc, "signal_spec_registry_mismatch")


def test_wrong_builder_runtime_type_is_a_planner_invariant_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build_wrong_signal(_config: object) -> object:
        return object()

    monkeypatch.setattr(
        assembly,
        "SIGNAL_SPECS",
        MappingProxyType(
            {
                "sma": SignalSpec(
                    name="sma",
                    builder=build_wrong_signal,  # type: ignore[arg-type]
                    cardinality=SignalCardinality.SINGLE_SYMBOL,
                ),
                "top_k_momentum": assembly.SIGNAL_SPECS["top_k_momentum"],
            }
        ),
    )

    with pytest.raises(PlannerError) as exc:
        assembly.build_signal(
            name="sma",
            config={"period": 20},
            eligible_symbols=_ONE_SYMBOL,
        )
    assert_code(exc, "planner_invariant_violation")
