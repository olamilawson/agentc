"""The strategy and brief drafting graph (PRD: workflow graphs / strategy and brief drafting).

    intake (code) -> questions (model; waits for the owner if anything required is missing)
        -> outline (model) -> outline approval (interrupt)
        -> draft, one section at a time (model) -> critique (code figures, then a separate
           model call) -> revise (model) -> critique ... twice at most
        -> owner review (interrupt) -> save to the client folder

Drafting pauses for the owner twice, once after the outline and once at the
end, because direction set late is expensive to change. At either pause the
owner approves, edits then approves, or sends it back with a comment, which
starts a new pass. The difference between a draft and what the owner approved
is recorded for the learning loop.

The templates and the writing standard live in skills/strategy-drafting/SKILL.md.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from personal_agent.engine import steps
from personal_agent.engine.graph import ROLE
from personal_agent.gateway.tools import content_hash
from personal_agent.harness.context import build_context
from personal_agent.harness.contract import StepContract, StepFailed

SKILL = "strategy-drafting"
MAX_REVISIONS = 2  # PRD: critique and revision run twice at most
MAX_SOURCE_FILES = 10
MAX_SOURCE_CHARS = 24_000  # across all sources sent with a call


# --- schemas -------------------------------------------------------------------
class DraftingIn(BaseModel):
    goal: str = Field(min_length=1)
    document_type: str = "strategy"  # names a template in the skill
    audience: str | None = None  # who will read the document
    source_paths: list[str] = []  # files in the client folder; empty means the folder


class QuestionsIn(BaseModel):
    goal: str
    document_type: str
    audience: str | None = None


class QuestionsOut(BaseModel):
    questions: list[str] = Field(default=[], max_length=5)


class Section(BaseModel):
    heading: str = Field(min_length=1, max_length=120)
    purpose: str = ""


class OutlineIn(BaseModel):
    goal: str
    document_type: str
    audience: str | None = None
    questions: list[str] = []
    answers: str = ""
    previous_outline: list[dict] = []
    owner_comment: str = ""


class OutlineOut(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    sections: list[Section] = Field(min_length=2, max_length=12)


class SectionIn(BaseModel):
    goal: str
    document_type: str
    audience: str | None = None
    answers: str = ""
    title: str
    outline: list[dict]
    section_number: int
    heading: str
    purpose: str
    previous_sections: list[dict] = []


class SectionOut(BaseModel):
    body: str = Field(min_length=1)


class CritiqueIn(BaseModel):
    goal: str
    document_type: str
    audience: str | None = None
    answers: str = ""
    title: str
    sections: list[dict]  # number, heading, purpose, body


class Fault(BaseModel):
    section: int = Field(ge=1)
    fault: str = Field(min_length=1)


class CritiqueOut(BaseModel):
    faults: list[Fault] = []


class ReviseIn(BaseModel):
    goal: str
    document_type: str
    audience: str | None = None
    answers: str = ""
    title: str
    outline: list[dict]
    section_number: int
    heading: str
    purpose: str
    current_body: str
    faults: list[str] = Field(min_length=1)


class SaveIn(BaseModel):
    subject: str
    body: str


def _instruction(text: str, schema: type[BaseModel]) -> str:
    return f"{text}\n\nOutput schema: {json.dumps(schema.model_json_schema(), sort_keys=True)}"


def drafting_contracts() -> dict[str, StepContract]:
    code = dict(tier="cheap", instruction="")  # code nodes run without a model; the tier is unused
    return {
        "intake": StepContract(
            node="intake", allowed_tools=["list_client_files", "read_client_file"],
            input_schema=DraftingIn, output_schema=DraftingIn, **code,
        ),
        "questions": StepContract(
            node="questions",
            tier="standard",
            input_schema=QuestionsIn,
            output_schema=QuestionsOut,
            instruction=_instruction(
                "Decide whether anything the template requires for this document type is missing "
                "from the goal and the sources. Ask for it in at most five questions, each "
                "answerable in a line. If nothing required is missing, return no questions.",
                QuestionsOut,
            ),
        ),
        "outline": StepContract(
            node="outline",
            tier="standard",
            input_schema=OutlineIn,
            output_schema=OutlineOut,
            instruction=_instruction(
                "Propose the document's title and structure: one line per section, following the "
                "template for the document type. Each section has a heading and a one-line purpose "
                "saying what the section will establish for this goal. If the inputs carry the "
                "owner's comment on a previous outline, change the outline to meet it.",
                OutlineOut,
            ),
        ),
        "draft_section": StepContract(
            node="draft_section",
            tier="standard",
            limits={"max_tokens": 3000},
            input_schema=SectionIn,
            output_schema=SectionOut,
            instruction=_instruction(
                "Write the one section named in the inputs, to the writing standard, so that it "
                "does the job its purpose line sets and does not repeat the previous sections. "
                "Take facts only from the sources and the owner's answers; where a needed fact is "
                "missing write [to confirm: ...]. Return the section body without its heading.",
                SectionOut,
            ),
        ),
        "critique": StepContract(
            node="critique",
            tier="standard",
            limits={"max_tokens": 3000},
            input_schema=CritiqueIn,
            output_schema=CritiqueOut,
            instruction=_instruction(
                "You are reviewing a draft you did not write. Check each section against the stated "
                "goal, the evidence in the sources and the owner's answers, and the writing "
                "standard. List each fault with its section number and one line saying what is "
                "wrong. List nothing that is a matter of taste. No faults is a valid answer.",
                CritiqueOut,
            ),
        ),
        "revise": StepContract(
            node="revise",
            tier="standard",
            limits={"max_tokens": 3000},
            input_schema=ReviseIn,
            output_schema=SectionOut,
            instruction=_instruction(
                "Rewrite the one section in the inputs so that every listed fault is fixed and "
                "nothing else changes without reason. Take facts only from the sources and the "
                "owner's answers. Return the section body without its heading.",
                SectionOut,
            ),
        ),
        "save": StepContract(
            node="save", allowed_tools=["write_client_file"],
            input_schema=SaveIn, output_schema=SaveIn, **code,
        ),
    }


# --- text helpers (code) -----------------------------------------------------------
def outline_text(sections: list[dict]) -> str:
    return "\n".join(
        f"{i}. {s['heading']}" + (f": {s['purpose']}" if s.get("purpose") else "")
        for i, s in enumerate(sections, 1)
    ) + "\n"


def parse_outline(text: str) -> list[dict]:
    """Sections from an outline the owner edited: one line each, 'Heading: purpose'."""
    sections = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line).strip()
        if not line:
            continue
        heading, _, purpose = line.partition(": ")
        sections.append({"heading": heading.strip()[:120], "purpose": purpose.strip()})
    return sections


def document_text(title: str, sections: list[dict], bodies: list[str]) -> str:
    parts = [f"# {title}"]
    for section, body in zip(sections, bodies):
        parts.append(f"## {section['heading']}\n\n{body.strip()}")
    return "\n\n".join(parts) + "\n"


def split_document(text: str) -> dict[str, str]:
    """{heading: body} for each '## ' section of a document."""
    out, heading, lines = {}, None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading is not None:
                out[heading] = "\n".join(lines).strip()
            heading, lines = line[3:].strip(), []
        elif heading is not None:
            lines.append(line)
    if heading is not None:
        out[heading] = "\n".join(lines).strip()
    return out


def edit_rate_by_section(draft: str, approved: str) -> dict[str, float]:
    """How much of each section the owner changed: 0 untouched, 1 replaced or
    removed. A section the owner added is 1 under its own heading."""
    before, after = split_document(draft), split_document(approved)
    rates = {}
    for heading in list(before) + [h for h in after if h not in before]:
        a, b = before.get(heading), after.get(heading)
        rates[heading] = 1.0 if a is None or b is None else round(1 - difflib.SequenceMatcher(None, a, b).ratio(), 3)
    return rates


_FIGURE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)


def _figures(text: str) -> set[str]:
    found = {n.replace(",", "").rstrip(".") for n in _FIGURE.findall(_LIST_MARKER.sub("", text))}
    return {n for n in found if len(n.replace(".", "")) >= 2}  # single digits are rarely a claim


def unsupported_figures(bodies: list[str], evidence: str) -> list[dict]:
    """Faults for figures in a section that appear nowhere in the goal, the
    owner's answers or the sources."""
    known = _figures(evidence)
    return [
        {"section": i, "fault": "states a figure found nowhere in the goal, the answers or the sources: "
                               + ", ".join(sorted(_figures(body) - known))}
        for i, body in enumerate(bodies, 1)
        if _figures(body) - known
    ]


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "document"


