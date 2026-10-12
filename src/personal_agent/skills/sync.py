"""Keeps the working rules across deploys.

On the server the app's image carries the skill files as they ship, and an
image is replaced at every deploy. So the library the agent reads and the owner
changes lives outside the image, in the data folder, and this module brings
shipped updates into it without undoing the owner's accepted changes.

Three copies of each skill file:

  shipped    in the image: the file as the repository has it
  live       in the data folder: what the agent reads; accepted changes land here
  baseline   in the data folder: the shipped text the live file last started from

At start, for each shipped skill:

  no live file                 copy shipped to live and to baseline
  live equals baseline         the owner has not changed it: take the shipped text
  live differs from baseline   the owner has changed it: keep live. If the shipped
                               text has also moved on, the two have diverged and
                               the owner chooses on the Memory & decisions screen.
"""

from __future__ import annotations

from pathlib import Path

FILE = "SKILL.md"


def baseline_dir(live_dir: Path) -> Path:
    return live_dir.parent / f"{live_dir.name}-shipped"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def sync_library(shipped_dir: Path, live_dir: Path) -> dict[str, list[str]]:
    """Returns the skills {"added", "updated", "diverged"} by this pass."""
    report: dict[str, list[str]] = {"added": [], "updated": [], "diverged": []}
    base_dir = baseline_dir(live_dir)
    for source in sorted(shipped_dir.glob(f"*/{FILE}")):
        name = source.parent.name
        shipped = source.read_text(encoding="utf-8")
        live, base = _read(live_dir / name / FILE), _read(base_dir / name / FILE)
        if live is None:
            _write(live_dir / name / FILE, shipped)
            _write(base_dir / name / FILE, shipped)
            report["added"].append(name)
        elif base is None:
            # A live file with no record of where it started: treat it as the owner's.
            _write(base_dir / name / FILE, live)
            if live != shipped:
                report["diverged"].append(name)
        elif live == base:
            if shipped != base:
                _write(live_dir / name / FILE, shipped)
                _write(base_dir / name / FILE, shipped)
                report["updated"].append(name)
        elif shipped != base and shipped != live:
            report["diverged"].append(name)
        elif shipped == live:
            _write(base_dir / name / FILE, shipped)  # the repository caught up with the owner's text
    return report


def diverged(shipped_dir: Path, live_dir: Path) -> list[dict]:
    """Skills where the owner's text and a newer shipped text both moved on from the baseline."""
    out = []
    base_dir = baseline_dir(live_dir)
    for source in sorted(shipped_dir.glob(f"*/{FILE}")):
        name = source.parent.name
        shipped = source.read_text(encoding="utf-8")
        live, base = _read(live_dir / name / FILE), _read(base_dir / name / FILE)
        if live is not None and base is not None and live != base and shipped != base and shipped != live:
            out.append({"skill": name, "baseline": base, "shipped": shipped, "live": live})
    return out


def resolve(shipped_dir: Path, live_dir: Path, skill: str, take_shipped: bool) -> None:
    """The owner's choice for a diverged skill: keep their text, or take the shipped one."""
    shipped = (shipped_dir / skill / FILE).read_text(encoding="utf-8")
    if take_shipped:
        _write(live_dir / skill / FILE, shipped)
    _write(baseline_dir(live_dir) / skill / FILE, shipped)
