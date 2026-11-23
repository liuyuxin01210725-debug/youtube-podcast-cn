#!/usr/bin/env python3
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_publish_icloud import PublishError, main, publish_job


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs FFmpeg')
class PublishEntryTests(unittest.TestCase):
    def test_existing_audio_can_publish_to_an_explicit_library(self):
        fixture = (Path(__file__).resolve().parents[1] / 'fixtures' / 'point-listen' /
                   'episodes' / 'point-listen-isolated' / 'output.mp3')
        with tempfile.TemporaryDirectory() as raw:
            scratch = Path(raw).resolve()
            job = scratch / 'fixture-job'
            job.mkdir()
            source = job / 'output.mp3'
            shutil.copyfile(fixture, source)
            library = scratch / 'explicit-library'
            library.mkdir()
            listing = library / 'library.json'
            old = {'id': 'in-transit', 'resume_seconds': 12}
            listing.write_text(json.dumps({'episodes': [old], 'settings': {'pinned': True}}))
            fallback = scratch / 'unused-library'
            stderr = io.StringIO()

            with patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': str(fallback)}), \
                 patch('shengjian_audio_preflight.write_preview', side_effect=AssertionError('preview encoding')), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                status = main(['--job-dir', str(job), '--library', str(library), '--no-device'])
            self.assertEqual(status, 0, stderr.getvalue())
            self.assertFalse(fallback.exists())
            catalog = json.loads(listing.read_bytes())
            rows = {row['id']: row for row in catalog['episodes']}
            self.assertEqual(set(rows), {'fixture-job', 'in-transit'})
            self.assertEqual(rows['in-transit'], old)
            self.assertEqual(catalog['settings'], {'pinned': True})
            self.assertEqual((library / 'episodes' / job.name / source.name).read_bytes(), fixture.read_bytes())
            self.assertEqual(source.read_bytes(), fixture.read_bytes())
            report = json.loads((job / 'preflight' / 'preflight-report.json').read_bytes())
            self.assertEqual(report['verdict'], 'CLEAR')
            self.assertIsNone(report['preview'])
            self.assertFalse(report['audio_quality_pass'])
            self.assertTrue(report['ear_required'])

            before = listing.read_bytes()
            before_inode = listing.stat().st_ino
            failed_job = scratch / 'fixture-failed'
            failed_job.mkdir()
            shutil.copyfile(source, failed_job / source.name)
            installed = library / 'episodes' / failed_job.name
            replace = os.replace

            def fail_catalog_swap(src, dst):
                if dst == listing:
                    self.assertEqual((installed / source.name).read_bytes(), source.read_bytes())
                    raise OSError('injected catalog swap failure')
                return replace(src, dst)

            with patch('os.replace', side_effect=fail_catalog_swap), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                status = main(['--job-dir', str(failed_job), '--library', str(library), '--no-device'])
            self.assertEqual(status, 1)
            self.assertIn('injected catalog swap failure', stderr.getvalue())
            self.assertEqual(listing.read_bytes(), before)
            self.assertEqual(listing.stat().st_ino, before_inode)
            self.assertFalse(installed.exists())


class PublishHookTests(unittest.TestCase):
    def test_publish_job_refuses_block_and_review_and_is_not_orphan(self):
        with tempfile.TemporaryDirectory() as raw:
            job = Path(raw) / '6d898d4192de495fafcec86b9c938430'
            job.mkdir()
            (job / 'output.mp3').write_bytes(b'not-audio')
            library = Path(raw) / 'library'
            library.mkdir()
            called = {'n': 0}

            def fake_preflight(src, job_dir):
                called['n'] += 1
                raise PublishError('音频预检硬拦，拒绝发布 ready: hook-test')

            with patch('studio_publish_icloud._require_audio_preflight', side_effect=fake_preflight):
                with self.assertRaises(PublishError) as ctx:
                    publish_job(job, library)
            self.assertIn('硬拦', str(ctx.exception))
            self.assertEqual(called['n'], 1)
            self.assertFalse((library / 'library.json').exists())

    def test_snapshot_failure_refuses_merge(self):
        with tempfile.TemporaryDirectory() as raw:
            job = Path(raw) / '6d898d4192de495fafcec86b9c938430'
            job.mkdir()
            (job / 'output.mp3').write_bytes(b'x')
            library = Path(raw) / 'library'
            library.mkdir()
            with patch('studio_publish_icloud._require_audio_preflight', return_value={'verdict': 'CLEAR'}):
                with patch('studio_publish_icloud._snapshot_library_or_refuse', side_effect=PublishError('备份失败，拒绝合并: test')):
                    with self.assertRaises(PublishError) as ctx:
                        publish_job(job, library)
            self.assertIn('备份失败', str(ctx.exception))
            self.assertFalse((library / 'library.json').exists())

    def test_fault_during_publish_restores_snapshot(self):
        with tempfile.TemporaryDirectory() as raw:
            job = Path(raw) / '6d898d4192de495fafcec86b9c938430'
            job.mkdir()
            (job / 'output.mp3').write_bytes(b'x')
            library = Path(raw) / 'library'
            legacy = library / 'episodes' / job.name
            legacy.mkdir(parents=True)
            (legacy / 'output.mp3').write_bytes(b'x')
            (legacy / 'episode.json').write_bytes(b'{"title":"before"}')
            catalog = library / 'library.json'
            catalog.write_bytes(b'{"episodes":[]}')
            with patch('studio_publish_icloud._require_audio_preflight', return_value={'verdict': 'CLEAR'}):
                with patch('studio_publish_icloud._write_snapshot', side_effect=OSError('write failed')):
                    with self.assertRaisesRegex(PublishError, 'write failed'):
                        publish_job(job, library)
            self.assertEqual((legacy / 'episode.json').read_bytes(), b'{"title":"before"}')
            self.assertEqual(catalog.read_bytes(), b'{"episodes":[]}')


if __name__ == '__main__':
    unittest.main()
