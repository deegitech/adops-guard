"""Exceptions and process exit codes.

Every exception renders its message through :func:`adops_guard.redact.redact`,
so an error can never print a token, even if an API echoed one back.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from adops_guard.redact import redact


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1  # API, network or unexpected error
    USAGE = 2  # bad arguments, configuration or credentials setup
    REFUSED = 3  # a guard refused the change, or a check failed
    MISMATCH = 4  # the value read back after a write is not what was sent
    RATE_LIMITED = 5  # platform rate limit or our own usage guard; retry later
    INTERRUPTED = 130


class AdopsError(Exception):
    """Base class for expected, user-facing errors."""

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return redact(self.message)


class ConfigError(AdopsError):
    exit_code = ExitCode.USAGE


class CredentialError(AdopsError):
    exit_code = ExitCode.USAGE


class GuardRefused(AdopsError):
    exit_code = ExitCode.REFUSED


class LockBusy(AdopsError):
    exit_code = ExitCode.REFUSED


class ReadBackMismatch(AdopsError):
    exit_code = ExitCode.MISMATCH


class NetworkError(AdopsError):
    pass


class ApiError(AdopsError):
    """An API answered with an error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: Any = None,
        subcode: Any = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.code = code
        self.subcode = subcode
        self.transient = False  # set by clients when a retry may succeed


class RateLimited(ApiError):
    exit_code = ExitCode.RATE_LIMITED


class AmbiguousWrite(ApiError):
    """A write failed in a way that does not tell whether it was applied."""
