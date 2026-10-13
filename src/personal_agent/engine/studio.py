"""Studio: one thing made with a connector, at the owner's own request.

    make (code: one connector tool, through the gateway) -> done

There is no model step and no approval step. The owner asks for this one
thing on the Studio screen, the tool is draft-class, and what it makes stays
in the workstream's folder or in the owner's own Canva account. A refusal or a
failure from the service ends the run with the service's reason.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from personal_agent.harness.contract import StepFailed

WORKFLOW = "studio"
# The tool, and what the owner calls it.
TOOLS = {
    "higgsfield_generate_image": "Image", "higgsfield_generate_video": "Video",
    "canva_create_design": "Canva design", "canva_export_design": "Canva export",
}


class StudioState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    flags: list[str]
    result: dict
    error: str | None


def studio_outputs(state: dict) -> dict | None:
    if not state.get("result"):
        return None
    return {"tool": (state.get("inputs") or {}).get("tool"), "result": state["result"]}


def build_studio_graph(h):
    def make(state: StudioState) -> dict:
        inputs = state["inputs"] or {}
        tool = inputs.get("tool")
        if tool not in TOOLS:
            raise StepFailed(f"'{tool}' is not something the Studio makes")
        # The run's id goes with the request, so a second attempt at this run asks the service for the same thing once.
        payload = {"client": state["client"], "nonce": state["run_id"], **(inputs.get("params") or {})}
        try:
            result = h.tools.call(state["run_id"], "make", [tool], tool, payload)
        except Exception as e:  # the service's own reason, shown to the owner as it is
            raise StepFailed(str(e)) from e
        return {"result": result, "error": None}

    builder = StateGraph(StudioState)
    builder.add_node("make", make)
    builder.add_edge(START, "make")
    builder.add_edge("make", END)
    return builder
