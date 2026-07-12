"""Package-specific exceptions."""

from __future__ import annotations


class EurlexBuilderError(RuntimeError):
    """Base exception for recoverable pipeline failures."""


class TransientSourceError(EurlexBuilderError):
    """A data source request failed transiently and should be retried."""


class DoclingStartupError(EurlexBuilderError):
    """The isolated Docling worker could not initialize."""

    def __init__(self, message: str, *, fatal: bool) -> None:
        super().__init__(message)
        self.fatal = fatal
