"""Connectors to outside creative services: Canva and Higgsfield.

These are shells. Each connector names the settings it needs and the tools it
will offer, and registers those tools with the tool gateway, so the gateway's
rules already hold for them: a node may call only the tools on its allowlist, a
payload names its client, a release waits for the owner's approval of the exact
content. No tool here calls its service yet. A call stops with a reason:

  not connected     the server lacks one of the connector's settings
  not approved      the client is restricted and the connector is not on the
                    list approved for confidential work (config/providers.yaml)
  not built         the settings are there; the call to the service is not written

No workflow has these tools on a node's allowlist yet, so no run can reach them.

To be confirmed against each account before the calls are written: the setting
names below, what each plan's API allows, and what each call costs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from personal_agent.config import Settings
from personal_agent.db import Database
from personal_agent.gateway.tools import ToolClass, ToolGateway, ToolSpec


class ConnectorNotReady(Exception):
    """The tool cannot run: its connector is not connected, not approved for this client, or not built."""


@dataclass(frozen=True)
class ConnectorTool:
    name: str
    cls: ToolClass
    does: str


@dataclass(frozen=True)
class Connector:
    name: str  # also the name to list under approved_for_confidential
    label: str
    needs: tuple[str, ...]  # settings the server must hold
    tools: tuple[ConnectorTool, ...]

    def missing(self) -> list[str]:
        return [name for name in self.needs if not os.environ.get(name)]


CONNECTORS = (
    Connector(
        name="canva",
        label="Canva",
        needs=("CANVA_CLIENT_ID", "CANVA_CLIENT_SECRET"),
        tools=(
            ConnectorTool("canva_create_design", ToolClass.DRAFT, "Makes a design in your Canva account from a brief or a template."),
            ConnectorTool("canva_export_design", ToolClass.DRAFT, "Saves a design to the client's folder as a PDF or an image."),
            ConnectorTool("canva_share_design", ToolClass.RELEASE, "Shares a design with someone outside. Waits for your approval."),
        ),
    ),
    Connector(
        name="higgsfield",
        label="Higgsfield",
        needs=("HIGGSFIELD_API_KEY",),
        tools=(
            ConnectorTool("higgsfield_generate_image", ToolClass.DRAFT, "Generates an image and saves it to the client's folder."),
            ConnectorTool("higgsfield_generate_video", ToolClass.DRAFT, "Generates a short video and saves it to the client's folder."),
        ),
    ),
)


def register(tools: ToolGateway, db: Database, settings: Settings) -> None:
    for connector in CONNECTORS:
        for tool in connector.tools:
            tools.register(ToolSpec(name=tool.name, cls=tool.cls, fn=_shell(connector, tool, db, settings)))


def _shell(connector: Connector, tool: ConnectorTool, db: Database, settings: Settings):
    def call(payload: dict) -> dict:
        client = payload.get("client")
        if not client:
            raise ConnectorNotReady(f"{tool.name} was not run: the call names no client.")
        if db.is_restricted(client) and connector.name not in settings.confidential_providers:
            raise ConnectorNotReady(
                f"{tool.name} was not run: {client} is restricted and {connector.label} is not approved for confidential work."
            )
        missing = connector.missing()
        if missing:
            raise ConnectorNotReady(
                f"{tool.name} was not run: {connector.label} is not connected. The server has no {' or '.join(missing)}."
            )
        raise ConnectorNotReady(f"{tool.name} was not run: the call to {connector.label} is not built yet.")

    return call


def status() -> list[dict]:
    """Each connector's state, for the Settings screen."""
    return [
        {
            "label": c.label,
            "state": (
                f"Not connected: the server has no {' or '.join(c.missing())}" if c.missing()
                else "Settings saved. The calls to the service are not built yet"
            ),
            "tools": [(t.name, t.cls.value, t.does) for t in c.tools],
        }
        for c in CONNECTORS
    ]
