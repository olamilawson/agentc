"""The sponsorship triage graph (PRD: workflow graphs / sponsorship triage).

    new enquiry (code) -> classify (model, cheap) -> look up history (code)
        -> score and route (model, strong; the route itself is decided in code)
        -> find call dates (code; pursue route, from the owner's calendar)
        -> draft reply (model, standard) -> check reply (code facts, then model)
        -> owner approval (interrupt) -> send and log (release-class tool, ledger)

A mail that answers call times the agent proposed takes a short path: if the
sender chose one of the two times, the owner is asked to approve a calendar
event with that sender as attendee; if not, the mail is escalated. A mail from
a sender with a call already scheduled may ask to cancel it (the owner approves
the cancellation) or to move it (the agent proposes two new times, and the
sender's choice moves the same event once the owner approves).

Routes: pursue, ask for detail, decline, escalate. An escalated enquiry gets no
reply; the owner receives a summary and the original mail in the queue and
acknowledges it. Check reply returns a failed draft to draft reply twice at
most, then the draft goes to the owner with the open faults beside it.

The criteria, tone rules and templates live in skills/sponsorship-triage/SKILL.md
and the thresholds in config/sponsorship.yaml, never here.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, model_validator

from personal_agent.config import SponsorshipPolicy
from personal_agent.db import Database
from personal_agent import scheduling
from personal_agent.engine import steps
from personal_agent.engine.approval import make_approval_node
from personal_agent.engine.graph import ROLE
from personal_agent.gateway.tools import ApprovalLapsed, ApprovalRequired, UncertainOutcome
from personal_agent.graph_mail import GraphError
from personal_agent.harness.context import build_context
from personal_agent.harness.contract import StepContract

SKILL = "sponsorship-triage"
CLIENT = "sponsorship"  # enquiries are made to the owner's own company
MAX_CHECK_RETURNS = 2  # PRD: a failed check returns it to Draft reply, twice at most

Criterion = Literal[
    "strategic_fit", "audience_relevance", "commercial_value", "brand_safety", "activation_potential"
]
CRITERIA: tuple[str, ...] = Criterion.__args__
RiskFlag = Literal["reputational", "political", "legal"]
ReplyRoute = Literal["pursue", "ask_for_detail", "decline", "reschedule"]

_ROUTE_LABELS = {
    "pursue": "Pursue",
    "ask_for_detail": "Ask for detail",
    "decline": "Decline",
    "escalate": "Escalate to the owner",
    "reschedule": "Propose new call times",
}


# --- schemas -------------------------------------------------------------------
class EnquiryIn(BaseModel):
    message_id: str | None = None
    sender: str = Field(pattern=r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
    sender_name: str | None = None
    subject: str = ""
    body: str = Field(min_length=1)
    received_at: str | None = None


class ClassifyIn(BaseModel):
    enquiry_text: str = Field(min_length=1)
    # Call times the agent proposed to this sender earlier and that are still open.
    offered_times: list[str] = []
    # The call already in the calendar with this sender, if there is one.
    scheduled_call: str = ""


class ClassifyOut(BaseModel):
    is_sponsorship: bool
    reason: str = Field(min_length=1)
    sender_name: str | None = None
    organisation: str | None = None
    request: str | None = None
    amount: str | None = None  # as the mail states it
    amount_naira: float | None = None
    dates: str | None = None
    audience: str | None = None
    # One of the offered call times, word for word, if the mail accepts it.
    chosen_time: str | None = None
    # What the mail asks of a scheduled call, if anything.
    call_request: Literal["cancel", "move"] | None = None


class CriterionScore(BaseModel):
    criterion: Criterion
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)


class Risk(BaseModel):
    flag: RiskFlag
    reason: str = Field(min_length=1)


class ScoreIn(BaseModel):
    enquiry_text: str = Field(min_length=1)
    enquiry: dict
    history: list[dict] = []
    correspondence: list[dict] = []


class ScoreOut(BaseModel):
    scores: list[CriterionScore]
    risk_flags: list[Risk] = []
    missing_information: list[str] = Field(default=[], max_length=5)
    outside_criteria: bool = False
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def _every_criterion_once(self):
        if sorted(s.criterion for s in self.scores) != sorted(CRITERIA):
            raise ValueError("scores must cover each criterion exactly once: " + ", ".join(CRITERIA))
        return self


class ReplyIn(BaseModel):
    enquiry_text: str = Field(min_length=1)
    enquiry: dict
    history: list[dict] = []
    correspondence: list[dict] = []
    route: ReplyRoute
    route_reason: str
    missing_information: list[str] = []
    # Times found free in the owner's calendar, to propose as written. Empty: ask the sender for dates.
    call_times: list[str] = []
    faults: list[str] = []


class ReplyOut(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)


class CheckIn(BaseModel):
    enquiry_text: str = Field(min_length=1)
    route: ReplyRoute
    reply_subject: str
    reply_body: str


class CheckOut(BaseModel):
    passes: bool
    faults: list[str] = []

    @model_validator(mode="after")
    def _a_failure_names_its_faults(self):
        if not self.passes and not self.faults:
            raise ValueError("a reply that does not pass must list its faults")
        return self


class SendIn(BaseModel):
    to: str
    subject: str
    body: str
    in_reply_to: str | None = None


def _instruction(text: str, schema: type[BaseModel]) -> str:
    return f"{text}\n\nOutput schema: {json.dumps(schema.model_json_schema(), sort_keys=True)}"


def sponsorship_contracts() -> dict[str, StepContract]:
    return {
        "classify": StepContract(
            node="classify",
            tier="cheap",
            input_schema=ClassifyIn,
            output_schema=ClassifyOut,
            instruction=_instruction(
                "Decide whether this mail is a sponsorship request and extract what it states: "
                "sender name, organisation, the request, the amount, dates and audience. Copy the "
                "amount as written; give amount_naira as a number only when the amount is in "
                "naira, otherwise null. Use null for anything the mail does not state. If the inputs "
                "list offered call times and the mail accepts one of them, give that time word for "
                "word as chosen_time; if it accepts none, or asks for another time, give null. If the "
                "inputs name a scheduled call and the mail asks to cancel it or to move it, give "
                "call_request as cancel or move; otherwise null.",
                ClassifyOut,
            ),
        ),
        "look_up_history": StepContract(
            node="look_up_history",
            tier="cheap",  # code node: runs without a model; the tier is unused
            allowed_tools=["search_mail"],
            input_schema=ClassifyIn,
            output_schema=ClassifyIn,
            instruction="",
        ),
        "find_call_dates": StepContract(
            node="find_call_dates",
            tier="cheap",  # code node: runs without a model; the tier is unused
            allowed_tools=["read_calendar"],
            input_schema=ClassifyIn,
            output_schema=ClassifyIn,
            instruction="",
        ),
        "score_and_route": StepContract(
            node="score_and_route",
            tier="strong",
            limits={"max_tokens": 3000},
            input_schema=ScoreIn,
            output_schema=ScoreOut,
            instruction=_instruction(
                "Rate the enquiry against the owner's criteria, 1 to 5 each, using these ids: "
                + ", ".join(CRITERIA) + ". Set a risk flag only with a reason from the mail. List the "
                "missing information a reply would need to ask for, at most five items. Mark "
                "outside_criteria when the request is of a kind the criteria exclude. Write a "
                "two or three sentence summary for the owner. Do not choose a route.",
                ScoreOut,
            ),
        ),
        "draft_reply": StepContract(
            node="draft_reply",
            tier="standard",
            input_schema=ReplyIn,
            output_schema=ReplyOut,
            instruction=_instruction(
                "Write the reply for the given route, following the template and the tone rules. "
                "When the inputs give call times, propose those times word for word and no others. "
                "State no figure, date, address or commitment that is not in the enquiry or the inputs. If the "
                "inputs list faults from a previous attempt, correct them.",
                ReplyOut,
            ),
        ),
        "check_reply": StepContract(
            node="check_reply",
            tier="standard",
            input_schema=CheckIn,
            output_schema=CheckOut,
            instruction=_instruction(
                "Test the reply against the facts in the enquiry and the owner's tone rules, and "
                "against the template for its route. It fails if it states anything the enquiry "
                "does not support, promises or implies a decision, breaks a tone rule, or does not "
                "do what its route requires. List each fault in one line.",
                CheckOut,
            ),
        ),
        "send_and_log": StepContract(
            node="send_and_log",
            tier="cheap",  # code node: runs without a model; the tier is unused
            allowed_tools=[
                "send_mail", "read_calendar", "create_calendar_event", "change_calendar_event", "cancel_calendar_event",
            ],
            input_schema=SendIn,
            output_schema=SendIn,
            instruction="",
        ),
    }


# --- routing (code) ----------------------------------------------------------------
_MULTIPLIERS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}
_AMOUNT = re.compile(
    r"(?<![\w.])(?P<mark>₦|NGN\s?|N(?=\d))?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"\s?(?P<mult>thousand|million|billion|mn|bn|k|m)?\b\s?(?P<word>naira|NGN)?",
    re.IGNORECASE,
)


def max_naira_amount(text: str) -> float | None:
    """The largest naira amount written in the text, read without a model.

    Counts a number carrying a naira mark or word, or a million/billion
    multiplier. A backstop for the escalation rule: the amount the model
    extracted is never the only thing between a large request and a reply.
    """
    found = []
    for m in _AMOUNT.finditer(text):
        mult = (m.group("mult") or "").lower()
        if not (m.group("mark") or m.group("word") or mult in ("m", "mn", "million", "bn", "billion")):
            continue
        found.append(float(m.group("num").replace(",", "")) * _MULTIPLIERS.get(mult, 1))
    return max(found) if found else None


def decide_route(
    policy: SponsorshipPolicy | None, enquiry: dict, scored: dict, enquiry_text: str
) -> tuple[str, str]:
    """(route, reason). Escalation is checked first and never depends on a score."""
    mean = mean_score(scored)
    if scored["risk_flags"]:
        return "escalate", "Risk flagged: " + "; ".join(f"{r['flag']} ({r['reason']})" for r in scored["risk_flags"])
    if policy is None:
        return "escalate", "No sponsorship policy is configured, so the owner decides."

    written = max_naira_amount(enquiry_text)
    known = [v for v in (enquiry.get("amount_naira"), written) if v is not None]
    if enquiry.get("amount") or known:
        if policy.escalate_above is None:
            return "escalate", "The enquiry names an amount and the owner has set no limit."
        if not known:
            return "escalate", f"The amount ({enquiry['amount']}) could not be read as naira."
        if max(known) > policy.escalate_above:
            return "escalate", (
                f"The amount ({max(known):,.0f} naira) is above the owner's limit "
                f"({policy.escalate_above:,.0f} naira)."
            )

    if scored["outside_criteria"]:
        return "decline", "The request is outside the sponsorship criteria."
    if mean < policy.decline_below:
        return "decline", f"Mean score {mean} is below the decline threshold ({policy.decline_below})."
    if scored["missing_information"]:
        return "ask_for_detail", "The fit is possible and information is missing."
    if mean >= policy.pursue_at:
        return "pursue", f"Mean score {mean} is at or above the pursue threshold ({policy.pursue_at})."
    return "escalate", (
        f"Mean score {mean} sits between the decline and pursue thresholds with nothing missing; "
        "the owner decides."
    )


def mean_score(scored: dict) -> float:
    return round(sum(s["score"] for s in scored["scores"]) / len(scored["scores"]), 2)


# --- reply facts (code) ------------------------------------------------------------
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|https?://\S+|\bwww\.\S+", re.IGNORECASE)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text)}


def unsupported_facts(reply: str, enquiry_text: str) -> list[str]:
    """Figures and addresses in the reply that the enquiry does not contain."""
    reply = _LIST_MARKER.sub("", reply)  # "1. " before a question is not a figure
    faults = [
        f"the reply states a figure that is not in the enquiry: {n}"
        for n in sorted(_numbers(reply) - _numbers(enquiry_text))
    ]
    known = enquiry_text.casefold()
    faults += [
        f"the reply gives an address that is not in the enquiry: {a}"
        for a in _ADDRESS.findall(reply)
        if a.casefold().rstrip(".,") not in known
    ]
    return faults


# --- a call the sender has agreed to (code) ----------------------------------------------
def open_call_proposal(db: Database, sender: str, now: datetime) -> dict | None:
    """The most recent reply the agent sent this sender that proposed call times
    still in the future and not yet turned into an event."""
    runs = [r for r in db.list_runs() if r["workflow"] == "sponsorship-triage"]
    scheduled = {
        ((r["outputs"] or {}).get("event") or {}).get("proposal_run_id")
        for r in runs if r["status"] in ("done", "waiting_for_approval")
    }
    moment = now.isoformat(timespec="minutes")
    for run in runs:  # newest first
        out = run["outputs"] or {}
        if run["status"] != "done" or run["id"] in scheduled or not (out.get("sent") or {}).get("sent"):
            continue
        if (run["inputs"] or {}).get("sender", "").lower() != sender.lower():
            continue
        slots = [slot for slot in out.get("call_slots") or [] if slot["start"] > moment]
        if slots:
            return {
                "run_id": run["id"], "slots": slots, "subject": (run["inputs"] or {}).get("subject") or "",
                # Set when the times were proposed to move a call that already exists.
                "call_id": (out.get("call") or {}).get("id") if out.get("route") == "reschedule" else None,
            }
    return None


def build_event(policy, slot: dict, attendee: str, proposal: dict) -> dict:
    """The event for a chosen time. Every field comes from the agent's own
    earlier proposal and the envelope sender; none comes from the mail's text."""
    start = datetime.fromisoformat(slot["start"])
    topic = re.sub(r"^(?:\s*(?:re|fw|fwd)\s*:\s*)+", "", proposal["subject"], flags=re.IGNORECASE).strip()
    return {
        "proposal_run_id": proposal["run_id"],
        "label": slot["label"],
        "start": start.isoformat(timespec="minutes"),
        "end": (start + timedelta(minutes=policy.call_minutes)).isoformat(timespec="minutes"),
        "attendee": attendee,
        "subject": f"Call: {topic}" if topic else "Call about your sponsorship enquiry",
        "note": f"A call to discuss: {topic}." if topic else "A call to discuss your sponsorship enquiry.",
    }


