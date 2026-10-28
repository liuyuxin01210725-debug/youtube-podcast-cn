import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_lenses import (
    lens_for_show,
    lenses_ordered,
    load_json,
    load_showlist,
    mapped_show_ids,
    people_chips,
    shows_for_lens,
    validate_against_showlist,
)


class LensMappingTests(unittest.TestCase):
    def test_seven_default_lenses_theme_primary(self):
        data = load_json()
        self.assertEqual(data.get('ia'), 'theme-primary')
        lenses = lenses_ordered(data)
        self.assertEqual(len(lenses), 7)
        labels = [item['label_zh'] for item in lenses]
        self.assertEqual(labels, [
            '前沿科技',
            '具身与机器人',
            '健康长寿',
            '发芽发酵 / 食品',
            'AI 时代教育',
            '商业与投资',
            '小众生活与品牌',
        ])

    def test_confirm_v2_twelve_sources_mapped_once(self):
        problems = validate_against_showlist()
        self.assertEqual(problems, [])
        self.assertEqual(sorted(mapped_show_ids()), list(range(1, 13)))
        showlist = load_showlist()
        self.assertEqual(len(showlist['daily_scan']), 12)

    def test_people_nest_under_health_not_top_level(self):
        chips = people_chips('health-longevity')
        self.assertIn('Peter Attia', chips)
        self.assertIn('Rhonda Patrick', chips)
        # Huberman is observe-only, not a daily show chip under the lens shows list.
        shows = [show['name'] for show in shows_for_lens('health-longevity')]
        self.assertNotIn('Huberman Lab', shows)
        hit = lens_for_show(11)
        self.assertEqual(hit['lens_id'], 'health-longevity')
        hit2 = lens_for_show(1)
        self.assertEqual(hit2['lens_id'], 'frontier-tech')

    def test_empty_lenses_reserved(self):
        self.assertEqual(shows_for_lens('ai-education'), [])
        self.assertEqual(shows_for_lens('niche-life-brands'), [])


if __name__ == '__main__':
    unittest.main()
