"""Agency dispatch: approval, the hold window, release, recall, the halt notice and re-dispatch."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from personal_agent.api.main import create_app
from personal_agent.config import Workstream
from personal_agent.engine import dispatch
from personal_agent.gateway.tools import ApprovalRequired
from personal_agent.graph_mail import GraphMail
from personal_agent.harness.models import RecordedGateway

BRIEF = "Route A is approved. Produce the launch film and the outdoor set by 1 November."
LATER = datetime.now(timezone.utc) + timedelta(hours=1)


class FakeMail:
    """Stands in for the mailbox and remembers what it was asked to send."""

    mailbox = calendar = "owner@company.example"

    def __init__(self):
        self.sent: list[dict] = []

    def send_reply(self, to, subject, body, in_reply_to=None, attachments=None):
        self.sent.append({"to": to, "subject": subject, "body": body, "attachments": attachments or []})
        return {"sent": True, "via": "test", "to": to}


def _app(settings, mail=None):
    settings.workstreams = [Workstream(name="AltBank"), Workstream(name="MBN")]
    app = create_app(settings, gateway=RecordedGateway({}), mail=mail)
    return app, TestClient(app, follow_redirects=False)


def _prepare(client, **more):
    data = {"workstream": "AltBank", "agency": "Pulse Agency", "to": "desk@pulse.example", "subject": "AltBank launch brief",
            "body": BRIEF, "directive": "Route A.", "hold_minutes": "15", **more}
    sent = client.post("/dispatch", data=data, files=[("files", ("tone.txt", b"Warm, plain, direct."))])
    assert sent.status_code == 303 and sent.headers["location"].startswith("/queue#d-"), sent.headers.get("location")
    return sent.headers["location"].split("#d-")[1]


def _approve(client, run_id):
    done = client.post(f"/runs/{run_id}/approval", data={"decision": "approved", "comment": "Go."}, headers={"accept": "text/html"})
    assert done.status_code == 303 and done.headers["location"].startswith("/dispatch/"), done.headers.get("location")
    return done.headers["location"].rsplit("/", 1)[1]


def test_a_brief_waits_for_approval_then_is_held_and_nothing_leaves(settings):
    mail = FakeMail()
    app, client = _app(settings, mail)
    run_id = _prepare(client)
    review = client.get("/queue").text
    assert "Approve and dispatch to the agency" in review and "To: desk@pulse.example" in review
    assert "Direction: Route A." in review and "Attached: tone.txt." in review and "held for 15 minutes" in review
    engine = app.state.engine
    with pytest.raises(ApprovalRequired):  # the gateway refuses the send before the owner approves
        engine.harness.tools.call(run_id, "dispatch", [dispatch.TOOL], dispatch.TOOL, engine.pending_approval(run_id)["content"])

    log = client.get(f"/dispatch/{_approve(client, run_id)}").text
    assert "Held: you can recall it" in log and "Emergency recall (15 min remaining)" in log
    assert "Pulse Agency" in log and "tone.txt" in log and 'http-equiv="refresh"' in log
    assert mail.sent == []  # opening screens during the window sends nothing
    assert "Go." in client.get("/library").text  # the reason is in the decision library


def test_the_brief_goes_once_when_the_window_ends_with_its_files(settings):
    mail = FakeMail()
    app, client = _app(settings, mail)
    dispatch_id = _approve(client, _prepare(client))
    engine = app.state.engine
    assert dispatch.release_due(engine) == []  # not yet due
    released = dispatch.release_due(engine, now=LATER)
    assert [d["status"] for d in released] == ["sent"]
    assert mail.sent == [{"to": "desk@pulse.example", "subject": "AltBank launch brief",
                          "body": "Direction: Route A.\n\n" + BRIEF, "attachments": [("tone.txt", b"Warm, plain, direct.")]}]
    assert dispatch.release_due(engine, now=LATER) == [] and len(mail.sent) == 1
    log = client.get(f"/dispatch/{dispatch_id}").text
    assert ">Sent<" in log and "Recall: prepare a halt notice" in log and "http-equiv" not in log
    receipt = client.get(f"/dispatch/{dispatch_id}/receipt")
    assert "attachment" in receipt.headers["content-disposition"] and "Sent to       desk@pulse.example" in receipt.text
    assert f'href="/dispatch/{dispatch_id}"' in client.get("/work").text  # on the board, it opens its log


def test_a_recall_inside_the_window_stops_it_and_a_revision_replaces_it(settings):
    mail = FakeMail()
    app, client = _app(settings, mail)
    dispatch_id = _approve(client, _prepare(client))
    back = client.post(f"/dispatch/{dispatch_id}/recall")
    assert "Nothing was sent" in client.get(back.headers["location"]).text
    assert dispatch.release_due(app.state.engine, now=LATER) == [] and mail.sent == []
    log = client.get(f"/dispatch/{dispatch_id}").text
    assert "Recalled before sending" in log and f'href="/dispatch?revise={dispatch_id}"' in log

    form = client.get(f"/dispatch?revise={dispatch_id}").text
    assert 'value="Revised: AltBank launch brief"' in form and "This replaces the brief sent on" in form
    assert 'value="tone.txt" checked' in form
    second = _approve(client, _prepare(client, subject="Revised: AltBank launch brief", revision_of=dispatch_id, hold_minutes="0"))
    client.get("/work")  # with no window, opening a screen releases it
    assert [m["subject"] for m in mail.sent] == ["Revised: AltBank launch brief"]
    assert "re-dispatch log" in client.get(f"/dispatch/{second}").text
    assert "Replaced by" in client.get(f"/dispatch/{dispatch_id}").text


def test_after_it_has_gone_a_recall_is_a_halt_notice_the_owner_approves(settings):
    mail = FakeMail()
    app, client = _app(settings, mail)
    dispatch_id = _approve(client, _prepare(client))
    engine = app.state.engine
    dispatch.release_due(engine, now=LATER)

    to_review = client.post(f"/dispatch/{dispatch_id}/recall")
    halt_run = to_review.headers["location"].split("#d-")[1]
    review = client.get("/queue").text
    assert "Approve and send this halt notice" in review and "Please stop work on the brief" in review
    assert "Halt notice waits for your approval" in client.get(f"/dispatch/{dispatch_id}").text
    assert len(mail.sent) == 1  # the notice has not gone: it waits for approval

    _approve(client, halt_run)
    client.get("/work")
    assert [m["subject"] for m in mail.sent] == ["AltBank launch brief", "Please halt: AltBank launch brief"]
    assert "Halt notice sent" in client.get(f"/dispatch/{dispatch_id}").text
    assert "nothing to recall" in client.get(client.post(f"/dispatch/{dispatch_id}/recall").headers["location"]).text


def test_a_rejected_halt_notice_leaves_the_brief_standing(settings):
    app, client = _app(settings, FakeMail())
    dispatch_id = _approve(client, _prepare(client))
    dispatch.release_due(app.state.engine, now=LATER)
    halt_run = client.post(f"/dispatch/{dispatch_id}/recall").headers["location"].split("#d-")[1]
    client.post(f"/runs/{halt_run}/approval", data={"decision": "rejected", "comment": "It stands."})
    assert app.state.engine.db.get_dispatch(dispatch_id)["status"] == "sent"


def test_with_no_mailbox_the_approved_brief_is_kept_in_the_outbox(settings):
    app, client = _app(settings)
    assert "No mailbox is connected yet" in client.get("/dispatch").text
    dispatch_id = _approve(client, _prepare(client, hold_minutes="0"))
    log = client.get(f"/dispatch/{dispatch_id}").text
    assert "In the outbox: no mailbox connected" in log
    held = client.get("/outbox").json()
    assert held[0]["recipient"] == "desk@pulse.example" and "Files to attach: tone.txt" in held[0]["body"]


def test_a_brief_that_cannot_go_is_refused_with_the_reason(settings):
    _, client = _app(settings, FakeMail())
    cases = [
        ({"to": "not an address"}, None, "email address"),
        ({"workstream": "Nobody"}, None, "Choose the workstream"),
        ({"body": " "}, None, "needs a subject and its text"),
        ({}, ("big.bin", b"x" * (dispatch.MAX_FILE_BYTES + 1)), "over 3 MB"),
    ]
    for change, upload, reason in cases:
        data = {"workstream": "AltBank", "agency": "Pulse", "to": "desk@pulse.example", "subject": "s", "body": BRIEF, **change}
        back = client.post("/dispatch", data=data, files=[("files", upload)] if upload else None)
        assert reason in client.get(back.headers["location"]).text, change
    assert client.get("/runs").json() == [] and client.get("/dispatch/nope").status_code == 404


def test_the_mailbox_attaches_each_file_to_the_draft_before_it_sends():
    seen: list[tuple[str, str, dict]] = []

    def graph(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content and request.url.host == "graph.microsoft.com" else {}
        seen.append((request.method, request.url.path, body))
        if request.url.host != "graph.microsoft.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.url.path.endswith("/messages"):
            return httpx.Response(201, json={"id": "draft-1"})
        if request.url.path.endswith("/attachments"):
            return httpx.Response(201, json={"id": "att-1"})
        return httpx.Response(202)

    mail = GraphMail("tenant", "client", "secret", "owner@company.example", transport=httpx.MockTransport(graph))
    result = mail.send_reply("desk@pulse.example", "Brief", "Text", attachments=[("tone.txt", b"Warm.")])
    assert result["sent"] is True
    steps = [path.rsplit("/", 1)[1] for _, path, _ in seen if "graph" not in path and "/users/" in path]
    assert steps == ["messages", "attachments", "send"]  # the file is on the draft before the one call that cannot be taken back
    attached = next(body for _, path, body in seen if path.endswith("/attachments"))
    assert attached["name"] == "tone.txt" and base64.b64decode(attached["contentBytes"]) == b"Warm."
