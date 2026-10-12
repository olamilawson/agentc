"""The home screen: the owner's dashboard, built from what the agent holds and nothing invented."""

from __future__ import annotations

from fastapi.testclient import TestClient

from personal_agent.api.main import create_app
from personal_agent.harness.models import RecordedGateway
from tests.test_brief_evaluation import ASSESSMENT, BRIEF, RECORD, SCORES, VERDICTS_OK


def _client(settings, **responses) -> TestClient:
    return TestClient(create_app(settings, gateway=RecordedGateway(responses)), follow_redirects=False)


def test_an_empty_home_greets_the_owner_and_says_nothing_waits(settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    page = _client(settings).get("/").text
    assert ", Chidimma.</h1>" in page and "CHIDIMMA OS" not in page  # the name is upper-cased by the stylesheet
    assert "Nothing is waiting on you." in page and "No item needs your decision." in page
    assert "Calendar not connected" in page and "No call is scheduled." in page
    assert "No OPENAI_API_KEY on the server yet" in page
    assert "No clients yet." in page
    for href in ('href="/queue"', 'href="/new#brief"', 'href="/new#creative"', 'href="/new#draft"', 'href="/library"'):
        assert href in page
    assert "http://" not in page and "https://" not in page  # no outside host: fonts and icons ship with the app


def test_home_shows_the_waiting_item_the_client_and_the_recent_run(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    client = _client(settings, extract_fields=[RECORD], score_by_rubric=[SCORES], draft_assessment=[ASSESSMENT],
                     verify_evidence=[VERDICTS_OK])
    sent = client.post("/new", data={"workflow": "brief-evaluation", "client_new": "Acme Bank", "brief_text": BRIEF})
    run_id = sent.headers["location"].rsplit("/", 1)[1]
    page = client.get("/").text
    assert "<b>1</b><span>Decision</span>" in page
    assert "For Acme Bank. Open Review to read it and decide." in page
    assert '<span class="tag c-blue">Brief</span>' in page
    assert f'href="/inspect/{run_id}"' in page and "waiting for you" in page
    assert '<span>Acme Bank</span>' in page and ">AB</span>" in page  # the client is a workstream in the rail
    assert '<span class="count" aria-label="1 waiting">1</span>' in page
    assert "Model key set" in page and "test-key" not in page


def test_the_goal_typed_on_home_never_travels_in_the_url(settings):
    page = _client(settings).get("/").text
    assert 'id="ask-goal"' in page and 'name="goal"' not in page  # no name, so the form sends no text
    assert "sessionStorage.getItem('goal')" in _client(settings).get("/new").text
