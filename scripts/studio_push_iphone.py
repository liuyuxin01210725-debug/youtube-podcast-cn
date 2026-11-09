#!/usr/bin/env python3
"""Mirror a published episode into the PrivateListen app on a plugged-in iPhone.

iCloud Drive is the real path. This is the extra copy for a phone that is
connected to the Mac: `xcrun devicectl device copy to` writes into the app's
Documents/中文播客私人库, which the app reads next to the iCloud folder.
Every failure here is soft. It never changes the studio job or the iCloud
publish, and callers only get a summary dict back.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from studio_publish_icloud import (
    AUDIO_NAME,
    LIBRARY_NAME,
    _JOB_ID,
    atomic_write_json,
    default_library_root,
    read_json,
)

BUNDLE_ID = 'com.liuyuxin.privatelisten'
DEVICE_LIBRARY = f'Documents/{LIBRARY_NAME}'
STATUS_NAME = 'private-listen-device.json'
ENV_SWITCH = 'PRIVATE_LISTEN_DEVICE_PUSH'
_COVERS = ('cover.jpg', 'cover.png', 'cover.webp')
_LIST_TIMEOUT = 30
_SMALL_TIMEOUT = 90
_AUDIO_TIMEOUT = 900


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
    if switch in ('0', 'false', 'no', 'off'):
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


def episode_files(episode_dir: Path) -> list[tuple[Path, str]]:
    """(local file, path under the device library). Audio first, json last.

    The app only lists an episode once it can read episode.json or
    library.json, so writing those after the audio keeps a half-copied mp3
    out of the list.
    """
    episode_dir = Path(episode_dir)
    ident = episode_dir.name
    files: list[tuple[Path, str]] = []
    audio = episode_dir / AUDIO_NAME
    if audio.is_file() and not audio.is_symlink():
        files.append((audio, f'episodes/{ident}/{AUDIO_NAME}'))
    for cover in _COVERS:
        path = episode_dir / cover
        if path.is_file() and not path.is_symlink():
            files.append((path, f'episodes/{ident}/{cover}'))
    record = episode_dir / 'episode.json'
    if record.is_file() and not record.is_symlink():
        files.append((record, f'episodes/{ident}/episode.json'))
    return files


def copy_to_app(device_id: str, source: Path, destination: str, runner=_default_runner) -> str | None:
    """Returns None on success, else a short error."""
    timeout = _AUDIO_TIMEOUT if source.name == AUDIO_NAME else _SMALL_TIMEOUT
    code, stdout, stderr = runner([
        'xcrun', 'devicectl', 'device', 'copy', 'to',
        '--device', device_id,
        '--domain-type', 'appDataContainer',
        '--domain-identifier', BUNDLE_ID,
        '--source', str(source),
        '--destination', f'{DEVICE_LIBRARY}/{destination}',
    ], timeout)
    if code != 0:
        return _short_error(stderr or stdout, code)
    return None


def push_episodes(episode_dirs: list[Path], library: Path, *, runner=_default_runner) -> dict:
    """Copy episodes plus library.json to every reachable phone. Never raises."""
    summary = {'ok': False, 'devices': [], 'reason': None, 'episodes': [Path(p).name for p in episode_dirs]}
    plan: list[tuple[Path, str]] = []
    for episode_dir in episode_dirs:
        files = episode_files(episode_dir)
        if not any(path.name == AUDIO_NAME for path, _ in files):
            continue
        plan.extend(files)
    if not plan:
        summary['reason'] = '资料库里没有可拷贝的节目'
        return summary
    listing = Path(library) / 'library.json'
    if listing.is_file() and not listing.is_symlink():
        plan.append((listing, 'library.json'))
    if not devicectl_available():
        summary['reason'] = '这台机器没有 xcrun devicectl'
        return summary
    devices = reachable_devices(runner)
    if not devices:
        summary['reason'] = '没有连着的 iPhone'
        return summary
    for device in devices:
        entry = {'id': device['id'], 'name': device['name'], 'ok': False, 'error': None}
        summary['devices'].append(entry)
        if app_installed(device['id'], runner) is False:
            entry['error'] = '没有安装私人听'
            continue
        for source, destination in plan:
            error = copy_to_app(device['id'], source, destination, runner)
            if error:
                entry['error'] = f'{destination}：{error}'
                break
        else:
            entry['ok'] = True
    summary['ok'] = any(entry['ok'] for entry in summary['devices'])
    if not summary['ok'] and summary['reason'] is None:
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
    if summary.get('skipped'):
        return f"私人听：跳过 iPhone 直连同步（{summary.get('reason')}）。"
    return f"私人听：iPhone 直连同步没完成（{summary.get('reason') or '原因不明'}），iCloud 已发布，不影响任务。"


def push_after_publish(result: dict, job_dir: Path | None = None, *, runner=_default_runner, announce=print) -> dict:
    """Hook for engine / server / CLI after `publish_job` succeeded. Never raises."""
    try:
        library = Path(result.get('library') or default_library_root())
        episode_dir = Path(result.get('episode_dir') or (library / 'episodes' / str(result.get('id') or '')))
        if not push_enabled(library):
            return {'ok': False, 'skipped': True, 'reason': '资料库不是默认的 iCloud 位置，或已关闭', 'devices': []}
        summary = push_episodes([episode_dir], library, runner=runner)
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


def _library_episode_dirs(library: Path) -> list[Path]:
    root = Path(library) / 'episodes'
    if not root.is_dir():
        return []
    return sorted(
        (child for child in root.iterdir() if child.is_dir() and not child.is_symlink() and _JOB_ID.fullmatch(child.name)),
        key=lambda child: child.name,
    )


def main(argv: list[str] | None = None, *, runner=_default_runner) -> int:
    parser = argparse.ArgumentParser(description='把 iCloud 私人听资料库里的节目拷到连着 Mac 的 iPhone（私人听 App 的 Documents）')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--job-dir', type=Path, help='工作室任务目录；按目录名找资料库里的那一期')
    target.add_argument('--episode-id', help='资料库里的节目编号')
    target.add_argument('--all', action='store_true', help='资料库里的全部节目')
    parser.add_argument('--library', type=Path, default=None, help='资料库根目录，默认 iCloud Drive 里的「中文播客私人库」')
    args = parser.parse_args(argv)
    library = (args.library or default_library_root()).expanduser()
    if args.all:
        dirs = _library_episode_dirs(library)
    else:
        ident = args.job_dir.expanduser().name if args.job_dir else str(args.episode_id)
        if not _JOB_ID.fullmatch(ident):
            print('私人听：节目编号无效', file=sys.stderr)
            return 1
        dirs = [library / 'episodes' / ident]
    summary = push_episodes(dirs, library, runner=runner)
    if args.job_dir:
        _record(args.job_dir.expanduser(), summary)
    message = describe(summary)
    print(message, file=sys.stdout if summary['ok'] else sys.stderr)
    for entry in summary['devices']:
        if entry['error']:
            print(f"  {entry['name']}：{entry['error']}", file=sys.stderr)
    return 0 if summary['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
