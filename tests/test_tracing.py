"""The run trace: steps in order, model calls and tool calls per step, cost per node."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from personal_agent import learning, tracing
from personal_agent.api.main import create_app
from personal_agent.db import Database
from personal_agent.engine.runner import Engine, default_contracts
from personal_agent.harness.budgets import BudgetTracker
from personal_agent.harness.models import ModelGateway, RecordedGateway
from personal_agent.harness.runner import NodeRunner
from tests.conftest import INPUTS, VALID_DRAFT, make_engine
from tests.test_strategy_drafting import CLEAN, GOAL, OUTLINE, body


def _steps(trace: dict) -> list[tuple[str, str]]:
    return [(s["node"], s["status"]) for s in trace["steps"]]


def test_trace_follows_a_run_through_its_pause_with_cost_on_the_model_step(settings):
    engine = make_engine(settings, [VALID_DRAFT])
    run_id = engine.start_run("foundation-test", "acme", INPUTS)
    trace = tracing.build_trace(engine.db, run_id)
    assert _steps(trace) == [("intake", "done"), ("draft", "done"), ("approval", "waiting")]

    draft = trace["steps"][1]
    (call,) = draft["model_calls"]
    assert (call["tier"], call["model"], call["outcome"], call["attempt"]) == ("standard", "recorded", "ok", 1)
    assert call["input"]["objective"] == INPUTS["objective"]
    assert call["output"]["message_subject"] == VALID_DRAFT["message_subject"]
    assert len(call["prompt_version"]) == 12
    assert (draft["cost_usd"], draft["tokens"]) == (0.002, 230)
    assert trace["steps"][0]["model_calls"] == [] and trace["steps"][0]["cost_usd"] == 0

    engine.approve(run_id, "approved", None)
    trace = tracing.build_trace(engine.db, run_id)
    assert _steps(trace)[3:] == [("approval", "done"), ("release", "done"), ("finalize", "done")]
    release = trace["steps"][4]
    assert [(t["tool"], t["tool_class"], t["outcome"]) for t in release["tool_calls"]] == [
        ("send_test_message", "release", "executed")
    ]
    assert trace["cost_by_node"][0] == {"node": "draft", "steps": 1, "model_calls": 1, "tokens": 230, "cost_usd": 0.002}
    assert trace["traced_cost_usd"] == trace["run"]["cost_usd"] and trace["untraced_cost_usd"] == 0
    assert trace["prompt_versions"] == [("draft", call["prompt_version"])]


def test_a_retried_call_and_a_failed_step_are_both_on_the_trace(settings):
    engine = make_engine(settings, ["not json", VALID_DRAFT])
    retried = tracing.build_trace(engine.db, engine.start_run("foundation-test", "acme", INPUTS))
    calls = retried["steps"][1]["model_calls"]
    assert [(c["attempt"], c["outcome"]) for c in calls] == [(1, "invalid output"), (2, "ok")]
    assert "not valid JSON" in calls[0]["error"]

    failed = tracing.build_trace(engine.db, engine.start_run("foundation-test", "acme", INPUTS))
    assert _steps(failed) == [("intake", "done"), ("draft", "failed")]
    assert "no recorded response" in failed["steps"][1]["error"]
    assert failed["steps"][1]["model_calls"][0]["outcome"] == "provider error"


def test_each_section_of_a_draft_is_its_own_step_with_its_own_cost(settings):
    gateway = RecordedGateway({
        "questions": [{"questions": []}], "outline": [OUTLINE],
        "draft_section": [body("One."), body("Two.")], "critique": [CLEAN],
    })
    engine = Engine(settings, Database(settings.database_url), gateway=gateway)
    run_id = engine.start_run("strategy-drafting", "acme", {"goal": GOAL})
    engine.approve(run_id, "approved", None)
    trace = tracing.build_trace(engine.db, run_id)
    sections = [s for s in trace["steps"] if s["node"] == "draft_section"]
    assert len(sections) == 2 and all(s["cost_usd"] == 0.002 for s in sections)
    assert [c["input"]["section_number"] for s in sections for c in s["model_calls"]] == [1, 2]
    by_node = {n["node"]: n for n in trace["cost_by_node"]}
    assert by_node["draft_section"]["cost_usd"] == 0.004 and by_node["draft_section"]["steps"] == 2
    assert by_node["intake"]["cost_usd"] == 0
    assert round(sum(n["cost_usd"] for n in trace["cost_by_node"]), 6) == trace["run"]["cost_usd"] == 0.01


def test_learning_review_calls_are_traced_though_it_is_not_a_graph(settings):
    skill = settings.skills_dir / "foundation-test" / "SKILL.md"
    engine = make_engine(settings, [VALID_DRAFT])
    run_id = engine.start_run("foundation-test", "acme", INPUTS)
    engine.approve(run_id, "sent_back", "too long")
    engine.harness.gateway._responses["propose_skill_changes"] = [{"proposals": []}]
    review = learning.run_review(engine)
    trace = tracing.build_trace(engine.db, review["id"])
    assert _steps(trace) == [("propose_skill_changes", "done")]
    assert trace["steps"][0]["model_calls"][0]["tier"] == "strong"
    assert trace["traced_cost_usd"] == review["cost_usd"] == 0.002 and skill.exists()


def test_long_text_is_shortened_in_the_trace_and_says_so():
    clipped = tracing.clip({"brief": "x" * 2000, "pages": list(range(60)), "n": 3})
    assert clipped["brief"].endswith("[... 500 more characters]") and len(clipped["brief"]) < 1600
    assert clipped["pages"][-1] == "[... 10 more items]" and clipped["n"] == 3


def test_an_unparseable_reply_is_still_charged_to_the_run(settings, monkeypatch):
    import litellm

    replies = iter(["I cannot answer in JSON.", '{"message_subject": "s", "message_body": "b", "questions": []}'])

    def fake_completion(**kwargs):
        message = SimpleNamespace(content=next(replies))
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    monkeypatch.setattr(litellm, "completion", fake_completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda response: 0.01)
    tracker, recorded = BudgetTracker(settings.budgets), []
    runner = NodeRunner(ModelGateway(settings.route_table), tracker, recorded.append)
    runner.run(default_contracts()["draft"], {"objective": "x", "context_notes": ""})
    assert (tracker.tokens_in, tracker.tokens_out, round(tracker.cost_usd, 2)) == (200, 40, 0.02)
    assert [(r["outcome"], r["cost_usd"]) for r in recorded] == [("invalid output", 0.01), ("ok", 0.01)]
    assert recorded[0]["output"] == "I cannot answer in JSON."


def test_runs_pages_list_runs_and_show_one_with_cost_by_node(settings):
    hostile = {**VALID_DRAFT, "message_body": "<script>alert(1)</script>"}
    client = TestClient(create_app(settings, gateway=RecordedGateway({"draft": [hostile]})))
    run_id = client.post("/runs", json={"workflow": "foundation-test", "client": "acme", "inputs": INPUTS}).json()["run_id"]

    listing = client.get("/inspect").text
    assert f'href="/inspect/{run_id}"' in listing and "waiting for approval" in listing
    assert run_id not in client.get("/inspect", params={"status": "failed"}).text

    page = client.get(f"/inspect/{run_id}").text
    assert "Cost by node" in page and "2. draft" in page and "3. approval" in page
    assert "$0.0020" in page and "standard tier" in page and "skill library" in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert client.get(f"/runs/{run_id}/trace").json()["cost_by_node"][0]["node"] == "draft"
    assert client.get("/inspect/nope").status_code == 404
    assert client.get("/runs/nope/trace").status_code == 404
    assert 'href="/inspect"' in client.get("/queue").text
