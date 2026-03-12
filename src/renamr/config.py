"""Environment-backed settings for local development."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvironmentSettings(BaseSettings):
    """Secrets loaded from the environment or a local .env file."""

    openai_api_key: str | None = None
    openrouter_api_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
