"""Loopback-only Ollama transport, endpoint validation, and shared types.

Uses the standard library HTTP client against a literal loopback host: no proxy,
no redirects, no model downloads, and sanitized, size-bounded request/response.
"""

from __future__ import annotations

import http.client
import json
import math
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

DEFAULT_PORT = 11434
DEFAULT_TIMEOUT = 120.0
DEFAULT_KEEP_ALIVE = "5m"
DEFAULT_NUM_PREDICT = 512
DEFAULT_NUM_CTX = 4096
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_COMPLETION_CAPABILITY = "completion"
_VISION_CAPABILITY = "vision"
_STATUS_CATEGORIES = {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "rate_limited"}


class LocalClientError(Exception):
    """Base error for local inference failures; messages are always sanitized."""

    category = "local_client_error"


class EndpointValidationError(LocalClientError):
    """Raised when an endpoint is not a bare loopback HTTP address."""

    category = "invalid_endpoint"


class ModelNameError(LocalClientError):
    """Raised when a model reference is empty, remote, or cloud-backed."""

    category = "invalid_model"


class ModelNotInstalledError(ModelNameError):
    """Raised when the requested model has no local manifest on the server."""

    category = "model_not_installed"


class ModelCapabilityError(ModelNameError):
    """Raised when an installed model lacks a required capability."""

    category = "model_capability"


class LocalServerError(LocalClientError):
    """Raised for transport failures and non-200 server responses."""

    category = "server_error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        category: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        if category is not None:
            self.category = category


class ResponseTooLargeError(LocalClientError):
    """Raised when the local response exceeds the configured byte bound."""

    category = "response_too_large"


class SchemaOutputError(LocalClientError):
    """Raised when model output is missing or does not match the schema."""

    category = "invalid_schema_output"


class ConfigurationError(LocalClientError):
    """Raised when a client limit or request bound is invalid."""

    category = "invalid_configuration"


OutputMode = Literal["native", "validated_prompt"]


class ChatMessage(BaseModel):
    """A single chat message; images are attached to a user message."""

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class InstalledModel(BaseModel):
    """An entry from the local model inventory (`/api/tags`)."""

    name: str
    size: int | None = None
    digest: str | None = None


class LocalModelInfo(BaseModel):
    """Local metadata for a model from `/api/show`."""

    name: str
    digest: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None

    @property
    def supports_completion(self) -> bool:
        """Return whether the model advertises text generation."""
        return _COMPLETION_CAPABILITY in self.capabilities

    @property
    def supports_vision(self) -> bool:
        """Return whether the model advertises image input."""
        return _VISION_CAPABILITY in self.capabilities


class ChatMetrics(BaseModel):
    """Token and duration counters reported by Ollama for one request."""

    total_duration_ns: int | None = None
    load_duration_ns: int | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None


class StructuredResult[ResultT: BaseModel](BaseModel):
    """A validated structured model response plus its timing metrics."""

    data: ResultT
    metrics: ChatMetrics
    output_mode: OutputMode


def validate_endpoint(endpoint: str) -> tuple[str, int]:
    """Validate that an endpoint is a bare loopback HTTP address.

    Args:
        endpoint: Candidate endpoint such as ``http://127.0.0.1:11434``.

    Returns:
        The validated host and port. ``localhost`` is mapped to ``127.0.0.1`` so
        no name resolution can steer the connection off the loopback interface.

    Raises:
        EndpointValidationError: For non-loopback hosts, non-HTTP schemes, ports
            outside 1-65535, or any credentials, query, fragment, or path.
    """
    parsed = urlsplit(endpoint.strip())
    if parsed.scheme != "http":
        raise EndpointValidationError("endpoint must use the http scheme")
    if parsed.username or parsed.password:
        raise EndpointValidationError("endpoint must not embed credentials")
    if parsed.query or parsed.fragment:
        raise EndpointValidationError("endpoint must not include a query or fragment")
    if parsed.path not in ("", "/"):
        raise EndpointValidationError("endpoint must not include a path")
    host = parsed.hostname
    if host is None or host.lower() not in _ALLOWED_HOSTS:
        raise EndpointValidationError(
            "endpoint host must be loopback (localhost, 127.0.0.1, or ::1)"
        )
    try:
        port = parsed.port
    except ValueError:
        raise EndpointValidationError("endpoint port is invalid") from None
    if port is None:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        raise EndpointValidationError("endpoint port must be between 1 and 65535")
    return _loopback_address(host.lower()), port