def moved_event(policy, slot: dict, call: dict, proposal: dict) -> dict:
    """The same event at a time the sender chose from the agent's new proposal."""
    start = datetime.fromisoformat(slot["start"])
    return {
        "call_id": call["id"], "event_id": call["event_id"], "proposal_run_id": proposal["run_id"],
        "old_label": call["label"], "label": slot["label"], "start": start.isoformat(timespec="minutes"),
        "end": (start + timedelta(minutes=policy.call_minutes)).isoformat(timespec="minutes"),
        "attendee": call["sender"], "subject": call["subject"],
    }


def cancelled_event(call: dict) -> dict:
    return {
        "call_id": call["id"], "event_id": call["event_id"], "label": call["label"],
        "attendee": call["sender"], "subject": call["subject"], "comment": "Cancelled as you asked.",
    }


def move_payload(event: dict) -> dict:
    """The exact change the owner approves: one event, its old time and its new one."""
    return {
        "to": event["attendee"],
        "subject": f"Move: {event['subject']}",
        "body": f"From: {event['old_label']}\nTo: {event['label']}\nMicrosoft tells: {event['attendee']}\n",
        "event_id": event["event_id"],
        "start": event["start"],
        "end": event["end"],
    }


def cancel_payload(event: dict) -> dict:
    """The exact cancellation the owner approves, with the line the attendee reads."""
    return {
        "to": event["attendee"],
        "subject": f"Cancel: {event['subject']}",
        "body": f"When: {event['label']}\nMicrosoft tells: {event['attendee']}\n\n"
                f"Message with the cancellation: {event['comment']}\n",
        "event_id": event["event_id"],
        "comment": event["comment"],
    }


