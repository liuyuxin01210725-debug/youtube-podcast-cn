#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from shengjian_build_gate import (
    SAFE_BRANCH,
    BuildGateError,
    dirty_entries,
    require_build_ready,
    require_clean_worktree,
    require_hex_sha,
    require_remote_bound_tip,
)


def git(cwd, *args, check=True):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def init_repo(path, branch=SAFE_BRANCH):
    git(path, 'init', '-q')
    git(path, 'config', 'user.email', 'gate-test@example.com')
    git(path, 'config', 'user.name', 'Gate Test')
    git(path, 'checkout', '-q', '-b', branch)
    (path / 'README').write_text('ok\n', encoding='utf-8')
    git(path, 'add', 'README')
    git(path, 'commit', '-q', '-m', 'init')
    return git(path, 'rev-parse', 'HEAD').stdout.strip()


class BuildGateTests(unittest.TestCase):
    def test_dirty_tracked_and_untracked_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            init_repo(repo)
            (repo / 'README').write_text('changed\n', encoding='utf-8')
            with self.assertRaises(BuildGateError) as ctx:
                require_clean_worktree(repo)
            self.assertIn('不干净', str(ctx.exception))
            git(repo, 'checkout', '--', 'README')
            (repo / 'untracked.txt').write_text('new\n', encoding='utf-8')
            entries = dirty_entries(repo)
            self.assertTrue(any(path == 'untracked.txt' for _, path in entries))
            with self.assertRaises(BuildGateError):
                require_clean_worktree(repo)
            (repo / 'untracked.txt').unlink()
            (repo / '.gitignore').write_text('ignored.log\n', encoding='utf-8')
            git(repo, 'add', '.gitignore')
            git(repo, 'commit', '-q', '-m', 'ignore')
            (repo / 'ignored.log').write_text('no\n', encoding='utf-8')
            # ignored files must not fail the gate
            require_clean_worktree(repo)

    def test_fake_sha_and_missing_remote_and_unpushed_reject(self):
        with self.assertRaises(BuildGateError):
            require_hex_sha('not-a-sha')
        with self.assertRaises(BuildGateError):
            require_hex_sha('')
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            head = init_repo(repo)
            with self.assertRaises(BuildGateError) as ctx:
                require_remote_bound_tip(repo, expected_sha=head)
            self.assertIn('缺少 remote', str(ctx.exception))
            remote = Path(raw).parent / (Path(raw).name + '-remote.git')
            git(repo, 'clone', '--bare', '-q', str(repo), str(remote))
            git(repo, 'remote', 'add', 'origin', str(remote))
            # Unpushed extra commit
            (repo / 'README').write_text('ahead\n', encoding='utf-8')
            git(repo, 'add', 'README')
            git(repo, 'commit', '-q', '-m', 'local only')
            new_head = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            with self.assertRaises(BuildGateError) as ctx:
                require_build_ready(repo)
            self.assertIn('未推送', str(ctx.exception))
            with self.assertRaises(BuildGateError):
                require_build_ready(repo, expected_sha='0' * 40)
            git(repo, 'push', '-q', 'origin', f'HEAD:refs/heads/{SAFE_BRANCH}')
            info = require_build_ready(repo, expected_sha=new_head)
            self.assertEqual(info['head'], new_head)
            self.assertEqual(info['remote_sha'], new_head)

    def test_wrong_branch_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            init_repo(repo, branch='main')
            with self.assertRaises(BuildGateError) as ctx:
                require_remote_bound_tip(repo)
            self.assertIn('不在安全分支', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
