"""Schema-constrained local inference client over the loopback transport.

The client validates the endpoint and local model metadata before every request,
bounded by the transport's size limits, and never triggers a model download.

Two output modes exist because some local runtimes cannot enforce a grammar. In
``native`` mode the JSON schema is sent as the request ``format``. In
``validated_prompt`` mode the schema is embedded in the system message and the
response is still parsed with the same strict Pydantic validation. The mode is
chosen explicitly by the caller; there is no automatic fallback.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from renamr.local_transport import (
    DEFAULT_KEEP_ALIVE,
    DEFAULT_NUM_CTX,
    DEFAULT_NUM_PREDICT,
    DEFAULT_TIMEOUT,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ChatMessage,
    ChatMetrics,
    ConfigurationError,
    InstalledModel,
    LocalClientError,
    LocalModelInfo,
    LocalOllamaTransport,
    LocalServerError,
    ModelCapabilityError,
    ModelNameError,
    ModelNotInstalledError,
    OutputMode,
    SchemaOutputError,
    StructuredResult,
    _as_int,
    _as_str,
    _capability_list,
    normalize_model_name,
)


class LocalModelRegistry:
    """Local model inventory and metadata backed by a transport."""

    def __init__(self, transport: LocalOllamaTransport) -> None:
        self._transport = transport

    def list_models(self) -> list[InstalledModel]:
        """Return the models reported by ``/api/tags``."""
        payload = self._transport.request("GET", "/api/tags", None)
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise LocalServerError(
                "local Ollama server returned an unexpected model list",
                category="invalid_payload",
            )
        models: list[InstalledModel] = []
        for entry in raw_models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            if not isinstance(name, str) or not name:
                continue
            models.append(
                InstalledModel(
                    name=name,
                    size=_as_int(entry.get("size")),
                    digest=_as_str(entry.get("digest")),
                )
            )
        return models

    def show_model(self, model: str) -> LocalModelInfo:
        """Return verified local metadata for an installed model.

        Verification requires the name to be present in the local inventory plus
        positive ``/api/show`` evidence: a non-empty capability list including
        ``completion`` and non-empty local details with a known family or format.

        Args:
            model: Native or ``ollama/``-prefixed model reference.

        Returns:
            Local metadata including advertised capabilities.

        Raises:
            ModelNameError: If the model is remote/cloud-backed, missing from the
                inventory, or lacks verified local capabilities or details.
            LocalServerError: If the server is unreachable or rejects the request.
        """
        name = normalize_model_name(model)
        installed = {item.name: item for item in self.list_models()}
        if name not in installed:
            raise ModelNotInstalledError(
                f"model '{name}' is not present in the local model inventory; install it "
                "explicitly with 'ollama pull' before inference (renamr never downloads models)"
            )
        payload = self._show_payload(name)
        capabilities = _capability_list(payload.get("capabilities"))
        details = payload.get("details")
        details_map = details if isinstance(details, dict) else {}
        family = _as_str(details_map.get("family"))
        known_format = _as_str(details_map.get("format"))
        if not capabilities:
            raise ModelNameError(
                f"model '{name}' reports no local capabilities; refusing unverified inference"
            )
        if "completion" not in capabilities:
            raise ModelCapabilityError(f"model '{name}' does not report text generation capability")
        if not details_map or (family is None and known_format is None):
            raise ModelNameError(
                f"model '{name}' has no verified local details; refusing unverified inference"
            )
        return LocalModelInfo(
            name=name,
            digest=installed[name].digest,
            capabilities=capabilities,
            family=family,
            parameter_size=_as_str(details_map.get("parameter_size")),
            quantization=_as_str(details_map.get("quantization_level")),
        )

    def _show_payload(self, name: str) -> dict[str, Any]:
        """Fetch `/api/show` payload, rejecting remote metadata."""
        try:
            payload = self._transport.request("POST", "/api/show", {"model": name})
        except LocalServerError as error:
            if error.status == 404:
                raise ModelNotInstalledError(
                    f"model '{name}' is not installed locally; install it explicitly "
                    "with 'ollama pull' before inference (renamr never downloads models)"
                ) from None
            raise
        if _as_str(payload.get("remote_host")) or _as_str(payload.get("remote_model")):
            raise ModelNameError(
                f"model '{name}' resolves to a remote/cloud model; only locally "
                "installed models are allowed"
            )
        return payload


class LocalOllamaClient:
    """Inference client for a loopback-only Ollama server."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:11434",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        keep_alive: str = DEFAULT_KEEP_ALIVE,
        num_predict: int = DEFAULT_NUM_PREDICT,
        num_ctx: int = DEFAULT_NUM_CTX,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._transport = LocalOllamaTransport(
            endpoint,
            timeout=timeout,
            max_request_bytes=max_request_bytes,
            max_response_bytes=max_response_bytes,
        )
        self._registry = LocalModelRegistry(self._transport)
        if not keep_alive.strip():
            raise ConfigurationError("keep_alive must not be empty")
        if num_predict < 1 or num_ctx < 1:
            raise ConfigurationError("num_predict and num_ctx must be positive")
        self._keep_alive = keep_alive
        self._num_predict = num_predict
        self._num_ctx = num_ctx

    @property
    def endpoint(self) -> str:
        """Return the normalized, credential-free endpoint for display."""
        return self._transport.endpoint

    @property
    def registry(self) -> LocalModelRegistry:
        """Return the model inventory bound to this client's transport."""
        return self._registry

    def chat_structured[ModelT: BaseModel](
        self,
        model: str,
        messages: Sequence[ChatMessage],
        schema: type[ModelT],
        *,
        output_mode: OutputMode = "native",
        images: Sequence[str] = (),
        keep_alive: str | None = None,
        timeout: float | None = None,
        require_vision: bool = False,
    ) -> StructuredResult[ModelT]:
        """Run one non-streaming, schema-constrained chat completion.

        Args:
            model: Model reference; validated against local metadata first.
            messages: System/user/assistant messages.
            schema: Pydantic model used for schema validation in both modes.
            output_mode: ``native`` sends the schema as ``format``;
                ``validated_prompt`` embeds the schema in the system message.
            images: Optional base64 image payloads attached to the user message.
            keep_alive: Model retention override for this request.
            timeout: Per-request timeout override in seconds.
            require_vision: Force the vision capability check even without images.

        Returns:
            The validated response, timing metrics, and the mode actually used.

        Raises:
            ConfigurationError: If ``output_mode`` is not a known mode.
            ModelNameError: If the model is not a verified local model.
            ModelCapabilityError: If a required capability is not advertised.
            LocalClientError: On transport, size, or schema failures.
        """
        _validate_output_mode(output_mode)
        info = self._registry.show_model(model)
        _require_capabilities(info, images, require_vision)
        payload = _chat_payload(
            model=info.name,
            messages=_apply_output_mode(messages, schema, output_mode),
            schema=schema,
            images=images,
            keep_alive=keep_alive or self._keep_alive,
            output_mode=output_mode,
            options=self._options(),
        )
        response = self._transport.request("POST", "/api/chat", payload, timeout=timeout)
        return _parse_response(response, schema, output_mode)

    def _options(self) -> dict[str, int | float]:
        """Return bounded deterministic generation options."""
        return {"temperature": 0, "num_predict": self._num_predict, "num_ctx": self._num_ctx}


