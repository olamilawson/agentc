"""Skill runs: one repeatable piece of work, done to a versioned rubric.

    intake (code) -> produce (model) -> owner review (interrupt) -> save -> done
                        ^                     |
                        +---- sent back ------+

Each skill in SKILLS is a workflow of its own name, so the weekly learning
review reads the owner's edits to it and proposes changes to its rubric
(skills/<name>/SKILL.md), like any other workflow. They share this one graph.

The owner gives a request and material: pasted text, files, or both. The
material travels as marked data. The agent has no web access here, and each
rubric says so. The result waits for the owner, who approves it, edits it then
approves, or sends it back with a comment. Approved, it is saved to the
workstream's folder. Nothing leaves the system.
"""

from __future__ import annotations

from functools import partial
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from personal_agent.engine import steps
from personal_agent.engine.strategy_drafting import _slug
from personal_agent.gateway.tools import content_hash
from personal_agent.harness.contract import Limits, StepContract, StepFailed

# The skill's folder name, and the name the owner sees.
SKILLS = {
    "campaign-review": "Campaign Review",
    "copy-review": "Copy Review",
    "executive-email": "Executive Email",
    "reputation-assessment": "Reputation Assessment",
    "competitor-analysis": "Competitor Analysis",
    "executive-update": "Executive Update",
    "proposal-review": "Proposal Review",
    "media-summary": "Media Summary",
    "research-synthesis": "Research Synthesis",
    "task-prioritisation": "Task Prioritisation",
    "follow-up-management": "Follow-up Management",
}
# These two work from the workstream's own open tasks, so they can run with nothing pasted.
TASK_SKILLS = {"task-prioritisation", "follow-up-management"}
MAX_REVISIONS = 3
MATERIAL_CHARS = 60_000


class ProduceIn(BaseModel):
    skill: str
    request: str = Field(min_length=1, max_length=8000)
    owner_comment: str = ""  # what the owner asked to change, on a pass after a send-back
    previous: str = ""  # the version the comment is about


class ProduceOut(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1)
    # What the work could not establish from the material, or assumed.
    gaps: list[str] = []


class SkillState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    usage: dict
    flags: list[str]
    fallback_used: bool
    material: list[dict]
    title: str
    body: str
    gaps: list[str]
    revisions: int
    gate: str
    owner_comment: str
    approval_error: str | None
    approved_hash: str
    filed: dict
    error: str | None


def skill_contracts() -> dict[str, StepContract]:
    return {
        "skill_produce": StepContract(
            node="skill_produce",
            tier="standard",
            allowed_tools=[],
            limits=Limits(max_tokens=4000),
            input_schema=ProduceIn,
            output_schema=ProduceOut,
            instruction=(
                "Do the work the rubric describes, for the owner's request, from the material supplied. Follow the "
                "rubric's structure. If owner_comment is set, revise the previous version to meet it and change "
                "nothing else. Respond as a single JSON object with keys title (a short name for the document), "
                "body (the work itself, in Markdown) and gaps (a list: each thing the material did not establish "
                "and each assumption you made; empty if none)."
            ),
        )
    }


def skill_outputs(state: dict) -> dict | None:
    if not state.get("body"):
        return None
    return {"subject": state.get("title"), "body": state["body"], "gaps": state.get("gaps", []), "filed": state.get("filed")}


