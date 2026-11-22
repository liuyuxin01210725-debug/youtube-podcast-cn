import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sanitize_library_for_sim.py"
SPEC = importlib.util.spec_from_file_location("sanitize_library_for_sim", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SanitizeTextTests(unittest.TestCase):
    def assert_same_utf16(self, original: str) -> str:
        cleaned = MODULE.sanitize_text(original)
        self.assertEqual(MODULE.utf16_len(cleaned), MODULE.utf16_len(original))
        return cleaned

    def test_empty(self):
        self.assertEqual(MODULE.sanitize_text(""), "")

    def test_ascii_letters_change_but_keep_utf16_and_punctuation(self):
        cleaned = self.assert_same_utf16("Hello, world!\nNext.")
        self.assertTrue(cleaned.startswith(tuple("甲乙丙丁戊己庚辛壬癸")))
        self.assertIn(", ", cleaned)
        self.assertIn("!\n", cleaned)
        self.assertTrue(cleaned.endswith("."))
        self.assertNotEqual(cleaned, "Hello, world!\nNext.")
        self.assertEqual(cleaned.count("\n"), 1)

    def test_cjk_letters_same_utf16_length(self):
        original = "这是一段中文，含标点。"
        cleaned = self.assert_same_utf16(original)
        self.assertNotEqual(cleaned, original)
        self.assertTrue(cleaned.endswith("。"))
        self.assertIn("，", cleaned)
        self.assertEqual(len(cleaned), len(original))

    def test_emoji_keeps_two_utf16_units(self):
        original = "hi😀!"
        cleaned = self.assert_same_utf16(original)
        self.assertEqual(cleaned[-1], "!")
        self.assertEqual(MODULE.utf16_len(cleaned[2]), 2)

    def test_digits_and_marks_kept(self):
        original = "A12\u0301."
        cleaned = self.assert_same_utf16(original)
        self.assertIn("12", cleaned)
        self.assertIn("\u0301", cleaned)
        self.assertTrue(cleaned.endswith("."))

    def test_note_map_preserves_ids_and_timestamps(self):
        notes = {
            "abc123": {
                "body": "私密正文\n第二行",
                "episode_id": "abc123",
                "headline": "标题!",
                "tags": ["标签"],
                "updated_at": "2026-11-19T11:03:10+08:00",
            }
        }
        changed = MODULE.sanitize_note_map(notes)
        self.assertGreaterEqual(changed, 3)
        record = notes["abc123"]
        self.assertEqual(record["episode_id"], "abc123")
        self.assertEqual(record["updated_at"], "2026-11-19T11:03:10+08:00")
        self.assertEqual(MODULE.utf16_len(record["body"]), MODULE.utf16_len("私密正文\n第二行"))
        self.assertIn("\n", record["body"])
        self.assertTrue(record["headline"].endswith("!"))
        self.assertNotEqual(record["body"], "私密正文\n第二行")


if __name__ == "__main__":
    unittest.main()
