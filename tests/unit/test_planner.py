from datetime import date, datetime
from decimal import Decimal, getcontext

import pytest

from aquant.planner import (
    PLANNER_SCHEMA_VERSION,
    NoPreviousState,
    NoPreviousStateReason,
    PlannedTargets,
    PlannerError,
    PlannerLimits,
    PreviousTargets,
    plan_targets,
)

AS_OF = date(2026, 8, 7)
ELIGIBLE = frozenset({"000001", "000002", "510300"})
FIRST_PERIOD = NoPreviousState(NoPreviousStateReason.FIRST_PERIOD)


def assert_code(exc_info: pytest.ExceptionInfo[PlannerError], code: str) -> None:
    assert exc_info.value.code == code
    assert str(exc_info.value) == code


def previous(
    targets: dict[str, Decimal] | None = None,
    *,
    as_of: date = date(2026, 8, 6),
) -> PreviousTargets:
    return PreviousTargets(as_of=as_of, targets=targets or {})


def plan(
    signal_output: object,
    *,
    prior: PreviousTargets | NoPreviousState = FIRST_PERIOD,
    eligible_symbols: object = ELIGIBLE,
    limits: object = PlannerLimits(),
    as_of: object = AS_OF,
) -> PlannedTargets:
    return plan_targets(
        as_of=as_of,
        signal_output=signal_output,
        previous=prior,
        eligible_symbols=eligible_symbols,
        limits=limits,
    )


def test_public_api_and_schema_version() -> None:
    assert PLANNER_SCHEMA_VERSION == "1.0.0"
    assert issubclass(PlannerError, ValueError)
    assert NoPreviousStateReason.FIRST_PERIOD == "first_period"
    assert NoPreviousStateReason.EXPLICIT_RESET == "explicit_reset"


def test_previous_argument_is_required_and_none_is_rejected() -> None:
    with pytest.raises(TypeError):
        plan_targets(
            as_of=AS_OF,
            signal_output={},
            eligible_symbols=ELIGIBLE,
            limits=PlannerLimits(),
        )
    with pytest.raises(PlannerError) as exc:
        plan({}, prior=None)  # type: ignore[arg-type]
    assert_code(exc, "invalid_previous_state")


@pytest.mark.parametrize(
    "reason", [NoPreviousStateReason.FIRST_PERIOD, NoPreviousStateReason.EXPLICIT_RESET]
)
def test_no_previous_state_reasons_produce_empty_state(reason: NoPreviousStateReason) -> None:
    output = plan({}, prior=NoPreviousState(reason))
    assert output.as_of == AS_OF
    assert dict(output.targets) == {}


def test_no_previous_state_requires_exact_reason_enum() -> None:
    with pytest.raises(PlannerError) as exc:
        NoPreviousState("first_period")  # type: ignore[arg-type]
    assert_code(exc, "invalid_previous_state")


@pytest.mark.parametrize("bad_as_of", [datetime(2026, 8, 7), "2026-08-07", 1])
def test_as_of_requires_exact_date_for_state_and_planner(bad_as_of: object) -> None:
    with pytest.raises(PlannerError) as exc:
        PreviousTargets(as_of=bad_as_of, targets={})  # type: ignore[arg-type]
    assert_code(exc, "invalid_as_of")
    with pytest.raises(PlannerError) as exc:
        plan({}, as_of=bad_as_of)
    assert_code(exc, "invalid_as_of")


@pytest.mark.parametrize("state_type", [PreviousTargets, PlannedTargets])
def test_target_states_defensively_copy_sort_and_are_read_only(state_type: type[object]) -> None:
    source = {"510300": Decimal("0"), "000002": Decimal("0.2")}
    state = state_type(as_of=AS_OF, targets=source)  # type: ignore[call-arg]
    source["000002"] = Decimal("0.9")
    source["000001"] = Decimal("0.1")
    del source["510300"]
    source.clear()
    assert tuple(state.targets.items()) == (("000002", Decimal("0.2")), ("510300", Decimal("0")))  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        state.targets["000001"] = Decimal("0.1")  # type: ignore[attr-defined,index]
    with pytest.raises(TypeError):
        del state.targets["000002"]  # type: ignore[attr-defined]


