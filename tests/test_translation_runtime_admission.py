"""Verify the persisted admission through the real client and an offline transport."""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import Mock, patch

from tests.test_translation_budget import NOW, ONE_CAP, TEST_KEY, create, reply, segment

import podcast_cn
import studio_recovery
import translation_admission as admission
from translation_budget import (
    CONSERVED_PRIOR_MICROUNITS, ENDPOINT, FEE_MODE, MODEL, MODEL_REVISION, POLICY_EXPIRES_AT, TranslationBudget,
)


OBSERVED = 1795183893
EXPIRES = 1795270293
SOURCE_SHA = '1c7c48b451098b159c9d42482711472af2abce13c20411d7e052b333fc737253'
JOBS = ('516b03a087cd44f2a5b0c01ba4ea4004', '5682f75d9ec94898aec8090dd385c10f')
EXCLUDED = (
    '1c3ce3aa9ddb4266b1bc52d1918077b3',
    'local-20261120-rework-18857585',
    'local-20261120-startupcpg-213-53333936',
)


class RuntimeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.job = self.root / JOBS[0]
        self.job.mkdir()
        self.now = OBSERVED + 1
        self.path = self.root / admission.RuntimeAdmission.FILENAME
        self.load_key = Mock(return_value=TEST_KEY)
        self.opener = Mock(side_effect=self.reply)
        self.enterContext(patch('translation_admission.time.time', side_effect=lambda: self.now))
        self.enterContext(patch('podcast_cn.os.getenv', side_effect=self.getenv))
        self.network = Mock(side_effect=AssertionError('offline tests cannot access the network'))
        for target, name in (
            (socket.socket, 'connect'), (socket.socket, 'connect_ex'), (socket, 'getaddrinfo'),
            (socket, 'create_connection'), (urllib.request.OpenerDirector, 'open'),
        ):
            self.enterContext(patch.object(target, name, self.network))
        self.addCleanup(self.network.assert_not_called)

    @staticmethod
    def getenv(name):
        if name.endswith('API_KEY'):
            raise AssertionError('unexpected environment credential lookup')
        return None

    @staticmethod
    def reply(request, timeout):
        return reply(json.loads(request.data), model=MODEL_REVISION)

    def provision(self, job=None, cap=22_000_000, prior=21_130_000):
        job = self.job if job is None else job
        job.mkdir(exist_ok=True)
        create(job, cap, prior_microunits=prior, expires_at=POLICY_EXPIRES_AT, clock=lambda: NOW)

    def client(self, job=None, **kwargs):
        job = self.job if job is None else job
        return podcast_cn.create_translation_client('deepseek', **{
            'budget': TranslationBudget(job), 'credential_loader': self.load_key,
            'opener': self.opener, 'model': MODEL, 'base_url': ENDPOINT, **kwargs,
        })

    def blocked(self, client, code, batch=None):
        with self.assertRaises(studio_recovery.StudioRecoveryError) as raised:
            client.translate(batch or [segment()])
        self.assertEqual(raised.exception.code, code)
        return raised.exception.evidence

    def ledger(self):
        with closing(sqlite3.connect(self.job / TranslationBudget.FILENAME)) as db:
            return (db.execute('SELECT * FROM allowance').fetchall(),
                    db.execute('SELECT * FROM requests ORDER BY rowid').fetchall())

    def test_old_deadline_and_unpersisted_observation(self):
        self.provision(cap=3 * ONE_CAP, prior=0)
        self.assertEqual(POLICY_EXPIRES_AT, 1795135740)
        self.now = POLICY_EXPIRES_AT - 1
        self.assertEqual(self.client().translate([segment()]), {'a': '合成译文'})
        self.opener.assert_called_once()
        before = self.ledger()
        self.opener.reset_mock()
        self.load_key.reset_mock()
        for now in (POLICY_EXPIRES_AT, POLICY_EXPIRES_AT + 1, OBSERVED + 1):
            self.now = now
            self.assertTrue((admission._FIXTURE / admission._OBSERVATION).is_file())
            self.assertIsNone(admission.RuntimeAdmission.load(self.root))
            self.blocked(self.client(), 'translation_allowance_invalid', [segment('b')])
        fake = admission.RuntimeAdmission(self.root, '0' * 64)
        self.blocked(self.client(admission=fake), 'translation_admission_invalid')
        self.blocked(self.client(admission=True), 'translation_admission_invalid')
        self.opener.assert_not_called()
        self.load_key.assert_not_called()
        self.assertEqual(self.ledger(), before)

    def test_scoped_flash_through_client_journal_and_budget(self):
        self.provision()
        before, _ = self.ledger()
        issued = admission.admit_rate_card(self.root)
        self.assertEqual(self.ledger(), (before, []))
        self.assertEqual(issued, admission.RuntimeAdmission.load(self.root))
        data = json.loads(self.path.read_bytes())
        batch = json.loads((admission._FIXTURE / admission._BATCH).read_bytes())
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['source']['base_head'], '3783241f0c81c50e22c9c6d5a92bd839ea22fcf3')
        for name in ('translation_admission.py', 'translation_budget.py', 'podcast_cn.py'):
            file = Path(podcast_cn.__file__).with_name(name)
            self.assertEqual(data['source']['sha256'][name], hashlib.sha256(file.read_bytes()).hexdigest())
        self.assertEqual(data['authorization']['job_ids'], batch['job_ids'])
        self.assertEqual(len(set(batch['job_ids'])), 28)
        self.assertEqual(data['authorization']['source_sha256'], SOURCE_SHA)
        self.assertEqual(len(data['official_sources']), 4)
        for source in data['official_sources']:
            raw = (admission._FIXTURE / source['file']).read_bytes()
            self.assertEqual((hashlib.sha256(raw).hexdigest(), len(raw)), (source['sha256'], source['bytes']))
        self.assertEqual((data['observed_at_unix'], data['fact_expires_at_unix']), (OBSERVED, EXPIRES))
        self.assertEqual(data['contract'], {
            'api_name': MODEL, 'model_revision': MODEL_REVISION, 'endpoint': ENDPOINT,
            'fee_mode': 'cny_token_only', 'thinking': 'disabled', 'thinking_admitted': False,
            'context_tokens': 1_048_576, 'peak_uncached_input_cny_per_million': 2,
            'peak_output_cny_per_million': 8,
        })

        journal = studio_recovery.TranslationJournal(self.job)
        # The existing Studio factory call supplies a budget and journal.
        client = self.client(model=None, base_url=None, request_journal=journal)
        self.assertEqual((client.model, client.base_url), (MODEL, ENDPOINT))
        handle = client._admission
        actual_check = admission.RuntimeAdmission.check
        checked = []

        def capture(record, *args):
            checked.append(record)
            return actual_check(record, *args)

        with patch.object(admission.RuntimeAdmission, 'check', capture):
            self.assertEqual(client.translate([segment()]), {'a': '合成译文'})
        self.assertGreaterEqual(len(checked), 2)
        self.assertTrue(all(record is handle for record in checked))
        self.opener.assert_called_once()
        self.load_key.assert_called_once()
        wire = self.opener.call_args.args[0]
        self.assertEqual(wire.full_url, ENDPOINT + '/chat/completions')
        body = json.loads(wire.data)
        self.assertEqual((body['model'], body['thinking'], body['max_tokens']), (MODEL, {'type': 'disabled'}, 4096))
        allowance, (row,) = self.ledger()
        self.assertEqual(allowance, before)
        self.assertEqual(row[2:4], (2_129_920, 'completed'))
        receipt = json.loads(row[4])
        self.assertEqual(receipt['local_request_id'], journal.last_evidence['local_request_id'])
        self.assertEqual(receipt['response_model'], MODEL_REVISION)
        event = receipt['admission_event']
        self.assertEqual(event['authorization_refs'], ['t1294u', 't528u'])
        self.assertEqual(event['record_sha256'], issued.record_sha256)
        self.assertEqual(event['prior_microunits'], 21_130_000)
        self.assertEqual(event['source_sha256'], SOURCE_SHA)
        self.assertTrue(event['legacy_cap_lifted'])

    def test_scope_staleness_and_request_model_are_distinct(self):
        self.provision()
        record = admission.admit_rate_card(self.root)
        for job_id in (*EXCLUDED, 'entirely-new-job'):
            with self.subTest(job=job_id):
                job = self.root / job_id
                self.provision(job)
                self.blocked(self.client(job, admission=record), 'translation_admission_scope')
        default_client = self.client(admission=record, model=None, base_url=None)
        self.assertEqual(default_client.model, 'deepseek-chat')
        self.blocked(default_client, 'translation_policy_unverified')
        self.blocked(self.client(admission=record, base_url='https://proxy.invalid'), 'translation_policy_unverified')
        self.blocked(self.client(admission=record, model='unknown'), 'translation_policy_unverified')
        for now in (OBSERVED - 1, EXPIRES, EXPIRES + 1):
            self.now = now
            self.blocked(self.client(admission=record), 'translation_admission_stale')
            self.blocked(self.client(self.root / EXCLUDED[0], admission=record), 'translation_admission_scope')
            with self.assertRaises(ValueError):
                admission.admit_rate_card(self.root)
        self.assertEqual(admission.RuntimeAdmission.load(self.root), record)
        self.load_key.assert_not_called()
        self.opener.assert_not_called()

    def test_tampered_scope_contract_and_provenance_are_rejected(self):
        self.provision()
        admission.admit_rate_card(self.root)
        raw = self.path.read_bytes()
        for section, key, value, code in (
            ('authorization', 'source_sha256', '0' * 64, 'translation_admission_scope'),
            ('authorization', 'job_ids', [], 'translation_admission_scope'),
            ('authorization', 'lifted_legacy_caps_microunits', [100], 'translation_admission_scope'),
            ('contract', 'fee_mode', 'unknown', 'translation_admission_invalid'),
            ('contract', 'thinking', 'enabled', 'translation_admission_invalid'),
            ('contract', 'model_revision', 'wrong-revision', 'translation_admission_invalid'),
            ('contract', 'context_tokens', 2_000, 'translation_admission_invalid'),
            ('contract', 'peak_uncached_input_cny_per_million', 1, 'translation_admission_invalid'),
            ('source', 'base_head', '0' * 40, 'translation_admission_invalid'),
        ):
            with self.subTest(field=key):
                altered = json.loads(raw)
                altered[section][key] = value
                self.path.write_text(json.dumps(altered))
                self.blocked(self.client(admission=admission.RuntimeAdmission.load(self.root)), code)
        self.path.write_bytes(raw)
        fixture = self.root / 'fixture-copy'
        shutil.copytree(admission._FIXTURE, fixture)
        (fixture / 'sources/pricing-en.html').write_bytes(b'edited in test copy')
        with patch.object(admission, '_FIXTURE', fixture):
            self.blocked(self.client(admission=admission.RuntimeAdmission.load(self.root)), 'translation_admission_invalid')
            with self.assertRaises(ValueError):
                admission.admit_rate_card(self.root)
            (fixture / admission._BATCH).write_bytes(b'{}')
            self.blocked(self.client(admission=admission.RuntimeAdmission.load(self.root)), 'translation_admission_scope')
        with closing(sqlite3.connect(self.job / TranslationBudget.FILENAME)) as db, db:
            db.execute("UPDATE allowance SET fee_mode = 'unknown'")
        self.blocked(self.client(admission=admission.RuntimeAdmission.load(self.root)), 'translation_allowance_invalid')
        self.load_key.assert_not_called()
        self.opener.assert_not_called()

    def test_cap_override_applies_only_to_the_reviewed_legacy_caps(self):
        TranslationBudget.adopt(
            self.job, isolation_root=self.root, authorization_id='0' * 32, fee_mode=FEE_MODE,
            cap_microunits=870_000, prior_microunits=CONSERVED_PRIOR_MICROUNITS,
            verified_at=NOW - 60, expires_at=POLICY_EXPIRES_AT,
        )
        other = self.root / JOBS[1]
        self.provision(other, cap=CONSERVED_PRIOR_MICROUNITS + 1)
        record = admission.admit_rate_card(self.root)
        self.blocked(self.client(other, admission=record), 'translation_budget_exhausted')
        self.opener.assert_not_called()
        self.load_key.assert_not_called()
        self.assertEqual(self.client(admission=record).translate([segment()]), {'a': '合成译文'})
        self.opener.assert_called_once()

    def test_response_model_mismatch_is_uncertain_and_never_retried(self):
        self.provision()
        record = admission.admit_rate_card(self.root)
        self.opener.side_effect = lambda request, timeout: reply(json.loads(request.data), model='wrong-revision')
        client = self.client(admission=record)
        error = self.blocked(client, 'translation_model_unknown')
        self.assertTrue(error['billing_uncertain'])
        self.assertFalse(error['safe_to_retry'])
        self.assertEqual(self.ledger()[1][0][3], 'uncertain')
        self.blocked(client, 'translation_request_unresolved')
        self.opener.assert_called_once()

    def test_old_history_and_journal_survive_card_and_job_switch(self):
        self.provision(cap=21_130_000 + 3 * ONE_CAP)
        self.now = NOW
        journal = studio_recovery.TranslationJournal(self.job)
        legacy = self.client(request_journal=journal)
        self.assertEqual(legacy.translate([segment('done')]), {'done': '合成译文'})
        self.opener.side_effect = TimeoutError
        original = self.blocked(legacy, 'translation_transport_unknown')
        history = self.ledger()
        receipt = journal.path / original['receipt']
        saved = receipt.read_bytes()
        self.assertEqual([row[3] for row in history[1]], ['completed', 'uncertain'])
        self.assertEqual(json.loads(saved)['billing_state'], 'unknown')
        self.now = OBSERVED
        old_record = admission.admit_rate_card(self.root)
        self.path.write_bytes(self.path.read_bytes() + b'\n')
        new_record = admission.RuntimeAdmission.load(self.root)
        self.assertNotEqual(new_record.record_sha256, old_record.record_sha256)
        self.opener.reset_mock()
        self.load_key.reset_mock()
        self.blocked(self.client(admission=old_record), 'translation_admission_invalid')
        self.blocked(self.client(admission=new_record, model='deepseek-chat'), 'translation_policy_unverified')
        self.blocked(self.client(admission=new_record), 'translation_request_unresolved')
        self.blocked(self.client(admission=new_record, request_journal=journal), 'translation_request_already_recorded')
        other = self.root / JOBS[1]
        self.provision(other)
        blocked = self.blocked(self.client(other, admission=new_record), 'translation_request_unresolved')
        self.assertEqual((blocked['blocking_job_id'], blocked['blocking_request_id']),
                         (self.job.name, original['local_request_id']))
        self.assertEqual(self.ledger(), history)
        self.assertEqual(receipt.read_bytes(), saved)
        self.opener.assert_not_called()
        self.load_key.assert_not_called()

    def test_bound_cache_reuse_and_mismatch_preserve_original_bytes(self):
        self.provision()
        record = admission.admit_rate_card(self.root)
        client = self.client(admission=record)
        batch = [segment()]
        chunks = self.job / 'chunks'
        cached = podcast_cn.translate_with_resume(
            batch, chunks, lambda _: {'a': '离线译文'}, translation_identity=client.translation_identity,
        )
        path = chunks / 'chunk-0001.json'
        raw = path.read_bytes()
        self.now = EXPIRES
        self.assertEqual(podcast_cn.translate_with_resume(
            batch, chunks, client.translate, translation_identity=client.translation_identity,
        ), cached)
        self.assertEqual(path.read_bytes(), raw)
        default = self.client(admission=record, model='deepseek-chat')
        with self.assertRaises(studio_recovery.StudioRecoveryError) as error:
            podcast_cn.translate_with_resume(
                batch, chunks, default.translate, translation_identity=default.translation_identity,
                preserve_invalid=True,
            )
        self.assertEqual(error.exception.code, 'translation_cache_mismatch')
        self.assertEqual(path.read_bytes(), raw)
        self.opener.assert_not_called()
        self.load_key.assert_not_called()

    def test_expiry_during_credential_lookup_rolls_back_reservation(self):
        self.provision()
        record = admission.admit_rate_card(self.root)

        def load():
            self.now = EXPIRES
            return TEST_KEY

        self.load_key.side_effect = load
        journal = studio_recovery.TranslationJournal(self.job)
        self.blocked(self.client(admission=record, request_journal=journal), 'translation_admission_stale')
        self.assertFalse(journal.path.exists())
        self.assertEqual(self.ledger()[1], [])
        self.opener.assert_not_called()

    def test_expiry_or_revocation_after_durable_journal_prevents_open(self):
        for reason, job_id in zip(('stale', 'invalid'), JOBS):
            with self.subTest(reason=reason):
                self.job = self.root / job_id
                self.now = OBSERVED
                self.provision()
                record = admission.admit_rate_card(self.root)
                journal = studio_recovery.TranslationJournal(self.job)
                save = studio_recovery._save

                def interrupt(path, value):
                    save(path, value)
                    if value['request_state'] == 'submitted':
                        if reason == 'stale':
                            self.now = EXPIRES
                        else:
                            self.path.unlink()

                with patch.object(studio_recovery, '_save', side_effect=interrupt):
                    error = self.blocked(self.client(admission=record, request_journal=journal),
                                         'translation_admission_' + reason, [segment(reason)])
                receipt = json.loads((journal.path / error['receipt']).read_bytes())
                self.assertEqual((receipt['request_state'], receipt['billing_state']), ('not_sent', 'no_charge'))
                self.assertFalse(error['billing_uncertain'])
                self.assertEqual(self.ledger()[1][0][1], receipt['local_request_id'])
        self.opener.assert_not_called()

    def test_interruption_before_journal_write_leaves_reservation_discoverable(self):
        self.provision()
        record = admission.admit_rate_card(self.root)
        journal = studio_recovery.TranslationJournal(self.job)
        client = self.client(admission=record, request_journal=journal)
        with patch.object(studio_recovery, '_save', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                client.translate([segment()])
        self.assertEqual(list(journal.path.iterdir()), [])
        (row,) = self.ledger()[1]
        self.assertEqual(row[3], 'submitted')
        self.assertEqual(self.blocked(client, 'translation_request_unresolved')['blocking_request_id'], row[1])
        self.opener.assert_not_called()

    def test_lost_receipt_after_open_is_locatable_and_never_retried(self):
        self.provision()
        record = admission.admit_rate_card(self.root)
        journal = studio_recovery.TranslationJournal(self.job)
        client = self.client(admission=record, request_journal=journal)
        save = studio_recovery._save

        def lose_reply(path, value):
            if value['request_state'] == 'response_received':
                raise OSError('synthetic receipt write failure')
            save(path, value)

        with patch.object(studio_recovery, '_save', side_effect=lose_reply):
            error = self.blocked(client, 'translation_transport_unknown')
        (row,) = self.ledger()[1]
        self.assertEqual(row[3], 'uncertain')
        evidence = json.loads(row[4])
        receipt = json.loads((journal.path / evidence['receipt']).read_bytes())
        self.assertEqual(row[1], error['local_request_id'])
        self.assertEqual(row[1], receipt['local_request_id'])
        self.assertTrue(evidence['billing_uncertain'])
        self.assertEqual(receipt['completion_state'], 'unknown')
        self.blocked(client, 'translation_request_already_recorded')
        self.blocked(self.client(admission=record), 'translation_request_unresolved')
        self.opener.assert_called_once()

    def race(self, clients_and_batches):
        release = threading.Event()
        start = threading.Barrier(2)

        def opener(request, timeout):
            if not release.wait(10):
                raise AssertionError('second reservation did not finish')
            return self.reply(request, timeout)

        def request(client, batch):
            start.wait(10)
            try:
                client.translate(batch)
            except studio_recovery.StudioRecoveryError as error:
                return error.code
            return 'sent'

        self.opener.side_effect = opener
        with ThreadPoolExecutor(2) as pool:
            attempts = [pool.submit(request, client, batch) for client, batch in clients_and_batches]
            try:
                finished, _ = wait(attempts, timeout=10, return_when=FIRST_COMPLETED)
                self.assertEqual([result.result() for result in finished], ['translation_request_unresolved'])
            finally:
                release.set()
            self.assertCountEqual((result.result() for result in attempts), ['sent', 'translation_request_unresolved'])
        self.opener.assert_called_once()

    def test_concurrent_admission_keeps_one_cumulative_wallet(self):
        self.provision(cap=21_130_000 + ONE_CAP)
        record = admission.admit_rate_card(self.root)
        self.race([(self.client(admission=record), [segment(key)]) for key in ('a', 'b')])
        self.assertEqual(len(self.ledger()[1]), 1)
        self.path.write_bytes(self.path.read_bytes() + b'\n')
        client = self.client(admission=admission.RuntimeAdmission.load(self.root))
        self.blocked(client, 'translation_budget_exhausted', [segment('c')])
        self.opener.assert_called_once()
        self.load_key.assert_called_once()

    def test_concurrent_jobs_cannot_send_same_unresolved_key(self):
        other = self.root / JOBS[1]
        self.provision()
        self.provision(other)
        record = admission.admit_rate_card(self.root)
        self.race([(self.client(job, admission=record), [segment()]) for job in (self.job, other)])


if __name__ == '__main__':
    unittest.main()
