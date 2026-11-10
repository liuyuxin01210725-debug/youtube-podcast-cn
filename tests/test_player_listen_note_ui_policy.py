#!/usr/bin/env python3
"""Mirror of iOS PlayerPlaybackPolicy + ListenNoteEditorPolicy probes.

Linux CI has no swiftc; these tables must stay aligned with:
  ios/PrivateListen/PrivateListen/PlayerPlaybackPolicy.swift
  ios/PrivateListen/PrivateListen/ListenNoteEditorPolicy.swift
"""

from __future__ import annotations

import unittest


def should_restart_from_beginning(
    current: float,
    duration: float,
    live_clock: float | None = None,
    epsilon: float = 1.0,
) -> bool:
    if not (duration > 1 and current >= 0):
        return False
    playhead = current
    if live_clock is not None and live_clock >= 0:
        playhead = max(playhead, live_clock)
    return playhead >= max(0.0, duration - max(0.0, epsilon))


def playhead_to_persist(observed: float, live_clock: float) -> float:
    fallback = observed if observed >= 0 else 0.0
    if live_clock != live_clock or live_clock < 0:  # NaN or invalid
        return fallback
    if live_clock == 0 and fallback > 0:
        return fallback
    if live_clock < 1 and fallback >= 1:
        return fallback
    return live_clock


def progress_after_seek(target: float, live_clock: float) -> float:
    if target < 0:
        return 0.0
    if target < 1:
        if live_clock >= 0 and live_clock < 1:
            return live_clock
        return target
    return playhead_to_persist(target, live_clock)


def trimmed(text: str) -> str:
    return text.strip()


def should_reveal_end_on_load(text: str) -> bool:
    return bool(trimmed(text))


def should_move_caret_to_end_on_focus(text: str, is_first_focus_after_load: bool) -> bool:
    if not is_first_focus_after_load:
        return False
    return bool(trimmed(text))


class PlayerPlaybackPolicyTests(unittest.TestCase):
    def test_restart_at_exact_end(self):
        self.assertTrue(should_restart_from_beginning(8, 8))

    def test_restart_when_live_clock_at_end(self):
        self.assertTrue(should_restart_from_beginning(7.2, 8, live_clock=8))
        self.assertTrue(should_restart_from_beginning(0, 8, live_clock=8))

    def test_mid_track_does_not_restart(self):
        self.assertFalse(should_restart_from_beginning(3, 8))
        self.assertFalse(should_restart_from_beginning(6.5, 8, epsilon=1))

    def test_unknown_duration(self):
        self.assertFalse(should_restart_from_beginning(8, 0))

    def test_seek_to_start_ignores_stale_end_clock(self):
        self.assertEqual(progress_after_seek(0, 8), 0)
        self.assertEqual(progress_after_seek(0, 0.2), 0.2)

    def test_mid_seek_keeps_existing_persist_rules(self):
        self.assertEqual(progress_after_seek(40, 40.1), 40.1)
        self.assertEqual(progress_after_seek(40, 0), 40)


class ListenNoteEditorPolicyTests(unittest.TestCase):
    long_note = (
        "TEST-P0 1 syntheticisolatedlisten-note body\n"
        "appendreproduction\n"
        "TEST-R4-END\n"
        "TEST-PM-1\n"
    )

    def test_reveal_end_on_load(self):
        self.assertTrue(should_reveal_end_on_load(self.long_note))
        self.assertFalse(should_reveal_end_on_load("  \n"))

    def test_first_focus_moves_caret_to_end(self):
        self.assertTrue(
            should_move_caret_to_end_on_focus(self.long_note, is_first_focus_after_load=True)
        )
        self.assertFalse(
            should_move_caret_to_end_on_focus(self.long_note, is_first_focus_after_load=False)
        )
        self.assertFalse(
            should_move_caret_to_end_on_focus("", is_first_focus_after_load=True)
        )


if __name__ == "__main__":
    unittest.main()
