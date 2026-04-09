"""Error classification utilities for user-facing messages and structured logging."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClassifiedError:
    """A classified error with separate user-facing and logging messages."""

    category: str  # api_error, auth_error, network_error, rate_limit, internal
    user_message: str
    log_message: str
    retryable: bool


def classify_error(error: Exception) -> ClassifiedError:
    """Classify an exception into a structured error with user-friendly messaging.

    Inspects the exception type and message to determine the error category,
    produce a friendly user-facing message, and a detailed log message.
    """
    error_type = type(error).__name__
    error_str = str(error)
    error_str_lower = error_str.lower()

    # Google API HTTP errors (googleapiclient.errors.HttpError)
    if error_type == "HttpError" and hasattr(error, "resp"):
        status = int(getattr(error.resp, "status", 0))  # type: ignore[union-attr]
        reason = _get_http_error_reason(error)
        log_msg = f"Google API HttpError (HTTP {status}): {reason}"

        if status == 401 or status == 403:
            return ClassifiedError(
                category="auth_error",
                user_message=(
                    "There's an authentication issue with a Google API integration. "
                    "The credentials may need to be refreshed."
                ),
                log_message=log_msg,
                retryable=False,
            )
        if status == 429:
            return ClassifiedError(
                category="rate_limit",
                user_message="A Google API is rate-limiting requests. Try again in a minute.",
                log_message=log_msg,
                retryable=True,
            )
        if status in (500, 502, 503):
            return ClassifiedError(
                category="api_error",
                user_message=(
                    "A Google API returned a temporary server error. "
                    "This usually resolves on its own — try again in a moment."
                ),
                log_message=log_msg,
                retryable=True,
            )
        return ClassifiedError(
            category="api_error",
            user_message=f"A Google API request failed (HTTP {status}).",
            log_message=log_msg,
            retryable=False,
        )

    # Generic HTTP/API status codes in the error message
    if _matches_any(error_type, ("HTTPError", "HTTPStatusError")):
        status = _extract_status_code(error)
        log_msg = f"{error_type}: {_truncate(error_str, 300)}"
        if status and status in (500, 502, 503):
            return ClassifiedError(
                category="api_error",
                user_message=(
                    "An external API returned a temporary server error. "
                    "This usually resolves on its own — try again in a moment."
                ),
                log_message=log_msg,
                retryable=True,
            )
        if status and status == 429:
            return ClassifiedError(
                category="rate_limit",
                user_message="An external service is rate-limiting requests. Try again in a minute.",
                log_message=log_msg,
                retryable=True,
            )

    # Network / connectivity errors
    if _matches_any(error_type, ("ConnectError", "ConnectionError", "OSError", "socket.error")):
        return ClassifiedError(
            category="network_error",
            user_message="Couldn't reach an external service. It may be temporarily down.",
            log_message=f"Network error ({error_type}): {_truncate(error_str, 300)}",
            retryable=True,
        )

    # Timeout errors
    if "timeout" in error_type.lower() or "timeout" in error_str_lower:
        return ClassifiedError(
            category="network_error",
            user_message="A request to an external service timed out. Try again in a moment.",
            log_message=f"Timeout ({error_type}): {_truncate(error_str, 300)}",
            retryable=True,
        )

    # Auth / credential errors
    if _matches_any(error_type, ("RefreshError", "InvalidGrantError", "AuthError")):
        return ClassifiedError(
            category="auth_error",
            user_message="There's an authentication issue with one of my integrations.",
            log_message=f"Auth error ({error_type}): {_truncate(error_str, 300)}",
            retryable=False,
        )

    # Check for status-code-like patterns in the string (e.g. "500 Internal Server Error")
    if "500" in error_str and ("internal server error" in error_str_lower or "server error" in error_str_lower):
        return ClassifiedError(
            category="api_error",
            user_message=(
                "An external API returned a temporary server error. "
                "This usually resolves on its own — try again in a moment."
            ),
            log_message=f"API error ({error_type}): {_truncate(error_str, 300)}",
            retryable=True,
        )

    # Fallback: internal / unknown error
    return ClassifiedError(
        category="internal",
        user_message="Something unexpected went wrong. The error has been logged.",
        log_message=f"Unclassified error ({error_type}): {_truncate(error_str, 300)}",
        retryable=False,
    )


def _get_http_error_reason(error: Exception) -> str:
    """Extract a reason string from a googleapiclient HttpError."""
    if hasattr(error, "_get_reason"):
        try:
            return error._get_reason()  # type: ignore[union-attr]
        except Exception:
            pass
    return str(error)[:200]


def _extract_status_code(error: Exception) -> int | None:
    """Try to extract an HTTP status code from an exception."""
    # httpx.HTTPStatusError
    if hasattr(error, "response") and hasattr(error.response, "status_code"):  # type: ignore[union-attr]
        return int(error.response.status_code)  # type: ignore[union-attr]
    return None


def _matches_any(name: str, candidates: tuple[str, ...]) -> bool:
    return name in candidates


def _truncate(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[:max_len] + "..."
