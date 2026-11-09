import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from diarization_cache import assign_studio_voices
from diarization_core import DiarizationTurn
from studio_ads import apply_ad_hygiene, mark_sponsor_windows, sponsor_cue
from studio_speakers import use_male_slots


def seg(start, end, text, speaker):
    return {'id': f'{start}', 'start_ms': start, 'end_ms': end, 'text': text, 'speaker_id': speaker}


class AdWindowTests(unittest.TestCase):
    def test_sponsor_cues_and_plain_dialogue(self):
        cues = [
            'This episode is sponsored by Athletic Greens.',
            'Brought to you by Squarespace.',
            'A quick word from our sponsor.',
            'Use code DOAC at checkout.',
            'Promo code inside.',
            'That is a paid partnership.',
            "We'll be right back after the break.",
            'We will be right back.',
            'Visit athleticgreens.com slash doac.',
            'Head to the site / offer.',
        ]
        for text in cues:
            self.assertTrue(sponsor_cue(text), text)
            self.assertTrue(sponsor_cue(text.upper()), text)
        plain = [
            'What did you learn from that year in Cuba?',
            'I visited Paris and the zip code was wrong.',
            'The source code is on the desk.',
            'After the breakdown we kept talking.',
            '♪',
        ]
        for text in plain:
            self.assertFalse(sponsor_cue(text), text)
        marked = mark_sponsor_windows([
            seg(0, 1000, plain[0], 'host'),
            seg(1000, 2000, 'This episode is sponsored by Acme.', 'ad'),
            seg(2000, 3000, 'Use the code at acme.com.', 'ad'),
            seg(3000, 4000, plain[1], 'guest'),
        ])
        self.assertEqual([item['is_ad'] for item in marked], [False, True, True, False])
        self.assertEqual(marked[0]['text'], plain[0])
        self.assertEqual(marked[3]['text'], plain[1])

    def test_tone_extends_a_window_and_stops_on_dialogue(self):
        marked = mark_sponsor_windows([
            seg(0, 1000, 'Welcome back to the show.', 'host'),
            seg(1000, 2000, 'This episode is sponsored by Acme.', 'ad'),
            seg(2000, 3000, 'They are offering twenty percent off.', 'ad'),
            seg(20000, 21000, 'The discount is on the site.', 'ad'),
            seg(21000, 22000, 'Tell me about the lab.', 'guest'),
        ])
        self.assertEqual([item['is_ad'] for item in marked], [False, True, True, False, False])

    def test_short_ad_voice_folds_and_a_real_third_voice_does_not(self):
        segments = [
            seg(0, 2000, 'Tell me about the work.', 'host'),
            seg(2000, 4000, 'This episode is sponsored by Acme.', 'adread'),
            seg(4000, 6000, 'We kept going anyway.', 'guest'),
        ]
        turns = [
            DiarizationTurn(0, 2000, 'host'),
            DiarizationTurn(2000, 4000, 'adread'),
            DiarizationTurn(4000, 6000, 'guest'),
        ]
        folded, ad_only = apply_ad_hygiene(segments, turns)
        self.assertEqual(ad_only, {'adread'})
        self.assertEqual([item['speaker_id'] for item in folded], ['host', 'host', 'guest'])
        self.assertEqual(folded[1]['folded_from'], 'adread')
        self.assertTrue(folded[1]['is_ad'])
        self.assertFalse(folded[0]['is_ad'])
        slots = assign_studio_voices([item['speaker_id'] for item in folded], male_only=True)
        self.assertEqual(set(slots), {'host', 'guest'})
        self.assertTrue(all(slot.startswith('male_') for slot in slots.values()))

        long_turns = turns + [DiarizationTurn(6000, 90000, 'adread')]
        _folded, still = apply_ad_hygiene(segments, long_turns)
        self.assertEqual(still, set())

    def test_male_slot_default_stays_on_for_a_two_hander(self):
        self.assertTrue(use_male_slots({'content_type': 'conversation', 'expected_speakers': 2}))
        self.assertTrue(use_male_slots({'expected_speakers': 1}))
        self.assertFalse(use_male_slots({'expected_speakers': 2, 'prefer_male_slots': False}))
        self.assertFalse(use_male_slots({'expected_speakers': 4, 'prefer_male_slots': True}))
        self.assertEqual(
            list(assign_studio_voices(['host', 'guest'], male_only=True).values()),
            ['male_1', 'male_2'],
        )


if __name__ == '__main__':
    unittest.main()
