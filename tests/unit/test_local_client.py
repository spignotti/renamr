"""Tests for the native local Ollama client (offline, no network access)."""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import pytest

from renamr import local_client, local_transport
from renamr.benchmark_schema import DocumentFeatures
from renamr.local_client import LocalOllamaClient
from renamr.local_transport import (
    ChatMessage,
    ConfigurationError,
    EndpointValidationError,
    LocalClientError,
    LocalServerError,
    ModelCapabilityError,
    ModelNameError,
    ModelNotInstalledError,
    OutputMode,
    ResponseTooLargeError,
    SchemaOutputError,
    normalize_model_name,
    validate_endpoint,
)
from tests.unit.p0_support import (
    FakeConnection,
    FakeResponse,
    FakeServer,
    install_connection,
    install_server,
    json_body,
)

_EMPTY_FEATURES = {"document_type": "invoice", "needs_review": False, "evidence": []}


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:11434", ("127.0.0.1", 11434)),
        ("http://localhost", ("127.0.0.1", 11434)),
        ("http://localhost:8080/", ("127.0.0.1", 8080)),
        ("http://[::1]:11434", ("::1", 11434)),
    ],
)
def test_validate_endpoint_accepts_loopback(endpoint: str, expected: tuple[str, int]) -> None:
    assert validate_endpoint(endpoint) == expected


def test_localhost_maps_to_literal_loopback() -> None:
    assert validate_endpoint("http://localhost:11434") == ("127.0.0.1", 11434)
    assert LocalOllamaClient("http://localhost:11434").endpoint == "http://127.0.0.1:11434"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434",
        "http://example.com:11434",
        "http://user:pass@127.0.0.1:11434",
        "http://127.0.0.1:11434?debug=1",
        "http://127.0.0.1:11434#fragment",
        "http://127.0.0.1:11434/api",
        "127.0.0.1:11434",
        "http://127.0.0.1:not-a-port",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "http://localhost.evil.example:11434",
    ],
)
def test_validate_endpoint_rejects_non_loopback(endpoint: str) -> None:
    with pytest.raises(EndpointValidationError):
        validate_endpoint(endpoint)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("gemma4:e4b", "gemma4:e4b"),
        ("ollama/gemma4:e4b", "gemma4:e4b"),
        ("llama3.1:8b", "llama3.1:8b"),
        ("library/llama3", "library/llama3"),
    ],
)
def test_normalize_model_name_accepts_local_names(raw: str, expected: str) -> None:
    assert normalize_model_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ollama/",
        "gemma4:cloud",
        "model-cloud",
        "http://host/model",
        "a/../../b",
        "bad name",
    ],
)
def test_normalize_model_name_rejects_remote_or_malformed(raw: str) -> None:
    with pytest.raises(ModelNameError):
        normalize_model_name(raw)


def test_endpoint_property_is_credential_free() -> None:
    client = LocalOllamaClient("http://localhost:11434")
    assert client.endpoint == "http://127.0.0.1:11434"
    assert LocalOllamaClient("http://[::1]:11434").endpoint == "http://[::1]:11434"


def test_transport_rejects_invalid_bounds() -> None:
    with pytest.raises(ConfigurationError):
        LocalOllamaClient(timeout=0)
    with pytest.raises(ConfigurationError):
        LocalOllamaClient(max_request_bytes=0)
    with pytest.raises(ConfigurationError):
        LocalOllamaClient(max_response_bytes=0)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_transport_rejects_non_finite_constructor_timeout(timeout: float) -> None:
    with pytest.raises(ConfigurationError):
        LocalOllamaClient(timeout=timeout)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_request_rejects_invalid_timeout_before_opening_socket(
    monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    connection = install_connection(monkeypatch, 200, {"models": []})

    with pytest.raises(ConfigurationError):
        LocalOllamaClient()._transport.request(
            "POST", "/api/chat", {"model": "gemma4:e4b"}, timeout=timeout
        )

    assert connection.requests == []


def test_request_accepts_positive_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, float] = {}

    def fake_open(host: str, port: int, timeout: float) -> FakeConnection:
        captured["timeout"] = timeout
        return FakeConnection(FakeResponse(200, json_body({"models": []})))

    monkeypatch.setattr(local_transport, "_open_connection", fake_open)
    LocalOllamaClient()._transport.request("GET", "/api/tags", None, timeout=2.5)

    assert captured["timeout"] == 2.5


def test_client_rejects_invalid_generation_bounds() -> None:
    with pytest.raises(ConfigurationError):
        LocalOllamaClient(num_predict=0)
    with pytest.raises(ConfigurationError):
        LocalOllamaClient(num_ctx=0)
    with pytest.raises(ConfigurationError):
        LocalOllamaClient(keep_alive="  ")


