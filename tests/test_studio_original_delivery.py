"""Selected-original delivery, using synthetic packages and explicit in-memory sinks."""
import contextlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

if __package__:
    from . import test_studio_original_pending as fixture
    from . import test_studio_original_consumers as guard
else:
    import test_studio_original_pending as fixture
    import test_studio_original_consumers as guard
import studio_publish_icloud as publisher

WORK = fixture.WORK
ID = fixture.IDENT
FILES = (
    "output.mp3", "mostprise-original.m4a", "mostprise-original-asr-raw.json",
    "INPUT-METADATA.json", "mostprise-source-lock.json", "mostprise-asr-receipt.json",
    "mostprise-official-audio-binding.json", "transcript.pending.json", "manifest.json",
    "episode.json",
)
TEST_DEVICE = "A1B2C3D4-1122334455667788"
TEST_COREDEVICE = "11111111-2222-4333-8444-555555555555"
TEST_DEVICE_ENV = {
    "SHENGJIAN_APPROVED_DEVICE_UDID": TEST_DEVICE,
    "SHENGJIAN_COREDEVICE_IDENTIFIER": TEST_COREDEVICE,
    "PRIVATE_LISTEN_DEVICE_PUSH": "1",
}
TEST_BUCKET = "shengjian-private-hx01"


def setUpModule():
    guard.WORK = WORK
    guard.setUpModule()


def tearDownModule():
    guard.tearDownModule()


class FakeBucket:
    def __init__(self, catalog):
        self.objects = {"library.json": catalog}
        self.writes = []
        self.unknown = None
        self.fail_before = None
        self.after_put = None

    def get_object(self, key):
        import io
        if key not in self.objects:
            raise MissingObject()
        return io.BytesIO(self.objects[key])

    def put_object(self, key, data, headers=None):
        self.writes.append(key)
        if key == self.fail_before:
            raise TimeoutError("synthetic rejected operation")
        self.objects[key] = data
        if self.after_put:
            self.after_put(key)
        if key == self.unknown:
            raise TimeoutError("synthetic lost acknowledgment")


class MissingObject(Exception):
    status = 404


class FakeStore:
    prefix = "episodes/"
    bucket_name = TEST_BUCKET

    def __init__(self, catalog):
        self.bucket = FakeBucket(catalog)
        self.bucket.bucket_name = TEST_BUCKET

    def key(self, episode_id, name):
        return f"{self.prefix}{episode_id}/{name}"

    def upload_bytes(self, episode_id, name, data, *, content_type):
        return self.bucket.put_object(self.key(episode_id, name), data, {"Content-Type": content_type})

    def put_root_bytes(self, name, data, *, content_type):
        return self.bucket.put_object(name, data, {"Content-Type": content_type})


