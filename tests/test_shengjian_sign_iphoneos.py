#!/usr/bin/env python3
import json
import os
import shutil
import plistlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from shengjian_build_gate import SAFE_BRANCH
from shengjian_sign_iphoneos import (
    SignError,
    sign_iphoneos,
    wall_clock_timestamp,
)
from shengjian_current_promote import PromoteError, promote, read_current


TZ = ZoneInfo('Asia/Shanghai')


def git(cwd, *args):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def init_ready_repo(path):
    git(path, 'init', '-q')
    git(path, 'config', 'user.email', 'sign-test@example.com')
    git(path, 'config', 'user.name', 'Sign Test')
    git(path, 'checkout', '-q', '-b', SAFE_BRANCH)
    (path / 'README').write_text('ok\n', encoding='utf-8')
    git(path, 'add', 'README')
    git(path, 'commit', '-q', '-m', 'init')
    remote = path.parent / (path.name + '-remote.git')
    git(path, 'clone', '--bare', '-q', str(path), str(remote))
    git(path, 'remote', 'add', 'origin', str(remote))
    git(path, 'push', '-q', 'origin', f'HEAD:refs/heads/{SAFE_BRANCH}')
    return git(path, 'rev-parse', 'HEAD').stdout.strip()


def write_fake_app(directory, *, short_sha, build_timestamp, bundle='com.liuyuxin.privatelisten', name='声间'):
    app = Path(directory) / 'Build' / 'Products' / 'Debug-iphoneos' / 'PrivateListen.app'
    if app.exists():
        shutil.rmtree(app)
    app.mkdir(parents=True)
    info = {
        'CFBundleIdentifier': bundle,
        'CFBundleDisplayName': name,
        'CFBundleExecutable': 'PrivateListen',
        'GIT_SHORT_SHA': short_sha,
        'BUILD_TIMESTAMP': build_timestamp,
    }
    with open(app / 'Info.plist', 'wb') as handle:
        plistlib.dump(info, handle)
    (app / 'PrivateListen').write_bytes(b'fake-binary-' + short_sha.encode())
    os.chmod(app / 'PrivateListen', 0o755)
    return app


