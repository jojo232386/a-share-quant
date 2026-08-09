"""Public API for immutable rolling target-state planning."""

from .assembly import SIGNAL_SPECS, SignalCardinality, SignalSpec, build_signal
from .core import (
    PLANNER_SCHEMA_VERSION,
    NoPreviousState,
    NoPreviousStateReason,
    PlannedTargets,
    PlannerError,
    PlannerLimits,
    PreviousTargets,
    plan_targets,
)

__all__ = [
    "PLANNER_SCHEMA_VERSION",
    "SIGNAL_SPECS",
    "NoPreviousState",
    "NoPreviousStateReason",
    "PlannedTargets",
    "PlannerError",
    "PlannerLimits",
    "PreviousTargets",
    "SignalCardinality",
    "SignalSpec",
    "build_signal",
    "plan_targets",
]
