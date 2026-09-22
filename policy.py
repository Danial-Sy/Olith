# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""How much of Aether's surface a given model can hold.

Maps a capability profile onto a set of dials. `agent` decides what the dials
mean. Every dial ships off: see ARCHITECTURE.md for the measurements.
"""
from __future__ import annotations

B = 1_000_000_000

# Parameter count is all that is known before a model has run, so it seeds the
# tier. Anything measured later outranks it.
TIER_BOUNDS = (
    ("tiny", 2 * B),
    ("small", 8 * B),
    ("mid", 20 * B),
)
FULL = "full"

# Every dial is off. All three were implemented, benchmarked against five real
# models and rejected because they measured worse. The table stays so
# scripts/bench_tool_choice.py can re-ask on a model we have not seen.
_OFF = {"tool_budget": None, "order_tools": False, "lean_schemas": False}
POLICIES = {
    "tiny": dict(_OFF),
    "small": dict(_OFF),
    "mid": dict(_OFF),
    FULL: dict(_OFF),
}


def tier_for(profile: dict | None) -> str:
    """The tier a profile falls in. An unknown size is treated as full, since
    guessing small on a model we cannot size would hobble it."""
    params = int((profile or {}).get("params") or 0)
    if params <= 0:
        return FULL
    for name, ceiling in TIER_BOUNDS:
        if params < ceiling:
            return name
    return FULL


def policy_for(profile: dict | None) -> dict:
    """The dials for this model, as a fresh dict callers may keep."""
    policy = dict(POLICIES[tier_for(profile)])
    policy["tier"] = tier_for(profile)
    return policy


def edge_order(items: list) -> list:
    """Reorder by priority so the most important sit at the ends.

    Tools in the middle of a list are selected correctly 22-52% of the time
    against 31-32% at either end, so the least important end up there.
    """
    out: list = [None] * len(items)
    front, back = 0, len(items) - 1
    for i, item in enumerate(items):
        if i % 2 == 0:
            out[front] = item
            front += 1
        else:
            out[back] = item
            back -= 1
    return out
