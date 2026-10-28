import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_articles import (
    enqueue_article,
    extract_main_text,
    load_fixture,
    normalize_article_url,
)
from studio_inbox import process_inbox
from studio_publish_icloud import atomic_write_json


class ArticleNormalizeTests(unittest.TestCase):
    def test_normalize_keeps_article_rejects_youtube_and_private(self):
        self.assertEqual(
            normalize_article_url('https://Example.com/posts/hello-world?utm_source=x'),
            'https://example.com/posts/hello-world',
        )
        self.assertIsNone(normalize_article_url('https://youtu.be/abcdefghijk'))
        self.assertIsNone(normalize_article_url('https://www.youtube.com/watch?v=abcdefghijk'))
        self.assertIsNone(normalize_article_url('https://127.0.0.1/a'))


class ArticleExtractEnqueueTests(unittest.TestCase):
    def test_extract_fixture_and_enqueue_stub(self):
        html = load_fixture()
        extracted = extract_main_text(html)
        self.assertIn('Sample Frontier Note', extracted['title'])
        self.assertGreater(extracted['chars'], 40)
        self.assertIn('durable memory', extracted['text'])
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / 'jobs'
            info = enqueue_article(
                jobs,
                'https://example.com/posts/hello',
                execute=False,
                fixture_html=html,
            )
            self.assertTrue(info['stub_tts'])
            self.assertFalse(info['execute'])
            job = Path(info['path'])
            config = json.loads((job / 'config.json').read_text(encoding='utf-8'))
            self.assertEqual(config['content_type'], 'article')
            self.assertEqual(config['provider'], 'deepseek')
            self.assertEqual(config['tts'], 'edge')
            self.assertEqual(config['expected_speakers'], 1)
            self.assertEqual(config['article_url'], 'https://example.com/posts/hello')
            self.assertTrue((job / 'source.md').is_file())
            article = json.loads((job / 'article.json').read_text(encoding='utf-8'))
            self.assertTrue(article['stub_tts'])

    def test_inbox_article_content_type_enqueues_without_running_tts(self):
        html = load_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / 'library'
            jobs = root / 'jobs'
            inbox = library / 'inbox'
            inbox.mkdir(parents=True)
            ident = 'e' * 32
            atomic_write_json(inbox / f'{ident}.json', {
                'id': ident,
                'url': 'https://example.com/posts/hello',
                'shared_at': '2026-10-28T06:00:00Z',
                'content_type': 'article',
                'expected_speakers': 1,
            })
            with patch('studio_articles.fetch_or_fixture', return_value=html):
                results = process_inbox(library, jobs, run=True)
            by_id = {item['id']: item for item in results}
            self.assertEqual(by_id[ident]['status'], 'queued')
            self.assertTrue(by_id[ident].get('stub_tts'))
            job_id = by_id[ident]['job_id']
            state = json.loads((jobs / job_id / 'state.json').read_text(encoding='utf-8'))
            # execute=False ledger: prepared/queued bookkeeping, not a finished TTS publish.
            self.assertIn(state['status'], {'prepared', 'queued'})
            self.assertFalse((jobs / job_id / 'output.mp3').exists())


if __name__ == '__main__':
    unittest.main()
