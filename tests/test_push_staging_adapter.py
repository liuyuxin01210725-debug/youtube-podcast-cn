"""Offline checks for the isolated .push-staging Mac adapter.

These tests never open USB, never call devicectl, and never talk to a
simulator. A later Swift consume of a directory written here is a protocol
proof only, not a real-device pass.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import push_staging_adapter as adapter  # noqa: E402


def _payloads(root: Path, audio: bytes = b"adapter-audio") -> Path:
    folder = root / "payloads"
    folder.mkdir()
    (folder / "output.mp3").write_bytes(audio)
    (folder / "document.json").write_bytes(b'{"body":"adapter"}')
    (folder / "source-identity.json").write_bytes(b"{}")
    (folder / "render-identity.json").write_bytes(b"{}")
    (folder / "episode.json").write_bytes(b'{"id":"ep-adapter"}')
    return folder


class PushStagingAdapterTests(unittest.TestCase):
    def test_writes_manifest_then_ready_last_without_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            documents = Path(tmp) / "Documents"
            payloads = _payloads(Path(tmp))
            dest = adapter.write_from_payloads(
                documents, "adapter-tx-1", "ep-adapter", "Adapter Episode", payloads
            )
            self.assertEqual(dest, documents / ".push-staging" / "adapter-tx-1")
            self.assertTrue((dest / "ready").is_file())
            manifest = json.loads((dest / "manifest.json").read_text())
            self.assertEqual(manifest["tx_id"], "adapter-tx-1")
            self.assertEqual((dest / "ready").read_text(), adapter.sha256_bytes((dest / "manifest.json").read_bytes()))
            revision = manifest["packages"][0]["revision"]
            audio = dest / "objects" / revision / "output.mp3"
            self.assertEqual(audio.read_bytes(), b"adapter-audio")
            self.assertEqual(audio.stat().st_nlink, 1)
            self.assertEqual((dest / "manifest.json").stat().st_nlink, 1)
            # ready is last: its mtime is not earlier than the manifest.
            self.assertGreaterEqual((dest / "ready").stat().st_mtime, (dest / "manifest.json").stat().st_mtime)

    def test_refuses_path_traversal_and_existing_tx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            documents = Path(tmp) / "Documents"
            payloads = _payloads(Path(tmp))
            adapter.write_from_payloads(documents, "adapter-tx-1", "ep-adapter", "Adapter Episode", payloads)
            with self.assertRaises(FileExistsError):
                adapter.write_from_payloads(documents, "adapter-tx-1", "ep-adapter", "Adapter Episode", payloads)
            with self.assertRaises(ValueError):
                adapter.write_from_payloads(documents, "../escape", "ep-adapter", "Adapter Episode", payloads)
            with self.assertRaises(ValueError):
                adapter.write_transaction(
                    documents,
                    "tx-bad",
                    [{"id": "ep-adapter", "title": "x", "files": {"../output.mp3": b"x"}}],
                )

    def test_module_does_not_import_device_transports(self) -> None:
        self.assertNotIn("usb", sys.modules)
        source = Path(adapter.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(import|from)\s+usb\b")
        self.assertNotRegex(source, r"\bsubprocess\b")
        self.assertNotRegex(source, r"devicectl\s+")
        self.assertNotRegex(source, r"(?m)^\s*(import|from)\s+studio_push_iphone\b")


if __name__ == "__main__":
    unittest.main()
