import subprocess
import unittest
from unittest.mock import Mock

from scripts.shengjian_remote_tip import RemoteTipError, require_remote_tip


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
