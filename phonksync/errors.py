"""Shared exception types."""
from __future__ import annotations


class PhonkSyncError(RuntimeError):
    """Base error."""


class ConfigError(PhonkSyncError):
    """Invalid or missing configuration."""


class AuthError(PhonkSyncError):
    """Credential problem (expired cookie, invalid OAuth token, ...)."""


class QuotaExceeded(PhonkSyncError):
    """YouTube API daily quota or per-run search budget is exhausted."""


class NotFound(PhonkSyncError):
    """Playlist / video / track not found."""


class RateLimited(PhonkSyncError):
    """Upstream API asked us to slow down."""

    def __init__(self, retry_after: float = 1.0, message: str = "Rate limited"):
        super().__init__(message)
        self.retry_after = retry_after


class NotApplicable(PhonkSyncError):
    """A search method's precondition does not hold for this track.

    Raised by methods that should be recorded as 'never tried' rather than
    'tried but did not win'.
    """


class ShortVideoError(PhonkSyncError):
    """A candidate is a YouTube Short, not an allowed playlist video."""


class ShortsCheckUnavailable(PhonkSyncError):
    """Could not safely determine whether a video is a YouTube Short."""
