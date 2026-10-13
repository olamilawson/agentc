"""Model gateway (PRD: model routing and cost).

Every model call goes through LiteLLM; the tier's route in config/route_table.yaml
names a primary provider and one named fallback. The fallback is used only on
provider error and the run record shows when it did. Tests inject a gateway
that replays recorded responses, so tests never call a provider.
"""

from __future__ import annotations

import re
import time

from personal_agent.config import TierRoute
from personal_agent.harness.contract import StepContract, StepFailed


class ProviderError(Exception):
    pass


class ProviderNotApproved(StepFailed):
    """A restricted client's material may not go to any model this tier routes to."""


# A provider that says "too many requests this minute" is asked again after a wait, a few times.
RATE_LIMIT_TRIES, MAX_WAIT_SECONDS = 4, 45.0
SLEEP = time.sleep  # tests replace it


def rate_limit_wait(error: Exception) -> float | None:
    """Seconds to wait before asking again, or None when asking again will not help.
    An account with no credit also answers "rate limit"; waiting does not fix that."""
    text = str(error).lower()
    if not any(mark in text or mark in type(error).__name__.lower() for mark in ("ratelimit", "rate limit", "429")):
        return None
    if any(word in text for word in ("credit", "quota", "billing", "per day", "tokens per day", "requests per day")):
        return None
    named = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", text)
    seconds = (int(named.group(1) or 0) * 60 + float(named.group(2))) if named else 15.0
    return min(seconds + 1.0, MAX_WAIT_SECONDS)


def provider_of(model: str) -> str:
    return model.split("/", 1)[0].lower()


class ModelGateway:
    def __init__(self, route_table: dict[str, TierRoute]):
        self.route_table = route_table

    def complete(
        self,
        contract: StepContract,
        context: str,
        images: list[tuple[str, bytes]] | None = None,
        providers: set[str] | None = None,
    ) -> dict:
        """Returns {"data": <parsed json>, "model": str, "fallback_used": bool,
        "usage": {"tokens_in": int, "tokens_out": int, "cost_usd": float}}.

        images: (label, JPEG bytes) pairs sent with the request, each preceded
        by its label so the model can cite the page it is looking at.

        providers: for a restricted client, the only providers the call may go
        to. The route's models outside that set are never called; if the
        primary is one of them the named fallback is used and shown as such.
        """
        route = self.route_table[contract.tier]
        if images and "vision" in self.route_table:
            # A step that shows pictures goes to the model that can see them, whatever its tier.
            route = self.route_table["vision"]
        models = [(route.primary, False)] + ([(route.fallback, True)] if route.fallback else [])
        if providers is not None:
            models = [(m, fallback) for m, fallback in models if provider_of(m) in providers]
            if not models:
                named = " or ".join(m for m in (route.primary, route.fallback) if m)
                raise ProviderNotApproved(
                    f"node '{contract.node}' was not run: the client is restricted and the "
                    f"{contract.tier} tier routes only to {named}, which is not approved for confidential work"
                )
        for position, (model, fallback) in enumerate(models):
            try:
                result = self._call(model, contract, context, fallback, images)
                result["fallback_used"] = fallback
                return result
            except ProviderError:
                if position == len(models) - 1:
                    raise
        raise AssertionError("unreachable: a route always names a primary model")

    def probe(self, model: str) -> dict:
        """One small call to a named model, to learn whether it answers from this
        server. Returns {"ok": bool, "detail": str}; it never raises."""
        import litellm

        try:
            response = litellm.completion(
                model=model, messages=[{"role": "user", "content": "Reply with the one word: ready"}],
                max_tokens=200, timeout=60,
            )
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"[:600]}
        reply = (response.choices[0].message.content or "").strip()
        return {"ok": True, "detail": f"it replied {reply[:80]!r}" if reply else "it replied with no text"}

    def _call(
        self,
        model: str,
        contract: StepContract,
        context: str,
        fallback_used: bool,
        images: list[tuple[str, bytes]] | None = None,
    ) -> dict:
        import litellm

        ask = (
            f"Produce the output for the '{contract.node}' step as a single JSON object "
            "that conforms to the output schema described in the context. No prose outside the JSON."
        )
        user: str | list = ask
        if images:
            import base64

            user = [{"type": "text", "text": ask}]
            for label, data in images:
                user.append({"type": "text", "text": label})
                user.append({
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()},
                })
        messages = [
            {"role": "system", "content": contract.instruction + "\n\n" + context},
            {"role": "user", "content": user},
        ]
        response = None
        for attempt in range(RATE_LIMIT_TRIES):
            try:
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    max_tokens=contract.limits.max_tokens,
                    timeout=contract.limits.timeout_seconds,
                )
                break
            except Exception as e:  # provider/SDK errors — never a silent model swap without the route table
                wait = rate_limit_wait(e)
                if wait is None or attempt == RATE_LIMIT_TRIES - 1:
                    raise ProviderError(f"{model}: {e}") from e
                # The provider's minute is spent. The same request, to the same model, after the wait it names.
                SLEEP(wait)

        raw = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0
        try:
            cost = float(litellm.completion_cost(response))
        except Exception:
            cost = 0.0
        from personal_agent.harness.contract import OutputInvalid, _parse_json

        call = {
            "data": raw,
            "model": model,
            "fallback_used": fallback_used,
            "usage": {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost},
        }
        try:
            call["data"] = _parse_json(raw)
        except OutputInvalid as e:
            e.call = call  # the reply was paid for even though it cannot be used
            raise
        return call


class RecordedGateway(ModelGateway):
    """Replays recorded model responses per node. Used by tests and local eval — no network, no cost."""

    def __init__(self, responses: dict[str, list[str | dict]]):
        super().__init__(route_table={})
        self._responses = {k: list(v) for k, v in responses.items()}
        self.used_fallback = False

    def probe(self, model: str) -> dict:
        return {"ok": True, "detail": "it replied 'ready'"}

    def complete(
        self,
        contract: StepContract,
        context: str,
        images: list[tuple[str, bytes]] | None = None,
        providers: set[str] | None = None,  # a recording has no provider to restrict
    ) -> dict:
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