def test_request_uses_direct_loopback_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_open(host: str, port: int, timeout: float) -> FakeConnection:
        captured["host"] = host
        captured["port"] = port
        return FakeConnection(FakeResponse(200, json_body({"models": []})))

    monkeypatch.setattr(local_transport, "_open_connection", fake_open)
    client = LocalOllamaClient("http://localhost:11434")
    client.registry.list_models()

    assert captured == {"host": "127.0.0.1", "port": 11434}


def test_list_models_parses_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = install_connection(
        monkeypatch,
        200,
        {
            "models": [
                {"name": "gemma4:e4b", "size": 123, "digest": "abc"},
                {"model": "llama3.1:8b"},
                {"name": ""},
                "unexpected",
            ]
        },
    )
    models = LocalOllamaClient().registry.list_models()

    assert [model.name for model in models] == ["gemma4:e4b", "llama3.1:8b"]
    assert models[0].size == 123
    assert connection.requests[0][0] == "GET"
    assert connection.requests[0][1] == "/api/tags"


def test_show_model_reads_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(monkeypatch, client, FakeServer())
    info = client.registry.show_model("ollama/gemma4:e4b")

    assert info.name == "gemma4:e4b"
    assert info.supports_completion is True
    assert info.supports_vision is True


def test_show_model_requires_inventory_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(monkeypatch, client, FakeServer(models=[{"name": "other:latest"}]))

    with pytest.raises(ModelNotInstalledError, match="inventory"):
        client.registry.show_model("gemma4:e4b")


def test_show_model_requires_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(
        monkeypatch, client, FakeServer(show={"capabilities": [], "details": {"family": "x"}})
    )

    with pytest.raises(ModelNameError, match="no local capabilities"):
        client.registry.show_model("gemma4:e4b")


def test_show_model_requires_completion_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(
        monkeypatch,
        client,
        FakeServer(show={"capabilities": ["vision"], "details": {"family": "x"}}),
    )

    with pytest.raises(ModelCapabilityError, match="text generation"):
        client.registry.show_model("gemma4:e4b")


