#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_publish_icloud import PublishError, publish_job


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
            library.mkdir()
            restored = {'n': 0}

            def restore(snap, library):
                restored['n'] += 1

            with patch('studio_publish_icloud._require_audio_preflight', return_value={'verdict': 'CLEAR'}):
                with patch('studio_publish_icloud._snapshot_library_or_refuse', return_value='/tmp/fake-snap'):
                    with patch('studio_publish_icloud._restore_library_snapshot', side_effect=restore):
                        with patch('studio_publish_icloud._discard_library_snapshot', return_value=None):
                            with patch('studio_publish_icloud._publish_job_locked', side_effect=PublishError('write failed')):
                                with self.assertRaises(PublishError):
                                    publish_job(job, library)
            self.assertEqual(restored['n'], 1)


if __name__ == '__main__':
    unittest.main()