def _require_capabilities(
    info: LocalModelInfo, images: Sequence[str], require_vision: bool
) -> None:
    """Raise when the installed model lacks a capability required by the call."""
    missing: list[str] = []
    if not info.supports_completion:
        missing.append("completion")
    if (images or require_vision) and not info.supports_vision:
        missing.append("vision")
    if missing:
        raise ModelCapabilityError(
            f"model '{info.name}' does not report the required capability: {', '.join(missing)}"
        )


def _validate_output_mode(output_mode: str) -> None:
    """Reject an unknown output mode with a sanitized error before any request."""
    if output_mode not in ("native", "validated_prompt"):
        raise ConfigurationError("unknown output mode")


def _apply_output_mode(
    messages: Sequence[ChatMessage], schema: type[BaseModel], output_mode: OutputMode
) -> list[ChatMessage]:
    """Embed the JSON schema in the system message for prompted output mode."""
    if output_mode == "native":
        return list(messages)
    schema_text = json.dumps(schema.model_json_schema(), sort_keys=True)
    instruction = (
        "Return one JSON object that validates against this JSON schema exactly, with "
        f"no extra keys, markdown fences, or surrounding text:\n{schema_text}"
    )
    result = list(messages)
    for index, message in enumerate(result):
        if message.role == "system":
            result[index] = ChatMessage(
                role="system", content=f"{message.content}\n\n{instruction}"
            )
            return result
    return [ChatMessage(role="system", content=instruction), *result]


