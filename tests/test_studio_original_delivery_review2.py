"""Exercise actual CoreDevice receipt shapes through an offline command runner."""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from . import test_studio_original_delivery_review1 as r1
else:
    import test_studio_original_delivery_review1 as r1
import studio_original_delivery as delivery

WORK = r1.WORK
EPISODES = r1.CONTAINER_PREFIX + "episodes"
SELECTED = EPISODES + "/" + r1.v3.ID


def receipt_info(argv, kind, outcome):
    return {"arguments": argv[1:], "commandType": kind, "environment": {"TERM": "dumb"},
            "jsonVersion": 5, "outcome": outcome, "version": "651.32"}


def inventory_row(name):
    return {"name": name, "relativePath": name, "metadata": {"permissions": 493},
            "resources": {"isDirectory": True, "isHidden": False, "isReadable": True,
                          "isSymbolicLink": False, "isWritable": True}}


def setUpModule():
    r1.v3.guard.WORK = WORK
    r1.v3.guard.setUpModule()


def tearDownModule():
    r1.v3.guard.tearDownModule()


class CoreDeviceTests(unittest.TestCase):
    def setUp(self):
        base = r1.ReviewTests()
        self.addCleanup(base.doCleanups)
        with patch.object(r1, "WORK", WORK):
            base.setUp()
        self.base = base

    def fake(self, *, copy_case=None, list_case=None, existing_parent=False):
        calls, objects = [], {"library.json": self.base.catalog.read_bytes()}
        writes = []

        def fake(argv, timeout):
            self.assertEqual(argv[:3], ["xcrun", "devicectl", "device"])
            def flag(option):
                return argv[argv.index(option) + 1]
            report = Path(flag("--json-output"))
            self.assertEqual(flag("--device"), r1.APPROVED_DEVICE)
            self.assertEqual(flag("--domain-type"), "appDataContainer")
            self.assertEqual(flag("--domain-identifier"), "com.liuyuxin.privatelisten")
            if argv[3:5] == ["info", "files"]:
                remote = flag("--subdirectory")
                self.assertIn(remote, (EPISODES, SELECTED))
                self.assertEqual(argv[argv.index("--no-recurse") + 1], "--json-output")
                self.assertFalse({"--search", "--filter", "--recurse"} & set(argv))
                calls.append(("list", remote))
                payload = {
                    "info": receipt_info(argv, "devicectl.device.info.files", "success"),
                    "result": {"deviceIdentifier": r1.v3.TEST_COREDEVICE,
                               "domain": "appDataContainer", "domainIdentifier": "com.liuyuxin.privatelisten",
                               "files": [inventory_row("neighbor"),
                                         *(inventory_row(f"synthetic-{i:02d}") for i in range(1, 57))]},
                }
                rows = payload["result"]["files"]
                if remote == SELECTED:
                    rows.clear()
                elif existing_parent:
                    rows.append({**rows[0], "name": r1.v3.ID, "relativePath": r1.v3.ID,
                                 "resources": dict(rows[0]["resources"])})
                status = list_case(payload, remote) if list_case else 0
                report.write_text(json.dumps(payload))
                return status, "", "synthetic"

            self.assertEqual(argv[3], "copy")
            direction = argv[4]
            remote = flag("--destination" if direction == "to" else "--source")
            name = remote.removeprefix(r1.CONTAINER_PREFIX)
            calls.append((direction, name))
            if direction == "to":
                self.assertNotEqual(name, "library.json")
                objects[name] = Path(flag("--source")).read_bytes()
                writes.append(name)
            elif name in objects:
                Path(flag("--destination")).write_bytes(objects[name])
            else:
                payload = {
                    "info": receipt_info(argv, "devicectl.device.copy.from", "failed"),
                    "errorSignature": "(com.apple.dt.CoreDeviceError 7000)",
                    "error": {"domain": "com.apple.dt.CoreDeviceError", "code": 7000, "userInfo": {
                        "NSLocalizedDescription": {"string": "Failed to retrieve the file node for " + remote},
                    }},
                }
                status = copy_case(payload) if copy_case else 1
                report.write_text(json.dumps(payload))
                return status, "", "synthetic"
            return 0, "", ""

        return fake, calls, writes, objects

    def transmit(self, fake, calls, writes, objects):
        return delivery.deliver_to_device_via_devicectl(
            self.base.package, target=r1.APPROVED_DEVICE, command_runner=fake,
            scratch_root=self.base.previous.f.base, confirm_episode=r1.v3.ID,
            expected_catalog_sha256=self.base.baseline, exclusive_catalog=threading.Lock(),
            backup_root=self.base.previous.f.base,
            atomic_commit=r1.v3.fake_catalog_cas(self, objects, calls, writes))

    def test_real_shape_7000_requires_fresh_bound_list_and_then_readback(self):
        fake, calls, writes, objects = self.fake()
        self.assertEqual(self.transmit(fake, calls, writes, objects)["result"], "created")
        names = [f"episodes/{r1.v3.ID}/{name}" for name in r1.v3.FILES]
        self.assertEqual(writes, [*names, "library.json"])
        self.assertEqual(calls.count(("list", EPISODES)), 10)
        self.assertTrue(all(calls[i + 1] == ("from", name)
                            for i, (op, name) in enumerate(calls) if op == "to"))
        self.assertEqual(calls[-2:], [("cas", "library.json"), ("from", "library.json")])
        self.assertEqual(self.transmit(fake, calls, writes, objects)["result"], "already_present")
        self.assertEqual(calls.count(("list", EPISODES)), 10)
        self.assertEqual(writes, [*names, "library.json"])
        self.assertEqual([row["id"] for row in json.loads(objects["library.json"])["episodes"]],
                         ["neighbor", r1.v3.ID])

    def test_permission_connection_and_wrong_copy_target_never_use_inventory(self):
        def permission(payload):
            payload["error"]["userInfo"]["NSLocalizedDescription"]["string"] = "Permission denied"
            return 1
        def connection(payload):
            payload["error"]["userInfo"]["NSLocalizedDescription"]["string"] = "Connection lost"
            return 1
        def wrong_target(payload):
            payload["info"]["arguments"][5] = r1.OTHER_DEVICE
            return 1
        def underlying_permission(payload):
            payload["error"]["userInfo"]["NSUnderlyingError"] = {"domain": "NSPOSIXErrorDomain", "code": 13}
            return 1
        for case in (permission, connection, wrong_target, underlying_permission):
            with self.subTest(case=case.__name__):
                fake, calls, writes, objects = self.fake(copy_case=case)
                with self.assertRaises(delivery.DeliveryError):
                    self.transmit(fake, calls, writes, objects)
                self.assertFalse(writes)
                self.assertEqual([op for op, _ in calls], ["from", "from"])

    def test_unbound_or_incomplete_list_never_writes(self):
        def altered(kind):
            def attack(p, path):
                result, info = p["result"], p["info"]
                if kind == "wrong-device":
                    result["deviceIdentifier"] = "99999999-8888-4777-8666-555555555555"
                elif kind == "wrong-domain":
                    result["domain"] = "appGroupDataContainer"
                elif kind == "wrong-bundle":
                    result["domainIdentifier"] = "example.invalid"
                elif kind == "wrong-path":
                    info["arguments"][info["arguments"].index("--subdirectory") + 1] = EPISODES + "/other"
                elif kind == "filter":
                    info["arguments"] = [*info["arguments"], "--filter", "name CONTAINS 'test'"]
                elif kind == "incomplete-marker":
                    result["truncated"] = True
                elif kind == "missing-catalog-neighbor":
                    result["files"] = result["files"][1:]
                elif kind == "unknown-version":
                    info["version"] = "0.0"
                elif kind == "bad-row":
                    del result["files"][0]["relativePath"]
                elif kind == "permission":
                    info["outcome"] = "failed"
                    return 1
                return 0
            return attack
        for kind in ("wrong-device", "wrong-domain", "wrong-bundle", "wrong-path", "filter",
                     "incomplete-marker", "missing-catalog-neighbor", "unknown-version", "bad-row",
                     "permission"):
            with self.subTest(kind=kind):
                fake, calls, writes, objects = self.fake(list_case=altered(kind))
                with self.assertRaises(delivery.DeliveryError):
                    self.transmit(fake, calls, writes, objects)
                self.assertEqual(calls[-1], ("list", EPISODES))
                self.assertFalse(writes)

    def test_parent_present_demands_child_list_and_respects_present_file(self):
        def present(p, path):
            if path == SELECTED:
                neighbor = inventory_row("neighbor")
                p["result"]["files"].append({**neighbor, "name": "output.mp3", "relativePath": "output.mp3",
                                              "resources": {**neighbor["resources"], "isDirectory": False}})
            return 0
        fake, calls, writes, objects = self.fake(existing_parent=True, list_case=present)
        with self.assertRaises(delivery.DeliveryError):
            self.transmit(fake, calls, writes, objects)
        self.assertEqual(calls[-2:], [("list", EPISODES), ("list", SELECTED)])
        self.assertFalse(writes)

    def test_existing_episode_directory_can_have_verified_missing_members(self):
        fake, calls, writes, objects = self.fake(existing_parent=True)
        self.assertEqual(self.transmit(fake, calls, writes, objects)["result"], "created")
        self.assertEqual(calls.count(("list", EPISODES)), 10)
        self.assertEqual(calls.count(("list", SELECTED)), 10)
        self.assertEqual(len(writes), 11)