class SignEntryTests(unittest.TestCase):
    def test_wall_clock_is_not_author_date(self):
        when = datetime(2026, 11, 22, 20, 40, 15, tzinfo=TZ)
        stamp = wall_clock_timestamp(when)
        self.assertIn('2026-11-22 20:40:15', stamp)
        self.assertIn('Asia/Shanghai', stamp)
        self.assertNotIn('AuthorDate', stamp)

    def test_dirty_tree_rejects_before_xcode(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / 'repo'
            repo.mkdir()
            init_ready_repo(repo)
            (repo / 'dirt.txt').write_text('no\n', encoding='utf-8')
            called = {'n': 0}

            def builder(**kwargs):
                called['n'] += 1
                raise AssertionError('xcodebuild must not run on a dirty tree')

            with self.assertRaises(SignError) as ctx:
                sign_iphoneos(repo, Path(raw) / 'out', xcodebuild_fn=builder)
            self.assertIn('不干净', str(ctx.exception))
            self.assertEqual(called['n'], 0)

    def test_missing_params_fake_sha_and_product_mismatch_reject(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / 'repo'
            repo.mkdir()
            head = init_ready_repo(repo)
            out = Path(raw) / 'out'
            with self.assertRaises(SignError):
                sign_iphoneos(repo, out, expected_sha='deadbeef', xcodebuild_fn=lambda **k: None)
            with self.assertRaises(SignError):
                sign_iphoneos(repo, out, expected_sha='0' * 40, xcodebuild_fn=lambda **k: None)
            with self.assertRaises(SignError):
                sign_iphoneos(repo, out, skip_xcodebuild=True)
            when = datetime(2026, 11, 22, 21, 0, 0, tzinfo=TZ)

            def bad_product(*, repo, derived, full_sha, short_sha, build_timestamp):
                return write_fake_app(derived, short_sha=short_sha, build_timestamp=build_timestamp, bundle='com.example.wrong')

            with self.assertRaises(SignError) as ctx:
                sign_iphoneos(repo, out, expected_sha=head, now=when, xcodebuild_fn=bad_product)
            self.assertIn('产品标识不符', str(ctx.exception))

            def missing_stamp(*, repo, derived, full_sha, short_sha, build_timestamp):
                return write_fake_app(derived, short_sha=short_sha, build_timestamp='$(BUILD_TIMESTAMP)')

            with self.assertRaises(SignError) as ctx:
                sign_iphoneos(repo, out, expected_sha=head, now=when, xcodebuild_fn=missing_stamp)
            self.assertIn('BUILD_TIMESTAMP', str(ctx.exception))

    def test_current_output_rejected_and_success_does_not_touch_current(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / 'repo'
            repo.mkdir()
            head = init_ready_repo(repo)
            live = Path(raw) / 'dist'
            live.mkdir()
            (live / 'CURRENT-iphoneos').symlink_to(live / 'old-package')
            with self.assertRaises(SignError):
                sign_iphoneos(repo, live / 'CURRENT-iphoneos', expected_sha=head, xcodebuild_fn=lambda **k: None)
            when = datetime(2026, 11, 22, 21, 11, 11, tzinfo=TZ)
            isolated = Path(raw) / 'isolated-out'

            def ok(*, repo, derived, full_sha, short_sha, build_timestamp):
                self.assertEqual(short_sha, head[:12])
                self.assertIn('Asia/Shanghai', build_timestamp)
                self.assertEqual(build_timestamp, wall_clock_timestamp(when))
                return write_fake_app(derived, short_sha=short_sha, build_timestamp=build_timestamp)

            record = sign_iphoneos(
                repo, isolated, expected_sha=head, now=when, xcodebuild_fn=ok,
            )
            self.assertTrue(record['ready'])
            self.assertFalse(record['current_updated'])
            self.assertEqual(record['source_full_sha'], head)
            self.assertEqual(record['build_timestamp'], wall_clock_timestamp(when))
            self.assertFalse(record['build_timestamp_is_git_authordate'])
            self.assertTrue((isolated / 'build-record.json').is_file())
            # Live CURRENT untouched.
            self.assertEqual(os.path.realpath(live / 'CURRENT-iphoneos'), os.path.realpath(live / 'old-package'))


class IsolatedCurrentTests(unittest.TestCase):
    def _record(self, folder, package):
        rec = folder / 'build-record.json'
        rec.write_text(json.dumps({
            'source_full_sha': 'a' * 40,
            'package_binary_sha256': 'b' * 64,
            'build_timestamp': '2026-11-22 21:11:11 +0800 Asia/Shanghai',
            'build_timestamp_is_git_authordate': False,
            'package_path': str(package),
            'git_short_sha': 'a' * 12,
        }), encoding='utf-8')
        return rec

    def test_promote_requires_real_install_and_keeps_old_on_fault(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / 'old.app'
            new = root / 'new.app'
            old.mkdir(); new.mkdir()
            current_root = root / 'dist'
            current_root.mkdir()
            (current_root / 'CURRENT-iphoneos').symlink_to(old)
            record = self._record(root, new)
            with self.assertRaises(PromoteError):
                promote(current_root=current_root, record_path=record, install_succeeded=False, verified=False)
            self.assertEqual(read_current(current_root), str(old.resolve()))
            with self.assertRaises(PromoteError):
                promote(current_root=current_root, record_path=record, install_succeeded=True, verified=False)
            self.assertEqual(read_current(current_root), str(old.resolve()))
            # Sign success is not enough; missing verification keeps CURRENT.
            with self.assertRaises(PromoteError):
                promote(current_root=current_root, record_path=record, install_succeeded=False, verified=True)
            self.assertEqual(read_current(current_root), str(old.resolve()))
            result = promote(
                current_root=current_root, record_path=record,
                install_succeeded=True, verified=True,
            )
            self.assertEqual(result['current'], str(new.resolve()))
            self.assertEqual(read_current(current_root), str(new.resolve()))

    def test_uninstall_restore_refused_and_missing_record_fields_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = root / 'pkg.app'
            package.mkdir()
            current_root = root / 'dist'
            current_root.mkdir()
            old = root / 'old.app'; old.mkdir()
            (current_root / 'CURRENT-iphoneos').symlink_to(old)
            record = self._record(root, package)
            with self.assertRaises(PromoteError) as ctx:
                promote(
                    current_root=current_root, record_path=record,
                    install_succeeded=True, verified=True, allow_uninstall_restore=True,
                )
            self.assertIn('卸载', str(ctx.exception))
            self.assertEqual(read_current(current_root), str(old.resolve()))
            bad = root / 'bad.json'
            bad.write_text(json.dumps({'package_path': str(package)}), encoding='utf-8')
            with self.assertRaises(PromoteError):
                promote(current_root=current_root, record_path=bad, install_succeeded=True, verified=True)
            self.assertEqual(read_current(current_root), str(old.resolve()))


if __name__ == '__main__':
    unittest.main()
