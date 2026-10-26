"""P7 tests: MCP default deny, typed allowlists, revocation, and stdio roundtrip."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from renamr.core import mcp_gateway, workflow
from renamr.core.errors import CoreError
from renamr.core.extract_runner import InProcessRunner
from renamr.core.roots import RootRole
from renamr.core.workspace import Workspace

_TEXT = """Beispiel Versicherung AG
Versicherungsschein
Anbieter: Beispiel Versicherung AG
Vertragsbeginn: 2026-01-01
Mindestlaufzeit: 12 Monate
Kündigungsfrist: 30 Tage
Beitrag: 120,00 EUR monatlich
Ausgestellt am: 2025-12-15
"""


def _contracts(tmp_path: Path, *, second: bool = False) -> tuple[Workspace, list[str]]:
    workspace = Workspace.bootstrap(tmp_path / "state", config_path=tmp_path / "config.toml")
    library = tmp_path / "library"
    first = library / "alpha"
    first.mkdir(parents=True)
    (first / "initial.txt").write_text(_TEXT)
    if second:
        other = library / "beta"
        other.mkdir()
        (other / "initial.txt").write_text(_TEXT.replace("Beispiel", "Andere"))
    root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    workspace.runner = InProcessRunner()
    workflow.scan(workspace, root.root_id)
    grouped = workflow.configure_contracts(workspace, root.root_id, confirmed=True)
    ids = [item.contract_id for item in grouped]
    for index, contract_id in enumerate(ids):
        workflow.set_contract_alias(workspace, contract_id, f"contract-{index}")
        workflow.refresh_contracts(workspace, contract_ids=[contract_id])
    return workspace, ids


def test_default_deny_and_explicit_grant(tmp_path: Path) -> None:
    workspace, ids = _contracts(tmp_path)
    contract_id = ids[0]

    assert mcp_gateway.discover(workspace.db).contracts == []
    assert mcp_gateway.export(workspace.db, "contract-0").available is False

    workflow.set_contract_alias(workspace, contract_id, "alpha")
    granted = mcp_gateway.grant_field(workspace.db, contract_id, "provider")

    assert granted.alias == "alpha"
    discovery = mcp_gateway.discover(workspace.db)
    assert [item.alias for item in discovery.contracts] == ["alpha"]
    exported = mcp_gateway.export(workspace.db, "alpha")
    assert exported.available is True
    assert set(exported.facts) == {"provider"}
    assert exported.facts["provider"].value == "Beispiel Versicherung AG"
    assert exported.facts["provider"].verification == "explicit"


def test_unknown_field_and_revocation_are_immediate(tmp_path: Path) -> None:
    workspace, ids = _contracts(tmp_path)
    contract_id = ids[0]
    workflow.set_contract_alias(workspace, contract_id, "alpha")
    with pytest.raises(CoreError, match="allowlist"):
        mcp_gateway.grant_field(workspace.db, contract_id, "raw_text")
    mcp_gateway.grant_field(workspace.db, contract_id, "end_date")

    mcp_gateway.revoke_field(workspace.db, contract_id, "end_date")

    assert mcp_gateway.discover(workspace.db).contracts == []
    assert mcp_gateway.export(workspace.db, "alpha").available is False


def test_hidden_contract_and_conflict_do_not_leak(tmp_path: Path) -> None:
    workspace, ids = _contracts(tmp_path, second=True)
    workflow.set_contract_alias(workspace, ids[0], "alpha")
    workflow.set_contract_alias(workspace, ids[1], "beta")
    mcp_gateway.grant_field(workspace.db, ids[0], "provider")

    discovery = mcp_gateway.discover(workspace.db)
    assert [item.alias for item in discovery.contracts] == ["alpha"]
    denied = mcp_gateway.export(workspace.db, "beta")
    assert denied.available is False
    assert denied.facts == {}
    assert denied.alias == ""


def test_conflicting_fact_exports_state_without_value(tmp_path: Path) -> None:
    workspace, ids = _contracts(tmp_path)
    folder = tmp_path / "library" / "alpha"
    (folder / "second.txt").write_text(
        _TEXT.replace("Vertragsbeginn: 2026-01-01", "Vertragsbeginn: 2026-02-01")
    )
    root = workflow.list_roots(workspace)[0]
    workflow.scan(workspace, root.root_id)
    workflow.refresh_contracts(workspace, contract_ids=[ids[0]])
    workflow.set_contract_alias(workspace, ids[0], "alpha")
    mcp_gateway.grant_field(workspace.db, ids[0], "start_date")

    exported = mcp_gateway.export(workspace.db, "alpha")

    assert exported.facts["start_date"].verification == "conflicting"
    assert exported.facts["start_date"].value is None


def test_stdio_roundtrip_and_next_request_revocation(tmp_path: Path) -> None:
    workspace, ids = _contracts(tmp_path)
    workflow.set_contract_alias(workspace, ids[0], "alpha")
    mcp_gateway.grant_field(workspace.db, ids[0], "provider")
    mcp_gateway.grant_field(workspace.db, ids[0], "end_date")

    async def scenario() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "renamr",
                "mcp",
                "serve",
                "--state-dir",
                str(workspace.state_dir),
            ],
        )
        async with Client(params) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == [
                "list_approved_contracts",
                "get_contract_facts",
            ]
            discovery = await client.call_tool("list_approved_contracts", {})
            assert discovery.structured_content == {
                "contracts": [{"available": True, "alias": "alpha", "facts": {}}],
                "limit": 50,
            }
            exported = await client.call_tool("get_contract_facts", {"alias": "alpha"})
            structured = exported.structured_content
            assert structured is not None
            assert set(structured["facts"]) == {"provider", "end_date"}
            content = exported.content[0]
            assert isinstance(content, TextContent)
            assert '"value": "Beispiel Versicherung AG"' in content.text
            assert "evidence_quote" not in content.text
            assert "source_revision_id" not in content.text

            mcp_gateway.revoke_field(workspace.db, ids[0], "provider")
            after = await client.call_tool("get_contract_facts", {"alias": "alpha"})
            assert after.structured_content is not None
            assert set(after.structured_content["facts"]) == {"end_date"}

            denied = await client.call_tool("get_contract_facts", {"alias": "unknown"})
            assert denied.structured_content == {
                "available": False,
                "alias": "",
                "facts": {},
            }
            assert denied.is_error is False

    asyncio.run(scenario())
