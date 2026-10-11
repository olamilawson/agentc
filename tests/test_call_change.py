"""The owner moves or cancels a scheduled call from the Calls screen."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from personal_agent.api.main import create_app
from personal_agent.db import Database
from personal_agent.engine.runner import Engine
from personal_agent.harness.models import RecordedGateway
from tests.test_graph_mail import MAIL, FakeGraph, _mail, _scheduled, graph  # noqa: F401  (graph is a fixture)

LAGOS = ZoneInfo("Africa/Lagos")


def _new_time(call: dict, days: int = 7, hour: int = 14) -> tuple[str, datetime]:
    """A local date and time some days after the call, as the Calls screen sends it, and the same moment in UTC."""
    day = datetime.fromisoformat(call["start"]).astimezone(LAGOS).date() + timedelta(days=days)
    local = datetime(day.year, day.month, day.day, hour, 0, tzinfo=LAGOS)
    return local.strftime("%Y-%m-%dT%H:%M"), local.astimezone(timezone.utc)


def _change(engine: Engine, **inputs) -> dict:
    return engine.db.get_run(engine.start_run("call-change", "sponsorship", inputs))


def _item(engine: Engine, run: dict) -> dict:
    return next(i for i in engine.queue() if i["run_id"] == run["id"])


def test_owner_cancels_a_call_with_their_own_message_after_approving_it(settings, graph):
    engine, call = _scheduled(settings, graph)
    run = _change(engine, action="cancel", call_id=call["id"], message="I am travelling that week. Apologies.")
    assert run["status"] == "waiting_for_approval" and run["tokens_in"] == 0  # no model is involved
    assert graph.cancelled == []  # asking changes nothing

    item = _item(engine, run)
    assert item["action"] == "cancel"
    assert "Message with the cancellation: I am travelling that week. Apologies." in item["content"]["body"]
    assert any("You asked for this change" in note for note in item["notes"])

    assert engine.approve(run["id"], "approved", None)["status"] == "done"
    assert graph.cancelled == [{"event": "evt-1", "comment": "I am travelling that week. Apologies."}]
    assert engine.db.get_call(call["id"])["status"] == "cancelled"


def test_owner_moves_a_call_to_a_time_they_choose(settings, graph):
    engine, call = _scheduled(settings, graph)
    local, utc = _new_time(call)
    run = _change(engine, action="move", call_id=call["id"], start=local)
    assert run["status"] == "waiting_for_approval"
    item = _item(engine, run)
    assert item["action"] == "move"
    assert f"From: {call['label']}" in item["content"]["body"]
    assert "14:00 to 14:30 Lagos time (UTC+1)" in item["content"]["body"]  # the owner's clock, not UTC
    assert graph.moved == []

    assert engine.approve(run["id"], "approved", None)["status"] == "done"
    (moved,) = graph.moved
    assert moved["event"] == "evt-1" and moved["start"]["dateTime"] == utc.strftime("%Y-%m-%dT%H:%M:%S")
    updated = engine.db.get_call(call["id"])
    assert updated["start"] == utc.isoformat(timespec="minutes") and updated["status"] == "scheduled"
    # A later mail from the sender is tied to the call at its new time.
    assert engine.db.scheduled_call_for(MAIL["sender"], "2000")["label"] == updated["label"]


def test_a_request_that_cannot_stand_fails_with_its_reason_and_reaches_no_queue(settings, graph):
    engine, call = _scheduled(settings, graph)
    local, utc = _new_time(call)
    graph.busy = [(utc.strftime("%Y-%m-%dT%H:%M:%S"), (utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"))]
    busy = _change(engine, action="move", call_id=call["id"], start=local)
    assert busy["status"] == "failed" and "is not free in your calendar" in busy["error_reason"]

    past = _change(engine, action="move", call_id=call["id"], start="2020-01-01T10:00")
    assert past["status"] == "failed" and "already passed" in past["error_reason"]
    assert "needs the new date and time" in _change(engine, action="move", call_id=call["id"])["error_reason"]
    assert "could not be read" in _change(engine, action="move", call_id=call["id"], start="next Tuesday")["error_reason"]
    assert "not scheduled" in _change(engine, action="cancel", call_id="no-such-call")["error_reason"]

    engine.db.update_call(call["id"], status="cancelled")
    assert "not scheduled" in _change(engine, action="cancel", call_id=call["id"])["error_reason"]
    assert graph.moved == [] and graph.cancelled == [] and engine.queue() == []


def test_a_time_taken_between_the_request_and_the_approval_blocks_the_move(settings, graph):
    engine, call = _scheduled(settings, graph)
    local, utc = _new_time(call)
    run = _change(engine, action="move", call_id=call["id"], start=local)
    graph.busy = [(utc.strftime("%Y-%m-%dT%H:%M:%S"), (utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"))]
    held = engine.approve(run["id"], "approved", None)
    assert held["status"] == "waiting_for_approval" and graph.moved == []
    assert "no longer free" in _item(engine, run)["error"]

    graph.busy = []
    assert engine.approve(run["id"], "approved", None)["status"] == "done" and len(graph.moved) == 1


def test_rejecting_the_item_changes_nothing(settings, graph):
    engine, call = _scheduled(settings, graph)
    run = _change(engine, action="cancel", call_id=call["id"])
    assert "Message with the cancellation: This call is cancelled." in _item(engine, run)["content"]["body"]
    assert engine.approve(run["id"], "rejected", "changed my mind")["status"] == "cancelled"
    assert graph.cancelled == [] and engine.db.get_call(call["id"])["status"] == "scheduled"


def test_a_refusal_from_microsoft_returns_the_item_and_a_second_approval_retries(settings, graph):
    engine, call = _scheduled(settings, graph)
    run = _change(engine, action="cancel", call_id=call["id"])
    graph.fail["change"] = 404
    assert engine.approve(run["id"], "approved", None)["status"] == "waiting_for_approval"
    assert "no longer in the calendar" in _item(engine, run)["error"]
    assert engine.db.get_call(call["id"])["status"] == "scheduled"
    graph.fail.clear()
    assert engine.approve(run["id"], "approved", None)["status"] == "done"


# --- the Calls screen ---------------------------------------------------------------------
def _client(settings, graph: FakeGraph | None) -> tuple[TestClient, dict]:
    app = create_app(settings, gateway=RecordedGateway({}), mail=_mail(graph) if graph else None)
    db = Database(settings.database_url)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=5)
    call_id = db.create_call(
        "run-0", "evt-1", "ada@lagosfintechweek.example", "Call: Lagos Fintech Week",
        start.isoformat(timespec="minutes"), (start + timedelta(minutes=30)).isoformat(timespec="minutes"),
        "Friday 16 October 2026, 10:00 to 10:30 Lagos time (UTC+1)",
    )
    return TestClient(app, follow_redirects=False), db.get_call(call_id)


def test_calls_screen_lists_calls_and_sends_a_change_to_the_queue(settings, graph):
    client, call = _client(settings, graph)
    page = client.get("/calendar").text
    assert "Call: Lagos Fintech Week" in page and "Review this move" in page and "Review this cancellation" in page
    assert 'href="/calendar"' in client.get("/queue").text  # the screen is in the rail

    sent = client.post(f"/calls/{call['id']}/cancel", data={"message": "Sorry, I have to cancel."})
    assert sent.status_code == 303 and sent.headers["location"] == "/queue"
    queue = client.get("/queue").text
    assert "Approve and cancel this call" in queue and "Sorry, I have to cancel." in queue
    assert graph.cancelled == []  # the screen alone changes nothing

    as_json = client.post(f"/calls/{call['id']}/move", json={"start": _new_time(call)[0]})
    assert as_json.json()["status"] == "waiting_for_approval"
    assert client.post("/calls/no-such-call/cancel", json={}).status_code == 404


def test_calls_screen_shows_the_reason_when_a_request_cannot_stand(settings, graph):
    client, call = _client(settings, graph)
    back = client.post(f"/calls/{call['id']}/move", data={"start": "2020-01-01T10:00"})
    assert back.status_code == 303 and back.headers["location"].startswith("/calendar?notice=")
    assert "the new time has already passed" in client.get(back.headers["location"]).text
    assert client.post(f"/calls/{call['id']}/move", json={"start": "2020-01-01T10:00"}).status_code == 409


def test_calls_screen_without_a_calendar_connection_offers_no_change(settings):
    client, call = _client(settings, None)
    page = client.get("/calendar").text
    assert "No calendar connection yet" in page and "Review this move" not in page
    assert "no calendar connection" in client.post(f"/calls/{call['id']}/cancel", json={}).json()["detail"]
