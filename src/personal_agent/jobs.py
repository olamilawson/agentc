"""The scheduled jobs' last results, for the Settings and cost screen.

The server's cron runs deploy/jobs.sh, which writes one small text file per job
into data/jobs. This module reads those files. It runs no job.
"""

from __future__ import annotations

from pathlib import Path

JOBS = (
    ("backup", "Backup", "Each day at 02:15"),
    ("retention", "Retention", "Each day at 02:45"),
    ("learning", "Weekly learning review", "Mondays at 06:00"),
    ("dispatch", "Agency dispatch: send what is due", "Each minute"),
    ("signals", "Outside signals: read your sources", "Each hour, at 20 past"),
)


def last_runs(jobs_dir: Path) -> list[dict]:
    """One entry per job: its schedule and its last run, or None if it has not run."""
    out = []
    for name, label, schedule in JOBS:
        entry = {"name": name, "label": label, "schedule": schedule, "last": None}
        path = jobs_dir / f"{name}.status"
        try:
            fields = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
        except OSError:
            fields = {}
        if fields.get("time"):
            entry["last"] = {"time": fields["time"], "ok": fields.get("ok") == "yes", "detail": fields.get("detail", "")}
        out.append(entry)
    return out
