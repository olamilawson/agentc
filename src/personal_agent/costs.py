"""Cost by workflow and client for a month, and the monthly cap (PRD: model routing and cost).

The system has a monthly cap that alerts the owner at 80 percent and stops new
runs at 100 percent, except for runs the owner starts by hand. A run's cost
counts in the calendar month (UTC) it was created in.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from personal_agent.config import Settings
from personal_agent.db import Database

ALERT_AT = 0.8


class MonthlyCapReached(Exception):
    """The month's spend is at the cap; only a run the owner starts by hand may begin."""


def month_bounds(month: str | None = None) -> tuple[str, str, str]:
    """(month, since, until) for 'YYYY-MM', or for the current month. Timestamps
    are ISO strings in UTC, so they compare in order as text."""
    if month is None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("month must look like 2026-10")
    year, number = int(month[:4]), int(month[5:])
    following = f"{year + 1}-01" if number == 12 else f"{year}-{number + 1:02d}"
    return month, f"{month}-01T00:00:00", f"{following}-01T00:00:00"


def adjacent_months(month: str) -> tuple[str, str]:
    year, number = int(month[:4]), int(month[5:])
    before = f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"
    after = f"{year + 1}-01" if number == 12 else f"{year}-{number + 1:02d}"
    return before, after


def _grouped(runs: list[dict], key: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for run in runs:
        g = groups.setdefault(run[key], {"name": run[key], "runs": 0, "failed": 0, "tokens": 0, "cost_usd": 0.0})
        g["runs"] += 1
        g["failed"] += run["status"] == "failed"
        g["tokens"] += (run["tokens_in"] or 0) + (run["tokens_out"] or 0)
        g["cost_usd"] += run["cost_usd"] or 0.0
    for g in groups.values():
        g["cost_usd"] = round(g["cost_usd"], 4)
    return sorted(groups.values(), key=lambda g: (-g["cost_usd"], g["name"]))


def cap_status(total_usd: float, cap_usd: float | None) -> dict:
    """state: none (no cap set), ok, alert (80 percent reached) or stopped (cap reached)."""
    if not cap_usd:
        return {"cap_usd": None, "used": None, "state": "none"}
    used = total_usd / cap_usd
    state = "stopped" if used >= 1 else "alert" if used >= ALERT_AT else "ok"
    return {"cap_usd": cap_usd, "used": round(used, 4), "state": state}


def summary(db: Database, settings: Settings, month: str | None = None) -> dict:
    month, since, until = month_bounds(month)
    runs = db.runs_between(since, until)
    total = round(sum(r["cost_usd"] or 0.0 for r in runs), 4)
    return {
        "month": month,
        "total_usd": total,
        "runs": len(runs),
        "by_workflow": _grouped(runs, "workflow"),
        "by_client": _grouped(runs, "client"),
        "cap": cap_status(total, settings.budgets.monthly_cap_usd),
    }


def current_cap(db: Database, settings: Settings) -> dict:
    """The cap's state for the month now running."""
    return summary(db, settings)["cap"]
