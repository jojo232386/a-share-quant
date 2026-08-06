from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from aquant.data.calendar_snapshot import (
    CalendarSnapshotStore,
    load_verified_calendar,
)
from aquant.portfolio import PortfolioError
from aquant.portfolio.availability import (
    AvailabilityStatus,
    check_bar_availability,
)
from aquant.rules import RejectionReason


@pytest.fixture
def calendar(tmp_path):
    record = CalendarSnapshotStore(tmp_path).write(
        (
            date(2026, 7, 16),
            date(2026, 7, 17),
            date(2026, 7, 20),
            date(2026, 7, 21),
        ),
        source_provider="synthetic",
        source_function="test_calendar",
        source_version="1",
        fetched_at_utc=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
    )
    return load_verified_calendar(tmp_path, record)


def test_exact_next_official_session_with_bar_is_available(calendar):
    decision = check_bar_availability(
        intent_session=date(2026, 7, 17),
        execution_session=date(2026, 7, 20),
        calendar=calendar,
        available_bar_dates=frozenset({date(2026, 7, 20)}),
    )

    assert decision.status is AvailabilityStatus.AVAILABLE
    assert decision.source_rule_reason is None


def test_exact_next_official_session_without_bar_is_conservatively_unavailable(
    calendar,
):
    decision = check_bar_availability(
        intent_session=date(2026, 7, 17),
        execution_session=date(2026, 7, 20),
        calendar=calendar,
        available_bar_dates=frozenset(),
    )

    assert decision.status is AvailabilityStatus.NO_BAR_UNAVAILABLE
    assert decision.source_rule_reason is RejectionReason.SUSPENDED_NO_BAR


def test_availability_rejects_skipped_official_session(calendar):
    with pytest.raises(PortfolioError) as captured:
        check_bar_availability(
            intent_session=date(2026, 7, 17),
            execution_session=date(2026, 7, 21),
            calendar=calendar,
            available_bar_dates=frozenset({date(2026, 7, 21)}),
        )

    assert captured.value.code == "invalid_attempt_session"


@pytest.mark.parametrize(
    "available_bar_dates",
    [
        {date(2026, 7, 20)},
        frozenset({datetime(2026, 7, 20)}),
        frozenset({"2026-07-20"}),
    ],
)
def test_availability_requires_an_exact_frozenset_of_dates(
    calendar,
    available_bar_dates,
):
    with pytest.raises(PortfolioError) as captured:
        check_bar_availability(
            intent_session=date(2026, 7, 17),
            execution_session=date(2026, 7, 20),
            calendar=calendar,
            available_bar_dates=available_bar_dates,
        )

    assert captured.value.code == "invalid_availability_input"


def test_availability_rechecks_verified_calendar_identity(calendar):
    object.__setattr__(calendar, "calendar_id", "0" * 64)

    with pytest.raises(PortfolioError) as captured:
        check_bar_availability(
            intent_session=date(2026, 7, 17),
            execution_session=date(2026, 7, 20),
            calendar=calendar,
            available_bar_dates=frozenset({date(2026, 7, 20)}),
        )

    assert captured.value.code == "unverified_calendar"
