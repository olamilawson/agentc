"""The restricted-client flag: a restricted client's runs reach only approved providers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from personal_agent import learning, retention, tracing
from personal_agent.api.main import create_app
from personal_agent.db import Database, audit_log
from personal_agent.engine.runner import Engine
from personal_agent.harness.models import RecordedGateway
from tests.conftest import INPUTS, VALID_DRAFT

# The fixture's standard tier: primary openai/gpt-5, fallback anthropic/claude-sonnet-4-5.


@pytest.fixture
def called(monkeypatch):
    """Every model the gateway actually calls; openai can be made to fail."""
    import litellm

    class Calls(list):
        failing: set[str]

    models = Calls()
    models.failing = set()

    def fake_completion(**kwargs):
        models.append(kwargs["model"])
        if kwargs["model"].split("/")[0] in models.failing:
            raise RuntimeError("provider is down")
        message = SimpleNamespace(content=json.dumps(VALID_DRAFT))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    monkeypatch.setattr(litellm, "completion", fake_completion)
    return models


def _engine(settings, approved: list[str], restricted: bool = True) -> Engine:
    settings.confidential_providers = approved
    engine = Engine(settings, Database(settings.database_url))  # the real gateway and route table
    engine.harness.clients.ensure("acme")
    engine.db.set_restricted("acme", restricted)
    return engine


def _run(engine: Engine, client: str = "acme") -> dict:
    return engine.db.get_run(engine.start_run("foundation-test", client, INPUTS))


def test_an_unrestricted_client_uses_the_route_table_as_written(settings, called):
    run = _run(_engine(settings, approved=["anthropic"], restricted=False))
    assert run["status"] == "waiting_for_approval" and called == ["openai/gpt-5"]
    assert run["fallback_used"] is False


def test_a_restricted_client_never_reaches_a_provider_that_is_not_approved(settings, called):
    engine = _engine(settings, approved=["anthropic"])
    run = _run(engine)
    assert run["status"] == "waiting_for_approval"
    assert called == ["anthropic/claude-sonnet-4-5"]  # the primary was never called
    # The fallback answered, and the run record and the trace both say so.
    assert run["fallback_used"] is True
    (call,) = tracing.build_trace(engine.db, run["id"])["steps"][1]["model_calls"]
    assert (call["model"], call["fallback_used"]) == ("anthropic/claude-sonnet-4-5", True)

    # Another client in the same engine is not affected.
    assert _run(engine, "beta")["status"] == "waiting_for_approval"
    assert called[-1] == "openai/gpt-5"


def test_with_no_approved_provider_a_restricted_run_stops_before_any_model_call(settings, called):
    engine = _engine(settings, approved=[])
    run = _run(engine)
    assert run["status"] == "failed" and called == []
    assert "restricted" in run["error_reason"] and "not approved for confidential work" in run["error_reason"]
    step = tracing.build_trace(engine.db, run["id"])["steps"][1]
    assert step["status"] == "failed" and step["model_calls"][0]["outcome"] == "refused: provider not approved"


def test_a_failing_approved_provider_does_not_fall_back_to_one_that_is_not(settings, called):
    called.failing.add("openai")
    run = _run(_engine(settings, approved=["openai"]))
    assert run["status"] == "failed" and "provider is down" in run["error_reason"]
    assert called == ["openai/gpt-5"]  # anthropic is the fallback but is not approved


def test_the_flag_takes_effect_on_a_run_already_under_way(settings, called):
    from personal_agent.harness.budgets import BudgetTracker

    engine = _engine(settings, approved=["anthropic"], restricted=False)
    run = _run(engine)  # paused at approval; its client is not restricted yet

    def limit_for_next_call():
        return engine.harness.node_runner(BudgetTracker(settings.budgets), run["id"]).providers

    assert limit_for_next_call() is None
    engine.db.set_restricted("acme", True)
    assert limit_for_next_call() == {"anthropic"}  # the same run's next model call is limited
    engine.db.set_restricted("acme", False)
    assert limit_for_next_call() is None


def test_learning_review_leaves_a_restricted_clients_material_out(settings):
    gateway = RecordedGateway({"draft": [VALID_DRAFT, VALID_DRAFT], "propose_skill_changes": [{"proposals": []}]})
    engine = Engine(settings, Database(settings.database_url), gateway=gateway)
    open_run = engine.start_run("foundation-test", "beta", INPUTS)
    secret_run = engine.start_run("foundation-test", "acme", INPUTS)
    engine.approve(open_run, "sent_back", "too long")
    engine.approve(secret_run, "sent_back", "mentions the unannounced merger")
    engine.db.set_restricted("acme", True)

    since, until = "0", "9"
    comments = learning.gather_evidence(engine.db, since, until)["foundation-test"]["comments"]
    assert [c["run_id"] for c in comments] == [open_run]
    assert "merger" not in json.dumps(learning.gather_evidence(engine.db, since, until))


def test_api_sets_and_lifts_the_flag_and_the_change_is_audited(settings):
    settings.confidential_providers = ["anthropic"]
    client = TestClient(create_app(settings, gateway=RecordedGateway({})))
    client.post("/clients", json={"name": "acme"})

    assert client.get("/clients/acme").json()["restricted"] is False
    assert "Not restricted" in client.get("/files").text
    marked = client.post("/clients/acme/restricted", data={"restricted": "true"}).json()
    assert marked == {"client": "acme", "restricted": True, "approved_providers": ["anthropic"]}
    assert client.get("/clients/acme").json()["restricted"] is True
    page = client.get("/files").text
    assert "Its runs go to anthropic and no other provider." in page and "Lift the restriction" in page
    assert "Approved for confidential work" in client.get("/cost").text

    assert client.post("/clients/acme/restricted", json={"restricted": False}).json()["restricted"] is False
    assert client.post("/clients/acme/restricted", data={"restricted": "maybe"}).status_code == 400
    assert client.post("/clients/nobody/restricted", json={"restricted": True}).status_code == 404

    with Database(settings.database_url).engine.connect() as cx:
        audits = [dict(r)["detail"] for r in cx.execute(
            select(audit_log).where(audit_log.c.action == "client_restricted_set")
        ).mappings()]
    assert [a["restricted"] for a in audits] == [True, False]


def test_page_says_so_when_no_provider_is_approved_and_deleting_a_client_clears_its_flag(settings):
    client = TestClient(create_app(settings, gateway=RecordedGateway({})))
    client.post("/clients", json={"name": "acme"})
    client.post("/clients/acme/restricted", json={"restricted": True})
    assert "You have approved no provider for confidential work" in client.get("/files").text

    client.post("/clients/acme/delete", data={"confirm": "acme"})
    client.post("/clients", json={"name": "acme"})  # a new client of the same name starts unrestricted
    assert client.get("/clients/acme").json()["restricted"] is False
