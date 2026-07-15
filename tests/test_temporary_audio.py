#!/usr/bin/env python3
"""Tests for temporary source-audio ownership and verified cleanup."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from temporary_audio import (  # noqa: E402
    TemporaryAudioCleanupError,
    TemporaryAudioError,
    with_temporary_audio,
)


URL = "https://www.youtube.com/watch?v=abc123xyz00"


class TemporaryAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.job_dir = Path(self.temporary.name) / "job"
        self.job_dir.mkdir()

    def fake_runner(self, calls: list[list[str]]):
        def run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[0] == "yt-dlp":
                output_template = Path(command[command.index("--output") + 1])
                output_template.with_name("source.webm").write_bytes(b"source-audio")
            elif command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"mono-wav")
            return subprocess.CompletedProcess(command, 0, "", "")

        return run

    def test_download_is_audio_only_and_ffmpeg_is_mono_16k(self) -> None:
        calls: list[list[str]] = []
        seen_wav: list[Path] = []

        result = with_temporary_audio(
            URL,
            self.job_dir,
            lambda wav: seen_wav.append(wav) or "analyzed",
            runner=self.fake_runner(calls),
        )

        download, convert = calls
        self.assertIn("--no-playlist", download)
        self.assertIn("--no-write-info-json", download)
        self.assertIn("--no-write-thumbnail", download)
        self.assertIn("bestaudio/best", download)
        self.assertNotIn("--write-info-json", download)
        self.assertEqual(convert[convert.index("-ac") + 1], "1")
        self.assertEqual(convert[convert.index("-ar") + 1], "16000")
        self.assertEqual(convert[convert.index("-c:a") + 1], "pcm_s16le")
        self.assertEqual(result.value, "analyzed")
        self.assertEqual(
            result.source_audio_sha256,
            hashlib.sha256(b"source-audio").hexdigest(),
        )
        self.assertEqual(result.cleanup_status, "deleted")
        self.assertFalse(seen_wav[0].exists())

    def test_cleanup_runs_after_success_failure_and_interrupt(self) -> None:
        for error in (None, RuntimeError("backend"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__ if error else "success"):
                calls: list[list[str]] = []

                def analyze(wav: Path) -> str:
                    self.assertTrue(wav.exists())
                    if error is not None:
                        raise error
                    return "ok"

                if error is None:
                    with_temporary_audio(
                        URL,
                        self.job_dir,
                        analyze,
                        runner=self.fake_runner(calls),
                    )
                else:
                    with self.assertRaises(type(error)):
                        with_temporary_audio(
                            URL,
                            self.job_dir,
                            analyze,
                            runner=self.fake_runner(calls),
                        )
                self.assertEqual(
                    list(self.job_dir.glob(".diarization-media-*")), []
                )

    def test_cleanup_runs_when_download_fails(self) -> None:
        def failed_runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "download failed")

        with self.assertRaisesRegex(TemporaryAudioError, "下载原音频失败"):
            with_temporary_audio(
                URL, self.job_dir, lambda wav: None, runner=failed_runner
            )

        self.assertEqual(list(self.job_dir.glob(".diarization-media-*")), [])

    def test_download_error_does_not_expose_remote_url(self) -> None:
        signed_url = "https://www.youtube.com/watch?v=private&signature=secret"

        def failed_runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                command, 1, "", f"provider rejected {signed_url}"
            )

        with self.assertRaises(TemporaryAudioError) as raised:
            with_temporary_audio(
                URL, self.job_dir, lambda wav: None, runner=failed_runner
            )

        self.assertNotIn("youtube.com", str(raised.exception))
        self.assertNotIn("signature=secret", str(raised.exception))

    def test_rejects_multiple_downloaded_files(self) -> None:
        def duplicate_runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[0] == "yt-dlp":
                temp_dir = Path(command[command.index("--output") + 1]).parent
                (temp_dir / "source.webm").write_bytes(b"one")
                (temp_dir / "source.m4a").write_bytes(b"two")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(TemporaryAudioError, "恰好一个"):
            with_temporary_audio(
                URL, self.job_dir, lambda wav: None, runner=duplicate_runner
            )

    def test_rejects_symlinked_download(self) -> None:
        external = Path(self.temporary.name) / "external.webm"
        external.write_bytes(b"private")

        def symlink_runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[0] == "yt-dlp":
                temp_dir = Path(command[command.index("--output") + 1]).parent
                (temp_dir / "source.webm").symlink_to(external)
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(TemporaryAudioError, "安全规则"):
            with_temporary_audio(
                URL, self.job_dir, lambda wav: None, runner=symlink_runner
            )
        self.assertTrue(external.exists())

    def test_cleanup_failure_is_command_failure(self) -> None:
        calls: list[list[str]] = []
        real_rmtree = __import__("shutil").rmtree

        with mock.patch(
            "temporary_audio.shutil.rmtree", side_effect=OSError("busy")
        ):
            with self.assertRaisesRegex(
                TemporaryAudioCleanupError, "临时原音频删除失败"
            ):
                with_temporary_audio(
                    URL,
                    self.job_dir,
                    lambda wav: "ok",
                    runner=self.fake_runner(calls),
                )

        for path in self.job_dir.glob(".diarization-media-*"):
            real_rmtree(path)


if __name__ == "__main__":
    unittest.main()