def build_skill_graph(skill: str, h):
    db, label = h.db, SKILLS[skill]
    rubric = h.skills.get(skill, "")

    def intake(state: SkillState) -> dict:
        inputs = state["inputs"] or {}
        if not rubric:
            raise StepFailed(f"the {label} rubric is missing from the skill library, so the work was not started")
        material = []
        pasted = str(inputs.get("material") or "").strip()
        if pasted:
            material.append({"source": "pasted material", "text": pasted[:MATERIAL_CHARS]})
        if skill in TASK_SKILLS:
            # The owner's task list, as recorded. It goes in as data like any other material.
            open_tasks = [t for t in db.list_tasks() if t["workstream"] == state["client"] and t["status"] != "done"]
            listed = "\n".join(
                f"- {t['title']} | status: {t['status'].replace('_', ' ')} | risk: {t['risk'] or 'not set'} | "
                f"due: {t['due'] or 'no due time'} | owner: {t['owner'] or 'not set'}"
                + (f" | detail: {t['detail']}" if t["detail"] else "")
                for t in open_tasks
            )
            material.append({"source": "open tasks recorded for this workstream",
                             "text": listed or "No open task is recorded for this workstream."})
        if not material and not (inputs.get("source_paths") or []):
            raise StepFailed("there is no material to work from, so the work was not started")
        for path in inputs.get("source_paths") or []:
            read = h.tools.call(state["run_id"], "intake", ["read_client_file"], "read_client_file",
                                {"client": state["client"], "path": path})
            material.append({"source": f"file: {path}", "text": read["text"]})
        return {"material": material, "revisions": 0}

    def produce(state: SkillState) -> dict:
        inputs = state["inputs"] or {}
        payload = {"skill": label, "request": str(inputs.get("request") or ""),
                   "owner_comment": state.get("owner_comment", ""),
                   "previous": state.get("body", "") if state.get("owner_comment") else ""}
        out, usage = steps.run_model(
            h, "skill_produce", state, payload, rubric, [(m["source"], m["text"]) for m in state.get("material", [])],
        )
        return {**usage, "title": out["title"], "body": out["body"], "gaps": out.get("gaps", []),
                "owner_comment": "", "approval_error": None}

    def payload_of(state: SkillState) -> dict:
        return {"subject": f"{label}: {state['title']}", "body": state["body"]}

    def owner_review(state: SkillState) -> dict:
        """One pause for the owner: approve, edit then approve, or send back with a comment."""
        payload = payload_of(state)
        notes = [f"Not established or assumed: {gap}" for gap in state.get("gaps", [])]
        notes.append("Approving saves this to the workstream's folder. It sends nothing.")
        resume = interrupt({
            "type": "approval", "content": payload, "content_hash": content_hash(payload),
            "error": state.get("approval_error"), "action": "save", "stage": "final", "editable": True,
            "notes": notes + list(state.get("flags", [])),
        })
        rec = db.get_approval(resume["approval_id"])
        if rec and not rec["lapsed"] and rec["decision"] == "sent_back":
            if state.get("revisions", 0) >= MAX_REVISIONS:
                return {"gate": "retry", "approval_error": f"this has been revised {MAX_REVISIONS} times; edit it yourself, or reject it"}
            return {"gate": "sent_back", "owner_comment": rec["comment"] or "", "revisions": state.get("revisions", 0) + 1,
                    "approval_error": None}
        edited = resume.get("edited_body")
        approved = {**payload, "body": edited} if edited is not None else payload
        if rec and not rec["lapsed"] and rec["decision"] == "approved" and rec["content_hash"] == content_hash(approved):
            return {"gate": "approved", "approved_hash": rec["content_hash"], "approval_error": None,
                    **({"body": edited} if edited is not None else {})}
        if rec:
            db.lapse_approval(rec["id"])
            db.audit(state["run_id"], "approval_lapsed", {
                "approval_id": rec["id"], "note": "content no longer matches the approved hash; owner must approve again",
            })
        return {"gate": "retry", "approval_error": "approval does not match the current content; approve again"}

    def save(state: SkillState) -> dict:
        if content_hash(payload_of(state)) != state.get("approved_hash"):
            return {"gate": "retry", "approval_error": "content changed after approval; approve again"}
        filed = h.tools.call(
            state["run_id"], "save", ["write_client_file"], "write_client_file",
            {"client": state["client"], "filename": f"{skill}-{_slug(state['title'])}-{state['run_id'][:8]}.md",
             "text": state["body"]},
        )
        return {"filed": filed, "approval_error": None, "error": None}

    builder = StateGraph(SkillState)
    builder.add_node("intake", intake)
    builder.add_node("produce", produce)
    builder.add_node("owner_review", owner_review)
    builder.add_node("save", save)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "produce")
    builder.add_edge("produce", "owner_review")
    builder.add_conditional_edges(
        "owner_review", lambda s: {"approved": "save", "sent_back": "produce"}.get(s["gate"], "owner_review"),
        {"save": "save", "produce": "produce", "owner_review": "owner_review"},
    )
    builder.add_conditional_edges("save", lambda s: "owner_review" if s.get("approval_error") else END,
                                  {"owner_review": "owner_review", END: END})
    return builder


def builders() -> dict:
    """One graph builder for each skill, for the workflow table."""
    return {name: partial(build_skill_graph, name) for name in SKILLS}