def _chat_payload(
    *,
    model: str,
    messages: Sequence[ChatMessage],
    schema: type[BaseModel],
    images: Sequence[str],
    keep_alive: str,
    output_mode: OutputMode,
    options: dict[str, int | float],
) -> dict[str, Any]:
    """Build a non-streaming chat request payload for the requested output mode."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": _messages_payload(messages, images),
        "stream": False,
        "options": options,
        "think": False,
        "keep_alive": keep_alive,
    }
    if output_mode == "native":
        payload["format"] = schema.model_json_schema()
    return payload


def _messages_payload(
    messages: Sequence[ChatMessage], images: Sequence[str]
) -> list[dict[str, Any]]:
    """Serialize messages and attach optional images to the last user message."""
    if not messages:
        raise LocalClientError("at least one chat message is required")
    payload = [message.model_dump() for message in messages]
    if not images:
        return payload
    if len(images) > MAX_IMAGES:
        raise LocalClientError(f"at most {MAX_IMAGES} images are allowed per request")
    for image in images:
        _validate_image(image)
    for entry in reversed(payload):
        if entry["role"] == "user":
            entry["images"] = list(images)
            return payload
    raise LocalClientError("images require a user message")


def _validate_image(image: Any) -> None:
    """Reject non-base64 or oversized image payloads before they are sent."""
    if not isinstance(image, str) or not image:
        raise LocalClientError("image payloads must be non-empty base64 strings")
    try:
        decoded = base64.b64decode(image, validate=True)
    except (binascii.Error, ValueError):
        raise LocalClientError("image payloads must be valid base64 data") from None
    if not decoded:
        raise LocalClientError("image payloads must not decode to empty bytes")
    if len(decoded) > MAX_IMAGE_BYTES:
        raise LocalClientError("image payload exceeds the configured size limit")


def _parse_response[ModelT: BaseModel](
    response: dict[str, Any], schema: type[ModelT], output_mode: OutputMode
) -> StructuredResult[ModelT]:
    """Parse model content into the schema with strict validation in both modes."""
    content = _message_content(response)
    try:
        data = schema.model_validate_json(content)
    except ValidationError:
        raise SchemaOutputError("local model output did not match the required schema") from None
    return StructuredResult[ModelT](
        data=data, metrics=_metrics_from(response), output_mode=output_mode
    )


def _message_content(response: dict[str, Any]) -> str:
    """Extract the assistant content string from a chat response."""
    message = response.get("message")
    if not isinstance(message, dict):
        raise SchemaOutputError("local model returned no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise SchemaOutputError("local model returned an empty message")
    return content


def _metrics_from(response: dict[str, Any]) -> ChatMetrics:
    """Build timing metrics from the chat response counters."""
    return ChatMetrics(
        total_duration_ns=_as_int(response.get("total_duration")),
        load_duration_ns=_as_int(response.get("load_duration")),
        prompt_eval_count=_as_int(response.get("prompt_eval_count")),
        eval_count=_as_int(response.get("eval_count")),
    )
