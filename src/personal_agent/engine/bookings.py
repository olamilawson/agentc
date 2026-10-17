"""Cal.com bookings: people book a call from a booking page, and the owner approves each one.

The owner sets the event type in Cal.com to require confirmation. Then:

    someone books  -> Cal.com posts BOOKING_REQUESTED to /hooks/calcom
                   -> a run waits in Review with the time, the person and their note
    owner approves -> the agent confirms it through Cal.com's API (a release, like a send);
                      Cal.com puts it in the calendar and tells the person
    owner rejects  -> the agent declines it through the API, with the owner's reason

Bookings that need no confirmation, cancellations and reschedules are recorded on
the Calls screen as Cal.com reports them. A booking changed in Cal.com itself is
changed there; the Calls screen follows.

No model is involved. The person's name and note are outside text: they are
shown to the owner, escaped, and never acted on.

Written from Cal.com's published webhook and API v2 references (October 2026),
and not yet run against a live Cal.com account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from datetime import datetime, timezone
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from personal_agent import scheduling
from personal_agent.engine.approval import make_approval_node
from personal_agent.gateway.tools import ApprovalLapsed, ApprovalRequired

WORKFLOW = "booking-request"
CLIENT = "General"
TOOL = "confirm_booking"
API = "https://api.cal.com/v2"
API_VERSION = "2026-02-25"
# Tests replace this with a stand-in for the network.
TRANSPORT: httpx.BaseTransport | None = None


class BookingError(Exception):
    """Cal.com refused or could not be reached. The message says which."""


def signature_ok(body: bytes, header: str | None, secret: str | None) -> bool:
    """Cal.com signs the raw body with HMAC SHA-256. Its documentation does not say whether the digest
    is written as hex or as base64, so either is accepted; both are compared in constant time."""
    if not secret or not header:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    given = header.strip().removeprefix("sha256=")
    return hmac.compare_digest(given, digest.hex()) or hmac.compare_digest(given, base64.b64encode(digest).decode())


def _call(path: str, body: dict | None = None) -> dict:
    key = os.environ.get("CALCOM_API_KEY")
    if not key:
        raise BookingError("the server has no CALCOM_API_KEY, so the agent cannot answer Cal.com. "
                           "Confirm or decline this booking in Cal.com, or add the key with ./deploy/setup.sh --key calcom")
    try:
        with httpx.Client(transport=TRANSPORT, timeout=30.0) as http:
            response = http.post(f"{API}{path}", json=body or {}, headers={
                "Authorization": f"Bearer {key}", "cal-api-version": API_VERSION,
            })
    except httpx.HTTPError as e:
        raise BookingError(f"Cal.com could not be reached ({type(e).__name__})") from e
    if response.status_code not in (200, 201):
        try:
            detail = response.json().get("error", {}).get("message") or response.text
        except ValueError:
            detail = response.text
        raise BookingError(f"Cal.com refused ({response.status_code} {str(detail)[:200]})")
    return response.json()


def confirm(uid: str) -> dict:
    _call(f"/bookings/{uid}/confirm")
    return {"confirmed": True, "uid": uid}


def decline(uid: str, reason: str | None) -> dict:
    _call(f"/bookings/{uid}/decline", {"reason": reason} if reason else {})
    return {"declined": True, "uid": uid}


# --- reading what Cal.com sends -------------------------------------------------------------------
def _label(policy: scheduling.CalendarPolicy, start: datetime, end: datetime) -> str:
    """The agent's usual words for a call's time, with the booking's own end, since its length is set in Cal.com."""
    from zoneinfo import ZoneInfo

    text = scheduling.label(policy, start)
    finish = end.astimezone(ZoneInfo(policy.timezone)).strftime("%H:%M")
    return re.sub(r" to \d{2}:\d{2} ", f" to {finish} ", text, count=1)