# --- what the owner sees -------------------------------------------------------------
def _outline_payload(state: dict) -> dict:
    return {"subject": f"Outline: {state['title']}", "body": outline_text(state["sections"])}


def _outline_notes(state: dict) -> dict:
    notes = [f"Goal: {state['goal']}", "One line per section. Edit the lines, or send back with a comment."]
    return {"action": "outline", "stage": "outline", "editable": True, "notes": notes + list(state.get("flags", []))}


def _final_payload(state: dict) -> dict:
    return {"subject": state["title"], "body": document_text(state["title"], state["sections"], state["drafted"])}


def _final_notes(state: dict) -> dict:
    headings = [s["heading"] for s in state["sections"]]
    notes = [f"Goal: {state['goal']}"]
    notes += [f"Open fault, {headings[f['section'] - 1]}: {f['fault']}" for f in state.get("faults", [])]
    return {"action": "save", "stage": "final", "editable": True, "notes": notes + list(state.get("flags", []))}


def drafting_outputs(state: dict) -> dict | None:
    """Run-record outputs; partial work is kept when a run stops early."""
    if not state.get("goal"):
        return None
    out = {
        "goal": state["goal"],
        "sources": [s["path"] for s in state.get("sources", [])],
        "questions": state.get("questions", []),
        "answers": state.get("answers"),
        "title": state.get("title"),
        "sections": state.get("sections", []),
        "sections_drafted": len(state.get("drafted", [])),
        "faults": state.get("faults", []),
        "revisions": state.get("revisions", 0),
        "edit_rate": state.get("edit_rate"),
        "filed": state.get("filed"),
    }
    if state.get("drafted"):
        out["document"] = state.get("final_body") or document_text(
            state["title"], state["sections"], state["drafted"]
        )
    return out


