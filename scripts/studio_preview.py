"""Shared preview segment selection for studio server + engine."""

from __future__ import annotations


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
