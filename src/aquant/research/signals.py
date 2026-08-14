"""Deterministic signal contract: decisions separated from execution state.

A1 scope. A ``Signal`` answers only one question: "using information causally
available as of this date, what target-state decision does the strategy make
for each symbol?" It never submits orders, computes fills, manages cash,
implements T+1, rebalances portfolios, or touches broker/paper-trading state.

Public surface:

- :class:`SignalInput` -- frozen per-symbol causal indicator-price history
- :class:`Signal` -- the minimal explicit signal interface
- :class:`SmaSignal` -- SMA classification compatible with the audited
  ``SmaStrategy`` baseline decision semantics
- :class:`TopKMomentumSignal` -- multi-symbol contract demonstration only
- :func:`validate_signal_output` -- centralized fail-closed output validation
- :data:`SIGNAL_REGISTRY` -- tiny explicit name -> constructor registry

Output contract (frozen): a symbol omitted from the mapping means NO_DECISION
(preserve the later portfolio target state), never zero weight or rejection.
``Decimal("0")`` means an explicit FLAT target. Emitted weights are exact
``Decimal`` objects in ``[0, 1]`` with a total at most one; violations fail
closed with :class:`SignalError`.
"""

from __future__ import annotations

import decimal
import math
import numbers
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from aquant.risk.metrics import sample_standard_deviation


class SignalError(ValueError):
    """Raised when signal inputs or outputs violate the frozen contract."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SignalObservation:
    """One causal ``(session, indicator_close)`` point for a symbol."""

    session: date
    indicator_close: float


@dataclass(frozen=True, init=False)
class SignalInput:
    """Frozen per-symbol causal indicator-price history bounded by ``as_of``.

    Construction fails closed on any observation later than ``as_of`` and on
    ambiguous chronology (duplicate or out-of-order sessions). Instances never
    alias mutable caller-owned containers. No I/O, network, wall clock,
    randomness, or mutable global state is involved.
    """

    as_of: date
    _per_symbol: tuple[tuple[str, tuple[SignalObservation, ...]], ...]

    def __init__(
        self,
        *,
        as_of: date,
        per_symbol: Mapping[str, Sequence[SignalObservation]],
    ) -> None:
        if type(as_of) is not date:
            raise SignalError("invalid_as_of", "as_of must be a date")
        if not isinstance(per_symbol, Mapping):
            raise SignalError("invalid_input", "per_symbol must be a mapping")
        normalized: list[tuple[str, tuple[SignalObservation, ...]]] = []
        seen: set[str] = set()
        for symbol, values in per_symbol.items():
            if type(symbol) is not str or not symbol:
                raise SignalError("invalid_symbol", "symbols must be non-empty strings")
            if symbol in seen:
                raise SignalError("duplicate_symbol", f"symbol {symbol!r} appears more than once")
            seen.add(symbol)
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise SignalError(
                    "invalid_observations",
                    f"observations for {symbol!r} must be a sequence",
                )
            observations: list[SignalObservation] = []
            previous: date | None = None
            for observation in values:
                if type(observation) is not SignalObservation:
                    raise SignalError(
                        "invalid_observation",
                        f"observations for {symbol!r} must be SignalObservation values",
                    )
                session = observation.session
                if type(session) is not date:
                    raise SignalError(
                        "invalid_session",
                        f"observation session for {symbol!r} must be a date",
                    )
                if session > as_of:
                    raise SignalError(
                        "future_observation",
                        f"observation for {symbol!r} on {session.isoformat()} is later "
                        f"than as_of {as_of.isoformat()}",
                    )
                if previous is not None and session <= previous:
                    raise SignalError(
                        "non_ascending_sessions",
                        f"observations for {symbol!r} must be strictly chronological",
                    )
                previous = session
                indicator_close = observation.indicator_close
                if (
                    type(indicator_close) is bool
                    or not isinstance(indicator_close, numbers.Real)
                    or not math.isfinite(float(indicator_close))
                ):
                    raise SignalError(
                        "invalid_indicator_close",
                        f"indicator_close for {symbol!r} must be a finite real number",
                    )
                observations.append(
                    SignalObservation(session=session, indicator_close=float(indicator_close))
                )
            normalized.append((symbol, tuple(observations)))
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "_per_symbol", tuple(normalized))

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(symbol for symbol, _ in self._per_symbol)

    def observations(self, symbol: str) -> tuple[SignalObservation, ...]:
        for candidate, values in self._per_symbol:
            if candidate == symbol:
                return values
        raise SignalError("unknown_symbol", f"symbol {symbol!r} is absent from the input")


def _require_decimal_weight(weight: object, *, symbol: str | None) -> Decimal:
    label = symbol if symbol is not None else "signal"
    if type(weight) is not Decimal:
        raise SignalError(
            "non_decimal_weight",
            f"weight for {label} must be an exact Decimal, not {type(weight).__name__}",
        )
    if not weight.is_finite():
        raise SignalError("non_finite_weight", f"weight for {label} must be finite")
    if weight < 0:
        raise SignalError("negative_weight", f"weight for {label} must not be negative")
    if weight > 1:
        raise SignalError("weight_above_one", f"weight for {label} must not exceed one")
    return weight


def validate_signal_output(
    output: Mapping[str, Decimal],
    data: SignalInput,
) -> Mapping[str, Decimal]:
    """Fail closed on any explicit output contract violation.

    The typing ``Signal`` protocol does not enforce runtime weight rules; this
    helper is the single explicit validation boundary every signal passes
    through before returning.
    """
    if not isinstance(output, Mapping):
        raise SignalError("invalid_output_type", "signal output must be a mapping")
    known = frozenset(data.symbols)
    for symbol, weight in output.items():
        if symbol not in known:
            raise SignalError(
                "unknown_symbol",
                f"output symbol {symbol!r} is absent from the input",
            )
        _require_decimal_weight(weight, symbol=symbol)
    # Sum inside a fixed high-precision context so the <= 1 check never depends
    # on the caller's global Decimal context.
    with decimal.localcontext(decimal.Context(prec=60)):
        total = Decimal("0")
        for weight in output.values():
            total += weight
    if total > Decimal("1"):
        raise SignalError(
            "total_weight_above_one",
            "explicit output weights must not sum above one",
        )
    return output


def _check_as_of(as_of: object, data: SignalInput) -> None:
    if type(as_of) is not date:
        raise SignalError("invalid_as_of", "as_of must be a date")
    if as_of > data.as_of:
        raise SignalError(
            "as_of_beyond_input_horizon",
            "as_of cannot be later than the input horizon",
        )


@runtime_checkable
class Signal(Protocol):
    """Minimal explicit signal interface (compute-only and deterministic)."""

    def compute(self, as_of: date, data: SignalInput) -> Mapping[str, Decimal]:
        """Return the target-state decision mapping for ``data`` as of ``as_of``."""


class SmaSignal:
    """SMA classification matching the audited baseline decision semantics.

    Compatibility notes (kept intentionally unchanged):

    - consumes the causal ``indicator_close`` from :class:`SignalInput`; it
      never reconstructs adjusted prices from raw close
    - SMA price arithmetic uses float semantics: built-in ``sum``, newest-first
      summation order (window offsets 0 through ``period - 1``), plain
      division by ``period``; no ``math.fsum``, no Decimal, no reordering
    - the current bar is included in the window
    - ``close == sma`` is NO_DECISION (preserve the later target state), never
      a liquidation signal
    - history shorter than ``period`` is NO_DECISION (symbol omitted)

    This is a single-symbol compatibility signal: it reproduces the audited
    single-instrument ``SmaStrategy`` classification semantics and emits its
    fixed ``active_weight`` unchanged. Multi-symbol inputs fail closed with
    ``SignalError("single_symbol_only", ...)`` before any output is generated;
    the explicit-weight total contract therefore can never be violated by this
    signal itself. Portfolio-level multi-symbol target weights belong to the
    later planner scope (A2), not to this signal.

    ``active_weight`` exists only for compatibility with the current baseline
    sizing convention and is not intended to permanently couple alpha
    generation with portfolio sizing. It must be strictly positive
    (``0 < active_weight <= 1``): a zero value would collapse the ACTIVE
    classification into FLAT and break the three-state contract.
    """

    def __init__(self, period: int, active_weight: Decimal = Decimal("0.95")):
        if type(period) is not int or period <= 0:
            raise SignalError("invalid_period", "period must be a positive integer")
        _require_decimal_weight(active_weight, symbol=None)
        if active_weight == 0:
            raise SignalError(
                "invalid_active_weight",
                "active_weight must be strictly positive; Decimal('0') would collapse "
                "ACTIVE into FLAT",
            )
        self._period = period
        self._active_weight = active_weight

    @property
    def period(self) -> int:
        return self._period

    @property
    def active_weight(self) -> Decimal:
        return self._active_weight

    def compute(self, as_of: date, data: SignalInput) -> Mapping[str, Decimal]:
        _check_as_of(as_of, data)
        if len(data.symbols) != 1:
            raise SignalError(
                "single_symbol_only",
                "SmaSignal is a single-symbol compatibility signal; multi-symbol "
                "inputs are not supported",
            )
        symbol = data.symbols[0]
        history = tuple(o for o in data.observations(symbol) if o.session <= as_of)
        if len(history) < self._period:
            return validate_signal_output({}, data)  # OMIT / NO_DECISION
        window = [o.indicator_close for o in reversed(history[-self._period :])]
        sma = sum(window) / self._period
        close = window[0]
        if close > sma:
            output = {symbol: self._active_weight}
        elif close < sma:
            output = {symbol: Decimal("0")}
        else:
            output = {}  # close == sma -> OMIT / NO_DECISION
        return validate_signal_output(output, data)


class VolatilityRegimeDefenseSignal:
    """Single-symbol defensive allocation from causal realized volatility.

    ``lookback_returns=N`` consumes the latest ``N`` simple close-to-close
    returns and therefore requires ``N + 1`` causal ``indicator_close``
    observations. Volatility uses the repository's existing sample standard
    deviation convention (``ddof=1``) and is annualized by
    ``sqrt(annualization)``.

    A value at or below the threshold emits ACTIVE. A value above it emits the
    existing explicit FLAT weight. Insufficient history and invalid arithmetic
    omit the symbol (NO_DECISION); non-finite raw inputs remain rejected by
    :class:`SignalInput` before this signal runs.
    """

    def __init__(
        self,
        *,
        lookback_returns: int,
        annualization: int,
        volatility_threshold: Decimal,
        active_weight: Decimal = Decimal("0.95"),
    ) -> None:
        if type(lookback_returns) is not int or lookback_returns < 2:
            raise SignalError(
                "invalid_lookback",
                "lookback_returns must be an integer of at least two",
            )
        if type(annualization) is not int or annualization <= 0:
            raise SignalError(
                "invalid_annualization",
                "annualization must be a positive integer",
            )
        _require_decimal_weight(volatility_threshold, symbol=None)
        if volatility_threshold == 0:
            raise SignalError(
                "invalid_volatility_threshold",
                "volatility_threshold must be strictly positive",
            )
        _require_decimal_weight(active_weight, symbol=None)
        if active_weight == 0:
            raise SignalError(
                "invalid_active_weight",
                "active_weight must be strictly positive",
            )
        self._lookback_returns = lookback_returns
        self._annualization = annualization
        self._volatility_threshold = volatility_threshold
        self._active_weight = active_weight

    @property
    def lookback_returns(self) -> int:
        return self._lookback_returns

    @property
    def annualization(self) -> int:
        return self._annualization

    @property
    def volatility_threshold(self) -> Decimal:
        return self._volatility_threshold

    @property
    def active_weight(self) -> Decimal:
        return self._active_weight

    def compute(self, as_of: date, data: SignalInput) -> Mapping[str, Decimal]:
        _check_as_of(as_of, data)
        if len(data.symbols) != 1:
            raise SignalError(
                "single_symbol_only",
                "VolatilityRegimeDefenseSignal supports exactly one symbol",
            )
        symbol = data.symbols[0]
        history = tuple(o for o in data.observations(symbol) if o.session <= as_of)
        required_closes = self._lookback_returns + 1
        if len(history) < required_closes:
            return validate_signal_output({}, data)
        closes = tuple(o.indicator_close for o in history[-required_closes:])
        if any(close <= 0 for close in closes):
            return validate_signal_output({}, data)
        returns = tuple(
            current / previous - 1.0
            for previous, current in zip(closes[:-1], closes[1:], strict=True)
        )
        if any(not math.isfinite(value) for value in returns):
            return validate_signal_output({}, data)
        standard_deviation = sample_standard_deviation(returns)
        if standard_deviation is None:
            return validate_signal_output({}, data)
        annualized_volatility = standard_deviation * math.sqrt(self._annualization)
        if not math.isfinite(annualized_volatility):
            return validate_signal_output({}, data)
        output = (
            {symbol: self._active_weight}
            if annualized_volatility <= float(self._volatility_threshold)
            else {symbol: Decimal("0")}
        )
        return validate_signal_output(output, data)


_FIXED_DECIMAL_CONTEXT = decimal.Context(prec=50, rounding=decimal.ROUND_DOWN)


class TopKMomentumSignal:
    """Contract demonstration: deterministic multi-symbol top-k momentum.

    NOT a product strategy. Not wired into backtesting, portfolio
    coordination, CLI, paper trading, or execution. Exists solely to prove the
    :class:`Signal` contract supports multi-symbol output mappings.

    ``lookback=N`` means N trading intervals and therefore requires ``N + 1``
    observations per eligible symbol. The return is
    ``latest_indicator_close / indicator_close_N_intervals_ago - 1``. Ranking
    is descending return with ascending symbol as the deterministic tie-break.
    Equal selected weights use a fixed internal Decimal context with
    ``ROUND_DOWN``, so results never depend on the caller's global Decimal
    context; a tiny residual cash weight is accepted.
    """

    def __init__(self, lookback: int, k: int):
        if type(lookback) is not int or lookback <= 0:
            raise SignalError("invalid_lookback", "lookback must be a positive integer")
        if type(k) is not int or k <= 0:
            raise SignalError("invalid_k", "k must be a positive integer")
        self._lookback = lookback
        self._k = k

    @property
    def lookback(self) -> int:
        return self._lookback

    @property
    def k(self) -> int:
        return self._k

    def compute(self, as_of: date, data: SignalInput) -> Mapping[str, Decimal]:
        _check_as_of(as_of, data)
        eligible: list[tuple[float, str]] = []
        for symbol in data.symbols:
            history = tuple(o for o in data.observations(symbol) if o.session <= as_of)
            if len(history) < self._lookback + 1:
                continue  # OMIT / NO_DECISION
            base = history[-1 - self._lookback].indicator_close
            if base <= 0:
                raise SignalError(
                    "nonpositive_indicator_close",
                    f"cannot rank momentum for {symbol!r} with a non-positive base price",
                )
            ret = history[-1].indicator_close / base - 1.0
            eligible.append((ret, symbol))
        if not eligible:
            return validate_signal_output({}, data)
        eligible.sort(key=lambda item: (-item[0], item[1]))
        selected = eligible[: self._k]
        with decimal.localcontext(_FIXED_DECIMAL_CONTEXT):
            weight = Decimal(1) / Decimal(len(selected))
        output = {symbol: weight for _, symbol in selected}
        for _, symbol in eligible[self._k :]:
            output[symbol] = Decimal("0")
        return validate_signal_output(output, data)


SIGNAL_REGISTRY: Mapping[str, type[Signal]] = {
    "sma": SmaSignal,
    "top_k_momentum": TopKMomentumSignal,
}
