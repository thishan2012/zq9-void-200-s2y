"""The three safety checks that run under EVERY search method.

1. same_song_title() - blocks a different song that only shares one word.
2. version_ok()      - blocks slowed <-> original <-> sped up <-> remix swaps.
3. artist_ok()       - blocks the same title uploaded by a different artist.

A method may relax *thresholds*, but it can never disable the version check,
and it can never accept a candidate whose artist actively conflicts.
"""
from __future__ import annotations

import re

from .models import Candidate, Track
from .normalize import (
    SEP_RE,
    base_title,
    channel_artist,
    extract_version,
    fold,
    similarity,
    tokens,
)

# ---------------------------------------------------------------- title check


def _title_core(text: str) -> str:
    return base_title(text)


def _fuzzy_token_overlap(ta: set[str], tb: set[str]) -> int:
    """Count tokens of `ta` that have an exact or near-exact twin in `tb`."""
    count = 0
    for a in ta:
        if any(a == b or similarity(a, b) >= 0.85 for b in tb):
            count += 1
    return count


def _compare_title(a: str, b: str, floor: float) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = set(tokens(a)), set(tokens(b))
    # One shared word out of several is a different song, not a match.
    # (Near-exact twins count as the same word, so typos still pass.)
    if len(ta) > 1 and len(tb) > 1 and _fuzzy_token_overlap(ta, tb) <= 1:
        return False
    # Short titles must be contained, not merely similar.
    if min(len(ta), len(tb)) == 1 and ta != tb:
        if not (ta <= tb or tb <= ta):
            return False
    return similarity(a, b) >= floor


def same_song_title(track_name: str, candidate_title: str, floor: float = 0.85) -> bool:
    """True when both titles refer to the same song.

    Uploads often use `Artist - Title` or `Title | Artist`; the title segment
    is compared as well as the full string. Exact normalized matches always
    pass, otherwise fuzzy similarity must reach `floor`.
    """
    a = _title_core(track_name)
    variants = [_title_core(candidate_title)]
    parts = SEP_RE.split(candidate_title or "")
    if len(parts) >= 2:
        variants.extend(_title_core(p) for p in parts)
    return any(_compare_title(a, b, floor) for b in variants if b)


def title_in_title(track_name: str, candidate_title: str) -> bool:
    """Every word of the track title appears in the candidate title."""
    ta, tb = set(tokens(_title_core(track_name))), set(tokens(_title_core(candidate_title)))
    return bool(ta) and ta <= tb


# -------------------------------------------------------------- version check


def version_ok(track: Track, candidate_title: str) -> bool:
    """Version signature of source and candidate must be identical.

    'X (Slowed)' may only match a slowed video; the original may never match a
    slowed / sped-up / nightcore / remix upload, and vice versa.
    """
    want = extract_version(track.name) | extract_version(track.album)
    got = extract_version(candidate_title)
    return want == got


# --------------------------------------------------------------- artist check

_ARTIST_LINE_RE = re.compile(
    r"^\s*(?:artist|artists|by|performed by)\s*[:\-]\s*(.+)$", re.I | re.M
)


def _artist_evidence(candidate: Candidate, track: Track | None = None) -> tuple[list[str], list[str]]:
    """(strong, weak) places where a candidate video exposes its artist.

    Strong: 'Artist - Title' segments, 'Artist:' description lines, Topic
    channels. Weak: a plain uploader/channel name (often a promo channel, so
    it can confirm a match but never create a conflict on its own).
    """
    strong: list[str] = []
    weak: list[str] = []
    ch = candidate.channel or ""
    if ch:
        if fold(ch).endswith("- topic"):
            strong.append(channel_artist(ch))
        else:
            weak.append(ch)
    parts = SEP_RE.split(candidate.title)
    if len(parts) >= 2:
        for part in (parts[0].strip(), parts[-1].strip()):
            if not part:
                continue
            # Ignore marketing/version-only segments and the song title itself.
            if not base_title(part):
                continue
            if track is not None and same_song_title(track.name, part, floor=0.90):
                continue
            strong.append(part)
    for m in _ARTIST_LINE_RE.finditer(candidate.description or ""):
        strong.append(m.group(1).strip())
    return [e for e in strong if e], [e for e in weak if e]


def _artist_matches(want: str, evidence: str) -> bool:
    w, e = fold(want), fold(evidence)
    if not w or not e:
        return False
    if w == e:
        return True
    if len(w) >= 3 and (w in e or e in w):
        return True
    tw, te = set(tokens(w)), set(tokens(e))
    if tw and te and (tw <= te or te <= tw):
        return True
    return similarity(w, e) >= 0.88


def artist_ok(track: Track, candidate: Candidate, strict: bool = True) -> bool:
    """True when the candidate's artist does not conflict with the track.

    strict=True  -> artist must be positively confirmed (strong or weak).
    strict=False -> no *strong* conflicting artist evidence allowed; a plain
                    promo channel name alone never blocks a match.
    """
    strong, weak = _artist_evidence(candidate, track)
    wants = list(track.artists)
    for ev in strong + weak:
        for want in wants:
            if _artist_matches(want, ev):
                return True
    if not strict and not strong:
        return True
    return False
