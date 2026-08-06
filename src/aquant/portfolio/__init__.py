"""Shared-cash portfolio research contracts."""

from importlib import import_module

from aquant.portfolio.accounting import (
    BuyPosting,
    CashEventKind,
    CashLedgerEvent,
    CashReceivable,
    DailyAccountSnapshot,
    PortfolioLedger,
    SymbolValuation,
    cash_after_fill,
    close_session,
    create_portfolio_ledger,
    decimal_yuan_to_fen,
    notional_fen,
    pay_receivables,
    post_buy,
    register_receivable,
    verify_portfolio_ledger,
)
from aquant.portfolio.availability import (
    AvailabilityDecision,
    AvailabilityStatus,
    check_bar_availability,
)
from aquant.portfolio.contracts import (
    BUDGET_MODE,
    DIVIDEND_TAX_MODE,
    NO_BAR_VALUATION_MODE,
    PORTFOLIO_ENGINE,
    PORTFOLIO_SCHEMA_VERSION,
    PRICE_STREAM_VERSION,
    RETRY_MODE,
)
from aquant.portfolio.coordinator import (
    AttemptStatus,
    AvailabilityAudit,
    DividendAudit,
    EntryAttempt,
    EntryTarget,
    PortfolioBacktestResult,
    TargetStatus,
    actual_cash_date,
    run_portfolio_backtest,
)
from aquant.portfolio.models import (
    PortfolioConfig,
    PortfolioError,
    PortfolioInstrumentInput,
    PortfolioStrategy,
    TargetAllocation,
    allocate_equal_targets,
    validate_portfolio_inputs,
)

_LAZY_EXPORTS = {
    "PORTFOLIO_ARTIFACT_FILES": (
        "aquant.portfolio.export",
        "PORTFOLIO_ARTIFACT_FILES",
    ),
    "PORTFOLIO_PAYLOAD_FILES": (
        "aquant.portfolio.export",
        "PORTFOLIO_PAYLOAD_FILES",
    ),
    "PortfolioArtifactError": (
        "aquant.portfolio.verify",
        "PortfolioArtifactError",
    ),
    "PortfolioExportError": (
        "aquant.portfolio.export",
        "PortfolioExportError",
    ),
    "PortfolioMetrics": (
        "aquant.portfolio.metrics",
        "PortfolioMetrics",
    ),
    "PortfolioRunIdentity": (
        "aquant.portfolio.identity",
        "PortfolioRunIdentity",
    ),
    "VerifiedPortfolioArtifact": (
        "aquant.portfolio.verify",
        "VerifiedPortfolioArtifact",
    ),
    "VerifiedPortfolioRun": (
        "aquant.portfolio.identity",
        "VerifiedPortfolioRun",
    ),
    "compute_portfolio_metrics": (
        "aquant.portfolio.metrics",
        "compute_portfolio_metrics",
    ),
    "export_portfolio_run": (
        "aquant.portfolio.export",
        "export_portfolio_run",
    ),
    "portfolio_payload_bytes": (
        "aquant.portfolio.export",
        "portfolio_payload_bytes",
    ),
    "run_verified_portfolio": (
        "aquant.portfolio.identity",
        "run_verified_portfolio",
    ),
    "verify_portfolio_artifact": (
        "aquant.portfolio.verify",
        "verify_portfolio_artifact",
    ),
    "verify_portfolio_run": (
        "aquant.portfolio.identity",
        "verify_portfolio_run",
    ),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "AvailabilityDecision",
    "AvailabilityAudit",
    "AvailabilityStatus",
    "AttemptStatus",
    "BuyPosting",
    "BUDGET_MODE",
    "CashEventKind",
    "CashLedgerEvent",
    "CashReceivable",
    "DailyAccountSnapshot",
    "DIVIDEND_TAX_MODE",
    "DividendAudit",
    "EntryAttempt",
    "EntryTarget",
    "PortfolioConfig",
    "PortfolioBacktestResult",
    "PortfolioError",
    "PortfolioArtifactError",
    "PortfolioExportError",
    "PortfolioInstrumentInput",
    "PortfolioLedger",
    "PortfolioMetrics",
    "PortfolioRunIdentity",
    "PortfolioStrategy",
    "PORTFOLIO_ENGINE",
    "PORTFOLIO_ARTIFACT_FILES",
    "PORTFOLIO_PAYLOAD_FILES",
    "PORTFOLIO_SCHEMA_VERSION",
    "PRICE_STREAM_VERSION",
    "NO_BAR_VALUATION_MODE",
    "RETRY_MODE",
    "SymbolValuation",
    "TargetAllocation",
    "TargetStatus",
    "allocate_equal_targets",
    "actual_cash_date",
    "cash_after_fill",
    "check_bar_availability",
    "close_session",
    "compute_portfolio_metrics",
    "create_portfolio_ledger",
    "decimal_yuan_to_fen",
    "export_portfolio_run",
    "notional_fen",
    "pay_receivables",
    "post_buy",
    "portfolio_payload_bytes",
    "register_receivable",
    "run_portfolio_backtest",
    "run_verified_portfolio",
    "validate_portfolio_inputs",
    "verify_portfolio_ledger",
    "verify_portfolio_artifact",
    "verify_portfolio_run",
    "VerifiedPortfolioArtifact",
    "VerifiedPortfolioRun",
]
