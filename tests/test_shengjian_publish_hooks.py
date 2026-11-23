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
from shengjian_audio_preflight import PreflightError
from studio_publish_icloud import PublishError, main, publish_job


FIXTURE_AUDIO = (Path(__file__).resolve().parents[1] / 'fixtures' / 'point-listen' /
                 'episodes' / 'point-listen-isolated' / 'output.mp3')


def library_bytes(library):
    return {path.relative_to(library): path.read_bytes()
            for path in library.rglob('*') if path.is_file()}


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs FFmpeg')
class PublishQualityEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        scratch = Path(temporary.name).resolve()
        self.job = scratch / 'fixture-job'
        self.job.mkdir()
        shutil.copyfile(FIXTURE_AUDIO, self.job / 'output.mp3')
        self.library = scratch / 'explicit-library'
        old = self.library / 'episodes' / 'old'
        old.mkdir(parents=True)
        (old / 'output.mp3').write_bytes(b'old media bytes')
        (self.library / 'library.json').write_bytes(b'{"episodes":[{"id":"old"}]}\n')
        (self.library / 'user-state.json').write_bytes(b'{"fixture_progress":13}\n')
        self.fallback = scratch / 'unused-library'

    def assert_quality_refusal(self, error_fragment):
        before = library_bytes(self.library)
        stderr = io.StringIO()
        with patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': str(self.fallback)}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            status = main(['--job-dir', str(self.job), '--library', str(self.library), '--no-device'])
        self.assertEqual(status, 1, stderr.getvalue())
        self.assertIn(error_fragment, stderr.getvalue())
        self.assertEqual(library_bytes(self.library), before)
        self.assertFalse((self.library / 'episodes' / self.job.name).exists())
        self.assertFalse(self.fallback.exists())
        self.assertFalse(json.loads((self.job / 'private-listen.json').read_bytes())['ok'])
        return json.loads((self.job / 'preflight' / 'preflight-report.json').read_bytes())

    def test_missing_segment_receipt_refuses_publish_and_still_writes_inspection_mp3(self):
        report = self.assert_quality_refusal('音频预检待人耳')
        self.assertEqual(report['verdict'], 'REVIEW')
        evidence = next(item for item in report['findings'] if item['code'] == 'segment_integrity')
        self.assertEqual(evidence['severity'], 'REVIEW')
        self.assertIn('缺少逐段', evidence['reason'])
        preview = self.job / 'preflight' / 'inspect-45s.mp3'
        self.assertEqual(Path(report['preview']['path']), preview)
        self.assertGreater(preview.stat().st_size, 0)
        self.assertFalse(report['publish_ready'])
        self.assertFalse(report['audio_quality_pass'])
        self.assertTrue(report['ear_required'])

    def test_inspection_failure_alone_refuses_publish(self):
        # Isolate the existing inspection rule from the separately tested segment gate.
        with patch('studio_publish_icloud.segment_integrity',
                   return_value={'code': 'segment_integrity', 'severity': 'CLEAR'}), \
             patch('shengjian_audio_preflight.write_preview',
                   side_effect=PreflightError('injected inspection failure')) as inspect:
            report = self.assert_quality_refusal('音频预检待人耳')
        inspect.assert_called_once()
        self.assertEqual(report['verdict'], 'REVIEW')
        self.assertEqual([item for item in report['findings'] if item['severity'] == 'REVIEW'],
                         [{'code': 'preview', 'severity': 'REVIEW',
                           'reason': 'injected inspection failure'}])
        self.assertIsNone(report['preview'])

    def test_corrupt_source_is_hard_blocked(self):
        (self.job / 'output.mp3').write_bytes(b'not an audio file')
        report = self.assert_quality_refusal('音频预检硬拦')
        self.assertEqual(report['verdict'], 'BLOCK')
        self.assertEqual(report['findings'][0]['code'], 'decode')
        self.assertIsNone(report['preview'])


class PublishTransactionTests(unittest.TestCase):
    def test_append_and_catalog_swap_failure_with_preflight_stub(self):
        with tempfile.TemporaryDirectory() as raw:
            scratch = Path(raw).resolve()
            job = scratch / 'fixture-job'
            job.mkdir()
            source = job / 'output.mp3'
            shutil.copyfile(FIXTURE_AUDIO, source)
            library = scratch / 'explicit-library'
            library.mkdir()
            listing = library / 'library.json'
            old = {'id': 'in-transit', 'resume_seconds': 12}
            existing = {'id': 'existing', 'title': 'Existing fixture', 'created_at': '2025-01-01',
                        'source_job_id': 'existing', 'duration_sec': 3, 'file': 'output.mp3',
                        'resume_seconds': 2}
            old_dir = library / 'episodes' / 'existing'
            old_dir.mkdir(parents=True)
            (old_dir / 'output.mp3').write_bytes(b'old media bytes')
            (old_dir / 'episode.json').write_text(json.dumps(existing))
            (library / 'user-state.json').write_bytes(b'{"fixture_progress":13}\n')
            listing.write_text(json.dumps({'episodes': [old, existing], 'settings': {'pinned': True}}))
            old_files = library_bytes(library)

            # Test only the directory transaction, assuming an authorized preflight already passed.
            with patch('studio_publish_icloud._require_audio_preflight',
                       return_value={'verdict': 'CLEAR'}) as preflight:
                publish_job(job, library)
            preflight.assert_called_once_with(source, job)
            catalog = json.loads(listing.read_bytes())
            rows = {row['id']: row for row in catalog['episodes']}
            self.assertEqual(set(rows), {'fixture-job', 'in-transit', 'existing'})
            self.assertEqual(rows['in-transit'], old)
            self.assertEqual(rows['existing'], existing)
            self.assertEqual(catalog['settings'], {'pinned': True})
            for path, content in old_files.items():
                if path != Path('library.json'):
                    self.assertEqual((library / path).read_bytes(), content)
            self.assertEqual((library / 'episodes' / job.name / source.name).read_bytes(), FIXTURE_AUDIO.read_bytes())
            self.assertEqual(source.read_bytes(), FIXTURE_AUDIO.read_bytes())

            before = library_bytes(library)
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

            with patch('studio_publish_icloud._require_audio_preflight',
                       return_value={'verdict': 'CLEAR'}), \
                 patch('os.replace', side_effect=fail_catalog_swap):
                with self.assertRaisesRegex(PublishError, 'injected catalog swap failure'):
                    publish_job(failed_job, library)
            self.assertEqual(library_bytes(library), before)
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
