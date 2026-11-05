# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Optional tool-list changes by model size.

All options are off after benchmark results. See ARCHITECTURE.md.
"""
from __future__ import annotations

B = 1_000_000_000

# Use parameter count for the initial tier. Prefer measured capabilities later.
TIER_BOUNDS = (
    ("tiny", 2 * B),
    ("small", 8 * B),
    ("mid", 20 * B),
)
FULL = "full"

# Keep all options off; each performed worse in the five-model benchmark.
# Leave the table available for future comparisons.
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
    """Put high-priority tools at the ends of the list. Used only by the benchmark."""
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
