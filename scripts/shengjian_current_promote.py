#!/usr/bin/env python3
"""Atomically update CURRENT only after a real official install + verification.

This is not the sign-package entry. A successful sign must not call this.
This round of work does not install on a device; live CURRENT must stay
untouched. Tests use an isolated CURRENT root and fault injection.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


class PromoteError(RuntimeError):
    pass


def read_record(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromoteError(f'构建记录不可读: {exc}') from exc
    for key in ('source_full_sha', 'package_binary_sha256', 'build_timestamp', 'package_path', 'git_short_sha'):
        if not data.get(key):
            raise PromoteError(f'构建记录缺 {key}，不得伪 ready、不得提升 CURRENT')
    if data.get('build_timestamp_is_git_authordate'):
        raise PromoteError('构建时间是 Git AuthorDate，拒绝提升 CURRENT')
    return data


def current_link(current_root):
    return Path(current_root) / 'CURRENT-iphoneos'


def read_current(current_root):
    link = current_link(current_root)
    if not link.exists() and not link.is_symlink():
        return None
    return os.path.realpath(link)


def atomic_replace_symlink(link, target):
    link = Path(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.CURRENT.', dir=str(link.parent))
    os.close(fd)
    tmp_path = Path(tmp)
    tmp_path.unlink()
    os.symlink(str(Path(target).resolve()), tmp_path)
    os.replace(tmp_path, link)


def promote(
    *,
    current_root,
    record_path,
    package_path=None,
    install_succeeded=False,
    verified=False,
    allow_uninstall_restore=False,
):
    if allow_uninstall_restore:
        raise PromoteError('卸载 App 会删除 Documents/objects，拒绝以轻量快照承诺全恢复，CURRENT 不更新')
    if not install_succeeded or not verified:
        raise PromoteError('官方安装未成功或校验未通过，CURRENT 保持原值')
    record = read_record(record_path)
    package = Path(package_path or record['package_path']).resolve()
    if not package.is_dir():
        raise PromoteError('签包目录不存在，CURRENT 保持原值')
    recorded = Path(record['package_path']).resolve()
    if package != recorded:
        raise PromoteError('提升目标与构建记录中的包路径不符，CURRENT 保持原值')
    previous = read_current(current_root)
    try:
        atomic_replace_symlink(current_link(current_root), package)
    except OSError as exc:
        raise PromoteError(f'CURRENT 更新失败，已保持原值: {exc}') from exc
    return {'previous': previous, 'current': read_current(current_root), 'record': record}


def main(argv=None):
    p = argparse.ArgumentParser(description='仅在官方安装并校验成功后原子更新 CURRENT')
    p.add_argument('--current-root', required=True)
    p.add_argument('--record', required=True)
    p.add_argument('--package', default=None)
    p.add_argument('--install-succeeded', action='store_true')
    p.add_argument('--verified', action='store_true')
    p.add_argument('--allow-uninstall-restore', action='store_true')
    args = p.parse_args(argv)
    try:
        result = promote(
            current_root=args.current_root,
            record_path=args.record,
            package_path=args.package,
            install_succeeded=args.install_succeeded,
            verified=args.verified,
            allow_uninstall_restore=args.allow_uninstall_restore,
        )
    except PromoteError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
