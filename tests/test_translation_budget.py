"""Offline, synthetic translation transport and cache tests."""
import io
import json
import multiprocessing
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import podcast_cn
from translation_budget import ENDPOINT, MODEL, POLICY_EXPIRES_AT, POLICY_VERIFIED_AT, TranslationBudget, TranslationRequestError

NOW = 1795049900
ONE_CAP = 2_129_920
TEST_KEY = 'synthetic-key-never-persist'
PROVIDER_ID = '12345678-1234-4123-8123-1234567890ab'


def reply(body, **overrides):
    segments = json.loads(body['messages'][-1]['content'])
    choices = [{
        'finish_reason': 'stop',
        'message': {'content': json.dumps({'translations': [
            {'id': s['id'], 'text': '合成译文'} for s in segments
        ]})},
    }]
    payload = {
        'id': PROVIDER_ID,
        'model': 'deepseek-flash',
        'choices': choices,
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
        **overrides,
    }
    result = io.BytesIO(json.dumps(payload).encode())
    result.status = 200
    result.headers = {'x-request-id': PROVIDER_ID}
    return result


def segment(key='a', text='Synthetic source'):
    return podcast_cn.SubtitleSegment(key, 0, 100, text)


def client(job, opener, **overrides):
    return podcast_cn.DeepSeekClient(
        api_key=TEST_KEY, budget=TranslationBudget(job), opener=opener,
        **{'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com', **overrides},
    )


def create(job, amount=ONE_CAP, **overrides):
    TranslationBudget.provision(job, **{
        'authorization_id': '12345678-1234-4123-8123-1234567890cd',
        'fee_mode': 'cny_token_only',
        'cap_microunits': amount,
        'prior_microunits': 0,
        'verified_at': NOW - 60,
        'expires_at': NOW + 60,
        **overrides,
    })