def event_payload(event: dict) -> dict:
    """The exact event the owner approves and the tool creates."""
    return {
        "to": event["attendee"],
        "subject": event["subject"],
        "body": f"When: {event['label']}\nInvitation to: {event['attendee']}\n\n{event['note']}\n",
        "start": event["start"],
        "end": event["end"],
        "note": event["note"],
    }


# --- what the owner sees -------------------------------------------------------------
def approval_payload(state: dict) -> dict:
    """The exact content: the reply as it will be sent, or the escalation summary."""
    inputs = state["inputs"]
    if state["route"] == "schedule_call":
        return event_payload(state["event"])
    if state["route"] == "move_call":
        return move_payload(state["event"])
    if state["route"] == "cancel_call":
        return cancel_payload(state["event"])
    if state["route"] != "escalate":
        return {
            "to": inputs["sender"],
            "subject": state["reply_subject"],
            "body": state["reply_body"],
            "in_reply_to": inputs.get("message_id"),
        }
    enquiry = state["enquiry"]
    lines = [
        state["route_reason"],
        "",
        state["summary"],
        "",
        f"Organisation: {enquiry.get('organisation') or 'not stated'}",
        f"Request: {enquiry.get('request') or 'not stated'}",
        f"Amount: {enquiry.get('amount') or 'not stated'}",
        f"Dates: {enquiry.get('dates') or 'not stated'}",
        "",
        "No reply has been drafted.",
        "",
        "--- Original mail ---",
        state["enquiry_text"],
    ]
    return {"subject": f"Escalated: {inputs.get('subject') or 'sponsorship enquiry'}", "body": "\n".join(lines)}


