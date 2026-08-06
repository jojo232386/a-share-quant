"""Conservative v0.2 market-bar availability boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from aquant.data.calendar_snapshot import (
    CalendarError,
    VerifiedTradingCalendar,
    verify_trading_calendar,
)
from aquant.portfolio.models import PortfolioError
from aquant.rules import RejectionReason


class AvailabilityStatus(StrEnum):
    """Auditable availability states understood by the portfolio engine."""

    AVAILABLE = "available"
    NO_BAR_UNAVAILABLE = "no_bar_unavailable"


@dataclass(frozen=True)
class AvailabilityDecision:
    """One exact-session bar-availability decision."""

    status: AvailabilityStatus
    source_rule_reason: RejectionReason | None

    def __post_init__(self) -> None:
        if type(self.status) is not AvailabilityStatus:
            raise PortfolioError(
                "invalid_availability_decision",
                "availability status is invalid",
            )
        if (
            self.status is AvailabilityStatus.AVAILABLE
            and self.source_rule_reason is not None
            or self.status is AvailabilityStatus.NO_BAR_UNAVAILABLE
            and self.source_rule_reason is not RejectionReason.SUSPENDED_NO_BAR
        ):
            raise PortfolioError(
                "invalid_availability_decision",
                "availability reason does not match its status",
            )


def check_bar_availability(
    *,
    intent_session: date,
    execution_session: date,
    calendar: VerifiedTradingCalendar,
    available_bar_dates: frozenset[date],
) -> AvailabilityDecision:
    """Require one exact next-session attempt, then check for a real bar."""
    try:
        verify_trading_calendar(calendar)
    except (AttributeError, CalendarError, TypeError, ValueError) as exc:
        raise PortfolioError(
            "unverified_calendar",
            "availability requires an exact verified calendar",
        ) from exc
    if (
        type(intent_session) is not date
        or type(execution_session) is not date
        or type(available_bar_dates) is not frozenset
        or any(type(item) is not date for item in available_bar_dates)
    ):
        raise PortfolioError(
            "invalid_availability_input",
            "availability input is invalid",
        )
    if (
        not calendar.contains(intent_session)
        or calendar.next_session(intent_session) != execution_session
    ):
        raise PortfolioError(
            "invalid_attempt_session",
            "attempt must target the exact next official session",
        )
    if execution_session not in available_bar_dates:
        return AvailabilityDecision(
            AvailabilityStatus.NO_BAR_UNAVAILABLE,
            RejectionReason.SUSPENDED_NO_BAR,
        )
    return AvailabilityDecision(AvailabilityStatus.AVAILABLE, None)