# --- the graph ---------------------------------------------------------------------
class DraftingState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    usage: dict
    flags: list[str]
    fallback_used: bool
    goal: str
    document_type: str
    audience: str | None
    sources: list[dict]
    questions: list[str]
    answers: str
    title: str
    sections: list[dict]
    owner_comment: str
    drafted: list[str]
    faults: list[dict]
    revisions: int
    gate: str
    approval_error: str | None
    approved_hash: str
    final_body: str
    edit_rate: dict
    filed: dict
    error: str | None


def build_drafting_graph(h):
    """h: engine.runner.Harness — kept untyped here to avoid an import cycle."""
    db = h.db
    skill_text = h.skills.get(SKILL, "")

    def untrusted(state: DraftingState) -> list[tuple[str, str]]:
        return [(f"source: {s['path']}", s["text"]) for s in state.get("sources", [])]

    def base(state: DraftingState) -> dict:
        return {"goal": state["goal"], "document_type": state["document_type"], "audience": state.get("audience")}

    def call(node: str, state: dict, payload: dict) -> tuple[dict, dict]:
        return steps.run_model(h, node, state, payload, skill_text, untrusted(state))

    def owner_gate(state: DraftingState, payload: dict, about: dict) -> dict:
        """One pause for the owner: approve, edit then approve, or send back.
        An approval matches the exact content, edited or not, by hash."""
        p_hash = content_hash(payload)
        resume = interrupt({
            "type": "approval", "content": payload, "content_hash": p_hash,
            "error": state.get("approval_error"), **about,
        })
        rec = db.get_approval(resume["approval_id"])
        if rec and not rec["lapsed"] and rec["decision"] == "sent_back":
            return {"gate": "sent_back", "owner_comment": rec["comment"] or "", "approval_error": None}
        edited = resume.get("edited_body")
        approved = {**payload, "body": edited} if edited is not None else payload
        if rec and not rec["lapsed"] and rec["decision"] == "approved" and rec["content_hash"] == content_hash(approved):
            return {"gate": "approved", "approved_hash": rec["content_hash"], "approval_error": None,
                    "_edited": edited}
        if rec:
            db.lapse_approval(rec["id"])
            db.audit(state["run_id"], "approval_lapsed", {
                "approval_id": rec["id"],
                "note": "content no longer matches the approved hash; owner must approve again",
            })
        return {"gate": "retry", "approval_error": "approval does not match the current content; approve again"}

    def intake(state: DraftingState) -> dict:
        # Code node: the owner's goal, and the source documents from the client folder.
        inputs = h.contracts["intake"].validate_input(state.get("inputs") or {})
        tools = h.contracts["intake"].allowed_tools
        flags = list(state.get("flags", []))
        paths = inputs["source_paths"]
        if not paths:  # the whole folder, within a limit
            listed = h.tools.call(state["run_id"], "intake", tools, "list_client_files", {"client": state["client"]})
            paths = [f["path"] for f in listed["files"]]
            if len(paths) > MAX_SOURCE_FILES:
                flags.append(f"sources: the folder has {len(paths)} files; only the first {MAX_SOURCE_FILES} were read")
                paths = paths[:MAX_SOURCE_FILES]
        sources = []
        for path in paths:
            try:
                doc = h.tools.call(
                    state["run_id"], "intake", tools, "read_client_file", {"client": state["client"], "path": path}
                )
            except ValueError as e:  # a file type the parser does not read
                if inputs["source_paths"]:
                    raise StepFailed(f"node 'intake' cannot read a named source: {e}") from e
                flags.append(f"sources: skipped {path} (unsupported type)")
                continue
            sources.append({"path": path, "text": doc["text"]})
        share = MAX_SOURCE_CHARS // max(1, len(sources))
        for s in sources:
            if len(s["text"]) > share:
                s["text"] = s["text"][:share]
                flags.append(f"sources: {s['path']} was shortened to fit; the draft saw only its first part")
        if not sources:
            flags.append("sources: none; the draft rests on the goal and the owner's answers alone")
        _, found = build_context(ROLE, "", [], [(f"source: {s['path']}", s["text"]) for s in sources], {})
        return {
            "goal": inputs["goal"],
            "document_type": inputs["document_type"],
            "audience": inputs["audience"],
            "sources": sources,
            "flags": flags + found,
        }

    def questions(state: DraftingState) -> dict:
        out, usage = call("questions", state, base(state))
        return {"questions": out["questions"], **usage}

    def after_questions(state: DraftingState) -> str:
        return "await_answers" if state["questions"] else "outline"

    def await_answers(state: DraftingState) -> dict:
        # The run waits for input; it resumes with the owner's answers as given.
        resume = interrupt({"type": "input", "questions": state["questions"], "goal": state["goal"]})
        return {"answers": str(resume.get("answers", "")).strip()}

    def outline(state: DraftingState) -> dict:
        out, usage = call("outline", state, {
            **base(state),
            "questions": state.get("questions", []),
            "answers": state.get("answers", ""),
            "previous_outline": state.get("sections", []) if state.get("owner_comment") else [],
            "owner_comment": state.get("owner_comment", ""),
        })
        return {"title": out["title"], "sections": out["sections"], "owner_comment": "", **usage}

    def outline_approval(state: DraftingState) -> dict:
        out = owner_gate(state, _outline_payload(state), _outline_notes(state))
        edited = out.pop("_edited", None)
        if out["gate"] == "approved":
            # An outline approved as shown keeps its sections; an edited one is read line by line.
            sections = state["sections"] if edited is None else parse_outline(edited)
            if len(sections) < 2:
                return {"gate": "retry", "approval_error": "an outline needs at least two sections, one per line"}
            out.update({"sections": sections, "drafted": [], "faults": [], "revisions": 0})
        return out

    def after_outline_approval(state: DraftingState) -> str:
        return {"approved": "draft_section", "sent_back": "outline"}.get(state["gate"], "outline_approval")

    def draft_section(state: DraftingState) -> dict:
        # One section per step, so each finished section is checkpointed.
        done = state.get("drafted", [])
        section = state["sections"][len(done)]
        earlier = [{"heading": s["heading"], "body": b} for s, b in zip(state["sections"], done)]
        out, usage = call("draft_section", state, {
            **base(state),
            "answers": state.get("answers", ""),
            "title": state["title"],
            "outline": state["sections"],
            "section_number": len(done) + 1,
            "heading": section["heading"],
            "purpose": section.get("purpose", ""),
            "previous_sections": earlier[-3:],  # enough to avoid repetition without resending the document
        })
        return {"drafted": done + [out["body"]], **usage}

    def after_draft(state: DraftingState) -> str:
        return "draft_section" if len(state["drafted"]) < len(state["sections"]) else "critique"

    def numbered(state: DraftingState) -> list[dict]:
        return [
            {"number": i, "heading": s["heading"], "purpose": s.get("purpose", ""), "body": body}
            for i, (s, body) in enumerate(zip(state["sections"], state["drafted"]), 1)
        ]

    def critique(state: DraftingState) -> dict:
        # Figures first, in code; then a separate model call reads the draft cold.
        evidence = "\n".join(
            [state["goal"], state.get("answers", ""), state.get("owner_comment", ""),
             *(s["text"] for s in state.get("sources", []))]
        )
        faults = unsupported_figures(state["drafted"], evidence)
        out, usage = call("critique", state, {
            **base(state), "answers": state.get("answers", ""), "title": state["title"], "sections": numbered(state),
        })
        count = len(state["sections"])
        faults += [f for f in out["faults"] if f["section"] <= count]
        return {"faults": faults, **usage}

    def after_critique(state: DraftingState) -> str:
        if state["faults"] and state.get("revisions", 0) < MAX_REVISIONS:
            return "revise"
        return "owner_review"  # clean, or the limit is reached and the faults go to the owner

    def revise(state: DraftingState) -> dict:
        bodies, current = list(state["drafted"]), dict(state)
        by_section: dict[int, list[str]] = {}
        for f in state["faults"]:
            by_section.setdefault(f["section"], []).append(f["fault"])
        for number, faults in sorted(by_section.items()):
            section = state["sections"][number - 1]
            out, usage = call("revise", current, {
                **base(state),
                "answers": state.get("answers", ""),
                "title": state["title"],
                "outline": state["sections"],
                "section_number": number,
                "heading": section["heading"],
                "purpose": section.get("purpose", ""),
                "current_body": bodies[number - 1],
                "faults": faults,
            })
            bodies[number - 1] = out["body"]
            current.update(usage)  # the next section's call starts from this one's usage
        return {
            "drafted": bodies,
            "revisions": state.get("revisions", 0) + 1,
            "usage": current.get("usage", {}),
            "fallback_used": current.get("fallback_used", False),
        }

    def owner_review(state: DraftingState) -> dict:
        out = owner_gate(state, _final_payload(state), _final_notes(state))
        edited = out.pop("_edited", None)
        if out["gate"] == "approved":
            draft = _final_payload(state)["body"]
            final = draft if edited is None else edited
            out.update({"final_body": final, "edit_rate": edit_rate_by_section(draft, final)})
        elif out["gate"] == "sent_back":
            # A revision request starts a new pass: the owner's comment is a fault on every section.
            comment = out["owner_comment"] or "The owner sent this back without a comment; tighten it."
            out.update({
                "faults": [{"section": i, "fault": f"Owner: {comment}"} for i in range(1, len(state["sections"]) + 1)],
                "revisions": 0,
            })
        return out

    def after_owner_review(state: DraftingState) -> str:
        return {"approved": "save", "sent_back": "revise"}.get(state["gate"], "owner_review")

    def save(state: DraftingState) -> dict:
        payload = h.contracts["save"].validate_input({"subject": state["title"], "body": state["final_body"]})
        if content_hash(payload) != state.get("approved_hash"):
            return {"gate": "retry", "approval_error": "content changed after approval; approve again"}
        filed = h.tools.call(
            state["run_id"], "save", h.contracts["save"].allowed_tools, "write_client_file",
            {
                "client": state["client"],
                "filename": f"{_slug(state['title'])}-{state['run_id'][:8]}.md",
                "text": payload["body"],
            },
        )
        return {"filed": filed, "approval_error": None, "error": None}

    def after_save(state: DraftingState) -> str:
        return "owner_review" if state.get("approval_error") else END

    builder = StateGraph(DraftingState)
    for name, fn in [
        ("intake", intake), ("questions", questions), ("await_answers", await_answers),
        ("outline", outline), ("outline_approval", outline_approval), ("draft_section", draft_section),
        ("critique", critique), ("revise", revise), ("owner_review", owner_review), ("save", save),
    ]:
        builder.add_node(name, fn)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "questions")
    builder.add_conditional_edges("questions", after_questions, {"await_answers": "await_answers", "outline": "outline"})
    builder.add_edge("await_answers", "outline")
    builder.add_edge("outline", "outline_approval")
    builder.add_conditional_edges(
        "outline_approval", after_outline_approval,
        {"draft_section": "draft_section", "outline": "outline", "outline_approval": "outline_approval"},
    )
    builder.add_conditional_edges(
        "draft_section", after_draft, {"draft_section": "draft_section", "critique": "critique"}
    )
    builder.add_conditional_edges("critique", after_critique, {"revise": "revise", "owner_review": "owner_review"})
    builder.add_edge("revise", "critique")
    builder.add_conditional_edges(
        "owner_review", after_owner_review, {"save": "save", "revise": "revise", "owner_review": "owner_review"}
    )
    builder.add_conditional_edges("save", after_save, {"owner_review": "owner_review", END: END})
    return builder
