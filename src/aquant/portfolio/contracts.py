"""Immutable version contracts shared by portfolio producers and verifiers."""

PORTFOLIO_SCHEMA_VERSION = "0.2.0"
PORTFOLIO_ENGINE = "aquant-shared-cash-portfolio-0.2"
PRICE_STREAM_VERSION = "raw-open-close-v1"
DIVIDEND_TAX_MODE = "gross-before-personal-tax-v1"
NO_BAR_VALUATION_MODE = "carry-last-mark-cash-dividend-adjusted-v1"
BUDGET_MODE = "fixed-equal-notional-fee-aware-lot-reduction-v1"
RETRY_MODE = "next-official-session-bounded-attempts-v1"
