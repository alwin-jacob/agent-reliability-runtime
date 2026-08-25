"""Typed runtime errors and sanitized failure normalization."""

from __future__ import annotations

import re
from typing import Any

from agent_runtime.domain import FailureOrigin, FailureRecord, RuntimePhase


def sanitize_message(value: object) -> str:
    """Remove private path shapes and bound messages persisted in artifacts."""

    message = str(value) or type(value).__name__
    message = re.sub(r"/(?:Users|home)/[^/\s]+", "/<private>", message)
    message = re.sub(r"[A-Za-z]:\\Users\\[^\\\s]+", r"C:\\<private>", message)
    return message[:500]


class ClassifiedError(Exception):
    """Expected failure with stable retry and taxonomy metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        origin: FailureOrigin,
        retryable: bool,
    ) -> None:
        super().__init__(sanitize_message(message))
        self.code = code
        self.origin = origin
        self.retryable = retryable


class ConfigurationError(ClassifiedError):
    def __init__(self, message: str, *, code: str = "invalid_configuration") -> None:
        super().__init__(
            message,
            code=code,
            origin=FailureOrigin.CONFIGURATION,
            retryable=False,
        )


class ProviderError(ClassifiedError):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(
            message,
            code=code,
            origin=FailureOrigin.MODEL_PROVIDER,
            retryable=retryable,
        )


class ModelOutputError(ClassifiedError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(
            message,
            code=code,
            origin=FailureOrigin.MODEL_OUTPUT,
            retryable=False,
        )


class ToolPolicyError(ClassifiedError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code, origin=FailureOrigin.TOOL_POLICY, retryable=False)


class ToolInputError(ClassifiedError):
    def __init__(self, message: str, *, code: str = "tool_input_invalid") -> None:
        super().__init__(message, code=code, origin=FailureOrigin.TOOL_INPUT, retryable=False)


class ToolExecutionError(ClassifiedError):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(
            message,
            code=code,
            origin=FailureOrigin.TOOL_EXECUTION,
            retryable=retryable,
        )


class ToolOutputError(ClassifiedError):
    def __init__(self, message: str, *, code: str = "tool_output_invalid") -> None:
        super().__init__(message, code=code, origin=FailureOrigin.TOOL_OUTPUT, retryable=False)


class OrchestrationError(ClassifiedError):
    def __init__(self, message: str, *, code: str = "orchestration_error") -> None:
        super().__init__(
            message,
            code=code,
            origin=FailureOrigin.ORCHESTRATION,
            retryable=False,
        )


class ArtifactError(ClassifiedError):
    def __init__(self, message: str, *, code: str = "artifact_invalid") -> None:
        super().__init__(message, code=code, origin=FailureOrigin.ARTIFACT, retryable=False)


class TransientInfrastructureError(ClassifiedError):
    def __init__(self, message: str, *, code: str = "transient_infrastructure") -> None:
        super().__init__(
            message,
            code=code,
            origin=FailureOrigin.UNEXPECTED_INTERNAL,
            retryable=True,
        )


class InvocationFailed(Exception):
    """Internal signal that an invocation exhausted or rejected its attempts."""

    def __init__(self, failure: FailureRecord) -> None:
        super().__init__(failure.message)
        self.failure = failure


def failure_from_error(
    error: ClassifiedError,
    *,
    phase: RuntimePhase,
    source_component: str,
    timestamp: str,
    attempt: int | None = None,
    model_attempt_id: str | None = None,
    tool_call_id: str | None = None,
    span_id: str | None = None,
) -> FailureRecord:
    return FailureRecord(
        code=error.code,
        origin=error.origin,
        phase=phase,
        source_component=source_component,
        retryable=error.retryable,
        attempt=attempt,
        message=sanitize_message(error),
        exception_type=type(error).__name__,
        timestamp=timestamp,
        model_attempt_id=model_attempt_id,
        tool_call_id=tool_call_id,
        span_id=span_id,
    )


def unexpected_failure(
    error: Exception,
    *,
    phase: RuntimePhase,
    source_component: str,
    timestamp: str,
    span_id: str | None = None,
) -> FailureRecord:
    return FailureRecord(
        code="unexpected_internal_error",
        origin=FailureOrigin.UNEXPECTED_INTERNAL,
        phase=phase,
        source_component=source_component,
        retryable=False,
        message=sanitize_message(error),
        exception_type=type(error).__name__,
        timestamp=timestamp,
        span_id=span_id,
    )


def sanitized_payload(value: Any) -> Any:
    """Recursively sanitize event payloads without reading ambient environment data."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("secret", "password", "token", "api_key")):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = sanitized_payload(item)
        return result
    if isinstance(value, list):
        return [sanitized_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitized_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_message(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_message(value)
