"""The sponsorship triage graph (PRD: workflow graphs / sponsorship triage).

    new enquiry (code) -> classify (model, cheap) -> look up history (code)
        -> score and route (model, strong; the route itself is decided in code)
        -> draft reply (model, standard) -> check reply (code facts, then model)
        -> owner approval (interrupt) -> send and log (release-class tool, ledger)

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
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, model_validator

from personal_agent.config import SponsorshipPolicy
from personal_agent.db import Database
from personal_agent.engine import steps
from personal_agent.engine.approval import make_approval_node
from personal_agent.engine.graph import ROLE
from personal_agent.gateway.tools import ApprovalLapsed, ApprovalRequired
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
ReplyRoute = Literal["pursue", "ask_for_detail", "decline"]

_ROUTE_LABELS = {
    "pursue": "Pursue",
    "ask_for_detail": "Ask for detail",
    "decline": "Decline",
    "escalate": "Escalate to the owner",
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
    route: ReplyRoute
    route_reason: str
    missing_information: list[str] = []
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
                "naira, otherwise null. Use null for anything the mail does not state.",
                ClassifyOut,
            ),
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
                "State no figure, date, address or commitment that is not in the enquiry. If the "
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
            allowed_tools=["send_mail"],
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


# --- what the owner sees -------------------------------------------------------------
def approval_payload(state: dict) -> dict:
    """The exact content: the reply as it will be sent, or the escalation summary."""
    inputs = state["inputs"]
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
    notes += [f"Open fault: {fault}" for fault in state.get("faults", [])]
    return {"action": "send", "notes": notes + list(state.get("flags", []))}


def sponsorship_outputs(state: dict) -> dict | None:
    """Run-record outputs; partial work is kept when a run stops early."""
    if not state.get("enquiry"):
        return None
    out = {
        "enquiry": state["enquiry"],
        "history": state.get("history", []),
        "scores": state.get("scores"),
        "mean_score": state.get("mean_score"),
        "risk_flags": state.get("risk_flags", []),
        "missing_information": state.get("missing_information", []),
        "route": state.get("route"),
        "route_reason": state.get("route_reason"),
        "summary": state.get("summary"),
        "faults": state.get("faults", []),
        "ledger_id": state.get("ledger_id"),
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
    scores: list[dict]
    mean_score: float
    risk_flags: list[dict]
    missing_information: list[str]
    summary: str
    route: str
    route_reason: str
    ledger_id: str
    reply_subject: str
    reply_body: str
    faults: list[str]
    check_returns: int
    approval_error: str | None
    approved_hash: str
    sent: dict
    error: str | None


def build_sponsorship_graph(h):
    """h: engine.runner.Harness — kept untyped here to avoid an import cycle."""
    db = h.db
    skill_text = h.skills.get(SKILL, "")
    policy = h.settings.sponsorship

    def run_model(node: str, state: TriageState, payload: dict, rubric: str) -> tuple[dict, dict]:
        # The mail and the ledger history are data: both come from outside senders.
        untrusted = [("enquiry", state["enquiry_text"])]
        if payload.get("history"):
            untrusted.append(("ledger history", json.dumps(payload["history"], indent=2)))
        return steps.run_model(h, node, state, payload, rubric, untrusted, hide=("enquiry_text", "history"))

    def new_enquiry(state: TriageState) -> dict:
        # Code node. The reply address is the envelope sender, fixed here; nothing
        # a model extracts from the mail can change where a reply goes.
        mail = EnquiryIn.model_validate(state.get("inputs") or {}).model_dump()
        who = f"{mail['sender_name']} <{mail['sender']}>" if mail["sender_name"] else mail["sender"]
        text = f"From: {who}\nSubject: {mail['subject']}\n\n{mail['body']}"
        _, found = build_context(ROLE, "", [], [("enquiry", text)], {})
        return {"inputs": mail, "enquiry_text": text, "flags": list(state.get("flags", [])) + found}

    def classify(state: TriageState) -> dict:
        out, usage = run_model("classify", state, {"enquiry_text": state["enquiry_text"]}, skill_text)
        if not out["is_sponsorship"]:
            db.audit(state["run_id"], "not_a_sponsorship_request", {"reason": out["reason"]})
            return {"enquiry": out, "route": "not_sponsorship", "route_reason": out["reason"], **usage}
        return {"enquiry": out, **usage}

    def after_classify(state: TriageState) -> str:
        return END if state.get("route") == "not_sponsorship" else "look_up_history"

    def look_up_history(state: TriageState) -> dict:
        # Code node: earlier enquiries from this sender or organisation on the ledger.
        rows = db.ledger_history(
            state["inputs"]["sender"], state["enquiry"].get("organisation"), exclude_run_id=state["run_id"]
        )
        keep = ("received_at", "sender", "organisation", "request", "amount", "route", "decision", "outcome")
        return {"history": [{k: row[k] for k in keep} for row in rows]}

    def score_and_route(state: TriageState) -> dict:
        scored, usage = run_model(
            "score_and_route",
            state,
            {"enquiry_text": state["enquiry_text"], "enquiry": state["enquiry"], "history": state["history"]},
            skill_text,
        )
        route, reason = decide_route(policy, state["enquiry"], scored, state["enquiry_text"])
        enquiry, mail = state["enquiry"], state["inputs"]
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
        row = db.ledger_row_for_run(state["run_id"])  # one row per enquiry, even if this node re-runs
        if row:
            db.update_ledger_row(row["id"], **fields)
        ledger_id = row["id"] if row else db.create_ledger_row(state["run_id"], **fields)
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
        return "approval" if state["route"] == "escalate" else "draft_reply"

    def draft_reply(state: TriageState) -> dict:
        out, usage = run_model(
            "draft_reply",
            state,
            {
                "enquiry_text": state["enquiry_text"],
                "enquiry": state["enquiry"],
                "history": state["history"],
                "route": state["route"],
                "route_reason": state["route_reason"],
                "missing_information": state["missing_information"],
                "faults": state.get("faults", []),
            },
            skill_text,
        )
        return {"reply_subject": out["subject"], "reply_body": out["body"], **usage}

    def check_reply(state: TriageState) -> dict:
        # Facts first, without a model: an invented figure or address goes straight back.
        faults = unsupported_facts(f"{state['reply_subject']}\n{state['reply_body']}", state["enquiry_text"])
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

    def send_and_log(state: TriageState) -> dict:
        if state["route"] == "escalate":
            db.update_ledger_row(
                state["ledger_id"], decision="escalated to owner", outcome="acknowledged; no reply sent"
            )
            return {"approval_error": None, "error": None}
        payload = h.contracts["send_and_log"].validate_input(approval_payload(state))
        try:
            sent = h.tools.call(
                state["run_id"], "send_and_log", h.contracts["send_and_log"].allowed_tools, "send_mail", payload
            )
        except (ApprovalRequired, ApprovalLapsed) as e:
            return {"approval_error": str(e)}
        db.update_ledger_row(
            state["ledger_id"],
            decision="reply approved by owner",
            outcome="reply sent" if sent.get("sent") else "reply held in the local outbox; mail is not connected",
        )
        return {"sent": sent, "approval_error": None, "error": None}

    def after_send(state: TriageState) -> str:
        return "approval" if state.get("approval_error") else END

    builder = StateGraph(TriageState)
    builder.add_node("new_enquiry", new_enquiry)
    builder.add_node("classify", classify)
    builder.add_node("look_up_history", look_up_history)
    builder.add_node("score_and_route", score_and_route)
    builder.add_node("draft_reply", draft_reply)
    builder.add_node("check_reply", check_reply)
    builder.add_node("approval", approval)
    builder.add_node("send_and_log", send_and_log)
    builder.add_edge(START, "new_enquiry")
    builder.add_edge("new_enquiry", "classify")
    builder.add_conditional_edges("classify", after_classify, {END: END, "look_up_history": "look_up_history"})
    builder.add_edge("look_up_history", "score_and_route")
    builder.add_conditional_edges(
        "score_and_route", after_route, {"approval": "approval", "draft_reply": "draft_reply"}
    )
    builder.add_edge("draft_reply", "check_reply")
    builder.add_conditional_edges(
        "check_reply", after_check, {"draft_reply": "draft_reply", "approval": "approval"}
    )
    builder.add_conditional_edges(
        "approval", after_approval, {"approval": "approval", "send_and_log": "send_and_log"}
    )
    builder.add_conditional_edges("send_and_log", after_send, {"approval": "approval", END: END})
    return builder