def test_show_model_requires_verified_details(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(
        monkeypatch,
        client,
        FakeServer(show={"capabilities": ["completion"], "details": {}}),
    )

    with pytest.raises(ModelNameError, match="no verified local details"):
        client.registry.show_model("gemma4:e4b")


def test_show_model_rejects_remote_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(
        monkeypatch,
        client,
        FakeServer(show={"capabilities": ["completion"], "remote_host": "ollama.com"}),
    )

    with pytest.raises(ModelNameError):
        client.registry.show_model("gemma4:e4b")


def test_show_model_missing_manifest_reports_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LocalOllamaClient()
    install_server(monkeypatch, client, FakeServer(show_status=404))

    with pytest.raises(ModelNotInstalledError, match="never downloads models"):
        client.registry.show_model("gemma4:e4b")


def _features_payload(features: dict[str, Any]) -> dict[str, Any]:
    return {"message": {"content": json.dumps(features)}, "eval_count": 7}


def test_chat_structured_native_mode_sends_format(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    server = FakeServer(chat=_features_payload(_EMPTY_FEATURES))
    install_server(monkeypatch, client, server)

    result = client.chat_structured(
        "gemma4:e4b", [ChatMessage(role="user", content="text")], DocumentFeatures
    )

    assert result.data.document_type == "invoice"
    assert result.metrics.eval_count == 7
    assert result.output_mode == "native"
    payload = server.chat_payload
    assert payload is not None
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["options"] == {"temperature": 0, "num_predict": 512, "num_ctx": 4096}
    assert payload["keep_alive"] == "5m"
    assert payload["format"]["type"] == "object"


def test_chat_structured_rejects_unknown_output_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    server = FakeServer(chat=_features_payload(_EMPTY_FEATURES))
    install_server(monkeypatch, client, server)

    with pytest.raises(ConfigurationError, match="output mode"):
        client.chat_structured(
            "gemma4:e4b",
            [ChatMessage(role="user", content="text")],
            DocumentFeatures,
            output_mode=cast(OutputMode, "bogus"),
        )

    assert server.calls == []  # validated before any request is sent


def test_chat_structured_validated_prompt_embeds_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    server = FakeServer(chat=_features_payload(_EMPTY_FEATURES))
    install_server(monkeypatch, client, server)

    result = client.chat_structured(
        "gemma4:e4b",
        [ChatMessage(role="system", content="Extract metadata.")],
        DocumentFeatures,
        output_mode="validated_prompt",
    )

    assert result.output_mode == "validated_prompt"
    payload = server.chat_payload
    assert payload is not None
    assert "format" not in payload
    system_message = payload["messages"][0]
    assert system_message["role"] == "system"
    assert "needs_review" in system_message["content"]
    assert "Extract metadata." in system_message["content"]


def test_chat_structured_prompted_mode_still_validates_strictly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LocalOllamaClient()
    server = FakeServer(chat={"message": {"content": "not json at all"}})
    install_server(monkeypatch, client, server)

    with pytest.raises(SchemaOutputError):
        client.chat_structured(
            "gemma4:e4b",
            [ChatMessage(role="system", content="Extract.")],
            DocumentFeatures,
            output_mode="validated_prompt",
        )
    chat_calls = [call for call in server.calls if call[1] == "/api/chat"]
    assert len(chat_calls) == 1  # no silent retry or fallback


def test_chat_structured_rejects_invalid_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    secret = "RAW_MODEL_OUTPUT_MARKER"
    install_server(monkeypatch, client, FakeServer(chat={"message": {"content": secret}}))

    with pytest.raises(SchemaOutputError) as error:
        client.chat_structured(
            "gemma4:e4b", [ChatMessage(role="user", content="text")], DocumentFeatures
        )

    assert secret not in str(error.value)


def test_chat_structured_requires_vision_for_images(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(
        monkeypatch,
        client,
        FakeServer(show={"capabilities": ["completion"], "details": {"family": "x"}}),
    )

    with pytest.raises(ModelCapabilityError, match="vision"):
        client.chat_structured(
            "gemma4:e4b",
            [ChatMessage(role="user", content="text")],
            DocumentFeatures,
            images=["AAAA"],
        )


def test_chat_structured_attaches_images_to_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    server = FakeServer(chat=_features_payload(_EMPTY_FEATURES))
    install_server(monkeypatch, client, server)

    client.chat_structured(
        "gemma4:e4b",
        [ChatMessage(role="user", content="text")],
        DocumentFeatures,
        images=["AAAA", "BBBB"],
    )

    assert server.chat_payload is not None
    assert server.chat_payload["messages"][0]["images"] == ["AAAA", "BBBB"]


def test_chat_structured_rejects_empty_message(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LocalOllamaClient()
    install_server(monkeypatch, client, FakeServer(chat={"message": {"content": "   "}}))

    with pytest.raises(SchemaOutputError):
        client.chat_structured(
            "gemma4:e4b", [ChatMessage(role="user", content="text")], DocumentFeatures
        )


def test_messages_payload_rejects_invalid_base64_images() -> None:
    with pytest.raises(LocalClientError, match="valid base64"):
        local_client._messages_payload([ChatMessage(role="user", content="text")], ["not base64!!"])


def test_messages_payload_bounds_image_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_client, "MAX_IMAGE_BYTES", 4)
    encoded = base64.b64encode(b"12345").decode("ascii")

    with pytest.raises(LocalClientError, match="size limit"):
        local_client._messages_payload([ChatMessage(role="user", content="text")], [encoded])


def test_request_sanitizes_server_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "RAW_PROVIDER_BODY_MARKER"
    install_connection(monkeypatch, 500, {"error": marker})

    with pytest.raises(LocalServerError) as error:
        LocalOllamaClient().registry.list_models()

    assert marker not in str(error.value)
    assert error.value.status == 500
    assert error.value.category == "server_error"


def test_request_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = install_connection(monkeypatch, 302, {"location": "http://example.com"})

    with pytest.raises(LocalServerError) as error:
        LocalOllamaClient().registry.list_models()

    assert error.value.status == 302
    assert len(connection.requests) == 1


def test_request_reports_unreachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(host: str, port: int, timeout: float) -> FakeConnection:
        raise OSError("connection refused")

    monkeypatch.setattr(local_transport, "_open_connection", fail)

    with pytest.raises(LocalServerError) as error:
        LocalOllamaClient().registry.list_models()

    assert error.value.category == "unreachable"


def test_request_bounds_response_size(monkeypatch: pytest.MonkeyPatch) -> None:
    install_connection(monkeypatch, 200, {"models": [], "padding": "x" * 100})
    client = LocalOllamaClient(max_response_bytes=10)

    with pytest.raises(ResponseTooLargeError):
        client.registry.list_models()


def test_request_bounds_request_size() -> None:
    client = LocalOllamaClient(max_request_bytes=3)

    with pytest.raises(LocalClientError, match="request exceeds"):
        client._transport.request("POST", "/api/show", {"model": "gemma4:e4b"})


def test_messages_payload_requires_user_message_for_images() -> None:
    with pytest.raises(LocalClientError, match="images require a user message"):
        local_client._messages_payload([ChatMessage(role="system", content="system")], ["AAAA"])
