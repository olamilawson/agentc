"""The New work screen: starting a brief evaluation, a creative review or a draft from the site."""

from __future__ import annotations

from fastapi.testclient import TestClient

from personal_agent.api.main import create_app
from personal_agent.db import Database
from personal_agent.evaluation import text_pdf
from personal_agent.harness.models import RecordedGateway
from tests.test_brief_evaluation import ASSESSMENT, BRIEF, RECORD, SCORES, VERDICTS_OK
from tests.test_creative_review import feedback, ratings
from tests.test_strategy_drafting import GOAL, OUTLINE


def _client(settings, **responses) -> TestClient:
    return TestClient(create_app(settings, gateway=RecordedGateway(responses)), follow_redirects=False)


def _brief_gateway() -> dict:
    return {"extract_fields": [RECORD], "score_by_rubric": [SCORES], "draft_assessment": [ASSESSMENT],
            "verify_evidence": [VERDICTS_OK]}


def _run_id(response) -> str:
    assert response.status_code == 303 and response.headers["location"].startswith("/inspect/"), response.text
    return response.headers["location"].rsplit("/", 1)[1]


def test_screen_offers_the_three_kinds_of_work_and_lists_clients(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    client = _client(settings)
    client.post("/clients", json={"name": "acme"})
    page = client.get("/new").text
    for label in ("Evaluate this brief", "Review this work", "Start this draft"):
        assert label in page
    assert '<option value="acme">acme</option>' in page
    assert "The server has no" not in page  # the key the routes need is set
    assert 'href="/new"' in client.get("/queue").text  # in the rail, and in the empty queue's message


def test_screen_says_so_when_the_model_key_is_missing(settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    page = _client(settings).get("/new").text
    assert "The server has no OPENAI_API_KEY yet" in page
    assert "test-key" not in page


def test_a_pasted_brief_for_a_new_client_becomes_a_run_and_reaches_the_queue(settings):
    client = _client(settings, **_brief_gateway())
    sent = client.post("/new", data={"workflow": "brief-evaluation", "client_new": "Acme Bank", "brief_text": BRIEF})
    run = client.get(f"/runs/{_run_id(sent)}").json()
    assert (run["workflow"], run["client"], run["status"]) == ("brief-evaluation", "Acme Bank", "waiting_for_approval")
    assert client.get("/clients").json() == ["Acme Bank"]  # the new client's folder exists
    assert len(client.get("/approvals/pending").json()) == 1
    page = client.get(f"/inspect/{run['id']}").text
    assert 'waiting for you in the <a href="/queue">Approval queue</a>' in page and "http-equiv" not in page


def test_an_uploaded_brief_is_saved_to_the_clients_folder_and_evaluated_from_there(settings):
    client = _client(settings, **_brief_gateway())
    client.post("/clients", json={"name": "acme"})
    sent = client.post(
        "/new", data={"workflow": "brief-evaluation", "client_existing": "acme"},
        files={"brief_file": ("launch brief.md", BRIEF.encode())},
    )
    run = client.get(f"/runs/{_run_id(sent)}").json()
    assert run["inputs"] == {"brief_path": "launch brief.md"} and run["status"] == "waiting_for_approval"
    assert [f["path"] for f in client.get("/clients/acme/files").json()] == ["launch brief.md"]


def test_creative_work_and_its_brief_start_a_review(settings):
    client = _client(settings, score_by_brief=[ratings()], write_feedback=[feedback()])
    sent = client.post(
        "/new",
        data={"workflow": "creative-review", "client_new": "acme", "brief_text": BRIEF,
              "discipline": "copywriter", "work_kind": "outdoor"},
        files=[("work_files", ("poster.pdf", text_pdf(["Open your business account in ten minutes.", "Bring your CAC number."]))),],
    )
    run = client.get(f"/runs/{_run_id(sent)}").json()
    assert run["status"] == "waiting_for_approval"
    assert run["inputs"]["work_paths"] == ["poster.pdf"] and run["inputs"]["discipline"] == "copywriter"


def test_a_goal_starts_a_draft_which_may_come_back_with_questions(settings):
    client = _client(settings, questions=[{"questions": ["What is the launch date?"]}], outline=[OUTLINE])
    sent = client.post("/new", data={
        "workflow": "strategy-drafting", "client_new": "acme", "goal": GOAL, "document_type": "memo", "audience": "the MD",
    })
    run = client.get(f"/runs/{_run_id(sent)}").json()
    assert run["status"] == "waiting_for_input"
    assert run["inputs"]["document_type"] == "memo" and run["inputs"]["audience"] == "the MD"
    assert "Questions before drafting" in client.get("/queue").text


def test_a_request_missing_something_goes_back_with_the_reason_and_starts_nothing(settings):
    client = _client(settings)
    cases = [
        ({"workflow": "brief-evaluation", "brief_text": BRIEF}, "Choose a client"),
        ({"workflow": "brief-evaluation", "client_new": "acme"}, "Add the brief"),
        ({"workflow": "creative-review", "client_new": "acme", "brief_text": BRIEF}, "Add the creative work"),
        ({"workflow": "strategy-drafting", "client_new": "acme"}, "Say what the document is for"),
        ({"workflow": "something-else", "client_new": "acme"}, "not one the agent does"),
        ({"workflow": "brief-evaluation", "client_new": "../escape", "brief_text": BRIEF}, "may not contain"),
    ]
    for data, reason in cases:
        back = client.post("/new", data=data)
        assert back.status_code == 303 and back.headers["location"].startswith("/new?notice="), data
        assert reason in client.get(back.headers["location"]).text, data
    assert client.get("/runs").json() == []


def test_a_run_still_being_worked_on_has_a_page_that_updates_itself(settings):
    client = _client(settings)
    db = Database(settings.database_url)
    db.create_run("run-working", "brief-evaluation", "acme", {"brief_text": BRIEF}, "v0")  # recorded, not yet worked
    page = client.get("/inspect/run-working").text
    assert '<meta http-equiv="refresh" content="4"/>' in page and "The agent is working on this" in page
