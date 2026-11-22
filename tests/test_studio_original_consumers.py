"""Exercise the ordinary publisher and local consumers with a synthetic original."""
import contextlib
import functools
import io
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from . import test_studio_original_pending as fixtures
else:
    import test_studio_original_pending as fixtures
import studio_publish_icloud as publisher
import studio_push_iphone as mirror

WORK = fixtures.WORK
FORBIDDEN = []
COMMANDS = []
_audit_active = False
_audit_installed = False
LEGACY_PROBE = [fixtures.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1"]


def forbid(name, *args, **kwargs):
    FORBIDDEN.append(name)
    raise AssertionError(f"forbidden consumer call: {name}")


def audit(event, args):
    if not _audit_active:
        return
    if event.startswith("socket.") or event in ("os.system", "os.posix_spawn", "os.exec"):
        forbid(event)
    if event == "subprocess.Popen":
        executable, argv, _, _ = args
        if executable not in fixtures.MEDIA or not isinstance(argv, (list, tuple)):
            forbid(event)
        paths = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-i"]
        if argv[-2:] != ["framecrc", "-"]:
            paths.append(argv[-1])
        if not paths or any(not Path(path).resolve().is_relative_to(WORK) for path in paths):
            forbid("media endpoint outside synthetic directory")
        if "-protocol_whitelist" in argv:
            if argv[argv.index("-protocol_whitelist") + 1] != "file":
                forbid("network media protocol")
        elif list(argv[:-1]) != LEGACY_PROBE:
            forbid("non-allowlisted subprocess arguments")
        COMMANDS.append(tuple(argv))


def setUpModule():
    global _audit_active, _audit_installed
    if not _audit_installed:
        sys.addaudithook(audit)
        _audit_installed = True
    _audit_active = True


def tearDownModule():
    global _audit_active
    _audit_active = False
    print(f"consumer guard: direct attempts={len(FORBIDDEN)}; "
          f"provider/Studio fixture attempts={len(fixtures.FORBIDDEN)}; "
          f"allowlisted legacy duration probes={sum(list(c[:-1]) == LEGACY_PROBE for c in COMMANDS)}; "
          f"allowlisted FFmpeg/FFprobe calls={len(COMMANDS)}", flush=True)
    if FORBIDDEN or fixtures.FORBIDDEN:
        raise AssertionError("a forbidden entrypoint was attempted")


class OriginalConsumerTests(unittest.TestCase):
    def setUp(self):
        for name in ("studio_publish_icloud.default_library_root",
                     "studio_push_iphone.default_library_root",
                     "studio_push_iphone.push_after_publish", "studio_push_iphone.copy_to_app",
                     "studio_push_iphone.reachable_devices", "studio_artwork.download_image",
                     "studio_artwork.render_title_png"):
            guard = patch(name, side_effect=functools.partial(forbid, name))
            guard.start()
            self.addCleanup(guard.stop)
        work_guard = patch.object(fixtures, "WORK", WORK)
        work_guard.start()
        self.addCleanup(work_guard.stop)
        fixture = fixtures.OriginalPendingTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.library, self.episode, self.catalog = fixture.library, fixture.episode, fixture.catalog
        self.normal = {
            "id": "old", "title": "合成旧节目", "file": "output.mp3",
            "created_at": "2026-10-01T00:00:00+08:00", "duration_sec": 60,
            "source_job_id": "old", "status": "legacy_status", "extra_disk": [1, 2],
            "show": "保留节目", "lens_ids": ["sleep"],
        }
        self.catalog_only = {
            "id": "waiting", "title": "合成未同步条目", "file": "output.mp3",
            "created_at": "2026-10-02T00:00:00+08:00", "status": "waiting", "extra": True,
        }
        fixtures.dump(fixture.old / "episode.json", self.normal)
        fixtures.dump(self.catalog, {"episodes": [
            self.catalog_only, {**self.normal, "extra_catalog": {"owner": "合成记录"},
                                "show": "索引里的节目名", "lens_ids": ["nutrition"]},
        ], "unknown": {"keep": True}})
        self.assertEqual(fixture.admit(confirmed=True)["result"], "created")
        fixtures.dump(self.episode / "notes.json", {"notes": "合成笔记"})
        fixtures.dump(self.episode / "stars.json", {"starred": True})
        fixtures.dump(self.episode / "progress.json", {"seconds": 0.25})
        self.pending_record = fixtures.load(self.episode / "episode.json")
        self.original_catalog = fixtures.load(self.catalog)

    def entries(self):
        return {entry["id"]: entry for entry in fixtures.load(self.catalog)["episodes"]}

    def assert_catalog_compatible(self):
        entries = self.entries()
        original = entries[fixtures.IDENT]
        self.assertEqual(original.get("status"), "pending_review")
        self.assertEqual(original.get("transcript_status"), "asr_unreviewed")
        self.assertFalse({"source_job_id", "ready", "complete", "translated", "tts"} & original.keys())
        self.assertEqual({key: original.get(key) for key in self.pending_record}, self.pending_record)
        for key in ("waiting", "old"):
            prior = next(x for x in self.original_catalog["episodes"] if x["id"] == key)
            self.assertEqual(entries[key], prior)
        self.assertEqual(fixtures.load(self.catalog)["unknown"], {"keep": True})

    def assert_pending_links(self):
        record = self.entries()[fixtures.IDENT]
        manifest = fixtures.load(self.episode / record["manifest"])
        transcript = fixtures.load(self.episode / record["transcript"])
        assets = manifest["sha256"]
        for (field, name) in (("source_audio_sha256", fixtures.AUDIO),
                              ("raw_asr_sha256", fixtures.RAW),
                              ("playback_audio_sha256", record["file"])):
            self.assertEqual(transcript[field], assets[name])
            self.assertEqual(fixtures.digest(self.episode / name), assets[name])
        self.assertEqual(manifest["derivation"]["input_sha256"], assets[fixtures.AUDIO])
        self.assertEqual(manifest["derivation"]["output_sha256"], assets[record["file"]])
        self.assertEqual(fixtures.digest(self.episode / record["transcript"]), assets[record["transcript"]])
        self.assertEqual(fixtures.digest(self.episode / "episode.json"), assets["episode.json"])
        self.assertTrue(all(seg["status"] == "pending_review" for seg in transcript["segments"]))
        self.assertFalse(any((self.episode / name).exists() for name in
                             ("transcript.json", "transcript_zh.json", "translation.json",
                              "translations.json", "mock_transcript.txt", "extract.json")))

    def make_job(self, ident):
        job = self.fixture.base / ident
        job.mkdir()
        fixtures.dump(job / "state.json", {"title": "合成普通发布", "status": "complete"})
        shutil.copyfile(self.episode / "output.mp3", job / "output.mp3")
        (job / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic-cover-fixture" * 2)
        return job

    def test_rebuild_preserves_pending_and_catalog_neighbors(self):
        prior = fixtures.load(self.catalog)
        next(row for row in prior["episodes"] if row["id"] == "old").pop("extra_disk")
        fixtures.dump(self.catalog, prior)
        physical = fixtures.snapshot(self.library / "episodes")
        calls = len(COMMANDS)
        payload = publisher.rebuild_library(self.library)
        self.assertEqual(payload, fixtures.load(self.catalog))
        self.assert_catalog_compatible()
        self.assert_pending_links()
        self.assertEqual(fixtures.snapshot(self.library / "episodes"), physical)
        self.assertEqual(len(COMMANDS), calls)

    def test_unusable_index_with_pending_package_is_preserved_then_rebuilds_after_recovery(self):
        original = self.catalog.read_bytes()
        for invalid in (b'{', b'{"episodes":[],"episodes":[]}'):
            with self.subTest(invalid=invalid):
                self.catalog.write_bytes(invalid)
                before = fixtures.snapshot(self.library)
                with self.assertRaisesRegex(publisher.PublishError, "索引格式无效"):
                    publisher.rebuild_library(self.library)
                self.assertEqual(fixtures.snapshot(self.library), before)
        self.catalog.write_bytes(original)
        before = fixtures.snapshot(self.library)
        read_text = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == self.catalog:
                raise PermissionError("synthetic read failure")
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", unreadable):
            with self.assertRaisesRegex(publisher.PublishError, "无法读取现有资料库索引"):
                publisher.rebuild_library(self.library)
        self.assertEqual(fixtures.snapshot(self.library), before)
        publisher.rebuild_library(self.library)
        self.assert_catalog_compatible()
        self.assert_pending_links()

    def test_real_neighbor_publish_leaves_pending_readable_and_duplicate_noop(self):
        physical = fixtures.snapshot(self.library / "episodes")
        calls = len(COMMANDS)
        new = publisher.publish_job(self.make_job("normal-neighbor"), library_root=self.library)
        self.assertEqual(new["source_job_id"], "normal-neighbor")
        self.assertGreater(new["duration_sec"], 0)
        self.assertEqual(len(COMMANDS), calls + 1)
        self.assertEqual(self.entries()["normal-neighbor"]["source_job_id"], "normal-neighbor")
        self.assert_catalog_compatible()
        self.assert_pending_links()
        for prefix in ("old", fixtures.IDENT):
            current = fixtures.snapshot(self.library / "episodes" / prefix)
            self.assertEqual({f"{prefix}/{k}": v for k, v in current.items()},
                             {k: v for k, v in physical.items() if k.startswith(prefix + "/")})
        self.assertEqual(self.fixture.admit(confirmed=True)["result"], "already_present")
        self.assertEqual(len(COMMANDS), calls + 1)

    def test_rebuild_cleans_stale_job_projection_preserving_other_fields(self):
        payload = fixtures.load(self.catalog)
        pending = next(x for x in payload["episodes"] if x["id"] == fixtures.IDENT)
        pending.update(source_job_id=fixtures.IDENT, status="complete", ready=True,
                       complete=True, translated=True, tts="fake", catalog_note={"label": "合成"})
        fixtures.dump(self.catalog, payload)
        calls = len(COMMANDS)
        publisher.rebuild_library(self.library)
        self.assert_catalog_compatible()
        self.assertEqual(self.entries()[fixtures.IDENT]["catalog_note"], {"label": "合成"})
        self.assertEqual(self.fixture.admit(confirmed=True)["result"], "already_present")
        self.assertEqual(len(COMMANDS), calls)

    def test_missing_pending_sidecar_is_not_indexed_as_job_or_pending(self):
        (self.episode / "transcript.pending.json").unlink()
        publisher.rebuild_library(self.library)
        self.assertNotIn(fixtures.IDENT, self.entries())

    def test_ordinary_publish_cannot_overwrite_reserved_pending_episode(self):
        job = self.make_job(fixtures.IDENT)
        before = fixtures.snapshot(self.library)
        calls = len(COMMANDS)
        with self.assertRaises(publisher.PublishError):
            publisher.publish_job(job, library_root=self.library)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(publisher.main(["--job-dir", str(self.episode), "--refresh-title",
                                             "--library", str(self.library), "--no-device"]), 1)
        self.assertEqual(fixtures.snapshot(self.library), before)
        self.assertFalse((job / "private-listen.json").exists())
        self.assertEqual(len(COMMANDS), calls)

    def test_local_copy_planner_lists_resources_without_marking_pending_ready(self):
        files = mirror.episode_files(self.episode)
        self.assertEqual(files[0], (self.episode / "output.mp3", f"episodes/{fixtures.IDENT}/output.mp3"))
        self.assertEqual(files[-1], (self.episode / "episode.json", f"episodes/{fixtures.IDENT}/episode.json"))
        self.assertIn((self.episode / "transcript.pending.json",
                       f"episodes/{fixtures.IDENT}/transcript.pending.json"), files)
        self.assertNotIn("transcript_zh.json", {path.name for path, _ in files})
        self.assert_catalog_compatible()
        self.assert_pending_links()
        self.assertEqual(self.fixture.admit(confirmed=True)["result"], "already_present")
