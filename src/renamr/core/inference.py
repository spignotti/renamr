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
        client: LocalOllamaClient | None = None,
    ) -> None:
        self._output_mode: OutputMode = output_mode
        self._client = client or LocalOllamaClient(
            endpoint, timeout=timeout, num_predict=num_predict, num_ctx=num_ctx
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
        cls, settings: Settings, *, client: LocalOllamaClient | None = None
    ) -> LocalInference:
        """Build an adapter from database-owned settings."""
        return cls(
            endpoint=settings.endpoint,
            output_mode=settings.output_mode,
            timeout=settings.request_timeout_s,
            num_predict=settings.num_predict,
            num_ctx=settings.num_ctx,
            client=client,
        )

    def health(self) -> bool:
        """Return whether the local server answers a bounded inventory request."""
        try:
            self._client.registry.list_models()
        except LocalClientError:
            return False
        return True

    def verify_model(self, model: str) -> LocalModelInfo:
        """Return verified local metadata for a model.

        Raises:
            InferenceError: If the model is not a verified local model.
        """
        try:
            return self._client.registry.show_model(model)
        except LocalClientError as exc:
            raise InferenceError(exc.category) from None

    def complete[ModelT: BaseModel](
        self,
        model: str,
        messages: Sequence[ChatMessage],
        schema: type[ModelT],
        *,
        output_mode: OutputMode | None = None,
        images: Sequence[str] = (),
        require_vision: bool = False,
    ) -> StructuredResult[ModelT]:
        """Run one validated structured completion.

        Args:
            model: Local model reference.
            messages: System/user/assistant messages.
            schema: Pydantic schema for strict output validation.
            output_mode: Optional per-call mode override.
            images: Optional base64 image payloads.
            require_vision: Force the vision capability check.

        Returns:
            The validated response with metrics and the mode used.

        Raises:
            InferenceError: On any transport, capability, or schema failure.
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
            )
        except LocalClientError as exc:
            raise InferenceError(exc.category) from None
