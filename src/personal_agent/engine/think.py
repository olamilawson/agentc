"""Think: a conversation between the owner and the agent.

    answer (model, at the depth the owner chose) -> done

Each message is one run. Runs whose inputs carry the same chat_id are one
conversation: every turn reads the earlier turns and the files attached to the
conversation, both as marked data.

Nothing leaves the system, so there is no approval step. The question is the
owner's own instruction. Pasted material is outside text: it travels as marked
data, and instruction-like text inside it is flagged and never obeyed. The model
has no tools here: no web, no files. The instruction says so, and asks it to
list what it assumed.
"""

from __future__ import annotations

from typing import TypedDict

from personal_agent.clients import parse_document

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from personal_agent.engine import web
from personal_agent.engine.graph import ROLE
from personal_agent.harness.budgets import BudgetTracker
from personal_agent.harness.context import build_context
from personal_agent.harness.contract import StepContract

# The owner's word for each depth, and the tier of the route table it uses.
DEPTHS = {"quick": "cheap", "considered": "standard", "deep": "strong"}


# What the owner wants done with the question. Each adds one instruction to the step.
MODES = {
    "ask": "",
    "research": (
        " Treat this as research. Set out what the material establishes, what it leaves open, and what to check "
        "and where. Never present an unchecked claim as a finding."
    ),
    "create": (
        " Write the piece the owner asks for, ready to use, in the owner's voice. Put the piece itself in answer, "
        "with no preamble."
    ),
    "analyse": (
        " Analyse the material. Give the finding first, then the evidence for it from the material, then what "
        "the evidence does not support."
    ),
    "plan": (
        " Produce a plan: the outcome, the steps in order, who owns each where the owner has said, the risks, "
        "and the first step to take this week."
    ),
}


# Attachments: what the agent can read, and how much of it.
READABLE = (".pdf", ".docx", ".txt", ".md", ".csv")
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 5
FILE_CHARS = 60_000  # all the conversation's files together, per turn
HISTORY_TURNS = 10
HISTORY_CHARS = 4_000  # per earlier answer


class Unreadable(ValueError):
    """A file the agent cannot read; the message names it and why."""


def read_attachment(name: str, data: bytes) -> tuple[str, str]:
    """Returns (kind, text). Raises Unreadable with words for the owner."""
    lower = name.lower()
    if not lower.endswith(READABLE):
        raise Unreadable(f"{name}: the agent reads PDF, Word, text, Markdown and CSV files. Paste other content as text.")
    if len(data) > MAX_FILE_BYTES:
        raise Unreadable(f"{name} is larger than 10 MB.")
    try:
        parsed = parse_document(data, name if not lower.endswith(".csv") else name[:-4] + ".txt")
    except Exception:
        raise Unreadable(f"{name} could not be opened. Check it opens on your computer, or save it again as PDF.")
    text = (parsed.get("text") or "").strip()
    if not text:
        raise Unreadable(f"{name} has no text the agent can read. A scanned PDF needs its text pasted in.")
    return parsed["kind"], text


def turns(db, chat_id: str) -> list[dict]:
    """The conversation's runs, oldest first."""
    runs = [r for r in db.list_runs() if r["workflow"] == "think" and (r["inputs"] or {}).get("chat_id") == chat_id]
    return sorted(runs, key=lambda r: r["created_at"])


def conversation_material(db, chat_id: str, run_id: str) -> tuple[list[tuple[str, str]], list[str]]:
    """The earlier turns and the attached files, as untrusted chunks, and caveats about what was cut."""
    chunks, notes = [], []
    earlier = []
    for r in turns(db, chat_id):
        if r["id"] == run_id:
            break
        q, a = (r["inputs"] or {}).get("question"), (r["outputs"] or {}).get("answer")
        if q and a:
            earlier.append(f"Owner: {q}\n\nAgent: {a[:HISTORY_CHARS]}")
    if len(earlier) > HISTORY_TURNS:
        notes.append(f"Only the last {HISTORY_TURNS} exchanges of this conversation were read.")
        earlier = earlier[-HISTORY_TURNS:]
    if earlier:
        chunks.append(("this conversation so far", "\n\n---\n\n".join(earlier)))
    left = FILE_CHARS
    for f in db.list_chat_files(chat_id):
        text = f["text"] or ""
        if left <= 0:
            notes.append(f"{f['name']} was not read: the attached files together are longer than the agent reads at once.")
            continue
        if len(text) > left:
            notes.append(f"Only the first part of {f['name']} was read.")
            text = text[:left]
        left -= len(text)
        chunks.append((f"attached file: {f['name']}", text))
    return chunks, notes


