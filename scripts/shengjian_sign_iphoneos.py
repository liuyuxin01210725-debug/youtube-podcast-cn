#!/usr/bin/env python3
"""Canonical compile/sign-package entry for 声间 (iphoneos).

Fail-closed: dirty tree (tracked + untracked non-ignored), missing params,
fake SHA, missing remote, unpushed HEAD, or a signed product that does not
match 声间 / the bound SHA / the wall-clock BUILD_TIMESTAMP all reject.
BUILD_TIMESTAMP is the actual build wall clock (Asia/Shanghai), never the
commit AuthorDate. GIT_SHORT_SHA + BUILD_TIMESTAMP are injected only into
the product via xcodebuild settings; committed Info.plist keeps placeholders.

This entry never updates dist/CURRENT. A later promote path may do so only
after a real official install and matching verification. Signing uses only
already-installed identities; it does not download profiles, create an App
ID, or pass -allowProvisioningUpdates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shengjian_build_gate import (
    SAFE_BRANCH,
    BuildGateError,
    require_build_ready,
    require_hex_sha,
    require_short_sha,
)

REPO_ROOT_MARKER = ('ios', 'PrivateListen', 'PrivateListen.xcodeproj')
BUNDLE_ID = 'com.liuyuxin.privatelisten'
DISPLAY_NAME = '声间'
SCHEME = 'PrivateListen'
TZ = ZoneInfo('Asia/Shanghai')


class SignError(RuntimeError):
    pass


def wall_clock_timestamp(now=None):
    when = now or datetime.now(TZ)
    if when.tzinfo is None:
        when = when.replace(tzinfo=TZ)
    when = when.astimezone(TZ)
    # Explicit zone label so this is never confused with Git AuthorDate.
    return when.strftime('%Y-%m-%d %H:%M:%S %z') + ' Asia/Shanghai'


def project_path(repo):
    path = Path(repo) / 'ios' / 'PrivateListen' / 'PrivateListen.xcodeproj'
    if not path.is_dir():
        raise SignError(f'找不到工程 {path}')
    return path


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_info_plist(app_dir):
    plist = Path(app_dir) / 'Info.plist'
    if not plist.is_file():
        raise SignError('签包缺少 Info.plist，拒绝')
    with open(plist, 'rb') as handle:
        return plistlib.load(handle)


def require_product_matches(app_dir, *, full_sha, short_sha, build_timestamp):
    info = read_info_plist(app_dir)
    bundle = info.get('CFBundleIdentifier')
    name = info.get('CFBundleDisplayName') or info.get('CFBundleName')
    got_short = str(info.get('GIT_SHORT_SHA') or '').strip()
    got_ts = str(info.get('BUILD_TIMESTAMP') or '').strip()
    if bundle != BUNDLE_ID:
        raise SignError(f'签包产品标识不符: {bundle}')
    if name != DISPLAY_NAME:
        raise SignError(f'签包产品名不符: {name}')
    if not got_short or got_short in ('$(GIT_SHORT_SHA)', 'UNKNOWN', 'unknown', ''):
        raise SignError('签包缺少 GIT_SHORT_SHA，不得伪 ready')
    if not got_ts or got_ts in ('$(BUILD_TIMESTAMP)', 'UNKNOWN', 'unknown', '') or 'AuthorDate' in got_ts:
        raise SignError('签包缺少真实 BUILD_TIMESTAMP，不得伪 ready')
    require_short_sha(got_short)
    if not full_sha.startswith(got_short) and got_short != full_sha[:len(got_short)]:
        raise SignError(f'签包 GIT_SHORT_SHA {got_short} 与源 {full_sha} 不符')
    if got_short != short_sha:
        raise SignError(f'签包 GIT_SHORT_SHA {got_short} 与注入 {short_sha} 不符')
    if got_ts != build_timestamp:
        raise SignError(f'签包 BUILD_TIMESTAMP 与本次构建墙钟不符（不得用 Git AuthorDate）')
    executable = info.get('CFBundleExecutable') or 'PrivateListen'
    binary = Path(app_dir) / executable
    if not binary.is_file():
        raise SignError('签包缺少可执行文件，拒绝')
    return {
        'bundle_id': bundle,
        'display_name': name,
        'git_short_sha': got_short,
        'build_timestamp': got_ts,
        'binary_sha256': sha256_file(binary),
    }


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def default_derived(output):
    return Path(output) / 'DerivedData'


def locate_app(derived):
    matches = list(Path(derived).glob('**/Build/Products/Debug-iphoneos/PrivateListen.app'))
    if not matches:
        matches = list(Path(derived).glob('**/PrivateListen.app'))
    apps = [p for p in matches if p.is_dir() and (p / 'Info.plist').is_file()]
    if not apps:
        raise SignError('xcodebuild 未产出 PrivateListen.app')
    if len(apps) > 1:
        # Prefer the canonical products path.
        preferred = [p for p in apps if 'Debug-iphoneos' in str(p)]
        apps = preferred or apps
    return apps[0]


def run_xcodebuild(*, repo, derived, full_sha, short_sha, build_timestamp, extra_env=None):
    cmd = [
        'xcodebuild',
        '-project', str(project_path(repo)),
        '-scheme', SCHEME,
        '-configuration', 'Debug',
        '-destination', 'generic/platform=iOS',
        '-derivedDataPath', str(derived),
        'CODE_SIGN_STYLE=Automatic',
        'CODE_SIGN_IDENTITY=Apple Development',
        f'GIT_SHORT_SHA={short_sha}',
        f'BUILD_TIMESTAMP={build_timestamp}',
        'INFOPLIST_EXPAND_BUILD_SETTINGS=YES',
        'ONLY_ACTIVE_ARCH=NO',
    ]
    # Existing team only, if the operator already has it in the environment.
    # Never invent a team, never -allowProvisioningUpdates, never download a profile.
    team = os.environ.get('SHENGJIAN_DEVELOPMENT_TEAM', '').strip()
    if team:
        cmd.append(f'DEVELOPMENT_TEAM={team}')
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    # Refuse profile auto-download even if someone exported the flag.
    cmd = [part for part in cmd if 'allowProvisioningUpdates' not in part]
    result = subprocess.run(cmd, cwd=repo, env=env, check=False)
    if result.returncode != 0:
        raise SignError(f'xcodebuild 失败，退出码 {result.returncode}')
    return locate_app(derived)


def package_app(app, dest_app):
    if dest_app.exists():
        shutil.rmtree(dest_app)
    dest_app.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(app, dest_app, symlinks=False)
    return dest_app


def sign_iphoneos(
    repo,
    output,
    *,
    branch=SAFE_BRANCH,
    remote='origin',
    expected_sha=None,
    runner=None,
    now=None,
    xcodebuild_fn=None,
    skip_xcodebuild=False,
):
    output = Path(output)
    if output.resolve() == Path(repo).resolve():
        raise SignError('--output 不能是仓库根')
    # Never write into the live CURRENT pointer from this entry.
    if output.name == 'CURRENT-iphoneos' or output.name == 'CURRENT':
        raise SignError('签包入口不得直接写 CURRENT')
    try:
        tip = require_build_ready(
            repo, branch=branch, remote=remote, expected_sha=expected_sha, runner=runner,
        )
    except BuildGateError as exc:
        raise SignError(str(exc)) from exc
    full_sha = tip['head']
    short_sha = full_sha[:12]
    build_timestamp = wall_clock_timestamp(now)
    derived = default_derived(output)
    derived.mkdir(parents=True, exist_ok=True)
    if skip_xcodebuild and xcodebuild_fn is None:
        raise SignError('缺参数：测试替身未提供，拒绝签包')
    builder = xcodebuild_fn or run_xcodebuild
    app = builder(
        repo=repo, derived=derived, full_sha=full_sha,
        short_sha=short_sha, build_timestamp=build_timestamp,
    )
    product = require_product_matches(
        app, full_sha=full_sha, short_sha=short_sha, build_timestamp=build_timestamp,
    )
    dest_app = output / f'PrivateListen-{short_sha}-iphoneos.app'
    package_app(app, dest_app)
    package_hash = sha256_file(dest_app / (read_info_plist(dest_app).get('CFBundleExecutable') or 'PrivateListen'))
    # Also hash a zip-less tree manifest of regular files (no secrets assumed in the app bundle).
    tree_hasher = hashlib.sha256()
    for file in sorted(p for p in dest_app.rglob('*') if p.is_file() and not p.is_symlink()):
        rel = file.relative_to(dest_app).as_posix()
        tree_hasher.update(rel.encode('utf-8'))
        tree_hasher.update(b'\0')
        tree_hasher.update(sha256_file(file).encode('ascii'))
        tree_hasher.update(b'\n')
    record = {
        'product': DISPLAY_NAME,
        'bundle_id': BUNDLE_ID,
        'branch': tip['branch'],
        'remote': tip['remote'],
        'source_full_sha': full_sha,
        'git_short_sha': short_sha,
        'build_timestamp': build_timestamp,
        'build_timestamp_zone': 'Asia/Shanghai',
        'build_timestamp_is_git_authordate': False,
        'package_path': str(dest_app),
        'package_binary_sha256': package_hash,
        'package_tree_sha256': tree_hasher.hexdigest(),
        'current_updated': False,
        'ready': True,
        'note': 'CURRENT 未更新：本入口只签包，不安装、不提升 CURRENT',
        **{k: product[k] for k in ('display_name', 'binary_sha256')},
    }
    if not record['source_full_sha'] or not record['package_binary_sha256'] or not record['build_timestamp']:
        raise SignError('构建记录缺值，不得伪 ready')
    record_path = output / 'build-record.json'
    write_record(record_path, record)
    return record


def main(argv=None):
    p = argparse.ArgumentParser(description='声间官方 iphoneos 签包入口（不更新 CURRENT）')
    p.add_argument('--output', required=True, help='隔离输出目录，不得是 live CURRENT')
    p.add_argument('--cwd', default=None)
    p.add_argument('--branch', default=SAFE_BRANCH)
    p.add_argument('--remote', default='origin')
    p.add_argument('--expected-sha', default=None)
    args = p.parse_args(argv)
    if not args.output:
        print('ERROR: 缺参数 --output', file=sys.stderr)
        return 2
    repo = Path(args.cwd or Path.cwd()).resolve()
    try:
        record = sign_iphoneos(
            repo, args.output, branch=args.branch, remote=args.remote, expected_sha=args.expected_sha,
        )
    except (SignError, BuildGateError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
