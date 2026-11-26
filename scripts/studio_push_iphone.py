#!/usr/bin/env python3
"""Mirror a published episode into the PrivateListen app on a plugged-in iPhone.

iCloud Drive is the primary path. A direct push backs up the phone index and
transfers an immutable staging transaction for the App to merge and commit.
Every failure here is soft. It never changes the studio job or the iCloud
publish, and callers only get a summary dict back.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

import push_staging_adapter as staging
from studio_original_pending import digest, file_digest, json_bytes, open_regular
from studio_publish_icloud import (
    AUDIO_NAME,
    LIBRARY_NAME,
    RENDER_FILES,
    _JOB_ID,
    atomic_write_json,
    default_library_root,
    read_json,
)

BUNDLE_ID = 'com.liuyuxin.privatelisten'
DEVICE_LIBRARY = f'Documents/{LIBRARY_NAME}'
STATUS_NAME = 'private-listen-device.json'
ENV_SWITCH = 'PRIVATE_LISTEN_DEVICE_PUSH'
_INDEX = 'library.json'
_DISABLED = ('0', 'false', 'no', 'off')
_LIST_TIMEOUT = 30
_SMALL_TIMEOUT = 90
_AUDIO_TIMEOUT = 900
_REQUIRED_FILES = {AUDIO_NAME, 'episode.json', *RENDER_FILES}
_SHA256 = re.compile(r'[0-9a-f]{64}')
_REVISION_RESOURCE = re.compile(r'[A-Za-z0-9_.-]+')


def _default_runner(command: list[str], timeout: float):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, '', f'{type(error).__name__}: {error}'
    return result.returncode, result.stdout or '', result.stderr or ''


def devicectl_available() -> bool:
    return sys.platform == 'darwin' and shutil.which('xcrun') is not None


def push_enabled(library: Path) -> bool:
    """`1` forces, `0` disables. Unset: only for the real iCloud library.

    Tests and scratch runs point PRIVATE_LISTEN_LIBRARY at a temp folder;
    those must never land on a phone that happens to be plugged in.
    """
    switch = os.environ.get(ENV_SWITCH, '').strip().lower()
    if switch in _DISABLED:
        return False
    if switch in ('1', 'true', 'yes', 'on'):
        return True
    if os.environ.get('PRIVATE_LISTEN_LIBRARY', '').strip():
        return False
    try:
        return Path(library).expanduser().resolve() == default_library_root().expanduser().resolve()
    except OSError:
        return False


def _devicectl_json(arguments: list[str], runner, timeout: float):
    """Run one devicectl command with --json-output. Returns (ok, payload, error)."""
    handle, name = tempfile.mkstemp(prefix='privatelisten-devicectl-', suffix='.json')
    os.close(handle)
    output = Path(name)
    try:
        code, stdout, stderr = runner(['xcrun', 'devicectl', *arguments, '--json-output', str(output)], timeout)
        payload = read_json(output)
    finally:
        output.unlink(missing_ok=True)
    if code != 0:
        return False, payload, _short_error(stderr or stdout, code)
    return True, payload, None


def _short_error(text: str, code: int) -> str:
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    tail = lines[-1] if lines else f'退出码 {code}'
    return tail[:300]


def _result(payload) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get('result'), dict):
        return payload['result']
    return {}


def reachable_devices(runner=_default_runner) -> list[dict]:
    """Paired physical iOS devices that are not reported unavailable. Wired first."""
    ok, payload, _ = _devicectl_json(['list', 'devices'], runner, _LIST_TIMEOUT)
    if not ok:
        return []
    found = []
    for device in _result(payload).get('devices') or []:
        if not isinstance(device, dict):
            continue
        hardware = device.get('hardwareProperties') or {}
        connection = device.get('connectionProperties') or {}
        properties = device.get('deviceProperties') or {}
        if hardware.get('platform') != 'iOS':
            continue
        if hardware.get('reality') not in (None, 'physical'):
            continue
        if connection.get('pairingState') not in (None, 'paired'):
            continue
        if connection.get('tunnelState') == 'unavailable':
            continue
        ident = device.get('identifier') or hardware.get('udid')
        if not isinstance(ident, str) or not ident.strip():
            continue
        found.append({
            'id': ident.strip(),
            'name': str(properties.get('name') or hardware.get('deviceType') or ident)[:80],
            'transport': connection.get('transportType'),
        })
    found.sort(key=lambda item: 0 if item['transport'] == 'wired' else 1)
    return found


def app_installed(device_id: str, runner=_default_runner) -> bool | None:
    """True / False when devicectl answers, None when it could not tell."""
    ok, payload, _ = _devicectl_json(
        ['device', 'info', 'apps', '--device', device_id, '--bundle-id', BUNDLE_ID],
        runner,
        _LIST_TIMEOUT,
    )
    if not ok:
        return None
    apps = _result(payload).get('apps')
    if not isinstance(apps, list):
        return None
    return len(apps) > 0


def _safe_name(name: str) -> bool:
    return bool(name and name not in ('.', '..') and '/' not in name and '\\' not in name
                and not any(ord(char) < 32 or ord(char) == 127 for char in name))


def _resource_name(name: str) -> bool:
    return _safe_name(name) and not name.startswith('.') and not name.endswith(('.tmp', '.partial'))


def episode_files(episode_dir: Path) -> list[tuple[Path, str]]:
    """Collect regular episode resources, without following links or copying directories."""
    episode_dir = Path(episode_dir)
    if (not _JOB_ID.fullmatch(episode_dir.name) or not episode_dir.is_dir()
            or episode_dir.is_symlink() or '..' in episode_dir.parts):
        return []

    def walk_error(error):
        raise error

    files: list[tuple[Path, str]] = []
    for current, directories, names in os.walk(episode_dir, onerror=walk_error):
        folder = Path(current)
        directories[:] = [name for name in directories if _resource_name(name)
                          and stat.S_ISDIR((folder / name).lstat().st_mode)]
        for name in names:
            path = folder / name
            if _resource_name(name) and stat.S_ISREG(path.lstat().st_mode):
                relative = path.relative_to(episode_dir).as_posix()
                files.append((path, f'episodes/{episode_dir.name}/{relative}'))
    return sorted(files, key=lambda item: (
        0 if item[0] == episode_dir / AUDIO_NAME else 2 if item[0] == episode_dir / 'episode.json' else 1,
        item[1],
    ))


def _app_container(device_id: str) -> list[str]:
    return ['--device', device_id, '--domain-type', 'appDataContainer', '--domain-identifier', BUNDLE_ID]


def copy_to_app(device_id: str, source: Path, destination: str, runner=_default_runner) -> str | None:
    """Copy an episode resource. Returns None on success, else a short error."""
    parts = destination.split('/')
    legacy = (
        len(parts) >= 3
        and parts[0] == 'episodes'
        and _JOB_ID.fullmatch(parts[1])
        and all(_resource_name(part) for part in parts[1:])
    )
    revision = (
        len(parts) == 4
        and parts[0] == 'revisions'
        and _JOB_ID.fullmatch(parts[1])
        and _SHA256.fullmatch(parts[2])
        and _resource_name(parts[3])
        and _REVISION_RESOURCE.fullmatch(parts[3])
    )
    obj = (
        len(parts) == 3
        and parts[0] == 'objects'
        and _SHA256.fullmatch(parts[1])
        and _resource_name(parts[2])
        and _REVISION_RESOURCE.fullmatch(parts[2])
    )
    if not (legacy or revision or obj):
        return '目标不是安全的节目资源路径'
    return _copy_to_container(device_id, source, f'{DEVICE_LIBRARY}/{destination}', runner)


def _copy_to_container(device_id: str, source: Path, destination: str, runner) -> str | None:
    timeout = _AUDIO_TIMEOUT if source.name == AUDIO_NAME else _SMALL_TIMEOUT
    code, stdout, stderr = runner([
        'xcrun', 'devicectl', 'device', 'copy', 'to', *_app_container(device_id),
        '--source', str(source),
        '--destination', destination,
    ], timeout)
    if code != 0:
        return _short_error(stderr or stdout, code)
    return None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f'JSON 对象字段重复：{key}')
        obj[key] = value
    return obj


def _finite_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('JSON 数字超出有限范围')
    return value


def _episode_id(record: dict) -> str:
    # Same identity precedence as the publisher. A malformed chosen ID must
    # never send the record to a lower-priority alias.
    ident = next((record[key] for key in ('id', 'episode_id', 'source_job_id')
                  if record.get(key) is not None), None)
    if not isinstance(ident, str) or not _JOB_ID.fullmatch(ident):
        raise ValueError('节目缺少有效 ID')
    return ident


def _validated_index(raw: bytes) -> tuple[dict, dict[str, dict]]:
    index = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_float=_finite_number, parse_constant=_finite_number)
    if not isinstance(index, dict) or not isinstance(index.get('episodes'), list):
        raise ValueError('索引必须是包含 episodes 数组的对象')
    records = {}
    for record in index['episodes']:
        if not isinstance(record, dict):
            raise ValueError('episodes 条目必须是对象')
        ident = _episode_id(record)
        if ident in records:
            raise ValueError(f'节目 ID 重复：{ident}')
        for key in ('id', 'episode_id', 'source_job_id', 'title', 'show', 'created_at',
                    'file', 'youtube_id', 'artwork', 'artwork_kind'):
            if record.get(key) is not None and not isinstance(record[key], str):
                raise ValueError(f'节目 {ident} 的 {key} 必须是字符串')
        for key in ('guests', 'lens_ids'):
            value = record.get(key)
            if value is not None and (not isinstance(value, list)
                                      or not all(isinstance(item, str) for item in value)):
                raise ValueError(f'节目 {ident} 的 {key} 必须是字符串数组')
        duration = record.get('duration_sec')
        if duration is not None and type(duration) not in (int, float):
            raise ValueError(f'节目 {ident} 的 duration_sec 必须是数字')
        records[ident] = record
    return index, records


def _overlay(old: dict, new: dict) -> dict:
    merged = old.copy()
    for key, value in new.items():
        prior = merged.get(key)
        merged[key] = _overlay(prior, value) if isinstance(prior, dict) and isinstance(value, dict) else value
    return merged


def _creation_time(record: dict) -> datetime:
    try:
        stamp = datetime.fromisoformat((record.get('created_at') or '').replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _merged_index(phone: dict, records: dict[str, dict], delta: dict[str, dict]) -> bytes:
    merged = records.copy()
    for ident, item in delta.items():
        old = records.get(ident, {}).copy()
        prior_revision = old.pop('media_revision', None)
        next_revision = item.get('media_revision')
        if prior_revision is not None or next_revision is not None:
            # These fields describe the selected package, so do not inherit
            # artwork or manifest entries from a different package.
            for key in ('artwork', 'artwork_kind'):
                old.pop(key, None)
            if prior_revision != next_revision:
                for key in ('resume_seconds', 'resume_sec', 'position_seconds', 'playback_progress'):
                    old.pop(key, None)
        record = _overlay(old, item)
        if _episode_id(record) != ident:
            raise ValueError(f'合并后的节目 ID 冲突：{ident}')
        merged[ident] = record
    order = sorted(merged, key=lambda ident: (_creation_time(merged[ident]), ident), reverse=True)
    result = {**phone, 'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'episodes': [merged[ident] for ident in order]}
    return (json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')


def _listed_names(device_id: str, folder: str, runner) -> set[str] | None:
    ok, payload, _ = _devicectl_json([
        'device', 'info', 'files', *_app_container(device_id), '--subdirectory', folder, '--no-recurse',
    ], runner, _LIST_TIMEOUT)
    files = _result(payload).get('files')
    if not ok or not isinstance(files, list):
        return None
    names = set()
    for item in files:
        if not isinstance(item, dict):
            return None
        path = item.get('relativePath', item.get('name'))
        if not isinstance(path, str):
            return None
        name = path.removeprefix(f'{folder}/')
        if not _safe_name(name) or name.casefold() in names:
            return None
        names.add(name.casefold())
    return names


def _verified_empty_library(device_id: str, runner) -> bool:
    names = _listed_names(device_id, DEVICE_LIBRARY, runner)
    if names is not None:
        return names.isdisjoint({_INDEX, 'episodes', 'revisions', 'objects'})
    # Documents must be readable even when the library folder does not exist.
    documents = _listed_names(device_id, 'Documents', runner)
    return documents is not None and LIBRARY_NAME.casefold() not in documents


def _backup_index(device_id: str, root: Path, runner, entry: dict) -> bytes | None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError('手机索引备份目录不能是符号链接')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    folder = Path(tempfile.mkdtemp(prefix=f'{timestamp}-', dir=root))
    backup = folder / _INDEX
    code, stdout, stderr = runner([
        'xcrun', 'devicectl', 'device', 'copy', 'from', *_app_container(device_id),
        '--source', f'{DEVICE_LIBRARY}/{_INDEX}', '--destination', str(backup),
    ], _SMALL_TIMEOUT)
    if backup.exists() or backup.is_symlink():
        entry['backup'] = str(backup)
    if code != 0:
        if 'backup' not in entry:
            folder.rmdir()
            if _verified_empty_library(device_id, runner):
                return None
        raise ValueError(f'读取手机索引失败：{_short_error(stderr or stdout, code)}')
    if backup.is_symlink() or not backup.is_file():
        raise ValueError('读取手机索引后未取得普通备份文件')
    with backup.open('rb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        os.fsync(stream.fileno())
        return stream.read()


# The adapter must compare the current on-device index to the snapshot (None
# means absent), atomically commit on-device, and acknowledge it before returning
# None. A string reports failure. The default transport uses App staging.
AtomicCommit = Callable[[str, bytes, bytes | None], str | None]


def _requested_episode(
    requested: Path | str, library: Path, catalog: dict[str, dict] | None = None
) -> tuple[str, str | None]:
    if isinstance(requested, str) and _JOB_ID.fullmatch(requested):
        return requested, None
    path = Path(requested).expanduser().absolute()
    if '..' not in path.parts and path.is_relative_to(library):
        parts = path.relative_to(library).parts
        if len(parts) == 2 and parts[0] == 'episodes' and _JOB_ID.fullmatch(parts[1]):
            return parts[1], None
        if len(parts) == 3 and parts[0] == 'revisions' and _JOB_ID.fullmatch(parts[1]) and _SHA256.fullmatch(parts[2]):
            return parts[1], '/'.join(parts)
        if len(parts) == 2 and parts[0] == 'objects' and _SHA256.fullmatch(parts[1]):
            if catalog is None:
                raise ValueError('缺少资料库索引，无法反查对象修订')
            target_prefix = f'objects/{parts[1]}'
            matches = [
                ident for ident, row in catalog.items()
                if isinstance(row.get('media_revision'), dict) and row['media_revision'].get('path') == target_prefix
            ]
            if not matches:
                raise ValueError(f'资料库索引中未找到对象修订：{target_prefix}')
            if len(matches) > 1:
                raise ValueError(f'对象修订命中多条节目存在歧义：{target_prefix}')
            return matches[0], target_prefix
    raise ValueError(f'请求节目不属于资料库：{path.name}')


def _revision_files(row: dict, ident: str) -> tuple[str, dict[str, str]]:
    revision = row['media_revision']
    sha = revision.get('sha256') if isinstance(revision, dict) else None
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise ValueError(f'节目修订摘要无效：{ident}')
    path = revision.get('path')
    if path not in (f'objects/{sha}', f'revisions/{ident}/{sha}'):
        raise ValueError(f'节目修订路径无效：{ident}')
    prior = revision.get('previous_path')
    valid_prior = isinstance(prior, str) and prior != path and (
        prior == f'episodes/{ident}'
        or bool(re.fullmatch(rf'(?:revisions/{ident}|objects)/[0-9a-f]{{64}}', prior))
        or (prior == '' and path == f'objects/{sha}')
    )
    if not valid_prior or revision.get('resume_policy') != 'reset_unmapped':
        raise ValueError(f'节目修订历史指针无效：{ident}')
    files = revision.get('files')
    if (not isinstance(files, dict) or not _REQUIRED_FILES.issubset(files)
            or len({name.casefold() for name in files}) != len(files)
            or not all(_resource_name(name) and _REVISION_RESOURCE.fullmatch(name)
                       and isinstance(value, str) and _SHA256.fullmatch(value)
                       for name, value in files.items())):
        raise ValueError(f'节目修订文件清单无效：{ident}')
    if digest(json_bytes(dict(sorted(files.items())))) != sha:
        raise ValueError(f'节目修订文件清单摘要不匹配：{ident}')
    return path, files


def _prepare_push(
    library: Path,
    requests: list[Path | str] | None,
    work: Path,
    *,
    expected_revisions: dict[str, str] | None = None,
):
    listing = library / _INDEX
    if library.is_symlink() or '..' in library.parts:
        raise ValueError('资料库或索引路径无效')
    trusted = library.resolve(strict=True)
    with open_regular(listing) as source:
        _, catalog = _validated_index(source.read())
    delta = {}
    plan = []
    for requested in sorted(catalog) if requests is None else requests:
        ident, requested_revision = _requested_episode(requested, library, catalog=catalog)
        if ident not in catalog:
            raise ValueError(f'请求节目不属于资料库索引：{ident}')
        row = catalog[ident]
        prefix = f'episodes/{ident}'
        hashes = {}
        if 'media_revision' in row:
            prefix, hashes = _revision_files(row, ident)
        expected_rev = expected_revisions.get(ident) if expected_revisions else None
        if requested_revision is not None and expected_rev is not None and requested_revision != expected_rev:
            raise ValueError(f'请求路径与期望节目修订不一致：{ident}')
        target_expected = expected_rev if expected_rev is not None else requested_revision
        if target_expected is not None and target_expected != prefix:
            raise ValueError(f'请求路径与当前节目修订不一致：{ident}')
        if ident in delta:
            continue
        folder = library / prefix
        if folder.resolve(strict=True) != trusted / prefix:
            raise ValueError(f'节目目录路径无效：{ident}')
        files = ([(folder / name, f'{prefix}/{name}') for name in sorted(hashes)] if hashes
                 else episode_files(folder))
        names = {source.relative_to(folder).as_posix() for source, _ in files}
        if not _REQUIRED_FILES.issubset(names):
            raise ValueError(f'节目缺少必要文件：{ident}：{", ".join(sorted(_REQUIRED_FILES - names))}')
        if row.get('file') not in (None, AUDIO_NAME) or (row.get('artwork') and row['artwork'] not in names):
            raise ValueError(f'节目索引引用了无效资源：{ident}')
        for source, destination in files:
            if source.resolve(strict=True) != trusted / destination:
                raise ValueError(f'节目资源路径无效：{destination}')
            staged = work / destination
            staged.parent.mkdir(parents=True, exist_ok=True)
            with open_regular(source) as incoming, staged.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            if (hashes or source.name in _REQUIRED_FILES) and not staged.stat().st_size:
                raise ValueError(f'节目资源为空：{destination}')
            actual = file_digest(staged)
            if hashes and actual != hashes[source.name]:
                raise ValueError(f'节目资源哈希不匹配：{destination}')
            plan.append((staged, destination, prefix, actual))
        delta[ident] = row
    return delta, plan


def _protected_packages(records: dict[str, dict]) -> set[str]:
    protected = set()
    for ident, row in records.items():
        revision = row.get('media_revision')
        if isinstance(revision, dict):
            protected.update(path for key in ('path', 'previous_path')
                             if isinstance(path := revision.get(key), str))
        else:
            protected.add(f'episodes/{ident}')
    return protected


@contextmanager
def _readback_device_file(device_id: str, destination: str, work: Path, runner):
    readback = work / '.device-resource'
    timeout = _AUDIO_TIMEOUT if destination.endswith(f'/{AUDIO_NAME}') else _SMALL_TIMEOUT
    try:
        code, stdout, stderr = runner([
            'xcrun', 'devicectl', 'device', 'copy', 'from', *_app_container(device_id),
            '--source', f'{DEVICE_LIBRARY}/{destination}', '--destination', str(readback),
        ], timeout)
        if code != 0:
            raise ValueError(f'手机现有资源读取失败：{destination}：{_short_error(stderr or stdout, code)}')
        yield readback
    finally:
        readback.unlink(missing_ok=True)


def _verify_device_asset(device_id: str, destination: str, expected: str, work: Path, runner) -> None:
    with _readback_device_file(device_id, destination, work, runner) as readback:
        if file_digest(readback) != expected:
            raise ValueError(f'手机现有资源与请求包不一致：{destination}')


def _catalog_content(raw: bytes):
    def number(value: str) -> tuple[Decimal]:
        # Tuples distinguish JSON numbers from booleans even for 0 and 1.
        try:
            return (Decimal(value),)
        except InvalidOperation as error:
            raise ValueError('JSON 数字无效') from error

    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                      parse_int=number, parse_float=number, parse_constant=_finite_number)


def _verify_device_catalog(device_id: str, expected: bytes, work: Path, runner) -> None:
    with _readback_device_file(device_id, _INDEX, work, runner) as readback:
        with open_regular(readback) as source:
            actual = source.read()
        # LibraryStore.commitSnapshot re-encodes the merged catalog with Foundation.
        # Compare the complete JSON content and keep byte-level hashes for assets.
        try:
            matches = actual == expected or _catalog_content(actual) == _catalog_content(expected)
        except (ValueError, RecursionError) as error:
            raise ValueError(f'手机索引读回无效：{_INDEX}：{error}') from error
        if not matches:
            raise ValueError(f'手机索引读回与请求目录不一致：{_INDEX}')


def _stage_prepared(device_id: str, records: dict, delta: dict, plan: list, work: Path, runner) -> str:
    tx_id = f'{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid4().hex}'
    target = f'Documents/{staging.STAGING_NAME}/{tx_id}'
    resources = {}
    for source, destination, prefix, sha in plan:
        name = destination.removeprefix(f'{prefix}/')
        if not _REVISION_RESOURCE.fullmatch(name):
            raise ValueError(f'App 暂存不支持此节目资源路径：{destination}')
        resources.setdefault(prefix, {})[name] = (source, sha)

    copies = {}
    packages = []
    for ident, row in delta.items():
        parts = resources[row['media_revision']['path'] if 'media_revision' in row else f'episodes/{ident}']
        if len({name.casefold() for name in parts}) != len(parts):
            raise ValueError(f'节目资源名称冲突：{ident}')
        prior = records.get(ident)
        previous_path = ''
        if prior is not None:
            if 'media_revision' in prior:
                previous = prior['media_revision']
                sha = previous.get('sha256') if isinstance(previous, dict) else None
                previous_path = previous.get('path') if isinstance(previous, dict) else None
                if (not isinstance(sha, str) or not _SHA256.fullmatch(sha)
                        or previous_path not in (f'objects/{sha}', f'revisions/{ident}/{sha}')):
                    raise ValueError(f'手机现有修订路径无效：{ident}')
            else:
                previous_path = f'episodes/{ident}'
        package = staging.package_manifest(ident, row, previous_path, {
            name: {'sha256': sha, 'size': source.stat().st_size}
            for name, (source, sha) in parts.items()
        })
        packages.append(package)
        for name, (source, _) in parts.items():
            copies[f'objects/{package["revision"]}/{name}'] = source

    manifest, ready = staging.encode_transaction(tx_id, packages)
    for name, content in ((staging.MANIFEST_NAME, manifest), (staging.READY_NAME, ready)):
        path = work / name
        path.write_bytes(content)
        copies[name] = path
    for relative, source in copies.items():
        error = _copy_to_container(device_id, source, f'{target}/{relative}', runner)
        if error is not None:
            raise ValueError(f'{relative}：{error}')
    return tx_id


def push_episodes(episode_dirs: list[Path | str] | None, library: Path, *, runner=_default_runner,
                  backup_root: Path | None = None, atomic_commit: AtomicCommit | None = None,
                  expected_revisions: dict[str, str] | None = None) -> dict:
    """Select catalog IDs or package paths. None selects every row in the same snapshot."""
    summary = {'ok': False, 'devices': [], 'reason': None, 'episodes': []}
    if os.environ.get(ENV_SWITCH, '').strip().lower() in _DISABLED:
        summary.update(skipped=True, reason='已关闭 iPhone 直连同步')
        return summary

    with tempfile.TemporaryDirectory(prefix='privatelisten-push-') as temporary:
        work = Path(temporary)
        try:
            delta, plan = _prepare_push(
                Path(library).expanduser().absolute(),
                episode_dirs,
                work,
                expected_revisions=expected_revisions,
            )
        except (OSError, ValueError, RecursionError) as error:
            summary['reason'] = f'本地增量无效：{error}'
            return summary
        if not delta:
            summary['reason'] = '资料库里没有可拷贝的节目'
            return summary
        summary['episodes'] = list(delta)
        return _push_prepared(summary, delta, plan, work, runner, backup_root, atomic_commit)


def _push_prepared(summary, delta, plan, work, runner, backup_root, atomic_commit):
    if not devicectl_available():
        summary['reason'] = '这台机器没有 xcrun devicectl'
        return summary
    devices = reachable_devices(runner)
    if not devices:
        summary['reason'] = '没有连着的 iPhone'
        return summary
    backup_root = (Path(backup_root).expanduser() if backup_root is not None else
                   Path.home() / 'Library' / 'Application Support' / 'PrivateListen' / 'device-library-backups')
    for device in devices:
        entry = {'id': device['id'], 'name': device['name'], 'ok': False, 'error': None}
        summary['devices'].append(entry)
        if app_installed(device['id'], runner) is False:
            entry['error'] = '没有安装私人听'
            continue
        try:
            snapshot = _backup_index(device['id'], backup_root, runner, entry)
            phone, records = _validated_index(snapshot) if snapshot is not None else ({}, {})
            if atomic_commit is None:
                entry['transaction'] = _stage_prepared(device['id'], records, delta, plan, work, runner)
                entry['staged'] = True
                continue
            merged = _merged_index(phone, records, delta)
            protected = _protected_packages(records)
            for source, destination, prefix, expected in plan:
                if prefix not in protected:
                    error = copy_to_app(device['id'], source, destination, runner)
                    if error is not None:
                        raise ValueError(f'{destination}：{error}')
                _verify_device_asset(device['id'], destination, expected, work, runner)
            error = atomic_commit(device['id'], merged, snapshot)
            if error is not None:
                raise ValueError(f'手机索引提交失败：{error}')
            # A staging acknowledgement alone cannot prove the catalog
            # actually became visible on this device.
            _verify_device_catalog(device['id'], merged, work, runner)
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError) as error:
            entry['error'] = str(error)
        else:
            entry['ok'] = True
    summary['ok'] = any(entry['ok'] for entry in summary['devices'])
    summary['staged'] = any(entry.get('staged') for entry in summary['devices'])
    if not (summary['ok'] or summary['staged']) and summary['reason'] is None:
        summary['reason'] = next((entry['error'] for entry in summary['devices'] if entry['error']), '拷贝失败')
    return summary


def _record(job_dir: Path | None, summary: dict) -> None:
    if job_dir is None or not Path(job_dir).is_dir():
        return
    try:
        atomic_write_json(Path(job_dir) / STATUS_NAME, {
            **summary,
            'pushed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        })
    except OSError:
        pass


def describe(summary: dict) -> str:
    if summary.get('ok'):
        names = '、'.join(entry['name'] for entry in summary.get('devices', []) if entry.get('ok'))
        return f'私人听：已同步到 iPhone「{names}」（App 内 {DEVICE_LIBRARY}），下拉即可看到。'
    if summary.get('staged'):
        names = '、'.join(entry['name'] for entry in summary['devices'] if entry.get('staged'))
        return f'私人听：已传到 iPhone「{names}」的暂存区，等待 App 前台校验和提交。'
    if summary.get('skipped'):
        return f"私人听：跳过 iPhone 直连同步（{summary.get('reason')}）。"
    return f"私人听：iPhone 直连同步没完成（{summary.get('reason') or '原因不明'}），iCloud 已发布，不影响任务。"


def push_after_publish(result: dict, job_dir: Path | None = None, *, runner=_default_runner, announce=print,
                       backup_root: Path | None = None, atomic_commit: AtomicCommit | None = None) -> dict:
    """Hook for engine / server / CLI after `publish_job` succeeded. Never raises."""
    try:
        library = Path(result.get('library') or default_library_root())
        ep_id = result.get('id')
        ep_dir_raw = result.get('episode_dir')
        expected_revisions = None
        if isinstance(ep_id, str) and _JOB_ID.fullmatch(ep_id) and ep_dir_raw:
            ep_dir = Path(ep_dir_raw).expanduser().absolute()
            abs_lib = library.expanduser().absolute()
            if '..' not in ep_dir.parts and ep_dir.is_relative_to(abs_lib):
                rel_parts = ep_dir.relative_to(abs_lib).parts
                if len(rel_parts) == 2 and rel_parts[0] == 'objects' and _SHA256.fullmatch(rel_parts[1]):
                    requested = ep_id
                    expected_revisions = {ep_id: f'objects/{rel_parts[1]}'}
                else:
                    requested = ep_dir
            else:
                requested = ep_dir
        else:
            requested = Path(ep_dir_raw) if ep_dir_raw else str(ep_id or '')
        if not push_enabled(library):
            return {'ok': False, 'skipped': True, 'reason': '资料库不是默认的 iCloud 位置，或已关闭', 'devices': []}
        summary = push_episodes([requested], library, runner=runner,
                                backup_root=backup_root, atomic_commit=atomic_commit,
                                expected_revisions=expected_revisions)
        if summary.get('reason') in ('这台机器没有 xcrun devicectl', '没有连着的 iPhone'):
            summary['skipped'] = True
    except Exception as error:
        summary = {'ok': False, 'devices': [], 'reason': f'{type(error).__name__}: {error}'[:300]}
    _record(job_dir, summary)
    try:
        if announce is not None:
            announce(describe(summary), flush=True)
    except Exception:
        pass
    return summary


def main(argv: list[str] | None = None, *, runner=_default_runner,
         backup_root: Path | None = None, atomic_commit: AtomicCommit | None = None) -> int:
    parser = argparse.ArgumentParser(description='把 iCloud 私人听资料库里的节目拷到连着 Mac 的 iPhone（私人听 App 的 Documents）')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--job-dir', type=Path, help='工作室任务目录；按目录名找资料库里的那一期')
    target.add_argument('--episode-id', help='资料库里的节目编号')
    target.add_argument('--all', action='store_true', help='资料库里的全部节目')
    parser.add_argument('--library', type=Path, default=None, help='资料库根目录，默认 iCloud Drive 里的「中文播客私人库」')
    args = parser.parse_args(argv)
    library = (args.library or default_library_root()).expanduser()
    if args.all:
        requested = None
    else:
        ident = args.job_dir.expanduser().name if args.job_dir else str(args.episode_id)
        if not _JOB_ID.fullmatch(ident):
            print('私人听：节目编号无效', file=sys.stderr)
            return 1
        requested = [ident]
    summary = push_episodes(requested, library, runner=runner,
                            backup_root=backup_root, atomic_commit=atomic_commit)
    if args.job_dir:
        _record(args.job_dir.expanduser(), summary)
    message = describe(summary)
    accepted = summary['ok'] or summary.get('staged')
    print(message, file=sys.stdout if accepted else sys.stderr)
    for entry in summary['devices']:
        if entry['error']:
            print(f"  {entry['name']}：{entry['error']}", file=sys.stderr)
    return 0 if accepted else 1


if __name__ == '__main__':
    raise SystemExit(main())
