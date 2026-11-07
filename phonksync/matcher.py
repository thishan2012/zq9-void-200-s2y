"""The accuracy-first matching engine.

Pipeline order (first verified win stops the search):

    MANUAL -> STRICT -> LOOSE -> DETAIL -> 29 numbered fallback steps.

Every candidate produced by any step is passed through the three safety
checks in ``checks.py`` (title / version / artist). The only exceptions are
the authoritative-identity steps (MANUAL, ISRC, LINKED), which trust an exact
user mapping / ISRC / Spotify link instead of fuzzy text.

Quota discipline:
  * All steps share one candidate pool per track.
  * A query is issued at most once per track and is cached across runs.
  * Steps whose condition does not hold are skipped ("never tried").
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from . import checks
from .cache import SearchCache
from .errors import NotApplicable, QuotaExceeded
from .models import Candidate, MatchResult, Track
from .normalize import (
    SEP_RE,
    base_title,
    channel_artist,
    extract_version,
    fold,
    has_non_ascii,
    part_number,
    similarity,
    split_feat,
    strip_version,
    tokens,
)
from .overrides import Overrides
from .youtube import YouTubeClient

log = logging.getLogger(__name__)

_SPOTIFY_LINK_RE = re.compile(
    r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]{22})"
)
_ISRC_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{3}\d{7})\b")
_SONG_TAG_RE = re.compile(
    r"^\s*(?:song|title|track)\s*[:\-]\s*(.+)$", re.I | re.M
)
_ARTIST_TAG_RE = re.compile(
    r"^\s*(?:artist|artists)\s*[:\-]\s*(.+)$", re.I | re.M
)
_MUSIC_LINE_RE = re.compile(r"^\s*[♪♫🎵]\s*(.+)$", re.M)
_HASHTAG_RE = re.compile(r"#(\w+)")
_DESC_LABEL_RE = re.compile(
    r"^\s*(?:provided to youtube|released on|auto-generated|isrc|upc|label|"
    r"copyright|℗|©|produced|written|composed|mixed|mastered|spotify|apple|"
    r"http|www\.|follow|subscribe|like|share|stream|download|lyrics?)\b",
    re.I,
)


@dataclass
class MatchContext:
    track: Track
    pool: dict[str, Candidate] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    first_results: list[str] = field(default_factory=list)
    searched: set[str] = field(default_factory=set)
    searches_used: int = 0
    free_queries: list[str] = field(default_factory=list)

    def add(self, candidates: list[Candidate]) -> None:
        for cand in candidates:
            if cand.video_id not in self.pool:
                self.pool[cand.video_id] = cand
                self.order.append(cand.video_id)

    def candidates(self) -> list[Candidate]:
        return [self.pool[vid] for vid in self.order]


class Matcher:
    def __init__(
        self,
        youtube: YouTubeClient,
        cache: SearchCache,
        overrides: Overrides,
        results_per_query: int = 10,
        duration_tolerance: int = 7,
        exact_duration_tolerance: int = 3,
    ):
        self.yt = youtube
        self.cache = cache
        self.overrides = overrides
        self.results_per_query = results_per_query
        self.duration_tolerance = duration_tolerance
        self.exact_duration_tolerance = exact_duration_tolerance
        self.last_tried: list[str] = []
        self.last_won: str = ""
        # The candidates a failed track actually produced (safe to show later).
        self.last_pool: list[Candidate] = []

    # ------------------------------------------------------------- searching

    def _search(self, ctx: MatchContext, query: str) -> list[Candidate]:
        query = " ".join(query.split())
        if not query or query in ctx.searched:
            return []
        ctx.searched.add(query)
        key = self.cache.query_key(query, self.results_per_query, self.yt.region_code)
        ids = self.cache.get_query(key)
        if ids is None:
            # Metered API search — only after the free pass missed.
            ids = self.yt.search(query, max_results=self.results_per_query)
            self.cache.put_query(key, ids)
            ctx.searches_used += 1
            found, missing = self.cache.get_videos(ids)
            if missing:
                fetched = self.yt.videos(missing)
                self.cache.put_videos(fetched)
                found = found + fetched
        else:
            found, missing = self.cache.get_videos(ids)
            if missing:
                fetched = self.yt.videos(missing)
                self.cache.put_videos(fetched)
                found = found + fetched
        # Preserve search ranking order.
        by_id = {c.video_id: c for c in found}
        ordered = [by_id[vid] for vid in ids if vid in by_id]
        ctx.add(ordered)
        if not ctx.first_results:
            ctx.first_results = list(ids)
        return ordered

    # ------------------------------------------------------------ verifying

    def _verify(
        self,
        track: Track,
        cand: Candidate,
        title_floor: float = 0.85,
        artist_strict: bool = False,
        require_title: bool = True,
    ) -> bool:
        if require_title and not checks.same_song_title(track.name, cand.title, title_floor):
            return False
        if not checks.version_ok(track, cand.title):
            return False
        if not checks.artist_ok(track, cand, strict=artist_strict):
            return False
        return True

    def _duration_close(self, track: Track, cand: Candidate, tol: int) -> bool:
        if not track.duration_s or not cand.duration_s:
            return False
        return abs(track.duration_s - cand.duration_s) <= tol

    def _pick_verified(
        self,
        ctx: MatchContext,
        title_floor: float = 0.85,
        artist_strict: bool = False,
        candidates: list[Candidate] | None = None,
    ) -> Candidate | None:
        for cand in candidates if candidates is not None else ctx.candidates():
            if self._verify(ctx.track, cand, title_floor, artist_strict):
                return cand
        return None

    # =====================================================================
    # Individual methods. Each returns a Candidate or None.
    # =====================================================================

    def m_manual(self, ctx: MatchContext) -> Candidate | None:
        vid = self.overrides.lookup(ctx.track)
        if not vid:
            raise NotApplicable("no manual override for this track")
        found, missing = self.cache.get_videos([vid])
        if missing:
            fetched = self.yt.videos(missing)
            self.cache.put_videos(fetched)
            found = found + fetched
        if not found:
            return None
        cand = found[0]
        ctx.add([cand])
        # User-pinned mapping is authoritative; only confirm the video exists.
        return cand

    def m_strict(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, f"{t.primary_artist} {t.name}")
        return self._pick_verified(ctx, title_floor=1.0, artist_strict=False)

    def m_loose(self, ctx: MatchContext) -> Candidate | None:
        return self._pick_verified(ctx, title_floor=0.85, artist_strict=False)

    def m_detail(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        # Build metadata from description Artist:/Title: lines of pool videos.
        for cand in ctx.candidates():
            desc = cand.description or ""
            title_m = _SONG_TAG_RE.search(desc)
            artist_m = _ARTIST_TAG_RE.search(desc)
            if title_m and artist_m:
                d_title = title_m.group(1).strip()
                d_artist = artist_m.group(1).strip()
                if checks.same_song_title(t.name, d_title, 0.9) and any(
                    checks._artist_matches(a, d_artist) for a in t.artists
                ):
                    if self._verify(t, cand, 0.85, False):
                        return cand
        # Otherwise search using the richest description metadata found.
        for cand in ctx.candidates()[:3]:
            desc = cand.description or ""
            title_m = _SONG_TAG_RE.search(desc)
            artist_m = _ARTIST_TAG_RE.search(desc)
            if title_m and artist_m:
                self._search(ctx, f"{artist_m.group(1).strip()} {title_m.group(1).strip()}")
                hit = self._pick_verified(ctx, 0.9, False)
                if hit:
                    return hit
        return None

    # ---- numbered fallback steps -----------------------------------------

    def m_reversed(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, f"{t.name} {t.primary_artist}")
        for cand in ctx.candidates():
            parts = SEP_RE.split(cand.title)
            if len(parts) >= 2:
                # "Song - Artist" shape: last part is the artist.
                if checks.same_song_title(t.name, parts[0], 0.9) and any(
                    checks._artist_matches(a, parts[-1]) for a in t.artists
                ):
                    if self._verify(t, cand, 0.85, False):
                        return cand
        return self._pick_verified(ctx, 0.9, False)

    def m_single(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if len(t.artists) < 2:
            raise NotApplicable("not a collaboration")
        self._search(ctx, f"{t.artists[0]} {t.name}")
        return self._pick_verified(ctx, 0.9, False)

    def m_channel(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, t.name)
        for cand in ctx.candidates():
            ch = channel_artist(cand.channel)
            if ch and any(checks._artist_matches(a, ch) for a in t.artists):
                if self._verify(t, cand, 0.85, False):
                    return cand
        return None

    def m_topic(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, f"{t.primary_artist} {t.name} topic")
        for cand in ctx.candidates():
            if fold(cand.channel).endswith("- topic"):
                real = channel_artist(cand.channel)
                if any(checks._artist_matches(a, real) for a in t.artists):
                    if self._verify(t, cand, 0.85, False):
                        return cand
        return None

    def m_suffix(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, f"{t.primary_artist} {base_title(t.name)}")
        return self._pick_verified(ctx, 0.9, False)

    def m_country(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if not (has_non_ascii(t.name) or any(has_non_ascii(a) for a in t.artists)):
            raise NotApplicable("no accented/non-ASCII characters")
        self._search(ctx, f"{fold(t.primary_artist)} {fold(t.name)}")
        return self._pick_verified(ctx, 0.88, False)

    def m_feat(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        _, featured = split_feat(t.name)
        if not featured:
            raise NotApplicable("no feat. clause")
        main, _ = split_feat(t.name)
        for feat in featured:
            self._search(ctx, f"{feat} {main}")
            hit = self._pick_verified(ctx, 0.88, False)
            if hit:
                return hit
        return None

    def m_duration(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if not t.duration_s:
            raise NotApplicable("no source duration")
        eligible = [
            c for c in ctx.candidates()
            if self._verify(t, c, 0.85, False) and c.duration_s
        ]
        if len(eligible) < 2:
            raise NotApplicable("fewer than 2 same-name versions in pool")
        eligible.sort(key=lambda c: abs(c.duration_s - t.duration_s))
        best = eligible[0]
        if self._duration_close(t, best, self.duration_tolerance):
            return best
        return None

    def m_artists(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if len(t.artists) < 2:
            raise NotApplicable("only one credited artist")
        for artist in t.artists:
            self._search(ctx, f"{artist} {t.name}")
            hit = self._pick_verified(ctx, 0.9, False)
            if hit:
                return hit
        return None

    def m_short(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        core = base_title(t.name)
        if len(core) > 4 and len(tokens(core)) > 1:
            raise NotApplicable("title is not short")
        self._search(ctx, f"{t.primary_artist} {t.name}")
        return self._pick_verified(ctx, 1.0, artist_strict=True)

    def m_artistless(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, base_title(t.name))
        return self._pick_verified(ctx, 0.92, artist_strict=False)

    def m_exact(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, f"{t.primary_artist} {strip_version(t.name)}")
        want = tokens(base_title(t.name))
        for cand in ctx.candidates():
            if tokens(base_title(cand.title)) == want:
                if self._verify(t, cand, 0.9, False):
                    return cand
        return None

    def m_wordset(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        want = sorted(tokens(base_title(t.name)))
        if not want:
            raise NotApplicable("empty title")
        for cand in ctx.candidates():
            if sorted(tokens(base_title(cand.title))) == want:
                if self._verify(t, cand, 0.85, False):
                    return cand
        return None

    def m_slowed(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if "slowed" not in extract_version(t.name):
            raise NotApplicable("source is not a slowed version")
        self._search(ctx, f"{base_title(t.name)} slowed")
        for cand in ctx.candidates():
            if "slowed" in extract_version(cand.title):
                if self._verify(t, cand, 0.85, False):
                    return cand
        return None

    def m_uploader(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        cands = ctx.candidates()
        if not cands:
            raise NotApplicable("empty candidate pool")
        channel = cands[0].channel
        if not channel:
            raise NotApplicable("no uploader name")
        self._search(ctx, f"{channel} {t.name}")
        return self._pick_verified(ctx, 0.88, False)

    def m_linked(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        linked = [
            c for c in ctx.candidates() if _SPOTIFY_LINK_RE.search(c.description or "")
        ]
        if not linked:
            raise NotApplicable("no Spotify track links in any description")
        for cand in linked:
            for m in _SPOTIFY_LINK_RE.finditer(cand.description or ""):
                if m.group(1) == t.id:
                    # Exact Spotify link in the description is authoritative.
                    if checks.version_ok(t, cand.title):
                        return cand
        return None

    def m_field(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, f'track:"{base_title(t.name)}" artist:"{t.primary_artist}"')
        return self._pick_verified(ctx, 0.9, artist_strict=False)

    def m_variants(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        for query in (
            f"{t.primary_artist} {t.name}",
            f"{t.name} {t.primary_artist}",
            f"{t.primary_artist} - {t.name}",
        ):
            self._search(ctx, query)
        return self._pick_verified(ctx, 0.88, False)

    def m_durver(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if not t.duration_s:
            raise NotApplicable("no source duration")
        best = None
        best_diff = 1e9
        for cand in ctx.candidates():
            if not cand.duration_s:
                continue
            diff = abs(cand.duration_s - t.duration_s)
            if diff <= self.exact_duration_tolerance and similarity(
                base_title(t.name), base_title(cand.title)
            ) >= 0.7:
                if self._verify(t, cand, 0.7, False) and diff < best_diff:
                    best, best_diff = cand, diff
        return best

    def m_cleanartist(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, f"{t.primary_artist} {base_title(t.name)}")
        return self._pick_verified(ctx, 0.9, False)

    def m_titleonly(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if not t.duration_s:
            raise NotApplicable("no source duration to verify against")
        self._search(ctx, base_title(t.name))
        for cand in ctx.candidates():
            if checks.title_in_title(t.name, cand.title) and self._duration_close(
                t, cand, self.duration_tolerance
            ):
                if self._verify(t, cand, 0.85, False):
                    return cand
        return None

    def m_songtag(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        want_tags = {fold(base_title(t.name)).replace(" ", "")}
        for cand in ctx.candidates():
            desc = cand.description or ""
            tagged_title = _SONG_TAG_RE.search(desc) or _MUSIC_LINE_RE.search(desc)
            tagged_artist = _ARTIST_TAG_RE.search(desc)
            hashtags = {fold(h) for h in _HASHTAG_RE.findall(desc)}
            title_ok = False
            if tagged_title and checks.same_song_title(t.name, tagged_title.group(1), 0.9):
                title_ok = True
            if want_tags & hashtags:
                title_ok = True
            artist_ok = True
            if tagged_artist:
                artist_ok = any(
                    checks._artist_matches(a, tagged_artist.group(1)) for a in t.artists
                )
            if title_ok and artist_ok and self._verify(t, cand, 0.8, False):
                return cand
        return None

    def m_isrc(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if not t.isrc:
            raise NotApplicable("no source ISRC")
        with_isrc = [c for c in ctx.candidates() if _ISRC_RE.search(c.description or "")]
        if not with_isrc:
            raise NotApplicable("no ISRC codes in any description")
        for cand in with_isrc:
            found = _ISRC_RE.findall(cand.description or "")
            if t.isrc.upper() in {f.upper() for f in found}:
                # Exact recording identity; name/artist checks are bypassed.
                return cand
        return None

    def m_desctext(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        for cand in ctx.candidates():
            for line in (cand.description or "").splitlines():
                line = line.strip()
                if not line or _DESC_LABEL_RE.match(line) or len(line) > 120:
                    continue
                if similarity(base_title(t.name), base_title(line)) >= 0.92:
                    if self._verify(t, cand, 0.85, False):
                        return cand
        return None

    def m_broken(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        self._search(ctx, f"{t.name} {t.primary_artist}")
        for cand in ctx.candidates():
            parts = SEP_RE.split(cand.title)
            if len(parts) >= 2:
                song, artist = parts[0].strip(), parts[-1].strip()
                if checks.same_song_title(t.name, song, 0.9) and any(
                    checks._artist_matches(a, artist) for a in t.artists
                ):
                    if self._verify(t, cand, 0.85, False):
                        return cand
        return None

    def m_album(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        if not t.album:
            raise NotApplicable("no album name")
        self._search(ctx, f"{t.primary_artist} {t.name} {t.album}")
        return self._pick_verified(ctx, 0.88, False)

    def m_part(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        part = part_number(t.name)
        if not part:
            raise NotApplicable("no Pt./Part number in title")
        for variant in (f"Pt. {part}", f"Part {part}", f"Pt.{part}"):
            query = re.sub(
                r"\b(?:pt\.?|part|vol\.?|volume)\s*\d+\b", variant, t.name, flags=re.I
            )
            self._search(ctx, f"{t.primary_artist} {query}")
        for cand in ctx.candidates():
            if part_number(cand.title) == part and self._verify(t, cand, 0.85, False):
                return cand
        return None

    def m_popular(self, ctx: MatchContext) -> Candidate | None:
        t = ctx.track
        eligible = [c for c in ctx.candidates() if self._verify(t, c, 0.85, False)]
        if len(eligible) < 2:
            raise NotApplicable("fewer than 2 verified same-title candidates")
        eligible.sort(key=lambda c: c.views, reverse=True)
        if eligible[0].views == eligible[1].views:
            return None  # tie -> do not guess
        return eligible[0]

    def m_topresult(self, ctx: MatchContext) -> Candidate | None:
        if not ctx.first_results:
            raise NotApplicable("no search results at all")
        first = ctx.pool.get(ctx.first_results[0])
        if first and self._verify(ctx.track, first, 0.85, False):
            return first
        return None

    # =====================================================================
    # Pipeline
    # =====================================================================

    def pipeline(self) -> list[tuple[str, Callable[[MatchContext], Candidate | None]]]:
        return [
            ("MANUAL", self.m_manual),
            ("STRICT", self.m_strict),
            ("LOOSE", self.m_loose),
            ("DETAIL", self.m_detail),
            ("REVERSED", self.m_reversed),
            ("SINGLE", self.m_single),
            ("CHANNEL", self.m_channel),
            ("TOPIC", self.m_topic),
            ("SUFFIX", self.m_suffix),
            ("COUNTRY", self.m_country),
            ("FEAT", self.m_feat),
            ("DURATION", self.m_duration),
            ("ARTISTS", self.m_artists),
            ("SHORT", self.m_short),
            ("ARTISTLESS", self.m_artistless),
            ("EXACT", self.m_exact),
            ("WORDSET", self.m_wordset),
            ("SLOWED", self.m_slowed),
            ("UPLOADER", self.m_uploader),
            ("LINKED", self.m_linked),
            ("FIELD", self.m_field),
            ("VARIANTS", self.m_variants),
            ("DUR+VER", self.m_durver),
            ("CLEANARTIST", self.m_cleanartist),
            ("TITLEONLY", self.m_titleonly),
            ("SONGTAG", self.m_songtag),
            ("ISRC", self.m_isrc),
            ("DESCTEXT", self.m_desctext),
            ("BROKEN", self.m_broken),
            ("ALBUM", self.m_album),
            ("PART", self.m_part),
            ("POPULAR", self.m_popular),
            ("TOPRESULT", self.m_topresult),
        ]

    def _free_pass(self, ctx: MatchContext) -> MatchResult | None:
        """Spend nothing first: the same pipeline over zero-quota results.

        YouTube's public search page costs no API units, so its results are put
        in the pool before any metered search. A match here is exactly as
        verified as any other (same verifiers), it just costs no quota.
        """
        free = getattr(self.yt, "free_search_ids", None)
        if free is None:
            return None
        t = ctx.track
        query = f"{t.primary_artist} {t.name}".strip()
        if not query:
            return None
        # A separate cache namespace: free-page results must never be mistaken
        # for a metered API answer.
        key = self.cache.query_key(query, self.results_per_query, self.yt.region_code) + "|free"
        ids = self.cache.get_query(key)
        if ids is None:
            ids = free(query, max_results=self.results_per_query)
            if not ids:
                return None
            self.cache.put_query(key, ids)
            ctx.free_queries.append(query)
        # The results are already in the pool: do not pay 100 units to fetch the
        # same query again from the API when a method asks for it.
        ctx.searched.add(query)
        found, missing = self.cache.get_videos(ids)
        free_videos = getattr(self.yt, "free_videos", None)
        if missing and free_videos is not None:
            fetched = free_videos(missing)
            self.cache.put_videos(fetched)
            found = found + fetched
        if not found:
            return None
        by_id = {c.video_id: c for c in found}
        ordered = [by_id[vid] for vid in ids if vid in by_id]
        ctx.add(ordered)
        if not ctx.first_results:
            ctx.first_results = list(ids)
        for code, fn in self.pipeline():
            try:
                hit = fn(ctx)
            except NotApplicable:
                continue
            except QuotaExceeded:
                raise
            except Exception as exc:  # a broken step must not kill the run
                log.warning("Free method %s failed for %s: %s", code, ctx.track.id, exc)
                hit = None
            self.last_tried.append(code)
            if hit:
                self.last_won = code
                return MatchResult(
                    candidate=hit, method=code, tried=list(self.last_tried), free=True
                )
        return None

    def match(self, track: Track) -> MatchResult | None:
        ctx = MatchContext(track=track)
        self.last_tried = []
        self.last_won = ""
        self.last_pool = []
        early = self._free_pass(ctx)
        if early is not None:
            return early
        for code, fn in self.pipeline():
            try:
                hit = fn(ctx)
            except NotApplicable:
                continue  # condition does not hold -> "never tried"
            except QuotaExceeded:
                raise
            except Exception as exc:  # a broken step must not kill the run
                log.warning("Method %s failed for %s: %s", code, track.id, exc)
                hit = None
            self.last_tried.append(code)
            if hit:
                self.last_won = code
                return MatchResult(candidate=hit, method=code, tried=list(self.last_tried))
        self.last_pool = ctx.candidates()
        return None