def fake_catalog_cas(test, objects, calls, writes=None):
    def commit(target, data, snapshot):
        test.assertEqual(target, TEST_DEVICE)
        calls.append(("cas", "library.json"))
        if objects["library.json"] != snapshot:
            return "synthetic CAS conflict"
        objects["library.json"] = data
        if writes is not None:
            writes.append("library.json")
        return None
    return commit


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, TEST_DEVICE_ENV))
        for name in ("studio_publish_icloud.default_library_root", "studio_push_iphone.default_library_root",
                     "studio_push_iphone.push_after_publish", "studio_push_iphone.reachable_devices",
                     "studio_push_iphone.copy_to_app", "studio_artwork.download_image",
                     "studio_artwork.render_title_png"):
            self.stack.enter_context(patch(name, side_effect=lambda *a, _name=name, **kw: guard.forbid(_name)))
        self.stack.enter_context(patch.object(fixture, "WORK", WORK))
        self.f = fixture.OriginalPendingTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.raw["result"]["segments"] = [
            {"id": index, "start": index / 1000, "end": (index + 1) / 1000,
             "text": "仅用于离线测试的合成中文。"} for index in range(675)
        ]
        self.f.raw["result"]["segments"][-1]["end"] = 1.1
        self.f.seal()
        self.f.admit(confirmed=True)
        self.package = self.f.episode
        self.target = self.f.base / "fixture/data/shengjian/library"
        (self.target / "episodes").mkdir(parents=True)
        self.catalog = self.target / "library.json"
        self.record = fixture.load(self.package / "episode.json")
        fixture.dump(self.catalog, {"episodes": [{"id": "neighbor", "label": "保留"}],
                                    "future": {"flag": True}})
        self.baseline = fixture.digest(self.catalog)

    def delivery(self):
        self.assertIsNotNone(importlib.util.find_spec("studio_original_delivery"),
                             "V2 lacks the frozen-package delivery and transfer path")
        delivery = importlib.import_module("studio_original_delivery")
        self.stack.enter_context(patch.object(delivery, "APPROVED_LIBRARY_ROOT", self.target))
        self.stack.enter_context(patch.object(delivery, "FROZEN_MANIFEST_SHA256",
                                             fixture.digest(self.package / "manifest.json")))
        return delivery

    def receive(self, **override):
        self.assertTrue(hasattr(publisher, "publish_original_pending"),
                        "V2 ordinary publisher has no typed sealed-original entry")
        return publisher.publish_original_pending(
            self.package, **{"library_root": self.target, "expected_catalog_sha256": self.baseline,
                             "confirm_episode": ID, **override})

    def test_frozen_plan_and_workshop_rejection(self):
        delivery = self.delivery()
        with self.assertRaises(publisher.PublishError):
            publisher.publish_job(self.package, library_root=self.target)
        self.assertEqual([entry.name for entry in delivery.plan(self.package)], list(FILES))

    def test_typed_publish_union_and_noop_excludes_user_files(self):
        self.delivery()
        fixture.dump(self.package / "listen_behavior.json", {"private": True})
        fixture.dump(self.package / "notes.json", {"private": True})
        fixture.dump(self.package / "preferences.json", {"private": True})
        media = len(guard.COMMANDS)
        result = self.receive()
        dest = self.target / "episodes" / ID
        self.assertEqual(result["result"], "created")
        self.assertEqual({p.name for p in dest.iterdir()}, set(FILES))
        self.assertEqual([fixture.digest(dest / name) for name in FILES],
                         [fixture.digest(self.package / name) for name in FILES])
        self.assertEqual(fixture.load(self.catalog)["episodes"], [{"id": "neighbor", "label": "保留"}, self.record])
        self.assertEqual(fixture.load(self.catalog)["future"], {"flag": True})
        self.assertEqual(self.receive()["result"], "already_present")
        self.assertEqual(len(guard.COMMANDS), media)
        self.assertFalse((dest / "transcript_zh.json").exists())

    def test_rejects_wrong_target_confirmation_conflict_and_mixed_bytes(self):
        delivery = self.delivery()
        before = fixture.snapshot(self.target)
        for options in ({"confirm_episode": "other"}, {"library_root": self.f.library},
                        {"expected_catalog_sha256": "a" * 64}):
            with self.subTest(options=options), self.assertRaises(delivery.DeliveryError):
                self.receive(**options)
        for filename in ("manifest.json", "episode.json", "output.mp3", "transcript.pending.json",
                         "mostprise-original-asr-raw.json"):
            asset = self.package / filename
            raw = asset.read_bytes()
            asset.write_bytes(raw + b"mixed")
            with self.subTest(filename=filename), self.assertRaises(delivery.DeliveryError):
                self.receive()
            asset.write_bytes(raw)
        (self.package / "transcript.pending.json").unlink()
        with self.assertRaises(delivery.DeliveryError):
            self.receive()
        self.assertEqual(fixture.snapshot(self.target), before)

    def reseal_fixture(self, filename, value):
        fixture.dump(self.package / filename, value)
        manifest_path = self.package / "manifest.json"
        manifest = fixture.load(manifest_path)
        manifest["sha256"][filename] = fixture.digest(self.package / filename)
        fixture.dump(manifest_path, manifest)
        self.stack.enter_context(patch("studio_original_delivery.FROZEN_MANIFEST_SHA256",
                                       fixture.digest(manifest_path)))

    def test_chinese_and_same_source_checked_even_with_test_pinned_manifest(self):
        delivery = self.delivery()
        entry = fixture.load(self.package / "episode.json")
        entry["language"] = "en"
        self.reseal_fixture("episode.json", entry)
        with self.assertRaises(delivery.DeliveryError):
            self.receive()
        entry["language"] = "zh"
        self.reseal_fixture("episode.json", entry)
        pending = fixture.load(self.package / "transcript.pending.json")
        pending["source_audio_sha256"] = "b" * 64
        self.reseal_fixture("transcript.pending.json", pending)
        with self.assertRaises(delivery.DeliveryError):
            self.receive()

    def test_failed_local_catalog_commit_rolls_back_only_owned_package(self):
        delivery = self.delivery()
        before = fixture.snapshot(self.target)
        replace = os.replace
        def fail(src, dst):
            if Path(dst) == self.catalog:
                raise OSError("synthetic replacement failure")
            return replace(src, dst)
        with patch("os.replace", side_effect=fail), self.assertRaises(delivery.DeliveryError):
            self.receive()
        self.assertEqual(fixture.snapshot(self.target), before)

    def test_concurrent_receivers_share_the_local_catalog_lock(self):
        self.delivery()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.receive()["result"], range(2)))
        self.assertCountEqual(results, ["created", "already_present"])
        self.assertEqual([x["id"] for x in fixture.load(self.catalog)["episodes"]], ["neighbor", ID])

    def test_local_conflict_during_commit_keeps_concurrent_neighbor_and_note(self):
        delivery = self.delivery()
        replace = os.replace
        dest = self.target / "episodes" / ID
        def fail(src, dst):
            if Path(dst) == self.catalog:
                fixture.dump(dest / "notes.json", {"new": "合成笔记"})
                fixture.dump(self.catalog, {"episodes": [{"id": "neighbor"}, {"id": "concurrent"}]})
                raise OSError("synthetic replacement failure")
            return replace(src, dst)
        with patch("os.replace", side_effect=fail), self.assertRaises(delivery.DeliveryError):
            self.receive()
        self.assertEqual(fixture.load(dest / "notes.json"), {"new": "合成笔记"})
        baseline = fixture.digest(self.catalog)
        self.assertEqual(self.receive(expected_catalog_sha256=baseline)["result"], "catalog_recovered")
        self.assertEqual([x["id"] for x in fixture.load(self.catalog)["episodes"]],
                         ["neighbor", "concurrent", ID])

    def fake_store(self):
        store = FakeStore(self.catalog.read_bytes())
        return store, store.bucket

    def test_oss_adapter_with_fake_store_orders_and_reads_back(self):
        delivery = self.delivery()
        store, bucket = self.fake_store()
        args = dict(expected_catalog_sha256=self.baseline, confirm_episode=ID,
                    exclusive_catalog=threading.Lock())
        result = delivery.mirror_private_oss(self.package, store, **args)
        keys = [f"episodes/{ID}/{name}" for name in FILES]
        self.assertEqual(bucket.writes, [*keys, "library.json"])
        self.assertEqual([x["id"] for x in publisher.json.loads(bucket.objects["library.json"])["episodes"]],
                         ["neighbor", ID])
        self.assertEqual([fixture.digest(self.package / name) for name in FILES],
                         [delivery.sha256(bucket.objects[name]) for name in keys])
        self.assertEqual(result["result"], "created")
        self.assertEqual(delivery.mirror_private_oss(self.package, store, **args)["result"], "already_present")
        self.assertEqual(bucket.writes, [*keys, "library.json"])

    def test_oss_unknown_ack_and_stale_baseline_do_not_blindly_retry(self):
        delivery = self.delivery()
        store, bucket = self.fake_store()
        args = dict(expected_catalog_sha256=self.baseline, confirm_episode=ID,
                    exclusive_catalog=threading.Lock())
        first = f"episodes/{ID}/{FILES[0]}"
        bucket.unknown = first
        with self.assertRaises(delivery.DeliveryError):
            delivery.mirror_private_oss(self.package, store, **args)
        self.assertEqual(bucket.writes, [first])
        self.assertEqual(bucket.objects["library.json"], self.catalog.read_bytes())
        bucket.unknown = "library.json"
        with self.assertRaises(delivery.DeliveryError):
            delivery.mirror_private_oss(self.package, store, **args)
        self.assertEqual(bucket.writes.count(first), 1)
        self.assertEqual(delivery.mirror_private_oss(self.package, store, **args)["result"], "already_present")
        self.assertEqual(bucket.writes.count("library.json"), 1)

        other_store, other = self.fake_store()
        other.objects["library.json"] = b'{"episodes":[{"id":"concurrent"}]}'
        with self.assertRaises(delivery.DeliveryError):
            delivery.mirror_private_oss(self.package, other_store, **args)
        self.assertEqual(other.writes, [])

    def test_oss_late_catalog_change_preserves_neighbor_and_recovers_from_readback(self):
        delivery = self.delivery()
        store, bucket = self.fake_store()
        other_catalog = b'{"episodes":[{"id":"neighbor"},{"id":"concurrent"}]}'
        target_note = f"episodes/{ID}/notes.json"
        bucket.objects[target_note] = b"synthetic note on target"
        def concurrent(key):
            if key == f"episodes/{ID}/episode.json":
                bucket.objects["library.json"] = other_catalog
        bucket.after_put = concurrent
        args = dict(confirm_episode=ID, exclusive_catalog=threading.Lock())
        with self.assertRaises(delivery.DeliveryError):
            delivery.mirror_private_oss(self.package, store, expected_catalog_sha256=self.baseline, **args)
        self.assertNotIn("library.json", bucket.writes)
        self.assertEqual(bucket.objects["library.json"], other_catalog)
        self.assertEqual(bucket.objects[target_note], b"synthetic note on target")
        result = delivery.mirror_private_oss(self.package, store,
                                             expected_catalog_sha256=delivery.sha256(other_catalog), **args)
        self.assertEqual(result["result"], "catalog_recovered")
        self.assertEqual(len(bucket.writes), 11)
        self.assertEqual([x["id"] for x in publisher.json.loads(bucket.objects["library.json"])["episodes"]],
                         ["neighbor", "concurrent", ID])

    def test_device_requires_valid_identity_configuration_before_transfer(self):
        delivery = self.delivery()
        runner = Mock()
        options = dict(expected_catalog_sha256=self.baseline, confirm_episode=ID,
                       exclusive_catalog=threading.Lock(), backup_root=self.f.base, atomic_commit=Mock())
        for key, value in (
            ("SHENGJIAN_APPROVED_DEVICE_UDID", None),
            ("SHENGJIAN_APPROVED_DEVICE_UDID", ""),
            ("SHENGJIAN_APPROVED_DEVICE_UDID", "all"),
            ("SHENGJIAN_APPROVED_DEVICE_UDID", "../device"),
            ("SHENGJIAN_APPROVED_DEVICE_UDID", f"{TEST_DEVICE},{TEST_DEVICE}"),
            ("SHENGJIAN_COREDEVICE_IDENTIFIER", None),
            ("SHENGJIAN_COREDEVICE_IDENTIFIER", " "),
        ):
            config = TEST_DEVICE_ENV.copy()
            if value is None:
                del config[key]
            else:
                config[key] = value
            target = config.get("SHENGJIAN_APPROVED_DEVICE_UDID", TEST_DEVICE)
            with self.subTest(key=key, value=value), patch.dict(os.environ, config, clear=True):
                with self.assertRaisesRegex(delivery.DeliveryError, key):
                    delivery.deliver_to_device(self.package, target=target, runner=runner, **options)
                with self.assertRaisesRegex(delivery.DeliveryError, key):
                    delivery.deliver_to_device_via_devicectl(
                        self.package, target=target, command_runner=runner,
                        scratch_root=self.f.base, **options)
        runner.assert_not_called()

    def test_device_requires_backup_and_cas_before_writing(self):
        delivery = self.delivery()
        catalog = self.catalog.read_bytes()
        objects = {"library.json": catalog}
        calls, writes = [], []

        def runner(target, operation, name, payload=None):
            self.assertEqual(target, TEST_DEVICE)
            calls.append((operation, name))
            if operation == "read":
                return objects.get(name)
            self.assertEqual(operation, "write")
            self.assertNotEqual(name, "library.json")
            self.assertTrue(any(path.read_bytes() == catalog
                                for path in self.f.base.glob("selected-original-*/library.json")))
            writes.append(name)
            objects[name] = payload

        options = dict(expected_catalog_sha256=self.baseline, confirm_episode=ID,
                       exclusive_catalog=threading.Lock(), runner=runner, backup_root=self.f.base)
        with self.assertRaisesRegex(delivery.DeliveryError, "备份或原子提交"):
            delivery.deliver_to_device(self.package, target=TEST_DEVICE, **options)
        options["atomic_commit"] = fake_catalog_cas(self, objects, calls, writes)
        with patch.dict(os.environ, {"PRIVATE_LISTEN_DEVICE_PUSH": "0"}):
            with self.assertRaisesRegex(delivery.DeliveryError, "已关闭"):
                delivery.deliver_to_device(self.package, target=TEST_DEVICE, **options)
        for target in ([TEST_DEVICE, "another"], "all", "../another"):
            with self.subTest(target=target), self.assertRaises(delivery.DeliveryError):
                delivery.deliver_to_device(self.package, target=target, **options)
        self.assertEqual(calls, [])

        def reject_commit(target, data, snapshot):
            self.assertEqual(snapshot, catalog)
            objects["library.json"] = b'{"episodes":[{"id":"concurrent"}]}'
            return "synthetic CAS conflict"

        with self.assertRaisesRegex(delivery.DeliveryError, "原子提交失败"):
            delivery.deliver_to_device(self.package, target=TEST_DEVICE,
                                       **{**options, "atomic_commit": reject_commit})
        self.assertEqual(objects["library.json"], b'{"episodes":[{"id":"concurrent"}]}')
        self.assertEqual(writes, [f"episodes/{ID}/{name}" for name in FILES])
        writes.clear()
        objects["library.json"] = catalog
        self.assertEqual(delivery.deliver_to_device(self.package, target=TEST_DEVICE, **options)["result"],
                         "catalog_recovered")
        self.assertEqual(writes, ["library.json"])
        self.assertEqual(calls[-2:], [("cas", "library.json"), ("read", "library.json")])

    def test_candidate_cli_delivery_then_workshop_publish_preserves_pending(self):
        self.delivery()
        indexed = publisher.rebuild_library(self.f.library)
        row = next(item for item in indexed["episodes"] if item["id"] == ID)
        self.assertEqual(row, self.record)
        self.assertEqual(row["status"], "pending_review")
        self.assertNotIn("source_job_id", row)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = publisher.main(["original-pending", "--package-dir", str(self.package),
                                   "--library", str(self.target), "--expected-catalog-sha256",
                                   self.baseline, "--confirm-episode", ID])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["result"], "created")
        before = fixture.snapshot(self.target / "episodes" / ID)
        job = self.f.base / "synthetic-ordinary-job"
        job.mkdir()
        shutil.copyfile(self.package / "output.mp3", job / "output.mp3")
        fixture.dump(job / "state.json", {"title": "合成普通节目"})
        (job / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic fixture cover" * 2)
        normal = publisher.publish_job(job, library_root=self.target)
        rows = {item["id"]: item for item in fixture.load(self.catalog)["episodes"]}
        self.assertEqual(rows[ID], self.record)
        self.assertEqual(rows[job.name]["source_job_id"], normal["source_job_id"])
        self.assertEqual(rows["neighbor"], {"id": "neighbor", "label": "保留"})
        self.assertEqual(fixture.snapshot(self.target / "episodes" / ID), before)

    def test_frozen_receive_refuses_ambiguous_catalog_bytes(self):
        delivery = self.delivery()
        for raw in (b'{"episodes":[],"episodes":[]}', b'{"episodes":[null]}'):
            with self.subTest(raw=raw):
                self.catalog.write_bytes(raw)
                before = fixture.snapshot(self.target)
                with self.assertRaisesRegex(delivery.DeliveryError, "目录格式无效"):
                    self.receive(expected_catalog_sha256=delivery.sha256(raw))
                self.assertEqual(fixture.snapshot(self.target), before)
