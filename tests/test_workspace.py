"""The owner's workspace: tasks across workstreams, decisions with their reasons, memory, the watchlist, Core."""

from __future__ import annotations

from fastapi.testclient import TestClient

from personal_agent.api.main import create_app
from personal_agent.config import CoreSettings, Workstream
from personal_agent.harness.models import RecordedGateway
from tests.test_brief_evaluation import ASSESSMENT, BRIEF, RECORD, SCORES, VERDICTS_OK

ANSWER = {"answer": "Lead with the savings rate.", "caveats": []}


class Listening(RecordedGateway):
    """A recorded gateway that keeps the context each step was given."""

    def __init__(self, responses):
        super().__init__(responses)
        self.contexts: list[str] = []

    def complete(self, contract, context, images=None, providers=None):
        self.contexts.append(context)
        return super().complete(contract, context, images, providers)


def _client(settings, gateway=None, **responses) -> TestClient:
    settings.workstreams = [Workstream(name="AltBank", colour="#059669"), Workstream(name="MBN", colour="#b45309")]
    return TestClient(create_app(settings, gateway=gateway or RecordedGateway(responses)), follow_redirects=False)


def _task(client, **more) -> str:
    data = {"title": "Approve campaign direction", "workstream": "AltBank", "status": "awaiting_approval", "risk": "high",
            "detail": "Agency has submitted 3 routes", **more}
    return client.post("/tasks", data=data).json()["task"]


def test_the_owners_workstreams_are_created_once_and_a_deleted_one_stays_deleted(settings):
    client = _client(settings)
    assert client.get("/clients").json() == ["AltBank", "MBN"]
    page = client.get("/work").text
    assert 'href="/work?ws=AltBank"' in page and 'style="background:#059669"' in page
    client.post("/clients/MBN/delete", data={"confirm": "MBN"})
    assert TestClient(create_app(settings, gateway=RecordedGateway({}))).get("/clients").json() == ["AltBank"]


def test_a_task_shows_on_today_and_on_the_board_and_filters_by_workstream_and_state(settings):
    client = _client(settings)
    assert "Nothing here yet." in client.get("/work").text and "No item here." in client.get("/today").text
    task = _task(client, status="todo", due="2030-01-02T12:00")
    _task(client, title="Review expansion case", workstream="MBN", status="done", risk="")
    today = client.get("/today").text  # open items by default
    assert "Approve campaign direction" in today and "Review expansion case" not in today
    assert "High risk" in today and "2 Jan" in today and f'data-open="e-{task}"' in today
    assert 'aria-label="1 today">1</span>' in today  # the rail counts open items
    assert "Review expansion case" in client.get("/today?f=done").text
    board = client.get("/work?ws=MBN").text
    assert "Review expansion case" in board and "Approve campaign direction" not in board
    assert client.get("/today?f=risk&ws=AltBank").text.count("Approve campaign direction") >= 1
    assert client.post("/tasks", data={"title": "x", "workstream": "Nobody"}).status_code == 400


def test_a_task_is_edited_and_removed_from_its_sheet(settings):
    client = _client(settings)
    task = _task(client, status="todo")
    moved = client.post(f"/tasks/{task}", data={"title": "Approve the route", "status": "in_progress", "risk": "low", "back": "/today"})
    assert moved.status_code == 303 and moved.headers["location"] == "/today"
    page = client.get("/work").text
    assert "Approve the route" in page and "Low risk" in page
    away = client.post(f"/tasks/{task}", data={"title": "x", "back": "//evil.example"})
    assert away.headers["location"] == "/work"  # never off this site
    client.post(f"/tasks/{task}/delete", data={})
    assert "Approve the route" not in client.get("/work").text
    assert client.post(f"/tasks/{task}", data={"title": "x"}).status_code == 404


def test_a_decision_on_a_task_is_made_in_review_and_kept_with_its_reason(settings):
    client = _client(settings)
    task = _task(client)
    review = client.get("/queue").text
    assert "1 decision pending" in review and f'<a href="#d-t{task}" data-open="d-t{task}">' in review
    assert "Why this decision?" in review and "Agency has submitted 3 routes" in review
    assert "Approve campaign direction" not in client.get("/queue?ws=MBN").text
    assert "<b>1</b><span>Decision</span>" in client.get("/").text

    done = client.post(f"/tasks/{task}/decision", data={"decision": "changes", "reason": "Route A is strong; sharpen the offer."})
    assert done.status_code == 303 and done.headers["location"].startswith("/queue?done=")
    confirmed = client.get(done.headers["location"]).text
    assert "No item needs your decision." in confirmed and "Changes requested · recorded" in confirmed
    home = client.get("/").text  # decided today: still in view, struck through
    assert '<b class="struck">Approve campaign direction</b>' in home
    memory = client.get("/library").text
    assert "Route A is strong; sharpen the offer." in memory and "Changes requested" in memory
    assert "In progress" in client.get("/work").text  # the task went back to work
    assert client.post(f"/tasks/{task}/decision", data={"decision": "maybe"}).status_code == 400