def booking_of(payload: dict, policy: scheduling.CalendarPolicy) -> dict:
    """The parts of a Cal.com booking the agent keeps, in its own terms."""
    attendee = (payload.get("attendees") or [{}])[0]
    start = datetime.fromisoformat(str(payload["startTime"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(str(payload["endTime"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    responses = payload.get("responses") or {}
    note = responses.get("notes") or payload.get("additionalNotes") or payload.get("description") or ""
    if isinstance(note, dict):
        note = note.get("value") or ""
    return {
        "uid": str(payload["uid"]), "title": str(payload.get("title") or "Call")[:200],
        "attendee": str(attendee.get("email") or "")[:320], "name": str(attendee.get("name") or "")[:200],
        "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"),
        "label": _label(policy, start, end), "note": str(note)[:2000],
        "reschedules": payload.get("rescheduleUid") or payload.get("fromReschedule"),
    }


def event_id(uid: str) -> str:
    return f"calcom:{uid}"


def call_for(db, uid: str) -> dict | None:
    return next((c for c in db.list_calls() if c["event_id"] == event_id(uid)), None)


def record(db, booking: dict, run_id: str = "") -> str:
    """The booking on the Calls screen, once. A second report of the same booking updates it."""
    held = call_for(db, booking["uid"])
    if held:
        db.update_call(held["id"], start=booking["start"], end=booking["end"], label=booking["label"], status="scheduled")
        return held["id"]
    return db.create_call(run_id, event_id(booking["uid"]), booking["attendee"], booking["title"],
                          booking["start"], booking["end"], booking["label"])


def pending_run(db, uid: str) -> dict | None:
    return next((r for r in db.list_runs(status="waiting_for_approval")
                 if r["workflow"] == WORKFLOW and (r["inputs"] or {}).get("uid") == uid), None)


# --- the graph -----------------------------------------------------------------------------------
class BookingState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    flags: list[str]
    approval_error: str | None
    approved_hash: str
    confirmed: dict
    error: str | None


def payload_of(state: dict) -> dict:
    """What the owner approves, and the exact content of the confirmation that follows."""
    b = state["inputs"]
    return {"uid": b["uid"], "subject": b["title"], "to": b["attendee"],
            "body": f"{b['label']}, with {b['name'] or b['attendee']}."}


def about(state: dict) -> dict:
    b = state["inputs"]
    notes = [f"Booked through Cal.com by {b['name'] or 'someone'} ({b['attendee']})."]
    if b.get("note"):
        notes.append(f"Their note: {b['note']}")
    if b.get("busy"):
        notes.append(b["busy"])
    notes.append("Approve and Cal.com confirms it and tells them. Reject and Cal.com declines it, with your reason.")
    return {"action": "booking", "notes": notes}


def booking_outputs(state: dict) -> dict | None:
    return {"confirmed": state.get("confirmed")} if state.get("confirmed") else None


def booking_rejected(db, run: dict, comment: str | None) -> None:
    """The owner rejected the booking in Review: Cal.com declines it, with the owner's reason."""
    uid = (run["inputs"] or {}).get("uid")
    try:
        decline(uid, comment)
        outcome, detail = "executed", {"declined": True}
    except BookingError as e:
        outcome, detail = "failed", {"error": str(e)}
    db.record_tool_call(run["id"], "decline", "decline_booking", "release", "", None, outcome, detail)
    db.audit(run["id"], "booking_declined" if outcome == "executed" else "booking_decline_failed", detail)


def build_booking_graph(h):
    db = h.db
    approval = make_approval_node(db, payload_of, about)

    def confirm_it(state: BookingState) -> dict:
        try:
            result = h.tools.call(state["run_id"], "confirm", [TOOL], TOOL, payload_of(state))
        except (ApprovalRequired, ApprovalLapsed) as e:
            return {"approval_error": str(e)}
        # Recorded here as well as from Cal.com's own report, so the Calls screen shows it at once.
        record(db, state["inputs"], state["run_id"])
        return {"confirmed": result, "approval_error": None, "error": None}

    builder = StateGraph(BookingState)
    builder.add_node("approval", approval)
    builder.add_node("confirm", confirm_it)
    builder.add_edge(START, "approval")
    builder.add_conditional_edges("approval", lambda s: "approval" if s.get("approval_error") else "confirm",
                                  {"approval": "approval", "confirm": "confirm"})
    builder.add_conditional_edges("confirm", lambda s: "approval" if s.get("approval_error") else END,
                                  {"approval": "approval", END: END})
    return builder


def take(engine, event: dict) -> dict:
    """One webhook delivery from Cal.com. Returns what was done, for the reply and the audit log."""
    db, policy = engine.db, engine.settings.calendar
    trigger = str(event.get("triggerEvent") or "")
    payload = event.get("payload") or {}
    if "uid" not in payload or "startTime" not in payload:
        return {"ignored": trigger or "an event with no booking"}
    booking = booking_of(payload, policy)
    waiting = pending_run(db, booking["uid"])

    if trigger == "BOOKING_REQUESTED":
        if waiting or call_for(db, booking["uid"]):
            return {"duplicate": True}
        mail = engine.harness.mail
        if mail is not None:  # what the owner's calendar says about that time, beside the request
            try:
                start, end = (datetime.fromisoformat(booking[k]) for k in ("start", "end"))
                busy = mail.busy_periods(start, end)
                booking["busy"] = ("Your calendar shows you busy at that time." if any(s < end and e > start for s, e in busy)
                                   else "Your calendar is free at that time.")
            except Exception:  # a calendar that cannot be read is said, not hidden
                booking["busy"] = "Your calendar could not be read for that time."
        run_id = engine.start_run(WORKFLOW, CLIENT, booking, by_hand=False)
        return {"run_id": run_id, "waiting": True}

    if trigger in ("BOOKING_CREATED", "BOOKING_RESCHEDULED"):
        if booking.get("reschedules"):  # the old booking is replaced by this one
            old = call_for(db, str(booking["reschedules"]))
            if old:
                db.update_call(old["id"], status="cancelled")
        if waiting:  # confirmed in Cal.com itself: nothing left for Review
            db.update_run(waiting["id"], status="cancelled", error_reason="confirmed in Cal.com")
        return {"call_id": record(db, booking, waiting["id"] if waiting else "")}

    if trigger in ("BOOKING_CANCELLED", "BOOKING_REJECTED"):
        held = call_for(db, booking["uid"])
        if held:
            db.update_call(held["id"], status="cancelled")
        if waiting:
            db.update_run(waiting["id"], status="cancelled", error_reason=f"{trigger.lower().replace('_', ' ')} in Cal.com")
        return {"cancelled": bool(held or waiting)}

    return {"ignored": trigger}
