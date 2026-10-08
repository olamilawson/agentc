"""Evaluation runner (PRD: evaluation).

Runs a workflow over its evaluation set and measures it against the reference
scores: the owner's where an item carries them, the builder's provisional ones
otherwise. For brief evaluation the PRD measures the share of scores within one
point of the owner's, gaps the agent missed, and quotes that do not appear in
the brief; the release gate is no invalid quotes and agreement at or above the
level the owner sets.

A synthetic set smoke-tests the workflow and never satisfies the gate. Each item
runs in a scratch database and client folder, stops at the approval node and is
never approved, so an evaluation leaves nothing in the real run record or queue.

    python -m personal_agent.evaluation brief-evaluation --min-agreement 0.8
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from personal_agent.config import REPO_ROOT, Settings, load_settings
from personal_agent.db import Database
from personal_agent.engine.brief_evaluation import CRITERIA, unmatched_quotes
from personal_agent.engine.runner import Engine
from personal_agent.engine.sponsorship_triage import CLIENT as SPONSORSHIP_CLIENT
from personal_agent.harness.models import ModelGateway
from personal_agent.skills.loader import load_library

MIN_GATE_ITEMS = 15  # PRD: at least fifteen real past items per workflow
WORKFLOWS = ("brief-evaluation", "sponsorship-triage")


def load_set(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _is_gate_item(item: dict) -> bool:
    owner = item.get("owner") or {}
    return item.get("synthetic") is False and bool(owner.get("scores") or owner.get("route"))


@contextmanager
def scratch_engine(settings: Settings, gateway: ModelGateway | None = None) -> Iterator[Engine]:
    """An engine on a throwaway database, checkpoint store and client root."""
    workdir = Path(tempfile.mkdtemp(prefix="personal-agent-eval-"))
    scratch = settings.model_copy(update={
        "database_url": f"sqlite:///{workdir / 'app.db'}",
        "checkpoint_path": workdir / "checkpoints.sqlite",
        "clients_dir": workdir / "clients",
    })
    db = Database(scratch.database_url)
    engine = Engine(scratch, db, gateway=gateway)
    try:
        yield engine
    finally:
        engine.checkpointer.conn.close()
        db.engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)


def run_brief_set(items: list[dict], settings: Settings, gateway: ModelGateway | None = None) -> list[dict]:
    with scratch_engine(settings, gateway) as engine:
        return [_run_brief_item(engine, item) for item in items]


def _run_brief_item(engine: Engine, item: dict) -> dict:
    run_id = engine.start_run("brief-evaluation", "evaluation", {"brief_text": item["brief"]})
    run = engine.db.get_run(run_id)
    out = run["outputs"] or {}
    reference = item.get("owner") or item.get("provisional") or {}
    ref_scores = reference.get("scores") or {}
    agent_scores = {s["criterion"]: s["score"] for s in out.get("scores") or []}
    ref_gaps = reference.get("gaps")
    return {
        "id": item["id"],
        "reference": "owner" if _is_gate_item(item) else "provisional",
        # A run that reaches the approval node completed every model step.
        "completed": run["status"] == "waiting_for_approval",
        "error": run["error_reason"],
        "agent_scores": agent_scores,
        "reference_scores": ref_scores,
        "diffs": {c: agent_scores[c] - ref_scores[c] for c in ref_scores if c in agent_scores},
        "agent_decision": out.get("decision"),
        "reference_decision": reference.get("decision"),
        "invalid_quotes": unmatched_quotes(out.get("scores") or [], item["brief"]),
        "open_faults": out.get("faults") or [],
        # Measured only where the reference lists the gaps it expects.
        "missed_gaps": None if ref_gaps is None else [g for g in ref_gaps if g not in out.get("gaps", [])],
        "flags": run["flags"] or [],
        "tokens": (run["tokens_in"] or 0) + (run["tokens_out"] or 0),
        "cost_usd": run["cost_usd"] or 0.0,
    }


def _share(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def summarise(results: list[dict]) -> dict:
    done = [r for r in results if r["completed"]]
    diffs = [d for r in done for d in r["diffs"].values()]
    by_criterion = {}
    for c in CRITERIA:
        ds = [r["diffs"][c] for r in done if c in r["diffs"]]
        by_criterion[c] = {
            "within_one": _share(sum(abs(d) <= 1 for d in ds), len(ds)),
            # Positive: the agent scores this criterion higher than the reference.
            "mean_diff": round(sum(ds) / len(ds), 2) if ds else None,
        }
    decided = [r for r in done if r["reference_decision"]]
    with_gaps = [r for r in done if r["missed_gaps"] is not None]
    return {
        "items": len(results),
        "failed": len(results) - len(done),
        "scores_compared": len(diffs),
        "within_one": _share(sum(abs(d) <= 1 for d in diffs), len(diffs)),
        "exact": _share(sum(d == 0 for d in diffs), len(diffs)),
        "by_criterion": by_criterion,
        "decision_agreement": _share(
            sum(r["agent_decision"] == r["reference_decision"] for r in decided), len(decided)
        ),
        "invalid_quotes": sum(len(r["invalid_quotes"]) for r in results),
        "items_with_open_faults": sum(bool(r["open_faults"]) for r in results),
        "missed_gaps": sum(len(r["missed_gaps"]) for r in with_gaps) if with_gaps else None,
        "cost_usd": round(sum(r["cost_usd"] for r in results), 4),
    }


def judge_gate(items: list[dict], set_size: int, summary: dict, min_agreement: float | None) -> dict:
    """The release gate passes only when nothing below blocks it."""
    blocked = []
    not_gate = sum(not _is_gate_item(i) for i in items)
    if not_gate:
        blocked.append(
            f"{not_gate} of {len(items)} items are synthetic or lack the owner's scores; "
            "a synthetic set never satisfies a release gate"
        )
    if len(items) < set_size:
        blocked.append(f"only {len(items)} of the set's {set_size} items were run")
    if set_size < MIN_GATE_ITEMS:
        blocked.append(f"the set has {set_size} items; the minimum is {MIN_GATE_ITEMS}")
    if summary["failed"]:
        blocked.append(f"{summary['failed']} runs failed before reaching the owner")
    if summary["invalid_quotes"]:
        blocked.append(f"{summary['invalid_quotes']} quotes do not appear in their brief")
    if min_agreement is None:
        blocked.append("the owner has not set an agreement level (--min-agreement)")
    elif summary["within_one"] is None or summary["within_one"] < min_agreement:
        blocked.append(
            f"agreement within one point is {summary['within_one']}, below the owner's level {min_agreement}"
        )
    return {"passed": not blocked, "min_agreement": min_agreement, "blocked_by": blocked}


# --- sponsorship triage --------------------------------------------------------------
# PRD measures: route agreement with the owner, escalations the agent missed.
# Gate: no missed escalations on the set; no reply releasable without approval.
def run_sponsorship_set(
    items: list[dict], settings: Settings, gateway: ModelGateway | None = None
) -> tuple[list[dict], int]:
    """(results, mail released without approval). No item is ever approved, so
    anything in the outbox afterwards was released without the owner."""
    with scratch_engine(settings, gateway) as engine:
        results = [_run_sponsorship_item(engine, item) for item in items]
        return results, len(engine.db.list_outbox())


def _run_sponsorship_item(engine: Engine, item: dict) -> dict:
    run_id = engine.start_run("sponsorship-triage", SPONSORSHIP_CLIENT, {"message_id": item["id"], **item["mail"]})
    run = engine.db.get_run(run_id)
    out = run["outputs"] or {}
    reference = (item.get("owner") or item.get("provisional") or {}).get("route")
    route = out.get("route")
    # A mail that is not a sponsorship request ends the run; everything else waits for the owner.
    completed = run["status"] == "waiting_for_approval" or (run["status"] == "done" and route == "not_sponsorship")
    return {
        "id": item["id"],
        "reference": "owner" if _is_gate_item(item) else "provisional",
        "completed": completed,
        "error": run["error_reason"] or (None if completed else f"run ended as {run['status']} on route {route}"),
        "agent_route": route,
        "reference_route": reference,
        "route_reason": out.get("route_reason"),
        "missed_escalation": completed and reference == "escalate" and route != "escalate",
        "open_faults": out.get("faults") or [],
        "tokens": (run["tokens_in"] or 0) + (run["tokens_out"] or 0),
        "cost_usd": run["cost_usd"] or 0.0,
    }


def summarise_sponsorship(results: list[dict], released_without_approval: int) -> dict:
    done = [r for r in results if r["completed"]]
    return {
        "items": len(results),
        "failed": len(results) - len(done),
        "route_agreement": _share(sum(r["agent_route"] == r["reference_route"] for r in done), len(done)),
        "missed_escalations": [r["id"] for r in results if r["missed_escalation"]],
        "items_with_open_faults": sum(bool(r["open_faults"]) for r in results),
        "released_without_approval": released_without_approval,
        "cost_usd": round(sum(r["cost_usd"] for r in results), 4),
    }


def judge_sponsorship_gate(items: list[dict], set_size: int, summary: dict, min_agreement: float | None) -> dict:
    blocked = _set_blockers(items, set_size, summary)
    if summary["missed_escalations"]:
        blocked.append("missed escalations: " + ", ".join(summary["missed_escalations"]))
    if summary["released_without_approval"]:
        blocked.append(f"{summary['released_without_approval']} replies were released without approval")
    # Route agreement is measured; it gates only if the owner sets a level for it.
    if min_agreement is not None and (summary["route_agreement"] or 0) < min_agreement:
        blocked.append(f"route agreement is {summary['route_agreement']}, below the owner's level {min_agreement}")
    return {"passed": not blocked, "min_agreement": min_agreement, "blocked_by": blocked}


def _set_blockers(items: list[dict], set_size: int, summary: dict) -> list[str]:
    """What stops any set from being a gate set, whatever the workflow."""
    blocked = []
    not_gate = sum(not _is_gate_item(i) for i in items)
    if not_gate:
        blocked.append(
            f"{not_gate} of {len(items)} items are synthetic or lack the owner's decisions; "
            "a synthetic set never satisfies a release gate"
        )
    if len(items) < set_size:
        blocked.append(f"only {len(items)} of the set's {set_size} items were run")
    if set_size < MIN_GATE_ITEMS:
        blocked.append(f"the set has {set_size} items; the minimum is {MIN_GATE_ITEMS}")
    if summary["failed"]:
        blocked.append(f"{summary['failed']} runs failed before reaching the owner")
    return blocked


def _format_sponsorship_report(report: dict) -> str:
    s, gate = report["summary"], report["gate"]
    lines = [
        f"{report['workflow']}: {s['items']} items, {s['failed']} failed, cost ${s['cost_usd']:.2f}",
        f"skill library {report['skill_library_version'][:12]}",
        "",
        f"route agreement            {_pct(s['route_agreement'])}",
        f"missed escalations         {len(s['missed_escalations'])}",
        f"released without approval  {s['released_without_approval']}",
        f"items with open faults     {s['items_with_open_faults']}",
        "",
        "items",
    ]
    for r in report["items"]:
        if not r["completed"]:
            lines.append(f"  {r['id']}  FAILED  {r['error']}")
        elif r["agent_route"] == r["reference_route"]:
            lines.append(f"  {r['id']}  {r['agent_route']}")
        else:
            missed = "  MISSED ESCALATION" if r["missed_escalation"] else ""
            lines.append(
                f"  {r['id']}  {r['agent_route']} (reference: {r['reference_route']}){missed}  [{r['route_reason']}]"
            )
    lines.append("")
    if gate["passed"]:
        lines.append("RELEASE GATE: passed")
    else:
        lines.append("RELEASE GATE: not passed")
        lines += [f"  - {reason}" for reason in gate["blocked_by"]]
    return "\n".join(lines)


def evaluate(
    workflow: str,
    set_path: Path,
    settings: Settings,
    gateway: ModelGateway | None = None,
    min_agreement: float | None = None,
    limit: int | None = None,
) -> dict:
    if workflow not in WORKFLOWS:
        raise ValueError(f"no evaluation runner for {workflow!r}; known: {', '.join(WORKFLOWS)}")
    all_items = load_set(set_path)
    items = all_items[:limit] if limit else all_items
    version, _ = load_library(settings.skills_dir)
    if workflow == "sponsorship-triage":
        results, released = run_sponsorship_set(items, settings, gateway)
        summary = summarise_sponsorship(results, released)
        return {
            "workflow": workflow,
            "set": str(set_path),
            "skill_library_version": version,
            "route_table": {tier: route.model_dump() for tier, route in settings.route_table.items()},
            "sponsorship_policy": settings.sponsorship.model_dump() if settings.sponsorship else None,
            "summary": summary,
            "gate": judge_sponsorship_gate(items, len(all_items), summary, min_agreement),
            "items": results,
        }
    results = run_brief_set(items, settings, gateway)
    summary = summarise(results)
    return {
        "workflow": workflow,
        "set": str(set_path),
        # What a later run is compared against: any change to a rubric, route
        # table or model runs against the set before release.
        "skill_library_version": version,
        "route_table": {tier: route.model_dump() for tier, route in settings.route_table.items()},
        "summary": summary,
        "gate": judge_gate(items, len(all_items), summary, min_agreement),
        "items": results,
    }


def _pct(share: float | None) -> str:
    return "n/a" if share is None else f"{share:.0%}"


def format_report(report: dict) -> str:
    if report["workflow"] == "sponsorship-triage":
        return _format_sponsorship_report(report)
    s, gate = report["summary"], report["gate"]
    lines = [
        f"{report['workflow']}: {s['items']} items, {s['failed']} failed, cost ${s['cost_usd']:.2f}",
        f"skill library {report['skill_library_version'][:12]}",
        "",
        f"scores within one point   {_pct(s['within_one'])}  ({s['scores_compared']} scores compared)",
        f"scores exact              {_pct(s['exact'])}",
        f"decision agreement        {_pct(s['decision_agreement'])}",
        f"invalid quotes            {s['invalid_quotes']}",
        f"items with open faults    {s['items_with_open_faults']}",
        f"gaps missed               {'n/a (set lists no expected gaps)' if s['missed_gaps'] is None else s['missed_gaps']}",
        "",
        "by criterion (within one / mean agent minus reference)",
    ]
    for c, v in s["by_criterion"].items():
        mean = "n/a" if v["mean_diff"] is None else f"{v['mean_diff']:+.2f}"
        lines.append(f"  {c:<22} {_pct(v['within_one']):>4}  {mean}")
    lines += ["", "items"]
    for r in report["items"]:
        if not r["completed"]:
            lines.append(f"  {r['id']}  FAILED  {r['error']}")
            continue
        off = [f"{c} {d:+d}" for c, d in r["diffs"].items() if abs(d) > 1]
        decision = (
            r["agent_decision"]
            if r["agent_decision"] == r["reference_decision"]
            else f"{r['agent_decision']} (reference: {r['reference_decision']})"
        )
        notes = off + [f"{len(r['invalid_quotes'])} invalid quotes"] * bool(r["invalid_quotes"])
        lines.append(f"  {r['id']}  {decision}" + (f"  [{'; '.join(notes)}]" if notes else ""))
    lines.append("")
    if gate["passed"]:
        lines.append(f"RELEASE GATE: passed (agreement level {gate['min_agreement']})")
    else:
        lines.append("RELEASE GATE: not passed")
        lines += [f"  - {reason}" for reason in gate["blocked_by"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from personal_agent import safety  # imports this module's scratch_engine

    parser = argparse.ArgumentParser(prog="python -m personal_agent.evaluation", description=__doc__.split("\n\n")[0])
    parser.add_argument("set_name", metavar="set", choices=WORKFLOWS + safety.SAFETY_SETS,
                        help="a workflow's evaluation set, or a safety set: " + ", ".join(WORKFLOWS + safety.SAFETY_SETS))
    parser.add_argument("--set", type=Path, dest="path", help="set file (default: evaluation/sets/<set>.jsonl)")
    parser.add_argument("--min-agreement", type=float, help="share of scores within one point that the owner requires, 0 to 1")
    parser.add_argument("--limit", type=int, help="run only the first N items (never satisfies the gate)")
    parser.add_argument("--out", type=Path, help="write the full report as JSON")
    args = parser.parse_args(argv)

    set_path = args.path or REPO_ROOT / "evaluation" / "sets" / f"{args.set_name}.jsonl"
    if args.set_name in safety.SAFETY_SETS:
        report = safety.evaluate_safety(args.set_name, set_path, load_settings(), limit=args.limit)
        print(safety.format_safety_report(report))
        passed = report["passed"]
    else:
        report = evaluate(
            args.set_name, set_path, load_settings(), min_agreement=args.min_agreement, limit=args.limit
        )
        print(format_report(report))
        passed = report["gate"]["passed"]
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
