"""The November 23 acknowledgement is finite, episode-scoped and offline."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_translation_budget import ONE_CAP, TEST_KEY, reply, segment
import podcast_cn
import deepseek_input
import studio_engine
import studio_recovery
from studio_engine import StudioEngine
import translation_budget as rates
from translation_budget import TranslationBudget, TranslationRequestError


SHELF = 'd1e5daab08174fd2be2b3868d14c7cd2'
CAPS = {
    SHELF: 620_000,
    'de91a4b274894fa29edebb2cb90c654d': 800_000,
    '2a8a996b3c5a433f8a06619ad45c56eb': 1_030_000,
    '1efe42d270884ae8b8df07f9c2e9de8a': 840_000,
}


class EpisodeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.now = rates.EPISODE_VERIFIED_AT + 1
        self.enterContext(patch.object(rates, '_EPISODE_ROOT', self.root))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.key = Mock(return_value=TEST_KEY)
        self.opener = Mock(side_effect=lambda req, timeout: reply(json.loads(req.data), usage={
            'prompt_tokens': 90, 'completion_tokens': 4, 'total_tokens': 94,
        }))
        self.job = self.root / SHELF
        self.job.mkdir()

    def provision(self, cap, **changes):
        TranslationBudget.provision(self.job, **{
            'authorization_id': self.job.name, 'fee_mode': 'cny_token_only',
            'cap_microunits': cap, 'prior_microunits': 0,
            'verified_at': rates.EPISODE_VERIFIED_AT, 'expires_at': rates.EPISODE_EXPIRES_AT,
            'clock': lambda: self.now, **changes,
        })

    def client(self, **options):
        return podcast_cn.create_translation_client(
            'deepseek', budget=TranslationBudget(self.job, clock=lambda: self.now),
            credential_loader=self.key, opener=self.opener, **options,
        )

    def denial(self, code, call):
        with self.assertRaises(TranslationRequestError) as error:
            call()
        self.assertEqual(error.exception.code, code)
        self.key.assert_not_called()
        self.opener.assert_not_called()

    def test_pinned_scope_and_real_caps_stop_before_credentials(self):
        self.assertEqual(rates._EPISODE_CAPS, CAPS)
        self.assertEqual((rates.EPISODE_VERIFIED_AT, rates.EPISODE_EXPIRES_AT), (1795403956, 1795490356))
        self.assertEqual(rates.EPISODE_EXPIRES_AT - rates.EPISODE_VERIFIED_AT, 86400)
        for job, cap in CAPS.items():
            with self.subTest(job=job):
                self.job = self.root / job
                self.job.mkdir(exist_ok=True)
                self.denial('translation_allowance_missing', self.client)
                self.provision(cap)
                client = self.client()
                self.assertEqual((client.model, client.base_url), (rates.MODEL, rates.ENDPOINT))
                self.assertIsNone(client._admission)
                segments = [segment(f'seg-{i:06d}', 'Synthetic English dialogue. ' * 4) for i in range(359)]
                chunks = podcast_cn.chunk_segments(segments)
                self.assertEqual(len(chunks), 6)
                quotes = client.preflight_episode(chunks, 'Synthetic topic')
                self.assertEqual(len(quotes), 7)
                self.assertLess(sum(q.amount for q in quotes), cap)
                self.key.assert_not_called()
                self.opener.assert_not_called()
                with closing(client._budget._connect(readonly=True)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone(), (0,))

    def test_isolated_root_job_prior_cap_and_window(self):
        for args in ({'cap': 620001}, {'prior_microunits': 1}, {'cap': 870000},
                     {'expires_at': rates.EPISODE_EXPIRES_AT + 1}):
            with self.assertRaises(ValueError):
                self.provision(**{'cap': 620000, **args})
        for self.now in (rates.EPISODE_VERIFIED_AT - 1, rates.EPISODE_EXPIRES_AT):
            with self.assertRaises(ValueError):
                self.provision(620000)
        self.now = rates.EPISODE_VERIFIED_AT
        other = self.root / 'elsewhere' / SHELF
        for self.job in (other, self.root / ('f' * 32)):
            self.job.mkdir(parents=True)
            with self.assertRaises(ValueError):
                self.provision(620000)
            self.assertEqual(self.client().model, 'deepseek-chat')
            self.denial('translation_allowance_invalid',
                        lambda: self.client(model=rates.MODEL).translate([segment()]))

    def test_card_and_wallet_rechecked_after_selection(self):
        self.provision(620000)
        client = self.client()
        self.now = rates.EPISODE_EXPIRES_AT
        self.denial('translation_allowance_invalid', lambda: client.translate([segment()]))
        self.now = rates.EPISODE_VERIFIED_AT
        for field, value in [('MODEL', 'other'), ('MODEL_REVISION', 'other'), ('ENDPOINT', 'https://invalid'),
                             ('INPUT_MICRO_CNY', 1), ('OUTPUT_MICRO_CNY', 4)]:
            with self.subTest(field=field), patch.object(rates, field, value):
                self.denial('translation_allowance_invalid', client._budget.check_episode_allowance)
        changed = self.root / 'observation.json'
        changed.write_bytes(rates._EPISODE_OBSERVATION.read_bytes() + b'\n')
        with patch.object(rates, '_EPISODE_OBSERVATION', changed):
            self.denial('translation_allowance_invalid', lambda: client.translate([segment()]))
        with closing(client._budget._connect()) as db, db:
            db.execute('UPDATE allowance SET cap_microunits = ?', (ONE_CAP,))
        self.denial('translation_allowance_invalid', lambda: client.translate([segment()]))
        self.denial('translation_allowance_invalid', self.client)

    def test_explicit_and_environment_mismatches_do_not_bind_flash(self):
        self.provision(620000)
        for kwargs in ({'model': 'deepseek-chat'}, {'base_url': 'https://wrong.invalid'}):
            self.denial('translation_policy_unverified', lambda: self.client(**kwargs).translate([segment()]))
        for env in ({'DEEPSEEK_MODEL': 'deepseek-chat'}, {'DEEPSEEK_BASE_URL': 'https://wrong.invalid'}):
            with patch.dict(os.environ, env):
                self.denial('translation_policy_unverified', lambda: self.client().translate([segment()]))

    def test_exact_body_and_title_at_cap_with_cache_resume(self):
        first, second = [segment()], [segment('b')]
        title = 'Synthetic topic'
        # 98 tokens per text request, 101 for the title. One micro-unit less
        # than the remaining batch must stop before the first cache gap.
        chunk_cost = 98 * 2 + 4096 * 8
        title_cost = 101 * 2 + 512 * 8
        self.provision(chunk_cost + title_cost)
        client = self.client()
        self.denial('translation_budget_exhausted', lambda: client.preflight_episode([first, second], title))
        self.denial('translation_plan_unverified', lambda: client.translate(first))
        directory = self.job / 'translations'
        podcast_cn.translate_with_resume(first, directory, lambda rows: {'a': '合成缓存'},
                                         max_chars=16, translation_identity=client.translation_identity)
        def translate_rest():
            return podcast_cn.translate_with_resume(
                first + second, directory, client.translate, max_chars=16, preserve_invalid=True,
                translation_identity=client.translation_identity,
                preflight=lambda missing: client.preflight_episode(missing, title),
            )
        with closing(client._budget._connect()) as db, db:
            db.execute('UPDATE allowance SET cap_microunits = cap_microunits - 1')
        self.denial('translation_budget_exhausted', translate_rest)
        with closing(client._budget._connect()) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone(), (0,))
            db.execute('UPDATE allowance SET cap_microunits = cap_microunits + 1')
        self.assertEqual(translate_rest(), {'a': '合成缓存', 'b': '合成译文'})
        self.denial_after_send('translation_plan_unverified', lambda: client.translate_title(title + '!'))
        self.opener.side_effect = lambda req, timeout: reply(
            {'messages': [{'content': '[]'}]}, choices=[{'finish_reason': 'stop',
            'message': {'content': '{"title":"合成标题"}'}}])
        self.assertEqual(client.translate_title(title), '合成标题')
        with closing(client._budget._connect(readonly=True)) as db:
            rows = list(db.execute('SELECT reserved_microunits, state, evidence FROM requests ORDER BY rowid'))
        self.assertEqual([(a, s) for a, s, _ in rows], [(chunk_cost, 'completed'), (title_cost, 'completed')])
        for _, _, evidence in rows:
            self.assertEqual(json.loads(evidence)['rate_observation_sha256'], rates._EPISODE_OBSERVATION_SHA)

    def denial_after_send(self, code, attempt):
        calls = self.opener.call_count
        with self.assertRaises(TranslationRequestError) as error:
            attempt()
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.opener.call_count, calls)

    def test_sum_rechecked_atomically_at_reservation(self):
        chunks = [[segment()], [segment('b')]]
        self.provision(100000)
        client = self.client()
        quotes = client.preflight_episode(chunks, 'Synthetic topic')
        with closing(client._budget._connect()) as db, db:
            db.execute('UPDATE allowance SET cap_microunits = ?', (sum(q.amount for q in quotes) - 1,))
        self.denial('translation_budget_exhausted', lambda: client.translate(chunks[0]))

    def test_token_ids_framing_max_chars_and_no_truncation(self):
        chunks = podcast_cn.chunk_segments([segment('seg-000001', 'x' * 8000), segment('seg-000002')])
        self.assertEqual([len(c) for c in chunks], [1, 1])
        for messages, expected, extra in [
            (podcast_cn._chunk_messages(chunks[0]), 1098, 4),
            (podcast_cn._title_messages('Synthetic topic'), 101, 3),
        ]:
            # The number of fields in a BatchEncoding (2) is never its token count.
            tokenizer, template, bos = deepseek_input._load(deepseek_input.ARCHIVE.read_bytes())
            rendered = template.render(messages=messages, bos_token=bos, add_generation_prompt=True)
            content = [m['content'] for m in messages]
            self.assertEqual(rendered, bos + content[0] + '<｜User｜>' + content[1] + '<｜Assistant｜>')
            ids = tokenizer.encode(rendered, add_special_tokens=False).ids
            batch_encoding = {'input_ids': ids, 'attention_mask': [1] * len(ids)}
            self.assertEqual(len(batch_encoding), 2)
            self.assertEqual(deepseek_input.count(messages), len(batch_encoding['input_ids']))
            self.assertEqual(len(ids), expected)
            self.assertEqual(expected - len(tokenizer.encode(''.join(content)).ids), extra)
        messages = podcast_cn._chunk_messages([segment(text='123456789' * 6000)])
        self.assertGreater(deepseek_input.count(messages), 16384)

    def test_missing_corrupt_tokenizer_and_thinking_gate_before_transport(self):
        self.provision(620000)
        client = self.client()
        batch = [segment()]
        for failure in ('missing', 'digest', 'dependency', 'no-thinking-field', 'enabled', 'extra-tools'):
            client.preflight_episode([batch], 'Synthetic topic')
            body_builder = rates.translation_body
            def altered(stage, messages):
                body = json.loads(body_builder(stage, messages))
                if failure == 'no-thinking-field':
                    del body['thinking']
                elif failure == 'enabled':
                    body['thinking']['type'] = 'enabled'
                else:
                    body['tools'] = []
                return json.dumps(body).encode()
            path = self.root / failure
            if failure == 'digest':
                path.write_bytes(deepseek_input.ARCHIVE.read_bytes() + b'x')
            if failure == 'dependency':
                deepseek_input._load.cache_clear()
                change = patch.dict(sys.modules, tokenizers=None)
            elif failure in ('missing', 'digest'):
                change = patch.object(deepseek_input, 'ARCHIVE', path)
            else:
                change = patch.object(rates, 'translation_body', altered)
            with self.subTest(failure=failure), change:
                self.denial('translation_input_unverified', lambda: client.translate(batch))

    def test_usage_over_either_bound_holds_one_row_and_stops_batch(self):
        for jid, prompt, completion in [(SHELF, 99, 1), (list(CAPS)[1], 90, 4097)]:
            self.job = self.root / jid
            self.job.mkdir(exist_ok=True)
            self.provision(CAPS[jid])
            client = self.client()
            batch = [segment()]
            client.preflight_episode([batch, [segment('b')]], 'Synthetic topic')
            self.opener.side_effect = lambda req, timeout: reply(json.loads(req.data), usage={
                'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': prompt + completion,
            })
            with self.assertRaises(TranslationRequestError) as err:
                client.translate(batch)
            self.assertEqual(err.exception.code, 'translation_usage_unknown')
            self.denial_after_send('translation_request_unresolved', lambda: client.translate(batch))
            self.denial_after_send('translation_request_unresolved',
                                   lambda: client.preflight_episode([[segment('b')]], 'Synthetic topic'))
            with closing(client._budget._connect(readonly=True)) as db:
                self.assertEqual(db.execute('SELECT state, reserved_microunits FROM requests').fetchall(),
                                 [('uncertain', 98 * 2 + 4096 * 8)])

    def make_legacy_source(self, jid):
        self.job = self.root / jid
        self.job.mkdir(exist_ok=True)
        self.provision(CAPS[jid])
        engine = StudioEngine(self.job)
        source = self.job / 'synthetic-audio'
        source.write_bytes(b'synthetic decoded media')
        selector = {'kind': 'local', 'value': str(source),
                    'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
        config = {'source': {'kind': 'local', 'value': str(source)}, 'expected_speakers': 1}
        ref = self.job / 'ref.wav'
        ref.write_bytes(b'synthetic reference')
        draft = {'schema_version': 1, 'transcript_source': 'fixture',
                 'references': {'host': {'path': 'ref.wav', 'sha256': hashlib.sha256(ref.read_bytes()).hexdigest()}},
                 'speakers': {'host': {}}, 'media_sha256': selector['input_sha256'],
                 'segments': [{'id': 'a', 'speaker_id': 'host', 'text': 'Synthetic source',
                               'start_ms': 0, 'end_ms': 100}]}
        (self.job / 'state.json').write_text(json.dumps({'title': 'Synthetic topic'}))
        path = self.job / 'translation-source.json'
        with (patch.object(engine, 'duration_ms', return_value=100),
              patch.object(engine, '_prepare_source', return_value=draft),
              patch('studio_engine.create_translation_client', return_value=self.client(model='deepseek-chat'))):
            self.denial('translation_policy_unverified', lambda: engine.prepare(config, source, source_identity=selector))
        # The old envelope format stores only the full binding hash, draft and digest.
        envelope = json.loads(path.read_bytes())
        del envelope['translation_identity']
        path.write_text(json.dumps(envelope))
        return engine, config, selector, path

    def resume(self, engine, config, selector):
        with (patch.object(engine, '_prepare_source', side_effect=AssertionError('unexpected ASR')),
              patch('studio_engine.create_translation_client', return_value=self.client())):
            return engine.prepare(config, source_identity=selector)

    def test_engine_rebinds_unused_source_once_then_resumes_new_binding(self):
        engine, config, selector, path = self.make_legacy_source(list(CAPS)[1])
        before = json.loads(path.read_bytes())
        # Interrupt after one accepted response but before document publication.
        real_write = studio_engine.atomic_json
        def interrupt(target, value):
            if target.name == 'document.json':
                raise InterruptedError()
            return real_write(target, value)
        with patch('studio_engine.atomic_json', side_effect=interrupt):
            with self.assertRaises(InterruptedError):
                self.resume(engine, config, selector)
        after = json.loads(path.read_bytes())
        self.assertNotEqual(after['binding'], before['binding'])
        self.assertEqual((after['draft'], after['digest']), (before['draft'], before['digest']))
        self.assertEqual(after['translation_identity'], self.client().translation_identity)
        self.resume(engine, config, selector)
        self.key.assert_called_once()
        self.opener.assert_called_once()

    def test_rebind_rejects_changed_source_reference_plan_and_request_evidence(self):
        engine, config, selector, path = self.make_legacy_source(SHELF)
        original = path.read_bytes()
        ref = self.job / 'ref.wav'
        reference = ref.read_bytes()
        for change in ('digest', 'reference', 'media', 'config', 'plan', 'identity',
                       'cache', 'journal', 'debug', 'completed-wallet', 'pending-wallet'):
            envelope = json.loads(original)
            cfg = dict(config)
            debug = self.job / 'debug-failure.json'
            journal = self.job / 'translation-requests'
            cache = self.job / 'translations/chunk-0001.json'
            if change == 'digest':
                envelope['draft']['segments'][0]['text'] += '!'
            elif change == 'reference':
                ref.write_bytes(b'changed')
            elif change in ('media', 'plan'):
                if change == 'media':
                    envelope['draft']['media_sha256'] = '0' * 64
                else:
                    envelope['draft']['translation_plan'] = []
                envelope['digest'] = studio_recovery.fingerprint(envelope['draft'])
            elif change == 'config':
                cfg['expected_speakers'] = 2
            elif change == 'identity':
                envelope['translation_identity'] = 'unrelated'
            elif change == 'cache':
                cache.write_text('{}')
            elif change == 'journal':
                journal.mkdir()
                (journal / 'bad.json').write_text('{}')
            elif change == 'debug':
                debug.write_text(json.dumps({'stage': 'translation', 'translation_request': {'http_status': 429}}))
            else:
                state = 'completed' if change == 'completed-wallet' else 'submitted'
                with closing(self.client()._budget._connect()) as db, db:
                    db.execute('INSERT INTO requests VALUES (?, ?, 100, ?, ?, 1)',
                               ('f' * 64, 'b' * 32, state, '{}'))
            path.write_text(json.dumps(envelope))
            before = path.read_bytes()
            with self.subTest(change=change), self.assertRaises(studio_recovery.StudioRecoveryError) as err:
                self.resume(engine, cfg, selector)
            self.assertEqual(err.exception.code, 'translation_source_mismatch')
            self.assertEqual(path.read_bytes(), before)
            self.key.assert_not_called()
            self.opener.assert_not_called()
            ref.write_bytes(reference)
            cache.unlink(missing_ok=True)
            debug.unlink(missing_ok=True)
            if journal.exists():
                (journal / 'bad.json').unlink()
                journal.rmdir()
            with closing(self.client()._budget._connect()) as db, db:
                db.execute('DELETE FROM requests')