def _loopback_address(host: str) -> str:
    """Map ``localhost`` to a literal address that never needs DNS resolution."""
    return "127.0.0.1" if host == "localhost" else host


def normalize_model_name(model: str) -> str:
    """Return a validated native Ollama model name.

    Args:
        model: Model reference; an optional ``ollama/`` prefix is stripped.

    Returns:
        The native local model name.

    Raises:
        ModelNameError: For empty names, remote registry references, or cloud
            tags (``:cloud``, ``-cloud``).
    """
    name = model.strip()
    if name.lower().startswith("ollama/"):
        name = name[len("ollama/") :]
    if not name:
        raise ModelNameError("model name is empty")
    if "://" in name:
        raise ModelNameError("model name must not reference a remote endpoint")
    if ".." in name:
        raise ModelNameError("model name must not contain path traversal")
    if not _MODEL_NAME_PATTERN.match(name):
        raise ModelNameError("model name contains unsupported characters")
    if "/" in name and "." in name.split("/", 1)[0]:
        raise ModelNameError(
            "model name must reference a locally installed model, not a remote registry"
        )
    last_segment = name.rsplit("/", 1)[-1]
    lowered = name.lower()
    cloud_tag = ":" in last_segment and last_segment.split(":", 1)[1].lower().startswith("cloud")
    if cloud_tag or lowered.endswith("-cloud"):
        raise ModelNameError("cloud/remote model tags are not allowed")
    return name


class LocalOllamaTransport:
    """Raw loopback HTTP transport for one Ollama server.

    Connections are direct ``http.client`` sockets to a literal loopback host, so
    no proxy is consulted and redirects are never followed.
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:11434",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._host, self._port = validate_endpoint(endpoint)
        self._timeout = _validate_timeout(timeout)
        if max_request_bytes < 1 or max_response_bytes < 1:
            raise ConfigurationError("size limits must be at least one byte")
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes

    @property
    def endpoint(self) -> str:
        """Return the normalized, credential-free endpoint for display."""
        host = f"[{self._host}]" if ":" in self._host else self._host
        return f"http://{host}:{self._port}"

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one size-bounded request and return a size-bounded JSON object."""
        effective_timeout = self._timeout if timeout is None else _validate_timeout(timeout)
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        if len(body) > self._max_request_bytes:
            raise LocalClientError("request exceeds the configured size limit")
        headers = {"Accept": "application/json"}
        if body:
            headers["Content-Type"] = "application/json"
        connection: http.client.HTTPConnection | None = None
        try:
            connection = _open_connection(self._host, self._port, effective_timeout)
            connection.request(method, path, body=body or None, headers=headers)
            response = connection.getresponse()
            status = response.status
            data = response.read(self._max_response_bytes + 1)
        except (OSError, http.client.HTTPException):
            raise LocalServerError(
                "local Ollama server is unreachable", category="unreachable"
            ) from None
        finally:
            if connection is not None:
                connection.close()
        if len(data) > self._max_response_bytes:
            raise ResponseTooLargeError("local server response exceeds the configured size limit")
        if status != 200:
            raise _status_error(status)
        return _decode_object(data)


def _validate_timeout(timeout: float) -> float:
    """Return a positive finite timeout or raise a sanitized error.

    Applied to both the constructor value and any per-request override, before
    the socket is opened, so a NaN, infinite, zero, or negative timeout can never
    reach ``http.client``.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigurationError("timeout must be a positive finite number")
    return timeout


def _open_connection(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    """Open a direct loopback connection with no proxy or redirect handling."""
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _decode_object(data: bytes) -> dict[str, Any]:
    """Decode a bounded JSON object or raise a sanitized payload error."""
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LocalServerError(
            "local Ollama server returned an unreadable payload", category="invalid_payload"
        ) from None
    if not isinstance(decoded, dict):
        raise LocalServerError(
            "local Ollama server returned an unexpected payload", category="invalid_payload"
        )
    return decoded


def _status_error(status: int) -> LocalServerError:
    """Map a non-200 status to a sanitized error without the response body."""
    category = _STATUS_CATEGORIES.get(status)
    if category is None:
        category = "server_error" if status >= 500 else "request_rejected"
    return LocalServerError(
        f"local Ollama server returned status {status} ({category})",
        status=status,
        category=category,
    )


def _capability_list(value: Any) -> list[str]:
    """Return only string entries from a capabilities payload."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _as_int(value: Any) -> int | None:
    """Return an int when the value is a real int, excluding booleans."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: Any) -> str | None:
    """Return a non-empty string value or None."""
    return value if isinstance(value, str) and value else None
