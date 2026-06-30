"""SpindleBot domain exceptions."""
from __future__ import annotations


class SpindleBotError(Exception):
    """Base class for SpindleBot domain errors."""


class MarkerMismatch(SpindleBotError):
    """A location's expected root carries a different location's marker file."""


class UnknownLocation(SpindleBotError):
    """A mounted volume could not be resolved to a known location."""


class IntegrityMismatch(SpindleBotError):
    """A copied file's hash did not match the source — the copy is not trusted."""


class MinCopiesViolation(SpindleBotError):
    """An action would drop a content below the configured min_copies floor."""
