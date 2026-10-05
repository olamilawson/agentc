"""Per-run ceilings (PRD: budgets and failure).

Hitting any ceiling stops the run with a clear reason and keeps the partial
work. The harness never swaps in a different model silently; a fallback is
allowed only if the route table names it, and the run record shows it.
"""

from __future__ import annotations

import time

from personal_agent.config import Budgets


class BudgetExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BudgetTracker:
    def __init__(self, budgets: Budgets, started_at: float | None = None):
        self.budgets = budgets
        self.started_at = started_at if started_at is not None else time.monotonic()
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.tool_calls = 0

    def count_tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.budgets.max_tool_calls:
            raise BudgetExceeded(f"tool-call ceiling reached ({self.budgets.max_tool_calls})")

    def add_usage(self, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost_usd
        self.check()

    def check(self) -> None:
        b = self.budgets
        total = self.tokens_in + self.tokens_out
        if total > b.max_total_tokens:
            raise BudgetExceeded(f"token ceiling reached ({total} > {b.max_total_tokens})")
        if self.cost_usd > b.max_cost_usd:
            raise BudgetExceeded(f"cost ceiling reached (${self.cost_usd:.2f} > ${b.max_cost_usd:.2f})")
        if time.monotonic() - self.started_at > b.max_elapsed_seconds:
            raise BudgetExceeded(f"elapsed-time ceiling reached ({b.max_elapsed_seconds}s)")
