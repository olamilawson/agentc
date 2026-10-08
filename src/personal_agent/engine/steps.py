"""One bounded model call under a node's contract, shared by the workflow graphs."""

from __future__ import annotations

from personal_agent.engine.graph import ROLE
from personal_agent.harness.budgets import BudgetTracker
from personal_agent.harness.context import build_context


def run_model(
    h,
    node: str,
    state: dict,
    payload: dict,
    rubric: str,
    untrusted: list[tuple[str, str]],
    hide: tuple[str, ...] = (),
) -> tuple[dict, dict]:
    """Returns (validated output, state update carrying the run's usage so far).

    Outside material travels as marked data in `untrusted`; `hide` names the
    payload fields already shown there, so they are not repeated as inputs.
    """
    tracker = BudgetTracker(h.settings.budgets)
    prior = state.get("usage") or {}
    tracker.add_usage(prior.get("tokens_in", 0), prior.get("tokens_out", 0), prior.get("cost_usd", 0.0))
    context, _ = build_context(
        ROLE, rubric, [], untrusted, {k: v for k, v in payload.items() if k not in hide}
    )
    out = h.node_runner(tracker).run(h.contracts[node], payload, context=context)
    return out, {
        "usage": {
            "tokens_in": tracker.tokens_in,
            "tokens_out": tracker.tokens_out,
            "cost_usd": tracker.cost_usd,
        },
        "fallback_used": state.get("fallback_used", False) or getattr(h.gateway, "used_fallback", False),
    }
