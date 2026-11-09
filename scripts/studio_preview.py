"""Shared preview segment selection for studio server + engine."""

from __future__ import annotations

# Five system slots are the distinguishable set. Do not grow this with the
# diarization ID cap: a sixth speaker already aliases an existing slot.
PREVIEW_SPEAKER_CAP = 5


def preview_limit(segments, cap=PREVIEW_SPEAKER_CAP):
    """One line per speaker, and never more lines than the distinguishable slots."""
    if not isinstance(segments, list) or cap <= 0:
        return 1 if cap > 0 else 0
    speakers = set()
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        speaker = segment.get("speaker_id")
        if speaker:
            speakers.add(speaker)
    if not speakers:
        return 1
    return min(len(speakers), cap)


def select_preview_segments(segments, limit=3):
    """Pick the longest Chinese line per speaker, then pad to limit."""
    if limit <= 0:
        return []
    best_by_speaker = {}
    for segment in segments:
        speaker = segment.get('speaker_id')
        if not speaker:
            continue
        score = len((segment.get('chinese') or '').strip())
        current = best_by_speaker.get(speaker)
        if current is None or score > len((current.get('chinese') or '').strip()):
            best_by_speaker[speaker] = segment
    ordered = []
    seen = set()
    for segment in segments:
        speaker = segment.get('speaker_id')
        if speaker in best_by_speaker and speaker not in seen:
            ordered.append(best_by_speaker[speaker])
            seen.add(speaker)
            if len(ordered) >= limit:
                return ordered
    ids = {segment['id'] for segment in ordered}
    for segment in segments:
        if segment['id'] in ids:
            continue
        ordered.append(segment)
        if len(ordered) >= limit:
            break
    return ordered
