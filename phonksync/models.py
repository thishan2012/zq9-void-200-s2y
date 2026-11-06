"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Track:
    """A track as read from a Spotify source playlist."""

    id: str
    name: str
    artists: list[str]
    album: str = ""
    duration_ms: int = 0
    isrc: str = ""
    uri: str = ""
    popularity: int = 0
    sources: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> int:
        return round(self.duration_ms / 1000)

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def display(self) -> str:
        return f"{', '.join(self.artists)} — {self.name}"


@dataclass
class Candidate:
    """A YouTube video with the metadata the matcher needs."""

    video_id: str
    title: str
    channel: str
    description: str = ""
    duration_s: int = 0
    views: int = 0

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class MatchResult:
    candidate: Candidate
    method: str
    tried: list[str]
    free: bool = False  # matched without spending a metered API search
