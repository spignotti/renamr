"""Shared application models for the scaffold."""

from pydantic import BaseModel, Field


class LoggingConfig(BaseModel):
    """Logging-related configuration."""

    level: str = Field(default="INFO")
    json_logs: bool = Field(default=False)


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    model: str = Field(default="gpt-4o-mini")
    api_base: str | None = None


class AppConfig(BaseModel):
    """Top-level application configuration."""

    inbox_path: str = Field(default=".")
    file_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".jpg", ".jpeg", ".png"])
    llm: LLMConfig = Field(default_factory=LLMConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
