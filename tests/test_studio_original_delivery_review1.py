"""Independent review regressions using only synthetic paths and runners."""
import contextlib
import hashlib
import io
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from . import test_studio_original_delivery as v3
else:
    import test_studio_original_delivery as v3
import studio_original_delivery as delivery
import studio_original_pending as original
import studio_publish_icloud as publisher

WORK = v3.WORK
APPROVED_DEVICE = v3.TEST_DEVICE
OTHER_DEVICE = "B2C3D4E5-8877665544332211"
APPROVED_BUCKET = "shengjian-private-hx01"
CONTAINER_PREFIX = "Documents/中文播客私人库/"


def setUpModule():
    v3.guard.WORK = WORK
    v3.guard.setUpModule()


def tearDownModule():
    v3.guard.tearDownModule()


class ReviewTests(unittest.TestCase):
    def setUp(self):
        previous = v3.DeliveryTests()
        self.addCleanup(previous.doCleanups)
        with patch.object(v3, "WORK", WORK):
            previous.setUp()
        self.previous = previous
        self.package = previous.package
        self.target = previous.f.base / "data/shengjian/library"
        self.catalog = self.make_library(self.target)
        self.baseline = v3.fixture.digest(self.catalog)
        self.addCleanup(self.enter_pins().close)

    def enter_pins(self):
        pins = contextlib.ExitStack()
        pins.enter_context(patch.object(delivery, "APPROVED_LIBRARY_ROOT", self.target, create=True))
        pins.enter_context(patch.object(delivery, "FROZEN_MANIFEST_SHA256",
                                        v3.fixture.digest(self.package / "manifest.json")))
        return pins

    def make_library(self, path):
        (path / "episodes").mkdir(parents=True)
        catalog = path / "library.json"
        v3.fixture.dump(catalog, {"episodes": [{"id": "neighbor", "label": "合成保留"}]})
        return catalog

    def test_review1_approved_data_shengjian_library_shape_via_publisher_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = publisher.main(["original-pending", "--package-dir", str(self.package),
                                 "--library", str(self.target), "--confirm-episode", v3.ID,
                                 "--expected-catalog-sha256", self.baseline])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["result"], "created")
        self.assertEqual({p.name for p in (self.target / "episodes" / v3.ID).iterdir()}, set(v3.FILES))

    def test_review1_other_library_paths_never_admitted(self):
        for alternate in (self.previous.f.base / "other" / publisher.LIBRARY_NAME,
                          self.previous.f.base / "other/data/shengjian/library"):
            with self.subTest(alternate=alternate):
                listing = self.make_library(alternate)
                before = listing.read_bytes()
                with self.assertRaises(delivery.DeliveryError):
                    publisher.publish_original_pending(
                        self.package, library_root=alternate, confirm_episode=v3.ID,
                        expected_catalog_sha256=delivery.sha256(before))
                self.assertEqual(listing.read_bytes(), before)
                self.assertEqual(list((alternate / "episodes").iterdir()), [])

    def test_review1_approved_path_cannot_be_a_symlink(self):
        link = self.previous.f.base / "linked-library"
        link.symlink_to(self.target, target_is_directory=True)
        with patch.object(delivery, "APPROVED_LIBRARY_ROOT", link, create=True):
            with self.assertRaises(delivery.DeliveryError):
                publisher.publish_original_pending(
                    self.package, library_root=link, confirm_episode=v3.ID,
                    expected_catalog_sha256=self.baseline)
        self.assertEqual(list((self.target / "episodes").iterdir()), [])

    def test_review1_other_bucket_rejected_before_bucket_io(self):
        for declared, actual in (("other-fake-bucket", "other-fake-bucket"),
                                 (APPROVED_BUCKET, "other-fake-bucket")):
            with self.subTest(declared=declared, actual=actual):
                store, bucket = self.previous.fake_store()
                store.bucket_name = declared
                bucket.bucket_name = actual
                with patch.object(bucket, "get_object", wraps=bucket.get_object) as read:
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.mirror_private_oss(
                            self.package, store, expected_catalog_sha256=self.previous.baseline,
                            confirm_episode=v3.ID, exclusive_catalog=threading.Lock())
                    read.assert_not_called()
                self.assertEqual(bucket.writes, [])

    def test_review1_well_formed_other_device_and_multi_targets_rejected_without_runner(self):
        options = dict(expected_catalog_sha256=self.baseline, confirm_episode=v3.ID,
                       exclusive_catalog=threading.Lock(), backup_root=self.previous.f.base,
                       atomic_commit=lambda *_: self.fail("unexpected catalog commit"))
        calls = []
        def fake(*args):
            calls.append(args)
            self.fail("unexpected device runner call")
        for target in (OTHER_DEVICE, v3.TEST_COREDEVICE, [APPROVED_DEVICE], "all", "../device",
                       f"{APPROVED_DEVICE},{OTHER_DEVICE}", APPROVED_DEVICE.lower()):
            with self.subTest(target=target):
                with self.assertRaisesRegex(delivery.DeliveryError, "已批准的物理 UDID"):
                    delivery.deliver_to_device(self.package, target=target, runner=fake, **options)
                with self.assertRaisesRegex(delivery.DeliveryError, "已批准的物理 UDID"):
                    delivery.deliver_to_device_via_devicectl(
                        self.package, target=target, command_runner=fake,
                        scratch_root=self.previous.f.base, **options)
        self.assertEqual(calls, [])

    def test_review1_stream_hash_and_full_receive_without_python311_api(self):
        source = self.package / "output.mp3"
        want = hashlib.sha256(source.read_bytes()).hexdigest()
        with patch.dict(hashlib.__dict__):
            hashlib.__dict__.pop("file_digest", None)
            self.assertEqual(original.file_digest(source), want)
            result = publisher.publish_original_pending(
                self.package, library_root=self.target, confirm_episode=v3.ID,
                expected_catalog_sha256=self.baseline)
        self.assertEqual(result["result"], "created")

    def fake_devicectl(self, *, unknown_missing=False):
        calls, objects = [], {"library.json": self.catalog.read_bytes()}
        def run(argv, timeout):
            self.assertEqual(argv[:4], ["xcrun", "devicectl", "device", "copy"])
            direction = argv[4]
            self.assertIn(direction, ("from", "to"))
            flags = dict(zip(argv[5::2], argv[6::2], strict=True))
            self.assertEqual(flags["--device"], APPROVED_DEVICE)
            self.assertEqual(flags["--domain-type"], "appDataContainer")
            self.assertEqual(flags["--domain-identifier"], "com.liuyuxin.privatelisten")
            remote = flags["--source" if direction == "from" else "--destination"]
            self.assertTrue(remote.startswith(CONTAINER_PREFIX))
            name = remote.removeprefix(CONTAINER_PREFIX)
            calls.append((direction, name))
            if direction == "to":
                self.assertNotEqual(name, "library.json")
                objects[name] = Path(flags["--source"]).read_bytes()
            elif name in objects:
                Path(flags["--destination"]).write_bytes(objects[name])
            else:
                v3.fixture.dump(Path(flags["--json-output"]), {
                    "error": {"domain": "NSCocoaErrorDomain", "code": 260,
                              "userInfo": {"NSFilePath": "unrelated" if unknown_missing else remote}}})
                return 1, "", "synthetic"
            return 0, "", ""
        return run, calls, objects

    def via_devicectl(self, run, atomic_commit):
        entry = getattr(delivery, "deliver_to_device_via_devicectl", None)
        self.assertTrue(callable(entry), "V3 lacks a concrete appDataContainer copy adapter")
        return entry(self.package, target=APPROVED_DEVICE, command_runner=run,
                     scratch_root=self.previous.f.base, confirm_episode=v3.ID,
                     expected_catalog_sha256=self.baseline, exclusive_catalog=threading.Lock(),
                     backup_root=self.previous.f.base, atomic_commit=atomic_commit)

    def test_review1_devicectl_adapter_orders_full_pack_and_readback(self):
        run, calls, objects = self.fake_devicectl()
        cas = v3.fake_catalog_cas(self, objects, calls)
        self.assertEqual(self.via_devicectl(run, cas)["result"], "created")
        names = [f"episodes/{v3.ID}/{name}" for name in v3.FILES]
        self.assertEqual([name for op, name in calls if op == "to"], names)
        self.assertEqual(calls[-2:], [("cas", "library.json"), ("from", "library.json")])
        self.assertTrue(all(calls[i + 1] == ("from", name)
                            for i, (op, name) in enumerate(calls) if op == "to"))
        for name in v3.FILES:
            self.assertEqual(hashlib.sha256(objects[f"episodes/{v3.ID}/{name}"]).hexdigest(),
                             v3.fixture.digest(self.package / name))
        self.assertEqual(self.via_devicectl(run, cas)["result"], "already_present")
        self.assertEqual([name for op, name in calls if op == "to"], names)
        self.assertEqual(calls.count(("cas", "library.json")), 1)

    def test_review1_devicectl_ambiguous_missing_is_not_permission_to_write(self):
        run, calls, objects = self.fake_devicectl(unknown_missing=True)
        with self.assertRaises(delivery.DeliveryError):
            self.via_devicectl(run, v3.fake_catalog_cas(self, objects, calls))
        self.assertEqual(calls, [("from", "library.json"), ("from", f"episodes/{v3.ID}/output.mp3")])
