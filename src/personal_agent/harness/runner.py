"""Node runner: executes one step of a workflow under its contract."""

from __future__ import annotations

import hashlib
import json
from typing import Callable

from personal_agent.harness.budgets import BudgetTracker
from personal_agent.harness.contract import OutputInvalid, StepContract, StepFailed
from personal_agent.harness.models import ModelGateway, ProviderError


class NodeRunner:
    def __init__(self, gateway: ModelGateway, tracker: BudgetTracker, record: Callable[[dict], None] | None = None):
        """record: called once per model call with what the trace keeps of it."""
        self.gateway = gateway
        self.tracker = tracker
        self.record = record

    def run(
        self,
        contract: StepContract,
        payload: dict,
        context: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> dict:
        """Returns the validated output dict. Raises StepFailed after one retry.

        `context` is the assembled prompt context (fixed order, untrusted data
        marked); when omitted a minimal context from the validated inputs is used.
        On invalid output the validation error is appended and the step retries
        once, then fails the run with the reason.
        """
        validated_input = contract.validate_input(payload)

        error_text: str | None = None
        for attempt in range(2):  # PRD: retry once with the validation error appended
            self.tracker.check()
            body = context or self._minimal_context(contract, validated_input)
            if error_text:
                body += (
                    f"\n\nYour previous output was invalid: {error_text}. "
                    "Return a corrected JSON object."
                )
            result: dict = {}
            output = None
            try:
                # Page images, when a step has them, go with every attempt.
                result = (
                    self.gateway.complete(contract, body, images=images)
                    if images
                    else self.gateway.complete(contract, body)
                )
                output = contract.validate_output(result["data"])
                outcome, error_text = "ok", None
            except OutputInvalid as e:  # unparseable JSON or schema violation both retry once
                # An unparseable reply still cost something: the gateway hands its usage over.
                result = result or getattr(e, "call", {})
                outcome, error_text = "invalid output", str(e)
            except ProviderError as e:
                self._record(contract, attempt, validated_input, {}, None, "provider error", str(e))
                raise
            usage = result.get("usage") or {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
            self._record(
                contract, attempt, validated_input, result, output if outcome == "ok" else result.get("data"),
                outcome, error_text,
            )
            self.tracker.add_usage(usage["tokens_in"], usage["tokens_out"], usage["cost_usd"])
            if outcome == "ok":
                return output

        raise StepFailed(f"node '{contract.node}' failed validation after retry: {error_text}")

    def _record(self, contract: StepContract, attempt: int, validated_input: dict, result: dict,
                output, outcome: str, error: str | None) -> None:
        if not self.record:
            return
        usage = result.get("usage") or {}
        self.record({
            "node": contract.node,
            "attempt": attempt + 1,
            "tier": contract.tier,
            "model": result.get("model"),
            "fallback_used": bool(result.get("fallback_used")),
            # Prompts live in the contract; the hash pins the wording a call used.
            "prompt_version": hashlib.sha256(contract.instruction.encode()).hexdigest()[:12],
            "tokens_in": usage.get("tokens_in", 0),
            "tokens_out": usage.get("tokens_out", 0),
            "cost_usd": usage.get("cost_usd", 0.0),
            "outcome": outcome,
            "error": error,
            "input": validated_input,
            "output": output,
        })

    def _minimal_context(self, contract: StepContract, validated_input: dict) -> str:
        lines = [
            f"Node: {contract.node}",
            f"Output schema: {json.dumps(contract.output_schema.model_json_schema(), sort_keys=True)}",
            "Inputs: " + json.dumps(validated_input, sort_keys=True),
        ]
        return "\n".join(lines)
