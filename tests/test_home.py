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
    assert 'class="hero clear"' in page and "All caught up" in page
    assert "Nothing is waiting on you." in page and "No open item." in page and "No signal on the watchlist yet." in page
    assert "Calendar not connected" in page and "No call is scheduled." in page
    assert "No OPENAI_API_KEY on the server yet" in page
    assert "No clients yet." in page
    for href in ('href="/queue"', 'href="/new#brief"', 'href="/new#creative"', 'href="/new#draft"', 'href="/library"'):
        assert href in page
    assert "http://" not in page and "https://" not in page  # no outside host: fonts and icons ship with the app


def test_home_shows_the_waiting_item_and_its_workstream(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    client = _client(settings, extract_fields=[RECORD], score_by_rubric=[SCORES], draft_assessment=[ASSESSMENT],
                     verify_evidence=[VERDICTS_OK])
    sent = client.post("/new", data={"workflow": "brief-evaluation", "client_new": "Acme Bank", "brief_text": BRIEF})
    run_id = sent.headers["location"].rsplit("/", 1)[1]
    page = client.get("/").text
    assert "<b>1</b><span>Decision</span>" in page
    assert "For Acme Bank. Open it to read it and decide." in page
    assert page.count(f'href="/queue#d-{run_id}"') == 2  # My view and the priority both open its sheet
    assert '<span class="tag c-blue">Brief</span>' in page
    assert '<span>Acme Bank</span>' in page and ">AB</span>" in page  # the client is a workstream in the rail
    assert '<span class="count" aria-label="1 waiting">1</span>' in page
    assert "Model key set" in page and "test-key" not in page


def test_the_goal_typed_on_home_never_travels_in_the_url(settings):
    page = _client(settings).get("/").text
    assert 'id="ask-goal"' in page and 'name="goal"' not in page  # no name, so the form sends no text
    assert "sessionStorage.getItem('goal')" in _client(settings).get("/think").text


def test_review_lists_the_item_and_holds_its_sheet_with_the_three_decisions(settings):
    client = _client(settings, extract_fields=[RECORD], score_by_rubric=[SCORES], draft_assessment=[ASSESSMENT],
                     verify_evidence=[VERDICTS_OK])
    sent = client.post("/new", data={"workflow": "brief-evaluation", "client_new": "Acme Bank", "brief_text": BRIEF})
    run_id = sent.headers["location"].rsplit("/", 1)[1]
    page = client.get("/queue").text
    assert f'<a href="#d-{run_id}" data-open="d-{run_id}">' in page  # the list row opens the sheet
    assert f'<dialog class="sheet" id="d-{run_id}" aria-labelledby="t-{run_id}" open>' in page  # readable with no script
    assert "Acme Bank workstream" in page and "Decision 1 of 1" in page and "<strong>Context:</strong>" in page
    for value in ("sent_back", "rejected", "approved"):
        assert f'<button form="f-{run_id}" name="decision" value="{value}"' in page
    assert "showModal" in page and "Your approval covers this content" in page


def test_a_decision_from_the_sheet_returns_the_browser_to_review(settings):
    client = _client(settings, extract_fields=[RECORD], score_by_rubric=[SCORES], draft_assessment=[ASSESSMENT],
                     verify_evidence=[VERDICTS_OK])
    sent = client.post("/new", data={"workflow": "brief-evaluation", "client_new": "Acme Bank", "brief_text": BRIEF})
    run_id = sent.headers["location"].rsplit("/", 1)[1]
    back = client.post(f"/runs/{run_id}/approval", data={"decision": "rejected", "comment": "Not this quarter."},
                       headers={"accept": "text/html"})
    assert back.status_code == 303 and back.headers["location"].startswith("/queue?done=")
    confirmed = client.get(back.headers["location"]).text
    assert "Declined · recorded" in confirmed and "Kept in Memory with your reason." in confirmed
    assert client.get(f"/runs/{run_id}").json()["status"] != "waiting_for_approval"
    assert "No item needs your decision." in client.get("/queue").text
