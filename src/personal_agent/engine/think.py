"""Think: the owner asks a question and reads an answer.

    answer (model, at the depth the owner chose) -> done

Nothing leaves the system, so there is no approval step. The question is the
owner's own instruction. Pasted material is outside text: it travels as marked
data, and instruction-like text inside it is flagged and never obeyed. The model
has no tools here: no web, no files. The instruction says so, and asks it to
list what it assumed.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

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
                "You have no web access and no access to files, so never present a guess about a current fact as "
                "known. Lead with the answer, then the reasoning. Respond as a single JSON object with keys "
                "answer (plain text or Markdown) and caveats (a list: each assumption you made and each thing "
                "you could not check; empty if none)."
            ),
        )
    }


def think_outputs(state: dict) -> dict | None:
    if not state.get("answer"):
        return None
    return {"answer": state["answer"], "caveats": state.get("caveats", [])}


def build_think_graph(h):
    def think(state: ThinkState) -> dict:
        inputs = state["inputs"] or {}
        depth = inputs.get("depth") if inputs.get("depth") in DEPTHS else "considered"
        tier = DEPTHS[depth]
        if tier not in h.settings.route_table:  # a route table without that tier: use what it has
            tier = "standard" if "standard" in h.settings.route_table else next(iter(h.settings.route_table), tier)
        base = h.contracts["think"]
        contract = base.model_copy(update={"tier": tier, "instruction": base.instruction + MODES.get(inputs.get("mode"), "")})
        material = inputs.get("material") or ""
        payload = {"question": inputs.get("question", ""), "material": material}
        tracker = BudgetTracker(h.settings.budgets)
        context, flags = build_context(
            ROLE + h.standing(state["client"]), "", [], [("pasted material", material)] if material else [],
            {"question": payload["question"], "client": state["client"]},
        )
        runner = h.node_runner(tracker, state["run_id"])
        out = runner.run(contract, payload, context=context)
        return {
            "answer": out["answer"],
            "caveats": out.get("caveats", []),
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
