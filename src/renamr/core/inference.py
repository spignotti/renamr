"""Guarded local-only inference adapter built on the P0 native client.

The adapter never introduces cloud paths: the endpoint must be loopback (the
transport enforces this), model references are normalized and validated against
the local inventory, and no request triggers a model download. Feature prompts
and pages are built by higher layers; this module only performs validated
structured calls.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from renamr.core.errors import InferenceError
from renamr.core.settings import Settings
from renamr.local_client import LocalOllamaClient
from renamr.local_transport import (
    ChatMessage,
    LocalClientError,
    LocalModelInfo,
    OutputMode,
    StructuredResult,
)


class LocalInference:
    """Thin, explicit wrapper around :class:`LocalOllamaClient`."""

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:11434",
        output_mode: OutputMode = "validated_prompt",
        timeout: float = 120.0,
        num_predict: int = 512,
        num_ctx: int = 4096,
        keep_alive: str = "5m",
        client: LocalOllamaClient | None = None,
    ) -> None:
        self._output_mode: OutputMode = output_mode
        self._client = client or LocalOllamaClient(
            endpoint,
            timeout=timeout,
            num_predict=num_predict,
            num_ctx=num_ctx,
            keep_alive=keep_alive,
        )

    @property
    def output_mode(self) -> OutputMode:
        """Return the configured output mode."""
        return self._output_mode

    @property
    def client(self) -> LocalOllamaClient:
        """Return the underlying local client (used by injected test doubles)."""
        return self._client

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        role: str = "main",
        client: LocalOllamaClient | None = None,
    ) -> LocalInference:
        """Build an adapter from database-owned settings for one model role."""
        keep_alive = getattr(settings.model_keep_alive, role, settings.model_keep_alive.main)
        return cls(
            endpoint=settings.endpoint,
            output_mode=settings.output_mode,
            timeout=settings.request_timeout_s,
            num_predict=settings.num_predict,
            num_ctx=settings.num_ctx,
            keep_alive=keep_alive,
            client=client,
        )

    def complete[ModelT: BaseModel](
        self,
        model: str,
        messages: Sequence[ChatMessage],
        schema: type[ModelT],
        *,
        output_mode: OutputMode | None = None,
        images: Sequence[str] = (),
        require_vision: bool = False,
        keep_alive: str | None = None,
        timeout: float | None = None,
    ) -> StructuredResult[ModelT]:
        """Run one strict-schema completion and return validated data.

        Args:
            model: Verified local model reference.
            messages: Chat messages.
            schema: Strict Pydantic output schema.
            output_mode: Optional per-call mode override.
            images: Optional base64 images.
            require_vision: Force the vision capability check.
            keep_alive: Optional model-retention override.
            timeout: Optional per-request HTTP timeout; None keeps the configured one.

        Returns:
            Validated data, timing metrics, and the mode used.

        Raises:
            InferenceError: On transport, capability, or schema failure.
        """
        mode = output_mode or self._output_mode
        try:
            return self._client.chat_structured(
                model,
                messages,
                schema,
                output_mode=mode,
                images=images,
                require_vision=require_vision,
                keep_alive=keep_alive,
                timeout=timeout,
            )
        except LocalClientError as exc:
            raise InferenceError(exc.category) from None


def local_health(inference: LocalInference) -> bool:
    """Return whether the local server answers a bounded inventory request."""
    try:
        inference.client.registry.list_models()
    except LocalClientError:
        return False
    return True


def verify_model(
    inference: LocalInference, model: str, *, timeout: float | None = None
) -> LocalModelInfo:
    """Return verified local metadata for a model.

    Args:
        inference: Local inference adapter.
        model: Local model name.
        timeout: Optional per-request timeout for inventory and show requests.

    Raises:
        InferenceError: If the model is not a verified local model.
    """
    try:
        return inference.client.registry.show_model(model, timeout=timeout)
    except LocalClientError as exc:
        raise InferenceError(exc.category) from None
