#!/usr/bin/env python3
"""Fail-closed preflight for the official 声间 compile/sign entry.

Checks a dirty worktree (tracked + untracked non-ignored) and that HEAD is
exactly the bound tip of the declared safe branch on the named remote.
Missing remotes, unpushed HEAD, and fake SHAs reject. This module does not
claim a gate by itself; shengjian_sign_iphoneos.py must call it before any
xcodebuild or package write.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SAFE_BRANCH = 'fix/shengjian-unified-backend-e-r-u-20261120-sanitized'
FULL_SHA = re.compile(r'^[0-9a-f]{40}$')
SHORT_SHA = re.compile(r'^[0-9a-f]{7,40}$')


class BuildGateError(RuntimeError):
    pass


def run_git(args, *, cwd, runner=None):
    cmd = ['git', *args]
    if runner is not None:
        return runner(cmd, cwd)
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or '').strip() or f'git {" ".join(args)} failed'
        raise BuildGateError(err)
    return result.stdout


def require_hex_sha(value, *, label='SHA'):
    if not isinstance(value, str) or not FULL_SHA.fullmatch(value.strip().lower()):
        raise BuildGateError(f'{label} 无效或缺失，拒绝签包')
    return value.strip().lower()


def require_short_sha(value, *, label='GIT_SHORT_SHA'):
    if not isinstance(value, str) or not SHORT_SHA.fullmatch(value.strip().lower()):
        raise BuildGateError(f'{label} 无效或缺失，拒绝签包')
    return value.strip().lower()


def dirty_entries(cwd, *, runner=None):
    """Tracked changes and untracked non-ignored paths. Ignored files omitted."""
    out = run_git(['status', '--porcelain=v1', '-uall', '--untracked-files=all'], cwd=cwd, runner=runner)
    entries = []
    for line in out.splitlines():
        if not line or len(line) < 3:
            continue
        # Ignored lines would be '!! ' only with --ignored; we do not pass it.
        path = line[3:]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        entries.append((line[:2], path))
    return entries


def require_clean_worktree(cwd, *, runner=None):
    entries = dirty_entries(cwd, runner=runner)
    if entries:
        preview = ', '.join(f'{code} {path}' for code, path in entries[:12])
        more = '' if len(entries) <= 12 else f' …共{len(entries)}项'
        raise BuildGateError(f'工作区不干净（含未跟踪非 ignored），拒绝签包: {preview}{more}')
    return True


def head_full_sha(cwd, *, runner=None):
    sha = run_git(['rev-parse', 'HEAD'], cwd=cwd, runner=runner).strip().lower()
    return require_hex_sha(sha, label='HEAD')


def head_short_sha(cwd, *, runner=None, length=12):
    full = head_full_sha(cwd, runner=runner)
    return full[:length]


def require_on_safe_branch(cwd, *, branch=SAFE_BRANCH, runner=None):
    current = run_git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=cwd, runner=runner).strip()
    if current != branch:
        raise BuildGateError(f'HEAD 不在安全分支 {branch}（当前 {current}），拒绝签包')
    return current


def require_remote_bound_tip(cwd, *, branch=SAFE_BRANCH, remote='origin', expected_sha=None, runner=None):
    require_on_safe_branch(cwd, branch=branch, runner=runner)
    head = head_full_sha(cwd, runner=runner)
    if expected_sha is not None:
        want = require_hex_sha(expected_sha, label='--expected-sha')
        if want != head:
            raise BuildGateError(f'HEAD {head} 与要求的 SHA {want} 不符，拒绝签包')
    # Fail closed if the remote is missing.
    remotes = [line.split()[0] for line in run_git(['remote'], cwd=cwd, runner=runner).splitlines() if line.strip()]
    if remote not in remotes:
        raise BuildGateError(f'缺少 remote {remote}，拒绝签包')
    # Ask the live remote, not only the stale local tracking ref.
    ls = run_git(['ls-remote', '--heads', remote, f'refs/heads/{branch}'], cwd=cwd, runner=runner)
    remote_sha = None
    for line in ls.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in (f'refs/heads/{branch}', branch):
            remote_sha = parts[0].strip().lower()
            break
    if not remote_sha:
        raise BuildGateError(f'remote {remote} 上不存在分支 {branch}，拒绝签包')
    require_hex_sha(remote_sha, label=f'{remote}/{branch}')
    if remote_sha != head:
        raise BuildGateError(
            f'HEAD {head} 未推送到 {remote}/{branch}（远端 tip {remote_sha}），拒绝签包'
        )
    return {'head': head, 'branch': branch, 'remote': remote, 'remote_sha': remote_sha}


def require_build_ready(cwd, *, branch=SAFE_BRANCH, remote='origin', expected_sha=None, runner=None):
    require_clean_worktree(cwd, runner=runner)
    return require_remote_bound_tip(
        cwd, branch=branch, remote=remote, expected_sha=expected_sha, runner=runner,
    )


if __name__ == '__main__':
    import argparse
    import sys
    p = argparse.ArgumentParser(description='Fail-closed git/worktree gate for 声间签包')
    p.add_argument('--cwd', default='.')
    p.add_argument('--branch', default=SAFE_BRANCH)
    p.add_argument('--remote', default='origin')
    p.add_argument('--expected-sha', default=None)
    args = p.parse_args()
    try:
        info = require_build_ready(Path(args.cwd), branch=args.branch, remote=args.remote, expected_sha=args.expected_sha)
    except BuildGateError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(2)
    print(f"OK {info['branch']} {info['head']} == {info['remote']}/{info['branch']}")
