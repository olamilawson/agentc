"""Skill runs, and the watcher's hook that takes an Outlook message as Microsoft gives it."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from personal_agent.api.main import create_app, html_to_text
from personal_agent.config import REPO_ROOT, Workstream
from personal_agent.engine import skill_run
from personal_agent.harness.models import RecordedGateway
from personal_agent.skills.loader import load_library
from tests.test_sponsorship_triage import CHECK_OK, ENQUIRY, MAIL, POLICY, REPLY, scored
from tests.test_workspace import Listening

REVIEW = {"title": "Savings launch", "body": "Verdict: proceed with changes.\n\n1. Lead with the rate.",
          "gaps": ["The plan gives no budget."]}
REVISED = {"title": "Savings launch", "body": "Verdict: proceed with changes.\n\n1. Lead with the rate.\n2. Cut the jingle.", "gaps": []}
PLAN = "Objective: 20,000 new savings accounts by March. Idea: a radio jingle and outdoor in Lagos."


def _client(settings, gateway=None, **responses) -> TestClient:
    settings.skills_dir = REPO_ROOT / "skills"  # the skill files as they ship
    settings.workstreams = [Workstream(name="AltBank")]
    return TestClient(create_app(settings, gateway=gateway or RecordedGateway(responses)), follow_redirects=False)


def _run(client, **more) -> str:
    data = {"workflow": "skill", "skill": "campaign-review", "client_existing": "AltBank",
            "request": "Review this launch plan.", "material": PLAN, **more}
    sent = client.post("/new", data=data)
    assert sent.status_code == 303 and sent.headers["location"].startswith("/inspect/"), sent.headers.get("location")
    return sent.headers["location"].rsplit("/", 1)[1]


def test_each_skill_ships_a_rubric_and_is_offered_on_the_screens(settings):
    _, library = load_library(REPO_ROOT / "skills")
    for name in skill_run.SKILLS:
        assert "## What to produce" in library[name] and "no web access" in library[name], name
    client = _client(settings)
    page = client.get("/new?skill=copy-review").text
    assert '<option value="copy-review" selected>Copy Review</option>' in page and "Run this skill" in page
    think = client.get("/think").text
    assert 'href="/new?skill=reputation-assessment#skill"' in think and think.count("Agent workflow") == 10


def test_a_skill_works_to_its_rubric_from_the_material_and_waits_for_the_owner(settings):
    gateway = Listening({"skill_produce": [REVIEW]})
    client = _client(settings, gateway=gateway)
    run_id = _run(client)
    run = client.get(f"/runs/{run_id}").json()
    assert (run["workflow"], run["client"], run["status"]) == ("campaign-review", "AltBank", "waiting_for_approval")
    context = gateway.contexts[0]
    assert "# Campaign review" in context  # its own rubric
    assert '<untrusted_data source="pasted material">' in context and "20,000 new savings accounts" in context
    review = client.get("/queue").text
    assert "Campaign Review: Savings launch" in review and "Approve and save this document" in review
    assert "Not established or assumed: The plan gives no budget." in review and "Edit, then approve" in review
    assert client.get("/clients/AltBank/files").json() == []  # nothing is saved before approval


def test_the_owner_edits_then_approves_and_the_document_is_saved_to_the_workstream(settings):
    client = _client(settings, skill_produce=[REVIEW])
    run_id = _run(client)
    mine = REVIEW["body"] + "\n2. Drop the outdoor."
    done = client.post(f"/runs/{run_id}/approval", data={"decision": "approved", "edited_body": mine})
    assert done.json()["status"] == "done"
    files = client.get("/clients/AltBank/files").json()
    assert len(files) == 1 and files[0]["path"].startswith("campaign-review-savings-launch-")
    assert client.get(f"/runs/{run_id}/edits").json()[0]["after"] == mine  # kept for the learning review
    assert client.get(f"/runs/{run_id}").json()["outputs"]["body"] == mine


def test_a_send_back_gets_one_revision_that_answers_the_comment(settings):
    gateway = Listening({"skill_produce": [REVIEW, REVISED]})
    client = _client(settings, gateway=gateway)
    run_id = _run(client)
    client.post(f"/runs/{run_id}/approval", data={"decision": "sent_back", "comment": "Say what to cut."})
    assert client.get(f"/runs/{run_id}").json()["status"] == "waiting_for_approval"
    assert "Cut the jingle." in client.get("/queue").text
    assert "Say what to cut." in gateway.contexts[1] and "Lead with the rate." in gateway.contexts[1]


def test_a_skill_reads_an_uploaded_file_and_refuses_a_request_with_nothing_to_work_from(settings):
    gateway = Listening({"skill_produce": [REVIEW]})
    client = _client(settings, gateway=gateway)
    sent = client.post("/new", data={"workflow": "skill", "skill": "copy-review", "client_existing": "AltBank",
                                     "request": "Review this copy."}, files=[("source_files", ("copy.txt", b"Zero fees. For ever."))])
    assert sent.status_code == 303 and sent.headers["location"].startswith("/inspect/")
    assert '<untrusted_data source="file: copy.txt">' in gateway.contexts[0] and "Zero fees. For ever." in gateway.contexts[0]
    for data, reason in (({"skill": "nonsense"}, "Choose a skill"), ({"material": ""}, "Give the agent material"),
                         ({"request": ""}, "Say what you want done")):
        back = client.post("/new", data={"workflow": "skill", "skill": "copy-review", "client_existing": "AltBank",
                                         "request": "x", "material": "y", **data})
        assert reason in client.get(back.headers["location"]).text, data


# --- the watcher --------------------------------------------------------------------------
OUTLOOK = {
    "id": "AAMk-graph-id-1", "subject": MAIL["subject"], "receivedDateTime": "2026-10-09T08:00:00Z",
    "from": {"emailAddress": {"name": "Ada Obi", "address": MAIL["sender"]}},
    "body": {"contentType": "html", "content": "<html><head><style>p{color:red}</style></head><body><p>Lagos Fintech Week "
             "returns in March.</p><p>We would like your bank as partner for <b>8m naira</b>.</p><script>x()</script></body></html>"},
}


def _hooked(settings) -> TestClient:
    settings.sponsorship, settings.n8n_secret = POLICY, "s3cret"
    return TestClient(create_app(settings, gateway=RecordedGateway({
        "classify": [ENQUIRY], "score_and_route": [scored(5)], "draft_reply": [REPLY], "check_reply": [CHECK_OK],
    })))


def test_an_html_mail_becomes_its_words():
    assert html_to_text(OUTLOOK["body"]["content"]) == (
        "Lagos Fintech Week returns in March.\n\nWe would like your bank as partner for 8m naira."
    )


def test_the_watcher_posts_outlooks_message_as_it_is_and_a_repeat_starts_nothing(settings):
    client = _hooked(settings)
    assert client.post("/hooks/n8n/outlook", json=OUTLOOK).status_code == 401
    assert client.post("/hooks/n8n/outlook", json=OUTLOOK, headers={"x-agent-secret": "wrong"}).status_code == 401
    ok = client.post("/hooks/n8n/outlook", json=OUTLOOK, headers={"x-agent-secret": "s3cret"})
    assert ok.status_code == 201 and ok.json()["status"] == "waiting_for_approval"
    run = client.get(f"/runs/{ok.json()['run_id']}").json()
    assert run["inputs"]["sender"] == MAIL["sender"] and run["inputs"]["message_id"] == "AAMk-graph-id-1"
    assert run["inputs"]["body"].startswith("Lagos Fintech Week returns in March.") and "<p>" not in run["inputs"]["body"]
    again = client.post("/hooks/n8n/outlook", json=OUTLOOK, headers={"x-agent-secret": "s3cret"})
    assert again.json()["duplicate"] is True and len(client.get("/runs").json()) == 1
    assert client.post("/hooks/n8n/outlook", json={"subject": "no sender"}, headers={"x-agent-secret": "s3cret"}).status_code == 422


def test_the_n8n_workflow_file_points_at_the_hook_and_carries_no_secret():
    text = (Path(REPO_ROOT) / "deploy" / "n8n-sponsorship-watcher.json").read_text(encoding="utf-8")
    flow = json.loads(text)
    trigger, post = flow["nodes"]
    assert trigger["type"] == "n8n-nodes-base.microsoftOutlookTrigger" and flow["active"] is False
    assert post["parameters"]["url"] == "https://ijeoma.pro/hooks/n8n/outlook"
    assert post["credentials"]["httpHeaderAuth"]["name"] == "Personal agent hook"
    assert "x-agent-secret" not in text and "value" not in post["parameters"]  # the secret lives in n8n's credential store
    assert flow["connections"][trigger["name"]]["main"][0][0]["node"] == post["name"]
