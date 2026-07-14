#!/usr/bin/env python3
"""Public-repository portability and privacy checks."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicPackageTests(unittest.TestCase):
    def test_required_public_files_exist(self) -> None:
        for relative_path in [
            "README.md",
            "LICENSE",
            ".gitignore",
            "install.sh",
            "Makefile",
            "skills/podcast-translate/SKILL.md",
        ]:
            with self.subTest(relative_path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_public_files_contain_no_personal_absolute_paths(self) -> None:
        public_suffixes = {".md", ".py", ".sh", ".txt"}
        forbidden = [
            "".join(["/Users/", "liuyuxin"]),
            "".join(["健康", "知识库"]),
        ]
        for path in ROOT.rglob("*"):
            if (
                not path.is_file()
                or path.suffix not in public_suffixes
                or ".git" in path.parts
                or ".venv" in path.parts
            ):
                continue
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(path=path.relative_to(ROOT), value=value):
                    self.assertNotIn(value, text)

    def test_package_contains_no_literal_api_key(self) -> None:
        secret_pattern = re.compile(r"s[k]-[A-Za-z0-9]{20,}")
        for path in ROOT.rglob("*"):
            if (
                path.is_file()
                and path.suffix in {".md", ".py", ".sh", ".txt"}
                and ".git" not in path.parts
                and ".venv" not in path.parts
            ):
                self.assertIsNone(
                    secret_pattern.search(path.read_text(encoding="utf-8")),
                    path.relative_to(ROOT),
                )

    def test_installer_creates_runtime_and_installs_skill(self) -> None:
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("python3 -m venv", installer)
        self.assertIn("requirements-podcast.txt", installer)
        self.assertIn('(\".codex\", \".claude\")', installer)
        self.assertIn('"skills/podcast-translate"', installer)
        self.assertIn("PODCAST_TRANSLATE_ROOT", installer)

    def test_runtime_requirements_include_verified_yt_dlp(self) -> None:
        requirements = (ROOT / "requirements-podcast.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("yt-dlp==2026.3.17", requirements)

    def test_readme_documents_both_translation_providers(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("DeepSeek", readme)
        self.assertIn("通义千问", readme)
        self.assertIn("--provider qwen", readme)
        self.assertIn("macOS", readme)


if __name__ == "__main__":
    unittest.main()
