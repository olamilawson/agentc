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


def test_retry_keeps_the_assembled_context(settings):
    """The retry appends the validation error to the same context, rubric and brief included."""
    seen: list[str] = []

    class Capturing(RecordedGateway):
        def complete(self, contract, context):
            seen.append(context)
            return super().complete(contract, context)

    runner = NodeRunner(Capturing({"draft": ["not json", VALID_DRAFT]}), BudgetTracker(settings.budgets))
    runner.run(_contract(), {"objective": "x", "context_notes": ""}, context="# Role\nRUBRIC")
    assert seen[0] == "# Role\nRUBRIC"
    assert seen[1].startswith("# Role\nRUBRIC") and "previous output was invalid" in seen[1]


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


def test_a_step_with_pictures_goes_to_the_vision_route_and_a_text_step_does_not():
    from personal_agent.config import TierRoute
    from personal_agent.harness.models import ModelGateway

    class Routed(ModelGateway):
        def _call(self, model, contract, context, fallback_used, images=None):
            return {"data": {}, "model": model, "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}}

    table = {"standard": TierRoute(primary="groq/text-model"), "vision": TierRoute(primary="groq/seeing-model")}
    contract = next(iter(__import__("personal_agent.engine.think", fromlist=["x"]).think_contracts().values()))
    gateway = Routed(table)
    assert gateway.complete(contract, "context")["model"] == "groq/text-model"
    assert gateway.complete(contract, "context", images=[("Page 1", b"jpeg")])["model"] == "groq/seeing-model"
    del table["vision"]  # with no vision route, the tier's own model is asked
    assert gateway.complete(contract, "context", images=[("Page 1", b"jpeg")])["model"] == "groq/text-model"
    with __import__("pytest").raises(Exception, match="not approved for confidential work"):
        gateway.complete(contract, "context", providers={"anthropic"})  # a restricted client still stops here


def test_a_spent_minute_is_waited_out_and_an_account_with_no_credit_is_not():
    import litellm

    from personal_agent.engine.think import think_contracts
    from personal_agent.harness import models

    assert models.rate_limit_wait(Exception("RateLimitError: Rate limit reached. Please try again in 7.5s.")) == 8.5
    assert models.rate_limit_wait(Exception("RateLimitError: try again in 2m10s")) == models.MAX_WAIT_SECONDS
    assert models.rate_limit_wait(Exception("RateLimitError: You have no credits remaining.")) is None
    assert models.rate_limit_wait(Exception("Rate limit reached for tokens per day (TPD)")) is None  # a day will not pass
    assert models.rate_limit_wait(Exception("model_not_found")) is None

    class Reply:
        usage = None

        class _Choice:
            class message:
                content = 'Here it is: {"answer": "Lead with the rate.", "caveats": []} Hope that helps.'

        choices = [_Choice]

    calls, waits = [], []

    def completion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) < 3:
            raise RuntimeError("RateLimitError: Rate limit reached. Please try again in 3s.")
        return Reply()

    real, models.SLEEP, litellm_real = models.SLEEP, waits.append, litellm.completion
    litellm.completion = completion
    try:
        contract = next(iter(think_contracts().values()))
        out = models.ModelGateway({"standard": models.TierRoute(primary="groq/openai/gpt-oss-120b")}).complete(contract, "context")
    finally:
        models.SLEEP, litellm.completion = real, litellm_real
    assert calls == ["groq/openai/gpt-oss-120b"] * 3 and waits == [4.0, 4.0]  # the same model, after the wait Groq named
    assert out["data"] == {"answer": "Lead with the rate.", "caveats": []}  # the object, without the words around it


def test_thinking_aloud_before_the_object_is_set_aside():
    from personal_agent.harness.contract import OutputInvalid, _parse_json

    assert _parse_json('<think>The user wants JSON.</think>\n{"a": 1}') == {"a": 1}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    import pytest

    with pytest.raises(OutputInvalid):
        _parse_json("I could not do that.")
    with pytest.raises(OutputInvalid):
        _parse_json("[1, 2]")
