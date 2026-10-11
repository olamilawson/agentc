"""Cost by workflow and client for the month, and the monthly cap."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from personal_agent import costs, learning
from personal_agent.api.main import create_app
from personal_agent.config import SponsorshipPolicy
from personal_agent.db import Database, audit_log
from personal_agent.harness.models import RecordedGateway
from tests.conftest import INPUTS, VALID_DRAFT
from tests.test_sponsorship_triage import ENQUIRY, MAIL

# Each recorded model call costs $0.002, so a foundation-test run costs $0.002.
HEADERS = {"x-agent-secret": "s3cret"}


def _client(settings, cap: float | None = None, drafts: int = 6) -> TestClient:
    settings.budgets.monthly_cap_usd = cap
    settings.n8n_secret = "s3cret"
    settings.sponsorship = SponsorshipPolicy(pursue_at=4.0, decline_below=2.5, escalate_above=10_000_000)
    gateway = RecordedGateway({
        "draft": [VALID_DRAFT] * drafts,
        "classify": [{**ENQUIRY, "is_sponsorship": False, "reason": "A supplier pitch."}] * 3,
    })
    return TestClient(create_app(settings, gateway=gateway))


def _run(client: TestClient, name: str = "acme"):
    return client.post("/runs", json={"workflow": "foundation-test", "client": name, "inputs": INPUTS})


def _audits(settings, action: str) -> list[dict]:
    with Database(settings.database_url).engine.connect() as cx:
        return [dict(r) for r in cx.execute(select(audit_log).where(audit_log.c.action == action)).mappings()]


def test_month_cost_is_grouped_by_workflow_and_by_client(settings):
    client = _client(settings)
    _run(client, "acme"), _run(client, "acme"), _run(client, "beta")
    client.post("/hooks/n8n/sponsorship", json=MAIL, headers=HEADERS)

    s = client.get("/costs").json()
    assert s["month"] == costs.month_bounds()[0]
    assert (s["runs"], s["total_usd"]) == (4, 0.008)
    assert [(w["name"], w["runs"], w["cost_usd"]) for w in s["by_workflow"]] == [
        ("foundation-test", 3, 0.006), ("sponsorship-triage", 1, 0.002),
    ]
    assert {c["name"]: c["cost_usd"] for c in s["by_client"]} == {"acme": 0.004, "beta": 0.002, "sponsorship": 0.002}
    assert s["by_workflow"][0]["tokens"] == 230 * 3
    assert s["cap"] == {"cap_usd": None, "used": None, "state": "none"}

    # Another month has its own figures; a malformed one is refused.
    assert client.get("/costs", params={"month": "2020-01"}).json()["runs"] == 0
    assert client.get("/costs", params={"month": "October"}).status_code == 400


def test_cost_page_shows_the_month_the_cap_and_the_settings(settings):
    client = _client(settings)
    _run(client)
    page = client.get("/cost").text
    assert "$0.00" in page and "By workflow" in page and "foundation-test" in page
    assert "You have set no monthly cap" in page
    assert "openai/gpt-5" in page  # the route table
    assert "send_mail" in page and "owner approval for every call" in page
    assert "s3cret" not in page  # the webhook secret is never shown
    assert "No runs this month" in client.get("/cost", params={"month": "2020-01"}).text


def test_cap_alerts_once_at_eighty_percent_then_stops_runs_not_started_by_hand(settings):
    client = _client(settings, cap=0.005)
    _run(client), _run(client)  # $0.004: 80 percent
    assert client.get("/costs").json()["cap"] == {"cap_usd": 0.005, "used": 0.8, "state": "alert"}
    assert "at 80% of the $0.01 cap" in client.get("/queue").text
    assert len(_audits(settings, "monthly_cap_alert")) == 1

    _run(client)  # $0.006: over the cap
    assert client.get("/costs").json()["cap"]["state"] == "stopped"
    assert len(_audits(settings, "monthly_cap_alert")) == 1  # alerted once, not on every run

    # The watcher's run is refused and nothing is spent on it.
    refused = client.post("/hooks/n8n/sponsorship", json=MAIL, headers=HEADERS)
    assert refused.status_code == 429 and "monthly cap" in refused.json()["detail"]
    assert client.get("/costs").json()["runs"] == 3
    assert _audits(settings, "run_refused_monthly_cap")

    # A run the owner starts by hand still goes ahead.
    assert _run(client).status_code == 201
    assert "Spend has reached the $0.01 monthly cap" in client.get("/queue").text
    assert "Spend has reached the $0.01 monthly cap" in client.get("/cost").text


def test_scheduled_learning_review_stops_at_the_cap_but_the_owners_does_not(settings):
    client = _client(settings, cap=0.001)
    _run(client)
    engine_db = Database(settings.database_url)
    from personal_agent.engine.runner import Engine

    engine = Engine(settings, engine_db, gateway=RecordedGateway({}))
    with pytest.raises(costs.MonthlyCapReached):
        learning.run_review(engine)
    assert learning.run_review(engine, by_hand=True)["status"] == "done"
    assert client.post("/learning/reviews").status_code == 201


def test_month_bounds():
    assert costs.month_bounds("2026-12") == ("2026-12", "2026-12-01T00:00:00", "2027-01-01T00:00:00")
    assert costs.adjacent_months("2026-01") == ("2025-12", "2026-02")
    with pytest.raises(ValueError):
        costs.month_bounds("2026-13")