@pytest.mark.parametrize("state_type", [PreviousTargets, PlannedTargets])
def test_target_states_do_not_expose_a_mutable_backing_dict(
    state_type: type[object],
) -> None:
    state = state_type(  # type: ignore[call-arg]
        as_of=AS_OF,
        targets={"000001": Decimal("0.2")},
    )
    assert not hasattr(state, "_targets")
    assert dict(state.targets) == {"000001": Decimal("0.2")}  # type: ignore[attr-defined]


def test_target_states_equal_across_insertion_order_and_preserve_decimal_value() -> None:
    left = PreviousTargets(
        as_of=AS_OF,
        targets={"510300": Decimal("0E-12"), "000001": Decimal("0.10")},
    )
    right = PreviousTargets(
        as_of=AS_OF,
        targets={"000001": Decimal("0.10"), "510300": Decimal("0E-12")},
    )
    assert left == right
    assert tuple(left.targets.items()) == (
        ("000001", Decimal("0.10")),
        ("510300", Decimal("0E-12")),
    )
    assert left.targets["510300"].as_tuple() == Decimal("0E-12").as_tuple()


@pytest.mark.parametrize(
    ("targets", "code"),
    [
        ([], "invalid_output_type"),
        ({"": Decimal("0")}, "invalid_symbol"),
        ({1: Decimal("0")}, "invalid_symbol"),
        ({"000001": 0}, "non_decimal_weight"),
        ({"000001": 0.1}, "non_decimal_weight"),
        ({"000001": True}, "non_decimal_weight"),
        ({"000001": Decimal("NaN")}, "non_finite_weight"),
        ({"000001": Decimal("Infinity")}, "non_finite_weight"),
        ({"000001": Decimal("-0.1")}, "negative_weight"),
        ({"000001": Decimal("1.1")}, "weight_above_one"),
    ],
)
def test_target_states_validate_mapping_symbols_and_weights(targets: object, code: str) -> None:
    with pytest.raises(PlannerError) as exc:
        PreviousTargets(as_of=AS_OF, targets=targets)  # type: ignore[arg-type]
    assert_code(exc, code)


def test_signal_output_must_be_mapping_and_is_independently_validated() -> None:
    with pytest.raises(PlannerError) as exc:
        plan([])
    assert_code(exc, "invalid_output_type")
    with pytest.raises(PlannerError) as exc:
        plan({"000001": 1})
    assert_code(exc, "non_decimal_weight")


@pytest.mark.parametrize(
    ("signal_output", "code"),
    [
        ({"": Decimal("0")}, "invalid_symbol"),
        ({"000001": Decimal("NaN")}, "non_finite_weight"),
        ({"000001": Decimal("-0.1")}, "negative_weight"),
        ({"000001": Decimal("1.1")}, "weight_above_one"),
    ],
)
def test_signal_output_has_the_same_symbol_and_weight_validation(
    signal_output: object, code: str
) -> None:
    with pytest.raises(PlannerError) as exc:
        plan(signal_output)
    assert_code(exc, code)


def test_merge_overrides_preserves_zeros_and_carries_omitted_values() -> None:
    prior = previous({"000001": Decimal("0.4"), "000002": Decimal("0")})
    output = plan({"000001": Decimal("0.5"), "510300": Decimal("0")}, prior=prior)
    assert tuple(output.targets.items()) == (
        ("000001", Decimal("0.5")),
        ("000002", Decimal("0")),
        ("510300", Decimal("0")),
    )


def test_current_zero_overrides_prior_positive_and_retains_the_key() -> None:
    output = plan({"000001": Decimal("0")}, prior=previous({"000001": Decimal("0.4")}))
    assert tuple(output.targets.items()) == (("000001", Decimal("0")),)


