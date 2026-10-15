"""Shared offline fakes and corpus helpers for the P0 unit tests.

This module is intentionally not named ``test_*`` so pytest does not collect it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from renamr import local_transport
from renamr.benchmark_cases import PARA_LOCAL_V1, RuleDialogCase, SyntheticDocument
from renamr.benchmark_schema import (
    DocumentFeatures,
    GroundingEvidence,
    RuleDraft,
    RulePredicate,
)
from renamr.local_client import LocalOllamaClient
from renamr.local_transport import LocalServerError

ISSUE_DATE_LABELS = ("Rechnungsdatum", "Invoice date", "Ausgestellt am", "Erstellt am", "Date: ")


class FakeResponse:
    """Minimal stand-in for ``http.client.HTTPResponse``."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            return self._body
        return self._body[:amount]


class FakeConnection:
    """Minimal stand-in for ``http.client.HTTPConnection``."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests.append((method, path, body, headers or {}))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class FakeServer:
    """Canned Ollama server used to patch a client's transport."""

    def __init__(
        self,
        *,
        models: list[dict[str, Any]] | None = None,
        show: dict[str, Any] | None = None,
        chat: dict[str, Any] | None = None,
        show_status: int = 200,
    ) -> None:
        self.models = models if models is not None else [{"name": "gemma4:e4b", "size": 9}]
        self.show = (
            show
            if show is not None
            else {
                "capabilities": ["completion", "vision"],
                "details": {"family": "gemma4", "parameter_size": "8.1B", "format": "safetensors"},
            }
        )
        self.chat = chat if chat is not None else {"message": {"content": "{}"}}
        self.show_status = show_status
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.chat_payload: dict[str, Any] | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, payload))
        if path == "/api/tags":
            return {"models": self.models}
        if path == "/api/show":
            if self.show_status != 200:
                raise LocalServerError(
                    "show failed", status=self.show_status, category="server_error"
                )
            return self.show
        if path == "/api/chat":
            self.chat_payload = payload
            return self.chat
        raise AssertionError(f"unexpected path {path}")


def json_body(payload: Any) -> bytes:
    """Encode a JSON response body."""
    return json.dumps(payload).encode("utf-8")


def install_server(
    monkeypatch: pytest.MonkeyPatch, client: LocalOllamaClient, server: FakeServer
) -> None:
    """Route a client's transport requests to a canned server."""
    monkeypatch.setattr(client._transport, "request", server.request)


def install_connection(
    monkeypatch: pytest.MonkeyPatch, status: int, payload: Any
) -> FakeConnection:
    """Route a client's socket opens to a canned HTTP response."""
    connection = FakeConnection(FakeResponse(status, json_body(payload)))
    monkeypatch.setattr(
        local_transport,
        "_open_connection",
        lambda host, port, timeout: connection,
    )
    return connection


def find_case(case_id: str) -> SyntheticDocument:
    """Return the corpus case with the given id."""
    for document in PARA_LOCAL_V1:
        if document.case_id == case_id:
            return document
    raise AssertionError(f"unknown case id: {case_id}")


def grounded_features(document: SyntheticDocument) -> DocumentFeatures:
    """Build a feature response that matches the manifest and is grounded."""
    stripped = document.page_texts[0].strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    evidence = [GroundingEvidence(page=1, source_text=first_line)] if first_line else []
    return DocumentFeatures(
        document_type=document.expected.document_type or "notice",
        issuer=document.expected.expected_issuer,
        date=document.expected.expected_date,
        needs_review=document.expected.needs_review,
        evidence=evidence,
    )


def valid_rule_draft(case: RuleDialogCase) -> RuleDraft:
    """Build a rule draft that satisfies the case's required semantics."""
    return RuleDraft(
        scope_root_id=case.scope_root_id,
        target_root_id=case.target_root_id,
        target_subdir=case.required_target_subdir or "invoices",
        filename_template="{date}_{subject}",
        predicates=[
            RulePredicate(
                field="document_type",
                operator="equals",
                values=[case.required_document_type or "invoice"],
            )
        ],
    )
