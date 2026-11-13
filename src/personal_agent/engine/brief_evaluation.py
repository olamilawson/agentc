"""The brief evaluation graph (PRD: workflow graphs / brief evaluation).

    receive brief (code) -> extract fields (model, cheap) -> check gaps (code)
        -> score by rubric (model, strong) -> draft assessment (model, standard)
        -> verify evidence (code string match, then model, standard)
        -> owner approval (interrupt) -> file assessment (draft-class tool)

Verify evidence confirms by string match that every quote appears in the brief,
then asks a model whether each score follows from its evidence. A failed check
returns the work to scoring, twice at most; after that the draft goes to the
owner with the open faults listed above it. Nothing is filed to the client
folder until the owner approves the exact content.

A brief sent through a private link (inputs carry reply_to) also gets its reply:

    verify evidence -> draft reply (model, standard) -> owner approval of one card
        -> file assessment -> send reply (release)

The card's body is the reply, which the owner can edit; the assessment rides
beside it. Approving the card is the owner's approval of that exact reply, so
the send records an approval for the mail itself, derived from hers, and the
gateway's rule (an approval for the exact content) still holds.

The rubric lives in skills/brief-evaluation/SKILL.md, never here.
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field, model_validator

from personal_agent.engine import steps
from personal_agent.engine.approval import make_approval_node
from personal_agent.engine.graph import ROLE
from personal_agent.gateway.tools import ApprovalLapsed, ApprovalRequired, UncertainOutcome, content_hash
from personal_agent.harness.context import build_context
from personal_agent.harness.contract import StepContract, StepFailed

SKILL = "brief-evaluation"
MAX_VERIFY_RETURNS = 2  # PRD: a failed check returns the draft, twice at most

Criterion = Literal[
    "strategic_soundness",
    "commercial_viability",
    "strategic_importance",
    "audience",
    "distinctiveness",
    "executional_clarity",
    "measurability",
    "risk_governance",
]
# Rubric order; the ids match evaluation/sets/brief-evaluation.jsonl.
CRITERIA: tuple[str, ...] = Criterion.__args__
Decision = Literal["pursue", "ask_for_detail", "decline", "escalate"]

RECORD_FIELDS = (
    "client",
    "objective",
    "audience",
    "budget",
    "timeline",
    "deliverables",
    "constraints",
    "success_measures",
)


# --- schemas -------------------------------------------------------------------
class ReceiveIn(BaseModel):
    brief_path: str | None = None
    brief_text: str | None = None


class BriefRecord(BaseModel):
    client: str | None = None
    objective: str | None = None
    audience: str | None = None
    budget: str | None = None
    timeline: str | None = None
    deliverables: list[str] = []
    constraints: list[str] = []
    success_measures: list[str] = []


class ExtractIn(BaseModel):
    brief_text: str = Field(min_length=1)


class CriterionScore(BaseModel):
    criterion: Criterion
    score: int = Field(ge=1, le=5)
    # brief: the quotes carry the score. absence: the brief does not state it.
    # context: the judgement rests on the owner's workstream context, said so explicitly.
    basis: Literal["brief", "absence", "context"] = "brief"
    quotes: list[str] = []
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _evidence_required(self):
        if self.basis == "brief" and not any(q.strip() for q in self.quotes):
            raise ValueError(f"{self.criterion}: a score without quoted evidence is invalid")
        return self


class ScoreIn(BaseModel):
    brief_text: str = Field(min_length=1)
    record: dict
    gaps: list[str] = []
    faults: list[str] = []


class ScoreOut(BaseModel):
    scores: list[CriterionScore]
    decision: Decision
    decision_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _every_criterion_once(self):
        if sorted(s.criterion for s in self.scores) != sorted(CRITERIA):
            raise ValueError("scores must cover each rubric criterion exactly once: " + ", ".join(CRITERIA))
        return self


class AssessmentIn(BaseModel):
    brief_text: str = Field(min_length=1)
    record: dict
    gaps: list[str] = []
    scores: list[dict]
    decision: Decision
    decision_reason: str


class AssessmentOut(BaseModel):
    note: str = Field(min_length=1)
    questions: list[str] = Field(default=[], max_length=5)


class VerifyIn(BaseModel):
    brief_text: str = Field(min_length=1)
    scores: list[dict]


class Verdict(BaseModel):
    criterion: Criterion
    follows: bool
    reason: str = ""


class VerifyOut(BaseModel):
    verdicts: list[Verdict]

    @model_validator(mode="after")
    def _every_criterion_once(self):
        if sorted(v.criterion for v in self.verdicts) != sorted(CRITERIA):
            raise ValueError("verdicts must cover each rubric criterion exactly once: " + ", ".join(CRITERIA))
        return self


class ReplyIn(BaseModel):
    first_name: str
    project: str
    owner: str
    decision: Decision
    decision_reason: str
    questions: list[str] = []
    booking_url: str | None = None


class ReplyOut(BaseModel):
    subject: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4000)


class FileIn(BaseModel):
    subject: str
    body: str


class FileOut(BaseModel):
    client: str
    file: str


def _instruction(text: str, schema: type[BaseModel]) -> str:
    return f"{text}\n\nOutput schema: {json.dumps(schema.model_json_schema(), sort_keys=True)}"


def brief_contracts() -> dict[str, StepContract]:
    return {
        "receive_brief": StepContract(
            node="receive_brief",
            tier="cheap",  # code node: runs without a model; the tier is unused
            allowed_tools=["read_client_file"],
            input_schema=ReceiveIn,
            output_schema=ExtractIn,
            instruction="",
        ),
        "extract_fields": StepContract(
            node="extract_fields",
            tier="cheap",
            input_schema=ExtractIn,
            output_schema=BriefRecord,
            instruction=_instruction(
                "Turn the brief into a record. Copy what the brief states; do not infer or "
                "improve it. Use null (or an empty list) for anything the brief does not state.",
                BriefRecord,
            ),
        ),
        "score_by_rubric": StepContract(
            node="score_by_rubric",
            tier="strong",
            limits={"max_tokens": 4000},
            input_schema=ScoreIn,
            output_schema=ScoreOut,
            instruction=_instruction(
                "Score the brief against the rubric. Rate each criterion from 1 to 5, in rubric "
                "order, using these ids: " + ", ".join(CRITERIA) + ". Every quote must be copied "
                "character for character from the brief — no paraphrase, no ellipsis. Set basis to "
                "'absence' when the brief does not state what the criterion needs, and to 'context' "
                "when the judgement rests on the owner's workstream context rather than brief text; "
                "otherwise 'brief', with at least one quote. Then give the rubric's decision and its "
                "reason. If the inputs list faults from a previous attempt, correct them.",
                ScoreOut,
            ),
        ),
        "draft_assessment": StepContract(
            node="draft_assessment",
            tier="standard",
            input_schema=AssessmentIn,
            output_schema=AssessmentOut,
            instruction=_instruction(
                "Write the owner's assessment note for this brief from the scores and the decision, "
                "strongest thought first, and the questions to send back to the client — at most "
                "five, each tied to a gap or a weak score. No questions when nothing is missing.",
                AssessmentOut,
            ),
        ),
        "verify_evidence": StepContract(
            node="verify_evidence",
            tier="standard",
            input_schema=VerifyIn,
            output_schema=VerifyOut,
            instruction=_instruction(
                "For each criterion, decide whether the score follows from its quoted evidence "
                "(or from the stated absence or context) under the rubric's anchors. Give one "
                "verdict per criterion; when a score does not follow, say why in one line.",
                VerifyOut,
            ),
        ),
        "draft_brief_reply": StepContract(
            node="draft_brief_reply",
            tier="standard",
            input_schema=ReplyIn,
            output_schema=ReplyOut,
            instruction=_instruction(
                "Write the email the owner sends back to the person who sent this brief, in the owner's own first "
                "person, signed with the owner's name. Keep it short, warm and plain. Thank them for the brief by its "
                "project name. Then follow the decision: pursue, say the work interests the owner and invite them to a "
                "call, giving the booking link if there is one; ask_for_detail, ask the questions given, numbered; "
                "decline, decline courteously, with no scores and no criticism; escalate, say the owner is reviewing "
                "it and will come back to them. Never mention scores, a rubric, an assessment or a model. Never promise "
                "a date, a price or terms the inputs do not give. Use no em dashes.",
                ReplyOut,
            ),
        ),
        "file_assessment": StepContract(
            node="file_assessment",
            tier="cheap",  # code node: runs without a model; the tier is unused
            allowed_tools=["write_client_file"],
            input_schema=FileIn,
            output_schema=FileOut,
            instruction="",
        ),
    }


# --- evidence check (code) -------------------------------------------------------
_TYPOGRAPHY = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
})


def _norm(text: str) -> str:
    # Parsed PDFs and Word files differ from a model's copy only in whitespace
    # and typographic quotes; anything else is a real mismatch.
    return " ".join(text.translate(_TYPOGRAPHY).split())


def unmatched_quotes(scores: list[dict], brief_text: str) -> list[str]:
    """Faults for every quote that does not appear in the brief."""
    haystack = _norm(brief_text)
    faults = []
    for s in scores:
        for quote in s.get("quotes", []):
            if not _norm(quote) or _norm(quote) not in haystack:
                faults.append(f"{s['criterion']}: quote does not appear in the brief: {quote!r}")
    return faults


# --- the content the owner approves ------------------------------------------------
_CRITERION_LABELS = {
    "strategic_soundness": "Strategic soundness",
    "commercial_viability": "Commercial viability",
    "strategic_importance": "Strategic importance and fit",
    "audience": "Audience definition and relevance",
    "distinctiveness": "Distinctiveness",
    "executional_clarity": "Executional clarity",
    "measurability": "Measurability",
    "risk_governance": "Risk and governance",
}
_DECISION_LABELS = {
    "pursue": "Pursue",
    "ask_for_detail": "Ask for detail",
    "decline": "Decline",
    "escalate": "Escalate to the owner",
}
_BASIS_NOTES = {
    "absence": "Evidence: the brief does not state this.",
    "context": "Evidence: workstream context, not brief text.",
}


def assessment_payload(state: dict) -> dict:
    """The exact content shown in the queue and filed to the client folder."""
    record = state.get("record") or {}
    lines = [
        f"# Brief assessment: {state['client']}",
        "",
        f"Source: {state.get('brief_source', 'pasted text')}",
        f"Recommendation: {_DECISION_LABELS[state['decision']]}. {state['decision_reason']}",
    ]
    if state.get("faults"):
        lines += ["", "## Open faults (evidence check limit reached)", ""]
        lines += [f"- {fault}" for fault in state["faults"]]
    lines += ["", "## Assessment", "", state["note"]]
    if state.get("questions"):
        lines += ["", "## Questions for the client", ""]
        lines += [f"{i}. {q}" for i, q in enumerate(state["questions"], 1)]
    lines += ["", "## Scores"]
    for s in state["scores"]:
        lines += ["", f"### {_CRITERION_LABELS[s['criterion']]}: {s['score']}/5", "", s["rationale"]]
        lines += [f'> "{quote}"' for quote in s.get("quotes", [])]
        if s.get("basis") in _BASIS_NOTES:
            lines.append(_BASIS_NOTES[s["basis"]])
    lines += ["", "## Extracted record", ""]
    for name in RECORD_FIELDS:
        value = record.get(name)
        shown = "; ".join(value) if isinstance(value, list) else value
        lines.append(f"- {name.replace('_', ' ').capitalize()}: {shown or 'not stated'}")
    return {
        "subject": f"Brief assessment: {state['client']} ({_DECISION_LABELS[state['decision']]})",
        "body": "\n".join(lines) + "\n",
    }


def card_payload(state: dict) -> dict:
    """The one card the owner approves for a brief that came through a private link: the reply to send,
    which she may edit, with the assessment beside it."""
    to = state["inputs"]["reply_to"]
    return {
        "subject": f"Reply to {to['first_name']}: {to['project']}",
        "to": to["email"],
        "reply_subject": state["reply_subject"],
        "body": state["reply_body"],
        "assessment": assessment_payload(state)["body"],
    }


def approval_content(state: dict) -> dict:
    return card_payload(state) if state.get("reply_body") else assessment_payload(state)


def reply_mail(state: dict) -> dict:
    return {"client": state["client"], "to": state["inputs"]["reply_to"]["email"],
            "subject": state["reply_subject"], "body": state["reply_body"]}


def brief_outputs(state: dict) -> dict | None:
    """Run-record outputs; partial work is kept when a run stops early."""
    if not state.get("record"):
        return None
    out = {
        "record": state.get("record"),
        "gaps": state.get("gaps", []),
        "scores": state.get("scores"),
        "decision": state.get("decision"),
        "decision_reason": state.get("decision_reason"),
        "note": state.get("note"),
        "questions": state.get("questions", []),
        "faults": state.get("faults", []),
        "filed": state.get("filed"),
    }
    if state.get("reply_body"):
        out["reply"] = {"to": state["inputs"]["reply_to"]["email"], "subject": state["reply_subject"],
                        "body": state["reply_body"], **(state.get("reply_result") or {})}
    if state.get("note"):
        out.update(assessment_payload(state))
    return out


# --- the graph ---------------------------------------------------------------------
class BriefState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    usage: dict
    flags: list[str]
    fallback_used: bool
    brief_source: str
    brief_text: str
    record: dict
    gaps: list[str]
    scores: list[dict]
    decision: str
    decision_reason: str
    note: str
    questions: list[str]
    faults: list[str]
    verify_returns: int
    approval_error: str | None
    approved_hash: str
    filed: dict
    reply_subject: str
    reply_body: str
    reply_result: dict
    error: str | None


def build_brief_graph(h):
    """h: engine.runner.Harness — kept untyped here to avoid an import cycle."""
    skill_text = h.skills.get(SKILL, "")

    def run_model(node: str, state: BriefState, payload: dict, rubric: str) -> tuple[dict, dict]:
        """The brief travels as marked data; the other inputs are the node's declared fields."""
        return steps.run_model(
            h, node, state, payload, rubric, [("brief", state["brief_text"])], hide=("brief_text",)
        )

    def receive_brief(state: BriefState) -> dict:
        # Code node: the brief is a file in the run's client folder (read through
        # the gateway, so the scope check applies) or text pasted with the request.
        inputs = h.contracts["receive_brief"].validate_input(state.get("inputs") or {})
        flags = list(state.get("flags", []))
        if inputs["brief_path"]:
            doc = h.tools.call(
                state["run_id"],
                "receive_brief",
                h.contracts["receive_brief"].allowed_tools,
                "read_client_file",
                {"client": state["client"], "path": inputs["brief_path"]},
            )
            text, source = doc["text"], inputs["brief_path"]
            if doc.get("truncated"):
                flags.append("brief: longer than the read limit; only the first part was evaluated")
        else:
            text, source = inputs["brief_text"] or "", "pasted text"
        if not text.strip():
            raise StepFailed("node 'receive_brief' found no brief: give brief_path or brief_text")
        _, found = build_context(ROLE, "", [], [("brief", text)], {})
        return {"brief_text": text, "brief_source": source, "flags": flags + found}

    def extract_fields(state: BriefState) -> dict:
        record, usage = run_model("extract_fields", state, {"brief_text": state["brief_text"]}, "")
        return {"record": record, **usage}

    def check_gaps(state: BriefState) -> dict:
        # Code node: missing fields by rule, never by a model's opinion.
        return {"gaps": [name for name in RECORD_FIELDS if not state["record"].get(name)]}

    def score_by_rubric(state: BriefState) -> dict:
        out, usage = run_model(
            "score_by_rubric",
            state,
            {
                "brief_text": state["brief_text"],
                "record": state["record"],
                "gaps": state["gaps"],
                "faults": state.get("faults", []),
            },
            skill_text,
        )
        return {
            "scores": out["scores"],
            "decision": out["decision"],
            "decision_reason": out["decision_reason"],
            **usage,
        }

    def draft_assessment(state: BriefState) -> dict:
        out, usage = run_model(
            "draft_assessment",
            state,
            {
                "brief_text": state["brief_text"],
                "record": state["record"],
                "gaps": state["gaps"],
                "scores": state["scores"],
                "decision": state["decision"],
                "decision_reason": state["decision_reason"],
            },
            skill_text,
        )
        return {"note": out["note"], "questions": out["questions"], **usage}

    def verify_evidence(state: BriefState) -> dict:
        # String match first, without a model: an invented quote goes straight back.
        faults = unmatched_quotes(state["scores"], state["brief_text"])
        usage: dict = {}
        if not faults:
            out, usage = run_model(
                "verify_evidence",
                state,
                {"brief_text": state["brief_text"], "scores": state["scores"]},
                skill_text,
            )
            faults = [
                f"{v['criterion']}: score does not follow from its evidence: {v['reason']}"
                for v in out["verdicts"]
                if not v["follows"]
            ]
        returns = state.get("verify_returns", 0)
        return {"faults": faults, "verify_returns": returns + 1 if faults else returns, **usage}

    def after_verify(state: BriefState) -> str:
        if state["faults"] and state["verify_returns"] <= MAX_VERIFY_RETURNS:
            return "score_by_rubric"
        # Clean, or the limit is reached and the faults go to the owner.
        return "draft_reply" if (state.get("inputs") or {}).get("reply_to") else "approval"

    approval = make_approval_node(h.db, assessment_payload)

    def draft_reply(state: BriefState) -> dict:
        to = state["inputs"]["reply_to"]
        # The brief itself is not sent to this step: the reply is built from the decision and the questions.
        out, usage = steps.run_model(h, "draft_brief_reply", state, {
            "first_name": to["first_name"], "project": to["project"], "owner": to["owner"],
            "decision": state["decision"], "decision_reason": state["decision_reason"],
            "questions": state.get("questions", []), "booking_url": to.get("booking_url"),
        }, "", [])
        return {"reply_subject": out["subject"], "reply_body": out["body"], **usage}

    def reply_approval(state: BriefState) -> dict:
        """One pause for the owner: approve the card, edit the reply then approve, or reject."""
        payload = card_payload(state)
        to = state["inputs"]["reply_to"]
        notes = [f"Approving files the assessment to the {state['client']} folder and sends this reply to {to['email']}.",
                 f"Recommendation: {_DECISION_LABELS[state['decision']]}. {state['decision_reason']}"]
        notes += [f"Open fault: {fault}" for fault in state.get("faults", [])]
        resume = interrupt({
            "type": "approval", "content": payload, "content_hash": content_hash(payload),
            "error": state.get("approval_error"), "action": "brief_reply", "stage": "reply", "editable": True,
            "notes": notes + list(state.get("flags", [])),
        })
        rec = h.db.get_approval(resume["approval_id"])
        edited = resume.get("edited_body")
        approved = {**payload, "body": edited} if edited is not None else payload
        if rec and not rec["lapsed"] and rec["decision"] == "approved" and rec["content_hash"] == content_hash(approved):
            return {"approved_hash": rec["content_hash"], "approval_error": None,
                    **({"reply_body": edited} if edited is not None else {})}
        if rec:
            h.db.lapse_approval(rec["id"])
            h.db.audit(state["run_id"], "approval_lapsed", {
                "approval_id": rec["id"], "note": "content no longer matches the approved hash; owner must approve again",
            })
        return {"approval_error": "approval does not match the current content; approve again"}

    def send_reply(state: BriefState) -> dict:
        if content_hash(card_payload(state)) != state.get("approved_hash"):
            return {"approval_error": "content changed after approval; approve again"}
        mail = reply_mail(state)
        # The owner approved this exact reply inside the card; the mail's own approval is derived from hers.
        h.db.record_approval(state["run_id"], content_hash(mail), "approved",
                             f"Part of the brief card approved as {state['approved_hash'][:12]}")
        try:
            result = h.tools.call(state["run_id"], "send_reply", ["send_mail"], "send_mail", mail)
        except (ApprovalRequired, ApprovalLapsed) as e:
            return {"approval_error": str(e)}
        except UncertainOutcome as e:  # never retried: it may have gone
            return {"reply_result": {"sent": None, "note": str(e)}, "approval_error": None}
        return {"reply_result": {"sent": bool(result.get("sent")), "note": result.get("note")},
                "approval_error": None, "error": None}

    def after_approval(state: BriefState) -> str:
        return "approval" if state.get("approval_error") else "file_assessment"

    def file_assessment(state: BriefState) -> dict:
        payload = h.contracts["file_assessment"].validate_input(assessment_payload(state))
        if content_hash(approval_content(state)) != state.get("approved_hash"):
            return {"approval_error": "content changed after approval; approve again"}
        filed = h.tools.call(
            state["run_id"],
            "file_assessment",
            h.contracts["file_assessment"].allowed_tools,
            "write_client_file",
            {
                "client": state["client"],
                "filename": f"brief-assessment-{state['run_id'][:8]}.md",
                "text": payload["body"],
            },
        )
        return {"filed": filed, "approval_error": None, "error": None}

    def after_file(state: BriefState) -> str:
        if state.get("approval_error"):
            return "reply_approval" if state.get("reply_body") else "approval"
        return "send_reply" if state.get("reply_body") else END

    builder = StateGraph(BriefState)
    builder.add_node("receive_brief", receive_brief)
    builder.add_node("extract_fields", extract_fields)
    builder.add_node("check_gaps", check_gaps)
    builder.add_node("score_by_rubric", score_by_rubric)
    builder.add_node("draft_assessment", draft_assessment)
    builder.add_node("verify_evidence", verify_evidence)
    builder.add_node("approval", approval)
    builder.add_node("file_assessment", file_assessment)
    builder.add_node("draft_reply", draft_reply)
    builder.add_node("reply_approval", reply_approval)
    builder.add_node("send_reply", send_reply)
    builder.add_edge(START, "receive_brief")
    builder.add_edge("receive_brief", "extract_fields")
    builder.add_edge("extract_fields", "check_gaps")
    builder.add_edge("check_gaps", "score_by_rubric")
    builder.add_edge("score_by_rubric", "draft_assessment")
    builder.add_edge("draft_assessment", "verify_evidence")
    builder.add_conditional_edges(
        "verify_evidence", after_verify,
        {"score_by_rubric": "score_by_rubric", "approval": "approval", "draft_reply": "draft_reply"},
    )
    builder.add_edge("draft_reply", "reply_approval")
    builder.add_conditional_edges(
        "reply_approval", lambda s: "reply_approval" if s.get("approval_error") else "file_assessment",
        {"reply_approval": "reply_approval", "file_assessment": "file_assessment"},
    )
    builder.add_conditional_edges(
        "send_reply", lambda s: "reply_approval" if s.get("approval_error") else END,
        {"reply_approval": "reply_approval", END: END},
    )
    builder.add_conditional_edges(
        "approval", after_approval, {"approval": "approval", "file_assessment": "file_assessment"}
    )
    builder.add_conditional_edges("file_assessment", after_file,
                                  {"approval": "approval", "reply_approval": "reply_approval", "send_reply": "send_reply", END: END})
    return builder
