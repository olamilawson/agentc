"""Microsoft Graph mail, against a stand-in for Microsoft's servers (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from personal_agent.config import SponsorshipPolicy
from personal_agent.db import Database
from personal_agent.engine.runner import Engine
from personal_agent.gateway.tools import UncertainOutcome
from personal_agent.graph_mail import GraphError, GraphMail, from_settings
from personal_agent.harness.models import RecordedGateway
from tests.test_sponsorship_triage import CHECK_OK, ENQUIRY, MAIL, REPLY, scored

MAILBOX = "partnerships@company.example"


class FakeGraph:
    """Answers the handful of calls the agent makes, and remembers them."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.bodies: dict[str, dict] = {}
        self.sent: list[str] = []
        self.deleted: list[str] = []
        self.tokens_issued = 0
        self.original_exists = True
        self.fail: dict[str, object] = {}  # step name -> status code, or an exception to raise
        self.inbox = [
            {"id": "msg-001", "subject": MAIL["subject"], "receivedDateTime": "2026-10-09T08:00:00Z",
             "from": {"emailAddress": {"address": MAIL["sender"]}}, "bodyPreview": "the enquiry itself"},
            {"id": "old-1", "subject": "Last year's summit", "receivedDateTime": "2025-11-02T10:00:00Z",
             "from": {"emailAddress": {"address": MAIL["sender"]}}, "bodyPreview": "Thank you for sponsoring us in 2025."},
        ]

    def _fail(self, step: str):
        outcome = self.fail.get(step)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if request.url.host == "login.microsoftonline.com":
            if self._fail("token"):
                return httpx.Response(401, json={"error": "invalid_client", "error_description": "bad secret"})
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"token-{self.tokens_issued}", "expires_in": 3600})
        assert request.headers["authorization"].startswith("Bearer token-")
        assert path.startswith(f"/v1.0/users/{MAILBOX}/messages")
        if method == "POST" and path.endswith("/createReply"):
            if not self.original_exists:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": "gone"}})
            return httpx.Response(201, json={"id": "draft-1"})
        if method == "PATCH":
            if code := self._fail("patch"):
                return httpx.Response(code, json={"error": {"code": "ErrorAccessDenied", "message": "no access"}})
            self.bodies[path.rsplit("/", 1)[1]] = json.loads(request.content)
            return httpx.Response(200, json={})
        if method == "POST" and path.endswith("/send"):
            if code := self._fail("send"):
                return httpx.Response(code, json={"error": {"code": "ErrorSendAsDenied", "message": "not allowed"}})
            self.sent.append(path.split("/")[-2])
            return httpx.Response(202)
        if method == "POST" and path.endswith("/messages"):
            self.bodies["draft-new"] = json.loads(request.content)
            return httpx.Response(201, json={"id": "draft-new"})
        if method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if method == "GET":
            assert request.url.params["$search"] == f'"participants:{MAIL["sender"]}"'
            return httpx.Response(200, json={"value": self.inbox})
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


def _mail(graph: FakeGraph) -> GraphMail:
    return GraphMail("tenant", "client", "secret", MAILBOX, transport=httpx.MockTransport(graph))


def _send(mail: GraphMail, in_reply_to: str | None = "msg-001") -> dict:
    return mail.send_reply("ada@lagosfintechweek.example", "Re: Partnership", "Could you share two dates?", in_reply_to)


# --- the connection itself -----------------------------------------------------------
def test_reply_is_sent_in_the_thread_with_exactly_the_approved_recipient_subject_and_body(graph):
    result = _send(_mail(graph))
    assert result == {
        "sent": True, "via": "Microsoft Graph", "mailbox": MAILBOX,
        "to": "ada@lagosfintechweek.example", "threaded": True,
    }
    assert graph.sent == ["draft-1"]
    draft = graph.bodies["draft-1"]
    assert draft["subject"] == "Re: Partnership"
    assert draft["body"] == {"contentType": "Text", "content": "Could you share two dates?"}
    # The recipient is set outright and copies are cleared: a Reply-To on the original cannot redirect it.
    assert draft["toRecipients"] == [{"emailAddress": {"address": "ada@lagosfintechweek.example"}}]
    assert draft["ccRecipients"] == [] and draft["bccRecipients"] == []


def test_token_is_fetched_once_and_reused(graph):
    mail = _mail(graph)
    _send(mail), _send(mail)
    assert graph.tokens_issued == 1


def test_a_missing_original_sends_a_new_message_and_says_so(graph):
    graph.original_exists = False
    result = _send(_mail(graph))
    assert result["sent"] and result["threaded"] is False
    assert graph.sent == ["draft-new"] and graph.bodies["draft-new"]["subject"] == "Re: Partnership"


def test_a_failure_before_the_send_is_definite_and_leaves_no_draft(graph):
    graph.fail["patch"] = 403
    with pytest.raises(GraphError, match="ErrorAccessDenied"):
        _send(_mail(graph))
    assert graph.sent == [] and graph.deleted == ["draft-1"]


def test_a_refused_send_is_definite(graph):
    graph.fail["send"] = 403
    with pytest.raises(GraphError, match="refused to send"):
        _send(_mail(graph))
    assert graph.sent == [] and graph.deleted == ["draft-1"]


