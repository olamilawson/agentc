"""Step contract (PRD: agent harness).

Every graph node declares its contract and the harness enforces it: the node
receives only fields matching its input schema, its output is structured and
validated before the graph moves on, the model tier is named, tools are an
explicit allowlist, and limits bound each step. Invalid output retries once
with the validation error appended, then fails the run with the reason.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ValidationError


class Limits(BaseModel):
    max_tokens: int = 2000
    timeout_seconds: int = 120


class StepContract(BaseModel):
    node: str
    tier: Literal["cheap", "standard", "strong"]
    allowed_tools: list[str] = []
    limits: Limits = Limits()
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    instruction: str

    def validate_input(self, payload: dict) -> dict:
        return self.input_schema.model_validate(payload).model_dump()

    def validate_output(self, raw: str | dict) -> dict:
        data = raw if isinstance(raw, dict) else _parse_json(raw)
        try:
            return self.output_schema.model_validate(data).model_dump()
        except ValidationError as e:
            raise OutputInvalid(_format_validation_error(e)) from e


class OutputInvalid(Exception):
    """Output failed the node's output schema; carries the validation error text."""


class StepFailed(Exception):
    """The step exhausted its contract (retry spent, budget, or provider error)."""


def _parse_json(raw: str) -> dict:
    import json
    import re

    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise OutputInvalid(f"output was not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise OutputInvalid("output was not a JSON object")
    return parsed


def _format_validation_error(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