def approval_notes(state: dict) -> dict:
    """Beside the content, never part of what is sent."""
    if state["route"] == "schedule_call":
        notes = [
            "The sender chose this time from the two you proposed.",
            "Approving creates the event in your calendar, and Microsoft sends the invitation.",
        ]
        return {"action": "invite", "notes": notes + list(state.get("flags", []))}
    if state["route"] == "move_call":
        notes = [
            "The sender chose this new time from the two you proposed.",
            "Approving moves the event in your calendar, and Microsoft tells the attendee.",
        ]
        return {"action": "move", "notes": notes + list(state.get("flags", []))}
    if state["route"] == "cancel_call":
        notes = [
            "The sender asked to cancel this call.",
            "Approving cancels the event in your calendar, and Microsoft tells the attendee.",
        ]
        return {"action": "cancel", "notes": notes + list(state.get("flags", []))}
    if state["route"] == "escalate":
        notes = [f"Previous enquiries on the ledger: {len(state.get('history', []))}"]
        return {"action": "acknowledge", "notes": notes + list(state.get("flags", []))}
    enquiry = state["enquiry"]
    notes = [
        f"Route: {_ROUTE_LABELS[state['route']]}. {state['route_reason']}",
        f"From: {enquiry.get('organisation') or 'organisation not stated'}; "
        f"amount: {enquiry.get('amount') or 'not stated'}",
        f"Previous enquiries on the ledger: {len(state.get('history', []))}",
    ]
    if state.get("correspondence"):
        notes.append(f"Earlier mail with this sender in the mailbox: {len(state['correspondence'])}")
    notes += [f"Open fault: {fault}" for fault in state.get("faults", [])]
    return {"action": "send", "notes": notes + list(state.get("flags", []))}


