"""Tests for the guarded local-only inference adapter (offline)."""

from __future__ import annotations

import pytest

from renamr.benchmark_schema import DocumentFeatures
from renamr.core.errors import InferenceError
from renamr.core.inference import LocalInference, local_health
from renamr.core.settings import DEFAULT_SETTINGS
from renamr.local_client import LocalOllamaClient
from renamr.local_transport import ChatMessage, EndpointValidationError
from tests.unit.p0_support import FakeServer, install_connection, install_server

_FEATURES = {"document_type": "invoice", "needs_review": False, "evidence": []}


def _payload(content: dict[str, object]) -> dict[str, object]:
    import json

    return {"message": {"content": json.dumps(content)}, "eval_count": 3}


def test_inference_rejects_remote_endpoint() -> None:
    with pytest.raises(EndpointValidationError):
        LocalInference(endpoint="https://api.openai.com")


def test_inference_from_settings_uses_loopback() -> None:
    inference = LocalInference.from_settings(DEFAULT_SETTINGS)
    assert inference.client.endpoint == "http://127.0.0.1:11434"
    assert inference.output_mode == "validated_prompt"


def test_inference_complete_validates_and_sends_no_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LocalOllamaClient()
    server = FakeServer(chat=_payload(_FEATURES))
    install_server(monkeypatch, client, server)
    inference = LocalInference(output_mode="validated_prompt", client=client)

    result = inference.complete(
        "gemma4:e4b",
        [ChatMessage(role="system", content="Extract features.")],
        DocumentFeatures,
    )

    assert result.data.document_type == "invoice"
    assert server.chat_payload is not None
    assert "format" not in server.chat_payload


def test_inference_requires_vision_for_images(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(
        monkeypatch,
        client,
        FakeServer(show={"capabilities": ["completion"], "details": {"family": "x"}}),
    )
    inference = LocalInference(client=client)

    with pytest.raises(InferenceError):
        inference.complete(
            "gemma4:e4b",
            [ChatMessage(role="user", content="text")],
            DocumentFeatures,
            images=["AAAA"],
        )


def test_inference_rejects_cloud_model(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(monkeypatch, client, FakeServer())
    inference = LocalInference(client=client)

    with pytest.raises(InferenceError):
        inference.complete(
            "gemma4:cloud", [ChatMessage(role="user", content="text")], DocumentFeatures
        )


def test_inference_health_reflects_server(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(monkeypatch, client, FakeServer())
    assert local_health(LocalInference(client=client)) is True

    install_connection(monkeypatch, 500, {"error": "boom"})
    assert local_health(LocalInference(client=LocalOllamaClient())) is False


def test_inference_blocks_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_connection(monkeypatch, 302, {"location": "http://example.com"})
    inference = LocalInference(client=client)

    with pytest.raises(InferenceError) as error:
        inference.complete(
            "gemma4:e4b", [ChatMessage(role="user", content="text")], DocumentFeatures
        )
    assert error.value.category == "request_rejected"


def test_per_request_timeout_reaches_preflight_and_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LocalOllamaClient()
    server = FakeServer(chat=_payload(_FEATURES))
    install_server(monkeypatch, client, server)
    inference = LocalInference(output_mode="validated_prompt", client=client)

    inference.complete(
        "gemma4:e4b",
        [ChatMessage(role="user", content="Extract features.")],
        DocumentFeatures,
        timeout=2.5,
    )

    assert server.timeouts
    assert all(timeout == 2.5 for timeout in server.timeouts)


def test_manual_unbounded_call_preserves_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LocalOllamaClient()
    server = FakeServer(chat=_payload(_FEATURES))
    install_server(monkeypatch, client, server)
    inference = LocalInference(output_mode="validated_prompt", client=client)

    inference.complete(
        "gemma4:e4b",
        [ChatMessage(role="user", content="Extract features.")],
        DocumentFeatures,
    )

    assert server.timeouts
    assert all(timeout is None for timeout in server.timeouts)
