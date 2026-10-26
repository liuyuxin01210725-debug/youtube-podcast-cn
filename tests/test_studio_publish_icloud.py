import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_engine import StudioEngine
from studio_publish_icloud import (
    PublishError,
    compose_display_title,
    fetch_youtube_metadata,
    looks_like_url_title,
    main,
    probe_duration_sec,
    publish_job,
    remember_source_title,
    translate_episode_topic,
    youtube_id_from_url,
)
from studio_server import Studio, read, write


class PublishTests(unittest.TestCase):
    def make_job(self, root, name='6d898d4192de495fafcec86b9c938430', title='肌酸对谈', youtube='https://www.youtube.com/watch?v=MM-C3JqCXBk', audio=b'full-audio'):
        job = Path(root) / name
        job.mkdir()
        write(job / 'state.json', {'title': title, 'status': 'complete', 'action': 'render'})
        write(job / 'config.json', {'source': {'kind': 'youtube', 'value': youtube}})
        (job / 'output.mp3').write_bytes(audio)
        return job

    def test_youtube_id_parser(self):
        self.assertEqual(youtube_id_from_url('https://www.youtube.com/watch?v=MM-C3JqCXBk&t=12'), 'MM-C3JqCXBk')
        self.assertEqual(youtube_id_from_url('https://youtu.be/MM-C3JqCXBk'), 'MM-C3JqCXBk')
        self.assertEqual(youtube_id_from_url('https://m.youtube.com/shorts/abcdefghijk'), 'abcdefghijk')
        self.assertEqual(youtube_id_from_url('https://www.youtube.com/embed/abcdefghijk'), 'abcdefghijk')
        self.assertIsNone(youtube_id_from_url('https://example.com/watch?v=abcdefghijk'))
        self.assertIsNone(youtube_id_from_url('not a url'))

    def test_publish_copies_orders_and_replaces(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            older = self.make_job(root, 'job-older', '较早的一期', audio=b'old')
            newer = self.make_job(root, 'job-newer', '最新的一期', audio=b'new')
            os.utime(older / 'output.mp3', (1_000, 1_000))
            os.utime(newer / 'output.mp3', (2_000, 2_000))
            (Path(library) / 'library.json').write_text('{', encoding='utf-8')
            with patch('studio_publish_icloud.shutil.which', return_value=None):
                first = publish_job(older, library)
                second = publish_job(newer, library)
            self.assertEqual(first['youtube_id'], 'MM-C3JqCXBk')
            self.assertIsNone(first['duration_sec'])
            self.assertEqual((Path(library) / 'episodes' / 'job-newer' / 'output.mp3').read_bytes(), b'new')
            self.assertNotEqual((newer / 'output.mp3').stat().st_ino, (Path(library) / 'episodes' / 'job-newer' / 'output.mp3').stat().st_ino)
            self.assertFalse(list(Path(library).rglob('*.partial')))
            self.assertFalse(list(Path(library).rglob('*.tmp')))
            listing = json.loads((Path(library) / 'library.json').read_text(encoding='utf-8'))
            self.assertEqual([item['id'] for item in listing['episodes']], ['job-newer', 'job-older'])
            episode = json.loads((Path(second['episode_dir']) / 'episode.json').read_text(encoding='utf-8'))
            for key in ('id', 'title', 'created_at', 'duration_sec', 'source_job_id', 'youtube_id', 'file'):
                self.assertIn(key, episode)
            self.assertEqual(episode['file'], 'output.mp3')
            self.assertEqual(episode['source_job_id'], 'job-newer')
            (newer / 'output.mp3').write_bytes(b'newer-still')
            os.utime(newer / 'output.mp3', (3_000, 3_000))
            publish_job(newer, library)
            listing = json.loads((Path(library) / 'library.json').read_text(encoding='utf-8'))
            self.assertEqual([item['id'] for item in listing['episodes']], ['job-newer', 'job-older'])
            self.assertEqual((Path(library) / 'episodes' / 'job-newer' / 'output.mp3').read_bytes(), b'newer-still')
            status = json.loads((newer / 'private-listen.json').read_text(encoding='utf-8'))
            self.assertTrue(status['ok'])
            self.assertEqual(status['episode_id'], 'job-newer')

    def test_metadata_title_and_video_id_win(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root, title='https://youtu.be/zzzzzzzzzzz')
            write(job / 'metadata.json', {'source_title': '肌酸：该怎么吃', 'video_id': 'dQw4w9WgXcQ'})
            publish_job(job, library)
            episode = json.loads((Path(library) / 'episodes' / job.name / 'episode.json').read_text(encoding='utf-8'))
            self.assertEqual(episode['title'], '肌酸：该怎么吃')
            self.assertEqual(episode['youtube_id'], 'dQw4w9WgXcQ')
            self.assertFalse(looks_like_url_title(episode['title']))

    def test_url_only_candidates_never_publish_as_title(self):
        url = 'https://www.youtube.com/watch?v=Jk7RAkFN4vk'
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root, '6d898d4192de495fafcec86b9c938430', title=url, youtube=url)
            write(job / 'metadata.json', {
                'source_title': url,
                'title': 'https://youtu.be/Jk7RAkFN4vk',
                'channel': 'The Diary Of A CEO',
                'video_id': 'Jk7RAkFN4vk',
                'webpage_url': url,
            })
            publish_job(job, library)
            episode = json.loads((Path(library) / 'episodes' / job.name / 'episode.json').read_text(encoding='utf-8'))
            listing = json.loads((Path(library) / 'library.json').read_text(encoding='utf-8'))
            for title in (episode['title'], listing['episodes'][0]['title']):
                self.assertFalse(looks_like_url_title(title))
                self.assertNotIn('http', title.casefold())
                self.assertNotIn('youtube.com', title.casefold())
                self.assertNotIn('youtu.be', title.casefold())
                self.assertEqual(title, 'The Diary Of A CEO · Jk7RAkFN4vk')

    def test_english_source_title_becomes_chinese_display_title(self):
        url = 'https://www.youtube.com/watch?v=Jk7RAkFN4vk'
        english = 'Creatine Expert: Creatine Is The Secret To Weight Loss'
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root, '6d898d4192de495fafcec86b9c938430', title=url, youtube=url)
            seen = []

            def translator(topic):
                seen.append(topic)
                return '肌酸专家：肌酸是减肥的秘密'

            remember_source_title(
                job,
                source_title=english,
                video_id='Jk7RAkFN4vk',
                channel='The Diary Of A CEO',
                webpage_url=url,
                translator=translator,
            )
            publish_job(job, library)
            episode = json.loads((Path(library) / 'episodes' / job.name / 'episode.json').read_text(encoding='utf-8'))
            metadata = json.loads((job / 'metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(seen, [english])
            self.assertNotIn('Diary', seen[0])
            self.assertEqual(metadata['source_title'], english)
            self.assertEqual(metadata['title_zh'], '肌酸专家：肌酸是减肥的秘密')
            self.assertEqual(metadata['title_status'], 'translated')
            self.assertEqual(episode['title'], '肌酸专家：肌酸是减肥的秘密 · The Diary Of A CEO')
            self.assertIn('The Diary Of A CEO', episode['title'])
            self.assertFalse(looks_like_url_title(episode['title']))
            self.assertTrue(any('\u4e00' <= char <= '\u9fff' for char in episode['title']))

    def test_missing_translator_falls_back_without_publishing_a_url(self):
        url = 'https://www.youtube.com/watch?v=Jk7RAkFN4vk'
        english = 'Creatine Expert: Creatine Is The Secret To Weight Loss | The Diary Of A CEO'
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root, title=url, youtube=url)
            remember_source_title(
                job,
                source_title=english,
                channel='The Diary Of A CEO',
                video_id='Jk7RAkFN4vk',
                translator=lambda topic: None,
            )
            publish_job(job, library)
            episode = json.loads((Path(library) / 'episodes' / job.name / 'episode.json').read_text(encoding='utf-8'))
            metadata = json.loads((job / 'metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['title_status'], 'untranslated')
            self.assertNotIn('display_title', metadata)
            self.assertEqual(episode['title'], 'Creatine Expert: Creatine Is The Secret To Weight Loss · The Diary Of A CEO')
            self.assertFalse(looks_like_url_title(episode['title']))
            self.assertNotIn('http', episode['title'].casefold())

    def test_refresh_title_rewrites_title_without_changing_audio(self):
        url = 'https://www.youtube.com/watch?v=Jk7RAkFN4vk'
        english = 'Creatine Expert: Creatine Is The Secret To Weight Loss'
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root, '6d898d4192de495fafcec86b9c938430', title=url, youtube=url)
            before = (job / 'output.mp3').read_bytes()
            fetched = {
                'source_title': english,
                'channel': 'The Diary Of A CEO',
                'video_id': 'Jk7RAkFN4vk',
                'webpage_url': url,
            }
            with patch('studio_publish_icloud.fetch_youtube_metadata', return_value=fetched), patch('studio_publish_icloud.translate_episode_topic', return_value='肌酸专家：肌酸是减肥的秘密'):
                self.assertEqual(main(['--job-dir', str(job), '--library', library, '--refresh-title']), 0)
            self.assertEqual((job / 'output.mp3').read_bytes(), before)
            episode = json.loads((Path(library) / 'episodes' / job.name / 'episode.json').read_text(encoding='utf-8'))
            self.assertEqual(episode['title'], '肌酸专家：肌酸是减肥的秘密 · The Diary Of A CEO')
            self.assertEqual((Path(library) / 'episodes' / job.name / 'output.mp3').read_bytes(), before)

    def test_oembed_stores_human_title_and_ignores_a_url_title(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({
                    'title': 'Creatine Expert: Creatine Is The Secret To Weight Loss',
                    'author_name': 'The Diary Of A CEO',
                }).encode()

        with patch('studio_publish_icloud.urllib.request.urlopen', return_value=FakeResponse()):
            meta = fetch_youtube_metadata('https://youtu.be/Jk7RAkFN4vk', allow_ytdlp=False)
        self.assertEqual(meta['source_title'], 'Creatine Expert: Creatine Is The Secret To Weight Loss')
        self.assertEqual(meta['channel'], 'The Diary Of A CEO')
        self.assertEqual(meta['video_id'], 'Jk7RAkFN4vk')
        self.assertFalse(looks_like_url_title(meta['source_title']))

        class UrlTitle:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({'title': 'https://www.youtube.com/watch?v=Jk7RAkFN4vk', 'author_name': 'The Diary Of A CEO'}).encode()

        with patch('studio_publish_icloud.urllib.request.urlopen', return_value=UrlTitle()):
            rejected = fetch_youtube_metadata('https://www.youtube.com/watch?v=Jk7RAkFN4vk')
        self.assertNotIn('source_title', rejected)
        self.assertEqual(rejected['channel'], 'The Diary Of A CEO')

    def test_translate_hook_without_api_key_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(translate_episode_topic('Creatine is the secret to weight loss', provider='deepseek'))
        self.assertEqual(
            compose_display_title('肌酸专家：肌酸是减肥的秘密', 'Huberman'),
            '肌酸专家：肌酸是减肥的秘密 · Huberman',
        )

    def test_bare_http_url_without_video_id_falls_back_to_job_id(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root, 'job-plain-url', title='https://example.com/episode', youtube='https://example.com/episode')
            write(job / 'config.json', {'source': {'kind': 'youtube', 'value': 'https://example.com/episode'}})
            publish_job(job, library)
            episode = json.loads((Path(library) / 'episodes' / job.name / 'episode.json').read_text(encoding='utf-8'))
            self.assertEqual(episode['title'], 'job-plain-url')
            self.assertFalse(looks_like_url_title(episode['title']))

    def test_rejects_missing_symlink_and_bad_id(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            empty = Path(root) / 'job-empty'
            empty.mkdir()
            with self.assertRaises(PublishError):
                publish_job(empty, library)
            job = self.make_job(root, 'job-link')
            target = job / 'real.mp3'
            target.write_bytes(b'x')
            (job / 'output.mp3').unlink()
            (job / 'output.mp3').symlink_to(target)
            with self.assertRaises(PublishError):
                publish_job(job, library)
            spaced = Path(root) / 'bad id'
            spaced.mkdir()
            (spaced / 'output.mp3').write_bytes(b'x')
            with self.assertRaises(PublishError):
                publish_job(spaced, library)
            self.assertEqual(main(['--job-dir', str(empty), '--library', library]), 1)
            self.assertFalse(json.loads((empty / 'private-listen.json').read_text(encoding='utf-8'))['ok'])

    def test_duration_probe_skips_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / 'output.mp3'
            audio.write_bytes(b'not-audio')
            with patch('studio_publish_icloud.shutil.which', return_value=None):
                self.assertIsNone(probe_duration_sec(audio))

    def test_cli_backfill(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root)
            stdout = io.StringIO()
            with patch('sys.stdout', stdout):
                self.assertEqual(main(['--job-dir', str(job), '--library', library]), 0)
            self.assertIn(job.name, stdout.getvalue())
            self.assertTrue((Path(library) / 'library.json').is_file())


class EngineHookTests(unittest.TestCase):
    def ready_engine(self, root):
        engine = StudioEngine(Path(root) / '6d898d4192de495fafcec86b9c938430')
        (engine.job / 'document.json').write_text('{}', encoding='utf-8')
        write(engine.job / 'state.json', {'title': '肌酸对谈'})
        config = {'source': {'kind': 'youtube', 'value': 'https://www.youtube.com/watch?v=MM-C3JqCXBk'}}
        return engine, config

    def test_full_render_publishes_preview_and_prepare_do_not(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            engine, config = self.ready_engine(root)
            mp3 = engine.job / 'output.mp3'
            mp3.write_bytes(b'full-audio')
            with patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': library}):
                with patch.object(engine, 'render', return_value=mp3):
                    self.assertEqual(engine.run(config, 'render'), mp3)
                published = Path(library) / 'episodes' / engine.job.name / 'output.mp3'
                self.assertEqual(published.read_bytes(), b'full-audio')
                progress = json.loads((engine.job / 'progress.json').read_text(encoding='utf-8'))
                self.assertEqual(progress['stage'], 'complete')
                self.assertTrue(json.loads((engine.job / 'private-listen.json').read_text(encoding='utf-8'))['ok'])
                self.assertFalse(engine.temp.exists())
                preview = engine.job / 'preview.mp3'
                preview.write_bytes(b'preview-only')
                with patch.object(engine, 'render', return_value=preview) as render:
                    engine.run(config, 'render', preview=2)
                    render.assert_called_once()
                self.assertEqual(published.read_bytes(), b'full-audio')
                with patch.object(engine, 'acquire', return_value=engine.job / 'missing.wav'), patch.object(engine, 'prepare', return_value={'segments': []}), patch('studio_publish_icloud.publish_job') as publish:
                    self.assertIsNone(engine.run(config, 'prepare'))
                    publish.assert_not_called()

    def test_publish_failure_keeps_job_successful(self):
        with tempfile.TemporaryDirectory() as root:
            engine, config = self.ready_engine(root)
            mp3 = engine.job / 'output.mp3'
            mp3.write_bytes(b'full-audio')
            with patch.object(engine, 'render', return_value=mp3), patch('studio_publish_icloud.publish_job', side_effect=OSError('disk full')):
                self.assertEqual(engine.run(config, 'render'), mp3)
            progress = json.loads((engine.job / 'progress.json').read_text(encoding='utf-8'))
            self.assertEqual(progress['stage'], 'complete')
            self.assertNotIn('disk full', json.dumps(progress))
            status = json.loads((engine.job / 'private-listen.json').read_text(encoding='utf-8'))
            self.assertFalse(status['ok'])
            self.assertIn('disk full', status['error'])


class ServerHookTests(unittest.TestCase):
    def test_retry_only_when_full_output_is_unpublished(self):
        with tempfile.TemporaryDirectory() as jobs, tempfile.TemporaryDirectory() as library:
            studio = Studio(jobs)
            job = Path(jobs) / 'abc123'
            job.mkdir()
            (job / 'output.mp3').write_bytes(b'audio')
            state = {'action': 'render', 'preview': False}
            self.assertTrue(studio.private_listen_needs_retry(job, state))
            self.assertFalse(studio.private_listen_needs_retry(job, {'action': 'render', 'preview': True}))
            self.assertFalse(studio.private_listen_needs_retry(job, {'action': 'prepare', 'preview': False}))
            link = Path(jobs) / 'linked'
            link.mkdir()
            (link / 'output.mp3').symlink_to(job / 'output.mp3')
            self.assertFalse(studio.private_listen_needs_retry(link, state))
            with patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': library}):
                studio.retry_private_listen(job)
            self.assertEqual((Path(library) / 'episodes' / 'abc123' / 'output.mp3').read_bytes(), b'audio')
            self.assertFalse(studio.private_listen_needs_retry(job, state))
            before = read(job / 'state.json')
            studio.retry_private_listen(link)
            self.assertEqual(read(job / 'state.json'), before)


if __name__ == '__main__':
    unittest.main()