def sponsorship_outputs(state: dict) -> dict | None:
    """Run-record outputs; partial work is kept when a run stops early."""
    if not state.get("enquiry"):
        return None
    out = {
        "enquiry": state["enquiry"],
        "history": state.get("history", []),
        "correspondence": state.get("correspondence", []),
        "scores": state.get("scores"),
        "mean_score": state.get("mean_score"),
        "risk_flags": state.get("risk_flags", []),
        "missing_information": state.get("missing_information", []),
        "route": state.get("route"),
        "route_reason": state.get("route_reason"),
        "summary": state.get("summary"),
        "faults": state.get("faults", []),
        "ledger_id": state.get("ledger_id"),
        "call_slots": state.get("call_slots", []),
        "event": state.get("event"),
        "call": state.get("call"),
        "sent": state.get("sent"),
    }
    if state.get("reply_body"):
        out["reply"] = {"to": state["inputs"]["sender"], "subject": state["reply_subject"], "body": state["reply_body"]}
    return out


def on_rejected(db: Database, run: dict, comment: str | None) -> None:
    """A rejection is a decision too: the ledger records it with its reason."""
    row = db.ledger_row_for_run(run["id"])
    if row:
        db.update_ledger_row(row["id"], decision="rejected by owner", outcome=comment or "no reason given")


# --- the graph ---------------------------------------------------------------------
class TriageState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    usage: dict
    flags: list[str]
    fallback_used: bool
    enquiry_text: str
    enquiry: dict
    history: list[dict]
    correspondence: list[dict]
    scores: list[dict]
    mean_score: float
    risk_flags: list[dict]
    missing_information: list[str]
    summary: str
    route: str
    route_reason: str
    ledger_id: str
    call_slots: list[dict]
    offered: dict
    call: dict
    event: dict
    reply_subject: str
    reply_body: str
    faults: list[str]
    check_returns: int
    approval_error: str | None
    approved_hash: str
    redraft: bool
    sent: dict
    error: str | None