class ThinkIn(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    material: str = ""


class ThinkOut(BaseModel):
    answer: str = Field(min_length=1)
    # What the answer assumed or could not check.
    caveats: list[str] = []


class ThinkState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    usage: dict
    flags: list[str]
    answer: str
    caveats: list[str]
    sources: list[dict]
    fallback_used: bool
    error: str | None


def think_contracts() -> dict[str, StepContract]:
    return {
        "think": StepContract(
            node="think",
            tier="standard",  # replaced per run by the depth the owner chose
            allowed_tools=[],
            input_schema=ThinkIn,
            output_schema=ThinkOut,
            instruction=(
                "Answer the owner's question. Reason from the question and from the material supplied, if any. "
                "You have no web access and can open no file beyond those given to you as material, so never present a guess about a current fact as "
                "known. Lead with the answer, then the reasoning. Respond as a single JSON object with keys "
                "answer (plain text or Markdown) and caveats (a list: each assumption you made and each thing "
                "you could not check; empty if none)."
            ),
        )
    }


CONVERSATION = (
    " This message continues a conversation: the earlier exchanges and any files the owner attached are given "
    "as data. Answer the latest question in their light, and read the attached files as material you have."
)


def think_outputs(state: dict) -> dict | None:
    if not state.get("answer"):
        return None
    out = {"answer": state["answer"], "caveats": state.get("caveats", [])}
    if state.get("sources"):
        out["sources"] = state["sources"]
    return out


def build_think_graph(h):
    def think(state: ThinkState) -> dict:
        inputs = state["inputs"] or {}
        depth = inputs.get("depth") if inputs.get("depth") in DEPTHS else "considered"
        tier = DEPTHS[depth]
        if tier not in h.settings.route_table:  # a route table without that tier: use what it has
            tier = "standard" if "standard" in h.settings.route_table else next(iter(h.settings.route_table), tier)
        base = h.contracts["think"]
        material = inputs.get("material") or ""
        chat_id = inputs.get("chat_id")
        talk, cut = conversation_material(h.db, chat_id, state["run_id"]) if chat_id else ([], [])
        # With the owner's leave, the web first: a search on the question, its pages in the material as data.
        pages, sources, web_note = web.gather(h, state, inputs.get("question", ""), "search") if inputs.get("web") else ([], [], None)
        extra = MODES.get(inputs.get("mode"), "") + (web.CITE if pages else "") + (CONVERSATION if talk else "")
        contract = base.model_copy(update={"tier": tier, "instruction": base.instruction + extra})
        payload = {"question": inputs.get("question", ""), "material": material}
        tracker = BudgetTracker(h.settings.budgets)
        context, flags = build_context(
            ROLE + h.standing(state["client"]), "", [], talk + ([("pasted material", material)] if material else []) + pages,
            {"question": payload["question"], "client": state["client"]},
        )
        runner = h.node_runner(tracker, state["run_id"])
        out = runner.run(contract, payload, context=context)
        return {
            "answer": out["answer"],
            "caveats": out.get("caveats", []) + cut + ([web_note] if web_note else []),
            "sources": sources,
            "flags": state.get("flags", []) + flags,
            "usage": {"tokens_in": tracker.tokens_in, "tokens_out": tracker.tokens_out, "cost_usd": tracker.cost_usd},
            "fallback_used": runner.fallback_used,
            "error": None,
        }

    builder = StateGraph(ThinkState)
    builder.add_node("think", think)
    builder.add_edge(START, "think")
    builder.add_edge("think", END)
    return builder