def test_a_decision_on_an_agents_draft_goes_into_the_same_library(settings):
    client = _client(settings, extract_fields=[RECORD], score_by_rubric=[SCORES], draft_assessment=[ASSESSMENT],
                     verify_evidence=[VERDICTS_OK])
    sent = client.post("/new", data={"workflow": "brief-evaluation", "client_existing": "AltBank", "brief_text": BRIEF})
    run_id = sent.headers["location"].rsplit("/", 1)[1]
    board = client.get("/work").text
    assert f'href="/queue#d-{run_id}"' in board and ">Agent<" in board  # the run is an item of work, awaiting approval
    client.post(f"/runs/{run_id}/approval", data={"decision": "rejected", "comment": "Not this quarter."})
    memory = client.get("/library?ws=AltBank").text
    assert "Not this quarter." in memory and "Declined" in memory and "Brief assessment" in memory
    assert "Not this quarter." not in client.get("/library?ws=MBN").text


def test_workstream_knowledge_and_the_principles_reach_the_agent_for_that_workstream_alone(settings):
    settings.core = CoreSettings(principles=["Activity is not impact."], lens=["Does anyone care?"])
    gateway = Listening({"think": [ANSWER, ANSWER]})
    client = _client(settings, gateway=gateway)
    client.post("/memory", data={"workstream": "AltBank", "kind": "brand_rule", "text": "Never promise zero fees."})
    assert "Never promise zero fees." in client.get("/library").text

    client.post("/think", data={"question": "How do we open?", "client": "AltBank"})
    client.post("/think", data={"question": "How do we open?", "client": "MBN"})
    for_altbank, for_mbn = gateway.contexts
    assert "Activity is not impact." in for_altbank and "Does anyone care?" in for_altbank
    assert "(brand rule) Never promise zero fees." in for_altbank
    assert "Never promise zero fees." not in for_mbn and "Activity is not impact." in for_mbn
    assert "<untrusted_data source" not in for_altbank  # the owner's own note is instruction, not outside material

    client.post("/clients/AltBank/delete", data={"confirm": "AltBank"})
    assert "Never promise zero fees." not in client.get("/library").text  # it goes with the workstream


def test_an_answer_from_think_is_saved_as_a_draft_once(settings):
    client = _client(settings, think=[ANSWER])
    client.post("/think", data={"question": "How do we open?", "client": "AltBank"})
    run_id = client.get("/runs").json()[0]["id"]
    assert f'action="/think/{run_id}/save"' in client.get("/think").text
    for _ in range(2):
        assert client.post(f"/think/{run_id}/save").status_code == 303
    assert client.get("/library").text.count("Lead with the savings rate.") == 1
    assert client.post("/think/nope/save").status_code == 404


def test_the_watchlist_holds_only_what_the_owner_adds(settings):
    client = _client(settings)
    empty = client.get("/intelligence").text
    assert "No signal on the watchlist yet." in empty and "Live monitoring is not connected" in empty
    client.post("/signals", data={"workstream": "AltBank", "kind": "regulation", "title": "CBN circular on fees",
                                  "note": "May touch the launch offer.", "source": "cbn.gov.ng"})
    assert "CBN circular on fees" in client.get("/intelligence?kind=regulation").text
    assert "CBN circular on fees" not in client.get("/intelligence?kind=reputation").text
    assert "CBN circular on fees" not in client.get("/intelligence?ws=MBN").text
    home = client.get("/").text
    assert "CBN circular on fees" in home and "<b>0</b><span>Reputation signals</span>" in home
    assert client.post("/signals", data={"workstream": "AltBank", "kind": "rumour", "title": "x"}).status_code == 400


def test_core_shows_the_dimensions_and_the_principles(settings):
    settings.core = CoreSettings(dimensions=[{"name": "Builder", "line": "Turn promising ideas into viable businesses."}],
                                 principles=["Good work should survive scrutiny."])
    page = _client(settings).get("/core").text
    assert "Your judgement, at the centre." in page and "Builder" in page
    assert '<span class="num c-purple">01</span><b>Good work should survive scrutiny.</b>' in page


def test_think_offers_starters_the_lens_and_the_fifteen_workflows(settings):
    settings.core = CoreSettings(lens=["Is the thinking sound?"])
    client = _client(settings)
    page = client.get("/think").text
    assert "Let's think this through." in page and "Is the thinking sound?" in page
    assert page.count("Agent workflow") == 10 and page.count("Starts a question here") == 5
    assert ">Challenge this campaign idea.</textarea>" in client.get("/think?start=Challenge%20this%20campaign%20idea.").text
