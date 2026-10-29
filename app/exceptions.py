"""Errors the run can end with. Each carries the process exit code (see run.py)."""
from __future__ import annotations


class PipelineError(RuntimeError):
    exit_code = 1


class ConfigurationError(PipelineError):
    """A required setting is missing or invalid."""

    exit_code = 2


class DatabaseError(PipelineError):
    """MySQL is unreachable, the table is missing, or a write was rolled back."""

    exit_code = 3


class PeriodError(PipelineError, ValueError):
    """The requested campaign start window is not valid."""

    exit_code = 5


class SpApiError(PipelineError):
    """Non-retryable SP-API failure, or retries exhausted."""

    exit_code = 6


class MalformedReport(PipelineError, ValueError):
    """The report as a whole cannot be trusted (wrong type, marketplace, missing coupon IDs...)."""

    exit_code = 7


class Locked(PipelineError):
    """Another run holds the lock."""

    exit_code = 11
