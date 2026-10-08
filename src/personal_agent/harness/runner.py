"""Node runner: executes one step of a workflow under its contract."""

from __future__ import annotations

import json

from personal_agent.harness.budgets import BudgetTracker
from personal_agent.harness.contract import OutputInvalid, StepContract, StepFailed
from personal_agent.harness.models import ModelGateway


class NodeRunner:
    def __init__(self, gateway: ModelGateway, tracker: BudgetTracker):
        self.gateway = gateway
        self.tracker = tracker

    def run(self, contract: StepContract, payload: dict, context: str | None = None) -> dict:
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
            try:
                result = self.gateway.complete(contract, body)
                self.tracker.add_usage(
                    result["usage"]["tokens_in"], result["usage"]["tokens_out"], result["usage"]["cost_usd"]
                )
                return contract.validate_output(result["data"])
            except OutputInvalid as e:  # unparseable JSON or schema violation both retry once
                error_text = str(e)

        raise StepFailed(f"node '{contract.node}' failed validation after retry: {error_text}")

    def _minimal_context(self, contract: StepContract, validated_input: dict) -> str:
        lines = [
            f"Node: {contract.node}",
            f"Output schema: {json.dumps(contract.output_schema.model_json_schema(), sort_keys=True)}",
            "Inputs: " + json.dumps(validated_input, sort_keys=True),
        ]
        return "\n".join(lines)
