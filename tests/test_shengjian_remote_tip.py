from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from scripts.shengjian_remote_tip import RemoteTipError, require_claim_ready, require_remote_tip


WANTED = "ab" * 20
OTHER = "cd" * 20


class RemoteTipTests(unittest.TestCase):
    def test_unadvertised_tip_is_rejected(self):
        runner = Mock(return_value=subprocess.CompletedProcess(
            [], 0, f"{OTHER}\trefs/heads/main\n", ""))
        with self.assertRaisesRegex(RemoteTipError, "未公布指定 tip"):
            require_remote_tip(WANTED, runner=runner)
        runner.assert_called_once_with(
            ["git", "ls-remote", "--exit-code", "--", "origin"],
            capture_output=True, text=True, timeout=30)

    def test_advertised_sha_is_accepted(self):
        runner = Mock(return_value=subprocess.CompletedProcess(
            [], 0, f"{OTHER}\tHEAD\n{WANTED}\trefs/heads/delivery\n", ""))
        self.assertEqual(require_remote_tip(WANTED.upper(), runner=runner), WANTED)

    def test_branch_requires_exact_remote_ref_and_sha(self):
        ref = "refs/heads/release/delivery"
        runner = Mock(return_value=subprocess.CompletedProcess(
            [], 0, f"{WANTED}\trefs/tags/release/delivery\n{OTHER}\t{ref}\n", ""))
        tip = f"release/delivery@{WANTED}"
        with self.assertRaises(RemoteTipError):
            require_remote_tip(tip, remote="upstream", runner=runner)
        runner.assert_called_once_with(
            ["git", "ls-remote", "--exit-code", "--", "upstream", ref],
            capture_output=True, text=True, timeout=30)
        runner.return_value.stdout = f"{WANTED}\t{ref}\n"
        self.assertEqual(require_remote_tip(tip, remote="upstream", runner=runner), WANTED)

    def test_remote_failure_is_rejected(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 128, "", "synthetic failure"))
        with self.assertRaisesRegex(RemoteTipError, "未成功"):
            require_remote_tip(WANTED, runner=runner)

    def test_malformed_tips_are_rejected_without_runner(self):
        runner = Mock()
        for tip in (None, "", " ", "abc123", "g" * 40, "0" * 40, f"main@{'0' * 40}",
                    "main@", f"@{WANTED}", f"bad branch@{WANTED}", f"{WANTED}\n"):
            with self.subTest(tip=tip), self.assertRaises(RemoteTipError):
                require_remote_tip(tip, runner=runner)
        runner.assert_not_called()


class ClaimReadyTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.dist = Path(work.name)
        self.record = self.dist / "CURRENT-iphoneos.sha.txt"
        self.record.write_text(f"delivery@{WANTED} ignored local fields\n", encoding="utf-8")
        self.current = self.dist / "CURRENT-iphoneos"
        self.binary = self.point_current_at(WANTED)
        self.runner = Mock(return_value=subprocess.CompletedProcess(
            [], 0, f"{WANTED}\trefs/heads/delivery\n{OTHER}\trefs/heads/unrelated\n", ""))

    def point_current_at(self, revision):
        self.current.unlink(missing_ok=True)
        app = self.dist / f"PrivateListen-fixture-{revision}-iphoneos.app"
        app.mkdir()
        (app / "Info.plist").touch()
        binary = app / "PrivateListen.debug.dylib"
        binary.write_text("继续播放 听后感 mock_not_official", encoding="utf-8")
        self.current.symlink_to(app.name)
        return binary

    def test_missing_malformed_zero_or_unrelated_tip_fails_before_remote(self):
        for tip in ("", "not-a-sha", "0" * 40, "0" * 7, "fe" * 20, OTHER,
                    f"unrelated@{WANTED}"):
            with self.subTest(tip=tip), self.assertRaises(RemoteTipError):
                require_claim_ready(tip, self.record, runner=self.runner)
        self.runner.assert_not_called()

    def test_record_must_exist_and_name_the_build_branch(self):
        self.record.unlink()
        with self.assertRaisesRegex(RemoteTipError, "无法读取.*构建源记录"):
            require_claim_ready(WANTED, self.record, runner=self.runner)
        self.record.write_text(WANTED, encoding="utf-8")
        with self.assertRaisesRegex(RemoteTipError, "必须以 branch@SHA 开头"):
            require_claim_ready(WANTED, self.record, runner=self.runner)
        self.runner.assert_not_called()

    def test_build_ref_must_advertise_the_exact_sha(self):
        for stdout in (f"{WANTED}\trefs/heads/unrelated\n", f"{OTHER}\trefs/heads/delivery\n"):
            self.runner.return_value.stdout = stdout
            with self.subTest(stdout=stdout), self.assertRaisesRegex(RemoteTipError, "未公布指定 tip"):
                require_claim_ready(WANTED, self.record, runner=self.runner)

    def test_current_must_resolve_to_the_bound_package_with_a_passing_binary(self):
        self.binary.unlink()
        with self.assertRaisesRegex(RemoteTipError, "无法读取 CURRENT-iphoneos App 或包内二进制"):
            require_claim_ready(WANTED, self.record, runner=self.runner)
        self.binary.write_text("继续播放", encoding="utf-8")
        with self.assertRaisesRegex(RemoteTipError, "二进制缺少"):
            require_claim_ready(WANTED, self.record, runner=self.runner)
        self.point_current_at(OTHER)
        with self.assertRaisesRegex(RemoteTipError, "包名无法对应构建源"):
            require_claim_ready(WANTED, self.record, runner=self.runner)
        self.current.unlink()
        with self.assertRaisesRegex(RemoteTipError, "无法读取 CURRENT-iphoneos App"):
            require_claim_ready(WANTED, self.record, runner=self.runner)

    def test_matching_record_ref_and_package_pass(self):
        self.assertEqual(require_claim_ready(WANTED, self.record, runner=self.runner), WANTED)
        self.runner.assert_called_once_with(
            ["git", "ls-remote", "--exit-code", "--", "origin", "refs/heads/delivery"],
            capture_output=True, text=True, timeout=30)

    def test_short_record_tip_and_package_require_unique_local_object(self):
        short = WANTED[:7]
        self.record.write_text(f"delivery@{short} (fixture build)\n", encoding="utf-8")
        self.point_current_at(short)
        collision = short + "1" * (40 - len(short))
        self.runner.side_effect = [subprocess.CompletedProcess([], 0, f"{WANTED}\n{collision}\n", "")]
        with self.assertRaisesRegex(RemoteTipError, "短前缀必须.*唯一"):
            require_claim_ready(short, self.record, runner=self.runner)
        resolved = subprocess.CompletedProcess([], 0, f"{WANTED}\n", "")
        self.runner.side_effect = [resolved, resolved, self.runner.return_value, resolved]
        self.assertEqual(require_claim_ready(short, self.record, runner=self.runner), WANTED)
