"""Safe, actionable error descriptions for CLI output and diagnostics."""

from litellm.exceptions import (
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    Timeout,
)


def is_fatal_provider_error(error: Exception) -> bool:
    """Return whether a provider configuration error should stop the run."""
    return isinstance(error, (AuthenticationError, PermissionDeniedError, NotFoundError))


def is_retryable_error(error: Exception) -> bool:
    """Return whether another request could reasonably succeed."""
    return not isinstance(
        error, (AuthenticationError, PermissionDeniedError, NotFoundError, BadRequestError)
    )


def describe_error(error: Exception) -> str:
    """Describe an error without including provider responses or secret values."""
    if isinstance(error, AuthenticationError):
        return (
            "Provider authentication failed. Check the provider API key in this process. "
            "For OpenRouter, supply OPENROUTER_API_KEY; with Infisical, use "
            "`infisical run -- renamr run`."
        )
    if isinstance(error, PermissionDeniedError):
        return "Provider access denied. Check the API key permissions and model access."
    if isinstance(error, NotFoundError):
        return "Provider model or endpoint unavailable. Check model and api_base in config.toml."
    if isinstance(error, BadRequestError):
        return "Provider rejected the request. Check model capabilities and configuration."
    if isinstance(error, RateLimitError):
        return "Provider rate limit reached. Check quota and try again later."
    if isinstance(error, Timeout):
        return "Provider request timed out. Check connectivity and try again."
    if isinstance(error, FileNotFoundError):
        return "File or inbox path does not exist. Check the configured paths."
    if isinstance(error, PermissionError):
        return "File access denied. Check file and directory permissions."
    return f"{type(error).__name__}: operation failed. See the diagnostic log for details."