def test_multi_period_output_preserves_keys_and_does_not_invent_keys() -> None:
    first = plan({"000001": Decimal("0.3")})
    second = plan(
        {"510300": Decimal("0.2")},
        prior=PreviousTargets(as_of=first.as_of, targets=first.targets),
        as_of=date(2026, 8, 8),
    )
    assert dict(second.targets) == {"000001": Decimal("0.3"), "510300": Decimal("0.2")}
    assert "000002" not in second.targets


@pytest.mark.parametrize("prior_date", [AS_OF, date(2026, 8, 8)])
def test_previous_state_must_strictly_precede_current_as_of(prior_date: date) -> None:
    with pytest.raises(PlannerError) as exc:
        plan({}, prior=previous(as_of=prior_date))
    assert_code(exc, "non_ascending_previous_state")


@pytest.mark.parametrize(
    "eligible",
    [set({"000001"}), frozenset(), frozenset({""}), frozenset({1})],
)
def test_eligible_symbols_must_be_nonempty_exact_frozenset(eligible: object) -> None:
    with pytest.raises(PlannerError) as exc:
        plan({}, eligible_symbols=eligible)
    assert_code(exc, "invalid_eligible_symbols")


def test_current_and_previous_keys_must_be_eligible_including_zero_previous() -> None:
    with pytest.raises(PlannerError) as exc:
        plan({"600000": Decimal("0")})
    assert_code(exc, "universe_mismatch")
    with pytest.raises(PlannerError) as exc:
        plan({}, prior=previous({"600000": Decimal("0")}))
    assert_code(exc, "universe_mismatch")


def test_complete_effective_state_enforces_hard_gross_ceiling() -> None:
    with pytest.raises(PlannerError) as exc:
        plan({"000002": Decimal("0.6")}, prior=previous({"000001": Decimal("0.6")}))
    assert_code(exc, "hard_gross_ceiling_exceeded")


