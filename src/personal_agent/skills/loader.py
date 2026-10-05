"""Skill library loader (PRD: memory and context / working rules).

Rubrics, tone rules, templates and calibration examples live as versioned
skills/<name>/SKILL.md files, never inside code. Every run records the library
version it used. The agent never edits this library; changes arrive only
through the weekly learning loop as owner-approved diffs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def load_library(skills_dir: Path) -> tuple[str, dict[str, str]]:
    """Returns (version_hash, {skill_name: markdown_text}).

    The version hash covers every skill file's content, so a run record pins
    the exact library state it used.
    """
    skills: dict[str, str] = {}
    digest = hashlib.sha256()
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        name = path.parent.name
        skills[name] = text
        digest.update(f"{name}\0{text}\0".encode())
    return digest.hexdigest(), skills
