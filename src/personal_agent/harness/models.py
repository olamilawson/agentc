"""Model gateway (PRD: model routing and cost).

Every model call goes through LiteLLM; the tier's route in config/route_table.yaml
names a primary provider and one named fallback. The fallback is used only on
provider error and the run record shows when it did. Tests inject a gateway
that replays recorded responses, so tests never call a provider.
"""

from __future__ import annotations

from personal_agent.config import TierRoute
from personal_agent.harness.contract import StepContract


class ProviderError(Exception):
    pass


class ModelGateway:
    def __init__(self, route_table: dict[str, TierRoute]):
        self.route_table = route_table

    def complete(self, contract: StepContract, context: str) -> dict:
        """Returns {"data": <parsed json>, "model": str, "fallback_used": bool,
        "usage": {"tokens_in": int, "tokens_out": int, "cost_usd": float}}."""
        route = self.route_table[contract.tier]
        try:
            return self._call(route.primary, contract, context, fallback_used=False)
        except ProviderError:
            if not route.fallback:
                raise
            result = self._call(route.fallback, contract, context, fallback_used=True)
            result["fallback_used"] = True
            return result

    def _call(self, model: str, contract: StepContract, context: str, fallback_used: bool) -> dict:
        import litellm

        messages = [
            {"role": "system", "content": contract.instruction + "\n\n" + context},
            {
                "role": "user",
                "content": (
                    f"Produce the output for the '{contract.node}' step as a single JSON object "
                    "that conforms to the output schema described in the context. No prose outside the JSON."
                ),
            },
        ]
        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                max_tokens=contract.limits.max_tokens,
                timeout=contract.limits.timeout_seconds,
            )
        except Exception as e:  # provider/SDK errors — never a silent model swap without the route table
            raise ProviderError(f"{model}: {e}") from e

        raw = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0
        try:
            cost = float(litellm.completion_cost(response))
        except Exception:
            cost = 0.0
        from personal_agent.harness.contract import _parse_json

        return {
            "data": _parse_json(raw),
            "model": model,
            "fallback_used": fallback_used,
            "usage": {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost},
        }


class RecordedGateway(ModelGateway):
    """Replays recorded model responses per node. Used by tests and local eval — no network, no cost."""

    def __init__(self, responses: dict[str, list[str | dict]]):
        super().__init__(route_table={})
        self._responses = {k: list(v) for k, v in responses.items()}
        self.used_fallback = False

    def complete(self, contract: StepContract, context: str) -> dict:
        queue = self._responses.get(contract.node)
        if not queue:
            raise ProviderError(f"no recorded response for node '{contract.node}'")
        raw = queue.pop(0)
        from personal_agent.harness.contract import _parse_json

        data = raw if isinstance(raw, dict) else _parse_json(raw)
        return {
            "data": data,
            "model": "recorded",
            "fallback_used": False,
            "usage": {"tokens_in": 150, "tokens_out": 80, "cost_usd": 0.002},
        }
