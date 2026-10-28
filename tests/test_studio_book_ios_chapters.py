"""Chapter index parse + resume cursor clamp + lazy synth enqueue (no network)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from studio_book_synth import (
    BookSynthError,
    audio_path,
    enqueue_block,
    enqueue_chapter,
    load_queue,
    mark,
    materialize_tiny_block,
    next_pending,
    prepare_en_pilot,
    relative_audio_file,
    summarize,
    synthesize_block,
    translate_block_text,
)
from studio_books import build_book, chapter_index, clamp_resume, seek


def _sample_book():
    paragraph = '这是用来填满一块听书的句子。' * 40
    chunk = '\n\n'.join([paragraph] * 8)
    text = '第一章 开端\n\n' + chunk + '\n\n第二章 后来\n\n' + chunk
    return build_book(text, title='示例书', language='zh')


class ChapterIndexTests(unittest.TestCase):
    def test_chapter_index_lists_titles_and_missing_audio(self):
        book = _sample_book()
        index = chapter_index(book)
        self.assertEqual(index['content_type'], 'book')
        self.assertEqual(index['title'], '示例书')
        self.assertEqual([c['title'] for c in index['chapters']], ['第一章 开端', '第二章 后来'])
        self.assertEqual(index['chapters'][0]['index'], 0)
        self.assertEqual(index['chapters'][0]['blocks'][0]['index'], 0)
        self.assertEqual(index['chapters'][0]['blocks'][0]['audio_status'], 'missing')
        self.assertIsNone(index['chapters'][0]['blocks'][0]['audio_file'])
        self.assertGreater(index['chapters'][0]['blocks'][0]['chars'], 0)
        self.assertEqual(index['progress']['chapter_id'], 'c001')
        self.assertEqual(index['progress']['block_id'], 'c001b001')
        self.assertEqual(index['progress']['offset_chars'], 0)

    def test_chapter_index_rejects_empty_book(self):
        with self.assertRaisesRegex(ValueError, '没有可听'):
            chapter_index({'chapters': []})


class ResumeClampTests(unittest.TestCase):
    def test_clamp_falls_back_and_caps_offset(self):
        book = _sample_book()
        first_block = book['chapters'][0]['blocks'][0]
        clamped = clamp_resume(book, {
            'chapter_id': 'c999',
            'block_id': 'missing',
            'offset_chars': -5,
        })
        self.assertEqual(clamped['chapter_id'], 'c001')
        self.assertEqual(clamped['block_id'], first_block['id'])
        self.assertEqual(clamped['offset_chars'], 0)

        over = clamp_resume(book, {
            'chapter_id': 'c001',
            'block_id': first_block['id'],
            'offset_chars': 10_000_000,
        })
        self.assertEqual(over['offset_chars'], first_block['chars'])

        ok = clamp_resume(book, {
            'chapter_id': 'c002',
            'block_id': book['chapters'][1]['blocks'][0]['id'],
            'offset_chars': 3,
            'audio_seconds': 12.5,
        })
        self.assertEqual(ok['chapter_id'], 'c002')
        self.assertEqual(ok['offset_chars'], 3)
        self.assertEqual(ok['audio_seconds'], 12.5)

    def test_clamp_drops_invalid_seconds_and_seek_still_raises(self):
        book = _sample_book()
        clamped = clamp_resume(book, {
            'chapter_id': 'c001',
            'block_id': 'c001b001',
            'offset_chars': 1,
            'audio_seconds': -1,
        })
        self.assertNotIn('audio_seconds', clamped)
        with self.assertRaisesRegex(ValueError, '找不到'):
            seek(book, chapter_id='c009', block_id='c009b001', offset_chars=0)


class LazySynthQueueTests(unittest.TestCase):
    def test_enqueue_is_idempotent_and_does_not_create_mp3(self):
        book = _sample_book()
        with tempfile.TemporaryDirectory() as root:
            job = Path(root)
            first = enqueue_block(job, book, 'c001b001')
            second = enqueue_block(job, book, 'c001b001')
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(first['status'], 'queued')
            self.assertFalse(first['execute'])
            self.assertEqual(first['audio_file'], 'chapters/c001/c001b001.mp3')
            self.assertFalse(audio_path(job, chapter_id='c001', block_id='c001b001').exists())
            batch = enqueue_chapter(job, book, 'c001')
            self.assertGreaterEqual(len(batch['enqueued']), 1)
            pending = next_pending(job)
            self.assertIsNotNone(pending)
            marked = mark(job, pending['id'], 'failed', error='脚手架不跑长 TTS')
            self.assertEqual(marked['status'], 'failed')
            self.assertEqual(marked['error'], '脚手架不跑长 TTS')
            summary = summarize(job, book)
            self.assertEqual(summary['queue']['failed'], 1)
            statuses = {
                block['audio_status']
                for chapter in summary['book']['chapters']
                for block in chapter['blocks']
            }
            self.assertIn('failed', statuses)
            self.assertIn('missing', statuses)
            with self.assertRaisesRegex(BookSynthError, '找不到这一章'):
                enqueue_chapter(job, book, 'c999')
            queue = load_queue(job)
            self.assertEqual(queue['schema_version'], 1)
            raw = json.loads((job / 'synth_queue.json').read_text())
            self.assertIsInstance(raw['items'], list)

    def test_materialize_tiny_block_uses_confirmed_path(self):
        book = _sample_book()
        with tempfile.TemporaryDirectory() as root:
            job = Path(root)
            enqueue_block(job, book, 'c001b001')
            dest = materialize_tiny_block(job, chapter_id='c001', block_id='c001b001')
            self.assertEqual(dest, job / 'chapters' / 'c001' / 'c001b001.mp3')
            self.assertTrue(dest.is_file())
            self.assertGreater(dest.stat().st_size, 32)
            self.assertEqual(relative_audio_file('c001', 'c001b001'), 'chapters/c001/c001b001.mp3')
            summary = summarize(job, book)
            block = summary['book']['chapters'][0]['blocks'][0]
            self.assertEqual(block['audio_status'], 'ready')
            self.assertEqual(block['audio_file'], 'chapters/c001/c001b001.mp3')


class EnPilotFixtureTests(unittest.TestCase):
    def test_en_pilot_index_queue_path_shape(self):
        source = ROOT / 'fixtures' / 'book-en-pilot' / 'source.md'
        self.assertTrue(source.is_file())
        with tempfile.TemporaryDirectory() as root:
            job = Path(root) / 'en-pilot-demo'
            result = prepare_en_pilot(job, source=source, materialize_first=True)
            self.assertEqual(result['audio_file'], 'chapters/c001/c001b001.mp3')
            self.assertEqual(result['materialized'], 'chapters/c001/c001b001.mp3')
            self.assertTrue((job / 'chapters' / 'c001' / 'c001b001.mp3').is_file())
            book = json.loads((job / 'book.json').read_text())
            self.assertEqual(book['language'], 'en')
            self.assertFalse(book['whole_book_file'])
            self.assertGreaterEqual(len(book['chapters']), 2)
            episode = json.loads((job / 'episode.json').read_text())
            self.assertEqual(episode['content_type'], 'book')
            self.assertIsNone(episode.get('file'))
            # UI-model shape App will parse: chapter titles + clamp-ready progress
            index = chapter_index(book)
            self.assertEqual(index['chapters'][0]['title'], 'Chapter 1 The Open Field')
            self.assertEqual(index['chapters'][1]['title'], 'Chapter 2 The Quiet Road')
            moved = clamp_resume(book, {
                'chapter_id': 'c002',
                'block_id': book['chapters'][1]['blocks'][0]['id'],
                'offset_chars': 4,
            })
            self.assertEqual(moved['chapter_id'], 'c002')
            self.assertEqual(moved['offset_chars'], 4)
            summary = summarize(job, book)
            self.assertEqual(summary['book']['chapters'][0]['blocks'][0]['audio_status'], 'ready')
            # First chapter queued (some may already be ready via materialize)
            self.assertGreaterEqual(result['enqueued'], 1)
            self.assertFalse(any(item.get('execute') for item in summary['queue']['items']))



class TranslateAndSynthStubTests(unittest.TestCase):
    def test_translate_stub_without_client_keeps_english(self):
        # Force no keychain/env lookup so CI and keyed Macs share the stub path.
        with patch('studio_book_synth._translation_client', return_value=None):
            result = translate_block_text('Hello open field.', language='en', client=None)
        self.assertEqual(result['text'], 'Hello open field.')
        self.assertFalse(result['translated'])
        self.assertTrue(result['stub'])

    def test_translate_uses_injected_client(self):
        client = MagicMock()
        client.translate.return_value = {'b1': '你好，开阔的田野。'}
        result = translate_block_text('Hello open field.', language='en', client=client)
        self.assertTrue(result['translated'])
        self.assertIn('你好', result['text'])
        self.assertFalse(result['stub'])

    def test_synthesize_refuses_network_when_execute_false(self):
        book = _sample_book()
        with tempfile.TemporaryDirectory() as root:
            job = Path(root)
            enqueue_block(job, book, 'c001b001')
            result = synthesize_block(job, book, 'c001b001', execute=False)
            self.assertTrue(result['skipped'])
            self.assertFalse(result['execute'])
            self.assertFalse(audio_path(job, chapter_id='c001', block_id='c001b001').exists())

    def test_synthesize_execute_true_with_mock_backend(self):
        book = _sample_book()
        with tempfile.TemporaryDirectory() as root:
            job = Path(root)
            enqueue_block(job, book, 'c001b001')

            class FakeBackend:
                async def synthesize(self, request, output):
                    Path(output).write_bytes(b'ID3' + b'\0' * 64)
                    return None

            translator = MagicMock()
            # Chinese book: no translate needed
            result = synthesize_block(
                job,
                book,
                'c001b001',
                execute=True,
                backend=FakeBackend(),
                translator=translator,
                translate=True,
            )
            self.assertTrue(result['ok'])
            dest = audio_path(job, chapter_id='c001', block_id='c001b001')
            self.assertTrue(dest.is_file())
            self.assertGreater(dest.stat().st_size, 32)
            translator.translate.assert_not_called()
            queue = load_queue(job)
            ready = [i for i in queue['items'] if i['block_id'] == 'c001b001'][0]
            self.assertEqual(ready['status'], 'ready')
            self.assertTrue(ready['execute'])


class OptionalMacEdgeSmoke(unittest.TestCase):
    """Network smoke: skips unless BOOK_SYNTH_SMOKE=1 (keys/network on Mac)."""

    def test_optional_edge_smoke_pilot_block(self):
        import os
        if os.environ.get('BOOK_SYNTH_SMOKE') != '1':
            self.skipTest('set BOOK_SYNTH_SMOKE=1 to run Edge/DeepSeek smoke')
        source = ROOT / 'fixtures' / 'book-en-pilot' / 'source.md'
        with tempfile.TemporaryDirectory() as root:
            job = Path(root) / 'en-pilot-smoke'
            result = prepare_en_pilot(job, source=source, materialize_first=False, execute=True)
            self.assertTrue(result['execute'])
            dest = job / result['audio_file']
            self.assertTrue(dest.is_file())
            self.assertGreater(dest.stat().st_size, 200)



if __name__ == '__main__':
    unittest.main()
