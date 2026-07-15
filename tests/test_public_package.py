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
            "requirements-diarization.txt",
            "scripts/diarization_models.json",
            "bin/setup-podcast-diarization.sh",
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

    def test_installer_keeps_diarization_lazy_and_prints_recovery_command(self) -> None:
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("pip install -r \"$ROOT/requirements-diarization.txt\"", installer)
        self.assertIn("setup-podcast-diarization.sh", installer)
        self.assertIn("首次", installer)

    def test_runtime_requirements_include_verified_yt_dlp(self) -> None:
        requirements = (ROOT / "requirements-podcast.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("yt-dlp==2026.3.17", requirements)

    def test_gitignore_excludes_tts_cache_and_wave_fragments(self) -> None:
        ignored_patterns = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn(".tts-cache/", ignored_patterns)
        self.assertIn("*.wav", ignored_patterns)

    def test_gitignore_excludes_persistent_setup_locks(self) -> None:
        ignored_patterns = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn(".*.setup.lock", ignored_patterns)

    def test_makefile_runs_every_tts_test_module(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("tests.test_tts_cn", makefile)
        self.assertIn("tests.test_tts_backends", makefile)
        self.assertIn("tests.test_tts_cache", makefile)

    def test_tts_wrapper_loads_qwen_key_only_for_qwen_backend(self) -> None:
        wrapper = (ROOT / "bin/tts-cn.sh").read_text(encoding="utf-8")
        self.assertIn("qwen3-api", wrapper)
        self.assertIn("podcast-cn-qwen", wrapper)
        self.assertIn("DASHSCOPE_API_KEY", wrapper)
        self.assertIn("--dump-segments", wrapper)
        self.assertNotIn("sk-", wrapper)

    def test_tts_wrapper_install_hint_uses_complete_requirements(self) -> None:
        wrapper = (ROOT / "bin/tts-cn.sh").read_text(encoding="utf-8")
        self.assertIn("pip install -r requirements-podcast.txt", wrapper)
        self.assertNotIn("pip install edge-tts pydub audioop-lts", wrapper)

    def test_readme_documents_qwen_tts_without_changing_default(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("--tts qwen3-api", readme)
        self.assertIn("--tts edge", readme)
        self.assertIn("EdgeTTS 仍是默认", readme)
        self.assertIn(".tts-cache", readme)

    def test_readme_explains_billing_errors_and_key_safety(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("余额或额度不足", readme)
        self.assertIn("不要把 Key 粘贴到聊天", readme)

    def test_skill_distinguishes_translation_provider_from_tts_backend(self) -> None:
        skill = (ROOT / "skills/podcast-translate/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--provider qwen", skill)
        self.assertIn("--tts qwen3-api", skill)
        self.assertIn("千问翻译", skill)
        self.assertIn("千问语音", skill)

    def test_readme_documents_both_translation_providers(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("DeepSeek", readme)
        self.assertIn("通义千问", readme)
        self.assertIn("--provider qwen", readme)
        self.assertIn("macOS", readme)

    def test_readme_documents_local_diarization_and_cleanup(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("--diarize auto", readme)
        self.assertIn("--diarize off", readme)
        self.assertIn("--diarize required", readme)
        self.assertIn("--speaker-count 2", readme)
        self.assertIn("临时原音频", readme)
        self.assertIn("不会保留", readme)
        self.assertIn("setup-podcast-diarization.sh", readme)
        self.assertIn("sherpa-onnx", readme)
        self.assertIn("Apache-2.0", readme)
        self.assertIn("MIT", readme)

    def test_skill_uses_generated_speakers_or_explicit_fallback(self) -> None:
        skill = (ROOT / "skills/podcast-translate/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("说话人序列:", skill)
        self.assertIn("已回退到内容判断", skill)
        self.assertIn("--speaker-count 2", skill)
        self.assertIn("--diarize off", skill)
        self.assertIn("--diarize required", skill)
        self.assertIn("合成音色", skill)

    def test_public_manifest_excludes_noncommercial_models(self) -> None:
        manifest = (
            ROOT / "scripts/diarization_models.json"
        ).read_text(encoding="utf-8").casefold()
        self.assertNotIn("reverb", manifest)
        self.assertNotIn("non-commercial", manifest)


if __name__ == "__main__":
    unittest.main()
