"""Synthetic Studio recovery tests. No fixture contains real episode content."""

import asyncio
from contextlib import ExitStack, closing
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError, URLError

import edge_tts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import podcast_cn
import studio_publish_icloud as publisher
import studio_recovery
from diarization_core import DiarizationTurn, SpeakerAssignment
from podcast_captions import CaptionPiece, PodcastPipelineError
from studio_document import segment_evidence_hash
from studio_engine import Cancelled, StudioEngine, StudioInputError, failure_details
from studio_recovery import StudioRecoveryError, podcast_job_lock
from translation_budget import FEE_MODE, MODEL, POLICY_EXPIRES_AT, POLICY_VERIFIED_AT, TranslationBudget
from studio_server import Studio

SECRET = 'synthetic-credential-do-not-save'
REMOTE_ID = '12345678-1234-4123-8123-1234567890ab'


class StudioRecoveryTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'fixture.wav'
        self.source.write_bytes(b'synthetic source')
        self.config = {
            'source': {'kind': 'local', 'value': str(self.source)},
            'expected_speakers': 1,
        }
        self.job = self.root / ('a' * 32)
        self.job.mkdir()
        (self.job / 'config.json').write_text(json.dumps(self.config))
        self.now = POLICY_VERIFIED_AT + 60
        TranslationBudget.provision(
            self.job, authorization_id='12345678-1234-4123-8123-1234567890cd',
            fee_mode=FEE_MODE, cap_microunits=10_000_000, prior_microunits=0,
            verified_at=POLICY_VERIFIED_AT, expires_at=self.now + 60,
            clock=lambda: self.now,
        )
        self.pieces = [
            CaptionPiece(n * 1000, (n + 1) * 1000, f'Synthetic caption {n} ' + 'x' * 4500, n)
            for n in range(3)
        ]
        self.calls = []
        self.acquisitions = 0
        self.transcriptions = 0
        self.credential_reads = 0
        self.failed_ids = {'seg-000002'}

    def fake_opener(self, request, timeout):
        body = json.loads(request.data)
        rows = json.loads(body['messages'][-1]['content'])
        ids = [row['id'] for row in rows]
        self.calls.append(ids)
        if self.failed_ids.intersection(ids):
            raise HTTPError(
                request.full_url, 429, SECRET, {'x-request-id': REMOTE_ID},
                io.BytesIO(json.dumps({'error': {
                    'code': 'rate_limit_exceeded',
                    'message': f'{SECRET} {rows[0]["text"][:80]} https://example.invalid/?token=secret',
                }}).encode()),
            )
        answer = {'translations': [{'id': ident, 'text': f'合成译文{ident[-1]}'} for ident in ids]}
        response = io.BytesIO(json.dumps({
            'id': REMOTE_ID, 'model': body['model'],
            'usage': {'prompt_tokens': 50, 'completion_tokens': 10, 'total_tokens': 60},
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}],
        }).encode())
        response.status = 200
        response.headers = {'x-request-id': REMOTE_ID}
        return response

    def run_studio(self, *, opener=None, action='prepare', ambiguous=False, credential=SECRET):
        engine = StudioEngine(self.job)

        def acquire(_):
            self.acquisitions += 1
            wav = engine.temp / 'source.wav'
            wav.write_bytes(b'synthetic decoded audio')
            return wav

        def transcribe(_wav, _config):
            self.transcriptions += 1
            return self.pieces, 'asr'

        def load_credential(_provider):
            self.credential_reads += 1
            return credential

        with ExitStack() as stack:
            for name, options in {
                'acquire': {'side_effect': acquire},
                'transcribe': {'side_effect': transcribe},
                'duration_ms': {'return_value': 3000},
                'ffmpeg': {'side_effect': lambda *args: Path(args[-1]).write_bytes(b'synthetic reference')},
                'diarize_with_progress': {'return_value': [DiarizationTurn(0, 3000, 'host')]},
                'credential': {'side_effect': load_credential},
                'publish_private_library': {'side_effect': AssertionError('unexpected publication')},
                'push_private_device': {'return_value': None},
            }.items():
                stack.enter_context(patch.object(engine, name, **options))
            stack.enter_context(patch.dict(os.environ, {'DEEPSEEK_MODEL': MODEL}, clear=True))
            stack.enter_context(patch(
                'studio_engine.TranslationBudget',
                side_effect=lambda job: TranslationBudget(job, clock=lambda: self.now),
            ))
            stack.enter_context(patch('studio_engine.build_backend', side_effect=AssertionError('unexpected TTS')))
            if ambiguous:
                assignments = [
                    SpeakerAssignment(piece.source_index, 'host', 500, .5, True, 'synthetic')
                    for piece in self.pieces
                ]
                stack.enter_context(patch('studio_engine.align_caption_pieces', return_value=assignments))
            stack.enter_context(patch(
                'studio_engine.create_translation_client',
                side_effect=lambda provider, **kwargs: podcast_cn.create_translation_client(
                    provider, opener=opener or self.fake_opener, **kwargs
                ),
            ))
            return engine.run(self.config, action)

    def test_failure_resumes_exact_checkpoint_and_does_not_resubmit_unknown(self):
        with self.assertRaises(PodcastPipelineError) as first:
            self.run_studio()
        cache = self.job / 'translations/chunk-0001.json'
        self.assertTrue(cache.is_file())
        before = cache.read_bytes()
        debug = (self.job / 'debug-failure.json').read_text()
        self.assertNotIn(SECRET, debug)
        self.assertNotIn('Synthetic caption', debug)
        with self.assertRaises(PodcastPipelineError) as blocked:
            self.run_studio(credential=None)
        self.assertEqual(cache.read_bytes(), before)
        observed = {
            'http_status': getattr(first.exception, 'evidence', {}).get('http_status'),
            'provider_request_id': getattr(first.exception, 'evidence', {}).get('provider_request_id'),
            'recovery_checkpoint_exists': (self.job / 'translation-source.json').is_file(),
            'acquisitions': self.acquisitions,
            'transcriptions': self.transcriptions,
            'calls': self.calls,
        }
        self.assertEqual(observed, {
            'http_status': 429, 'provider_request_id': REMOTE_ID,
            'recovery_checkpoint_exists': True,
            'acquisitions': 1, 'transcriptions': 1,
            'calls': [['seg-000001'], ['seg-000002']],
        })

        evidence = first.exception.evidence
        receipt_path = self.job / 'translation-requests' / evidence['receipt']
        original_receipt = receipt_path.read_bytes()
        receipt = json.loads(original_receipt)
        self.assertEqual(receipt['local_request_id'], evidence['local_request_id'])
        self.assertEqual(receipt['billing_state'], 'unknown')
        self.assertEqual(receipt['completion_state'], 'unknown')
        self.assertEqual(receipt['provider_error_code'], 'rate_limit_exceeded')
        for excluded in (SECRET, 'Synthetic caption', 'token=secret'):
            self.assertNotIn(excluded, original_receipt.decode())

        self.assertEqual(blocked.exception.evidence['local_request_id'], evidence['local_request_id'])
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.assertIn('seg-000002', (self.job / 'progress.json').read_text())
        with closing(sqlite3.connect(self.job / TranslationBudget.FILENAME)) as ledger:
            reservations = ledger.execute('SELECT local_id, state FROM requests ORDER BY rowid').fetchall()
        self.assertEqual([state for _, state in reservations], ['completed', 'uncertain'])
        self.assertEqual(reservations[-1][0], receipt['local_request_id'])

    def test_two_resumes_point_to_original_429_without_rewriting_receipt_or_diagnosis(self):
        self.pieces = self.pieces[:1]
        self.failed_ids = {'seg-000001'}
        opener = Mock(side_effect=self.fake_opener)
        with self.assertRaises(StudioRecoveryError) as first:
            self.run_studio(opener=opener)
        failure = first.exception
        self.assertEqual(failure.code, 'translation_http_error')
        original = failure.evidence
        receipt = self.job / 'translation-requests' / original['receipt']
        debug = self.job / 'debug-failure.json'
        prior_receipt, prior_debug = receipt.read_bytes(), debug.read_bytes()
        blocked_event = self.job / 'translation-retry-blocked.json'

        for _ in range(2):
            with self.assertRaises(StudioRecoveryError) as retry:
                self.run_studio(opener=opener, credential=None)
            self.assertEqual(retry.exception.code, 'translation_request_already_recorded')
            evidence = retry.exception.evidence
            self.assertEqual(evidence['request_state'], 'blocked_existing')
            self.assertEqual(evidence['original_request_state'], 'uncertain')
            for key in ('receipt', 'local_request_id', 'http_status', 'provider_request_id'):
                self.assertEqual(evidence[key], original[key])
            self.assertEqual((evidence['http_status'], evidence['provider_request_id']), (429, REMOTE_ID))
            event = json.loads(blocked_event.read_bytes())
            self.assertEqual(event['error_code'], retry.exception.code)
            self.assertEqual(event['translation_request'], evidence)
            self.assertEqual(receipt.read_bytes(), prior_receipt)
            self.assertEqual(debug.read_bytes(), prior_debug)
            self.assertEqual(self.credential_reads, 1)
            opener.assert_called_once()

    def test_expired_studio_uncached_translation_makes_no_credential_or_opener_calls(self):
        self.now = POLICY_EXPIRES_AT
        opener = Mock(side_effect=AssertionError('unexpected opener'))
        with self.assertRaises(StudioRecoveryError) as expired:
            self.run_studio(opener=opener)
        self.assertEqual(expired.exception.code, 'translation_allowance_invalid')
        self.assertEqual(self.credential_reads, 0)
        opener.assert_not_called()
        self.assertFalse((self.job / 'translation-requests').exists())
        self.assertFalse((self.job / 'document.json').exists())
        self.assertEqual(json.loads((self.job / 'progress.json').read_bytes())['stage'], 'failed')

    def test_successful_prepare_archives_official_failure_bytes(self):
        self.pieces = self.pieces[:1]
        self.failed_ids.clear()
        current = self.now
        self.now = POLICY_EXPIRES_AT
        with self.assertRaises(StudioRecoveryError) as failed:
            self.run_studio()
        self.assertEqual(failed.exception.code, 'translation_allowance_invalid')
        debug = self.job / 'debug-failure.json'
        original = debug.read_bytes()
        archived = self.job / 'debug-failures' / (hashlib.sha256(original).hexdigest() + '.json')

        self.now = current
        self.run_studio()

        self.assertFalse(debug.exists())
        self.assertEqual(archived.read_bytes(), original)
        self.assertEqual(json.loads(original)['stage'], 'translation')
        self.assertTrue((self.job / 'document.json').is_file())
        self.assertEqual(json.loads((self.job / 'progress.json').read_bytes())['stage'], 'complete')

    def test_later_failure_archives_original_and_failed_archive_keeps_active_bytes(self):
        engine = StudioEngine(self.job)
        engine.progress('translation')
        engine.record_failure(StudioRecoveryError('synthetic_failure', 'Synthetic translation failure'))
        debug = self.job / 'debug-failure.json'
        original = debug.read_bytes()
        archive = self.job / 'debug-failures'
        archive.write_bytes(b'synthetic obstruction')

        engine.progress('export')
        engine.record_failure(RuntimeError('synthetic later failure'))
        self.assertEqual(debug.read_bytes(), original)

        archive.unlink()
        engine.record_failure(RuntimeError('synthetic later failure'))
        self.assertEqual(json.loads(debug.read_bytes())['stage'], 'export')
        archived, = archive.iterdir()
        self.assertEqual(archived.name, hashlib.sha256(original).hexdigest() + '.json')
        self.assertEqual(archived.read_bytes(), original)

    def test_receipt_write_crossing_deadline_stops_before_the_opener(self):
        # The durable receipt can take long enough to cross a valid allowance's deadline.
        expires_at = self.now + 60
        save = studio_recovery._save
        opener = Mock(side_effect=AssertionError('unexpected opener'))

        def save_and_cross_deadline(path, value):
            save(path, value)
            if path.parent.name == 'translation-requests':
                self.now = expires_at

        with patch.object(studio_recovery, '_save', side_effect=save_and_cross_deadline):
            with self.assertRaises(StudioRecoveryError) as expired:
                self.run_studio(opener=opener)
        self.assertEqual(expired.exception.code, 'translation_allowance_invalid')
        self.assertEqual(self.credential_reads, 1)  # Still within allowance when admitted.
        opener.assert_not_called()
        receipt, = (self.job / 'translation-requests').glob('*.json')
        evidence = json.loads(receipt.read_bytes())
        self.assertEqual((evidence['request_state'], evidence['billing_state']), ('not_sent', 'no_charge'))
        with closing(sqlite3.connect(self.job / TranslationBudget.FILENAME)) as ledger:
            (state, raw), = ledger.execute('SELECT state, evidence FROM requests')
        self.assertEqual(state, 'completed')
        self.assertFalse(json.loads(raw)['billing_uncertain'])


    def interrupt_after_cache(self, *, ambiguous=False):
        writer = podcast_cn._write_new_chunk

        def stop(path, value):
            writer(path, value)
            if path.name == 'chunk-0001.json':
                raise Cancelled('synthetic stop between requests')

        with patch.object(podcast_cn, '_write_new_chunk', side_effect=stop):
            with self.assertRaises(Cancelled):
                self.run_studio(ambiguous=ambiguous)
        self.assertEqual(self.calls, [['seg-000001']])

    def test_resume_missing_chunks_then_confirm_one_using_current_document_cas(self):
        self.failed_ids.clear()
        self.interrupt_after_cache(ambiguous=True)
        chunk = self.job / 'translations/chunk-0001.json'
        original = chunk.read_bytes()
        self.run_studio()
        self.assertEqual(self.acquisitions, 1)
        self.assertEqual(self.transcriptions, 1)
        self.assertEqual(self.calls, [['seg-000001'], ['seg-000002'], ['seg-000003']])
        self.assertEqual(chunk.read_bytes(), original)
        self.assertEqual(len(list((self.job / 'translation-requests').glob('*.json'))), 3)

        path = self.job / 'document.json'
        document_bytes = path.read_bytes()
        digest = hashlib.sha256(document_bytes).hexdigest()
        doc = json.loads(document_bytes)
        self.assertEqual([item['ambiguous'] for item in doc['segments']], [True, True, True])
        self.assertEqual([item['id'] for item in doc['segments']], [f'seg-{n:06d}' for n in range(1, 4)])
        item = {
            'id': doc['segments'][0]['id'], 'speaker_id': 'host', 'official_speaker': 'synthetic host',
            'official_source_sha256': hashlib.sha256(b'synthetic official evidence').hexdigest(),
            'official_locator': 'synthetic position', 'official_excerpt': 'synthetic corroboration',
        }
        item['evidence_hash'] = segment_evidence_hash(digest, doc['segments'][0], item)
        studio = Studio(self.root, recover=False)
        ident = self.job.name
        bad = studio.confirm_segments(ident, {
            'document_sha256': digest, 'items': [{**item, 'official_excerpt': 'changed'}],
        })
        self.assertEqual(bad['rejected'][0]['reason'], 'evidence_hash_mismatch')
        self.assertEqual(path.read_bytes(), document_bytes)
        outcome = studio.confirm_segments(ident, {'document_sha256': digest, 'items': [item]})
        self.assertEqual(outcome['applied'], [item['id']])
        reviewed_bytes = path.read_bytes()
        self.assertEqual([row['ambiguous'] for row in json.loads(reviewed_bytes)['segments']], [False, True, True])
        with self.assertRaisesRegex(ValueError, 'document_sha256'):
            studio.confirm_segments(ident, {'document_sha256': digest, 'items': [item]})
        with self.assertRaisesRegex(StudioInputError, '2 段'):
            self.run_studio(action='render')
        with self.assertRaisesRegex(StudioRecoveryError, '复核记录'):
            self.run_studio()
        self.assertEqual(path.read_bytes(), reviewed_bytes)

    def test_malformed_later_cache_blocks_before_an_earlier_gap(self):
        self.failed_ids.clear()
        self.interrupt_after_cache()
        cache = self.job / 'translations/chunk-0003.json'
        cache.write_bytes(bytes([255]) + b' malformed synthetic cache')
        before = cache.read_bytes()
        with self.assertRaisesRegex(StudioRecoveryError, '第 3 块'):
            self.run_studio()
        self.assertEqual(cache.read_bytes(), before)
        self.assertEqual(self.calls, [['seg-000001']])

    def test_wrong_cache_binding_is_preserved(self):
        self.failed_ids.clear()
        self.interrupt_after_cache()
        cache = self.job / 'translations/chunk-0001.json'
        good = json.loads(cache.read_bytes())
        for key, value in (
            ('source_hash', 'different'), ('source_ids', ['seg-000004']),
            ('translation_identity', 'different-model'), ('chunk_index', 2),
        ):
            with self.subTest(field=key):
                bad = json.dumps({**good, key: value}).encode()
                cache.write_bytes(bad)
                with self.assertRaisesRegex(StudioRecoveryError, '第 1 块'):
                    self.run_studio()
                self.assertEqual(cache.read_bytes(), bad)
        self.assertEqual(self.calls, [['seg-000001']])

    def test_resume_rejects_changed_source_configuration_and_reference(self):
        self.failed_ids.clear()
        self.interrupt_after_cache()
        checkpoint = self.job / 'translation-source.json'
        original = checkpoint.read_bytes()
        self.source.write_bytes(b'changed synthetic source')
        with self.assertRaisesRegex(StudioInputError, '来源已经改变'):
            self.run_studio()
        self.source.write_bytes(b'synthetic source')
        self.config['provider'] = 'qwen'
        with self.assertRaisesRegex(StudioRecoveryError, '分段或参考样本无法核验'):
            self.run_studio()
        del self.config['provider']
        ref = next((self.job / 'references').glob('*.wav'))
        ref.write_bytes(b'changed synthetic reference')
        with self.assertRaisesRegex(StudioRecoveryError, '分段或参考样本无法核验'):
            self.run_studio()
        self.assertEqual(checkpoint.read_bytes(), original)
        self.assertEqual(self.calls, [['seg-000001']])
        self.assertEqual(self.acquisitions, 1)

    def test_legacy_without_checkpoint_does_not_reconstruct_and_keeps_evidence(self):
        chunks = self.job / 'translations'
        chunks.mkdir()
        cache = chunks / 'chunk-0001.json'
        cache.write_bytes(b'legacy synthetic cache')
        debug = self.job / 'debug-failure.json'
        debug.write_bytes(b'{"stage":"translation","exc_message":"synthetic old error"}')
        with self.assertRaisesRegex(StudioRecoveryError, '缺少可核验的原分段'):
            self.run_studio()
        self.assertEqual(self.acquisitions, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(cache.read_bytes(), b'legacy synthetic cache')
        self.assertEqual(json.loads(debug.read_bytes())['exc_message'], 'synthetic old error')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe required')
    def test_checkpoint_then_interrupted_edge_render_and_isolated_publish(self):
        self.failed_ids.clear()
        self.interrupt_after_cache()
        chunk = self.job / 'translations/chunk-0001.json'
        chunk_before = chunk.read_bytes()
        self.run_studio()
        self.assertEqual(chunk.read_bytes(), chunk_before)
        self.assertEqual(self.acquisitions, 1)
        self.assertEqual(self.transcriptions, 1)
        self.assertEqual(self.calls, [['seg-000001'], ['seg-000002'], ['seg-000003']])

        library = self.root / 'synthetic-library'
        existing = library / 'episodes' / 'existing'
        existing.mkdir(parents=True)
        (existing / 'output.mp3').write_bytes(b'synthetic prior media')
        prior = {'id': 'existing', 'title': '已有节目', 'file': 'output.mp3',
                 'created_at': '2026-01-01', 'duration_sec': 12}
        extensions = {'show': 'Synthetic show', 'lens_ids': ['synthetic-lens']}
        publisher.atomic_write_json(existing / 'episode.json', prior)
        publisher.atomic_write_json(library / 'library.json', {'episodes': [{**prior, **extensions}]})
        debug = self.job / 'debug-failure.json'
        debug.write_text('{"stage":"synthesis","exc_message":"synthetic prior failure"}')
        job = self.job
        calls = []

        # Exercise the real SDK configuration and audio preflight while
        # replacing the network stream with a deterministic audible signal.
        tone = struct.pack('<60h', *(int(4000 * math.sin(math.tau * frame / 60)) for frame in range(60)))

        class OfflineEdge(edge_tts.Communicate):
            def __init__(self, text, voice):
                super().__init__(text, voice)
                calls.append(text)

            async def save(self, filename):
                if len(calls) == 2:
                    Path(filename).write_bytes(b'partial')
                    (job / 'cancel').touch()
                    await asyncio.Event().wait()
                if len(calls) == 3:
                    raise TimeoutError('synthetic Edge timeout')
                with wave.open(filename, 'wb') as audio:
                    audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                    audio.writeframes(tone * (24000 * 12 // 60))

        with (
            patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': str(library)}),
            patch('tts_backends.edge_tts.Communicate', OfflineEdge),
            patch('tts_backends.asyncio.sleep', new_callable=AsyncMock) as backoff,
            patch('studio_push_iphone.push_after_publish') as push,
            patch('studio_engine.create_translation_client', side_effect=AssertionError('unexpected translation')),
        ):
            with self.assertRaises(Cancelled):
                StudioEngine(self.job).run(self.config, 'render')
            self.assertEqual(json.loads((self.job / 'progress.json').read_bytes())['completed'], 1)
            self.assertTrue(debug.exists())
            self.assertFalse((self.job / 'output.mp3').exists())
            push.assert_not_called()
            (self.job / 'cancel').unlink()
            [cached] = (self.job / 'audio-cache').glob('*.wav')
            cache_before = cached.read_bytes()
            output = StudioEngine(self.job).run(self.config, 'render')
            backoff.assert_awaited_once_with(1.0)
            push.assert_called_once()

        self.assertEqual(calls, ['合成译文1', '合成译文2', '合成译文2', '合成译文2', '合成译文3'])
        self.assertEqual(cached.read_bytes(), cache_before)
        self.assertFalse(debug.exists())
        self.assertFalse((self.job / 'temporary').exists())
        self.assertTrue(StudioEngine(self.job).valid_audio(output))
        self.assertAlmostEqual(publisher.probe_duration_sec(output), 36, delta=1)
        self.assertEqual((library / 'episodes' / self.job.name / 'output.mp3').read_bytes(), output.read_bytes())
        index = {row['id']: row for row in json.loads((library / 'library.json').read_bytes())['episodes']}
        self.assertEqual(set(index), {'existing', self.job.name})
        for key, value in extensions.items():
            self.assertEqual(index['existing'][key], value)
        self.assertNotIn('debug-failure', json.dumps(index))

    def test_interrupted_submission_remains_unrepeatable(self):
        def crash(request, timeout):
            self.calls.append('started')
            raise SystemExit(73)

        with self.assertRaises(SystemExit):
            self.run_studio(opener=crash)
        [path] = (self.job / 'translation-requests').glob('*.json')
        before = path.read_bytes()
        self.assertEqual(json.loads(before)['request_state'], 'submitted')
        with self.assertRaisesRegex(StudioRecoveryError, '已有请求记录'):
            self.run_studio()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.calls, ['started'])
        self.assertEqual(self.acquisitions, 1)

    def test_oversized_http_body_never_enters_the_receipt(self):
        def too_large(request, timeout):
            raise HTTPError(request.full_url, 503, SECRET, {},
                            io.BytesIO(SECRET.encode() * 200))

        with self.assertRaises(StudioRecoveryError) as failed:
            self.run_studio(opener=too_large)
        evidence = failed.exception.evidence
        self.assertEqual(evidence['http_status'], 503)
        self.assertEqual(evidence['error_body_summary'], 'omitted')
        self.assertIsNone(evidence['provider_error_code'])
        self.assertNotIn(SECRET, json.dumps(evidence))

    def test_timeout_has_no_fabricated_provider_status_or_usage(self):
        leaked = f'Authorization: Bearer {SECRET} https://example.invalid/?token=private-query private-body'

        def timeout(request, _unused=None, **kwargs):
            self.calls.append('started')
            raise URLError(TimeoutError(errno.ETIMEDOUT, leaked))

        with self.assertRaises(StudioRecoveryError) as failed:
            self.run_studio(opener=timeout)
        evidence = failed.exception.evidence
        self.assertIsNone(evidence['http_status'])
        self.assertIsNone(evidence['provider_request_id'])
        self.assertIsNone(evidence['usage'])
        self.assertEqual(evidence['billing_state'], 'unknown')
        diagnostic = {
            'exception_type': 'URLError', 'phase': 'opener.open',
            'reason': 'timed_out', 'errno': errno.ETIMEDOUT,
        }
        journal = json.loads((self.job / 'translation-requests' / evidence['receipt']).read_text())
        debug = json.loads((self.job / 'debug-failure.json').read_text())
        self.assertEqual(journal['transport_failure'], diagnostic)
        self.assertEqual(evidence['transport_failure'], diagnostic)
        self.assertEqual(debug['translation_request']['transport_failure'], diagnostic)
        self.assertEqual(debug['exc_type'], 'URLError')
        self.assertIn('opener.open', debug['exc_message'])
        self.assertEqual(failure_details(failed.exception), (debug['exc_type'], debug['exc_message']))
        self.assertEqual(debug['error_code'], 'translation_transport_unknown')
        for value in (journal, evidence, debug):
            for secret in ('Authorization', SECRET, 'example.invalid', 'token=', 'private-query', 'private-body'):
                self.assertNotIn(secret, json.dumps(value))
        with self.assertRaises(StudioRecoveryError):
            self.run_studio()
        self.assertEqual(self.calls, ['started'])

    def test_normal_finish_keeps_usage_and_still_needs_a_valid_chunk(self):
        def invalid(request, timeout):
            self.calls.append('sent')
            payload = {
                'id': REMOTE_ID, 'model': MODEL,
                'usage': {'prompt_tokens': 25, 'completion_tokens': 4, 'total_tokens': 29},
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"translations":[]}'}}],
            }
            response = io.BytesIO(json.dumps(payload).encode())
            response.status = 200
            return response

        self.pieces = [
            CaptionPiece(n * 1000, (n + 1) * 1000, f'Synthetic cue {n}.', n)
            for n in range(3)
        ]
        with self.assertRaisesRegex(StudioRecoveryError, '响应结构无法核验'):
            self.run_studio(opener=invalid)
        [path] = (self.job / 'translation-requests').glob('*.json')
        evidence = json.loads(path.read_bytes())
        self.assertEqual(evidence['completion_state'], 'provider_reported_stop')
        self.assertEqual(evidence['provider_response_id'], REMOTE_ID)
        self.assertEqual(evidence['usage']['total_tokens'], 29)
        self.assertEqual(evidence['billing_state'], 'usage_verified')
        self.assertEqual(len(evidence['segment_ids']), 3)
        with self.assertRaisesRegex(StudioRecoveryError, '已有请求记录'):
            self.run_studio()
        self.assertEqual(self.calls, ['sent'])

    def test_subtitle_fallback_does_not_persist_signed_url_or_exception_text(self):
        engine = StudioEngine(self.job)
        wav = self.root / 'empty.wav'
        with wave.open(str(wav), 'wb') as out:
            out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        config = {'source': {'kind': 'web', 'value': 'https://example.invalid/?token=private'}}
        with patch('studio_engine.fetch_video_info', side_effect=RuntimeError(SECRET)):
            self.assertEqual(engine.transcribe(wav, config), ([], 'asr'))
        logs = ''.join((self.job / name).read_text() for name in (
            'debug-subtitle-fallback.json', 'progress.json',
        ))
        self.assertNotIn(SECRET, logs)
        self.assertNotIn('token=private', logs)
        self.assertIn('英文字幕读取或解析失败', logs)

    def test_active_run_lock_prevents_a_second_worker_from_touching_state(self):
        errors = []
        with podcast_job_lock(self.job):
            def concurrent():
                try:
                    self.run_studio()
                except StudioRecoveryError as error:
                    errors.append(error.code)

            worker = threading.Thread(target=concurrent)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ['job_busy'])
        self.assertFalse((self.job / 'progress.json').exists())
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
