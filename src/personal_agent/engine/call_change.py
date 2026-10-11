"""The owner moves or cancels a call the agent scheduled.

    prepare (code) -> owner approval (interrupt) -> release (release-class tool)

The owner starts it from the Calls screen. There is no model in it: the event
and its attendee come from the agent's list of calls, the new time or the
cancellation message from the owner. Starting it changes nothing. The queue
shows the exact change first, and the approval there is what releases it,
as with any other change an attendee will see.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from personal_agent import scheduling
from personal_agent.engine.approval import make_approval_node
from personal_agent.engine.sponsorship_triage import cancel_payload, move_payload
from personal_agent.gateway.tools import ApprovalLapsed, ApprovalRequired, UncertainOutcome
from personal_agent.graph_mail import GraphError
from personal_agent.harness.contract import StepContract, StepFailed

WORKFLOW = "call-change"
DEFAULT_MESSAGE = "This call is cancelled."


class ChangeIn(BaseModel):
    action: Literal["move", "cancel"]
    call_id: str = Field(min_length=1)
    # For a move: the new start, as local date and time in the owner's time zone ("2026-10-20T14:00").
    start: str | None = None
    # For a cancellation: the line the attendee reads with it.
    message: str = Field(default=DEFAULT_MESSAGE, max_length=500)


def call_change_contracts() -> dict[str, StepContract]:
    code = dict(tier="cheap", instruction="", input_schema=ChangeIn, output_schema=ChangeIn)  # no model in this graph
    return {
        "prepare_call_change": StepContract(node="prepare_call_change", allowed_tools=["read_calendar"], **code),
        "release_call_change": StepContract(
            node="release_call_change",
            allowed_tools=["read_calendar", "change_calendar_event", "cancel_calendar_event"], **code,
        ),
    }


def _payload(state: dict) -> dict:
    return move_payload(state["event"]) if state["route"] == "move_call" else cancel_payload(state["event"])


def _notes(state: dict) -> dict:
    moving = state["route"] == "move_call"
    notes = [
        "You asked for this change from the Calls screen.",
        f"Approving {'moves' if moving else 'cancels'} the event in your calendar, and Microsoft tells the attendee.",
    ]
    return {"action": "move" if moving else "cancel", "notes": notes}


def call_change_outputs(state: dict) -> dict | None:
    if not state.get("event"):
        return None
    out = {"route": state["route"], "event": state["event"], "sent": state.get("sent")}
    out.update({k: v for k, v in _payload(state).items() if k in ("subject", "body")})
    return out


class ChangeState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    flags: list[str]
    route: str
    event: dict
    approval_error: str | None
    approved_hash: str
    sent: dict
    error: str | None


def build_call_change_graph(h):
    """h: engine.runner.Harness — kept untyped here to avoid an import cycle."""
    db = h.db
    calendar = h.settings.calendar

    def free_at(state: ChangeState, node: str, start: datetime, now: datetime) -> bool:
        found = h.tools.call(
            state["run_id"], node, h.contracts[node].allowed_tools, "read_calendar",
            {"start": now.isoformat(), "end": (max(start, now) + timedelta(days=1)).isoformat()},
        )
        busy = [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in found["busy"]]
        return scheduling.still_free(calendar, [{"start": start.isoformat(timespec="minutes")}], busy, now)

    def prepare(state: ChangeState) -> dict:
        # Code node: turn the owner's request into the exact change, or stop with the reason.
        ask = h.contracts["prepare_call_change"].validate_input(state.get("inputs") or {})
        if h.mail is None:
            raise StepFailed("there is no calendar connection, so the agent cannot change a call")
        call = db.get_call(ask["call_id"])
        now = datetime.now(timezone.utc)
        if not call or call["status"] != "scheduled":
            raise StepFailed("that call is not scheduled, so there is nothing to change")
        if call["start"] <= now.isoformat(timespec="minutes"):
            raise StepFailed("that call has already started or passed")
        event = {
            "call_id": call["id"], "event_id": call["event_id"], "attendee": call["sender"], "subject": call["subject"],
        }
        if ask["action"] == "cancel":
            message = (ask["message"] or "").strip() or DEFAULT_MESSAGE
            return {"route": "cancel_call", "event": {**event, "label": call["label"], "comment": message}}
        if not ask["start"]:
            raise StepFailed("a move needs the new date and time")
        try:
            local = datetime.fromisoformat(ask["start"])
        except ValueError as e:
            raise StepFailed(f"the new date and time could not be read: {ask['start']!r}") from e
        if local.tzinfo is None:  # the owner's clock, as the Calls screen sends it
            local = local.replace(tzinfo=ZoneInfo(calendar.timezone))
        start = local.astimezone(timezone.utc)
        if start <= now:
            raise StepFailed("the new time has already passed")
        try:
            if not free_at(state, "prepare_call_change", start, now):
                raise StepFailed(f"{scheduling.label(calendar, start)} is not free in your calendar")
        except GraphError as e:
            raise StepFailed(f"the calendar could not be read to confirm the new time: {e}") from e
        return {
            "route": "move_call",
            "event": {
                **event, "old_label": call["label"], "label": scheduling.label(calendar, start),
                "start": start.isoformat(timespec="minutes"),
                "end": (start + timedelta(minutes=calendar.call_minutes)).isoformat(timespec="minutes"),
            },
        }

    approval = make_approval_node(db, _payload, _notes)

    def after_approval(state: ChangeState) -> str:
        return "approval" if state.get("approval_error") else "release"

    def release(state: ChangeState) -> dict:
        route, event, now = state["route"], state["event"], datetime.now(timezone.utc)
        verb = "move" if route == "move_call" else "cancellation"
        try:
            call = db.get_call(event["call_id"])
            if not call or call["status"] != "scheduled" or call["event_id"] != event["event_id"]:
                return {"approval_error": "That call is no longer scheduled, so there is nothing to change. Reject this item."}
            if route == "move_call" and not free_at(state, "release_call_change", datetime.fromisoformat(event["start"]), now):
                return {"approval_error": "That time is no longer free in the calendar, or has passed. Free it and "
                                          "approve again, or reject this item and choose another time."}
            tool = "change_calendar_event" if route == "move_call" else "cancel_calendar_event"
            result = h.tools.call(
                state["run_id"], "release_call_change", h.contracts["release_call_change"].allowed_tools, tool, _payload(state)
            )
        except (ApprovalRequired, ApprovalLapsed) as e:
            return {"approval_error": str(e)}
        except GraphError as e:
            return {"approval_error": f"The {verb} did not go through: {e}. Approve again to retry."}
        except UncertainOutcome:
            db.audit(state["run_id"], "call_change_uncertain", {"call_id": event["call_id"], "change": verb})
            raise
        if route == "move_call":
            db.update_call(event["call_id"], start=event["start"], end=event["end"], label=event["label"])
        else:
            db.update_call(event["call_id"], status="cancelled")
        db.audit(state["run_id"], "call_changed_by_owner", {"call_id": event["call_id"], "change": verb, "label": event["label"]})
        return {"sent": result, "approval_error": None, "error": None}

    def after_release(state: ChangeState) -> str:
        return "approval" if state.get("approval_error") else END

    builder = StateGraph(ChangeState)
    builder.add_node("prepare", prepare)
    builder.add_node("approval", approval)
    builder.add_node("release", release)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "approval")
    builder.add_conditional_edges("approval", after_approval, {"approval": "approval", "release": "release"})
    builder.add_conditional_edges("release", after_release, {"approval": "approval", END: END})
    return builder
