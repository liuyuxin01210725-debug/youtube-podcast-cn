import re
import unittest
from pathlib import Path


LIBRARY = (
    Path(__file__).resolve().parents[1]
    / "ios/PrivateListen/PrivateListen/LibraryView.swift"
)


class LibrarySearchLayoutTests(unittest.TestCase):
    def test_native_search_has_explicit_navigation_bar_placement(self):
        source = LIBRARY.read_text()
        self.assertIn("placement: .navigationBarDrawer(displayMode: .always)", source)

    def test_toolbar_search_is_available_at_any_library_size(self):
        source = LIBRARY.read_text()
        self.assertRegex(
            source,
            r"ToolbarItem\(placement: \.topBarLeading\)\s*\{\s*Button\s*\{\s*searchPresented = true",
        )

    def test_search_presentation_removes_mini_player_from_bottom_inset(self):
        source = LIBRARY.read_text()
        inset = re.search(r"\.safeAreaInset\(edge: \.bottom, spacing: 0\)\s*\{\s*([^\n]+)", source)
        self.assertIsNotNone(inset)
        self.assertIn("!searchPresented", inset.group(1))


if __name__ == "__main__":
    unittest.main()