@pytest.mark.parametrize("failure", [503, httpx.ReadTimeout("timed out")])
def test_a_server_error_or_dropped_connection_on_the_send_is_uncertain(graph, failure):
    graph.fail["send"] = failure
    with pytest.raises(UncertainOutcome, match="may or may not have gone"):
        _send(_mail(graph))
    assert graph.deleted == []  # the draft is left alone: it may be on its way


def test_a_bad_application_secret_is_a_definite_failure_that_names_no_secret(graph):
    graph.fail["token"] = True
    with pytest.raises(GraphError) as e:
        _send(_mail(graph))
    assert "refused the application's sign-in" in str(e.value) and "secret" in str(e.value)
    assert "tenant" not in str(e.value).replace("bad secret", "")


def test_search_returns_recent_mail_with_an_address(graph):
    found = _mail(graph).search_correspondence(MAIL["sender"])
    assert [m["id"] for m in found] == ["msg-001", "old-1"]
    assert found[1] == {
        "id": "old-1", "received_at": "2025-11-02T10:00:00Z", "from": MAIL["sender"],
        "subject": "Last year's summit", "preview": "Thank you for sponsoring us in 2025.",
    }


def test_connection_needs_all_four_settings(settings):
    assert from_settings(settings) is None
    settings.graph_tenant_id, settings.graph_client_id, settings.graph_client_secret = "t", "c", "s"
    assert from_settings(settings) is None  # no mailbox named
    settings.graph_mailbox = MAILBOX
    assert from_settings(settings).mailbox == MAILBOX


# --- in the triage graph ---------------------------------------------------------------
def _engine(settings, graph: FakeGraph, drafts: int = 1) -> Engine:
    settings.sponsorship = SponsorshipPolicy(pursue_at=4.0, decline_below=2.5, escalate_above=10_000_000)
    gateway = RecordedGateway({
        "classify": [ENQUIRY], "score_and_route": [scored(5)],
        "draft_reply": [REPLY] * drafts, "check_reply": [CHECK_OK] * drafts,
    })
    return Engine(settings, Database(settings.database_url), gateway=gateway, mail=_mail(graph))


def test_approved_reply_goes_out_through_the_mailbox_and_the_ledger_says_sent(settings, graph):
    engine = _engine(settings, graph)
    run_id = engine.start_run("sponsorship-triage", "sponsorship", MAIL)
    assert graph.sent == []  # nothing leaves before approval
    done = engine.approve(run_id, "approved", None)
    assert done["status"] == "done" and done["outputs"]["sent"]["sent"] is True
    assert graph.sent == ["draft-1"] and graph.bodies["draft-1"]["body"]["content"] == REPLY["body"]
    assert graph.bodies["draft-1"]["toRecipients"][0]["emailAddress"]["address"] == MAIL["sender"]
    assert engine.db.list_ledger()[0]["outcome"] == "reply sent"
    assert engine.db.list_outbox() == []  # the local outbox is for when there is no connection


def test_earlier_mail_with_the_sender_reaches_scoring_as_data(settings, graph):
    engine = _engine(settings, graph)
    run = engine.db.get_run(engine.start_run("sponsorship-triage", "sponsorship", MAIL))
    # The enquiry itself is left out; the older message is kept.
    assert [m["subject"] for m in run["outputs"]["correspondence"]] == ["Last year's summit"]
    assert any("Earlier mail with this sender in the mailbox: 1" in n for n in engine.queue()[0]["notes"])


def test_a_definite_send_failure_returns_the_reply_to_the_queue_and_a_second_approval_retries(settings, graph):
    engine = _engine(settings, graph)
    run_id = engine.start_run("sponsorship-triage", "sponsorship", MAIL)
    graph.fail["send"] = 403
    held = engine.approve(run_id, "approved", None)
    assert held["status"] == "waiting_for_approval" and graph.sent == []
    assert "The reply was not sent" in engine.queue()[0]["error"]
    assert "nothing sent" in engine.db.list_ledger()[0]["outcome"]

    graph.fail.clear()
    assert engine.approve(run_id, "approved", None)["status"] == "done"
    assert len(graph.sent) == 1


def test_an_uncertain_send_stops_the_run_and_is_never_retried(settings, graph):
    engine = _engine(settings, graph)
    run_id = engine.start_run("sponsorship-triage", "sponsorship", MAIL)
    graph.fail["send"] = httpx.ReadTimeout("timed out")
    stopped = engine.approve(run_id, "approved", None)
    assert stopped["status"] == "failed" and "may or may not have gone" in stopped["error_reason"]
    assert "send outcome unknown" in engine.db.list_ledger()[0]["outcome"]
    assert sum(path.endswith("/send") for _, path in graph.calls) == 1  # one attempt, no retry
    with pytest.raises(ValueError):
        engine.approve(run_id, "approved", None)  # a stopped run cannot be approved into a second send


def test_an_unreadable_mailbox_is_flagged_and_triage_goes_on(settings, graph):
    engine = _engine(settings, graph)
    graph.inbox = None  # makes the stand-in's answer unusable

    def broken(request):
        if request.method == "GET":
            return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "no read access"}})
        return graph(request)

    engine.harness.mail = GraphMail("tenant", "client", "secret", MAILBOX, transport=httpx.MockTransport(broken))
    run = engine.db.get_run(engine.start_run("sponsorship-triage", "sponsorship", MAIL))
    assert run["status"] == "waiting_for_approval" and run["outputs"]["correspondence"] == []
    assert any("earlier correspondence could not be read" in flag for flag in run["flags"])
