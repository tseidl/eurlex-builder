"""Package-specific exceptions."""

from __future__ import annotations


class EurlexBuilderError(RuntimeError):
    """Base exception for recoverable pipeline failures."""


class TransientSourceError(EurlexBuilderError):
    """A data source request failed transiently and should be retried."""
