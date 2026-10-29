#!/usr/bin/env python3
"""声间抽取 MVP 门禁测试（§13.1–13.5 + §13.8）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_extract import (
    cancel_queued_on_unstar,
    enqueue_deep,
    export_markdown,
    light_packet,
    list_badge,
    load_extract,
    looks_like_universal_summary,
    merge_extract_state,
    mock_deep_extract,
    pending_confirm_count,
    preserve_extract_on_republish,
    resolve_schema,
    save_extract,
    schema_from_lens_id,
    schema_from_lens_label,
    span_locatable,
    validate_output_axis,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'extract'


class SchemaMappingTests(unittest.TestCase):
    def test_lens_id_table_1382(self):
        self.assertEqual(schema_from_lens_id(None), 'general')
        self.assertEqual(schema_from_lens_id('ferment-food'), 'ferment')
        self.assertEqual(schema_from_lens_id('business-investing'), 'biz_invest')
        self.assertEqual(schema_from_lens_id('health-longevity'), 'health')
        self.assertEqual(schema_from_lens_id('ai-education'), 'edu')
        self.assertEqual(schema_from_lens_id('embodied-robotics'), 'embodied')
        self.assertEqual(schema_from_lens_id('frontier-tech'), 'general')

    def test_lens_labels(self):
        self.assertEqual(schema_from_lens_label('发芽发酵 / 食品'), 'ferment')
        self.assertEqual(schema_from_lens_label('商业与投资'), 'biz_invest')
        self.assertEqual(schema_from_lens_label('健康长寿'), 'health')

    def test_no_lens_defaults_general_forbid_preclassify(self):
        resolved = resolve_schema([])
        self.assertEqual(resolved['schema_id'], 'general')
        self.assertTrue(resolved['forbid_preclassify'])

    def test_embodied_mvp_degrades_to_general_not_health(self):
        resolved = resolve_schema(['embodied-robotics'], allow_embodied=False)
        self.assertEqual(resolved['schema_id'], 'general')
        self.assertEqual(resolved['degraded_from'], 'embodied')
        self.assertNotEqual(resolved['schema_id'], 'health')

    def test_primary_lens_wins(self):
        resolved = resolve_schema(
            ['frontier-tech', 'ferment-food'],
            primary_lens='ferment-food',
        )
        self.assertEqual(resolved['schema_id'], 'ferment')
        self.assertIn('general', resolved['secondary_schemas'])


class GateTests(unittest.TestCase):
    def test_output_axis_rejects_bad_times(self):
        self.assertTrue(validate_output_axis(1.0, 2.0, duration_sec=100))
        self.assertFalse(validate_output_axis(-1, 2))
        self.assertFalse(validate_output_axis(5, 4))
        self.assertFalse(validate_output_axis('x', 2))

    def test_span_must_locate(self):
        text = '盐曲按两斤盐一斤曲，密封室温发酵'
        self.assertTrue(span_locatable('盐曲按两斤盐一斤曲', text))
        self.assertFalse(span_locatable('不存在的幻觉书名全集', text))
        self.assertFalse(span_locatable('短', text))

    def test_light_never_pending(self):
        packet = light_packet(work_id='ep1', schema_id='general', summary_line='听完一句话')
        self.assertEqual(packet['mode'], 'light')
        self.assertEqual(pending_confirm_count(packet), 0)
        self.assertIsNone(list_badge(packet))

    def test_deep_requires_transcript_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            ep = Path(tmp) / 'ep'
            ep.mkdir()
            failed = enqueue_deep(episode_dir=ep, work_id='ep', schema_id='ferment', has_transcript=False)
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(failed['failure_reason'], 'no_transcript')
            queued = enqueue_deep(episode_dir=ep, work_id='ep', schema_id='ferment', has_transcript=True)
            self.assertEqual(queued['status'], 'queued')

    def test_unstar_cancels_queued_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ep = Path(tmp) / 'ep'
            ep.mkdir()
            enqueue_deep(episode_dir=ep, work_id='ep', schema_id='general', has_transcript=True)
            cancelled = cancel_queued_on_unstar(ep)
            self.assertEqual(cancelled['status'], 'failed')
            self.assertEqual(cancelled['failure_reason'], 'cancelled_unstar')
            self.assertEqual(cancelled['cost_units'], 0)

            ready = mock_deep_extract(
                work_id='ep', schema_id='general',
                transcript='收藏只是入口抽成可行动卡片才算拥有。本期嘉宾提到一个值得关注的人。也提到另一个节目值得候选关注。这套方法换到中文语境是否仍成立。',
            )
            save_extract(ep, ready)
            kept = cancel_queued_on_unstar(ep)
            self.assertEqual(kept['status'], 'ready')

    def test_universal_summary_fails_packet(self):
        packet = {
            'mode': 'deep',
            'schema_id': 'ferment',
            'claims': [{'text': '今天嘉宾聊了很多很多故事情节发展', 'point_type': 'plot_dump'}],
            'methods': [], 'entities': [], 'quotes': [], 'open_questions': [],
        }
        self.assertTrue(looks_like_universal_summary(packet))


class MergePublishTests(unittest.TestCase):
    def test_merge_preserves_confirmed_and_actions(self):
        existing = mock_deep_extract(
            work_id='ep', schema_id='ferment',
            transcript=json.loads((FIXTURES / 'transcripts.json').read_text())['ferment'],
        )
        existing['methods'][0]['status'] = 'confirmed'
        existing['actions'] = [{'id': 'a1', 'kind': 'try_later', 'item_id': existing['methods'][0]['id']}]
        incoming = mock_deep_extract(
            work_id='ep', schema_id='ferment',
            transcript=json.loads((FIXTURES / 'transcripts.json').read_text())['ferment'],
        )
        merged = merge_extract_state(existing, incoming)
        method = next(m for m in merged['methods'] if m['id'] == existing['methods'][0]['id'])
        self.assertEqual(method['status'], 'confirmed')
        self.assertEqual(merged['actions'][0]['id'], 'a1')

    def test_preserve_on_republish_marks_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            ep = Path(tmp) / 'ep'
            ep.mkdir()
            packet = mock_deep_extract(
                work_id='ep', schema_id='health',
                transcript=json.loads((FIXTURES / 'transcripts.json').read_text())['health'],
            )
            save_extract(ep, packet)
            out = preserve_extract_on_republish(ep, new_transcript_hash='differenthash01')
            self.assertEqual(out['status'], 'stale')
            self.assertEqual(load_extract(ep)['status'], 'stale')


class GoldenFixtureTests(unittest.TestCase):
    def test_ferment_craft_card(self):
        golden = json.loads((FIXTURES / 'ferment_golden.json').read_text())
        self.assertEqual(golden['schema_id'], 'ferment')
        self.assertEqual(golden['status'], 'ready')
        self.assertTrue(any(m.get('point_type') == 'method_craft' for m in golden['methods']))
        craft = next(m for m in golden['methods'] if m['point_type'] == 'method_craft')
        self.assertTrue(craft.get('steps'))
        self.assertTrue(craft.get('params'))

    def test_biz_invest_outlook_and_org(self):
        golden = json.loads((FIXTURES / 'biz_invest_golden.json').read_text())
        self.assertTrue(any(c.get('point_type') == 'claim_outlook' for c in golden['claims']))
        self.assertTrue(any(e.get('type') == 'Org' for e in golden['entities']))
        # stance may exist internally but fixture documents it is not primary UI
        claim = next(c for c in golden['claims'] if c['point_type'] == 'claim_outlook')
        self.assertIn('stance', claim)

    def test_health_method_record_only(self):
        golden = json.loads((FIXTURES / 'health_golden.json').read_text())
        dose = next(m for m in golden['methods'] if m.get('point_type') == 'method_dose')
        self.assertEqual(dose.get('ui_primary_label'), '仅记录')
        self.assertEqual(dose.get('default_action'), 'record_only')
        self.assertIn('非诊断', dose.get('disclaimer', ''))

    def test_markdown_export_from_extract(self):
        golden = json.loads((FIXTURES / 'ferment_golden.json').read_text())
        md = export_markdown(golden)
        self.assertIn('Method', md)
        self.assertIn('method_craft', ''.join(json.dumps(golden)))
        self.assertIn('candidate', md)


class SecondaryCapTests(unittest.TestCase):
    def test_secondary_schemas_capped(self):
        text = json.loads((FIXTURES / 'transcripts.json').read_text())['general']
        packet = mock_deep_extract(
            work_id='ep', schema_id='general', transcript=text,
            secondary_schemas=['ferment', 'biz_invest', 'health', 'edu'],
        )
        secondary = 0
        for key in ('claims', 'methods', 'entities', 'quotes', 'open_questions'):
            for item in packet.get(key) or []:
                if item.get('from_secondary_schema'):
                    secondary += 1
        self.assertLessEqual(secondary, 5)


if __name__ == '__main__':
    unittest.main()
