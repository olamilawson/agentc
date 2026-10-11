"""The trace of a run, assembled for the runs screen (PRD: interface / runs).

Every step of every run is inspectable: each node's status, what went into its
model calls and what came out, its tool calls, its cost, and the skill and
prompt versions used. The rows come from run_steps, model_calls and tool_calls.
"""

from __future__ import annotations

from personal_agent.db import Database

MAX_TEXT = 1500
MAX_ITEMS = 50


def clip(value):
    """A copy safe to store and show: long text and long lists are shortened,
    and the shortening is stated in place so nothing looks complete that is not."""
    if isinstance(value, str):
        if len(value) <= MAX_TEXT:
            return value
        return f"{value[:MAX_TEXT]} [... {len(value) - MAX_TEXT} more characters]"
    if isinstance(value, dict):
        return {str(k): clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [clip(v) for v in value[:MAX_ITEMS]]
        return items + [f"[... {len(value) - MAX_ITEMS} more items]"] if len(value) > MAX_ITEMS else items
    return value


def build_trace(db: Database, run_id: str) -> dict | None:
    run = db.get_run(run_id)
    if not run:
        return None
    steps = [{**s, "model_calls": [], "tool_calls": []} for s in db.list_steps(run_id)]
    by_seq = {s["seq"]: s for s in steps}

    for call in db.list_model_calls(run_id):
        step = by_seq.get(call["step_seq"])
        if step is None:
            # A call made outside a graph step (the learning review), or in a step
            # that never got recorded: shown as a step of its own rather than hidden.
            step = {
                "seq": call["step_seq"], "node": call["node"], "status": "done", "output": None, "error": None,
                "duration_ms": None, "created_at": call["created_at"], "model_calls": [], "tool_calls": [],
            }
            by_seq[call["step_seq"]] = step
            steps.append(step)
        step["model_calls"].append(call)

    unplaced = []
    for call in db.list_tool_calls(run_id):
        # tool_calls predates the trace and has no step number: a call belongs to
        # the first step of its node that finished at or after the call was made.
        same_node = [s for s in steps if s["node"] == call["node"]]
        home = next((s for s in same_node if s["created_at"] >= call["created_at"]), None) or (
            same_node[-1] if same_node else None
        )
        (home["tool_calls"] if home else unplaced).append(call)

    steps.sort(key=lambda s: s["seq"])
    by_node: dict[str, dict] = {}
    for step in steps:
        step["tokens"] = sum((c["tokens_in"] or 0) + (c["tokens_out"] or 0) for c in step["model_calls"])
        step["cost_usd"] = round(sum(c["cost_usd"] or 0.0 for c in step["model_calls"]), 6)
        node = by_node.setdefault(step["node"], {"node": step["node"], "steps": 0, "model_calls": 0, "tokens": 0, "cost_usd": 0.0})
        node["steps"] += 1
        node["model_calls"] += len(step["model_calls"])
        node["tokens"] += step["tokens"]
        node["cost_usd"] = round(node["cost_usd"] + step["cost_usd"], 6)
    traced = round(sum(n["cost_usd"] for n in by_node.values()), 6)
    return {
        "run": run,
        "steps": steps,
        "cost_by_node": sorted(by_node.values(), key=lambda n: (-n["cost_usd"], n["node"])),
        "traced_cost_usd": traced,
        # Runs made before tracing existed have a total and no per-node rows.
        "untraced_cost_usd": round(max(0.0, (run["cost_usd"] or 0.0) - traced), 6),
        "unplaced_tool_calls": unplaced,
        "prompt_versions": sorted({(c["node"], c["prompt_version"]) for s in steps for c in s["model_calls"] if c["prompt_version"]}),
    }