def test_default_limits_and_invalid_limit_fields() -> None:
    assert PlannerLimits() == PlannerLimits(
        max_single_weight=Decimal("1"), max_gross=Decimal("1"), min_cash_ratio=Decimal("0")
    )
    for kwargs in (
        {"max_single_weight": 1},
        {"max_gross": Decimal("0")},
        {"min_cash_ratio": Decimal("1")},
        {"max_gross": Decimal("NaN")},
    ):
        with pytest.raises(PlannerError) as exc:
            PlannerLimits(**kwargs)  # type: ignore[arg-type]
        assert_code(exc, "invalid_limits")
    with pytest.raises(PlannerError) as exc:
        plan({}, limits=object())
    assert_code(exc, "invalid_limits")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_single_weight": 1},
        {"max_single_weight": Decimal("NaN")},
        {"max_single_weight": Decimal("0")},
        {"max_single_weight": Decimal("1.1")},
        {"max_gross": 1},
        {"max_gross": Decimal("Infinity")},
        {"max_gross": Decimal("0")},
        {"max_gross": Decimal("1.1")},
        {"min_cash_ratio": 0},
        {"min_cash_ratio": Decimal("NaN")},
        {"min_cash_ratio": Decimal("-0.1")},
        {"min_cash_ratio": Decimal("1")},
    ],
)
def test_every_planner_limit_field_requires_finite_decimal_in_range(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(PlannerError) as exc:
        PlannerLimits(**kwargs)  # type: ignore[arg-type]
    assert_code(exc, "invalid_limits")


def test_configured_limit_violations_and_order() -> None:
    with pytest.raises(PlannerError) as exc:
        plan({"000001": Decimal("0.6")}, limits=PlannerLimits(max_single_weight=Decimal("0.5")))
    assert_code(exc, "max_single_weight_exceeded")
    with pytest.raises(PlannerError) as exc:
        plan(
            {"000001": Decimal("0.5"), "000002": Decimal("0.4")},
            limits=PlannerLimits(max_gross=Decimal("0.8")),
        )
    assert_code(exc, "max_gross_exceeded")
    with pytest.raises(PlannerError) as exc:
        plan(
            {"000001": Decimal("0.8")},
            limits=PlannerLimits(min_cash_ratio=Decimal("0.3")),
        )
    assert_code(exc, "min_cash_ratio_violated")
    with pytest.raises(PlannerError) as exc:
        plan(
            {"000001": Decimal("0.7"), "000002": Decimal("0.3")},
            limits=PlannerLimits(max_single_weight=Decimal("0.6"), max_gross=Decimal("0.5")),
        )
    assert_code(exc, "max_single_weight_exceeded")


def test_fixed_decimal_context_makes_results_independent_of_caller_context() -> None:
    original_prec = getcontext().prec
    try:
        getcontext().prec = 6
        low_precision = plan({"000001": Decimal("0.12345678901234567890")})
        getcontext().prec = 50
        high_precision = plan({"000001": Decimal("0.12345678901234567890")})
    finally:
        getcontext().prec = original_prec
    assert low_precision == high_precision
    assert low_precision.targets["000001"] == Decimal("0.12345678901234567890")


@pytest.mark.parametrize(
    ("limits", "expected_code"),
    [
        (
            PlannerLimits(max_gross=Decimal("0.999999999999999999999999999998")),
            "max_gross_exceeded",
        ),
        (
            PlannerLimits(
                max_gross=Decimal("0.999999999999999999999999999999"),
                min_cash_ratio=Decimal("2E-30"),
            ),
            "min_cash_ratio_violated",
        ),
    ],
)
def test_fixed_decimal_context_controls_near_boundary_gross_validation(
    limits: PlannerLimits, expected_code: str
) -> None:
    weights = {
        "000001": Decimal("0.333333333333333333333333333333"),
        "000002": Decimal("0.333333333333333333333333333333"),
        "510300": Decimal("0.333333333333333333333333333333"),
    }
    original_prec = getcontext().prec
    outcomes: list[tuple[str, str | PlannedTargets]] = []
    try:
        for precision in (6, 50):
            getcontext().prec = precision
            try:
                outcomes.append(
                    (
                        "accepted",
                        plan(
                            weights,
                            limits=limits,
                        ),
                    )
                )
            except PlannerError as error:
                outcomes.append(("rejected", error.code))
    finally:
        getcontext().prec = original_prec
    assert outcomes == [("rejected", expected_code), ("rejected", expected_code)]


@pytest.mark.parametrize(
    ("signal_output", "limits", "expected_code"),
    [
        (
            {"000001": Decimal("1"), "000002": Decimal("1E-60")},
            PlannerLimits(),
            "hard_gross_ceiling_exceeded",
        ),
        (
            {"000001": Decimal("1")},
            PlannerLimits(min_cash_ratio=Decimal("1E-61")),
            "min_cash_ratio_violated",
        ),
    ],
)
def test_tiny_decimal_excesses_are_rejected_independently_of_caller_context(
    signal_output: dict[str, Decimal], limits: PlannerLimits, expected_code: str
) -> None:
    original_prec = getcontext().prec
    outcomes: list[str] = []
    try:
        for precision in (6, 80):
            getcontext().prec = precision
            with pytest.raises(PlannerError) as exc:
                plan(signal_output, limits=limits)
            outcomes.append(exc.value.code)
    finally:
        getcontext().prec = original_prec
    assert outcomes == [expected_code, expected_code]


def test_exact_sum_handles_exponents_beyond_the_default_context_range() -> None:
    original_prec = getcontext().prec
    outcomes: list[str] = []
    try:
        for precision in (6, 80):
            getcontext().prec = precision
            with pytest.raises(PlannerError) as exc:
                plan(
                    {"000001": Decimal("1E-1000100")},
                    limits=PlannerLimits(max_gross=Decimal("1E-1000101")),
                )
            outcomes.append(exc.value.code)
    finally:
        getcontext().prec = original_prec
    assert outcomes == ["max_gross_exceeded", "max_gross_exceeded"]
