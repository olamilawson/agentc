"""Tool gateway (PRD: tool gateway).

The gateway is the only code that calls outside systems. Every tool carries one
of three classes: read, draft or release. A release call requires an approval
record that matches the exact content by hash; if the content changes after
approval, the approval lapses and the owner approves again. Release calls carry
an idempotency key. After an uncertain outcome the harness never retries on its
own — it marks the run for the owner to check.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from personal_agent.db import Database


class ToolClass(str, Enum):
    READ = "read"
    DRAFT = "draft"
    RELEASE = "release"


@dataclass
class ToolSpec:
    name: str
    cls: ToolClass
    fn: Callable[[dict], dict]


class ToolRefused(Exception):
    """The node called a tool not on its allowlist."""


class ApprovalRequired(Exception):
    """A release call was made with no valid approval for this exact content."""


class ApprovalLapsed(Exception):
    """The content changed after approval; the approval no longer applies."""


class UncertainOutcome(Exception):
    """The tool call's outcome is unknown; the harness must not retry."""


def content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ToolGateway:
    def __init__(self, db: Database):
        self.db = db
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def call(
        self,
        run_id: str,
        node: str,
        allowed_tools: list[str],
        name: str,
        payload: dict,
    ) -> dict:
        if name not in allowed_tools:
            self.db.record_tool_call(run_id, node, name, "unknown", content_hash(payload),
                                     None, "refused", {"reason": "tool not on node allowlist"})
            self.db.audit(run_id, "tool_refused", {"node": node, "tool": name})
            raise ToolRefused(f"tool '{name}' is not on node '{node}' allowlist")

        spec = self._tools.get(name)
        if spec is None:
            self.db.record_tool_call(run_id, node, name, "unknown", content_hash(payload),
                                     None, "refused", {"reason": "tool not registered"})
            raise ToolRefused(f"tool '{name}' is not registered")

        # Client separation is checked in code: a tool payload naming another
        # client than the run's immutable scope is refused and audited.
        run = self.db.get_run(run_id)
        if run and isinstance(payload, dict) and payload.get("client") not in (None, run["client"]):
            self.db.record_tool_call(run_id, node, name, spec.cls.value, content_hash(payload),
                                     None, "refused",
                                     {"reason": f"cross-client access: run client is {run['client']!r}"})
            self.db.audit(run_id, "cross_client_refused", {
                "node": node, "tool": name,
                "attempted": payload.get("client"), "run_client": run["client"],
            })
            raise ToolRefused(
                f"tool '{name}' attempted to access client {payload['client']!r} "
                f"but the run scope is {run['client']!r}"
            )
        p_hash = content_hash(payload)

        if spec.cls is ToolClass.RELEASE:
            idem = hashlib.sha256(f"{run_id}:{name}:{p_hash}".encode()).hexdigest()
            seen = self.db.find_tool_call(idem)
            if seen:
                # Idempotency: the same release under the same key must not run twice.
                self.db.record_tool_call(run_id, node, name, spec.cls.value, p_hash, idem,
                                         "replay", {"original_outcome": seen["outcome"]})
                return (seen.get("detail") or {"sent": True})

            approval = self.db.latest_approval_for_hash(run_id, p_hash)
            if approval is None:
                self.db.record_tool_call(run_id, node, name, spec.cls.value, p_hash, idem,
                                         "awaiting_approval", None)
                raise ApprovalRequired(
                    f"release tool '{name}' requires owner approval matching content hash {p_hash[:12]}…"
                )

        try:
            result = spec.fn(payload)
        except UncertainOutcome as e:
            self.db.record_tool_call(run_id, node, name, spec.cls.value, p_hash, None,
                                     "uncertain", {"error": str(e)})
            self.db.audit(run_id, "uncertain_outcome", {
                "tool": name,
                "note": "harness did not retry; owner must check the provider state",
            })
            raise
        except Exception as e:  # definite failure: recorded, no silent retry
            self.db.record_tool_call(run_id, node, name, spec.cls.value, p_hash, None,
                                     "failed", {"error": str(e)})
            raise

        self.db.record_tool_call(run_id, node, name, spec.cls.value, p_hash, idem if spec.cls is ToolClass.RELEASE else None,
                                 "executed", result)
        return result
