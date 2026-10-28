"""Spoken sponsor windows for private-listen. Not a music-bed or ML ad detector.

English caption text is scanned before translation. A hit marks that segment,
then the window grows only while neighboring segments still sound like a
sponsor read. Those segments are kept in the document with ``is_ad`` and are
not translated or spoken. A short speaker id that lives inside the window is
folded away so it does not become an extra voice; the expected-speaker gate
still sees everyone else.
"""
from __future__ import annotations

import re

# A sponsor cue is enough to open a window.
_CUES = (
    r"brought to you by",
    r"this episode is sponsored",
    r"this show is sponsored",
    r"this podcast is sponsored",
    r"sponsored by",
    r"\bour sponsors?\b",
    r"sponsor message",
    r"word from our sponsors?",
    r"thanks to our sponsors?",
    r"paid partnership",
    r"\buse (?:the |my |our )?(?:promo |discount |coupon )?code\b",
    r"\bpromo code\b",
    r"\bdiscount code\b",
    r"\bcoupon code\b",
    r"\bad break\b",
    r"we(?:'|’)ll be right back",
    r"we will be right back",
    r"after the break\b",
    r"after this (?:short )?break\b",
    r"back after this\b",
    r"\b(?:visit|head to|go to)\b.{0,48}(?:\bslash\b|/)",
)
# Weaker lines can only extend a window that a cue already opened.
_TONE = _CUES + (
    r"\bsponsor(?:ed|s|ship)?\b",
    r"\bpromo\b",
    r"\bdiscount\b",
    r"\bcoupon\b",
    r"\bpercent off\b",
    r"\d+\s*%\s*off\b",
    r"\bfree trial\b",
    r"\blink in the description\b",
    r"\badvertisement\b",
    r"\blimited time\b",
    r"\.com\b",
    r"\bwww\.",
    r"\bslash\b",
)
_CUE_RE = re.compile("|".join(_CUES), re.IGNORECASE)
_TONE_RE = re.compile("|".join(_TONE), re.IGNORECASE)

MAX_EXTEND_SEGMENTS = 8
MAX_EXTEND_MS = 90_000
MAX_GAP_MS = 6_000
# Mostly inside the ad, and not a long stretch of the real conversation.
AD_TIME_RATIO = 0.6
AD_SHARE_LIMIT = 0.15
AD_NON_AD_MS = 20_000


def sponsor_cue(text: str) -> bool:
    return bool(text and _CUE_RE.search(text))


def sponsor_tone(text: str) -> bool:
    return bool(text and _TONE_RE.search(text))


def mark_sponsor_windows(segments: list[dict]) -> list[dict]:
    """Mark cue segments and contiguous sponsor-tone neighbors. Others stay put."""
    flags = [False] * len(segments)
    tones = [sponsor_tone(str(segment.get("text") or "")) for segment in segments]
    for index, segment in enumerate(segments):
        if not sponsor_cue(str(segment.get("text") or "")):
            continue
        flags[index] = True
        for direction in (-1, 1):
            previous = index
            cursor = index + direction
            steps = 0
            while 0 <= cursor < len(segments) and steps < MAX_EXTEND_SEGMENTS:
                if not tones[cursor]:
                    break
                gap = _gap_ms(segments[previous], segments[cursor], direction)
                anchor_gap = _gap_ms(segments[index], segments[cursor], direction)
                if gap > MAX_GAP_MS or anchor_gap > MAX_EXTEND_MS:
                    break
                flags[cursor] = True
                steps += 1
                previous = cursor
                cursor += direction
    marked = []
    for segment, flag in zip(segments, flags):
        copied = dict(segment)
        copied["is_ad"] = flag
        marked.append(copied)
    return marked


def ad_only_speakers(turns, segments: list[dict]) -> set[str]:
    """Speaker ids whose turns sit in the sponsor window and are not a main voice."""
    ranges = [
        (int(segment.get("start_ms") or 0), int(segment.get("end_ms") or 0))
        for segment in segments
        if segment.get("is_ad")
    ]
    if not ranges or not turns:
        return set()
    totals: dict[str, list[int]] = {}
    for turn in turns:
        speaker = getattr(turn, "speaker_id", None)
        if speaker is None and isinstance(turn, dict):
            speaker = turn.get("speaker_id")
        start = getattr(turn, "start_ms", None)
        end = getattr(turn, "end_ms", None)
        if isinstance(turn, dict):
            start = turn.get("start_ms", start)
            end = turn.get("end_ms", end)
        if not speaker or start is None or end is None:
            continue
        duration = max(0, int(end) - int(start))
        advert = sum(_overlap(int(start), int(end), left, right) for left, right in ranges)
        bucket = totals.setdefault(speaker, [0, 0])
        bucket[0] += duration
        bucket[1] += advert
    everyone = {speaker for speaker, (duration, _advert) in totals.items() if duration > 0}
    episode = sum(duration for duration, _advert in totals.values()) or 1
    chosen = set()
    for speaker, (duration, advert) in totals.items():
        if duration <= 0:
            continue
        outside = duration - advert
        if advert / duration < AD_TIME_RATIO:
            continue
        if duration / episode <= AD_SHARE_LIMIT or outside <= AD_NON_AD_MS:
            chosen.add(speaker)
    # Never fold the whole cast; the hard gate should fail closed instead.
    if not chosen or chosen >= everyone:
        return set()
    return chosen


def fold_ad_speakers(segments: list[dict], ad_only: set[str]) -> list[dict]:
    """Point ad-only ids at the nearest real speaker. Do not invent a voice slot."""
    if not ad_only:
        return [dict(segment) for segment in segments]
    folded = []
    for index, segment in enumerate(segments):
        speaker = segment.get("speaker_id")
        copied = dict(segment)
        if speaker in ad_only:
            host = _nearest_real_speaker(segments, index, ad_only)
            if host:
                copied["speaker_id"] = host
                copied["folded_from"] = speaker
        folded.append(copied)
    return folded


_AD_KEY = "\x1fad:"


def group_key(speaker_id, is_ad: bool):
    """Keep an ad cue from merging into the next line of real dialogue."""
    if is_ad:
        return f"{_AD_KEY}{speaker_id or ''}"
    return speaker_id


def split_group_key(key):
    if isinstance(key, str) and key.startswith(_AD_KEY):
        return key[len(_AD_KEY) :], True
    return key, False


def apply_ad_hygiene(segments: list[dict], turns) -> tuple[list[dict], set[str]]:
    marked = mark_sponsor_windows(segments)
    ad_only = ad_only_speakers(turns, marked)
    return fold_ad_speakers(marked, ad_only), ad_only


def _nearest_real_speaker(segments: list[dict], index: int, ad_only: set[str]) -> str | None:
    for distance in range(1, len(segments) + 1):
        for cursor in (index - distance, index + distance):
            if 0 <= cursor < len(segments):
                speaker = segments[cursor].get("speaker_id")
                if speaker and speaker not in ad_only:
                    return speaker
    for segment in segments:
        speaker = segment.get("speaker_id")
        if speaker and speaker not in ad_only:
            return speaker
    return None


def _gap_ms(earlier: dict, later: dict, direction: int) -> int:
    if direction > 0:
        return int(later.get("start_ms") or 0) - int(earlier.get("end_ms") or 0)
    return int(earlier.get("start_ms") or 0) - int(later.get("end_ms") or 0)


def _overlap(start: int, end: int, left: int, right: int) -> int:
    return max(0, min(end, right) - max(start, left))
