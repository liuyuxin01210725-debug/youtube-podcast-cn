"""Explicit delivery of the frozen MOSTPRISE original. No provider or device discovery."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import studio_original_pending as original
from studio_publish_icloud import decode_library_index

FROZEN_MANIFEST_SHA256 = "c57f7b8cdf489766dc7970276d62002cbeebd9934558d3c386cbd1a8edc71687"
APPROVED_LIBRARY_ROOT = Path("/data/shengjian/library")
APPROVED_OSS_BUCKET = "shengjian-private-hx01"
APPROVED_DEVICE_UDID_ENV = "SHENGJIAN_APPROVED_DEVICE_UDID"
COREDEVICE_IDENTIFIER_ENV = "SHENGJIAN_COREDEVICE_IDENTIFIER"
PHYSICAL_UDID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{16}|[0-9a-fA-F]{40}")
COREDEVICE_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
DEVICE_BUNDLE = "com.liuyuxin.privatelisten"
DEVICE_LIBRARY = "Documents/中文播客私人库"
COREDEVICE_VERSION = "651.32"
CATALOG = "library.json"
EPISODE = "episode.json"
ORDER = (
    original.PLAYBACK, original.AUDIO, original.RAW, original.META, original.LOCK,
    original.RECEIPT, original.OFFICIAL, original.TRANSCRIPT, original.MANIFEST, EPISODE,
)
CONTENT_TYPES = {original.PLAYBACK: "audio/mpeg", original.AUDIO: "audio/mp4"}
SHA256 = re.compile(r"[a-f0-9]{64}")
sha256 = original.digest


class DeliveryError(ValueError):
    """Safe message for an operator; payloads and underlying credentials stay private."""


@dataclass(frozen=True)
class Asset:
    name: str
    sha256: str
    size: int

    @property
    def destination(self):
        return f"episodes/{original.EPISODE_ID}/{self.name}"


@dataclass(frozen=True)
class _Package:
    source: Path
    record: dict
    contents: dict[str, bytes]

    def assets(self):
        return tuple(Asset(name, sha256(self.contents[name]), len(self.contents[name])) for name in ORDER)


def _require(condition, message):
    if not condition:
        raise DeliveryError(message)


def _load(package_dir):
    """Pin first, then validate all fixed files and the complete pending provenance."""
    try:
        directory = original.real_directory(package_dir)
        _require(directory.name == original.EPISODE_ID, "节目包目录 ID 超出授权")
        raw_manifest = original.read_bytes(directory / original.MANIFEST)
        _require(sha256(raw_manifest) == FROZEN_MANIFEST_SHA256, "冻结 manifest SHA-256 不符")
        manifest = json.loads(raw_manifest)
        hashes = manifest["sha256"]
        _require(set(hashes) == set(ORDER) - {original.MANIFEST}, "manifest 文件清单不完整")
        contents = {name: (raw_manifest if name == original.MANIFEST else original.read_bytes(directory / name))
                    for name in ORDER}
        _require(all(contents[name] and sha256(contents[name]) == expected for name, expected in hashes.items()),
                 "节目包缺件或文件 SHA-256 不符")
        source, proofs, audio_sha, raw_sha, duration, asr, units = original.checked_inputs(directory)
        created = manifest["created_at"]
        playback_sha = hashes[original.PLAYBACK]
        _require(
            manifest["schema_version"] == 1 and manifest["episode_id"] == original.EPISODE_ID
            and manifest["status"] == "pending_review" and isinstance(created, str)
            and manifest["source"] == source and manifest["asr"] == asr and len(units) == 675
            and hashes[original.AUDIO] == audio_sha and hashes[original.RAW] == raw_sha
            and all(hashes[name] == sha256(data) for name, data in proofs.items())
            and manifest["derivation"] == original.derivation(playback_sha, audio_sha),
            "manifest 语言、来源、原音或 ASR 绑定不符",
        )
        original.validate_coverage(manifest["media"]["source"], manifest["media"]["playback"], duration, units)
        record = json.loads(contents[EPISODE])
        _require(record == {
            "id": original.EPISODE_ID,
            "title": f'{source["title"]} · {source["show"]}（逐段待核）',
            "source_title": source["title"], "show": source["show"], "host": source["host"],
            "guests": [source["guest"]], "source_url": original.EPISODE_URL,
            "created_at": created, "duration_sec": duration, "file": original.PLAYBACK,
            "language": "zh", "audio_kind": "original_audio", "status": "pending_review",
            "transcript_status": "asr_unreviewed", "pending_units": 675,
            "transcript": original.TRANSCRIPT, "manifest": original.MANIFEST,
        }, "节目记录超出冻结待核契约")
        transcript = json.loads(contents[original.TRANSCRIPT])
        _require(transcript == {
            "schema_version": 1, "episode_id": original.EPISODE_ID, "language": "zh",
            "kind": "original_audio_asr", "status": "asr_unreviewed", "timeline": "output.mp3.seconds",
            "source_audio_sha256": audio_sha, "playback_audio_sha256": playback_sha,
            "raw_asr_sha256": raw_sha, "segments": units,
        }, "待核稿语言、文本或三项来源哈希不符")
        return _Package(directory, record, contents)
    except DeliveryError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DeliveryError("无法读取或校验冻结节目包") from None


def plan(package_dir) -> tuple[Asset, ...]:
    """Enumerate the ten verified content files. No library or transport access."""
    return _load(package_dir).assets()


def _request(confirmation, baseline):
    _require(confirmation == original.EPISODE_ID and isinstance(baseline, str) and SHA256.fullmatch(baseline),
             "需显式确认本期 ID 与目标目录的当前 SHA-256")


def _catalog_update(raw, package, baseline):
    """Return None on an equivalent existing row, otherwise the current catalog union."""
    try:
        _require(isinstance(raw, bytes), "目标缺少已有 library.json")
        catalog, by_id = decode_library_index(raw.decode("utf-8"))
        if original.EPISODE_ID in by_id:
            row = by_id[original.EPISODE_ID]
            _require(not original.PENDING_JOB_FIELDS.intersection(row)
                     and all(row.get(key) == value for key, value in package.record.items()),
                     "目标同 ID 条目与冻结包冲突")
            return None
        _require(sha256(raw) == baseline, "目标目录基线已改变，需重新核对")
        return original.json_bytes({
            **catalog, "updated_at": datetime.now(original.UTC8).isoformat(timespec="seconds"),
            "episodes": [*catalog["episodes"], package.record],
        })
    except DeliveryError:
        raise
    except (ValueError, RecursionError):
        raise DeliveryError("目标节目目录格式无效") from None


def _check_local_members(directory, package):
    original.real_directory(directory)
    _require(all(original.file_digest(directory / name) == sha256(data)
                 for name, data in package.contents.items()), "目标同 ID 节目包冲突")


def _restore_owned_directory(dest, staged, catalog, package, identity):
    """Only undo our own unpublished directory if no other writer touched it."""
    try:
        found = dest.lstat()
        if (found.st_dev, found.st_ino) != identity or set(p.name for p in dest.iterdir()) != set(ORDER):
            return
        _check_local_members(dest, package)
        _, current = decode_library_index(original.read_bytes(catalog).decode("utf-8"))
        if original.EPISODE_ID not in current:
            dest.rename(staged)
    except (OSError, ValueError, RecursionError):
        # Unknown ownership/commit state: preserve the directory for readback.
        return


def _receipt(result, catalog):
    return {"result": result, "episode_id": original.EPISODE_ID, "status": "pending_review",
            "manifest_sha256": FROZEN_MANIFEST_SHA256, "catalog_sha256": sha256(catalog)}


def receive(package_dir, *, library_root, expected_catalog_sha256, confirm_episode):
    """Explicit local receive into an existing private-library layout."""
    _request(confirm_episode, expected_catalog_sha256)
    requested_root = Path(library_root)
    _require(requested_root == APPROVED_LIBRARY_ROOT, "本期只允许接收至已批准的资料库绝对路径")
    package = _load(package_dir)
    try:
        library = original.real_directory(requested_root)
        _require(not package.source.is_relative_to(library)
                 and not library.is_relative_to(package.source), "目标须为独立既有私人库")
        with original.library_lock(library):
            episodes = original.real_directory(library / "episodes")
            listing = library / CATALOG
            before = original.read_bytes(listing)
            updated = _catalog_update(before, package, expected_catalog_sha256)
            dest = episodes / original.EPISODE_ID
            present = dest.exists() or dest.is_symlink()
            if present:
                _check_local_members(dest, package)
            if updated is None:
                _require(present, "目录已列本期但文件不完整")
                return _receipt("already_present", before)
            with tempfile.TemporaryDirectory(prefix=".selected-original-", dir=library) as temporary:
                stage = Path(temporary)
                staged_catalog = stage / CATALOG
                original.write_bytes(staged_catalog, updated)
                staged_episode = stage / original.EPISODE_ID
                if not present:
                    staged_episode.mkdir(mode=0o700)
                    for name in ORDER:
                        original.write_bytes(staged_episode / name, package.contents[name])
                _require(original.read_bytes(listing) == before, "提交前目录已改变，需重新核对")
                if not present:
                    _require(not dest.exists() and not dest.is_symlink(), "提交前出现同 ID 节目")
                    owned = staged_episode.stat()
                    staged_episode.rename(dest)
                try:
                    staged_catalog.replace(listing)
                except OSError:
                    if not present:
                        _restore_owned_directory(dest, staged_episode, listing, package,
                                                 (owned.st_dev, owned.st_ino))
                    raise
            return _receipt("catalog_recovered" if present else "created", updated)
    except DeliveryError:
        raise
    except (OSError, ValueError):
        raise DeliveryError("本机接收未确认，请核对当前目录和该集文件") from None


def _transfer(package_dir, read, write, *, expected_catalog_sha256, confirm_episode, exclusive_catalog):
    """Serialize against cooperating writers for the entire existing-catalog transaction."""
    _request(confirm_episode, expected_catalog_sha256)
    _require(exclusive_catalog is not None, "缺少与现有目录写入方共用的独占条件")
    package = _load(package_dir)
    try:
        with exclusive_catalog:
            before = read(CATALOG)
            updated = _catalog_update(before, package, expected_catalog_sha256)
            missing = []
            for name in ORDER:
                found = read(name)
                if found is None:
                    missing.append(name)
                elif not isinstance(found, bytes) or sha256(found) != sha256(package.contents[name]):
                    raise DeliveryError("目标已有同名异值文件，停止提交")
            if updated is None:
                _require(not missing, "目录已列本期但文件不完整")
                return _receipt("already_present", before)
            _require(EPISODE in missing or not missing, "目标已有节目记录但缺少前置材料")
            for name in missing:
                data = package.contents[name]
                write(name, data)
                _require(read(name) == data, "文件写入后回读不符，目录未提交")
            _require(read(CATALOG) == before, "目录写入前基线已改变，需重新核对")
            write(CATALOG, updated)
            _require(read(CATALOG) == updated, "目录写入回读未确认，需重新核对")
            return _receipt("catalog_recovered" if not missing else "created", updated)
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError("传输结果未知，停止操作，须独立回读后再决定") from None


def mirror_private_oss(package_dir, store, *, expected_catalog_sha256, confirm_episode, exclusive_catalog):
    """Use the existing private EpisodeOSSStore instance and its fixed catalog key.

    The operator must establish a private destination and a lock shared by all
    catalog writers. Historical OSS methods do not provide catalog compare-and-swap.
    """
    _require(getattr(store, "bucket_name", None) == APPROVED_OSS_BUCKET
             and getattr(getattr(store, "bucket", None), "bucket_name", None) == APPROVED_OSS_BUCKET,
             "本期只允许镜像至已批准的私人 OSS 桶")
    _require(store.prefix == "episodes/" and all(
        store.key(original.EPISODE_ID, name) == f"episodes/{original.EPISODE_ID}/{name}" for name in ORDER
    ), "OSS 目标的节目键不符合既有私人库布局")

    def read(name):
        key = CATALOG if name == CATALOG else store.key(original.EPISODE_ID, name)
        try:
            return store.bucket.get_object(key).read()
        except Exception as error:
            if getattr(error, "status", None) == 404 or getattr(error, "code", None) == "NoSuchKey":
                return None
            raise

    def write(name, data):
        if name == CATALOG:
            return store.put_root_bytes(CATALOG, data, content_type="application/json")
        return store.upload_bytes(original.EPISODE_ID, name, data,
                                  content_type=CONTENT_TYPES.get(name, "application/json"))

    return _transfer(package_dir, read, write, expected_catalog_sha256=expected_catalog_sha256,
                     confirm_episode=confirm_episode, exclusive_catalog=exclusive_catalog)


def _validate_device_target(target):
    from studio_push_iphone import ENV_SWITCH, _DISABLED

    _require(os.environ.get(ENV_SWITCH, "").strip().lower() not in _DISABLED, "已关闭 iPhone 直连同步")
    udid = os.environ.get(APPROVED_DEVICE_UDID_ENV, "")
    identifier = os.environ.get(COREDEVICE_IDENTIFIER_ENV, "")
    _require(PHYSICAL_UDID.fullmatch(udid), f"{APPROVED_DEVICE_UDID_ENV} 必须配置单台物理设备 UDID")
    _require(COREDEVICE_UUID.fullmatch(identifier), f"{COREDEVICE_IDENTIFIER_ENV} 必须配置 CoreDevice UUID")
    _require(isinstance(target, str) and target == udid, "本期只允许传输至已批准的物理 UDID")
    return identifier


def deliver_to_device(package_dir, *, target, runner, expected_catalog_sha256, confirm_episode,
                      exclusive_catalog, backup_root=None, atomic_commit=None):
    """Deliver through a caller-supplied device CAS, with a durable catalog backup."""
    _validate_device_target(target)
    return _deliver_to_device(package_dir, target=target, runner=runner,
                              expected_catalog_sha256=expected_catalog_sha256, confirm_episode=confirm_episode,
                              exclusive_catalog=exclusive_catalog, backup_root=backup_root,
                              atomic_commit=atomic_commit)


def _deliver_to_device(package_dir, *, target, runner, expected_catalog_sha256, confirm_episode,
                       exclusive_catalog, backup_root, atomic_commit):
    _require(callable(runner), "缺少显式单设备读写 runner")
    _require(backup_root is not None and callable(atomic_commit), "缺少手机索引备份或原子提交能力")
    snapshot = None

    def path(name):
        return name if name == CATALOG else f"episodes/{original.EPISODE_ID}/{name}"

    def read(name):
        nonlocal snapshot
        data = runner(target, "read", path(name))
        if name == CATALOG and snapshot is None and isinstance(data, bytes):
            root = original.real_directory(backup_root)
            backup = Path(tempfile.mkdtemp(prefix="selected-original-", dir=root)) / CATALOG
            original.write_bytes(backup, data)
            snapshot = data
        return data

    def write(name, data):
        if name == CATALOG:
            _require(snapshot is not None, "缺少手机索引备份")
            error = atomic_commit(target, data, snapshot)
            _require(error is None, "手机索引原子提交失败")
            return
        return runner(target, "write", path(name), data)

    return _transfer(package_dir, read, write, expected_catalog_sha256=expected_catalog_sha256,
                     confirm_episode=confirm_episode, exclusive_catalog=exclusive_catalog)


def _device_report(path):
    try:
        report = json.loads(original.read_bytes(path))
        return report if isinstance(report, dict) else {}
    except (OSError, ValueError):
        return {}


def _confirmed_missing_device_file(report, remote):
    try:
        error = report["error"]
        return (error["domain"] == "NSCocoaErrorDomain" and error["code"] in (4, 260)
                and error["userInfo"]["NSFilePath"] == remote)
    except (TypeError, KeyError):
        return False


def _bound_coredevice_report(report, argv, kind, outcome):
    info = report.get("info")
    return isinstance(info, dict) and set(info) == {
        "arguments", "commandType", "environment", "jsonVersion", "outcome", "version",
    } and info["arguments"] == argv[1:] and info["commandType"] == kind and (
        info["jsonVersion"], info["version"], info["outcome"]
    ) == (5, COREDEVICE_VERSION, outcome)


def _is_coredevice_lookup_failure(report, argv, remote):
    # The exact observed 7000 allows another read only. It never proves absence.
    return (set(report) == {"error", "errorSignature", "info"}
            and _bound_coredevice_report(report, argv, "devicectl.device.copy.from", "failed")
            and report["errorSignature"] == "(com.apple.dt.CoreDeviceError 7000)"
            and report["error"] == {
                "code": 7000, "domain": "com.apple.dt.CoreDeviceError", "userInfo": {
                    "NSLocalizedDescription": {"string": f"Failed to retrieve the file node for {remote}"},
                },
            })


def _device_directory_members(command_runner, device, folder, remote, coredevice_identifier):
    report_path = folder / f"inventory-{Path(remote).name}.json"
    argv = [
        "xcrun", "devicectl", "device", "info", "files",
        "--device", device, "--domain-type", "appDataContainer", "--domain-identifier", DEVICE_BUNDLE,
        "--subdirectory", remote, "--no-recurse", "--json-output", str(report_path),
    ]
    code, _, _ = command_runner(argv, 90)
    report = _device_report(report_path)
    _require(code == 0 and set(report) == {"info", "result"}
             and _bound_coredevice_report(report, argv, "devicectl.device.info.files", "success"),
             "设备目录清单未证明本次无过滤的读取成功，停止传输")
    result = report["result"]
    _require(isinstance(result, dict) and set(result) == {
        "deviceIdentifier", "domain", "domainIdentifier", "files",
    } and (result["deviceIdentifier"], result["domain"], result["domainIdentifier"]) == (
        coredevice_identifier, "appDataContainer", DEVICE_BUNDLE,
    ) and isinstance(result["files"], list), "设备目录清单的目标或完整性不明，停止传输")

    entries = {}
    flags = {"isDirectory", "isHidden", "isReadable", "isSymbolicLink", "isWritable"}
    for row in result["files"]:
        _require(isinstance(row, dict) and set(row) == {"name", "relativePath", "metadata", "resources"},
                 "设备目录清单条目不完整，停止传输")
        name, resources = row["name"], row["resources"]
        _require(isinstance(name, str) and name not in ("", ".", "..") and "/" not in name
                 and row["relativePath"] == name and name not in entries
                 and isinstance(row["metadata"], dict)
                 and isinstance(resources, dict) and set(resources) == flags
                 and all(type(value) is bool for value in resources.values()),
                 "设备目录清单条目异常，停止传输")
        entries[name] = resources
    return entries


def _confirmed_inventory_absence(command_runner, device, folder, name, catalog, coredevice_identifier):
    root = f"{DEVICE_LIBRARY}/episodes"
    entries = _device_directory_members(command_runner, device, folder, root, coredevice_identifier)
    # Cross-check against the current device catalog read in this transaction.
    _, catalog_ids = decode_library_index(catalog.decode("utf-8"))

    def readable_directory(item):
        return item["isDirectory"] and item["isReadable"] and not item["isSymbolicLink"]

    _require(catalog_ids and all(ident in entries and readable_directory(entries[ident]) for ident in catalog_ids),
             "设备目录清单未覆盖当前目录文件中的节目，停止传输")
    if original.EPISODE_ID not in entries:
        return True
    _require(readable_directory(entries[original.EPISODE_ID]), "本期文件夹无法安全读取，停止传输")
    members = _device_directory_members(command_runner, device, folder, f"{root}/{original.EPISODE_ID}",
                                        coredevice_identifier)
    return Path(name).name not in members


def deliver_to_device_via_devicectl(package_dir, *, target, command_runner, scratch_root,
                                   expected_catalog_sha256, confirm_episode, exclusive_catalog,
                                   backup_root=None, atomic_commit=None):
    """Use devicectl for reads and assets. The catalog requires an external CAS."""
    coredevice_identifier = _validate_device_target(target)
    _require(callable(command_runner), "缺少显式 devicectl 命令 runner")
    catalog = None

    def runner(device, operation, name, payload=None):
        nonlocal catalog
        scratch = original.real_directory(scratch_root)
        remote = f"{DEVICE_LIBRARY}/{name}"
        with tempfile.TemporaryDirectory(prefix=".selected-device-", dir=scratch) as temporary:
            folder = Path(temporary)
            local = folder / Path(name).name
            report = folder / "devicectl.json"
            writing = operation == "write"
            if writing:
                original.write_bytes(local, payload)
            argv = [
                "xcrun", "devicectl", "device", "copy", "to" if writing else "from",
                "--device", device, "--domain-type", "appDataContainer",
                "--domain-identifier", DEVICE_BUNDLE,
                "--source", str(local) if writing else remote,
                "--destination", remote if writing else str(local),
                "--json-output", str(report),
            ]
            timeout = 900 if local.name in CONTENT_TYPES else 90
            code, _, _ = command_runner(argv, timeout)
            if code == 0:
                if writing:
                    return None
                data = original.read_bytes(local)
                if name == CATALOG:
                    catalog = data
                return data
            if not writing:
                outcome = _device_report(report)
                if _confirmed_missing_device_file(outcome, remote):
                    return None
                if (code == 1 and catalog is not None and name != CATALOG
                        and _is_coredevice_lookup_failure(outcome, argv, remote)
                        and _confirmed_inventory_absence(command_runner, device, folder, name, catalog,
                                                         coredevice_identifier)):
                    return None
            raise DeliveryError("设备复制或回读结果未知，停止传输")

    return _deliver_to_device(package_dir, target=target, runner=runner,
                              expected_catalog_sha256=expected_catalog_sha256, confirm_episode=confirm_episode,
                              exclusive_catalog=exclusive_catalog, backup_root=backup_root,
                              atomic_commit=atomic_commit)


def receive_cli(argv, publish):
    parser = argparse.ArgumentParser(description="接收已冻结的 MOSTPRISE 中文原音待核包")
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--expected-catalog-sha256", required=True)
    parser.add_argument("--confirm-episode", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = publish(args.package_dir, library_root=args.library,
                          expected_catalog_sha256=args.expected_catalog_sha256,
                          confirm_episode=args.confirm_episode)
    except DeliveryError as error:
        print(f"待核原音接收停止：{error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False))
    return 0
