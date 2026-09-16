"""Top-up state machine (T007; FR-005), pinned to the exact State Transitions
table T002 added to spec.md. This table is data transcribed from the spec,
not policy invented here.
"""
from __future__ import annotations

STATES = (
    "Created",
    "Pending",
    "Processing",
    "RequiresAction",
    "Completed",
    "Failed",
    "Expired",
    "Cancelled",
    "Reversed",
    "UnderReview",
)

ALLOWED_TRANSITIONS = {
    "Created": {"Pending", "Failed"},
    "Pending": {"Processing", "RequiresAction", "Failed", "Expired", "Cancelled"},
    "Processing": {"Completed", "Failed", "UnderReview", "Expired"},
    "RequiresAction": {"Processing", "Failed", "Expired", "Cancelled"},
    "Completed": {"Reversed"},
    "Failed": set(),
    "Expired": set(),
    "Cancelled": set(),
    "UnderReview": {"Completed", "Failed"},
    "Reversed": set(),
}


class InvalidTransitionError(ValueError):
    pass


def transition(current_state: str, to_state: str) -> str:
    if current_state not in ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(f"unknown state: {current_state!r}")
    if to_state not in ALLOWED_TRANSITIONS[current_state]:
        raise InvalidTransitionError(f"{current_state} -> {to_state} is not an allowed transition")
    return to_state