def process_request(job, key, ready, start, queue, crash=False):
    def deny_network(event, args):
        if event.startswith('socket.'):
            raise AssertionError(event)

    sys.addaudithook(deny_network)
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if crash:
            os._exit(73)
        return reply(json.loads(request.data))

    with patch('translation_budget.time.time', return_value=NOW):
        ready.set()
        if not start.wait(10):
            return
        try:
            client(job, opener).translate([segment(key)])
            queue.put(('ok', calls))
        except TranslationRequestError as error:
            queue.put((error.code, calls))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.job = pathlib.Path(temporary.name)
        clock = patch('translation_budget.time.time', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.requests = []

    def opener(self, request, timeout):
        body = json.loads(request.data)
        self.requests.append(body)
        return reply(body)

    def records(self):
        import sqlite3
        with closing(sqlite3.connect(self.job / TranslationBudget.FILENAME)) as database:
            return [
                (cost, state, json.loads(evidence))
                for cost, state, evidence in database.execute(
                    'SELECT reserved_microunits, state, evidence FROM requests ORDER BY rowid'
                )
            ]

    def assert_blocked(self, translator, code, batch=None):
        with self.assertRaises(TranslationRequestError) as raised:
            translator.translate(batch or [segment()])
        self.assertEqual(raised.exception.code, code)
        return raised.exception.evidence

    def test_no_allowance_and_emma_limit_have_zero_opener_calls(self):
        translator = client(self.job, self.opener)
        self.assert_blocked(translator, 'translation_allowance_missing')
        create(self.job, 870_000)
        self.assert_blocked(translator, 'translation_budget_exhausted')
        self.assertEqual(self.requests, [])
        self.assertEqual(self.records(), [])

    def test_accepted_request_persists_exact_bounded_receipt(self):
        create(self.job)
        translator = client(self.job, self.opener)
        self.assertEqual(translator.translate([segment()]), {'a': '合成译文'})
        self.assertEqual(self.requests[0]['max_tokens'], 4096)
        self.assertEqual(self.requests[0]['thinking'], {'type': 'disabled'})
        [(cost, state, evidence)] = self.records()
        self.assertEqual((cost, state), (ONE_CAP, 'completed'))
        self.assertEqual(evidence['provider_response_id'], PROVIDER_ID)
        self.assertEqual(evidence['provider_request_id'], PROVIDER_ID)
        self.assertEqual(evidence['http_status'], 200)
        self.assertEqual(evidence['usage']['total_tokens'], 150)

    def test_cumulative_and_prior_spend_and_duplicate(self):
        create(self.job, 2 * ONE_CAP, prior_microunits=1)
        first = client(self.job, self.opener)
        first.translate([segment()])
        self.assert_blocked(first, 'translation_request_already_recorded')
        self.assert_blocked(first, 'translation_budget_exhausted', [segment('b')])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.records()), 1)

    def test_title_shares_job_cap_and_has_its_own_output_bound(self):
        create(self.job)

        def opener(request, timeout):
            body = json.loads(request.data)
            self.requests.append(body)
            choice = {'finish_reason': 'stop', 'message': {'content': '{"title":"合成标题"}'}}
            return reply({**body, 'messages': [{'content': '[]'}]}, choices=[choice])

        translator = client(self.job, opener)
        self.assertEqual(translator.translate_title('Synthetic title'), '合成标题')
        self.assertEqual(self.requests[0]['max_tokens'], 512)
        self.assertEqual(self.records()[0][2]['stage'], 'translation_title')
        self.assert_blocked(translator, 'translation_budget_exhausted')
        self.assertEqual(len(self.requests), 1)

    def test_missing_or_expired_allowance_does_not_load_credentials(self):
        from unittest.mock import Mock
        load_key = Mock(return_value=TEST_KEY)
        translator = podcast_cn.DeepSeekClient(
            model='deepseek-flash', base_url='https://api.deepseek.com',
            budget=TranslationBudget(self.job), opener=self.opener, credential_loader=load_key,
        )
        self.assert_blocked(translator, 'translation_allowance_missing')
        create(self.job)
        with patch('translation_budget.time.time', return_value=NOW + 60):
            self.assert_blocked(translator, 'translation_allowance_invalid')
        load_key.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_policy_deadline_blocks_uncached_translation_and_keeps_bound_cache(self):
        now = POLICY_EXPIRES_AT - 1
        clock = lambda: now
        create(self.job, expires_at=POLICY_EXPIRES_AT, clock=clock)
        budget = TranslationBudget(self.job, clock=clock)
        credential = Mock(return_value=TEST_KEY)
        opener = Mock(side_effect=self.opener)
        translator = podcast_cn.DeepSeekClient(
            model=MODEL, base_url=ENDPOINT, budget=budget, opener=opener, credential_loader=credential,
        )
        batch = [segment()]
        chunks = self.job / 'chunks'
        expected = podcast_cn.translate_with_resume(
            batch, chunks, translator.translate, translation_identity=translator.translation_identity,
        )
        self.assertEqual(expected, {'a': '合成译文'})
        credential.assert_called_once_with()
        opener.assert_called_once()
        cache = (chunks / 'chunk-0001.json').read_bytes()
        ledger = self.records()
        self.assertEqual(POLICY_EXPIRES_AT, POLICY_VERIFIED_AT + 86400)

        for now in (POLICY_EXPIRES_AT, POLICY_EXPIRES_AT + 86400):
            with self.subTest(now=now):
                credential.reset_mock()
                opener.reset_mock()
                self.assert_blocked(translator, 'translation_allowance_invalid', [segment('uncached')])
                self.assertEqual(podcast_cn.translate_with_resume(
                    batch, chunks, translator.translate, translation_identity=translator.translation_identity,
                ), expected)
                self.assertEqual((chunks / 'chunk-0001.json').read_bytes(), cache)
                self.assertEqual(self.records(), ledger)
                credential.assert_not_called()
                opener.assert_not_called()
                with self.assertRaises(ValueError):
                    create(self.job, expires_at=now + 1, clock=clock)

    def test_expired_budget_never_reads_the_default_environment_credential(self):
        budget = TranslationBudget(self.job, clock=lambda: POLICY_EXPIRES_AT)
        opener = Mock()
        with patch.object(os, 'getenv', side_effect=AssertionError('unexpected environment read')) as getenv:
            translator = podcast_cn.DeepSeekClient(model=MODEL, base_url=ENDPOINT, budget=budget, opener=opener)
            self.assert_blocked(translator, 'translation_allowance_invalid')
        getenv.assert_not_called()
        opener.assert_not_called()

    def test_default_deepseek_configuration_is_not_assumed_to_be_the_paid_policy(self):
        opener = Mock()
        credential = Mock(return_value=TEST_KEY)
        with patch.dict(os.environ, {}, clear=True):
            translator = podcast_cn.create_translation_client(
                'deepseek', budget=TranslationBudget(self.job, clock=lambda: NOW),
                opener=opener, credential_loader=credential,
            )
        self.assertEqual((translator.model, translator.base_url), ('deepseek-chat', ENDPOINT))
        self.assert_blocked(translator, 'translation_policy_unverified')
        credential.assert_not_called()
        opener.assert_not_called()

    def test_multithread_race_holds_budget_before_open(self):
        create(self.job)
        limits = threading.Barrier(2)

        def hold(request, timeout):
            limits.wait(timeout=10)
            limits.wait(timeout=10)
            return self.opener(request, timeout)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client(self.job, hold).translate, [segment()])
            limits.wait(timeout=10)
            try:
                self.assert_blocked(client(self.job, self.opener), 'translation_request_unresolved', [segment('b')])
            finally:
                limits.wait(timeout=10)
            self.assertEqual(first.result(timeout=10), {'a': '合成译文'})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.records()), 1)

    def test_multiprocess_race_has_exactly_one_opener_call(self):
        create(self.job)
        ctx = multiprocessing.get_context('spawn')
        start, queue = ctx.Event(), ctx.Queue()
        ready = [ctx.Event(), ctx.Event()]
        children = [ctx.Process(target=process_request, args=(self.job, str(i), signal, start, queue))
                    for i, signal in enumerate(ready)]
        self.run_children(children, ready, start)
        outcomes = [queue.get(timeout=10) for _ in children]
        self.assertEqual(sum(calls for _, calls in outcomes), 1)
        self.assertEqual([code for code, calls in outcomes if calls], ['ok'])
        self.assertEqual(len(self.records()), 1)

    def run_children(self, children, ready, start, expected=0):
        try:
            for child in children:
                child.start()
            for signal in ready:
                self.assertTrue(signal.wait(10))
            start.set()
            for child in children:
                child.join(10)
                self.assertEqual(child.exitcode, expected)
        finally:
            for child in children:
                if child.is_alive():
                    child.kill()
                    child.join()

    def test_process_crash_leaves_a_durable_reservation_after_restart(self):
        create(self.job, 2 * ONE_CAP)
        ctx = multiprocessing.get_context('spawn')
        start, queue, ready = ctx.Event(), ctx.Queue(), ctx.Event()
        child = ctx.Process(target=process_request, args=(self.job, 'a', ready, start, queue, True))
        self.run_children([child], [ready], start, expected=73)
        self.assertEqual(self.records()[0][:2], (ONE_CAP, 'submitted'))
        self.assert_blocked(client(self.job, self.opener), 'translation_request_unresolved')
        self.assert_blocked(client(self.job, self.opener), 'translation_request_unresolved', [segment('b')])
        self.assertEqual(self.requests, [])

    def test_timeout_does_not_retry_or_release(self):
        create(self.job, 2 * ONE_CAP)

        def timeout(request, timeout):
            self.requests.append(request)
            raise TimeoutError(TEST_KEY)

        translator = client(self.job, timeout)
        evidence = self.assert_blocked(translator, 'translation_transport_unknown', [segment('a'), segment('b')])
        self.assertIsNone(evidence['http_status'])
        self.assertTrue(evidence['billing_uncertain'])
        self.assertFalse(evidence['safe_to_retry'])
        self.assert_blocked(translator, 'translation_request_unresolved')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.records()[0][:2], (ONE_CAP, 'uncertain'))
        self.assertNotIn(TEST_KEY, json.dumps(self.records()))

    def test_invalid_usage_and_finish_never_trigger_structural_split(self):
        valid_usage = {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}
        cases = [
            ({'usage': None}, 'translation_usage_unknown'),
            ({'usage': {'prompt_tokens': 100}}, 'translation_usage_unknown'),
            *[({'usage': {**valid_usage, 'completion_tokens': value}}, 'translation_usage_unknown')
              for value in (-1, float('inf'), float('nan'), True, 1.5, '50', 4097)],
            ({'usage': {'prompt_tokens': 1_048_577, 'completion_tokens': 50,
                        'total_tokens': 1_048_627}}, 'translation_usage_unknown'),
            ({'usage': {**valid_usage, 'total_tokens': 149}}, 'translation_usage_unknown'),
            ({'usage': {'prompt_tokens': 100, 'completion_tokens': 0,
                        'total_tokens': 100}}, 'translation_usage_unknown'),
            ({'choices': [{'finish_reason': None}]}, 'translation_finish_unknown'),
            ({'choices': [{'finish_reason': 'length'}]}, 'translation_finish_unknown'),
        ]
        for index, (overrides, code) in enumerate(cases):
            with self.subTest(overrides=overrides):
                job = self.job / str(index)
                job.mkdir()
                create(job, 5 * ONE_CAP)
                calls = []

                def opener(request, timeout):
                    calls.append(request)
                    return reply(json.loads(request.data), **overrides)

                translator = client(job, opener)
                with self.assertRaises(TranslationRequestError) as raised:
                    podcast_cn.translate_with_resume(
                        [segment('a'), segment('b')], job / 'chunks', translator.translate,
                        translation_identity=translator.translation_identity,
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(len(calls), 1)
                self.assert_blocked(translator, 'translation_request_unresolved', [segment('c')])

    def test_bisection_is_charged_and_stops_before_uncovered_child(self):
        create(self.job, 2 * ONE_CAP)
        calls = []

        def opener(request, timeout):
            body = json.loads(request.data)
            ids = [row['id'] for row in json.loads(body['messages'][-1]['content'])]
            calls.append(ids)
            if len(ids) > 1:
                return reply(body, choices=[{'finish_reason': 'stop', 'message': {'content': '{}'}}])
            return reply(body)

        translator = client(self.job, opener)
        with self.assertRaisesRegex(TranslationRequestError, '额度'):
            podcast_cn.translate_with_resume(
                [segment('a'), segment('b')], self.job / 'chunks', translator.translate,
                translation_identity=translator.translation_identity,
            )
        self.assertEqual(calls, [['a', 'b'], ['a']])
        self.assertEqual([row[0] for row in self.records()], [ONE_CAP, ONE_CAP])
        self.assertEqual(self.records()[0][2]['code'], 'translation_invalid_structure')
        self.assertFalse((self.job / 'chunks' / 'chunk-0001.json').exists())

    def test_http_error_records_only_safe_summary_and_ids(self):
        create(self.job)

        def opener(request, timeout):
            self.requests.append(request)
            payload = {'error': {'code': 'rate_limit_exceeded',
                                 'message': f'Bearer {TEST_KEY}; Synthetic source https://signed.invalid'}}
            raise urllib.error.HTTPError(
                request.full_url, 429, TEST_KEY, {'x-request-id': PROVIDER_ID},
                io.BytesIO(json.dumps(payload).encode()),
            )

        evidence = self.assert_blocked(client(self.job, opener), 'translation_http_error')
        self.assertEqual(evidence['http_status'], 429)
        self.assertEqual(evidence['provider_request_id'], PROVIDER_ID)
        self.assertEqual(evidence['provider_error_code'], 'rate_limit_exceeded')
        self.assertTrue(evidence['provider_retryable'])
        self.assertFalse(evidence['safe_to_retry'])
        self.assertEqual(self.records()[0][2], evidence)
        self.assertFalse(any(s in json.dumps(evidence) for s in [TEST_KEY, 'Synthetic source', 'signed.invalid']))

    def test_transport_failure_without_journal_omits_untyped_url_error_details(self):
        create(self.job)
        opener = Mock(side_effect=urllib.error.URLError(
            f'Authorization: Bearer {TEST_KEY} https://signed.invalid/?secret=private-query private-body',
        ))
        evidence = self.assert_blocked(client(self.job, opener), 'translation_transport_unknown')
        opener.assert_called_once()
        self.assertEqual(evidence['transport_failure'], {
            'exception_type': 'URLError', 'phase': 'opener.open', 'reason': 'url_open_failed',
        })
        self.assertEqual(self.records()[0][2], evidence)
        for forbidden in ('Authorization', TEST_KEY, 'signed.invalid', 'private-query', 'private-body'):
            self.assertNotIn(forbidden, json.dumps(evidence))

    def test_unverified_model_endpoint_and_response_model(self):
        create(self.job)
        for options in ({'model': 'deepseek-chat'}, {'base_url': 'https://proxy.invalid'}):
            self.assert_blocked(client(self.job, self.opener, **options), 'translation_policy_unverified')
        self.assertEqual(self.requests, [])

        def opener(request, timeout):
            return reply(json.loads(request.data), model=f'model-{TEST_KEY}', id=TEST_KEY)

        evidence = self.assert_blocked(client(self.job, opener), 'translation_model_unknown')
        self.assertIsNone(evidence['provider_response_id'])
        self.assertTrue(evidence['billing_uncertain'])
        self.assertEqual(evidence['http_status'], 200)
        self.assertNotIn(TEST_KEY, json.dumps(self.records()))

    def test_bad_response_does_not_send_again(self):
        for index, (raw, expected_status) in enumerate([
            (b'{', 200), (b'{"choices": [], "choices": []}', 200),
            (b'x' * (512 * 1024 + 1), 200), (b'{}', None),
        ]):
            with self.subTest(index=index):
                job = self.job / str(index)
                job.mkdir()
                create(job)

                def opener(request, timeout):
                    self.requests.append(request)
                    response = io.BytesIO(raw)
                    response.status = expected_status
                    return response

                before = len(self.requests)
                translator = client(job, opener)
                evidence = self.assert_blocked(translator, 'translation_response_unknown')
                self.assertEqual(evidence['http_status'], expected_status)
                self.assertTrue(evidence['completion_uncertain'])
                self.assert_blocked(translator, 'translation_request_unresolved')
                self.assertEqual(len(self.requests), before + 1)

    def test_scope_copy_expiration_and_fee_mode_fail_closed(self):
        import shutil
        create(self.job)
        other = self.job / 'different-job'
        other.mkdir()
        shutil.copyfile(self.job / TranslationBudget.FILENAME, other / TranslationBudget.FILENAME)
        self.assert_blocked(client(other, self.opener), 'translation_allowance_invalid')
        with patch('translation_budget.time.time', return_value=NOW + 60):
            self.assert_blocked(client(self.job, self.opener), 'translation_allowance_invalid')
        self.assertEqual(self.requests, [])
        with self.assertRaises((ValueError, TranslationRequestError)):
            create(other, fee_mode='unknown')


    def test_independent_job_uses_only_its_own_authorization(self):
        create(self.job)
        other = self.job / 'independent'
        other.mkdir()
        self.assert_blocked(client(other, self.opener), 'translation_allowance_missing')
        create(other, authorization_id='12345678-1234-4123-8123-1234567890ef')
        for job in (self.job, other):
            translator = client(job, self.opener)
            self.assertEqual(translator.translate([segment()]), {'a': '合成译文'})
            self.assert_blocked(translator, 'translation_budget_exhausted', [segment('b')])
        self.assertEqual(len(self.requests), 2)

    def test_provision_cannot_replace_history(self):
        create(self.job)
        client(self.job, self.opener).translate([segment()])
        with self.assertRaises(FileExistsError):
            create(self.job, 10 * ONE_CAP)
        self.assertEqual(len(self.records()), 1)


class CacheTests(unittest.TestCase):
    def test_legacy_cache_resumes_before_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as path:
            job = pathlib.Path(path)
            batch = [segment()]
            translator = client(job, lambda *args, **kwargs: self.fail('unexpected opener'), model='deepseek-chat')
            cached = job / 'chunks'
            podcast_cn.translate_with_resume(batch, cached, lambda _: {'a': '缓存'},
                                             translation_identity=translator.translation_identity)
            before = (cached / 'chunk-0001.json').read_bytes()
            self.assertEqual(podcast_cn.translate_with_resume(
                batch, cached, translator.translate, translation_identity=translator.translation_identity,
            ), {'a': '缓存'})
            self.assertEqual((cached / 'chunk-0001.json').read_bytes(), before)

    def test_invalid_later_cache_blocks_even_an_earlier_missing_request(self):
        with tempfile.TemporaryDirectory() as path:
            cached = pathlib.Path(path)
            invalid = cached / 'chunk-0002.json'
            invalid.write_bytes(b'\xff\x00not-json')
            with self.assertRaisesRegex(podcast_cn.TranslationValidationError, '保留'):
                podcast_cn.translate_with_resume(
                    [segment('a', 'a'), segment('b', 'b')], cached,
                    lambda _: self.fail('earlier cache gap triggered retranslation'), max_chars=1,
                )
            self.assertEqual(invalid.read_bytes(), b'\xff\x00not-json')


    def test_studio_entry_reuses_bound_cache_without_loading_credentials(self):
        from diarization_core import DiarizationTurn
        from podcast_captions import CaptionPiece
        from studio_engine import Cancelled, StudioEngine, atomic_json

        with tempfile.TemporaryDirectory() as path:
            engine = StudioEngine(path)
            source = 'Synthetic caption for the Studio path'
            piece = CaptionPiece(0, 1000, source, 0)
            legacy_identity = 'deepseek|deepseek-chat|https://api.deepseek.com'
            config = {'expected_speakers': 1}
            wav = engine.job / 'synthetic.wav'
            wav.write_bytes(b'synthetic source')
            cache = engine.job / 'translations/chunk-0001.json'

            def stop_before_document(path, payload):
                if path.name == 'document.json':
                    raise Cancelled('synthetic interruption before document commit')
                atomic_json(path, payload)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(engine, 'transcribe', return_value=([piece], 'asr')) as transcribe,
                patch.object(engine, 'diarize_with_progress', return_value=[DiarizationTurn(0, 1000, 'host')]),
                patch.object(engine, 'duration_ms', return_value=1000),
                patch.object(engine, 'credential', side_effect=AssertionError('unexpected credentials')) as credential,
            ):
                with (
                    patch('studio_engine.atomic_json', side_effect=stop_before_document),
                    patch('studio_engine.create_translation_client') as factory,
                ):
                    factory.return_value.translation_identity = legacy_identity
                    factory.return_value.translate.return_value = {'seg-000001': '离线缓存'}
                    with self.assertRaises(Cancelled):
                        engine.prepare(config, wav)
                before = cache.read_bytes()
                document = engine.prepare(config, wav)
            transcribe.assert_called_once()
            credential.assert_not_called()
            self.assertEqual(document['segments'][0]['chinese'], '离线缓存')
            self.assertEqual(cache.read_bytes(), before)


    def test_studio_entry_keeps_safe_durable_error_evidence(self):
        from diarization_core import DiarizationTurn
        from podcast_captions import CaptionPiece
        from studio_engine import StudioEngine

        def fail(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 429, TEST_KEY, {'x-request-id': PROVIDER_ID},
                io.BytesIO(b'{"error":{"code":"rate_limit_exceeded"}}'),
            )

        with tempfile.TemporaryDirectory() as path:
            engine = StudioEngine(path)
            source = engine.job / 'synthetic.wav'
            source.write_bytes(b'fixture')
            word = CaptionPiece(0, 1000, 'Synthetic caption', 0)
            with (
                patch.dict(os.environ, {'DEEPSEEK_MODEL': 'deepseek-flash'}, clear=True),
                patch('translation_budget.time.time', return_value=NOW),
                patch.object(engine, 'acquire', return_value=source),
                patch.object(engine, 'transcribe', return_value=([word], 'asr')),
                patch.object(engine, 'diarize_with_progress', return_value=[DiarizationTurn(0, 1000, 'host')]),
                patch.object(engine, 'duration_ms', return_value=1000),
                patch.object(engine, 'credential', return_value=TEST_KEY),
                patch('studio_engine.create_translation_client',
                      side_effect=lambda provider, **kwargs: podcast_cn.create_translation_client(
                          provider, opener=fail, **kwargs)),
            ):
                create(engine.job)
                config = {'source': {'kind': 'local', 'value': str(source)}, 'expected_speakers': 1}
                with self.assertRaises(TranslationRequestError) as raised:
                    engine.run(config, 'prepare')
            debug = (engine.job / 'debug-failure.json').read_text()
            self.assertNotIn(TEST_KEY, debug)
            saved = json.loads(debug)
            self.assertEqual(saved['stage'], 'translation')
            self.assertEqual(saved['translation_request'], raised.exception.evidence)
            self.assertEqual(saved['translation_request']['http_status'], 429)
            progress = json.loads((engine.job / 'progress.json').read_text())
            self.assertEqual(progress['error_code'], 'translation_http_error')


if __name__ == '__main__':
    unittest.main()
