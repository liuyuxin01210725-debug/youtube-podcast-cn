#!/usr/bin/env python3
"""Stage-aware progress helpers for the studio engine (ETA + payload shape)."""
from __future__ import annotations

import time
from typing import Any


def estimate_eta_seconds(
    completed: int | float,
    total: int | float,
    stage_started_at: float,
    *,
    now: float | None = None,
) -> int | None:
    """Linear remaining-time estimate from stage elapsed time and completed/total.

    Returns None until at least one unit has finished (no rate yet), else a
    non-negative integer second count. completed/total use the same units the
    stage already reports (ASR chunks, diarization percent, translation
    segments, synthesis clips).
    """
    if total is None or completed is None:
        return None
    try:
        completed_n = float(completed)
        total_n = float(total)
    except (TypeError, ValueError):
        return None
    if total_n <= 0 or completed_n < 0:
        return None
    if completed_n >= total_n:
        return 0
    if completed_n <= 0:
        return None
    current = time.time() if now is None else now
    elapsed = current - float(stage_started_at)
    if elapsed <= 0:
        return None
    rate = completed_n / elapsed
    if rate <= 0:
        return None
    return max(0, int(round((total_n - completed_n) / rate)))


def build_progress_payload(
    stage: str,
    *,
    completed: int | float = 0,
    total: int | float = 0,
    stage_started_at: float,
    updated_at: float | None = None,
    subtitle_fallback_reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Assemble progress.json fields including optional ETA and fallback reason."""
    now = time.time() if updated_at is None else updated_at
    payload: dict[str, Any] = {
        "stage": stage,
        "completed": completed,
        "total": total,
        "updated_at": now,
        "stage_started_at": stage_started_at,
    }
    eta = estimate_eta_seconds(completed, total, stage_started_at, now=now)
    if eta is not None:
        payload["eta_seconds"] = eta
    if subtitle_fallback_reason:
        payload["subtitle_fallback_reason"] = subtitle_fallback_reason
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload
