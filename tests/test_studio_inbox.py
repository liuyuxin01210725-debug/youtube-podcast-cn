import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_engine import StudioEngine, StudioInputError
from studio_inbox import (
    blocking_job,
    canonical_url,
    enqueue,
    job_inbox_status,
    process_inbox,
    server_busy,
)
from studio_publish_icloud import atomic_write_json
from studio_urls import is_public_https
from temporary_audio import TemporaryAudioError


def share(inbox: Path, ident: str, url: str, shared_at: str = '2026-10-26T00:00:00Z') -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    atomic_write_json(inbox / f'{ident}.json', {
        'id': ident,
        'url': url,
        'shared_at': shared_at,
        'source_app': 'com.apple.mobilesafari',
    })


class UrlTests(unittest.TestCase):
    def test_youtube_forms_collapse_and_private_hosts_are_rejected(self):
        self.assertEqual(
            canonical_url('https://youtu.be/MM-C3JqCXBk?si=track'),
            'https://www.youtube.com/watch?v=MM-C3JqCXBk',
        )
        self.assertEqual(
            canonical_url('https://m.youtube.com/watch?v=MM-C3JqCXBk&t=12'),
            canonical_url('https://www.youtube.com/watch?v=MM-C3JqCXBk'),
        )
        self.assertEqual(
            canonical_url('https://Example.com/a/hello-world/?utm_source=ios&id=7'),
            'https://example.com/a/hello-world?id=7',
        )
        for raw in ('http://example.com/a', 'https://127.0.0.1/a', 'https://10.1.1.1/a', 'https://localhost/a', 'file:///tmp/a', 'not a url'):
            self.assertIsNone(canonical_url(raw))
            self.assertFalse(is_public_https(raw))


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = self.root / 'library'
        self.jobs = self.root / 'jobs'
        self.inbox = self.library / 'inbox'
        self.meta = patch('studio_server.lookup_source_metadata', return_value=None)
        self.topic = patch('studio_server.lookup_chinese_topic', return_value=None)
        self.meta.start()
        self.topic.start()

    def tearDown(self):
        self.topic.stop()
        self.meta.stop()
        self.tmp.cleanup()

    def test_dedupes_youtube_and_keeps_going_after_a_bad_share(self):
        share(self.inbox, 'a' * 32, 'https://127.0.0.1/secret', '2026-10-26T00:00:01Z')
        share(self.inbox, 'b' * 32, 'https://youtu.be/abcdefghijk', '2026-10-26T00:00:02Z')
        share(self.inbox, 'c' * 32, 'https://www.youtube.com/watch?v=abcdefghijk&list=PL', '2026-10-26T00:00:03Z')
        broken = self.inbox / f'{"d" * 32}.json'
        broken.write_text('{', encoding='utf-8')
        old = time.time() - 180
        os.utime(broken, (old, old))
        first = process_inbox(self.library, self.jobs, run=False)
        by_id = {item['id']: item for item in first}
        self.assertEqual(by_id['a' * 32]['status'], 'failed')
        self.assertEqual(by_id['b' * 32]['status'], 'queued')
        self.assertEqual(by_id['c' * 32]['status'], 'duplicate')
        self.assertEqual(by_id['b' * 32]['job_id'], by_id['c' * 32]['job_id'])
        broken_status = json.loads((self.inbox / f'{"d" * 32}.status.json').read_text(encoding='utf-8'))
        self.assertEqual(broken_status['status'], 'failed')
        job = self.jobs / by_id['b' * 32]['job_id']
        config = json.loads((job / 'config.json').read_text(encoding='utf-8'))
        state = json.loads((job / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(config['source']['kind'], 'youtube')
        self.assertEqual(config['source']['value'], 'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertEqual(config['tts'], 'edge')
        self.assertEqual(config['expected_speakers'], 2)
        self.assertEqual(state['action'], 'run')
        self.assertEqual(len([p for p in self.jobs.iterdir() if p.is_dir()]), 1)
        again = {item['id']: item['status'] for item in process_inbox(self.library, self.jobs, run=False)}
        self.assertEqual(again['b' * 32], 'queued')
        self.assertEqual(len([p for p in self.jobs.iterdir() if p.is_dir()]), 1)

    def test_shared_monologue_defaults_to_one_and_explicit_count_is_preserved(self):
        for index, options, expected in [(1, {'content_type': 'monologue'}, 1), (2, {'expected_speakers': 3}, 3)]:
            ident = str(index) * 32
            share(self.inbox, ident, f'https://example.com/episode-{index}')
            path = self.inbox / f'{ident}.json'
            atomic_write_json(path, {**json.loads(path.read_text()), **options})
            result = next(item for item in process_inbox(self.library, self.jobs) if item['id'] == ident)
            config = json.loads((self.jobs / result['job_id'] / 'config.json').read_text())
            self.assertEqual(config['expected_speakers'], expected)

    def test_web_link_uses_edge_and_a_failed_job_can_be_shared_again(self):
        share(self.inbox, 'e' * 32, 'https://open.substack.com/pub/example/p/hello-there')
        queued = process_inbox(self.library, self.jobs, run=False)[0]
        job = self.jobs / queued['job_id']
        config = json.loads((job / 'config.json').read_text(encoding='utf-8'))
        self.assertEqual(config['tts'], 'edge')
        self.assertEqual(config['source']['kind'], 'web')
        self.assertNotIn('http', json.loads((job / 'state.json').read_text(encoding='utf-8'))['title'])
        state = json.loads((job / 'state.json').read_text(encoding='utf-8'))
        state['status'] = 'failed'
        atomic_write_json(job / 'state.json', state)
        atomic_write_json(job / 'progress.json', {'message': '找不到运行程序，请检查 ffmpeg、yt-dlp 和项目 Python 环境。'})
        failed = {item['id']: item for item in process_inbox(self.library, self.jobs, run=False)}
        self.assertEqual(failed['e' * 32]['status'], 'failed')
        self.assertIn('ffmpeg', json.loads((self.inbox / f'{"e" * 32}.status.json').read_text(encoding='utf-8'))['error'])
        share(self.inbox, 'f' * 32, 'https://open.substack.com/pub/example/p/hello-there', '2026-10-26T01:00:00Z')
        retried = {item['id']: item for item in process_inbox(self.library, self.jobs, run=False)}
        self.assertEqual(retried['f' * 32]['status'], 'queued')
        self.assertNotEqual(retried['f' * 32]['job_id'], queued['job_id'])

    def test_run_is_skipped_while_the_workbench_holds_the_lock(self):
        share(self.inbox, '1' * 32, 'https://example.com/episode.mp3')
        with patch('studio_inbox.server_busy', return_value=True), patch('studio_inbox.run_job') as run:
            process_inbox(self.library, self.jobs, run=True)
            run.assert_not_called()
        with patch('studio_inbox.server_busy', return_value=False), patch('studio_inbox.run_job', return_value=0) as run:
            process_inbox(self.library, self.jobs, run=True)
            run.assert_called_once()

    def test_enqueue_failure_does_not_raise(self):
        share(self.inbox, '2' * 32, 'https://example.com/one')
        share(self.inbox, '3' * 32, 'https://example.com/two', '2026-10-26T02:00:00Z')
        real = enqueue

        def flaky(jobs, url, **options):
            if url.endswith('/one'):
                raise RuntimeError('boom')
            return real(jobs, url, **options)

        with patch('studio_inbox.enqueue', side_effect=flaky):
            results = {item['id']: item['status'] for item in process_inbox(self.library, self.jobs, run=False)}
        self.assertEqual(results['2' * 32], 'failed')
        self.assertEqual(results['3' * 32], 'queued')

    def test_published_job_is_recorded_and_private_url_never_downloads(self):
        share(self.inbox, '4' * 32, 'https://www.youtube.com/watch?v=abcdefghijk')
        queued = process_inbox(self.library, self.jobs, run=False)[0]
        job = self.jobs / queued['job_id']
        state = json.loads((job / 'state.json').read_text(encoding='utf-8'))
        state['status'] = 'complete'
        atomic_write_json(job / 'state.json', state)
        atomic_write_json(job / 'private-listen.json', {'ok': True})
        status, error = job_inbox_status(job)
        self.assertEqual((status, error), ('published', None))
        again = process_inbox(self.library, self.jobs, run=False)[0]
        self.assertEqual(again['status'], 'published')
        self.assertIsNone(blocking_job(self.jobs, 'https://example.com/missing'))
        with tempfile.TemporaryDirectory() as folder:
            engine = StudioEngine(Path(folder) / 'job')
            with patch('studio_engine.download_audio_only', side_effect=TemporaryAudioError('no')) as download:
                with self.assertRaises(StudioInputError):
                    engine.acquire({'source': {'kind': 'web', 'value': 'https://192.168.1.9/a.mp3'}})
                download.assert_not_called()
                with self.assertRaises(TemporaryAudioError):
                    engine.acquire({'source': {'kind': 'web', 'value': 'https://cdn.example.com/show.mp3'}})
                download.assert_called_once()

    def test_share_cannot_enqueue_a_book_url(self):
        ident = '9' * 32
        share(self.inbox, ident, 'https://example.com/some-book')
        path = self.inbox / f'{ident}.json'
        atomic_write_json(path, {**json.loads(path.read_text(encoding='utf-8')), 'content_type': 'book', 'expected_speakers': 1})
        result = process_inbox(self.library, self.jobs, run=False)
        self.assertEqual(result[0]['status'], 'failed')
        self.assertEqual(result[0]['id'], ident)
        self.assertEqual([p for p in self.jobs.iterdir() if p.is_dir()], [])

    def test_server_busy_follows_the_lock(self):
        self.jobs.mkdir()
        self.assertFalse(server_busy(self.jobs))
        import fcntl
        handle = (self.jobs / '.server.lock').open('a+')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(server_busy(self.jobs))
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self.assertFalse(server_busy(self.jobs))


if __name__ == '__main__':
    unittest.main()
