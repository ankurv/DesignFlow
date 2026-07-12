"""Provider error normalization for safe, consistent API and UI responses."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PublicProviderError:
    code: str
    message: str
    status_code: int | None = None
    retryable: bool = False

    def to_dict(self) -> dict:
        return {
            "error": self.message,
            "error_code": self.code,
            "status_code": self.status_code,
            "retryable": self.retryable,
        }


def classify_provider_error(exc: Exception) -> PublicProviderError:
    """Prefer structured SDK fields, then use bounded text matching."""
    status = getattr(exc, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    provider_code = str(getattr(exc, "code", "") or "").lower()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nested_error = body.get("error")
        nested_code = nested_error.get("code") if isinstance(nested_error, dict) else ""
        provider_code = str(body.get("code") or nested_code or provider_code).lower()
    text = str(exc).lower()[:4000]
    signal = f"{provider_code} {text}"

    if any(token in signal for token in ("insufficient_quota", "quota exhausted", "credit balance", "billing", "usage limit", "resource exhausted")):
        return PublicProviderError("quota_exhausted", "Model quota or provider credits are exhausted.", status, False)
    if status == 429 or re.search(r"\b429\b|rate.?limit|too many requests", signal):
        return PublicProviderError("rate_limited", "Provider rate limit reached. Try again shortly.", status or 429, True)
    if status in {401, 403} or any(token in signal for token in ("unauthorized", "forbidden", "authentication", "invalid_api_key", "api key", "credential")):
        return PublicProviderError("authentication_failed", "Provider authentication or model access failed.", status, False)
    if any(token in signal for token in ("context_length", "context length", "context window", "maximum context", "too many tokens", "token limit")):
        return PublicProviderError("context_exhausted", "Request exceeds the model context limit.", status, False)
    if status == 404 or re.search(r"model.*(not found|unavailable|unsupported|deprecated)|unknown model|invalid model", signal):
        return PublicProviderError("model_unavailable", "Configured model is unavailable.", status, False)
    if any(token in signal for token in ("timeout", "timed out", "deadline exceeded")):
        return PublicProviderError("provider_timeout", "Model provider timed out.", status, True)
    if status is not None and status >= 500:
        return PublicProviderError("provider_unavailable", "Model provider is temporarily unavailable.", status, True)
    if any(token in signal for token in ("connection", "connect failed", "name resolution", "dns")):
        return PublicProviderError("provider_unavailable", "Could not connect to the model provider.", status, True)
    if any(token in signal for token in ("content filter", "safety", "policy violation")):
        return PublicProviderError("safety_blocked", "Request was blocked by the provider safety policy.", status, False)
    return PublicProviderError("agent_failed", "Agent request failed.", status, False)