def build_sponsorship_graph(h):
    """h: engine.runner.Harness — kept untyped here to avoid an import cycle."""
    db = h.db
    skill_text = h.skills.get(SKILL, "")
    policy = h.settings.sponsorship
    calendar = h.settings.calendar

    def run_model(node: str, state: TriageState, payload: dict, rubric: str) -> tuple[dict, dict]:
        # The mail and the ledger history are data: both come from outside senders.
        untrusted = [("enquiry", state["enquiry_text"])]
        if payload.get("history"):
            untrusted.append(("ledger history", json.dumps(payload["history"], indent=2)))
        if payload.get("correspondence"):
            untrusted.append(("earlier correspondence", json.dumps(payload["correspondence"], indent=2)))
        return steps.run_model(
            h, node, state, payload, rubric, untrusted, hide=("enquiry_text", "history", "correspondence")
        )

    def new_enquiry(state: TriageState) -> dict:
        # Code node. The reply address is the envelope sender, fixed here; nothing
        # a model extracts from the mail can change where a reply goes.
        mail = EnquiryIn.model_validate(state.get("inputs") or {}).model_dump()
        who = f"{mail['sender_name']} <{mail['sender']}>" if mail["sender_name"] else mail["sender"]
        text = f"From: {who}\nSubject: {mail['subject']}\n\n{mail['body']}"
        _, found = build_context(ROLE, "", [], [("enquiry", text)], {})
        # Is this an answer to call times the agent proposed to this sender?
        now = datetime.now(timezone.utc)
        offered = open_call_proposal(db, mail["sender"], now) or {}
        # Is there a call with this sender already in the calendar?
        call = db.scheduled_call_for(mail["sender"], now.isoformat(timespec="minutes")) or {}
        return {"inputs": mail, "enquiry_text": text, "offered": offered, "call": call,
                "flags": list(state.get("flags", [])) + found}

    def record_on_ledger(state: TriageState, enquiry: dict, route: str) -> str:
        mail = state["inputs"]
        fields = {
            "message_id": mail.get("message_id"),
            "received_at": mail.get("received_at"),
            "sender": mail["sender"],
            "organisation": enquiry.get("organisation"),
            "request": enquiry.get("request"),
            "amount": enquiry.get("amount"),
            "route": route,
            "decision": "awaiting owner",
            "outcome": None,
        }
        row = db.ledger_row_for_run(state["run_id"])  # one row per enquiry, even if a node re-runs
        if row:
            db.update_ledger_row(row["id"], **fields)
        return row["id"] if row else db.create_ledger_row(state["run_id"], **fields)

    def classify(state: TriageState) -> dict:
        offered, call = state.get("offered") or {}, state.get("call") or {}
        labels = [slot["label"] for slot in offered.get("slots", [])]
        out, usage = run_model(
            "classify", state,
            {"enquiry_text": state["enquiry_text"], "offered_times": labels, "scheduled_call": call.get("label", "")},
            skill_text,
        )
        if labels or call:
            # A mail about a call. The model may only pick one of the times the agent
            # offered, or say the mail asks to cancel or move the scheduled call; code
            # decides what follows, and the owner approves any change.
            chosen = next((slot for slot in offered.get("slots", []) if slot["label"] == out.get("chosen_time")), None)
            request = out.get("call_request") if call else None
            route, reason, event = None, "", None
            if h.mail is None:
                if labels or request:
                    route, reason = "escalate", "The mail is about a call, and there is no calendar connection."
            elif chosen and offered.get("call_id"):
                if call and call["id"] == offered["call_id"]:
                    route, event = "move_call", moved_event(calendar, chosen, call, offered)
                    reason = f"The sender chose {chosen['label']} for the call set for {call['label']}."
                else:
                    route, reason = "escalate", "The sender chose a new time for a call that is no longer scheduled."
            elif chosen:
                route, event = "schedule_call", build_event(calendar, chosen, state["inputs"]["sender"], offered)
                reason = f"The sender chose {chosen['label']}."
            elif request == "cancel":
                route, event = "cancel_call", cancelled_event(call)
                reason = f"The sender asked to cancel the call set for {call['label']}."
            elif request == "move":
                route, reason = "reschedule", f"The sender asked to move the call set for {call['label']}."
            elif labels:
                route, reason = "escalate", "The sender answered your proposed call times without choosing one of them."
            if route:
                update: dict = {
                    "enquiry": out, "summary": out["reason"], "route": route, "route_reason": reason,
                    "history": [], "correspondence": [], "missing_information": [], **usage,
                }
                if event:
                    update["event"] = event
                update["ledger_id"] = record_on_ledger(state, out, route)
                db.audit(state["run_id"], "sponsorship_routed", {
                    "route": route, "reason": reason, "ledger_id": update["ledger_id"],
                })
                return update
            # A call is scheduled, and this mail is about something else: ordinary triage.
        if not out["is_sponsorship"]:
            db.audit(state["run_id"], "not_a_sponsorship_request", {"reason": out["reason"]})
            return {"enquiry": out, "route": "not_sponsorship", "route_reason": out["reason"], **usage}
        return {"enquiry": out, **usage}

    def after_classify(state: TriageState) -> str:
        if state.get("route") in ("schedule_call", "move_call", "cancel_call", "escalate"):
            return "approval"  # about a call: straight to the owner
        if state.get("route") == "reschedule":
            return "find_call_dates"  # new times, then a reply that proposes them
        return END if state.get("route") == "not_sponsorship" else "look_up_history"

    def look_up_history(state: TriageState) -> dict:
        # Code node: earlier enquiries from this sender or organisation on the ledger.
        rows = db.ledger_history(
            state["inputs"]["sender"], state["enquiry"].get("organisation"), exclude_run_id=state["run_id"]
        )
        keep = ("received_at", "sender", "organisation", "request", "amount", "route", "decision", "outcome")
        update: dict = {"history": [{k: row[k] for k in keep} for row in rows], "correspondence": []}
        if h.mail is not None:
            # Earlier mail with this sender, read from the connected mailbox. A failed
            # read is flagged and the triage goes on with the ledger alone.
            try:
                found = h.tools.call(
                    state["run_id"], "look_up_history", h.contracts["look_up_history"].allowed_tools,
                    "search_mail", {"address": state["inputs"]["sender"]},
                )
                this_mail = state["inputs"].get("message_id")
                update["correspondence"] = [
                    {k: m[k] for k in ("received_at", "from", "subject", "preview")}
                    for m in found["messages"] if m.get("id") != this_mail
                ]
            except GraphError as e:
                update["flags"] = list(state.get("flags", [])) + [f"earlier correspondence could not be read: {e}"]
        return update

    def score_and_route(state: TriageState) -> dict:
        scored, usage = run_model(
            "score_and_route",
            state,
            {
                "enquiry_text": state["enquiry_text"], "enquiry": state["enquiry"],
                "history": state["history"], "correspondence": state.get("correspondence", []),
            },
            skill_text,
        )
        route, reason = decide_route(policy, state["enquiry"], scored, state["enquiry_text"])
        ledger_id = record_on_ledger(state, state["enquiry"], route)
        db.audit(state["run_id"], "sponsorship_routed", {"route": route, "reason": reason, "ledger_id": ledger_id})
        return {
            "scores": scored["scores"],
            "mean_score": mean_score(scored),
            "risk_flags": scored["risk_flags"],
            "missing_information": scored["missing_information"],
            "summary": scored["summary"],
            "route": route,
            "route_reason": reason,
            "ledger_id": ledger_id,
            **usage,
        }

    def after_route(state: TriageState) -> str:
        if state["route"] == "escalate":
            return "approval"
        return "find_call_dates" if state["route"] == "pursue" else "draft_reply"

    def read_busy(state: TriageState, node: str, now: datetime,
                  until: datetime | None = None) -> list[tuple[datetime, datetime]]:
        # With `until`: from now to that moment, to confirm times already chosen.
        # Without: the window in which new times are looked for.
        start, end = (now, until) if until else scheduling.search_window(calendar, now)
        found = h.tools.call(
            state["run_id"], node, h.contracts[node].allowed_tools, "read_calendar",
            {"start": start.isoformat(timespec="minutes"), "end": end.isoformat()},
        )
        return [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in found["busy"]]

    def find_call_dates(state: TriageState) -> dict:
        # Code node: two free times from the owner's calendar, never chosen by a
        # model. With no calendar, or none free, the reply asks the sender for dates.
        flags = list(state.get("flags", []))
        if h.mail is None:
            return {"call_slots": []}
        now = datetime.now(timezone.utc)
        try:
            busy = read_busy(state, "find_call_dates", now)
        except GraphError as e:
            return {"call_slots": [], "flags": flags + [f"the calendar could not be read, so the reply asks for dates: {e}"]}
        # Times already offered in another reply are not offered twice.
        taken = {
            slot["start"]
            for run in db.list_runs()
            if run["workflow"] == "sponsorship-triage" and run["id"] != state["run_id"]
            and run["status"] in ("waiting_for_approval", "done")
            for slot in (run["outputs"] or {}).get("call_slots") or []
        }
        slots = scheduling.find_slots(calendar, busy, now, taken)
        if len(slots) < 2:
            return {"call_slots": [], "flags": flags + ["the calendar has fewer than two free times, so the reply asks for dates"]}
        return {"call_slots": slots}

    def draft_reply(state: TriageState) -> dict:
        out, usage = run_model(
            "draft_reply",
            state,
            {
                "enquiry_text": state["enquiry_text"],
                "enquiry": state["enquiry"],
                "history": state["history"],
                "correspondence": state.get("correspondence", []),
                "route": state["route"],
                "route_reason": state["route_reason"],
                "missing_information": state["missing_information"],
                "call_times": [slot["label"] for slot in state.get("call_slots", [])],
                "faults": state.get("faults", []),
            },
            skill_text,
        )
        return {"reply_subject": out["subject"], "reply_body": out["body"], **usage}

    def check_reply(state: TriageState) -> dict:
        # Facts first, without a model: an invented figure or address goes straight back.
        offered = [slot["label"] for slot in state.get("call_slots", [])]
        reply = f"{state['reply_subject']}\n{state['reply_body']}"
        faults = unsupported_facts(reply, "\n".join([state["enquiry_text"], *offered]))
        # A time the calendar found free goes into the reply as written, or not at all.
        faults += [f"the reply does not propose this time as written: {time}" for time in offered if time not in reply]
        usage: dict = {}
        if not faults:
            out, usage = run_model(
                "check_reply",
                state,
                {
                    "enquiry_text": state["enquiry_text"],
                    "route": state["route"],
                    "reply_subject": state["reply_subject"],
                    "reply_body": state["reply_body"],
                },
                skill_text,
            )
            faults = [] if out["passes"] else out["faults"]
        returns = state.get("check_returns", 0)
        return {"faults": faults, "check_returns": returns + 1 if faults else returns, **usage}

    def after_check(state: TriageState) -> str:
        if state["faults"] and state["check_returns"] <= MAX_CHECK_RETURNS:
            return "draft_reply"
        return "approval"  # clean, or the limit is reached and the faults go to the owner

    approval = make_approval_node(db, approval_payload, approval_notes)

    def after_approval(state: TriageState) -> str:
        return "approval" if state.get("approval_error") else "send_and_log"

    def release_call(state: TriageState) -> dict:
        # Creating, moving or cancelling an event tells the attendee at once: each is
        # a release, under the same rules as a send.
        route, event, now = state["route"], state["event"], datetime.now(timezone.utc)
        tool, verb, done = {
            "schedule_call": ("create_calendar_event", "invitation", f"invitation sent for {event['label']}"),
            "move_call": ("change_calendar_event", "change", f"call moved to {event['label']}"),
            "cancel_call": ("cancel_calendar_event", "cancellation", f"call cancelled ({event['label']})"),
        }[route]
        tools = h.contracts["send_and_log"].allowed_tools
        try:
            if route != "schedule_call":
                call = db.get_call(event["call_id"])
                if not call or call["status"] != "scheduled" or call["event_id"] != event["event_id"]:
                    return {"approval_error": "That call is no longer scheduled, so there is nothing to change. "
                                              "Reject this item."}
            if route != "cancel_call":
                start = datetime.fromisoformat(event["start"])
                busy = read_busy(state, "send_and_log", now, until=max(start, now) + timedelta(days=1))
                if not scheduling.still_free(calendar, [{"start": event["start"]}], busy, now):
                    return {"approval_error": "That time is no longer free in the calendar, or has passed. Free it and "
                                              "approve again, or reject this item and answer the sender yourself."}
            result = h.tools.call(state["run_id"], "send_and_log", tools, tool, approval_payload(state))
        except (ApprovalRequired, ApprovalLapsed) as e:
            return {"approval_error": str(e)}
        except GraphError as e:
            db.update_ledger_row(state["ledger_id"], outcome=f"{verb} failed, nothing changed: {e}")
            return {"approval_error": f"The {verb} did not go through: {e}. Approve again to retry."}
        except UncertainOutcome as e:
            db.update_ledger_row(
                state["ledger_id"], decision="call approved by owner", outcome=f"{verb} outcome unknown: {e}"
            )
            raise
        if route == "schedule_call":
            db.create_call(
                state["run_id"], result.get("event_id"), event["attendee"], event["subject"],
                event["start"], event["end"], event["label"],
            )
        elif route == "move_call":
            db.update_call(event["call_id"], start=event["start"], end=event["end"], label=event["label"])
        else:
            db.update_call(event["call_id"], status="cancelled")
        db.update_ledger_row(state["ledger_id"], decision="call approved by owner", outcome=done)
        return {"sent": result, "approval_error": None, "error": None}

    def send_and_log(state: TriageState) -> dict:
        if state["route"] == "escalate":
            db.update_ledger_row(
                state["ledger_id"], decision="escalated to owner", outcome="acknowledged; no reply sent"
            )
            return {"approval_error": None, "error": None}
        if state["route"] in ("schedule_call", "move_call", "cancel_call"):
            return release_call(state)
        payload = h.contracts["send_and_log"].validate_input(approval_payload(state))
        if state.get("call_slots"):
            # Approval can come hours after the times were found: confirm them before sending.
            now = datetime.now(timezone.utc)
            try:
                last = max(datetime.fromisoformat(slot["start"]) for slot in state["call_slots"]) + timedelta(days=1)
                busy = read_busy(state, "send_and_log", now, until=max(last, now + timedelta(days=1)))
                free = scheduling.still_free(calendar, state["call_slots"], busy, now)
            except GraphError as e:
                return {"approval_error": f"The reply was not sent: the calendar could not be read to confirm "
                                          f"the proposed times ({e}). Approve again to retry."}
            if not free:
                note = "a proposed time was no longer free at approval, so the reply was drafted again with new times"
                return {"approval_error": None, "redraft": True, "faults": [], "check_returns": 0,
                        "flags": list(state.get("flags", [])) + [note]}
        try:
            sent = h.tools.call(
                state["run_id"], "send_and_log", h.contracts["send_and_log"].allowed_tools, "send_mail", payload
            )
        except (ApprovalRequired, ApprovalLapsed) as e:
            return {"approval_error": str(e)}
        except GraphError as e:
            # A definite failure: nothing went out. The reply returns to the queue
            # with the reason, and approving it again tries again.
            db.update_ledger_row(state["ledger_id"], outcome=f"send failed, nothing sent: {e}")
            return {"approval_error": f"The reply was not sent: {e}. Approve again to retry."}
        except UncertainOutcome as e:
            # Unknown: it may have gone. The harness does not retry; the run stops for the owner.
            db.update_ledger_row(
                state["ledger_id"], decision="reply approved by owner", outcome=f"send outcome unknown: {e}"
            )
            raise
        db.update_ledger_row(
            state["ledger_id"],
            decision="reply approved by owner",
            outcome="reply sent" if sent.get("sent") else "reply held in the local outbox; mail is not connected",
        )
        return {"sent": sent, "approval_error": None, "error": None, "redraft": False}

    def after_send(state: TriageState) -> str:
        if state.get("redraft") and not state.get("sent"):
            return "find_call_dates"  # new times, a new draft, and a new approval
        return "approval" if state.get("approval_error") else END

    builder = StateGraph(TriageState)
    builder.add_node("new_enquiry", new_enquiry)
    builder.add_node("classify", classify)
    builder.add_node("look_up_history", look_up_history)
    builder.add_node("score_and_route", score_and_route)
    builder.add_node("find_call_dates", find_call_dates)
    builder.add_node("draft_reply", draft_reply)
    builder.add_node("check_reply", check_reply)
    builder.add_node("approval", approval)
    builder.add_node("send_and_log", send_and_log)
    builder.add_edge(START, "new_enquiry")
    builder.add_edge("new_enquiry", "classify")
    builder.add_conditional_edges(
        "classify", after_classify,
        {END: END, "look_up_history": "look_up_history", "approval": "approval", "find_call_dates": "find_call_dates"},
    )
    builder.add_edge("look_up_history", "score_and_route")
    builder.add_conditional_edges(
        "score_and_route", after_route,
        {"approval": "approval", "draft_reply": "draft_reply", "find_call_dates": "find_call_dates"},
    )
    builder.add_edge("find_call_dates", "draft_reply")
    builder.add_edge("draft_reply", "check_reply")
    builder.add_conditional_edges(
        "check_reply", after_check, {"draft_reply": "draft_reply", "approval": "approval"}
    )
    builder.add_conditional_edges(
        "approval", after_approval, {"approval": "approval", "send_and_log": "send_and_log"}
    )
    builder.add_conditional_edges(
        "send_and_log", after_send, {"approval": "approval", "find_call_dates": "find_call_dates", END: END}
    )
    return builder
