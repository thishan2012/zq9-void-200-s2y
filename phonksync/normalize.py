"""Text normalization shared by every matching method.

All comparisons in the matcher run on *folded* text: unicode accents removed,
case folded, marketing noise stripped. Version tags (slowed / sped up / remix
...) are extracted separately so they can be compared as a strict signature.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Characters NFKD does not decompose on its own.
_CHAR_MAP = str.maketrans(
    {
        "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
        "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
        "þ": "th", "Þ": "TH", "ð": "d", "Ð": "D",
    }
)

# Version tags. Order matters only for readability; detection is a set.
#
# These stay PRECISE on purpose: the matcher's version_ok() must still tell
# "Slowed" apart from "Ultra Slowed", because they are different listens and a
# Spotify "Slowed" track must not be matched to an "Ultra Slowed" video.
# Grouping versions into one song is song_family()'s job, further down.
_VERSION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("slowed", re.compile(r"\bslowed\b", re.I)),
    ("reverb", re.compile(r"\breverb(?:ed)?\b", re.I)),
    ("sped_up", re.compile(r"\b(?:sped[\s\-_]?up|speed[\s\-_]?up|faster)\b", re.I)),
    ("nightcore", re.compile(r"\bnight\s?core\b", re.I)),
    ("remix", re.compile(r"\bremix(?:es)?\b", re.I)),
    ("mashup", re.compile(r"\b(?:mashup|mash[\s\-_]?up)\b", re.I)),
    ("edit", re.compile(r"\bedit\b", re.I)),
    ("bass_boosted", re.compile(r"\bbass[\s\-_]?boosted\b", re.I)),
    ("8d", re.compile(r"\b8[\s\-_]?d\b", re.I)),
    ("cover", re.compile(r"\bcover\b", re.I)),
    ("live", re.compile(r"\blive\b", re.I)),
    ("acoustic", re.compile(r"\bacoustic\b", re.I)),
    ("instrumental", re.compile(r"\binstrumental\b", re.I)),
    ("extended", re.compile(r"\bextended(?:[\s\-_]?mix)?\b", re.I)),
    ("lofi", re.compile(r"\blo[\s\-_]?fi\b", re.I)),
    ("tiktok", re.compile(r"\btik\s?tok\b", re.I)),
]

_MARKETING_WORDS = {
    "official", "music", "video", "audio", "lyric", "lyrics", "visualizer",
    "visualiser", "hd", "hq", "4k", "mv", "explicit", "clean", "legendado",
    "traducao", "oficial", "clipe", "full", "song", "free", "download",
    "out", "now", "stream", "premiere", "visual",
}

_MARKETING_PHRASES = [
    "official music video", "official video", "official audio",
    "official lyric video", "lyric video", "music video", "official visualizer",
    "video oficial", "audio oficial", "clipe oficial", "full song",
    "full audio", "free download", "out now",
]

_MARKETING_END_WORDS = [
    "lyrics", "lyric", "visualizer", "visualiser", "hd", "hq", "4k", "mv",
    "explicit", "legendado", "traducao", "audio", "video", "official",
]

_FEAT_RE = re.compile(
    r"[\(\[\{]?\s*(?:feat\.?|ft\.?|featuring)\s+[^\)\]\}]*[\)\]\}]?", re.I
)
_FEAT_CAPTURE_RE = re.compile(
    r"(?:feat\.?|ft\.?|featuring)\s+([^\)\]\}]+)", re.I
)
_BRACKET_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_EMPTY_BRACKET_RE = re.compile(r"[\(\[\{]\s*[\)\]\}]")
_TOPIC_RE = re.compile(r"\s*-\s*topic\s*$", re.I)
SEP_RE = re.compile(r"\s[-–—]\s|\s*\|\s*|\s*//\s*")


def fold(text: str) -> str:
    """Lowercase + strip accents + map special latin characters."""
    text = (text or "").translate(_CHAR_MAP)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold().strip()


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", fold(text))


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, fold(a), fold(b)).ratio()


def extract_version(text: str) -> frozenset[str]:
    """Return the set of version tags found in a title/album string."""
    text = text or ""
    found = set()
    for tag, pat in _VERSION_PATTERNS:
        if pat.search(text):
            found.add(tag)
    return frozenset(found)


# Intensifiers belong to the version phrase, not the song title. Require
# whitespace (not a title separator), so "ULTRA - Slowed" keeps its title.
_INTENSIFIED_RATE_RE = re.compile(
    r"\b(ultra|super|mega|hyper|extra|extremely|very|slightly|semi)\s+"
    r"(slowed|sped[\s\-_]?up|speed[\s\-_]?up|faster)\b", re.I,
)


def version_intensities(text: str) -> frozenset[tuple[str, str]]:
    """Preserve speed modifiers so Slowed is not mistaken for Ultra Slowed."""
    return frozenset(
        ("slowed" if m.group(2).lower() == "slowed" else "sped_up", m.group(1).lower())
        for m in _INTENSIFIED_RATE_RE.finditer(text or "")
    )


def strip_version(text: str) -> str:
    out = _INTENSIFIED_RATE_RE.sub(" ", text or "")
    for _, pat in _VERSION_PATTERNS:
        out = pat.sub(" ", out)
    return out


# Connectors and punctuation left dangling after version/marketing removal.
_ORPHAN_PUNCT_RE = re.compile(r"[\s\-–—:+&/|•·]+$")

# --- grouping versions of one song (dedupe only, never used by the matcher) ---
#
# "Song - Slowed" in playlist 1 and "Song - Ultra Slowed" in playlist 2 are the
# same SONG. For the target playlist only the first one found belongs there, so
# the family key throws away the whole version phrase — intensifier included.
_VERSION_WORD = (
    r"(?:slow(?:ed|er)|reverb(?:ed)?|sped[\s\-_]?up|speed[\s\-_]?up|faster|"
    r"night\s?core|remix(?:es)?|mashup|mash[\s\-_]?up|edit|bass[\s\-_]?boosted|"
    r"8[\s\-_]?d|cover|live|acoustic|instrumental|extended(?:[\s\-_]?mix)?|"
    r"lo[\s\-_]?fi|tik\s?tok|version|mix)"
)
_INTENSIFIER_WORD = (
    r"(?:ultra|super|mega|hyper|extra|extremely|very|slightly|semi|"
    r"hard|deep|more|most|really|ultrafast|ultraslow)"
)
# One version phrase, with any number of intensifiers in front of it.
_FAMILY_VERSION_RE = re.compile(
    r"\b(?:" + _INTENSIFIER_WORD + r"\b[\s\-_]*)*" + _VERSION_WORD + r"\b", re.I
)
_BARE_VERSION_RE = re.compile(r"\b" + _VERSION_WORD + r"\b", re.I)


def _strip_family_version(text: str) -> str:
    """Remove version phrases AND the connectors they leave behind."""
    out = _FAMILY_VERSION_RE.sub(" ", text or "")
    # "Slowed + Reverb" / "Slowed & Reverb" / "Slowed and Reverb" become " + ",
    # " & ", " and " once both tags are gone. Drop only orphaned connectors, so
    # a real title like "Me and You" keeps its "and".
    out = re.sub(r"[\(\[\{]\s*(?:[+&/]|and\s*)+[\)\]\}]", " ", out, flags=re.I)
    out = re.sub(r"\s+(?:[+&]|and)\s*(?=\s*$|\s*[\)\]\}]|\s*[+&])", " ", out, flags=re.I)
    out = re.sub(r"\s*[-–—]\s*(?=\s*$|\s*[\)\]\}])", " ", out)
    return out



def song_family(name: str, artists: list[str] | None) -> str:
    """Identity of the SONG, ignoring which version/remaster it is.

    "MONTAGEM MANADO - Slowed" and "MONTAGEM MANADO - Ultra Slowed" by the same
    artist are one song for playlist purposes: the first one added wins and the
    later version is skipped. The primary artist is part of the key so two
    different artists' songs with the same title stay separate.
    """
    def clean(text: str) -> str:
        text = _ORPHAN_PUNCT_RE.sub("", text).strip()
        return re.sub(r"\s+", " ", text).strip(" -–—:+&")

    main, _ = split_feat(name or "")
    core = clean(fold(strip_marketing(_strip_family_version(main))))
    if not core:
        # The title was swallowed whole ("ULTRA - Slowed"): strip only the bare
        # version word so the song keeps its name.
        core = clean(fold(strip_marketing(_BARE_VERSION_RE.sub(" ", main))))
    primary = ""
    for artist in artists or []:
        cleaned = fold(channel_artist(artist))
        cleaned = re.sub(r"[^\w]+", "", cleaned)
        if cleaned:
            primary = cleaned
            break
    if not core:
        return ""
    return f"{core}::{primary}"


def _bracket_is_marketing(content: str) -> bool:
    words = set(tokens(content))
    return bool(words) and words <= _MARKETING_WORDS


def strip_marketing(text: str) -> str:
    """Remove 'Official Video', '[Lyrics]', 'HD' style noise."""
    out = text or ""
    # Whole bracket groups made only of marketing words.
    out = _BRACKET_RE.sub(
        lambda m: " " if _bracket_is_marketing(m.group(1)) else m.group(0), out
    )
    folded = fold(out)
    for phrase in _MARKETING_PHRASES:
        folded = re.sub(r"\b" + re.escape(phrase) + r"\b", " ", folded)
    # Single marketing words only at the very end ("Song Lyrics", "Song HD").
    changed = True
    while changed:
        changed = False
        for word in _MARKETING_END_WORDS:
            new = re.sub(r"\b" + re.escape(word) + r"\s*$", " ", folded).rstrip()
            if new != folded:
                folded = new
                changed = True
    folded = _EMPTY_BRACKET_RE.sub(" ", folded)
    folded = re.sub(r"\s+", " ", folded).strip(" -–—:|")
    return folded


def split_feat(name: str) -> tuple[str, list[str]]:
    """Return (title without feat clause, list of featured artists)."""
    featured: list[str] = []
    for match in _FEAT_CAPTURE_RE.finditer(name or ""):
        chunk = match.group(1)
        for part in re.split(r",|&|\bx\b|\band\b", chunk, flags=re.I):
            part = part.strip(" .-")
            if part:
                featured.append(part)
    main = _FEAT_RE.sub(" ", name or "")
    main = re.sub(r"\s+", " ", main).strip()
    return main, featured


def base_title(name: str) -> str:
    """Core song title: no feat clause, no version tags, no marketing noise."""
    main, _ = split_feat(name or "")
    out = strip_version(main)
    out = strip_marketing(out)
    out = re.sub(r"[_/|•·]+", " ", out)
    out = re.sub(r"\s+", " ", out).strip(" -–—:")
    return fold(out)


def channel_artist(channel: str) -> str:
    """'Artist - Topic' -> 'Artist'."""
    return _TOPIC_RE.sub("", channel or "").strip()


def has_non_ascii(text: str) -> bool:
    return any(ord(ch) > 127 for ch in (text or ""))


def part_number(text: str) -> str | None:
    m = re.search(r"\b(?:pt\.?|part|vol\.?|volume)\s*(\d+)\b", text or "", re.I)
    return m.group(1) if m else None
