"""Step contract: retry once with the validation error appended, then fail with the reason."""

from __future__ import annotations

import pytest

from personal_agent.harness.budgets import BudgetExceeded, BudgetTracker
from personal_agent.harness.contract import StepContract, StepFailed
from personal_agent.harness.models import RecordedGateway
from personal_agent.harness.runner import NodeRunner
from tests.conftest import INPUTS, VALID_DRAFT, make_engine, settings  # noqa: F401


def _contract() -> StepContract:
    from personal_agent.engine.runner import default_contracts

    return default_contracts()["draft"]


def test_invalid_output_retries_once_then_succeeds(settings):
    contract = _contract()
    gateway = RecordedGateway({"draft": ["this is not json", VALID_DRAFT]})
    runner = NodeRunner(gateway, BudgetTracker(settings.budgets))
    out = runner.run(contract, {"objective": "x", "context_notes": ""})
    assert out["message_subject"] == VALID_DRAFT["message_subject"]


def test_invalid_output_twice_fails_run_with_reason(settings):
    contract = _contract()
    gateway = RecordedGateway({"draft": ["nope", "still nope"]})
    runner = NodeRunner(gateway, BudgetTracker(settings.budgets))
    with pytest.raises(StepFailed) as e:
        runner.run(contract, {"objective": "x", "context_notes": ""})
    assert "failed validation after retry" in str(e.value)


def test_failed_run_is_recorded_with_reason(settings):
    engine = make_engine(settings, ["nope", "still nope"])
    run_id = engine.start_run("foundation-test", "acme", INPUTS)
    run = engine.db.get_run(run_id)
    assert run["status"] == "failed"
    assert "failed validation after retry" in run["error_reason"]


def test_budget_ceiling_stops_run_and_keeps_reason(settings):
    from personal_agent.config import Budgets

    settings.budgets = Budgets(max_total_tokens=10, max_tool_calls=10, max_cost_usd=5.0)
    engine = make_engine(settings, [VALID_DRAFT])  # recorded usage is 150+80 tokens
    run_id = engine.start_run("foundation-test", "acme", INPUTS)
    run = engine.db.get_run(run_id)
    assert run["status"] == "failed"
    assert "ceiling reached" in run["error_reason"]
