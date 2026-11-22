"""Exercise isolated history adoption through the production translation client."""

from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
import urllib.request
from unittest.mock import Mock, patch

from tests.test_translation_budget import NOW, ONE_CAP, TEST_KEY, create, reply, segment
from tests.test_translation_runtime_admission import JOBS, OBSERVED

import podcast_cn
from studio_recovery import TranslationJournal
from translation_admission import RuntimeAdmission, admit_rate_card
from translation_budget import (
    CONSERVED_PRIOR_MICROUNITS, ENDPOINT, FEE_MODE, MODEL_REVISION, POLICY_EXPIRES_AT,
    TranslationBudget, TranslationRequestError,
)


def write_book(job, rows):
    path = job / 'request-ledger.json'
    path.write_text(json.dumps({
        'schema_version': 1,
        'requests': {
            key: {
                'state': state, 'reserved_cost_microunits': amount,
                'stage': 'tts' if amount == 0 else 'translation',
                'reserved_characters': 10, 'provider': 'synthetic', 'model': 'synthetic',
                'submitted_at': '2026-11-19T00:00:00+00:00', 'allowance_evidence': 'synthetic',
            }
            for key, state, amount in rows
        },
    }))
    return path


def write_receipt(job, *, billing='unknown', request_key=None):
    journal = TranslationJournal(job)
    path, receipt = journal.preflight(ENDPOINT, b'old synthetic request', ['old-receipt'])
    journal.path.mkdir()
    receipt.update(request_state='response_received', billing_state=billing, completion_state='provider_reported_stop')
    if request_key is not None:
        receipt['request_key'] = request_key
    path.write_text(json.dumps(receipt))
    return path, receipt['local_request_id']


class TranslationHistoryAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.job = self.root / JOBS[0]
        self.job.mkdir()
        self.enterContext(patch('translation_budget.time.time', return_value=OBSERVED + 1))
        self.load_key = Mock(return_value=TEST_KEY)
        self.opener = Mock(side_effect=lambda request, timeout: reply(json.loads(request.data), model=MODEL_REVISION))
        deny = Mock(side_effect=AssertionError('external network used during offline test'))
        for target, name in (
            (socket.socket, 'connect'), (socket.socket, 'connect_ex'), (socket, 'getaddrinfo'),
            (socket, 'create_connection'), (urllib.request.OpenerDirector, 'open'),
        ):
            self.enterContext(patch.object(target, name, deny))
        self.addCleanup(deny.assert_not_called)

    def adopt(self, job=None, **options):
        job = job or self.job
        return TranslationBudget.adopt(job, **{
            'isolation_root': job.parent, 'authorization_id': '1' * 32, 'fee_mode': FEE_MODE,
            'cap_microunits': 22_000_000, 'prior_microunits': CONSERVED_PRIOR_MICROUNITS,
            'verified_at': NOW - 60, 'expires_at': POLICY_EXPIRES_AT, **options,
        })

    def client(self, job=None, *, journal=False):
        job = job or self.job
        return podcast_cn.create_translation_client(
            'deepseek', budget=TranslationBudget(job), opener=self.opener, credential_loader=self.load_key,
            request_journal=TranslationJournal(job) if journal else None,
        )

    def block(self, code, client, batch):
        with self.assertRaises(TranslationRequestError) as raised:
            client.translate(batch)
        self.assertEqual(raised.exception.code, code)
        return raised.exception.evidence

    def wire_key(self, batch):
        client = self.client()
        stop = TranslationRequestError('translation_allowance_missing', {})
        with patch.object(TranslationBudget, 'reserve', side_effect=stop) as reserve:
            self.block(stop.code, client, batch)
        return reserve.call_args.args[0]

    def test_adopted_short_sample_opens_once_and_preserves_every_historical_record(self):
        admit_rate_card(self.root)
        old_batch = [segment('book-key')]
        ledger = write_book(self.job, [
            (self.wire_key(old_batch), 'succeeded', 123), ('b' * 64, 'abandoned_unusable_output', 45),
        ])
        receipt, old_id = write_receipt(self.job, billing='usage_verified')
        before = {path: path.read_bytes() for path in (ledger, receipt)}
        budget = self.adopt()
        with closing(budget._connect(readonly=True)) as db:
            original = db.execute('SELECT snapshot FROM adopted_history').fetchone()
            adopted, history = budget._history(db)
            self.assertTrue(adopted)
            self.assertEqual(sum(row.reserved or 0 for row in history), 168)
            self.assertIn((receipt.name, old_id), {(row.receipt, row.local_id) for row in history})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone(), (0,))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

        client = self.client(journal=True)
        self.block('translation_request_already_recorded', client, old_batch)
        self.block('translation_request_already_recorded', self.client(), [segment('old-receipt', 'changed source')])
        self.opener.assert_not_called()
        self.load_key.assert_not_called()
        self.assertEqual(client.translate([segment('cut-01')]), {'cut-01': '合成译文'})
        self.opener.assert_called_once()
        self.load_key.assert_called_once()

        with closing(budget._connect(readonly=True)) as db:
            self.assertEqual(db.execute('SELECT snapshot FROM adopted_history').fetchone(), original)
            self.assertEqual(db.execute('SELECT prior_microunits FROM allowance').fetchone(),
                             (CONSERVED_PRIOR_MICROUNITS,))
            _, after = budget._history(db)
            self.assertEqual((after[0].reserved, after[0].state), (ONE_CAP, 'completed'))
            (key, raw), = db.execute('SELECT request_key, evidence FROM requests').fetchall()
            record = json.loads(raw)
            self.assertCountEqual((row for row in after if row.local_id != record['local_request_id']), history)
            self.assertTrue(record['admission_event']['legacy_cap_lifted'])
            self.assertEqual(record['admission_event']['history_microunits'], 168)
            saved = json.loads((receipt.parent / record['receipt']).read_bytes())
            self.assertEqual((saved['local_request_id'], saved['billing_state']),
                             (record['local_request_id'], 'usage_verified'))
            self.assertEqual(saved['request_key'], key)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_book_holds_and_journal_only_unknown_block_job_card_and_source_switches(self):
        other = self.root / JOBS[1]
        other.mkdir()
        record = admit_rate_card(self.root)
        # The book's four known states plus a journal whose provider reported
        # completion while its billing is still unknown.
        ledger = write_book(self.job, [
            ('1' * 64, 'succeeded', 500), ('2' * 64, 'abandoned_unusable_output', 100),
            ('3' * 64, 'reconciled_no_remote_completion', 39_662), ('4' * 64, 'uncertain', 0),
        ])
        receipt, local_id = write_receipt(self.job)
        before = {path: path.read_bytes() for path in (ledger, receipt)}
        self.adopt(other)
        changed_batch = [segment('different', 'other source')]
        blocked = self.block('translation_request_unresolved', self.client(other), changed_batch)
        self.assertEqual((blocked['blocking_job_id'], blocked['blocking_state']),
                         (self.job.name, 'reconciled_no_remote_completion'))
        self.assertFalse((self.job / TranslationBudget.FILENAME).exists())
        budget = self.adopt()
        admission_path = self.root / RuntimeAdmission.FILENAME
        admission_path.write_bytes(admission_path.read_bytes() + b'\n')
        self.assertNotEqual(RuntimeAdmission.load(self.root), record)
        self.block('translation_request_unresolved', self.client(other), changed_batch)
        self.block('translation_request_unresolved', self.client(), changed_batch)
        with closing(budget._connect(readonly=True)) as db:
            _, history = budget._history(db)
            self.assertEqual(sum(row.reserved or 0 for row in history), 40_262)
            self.assertEqual({row.state: row.reserved for row in history if row.unresolved and row.receipt is None},
                             {'reconciled_no_remote_completion': 39_662, 'uncertain': 0})
            self.assertIn((receipt.name, local_id, True),
                          {(row.receipt, row.local_id, row.unresolved) for row in history})
        self.assertEqual({path: path.read_bytes() for path in before}, before)

        # A sibling without a wallet or book ledger can still hold a receipt.
        separated = self.root / 'separate-isolation'
        journal_only = separated / 'journal-only'
        journal_only.mkdir(parents=True)
        _, journal_id = write_receipt(journal_only)
        replacement = separated / self.job.name
        replacement.mkdir()
        self.adopt(replacement)
        admit_rate_card(separated)
        blocked = self.block('translation_request_unresolved', self.client(replacement), changed_batch)
        self.assertEqual((blocked['blocking_job_id'], blocked['blocking_request_id']),
                         (journal_only.name, journal_id))
        self.opener.assert_not_called()
        self.load_key.assert_not_called()

    def test_submitted_and_reconciliation_required_from_book_are_held(self):
        self.adopt()
        admit_rate_card(self.root)
        sibling = self.root / 'book'
        sibling.mkdir()
        for state in ('submitted', 'uncertain', 'reconciliation_required'):
            with self.subTest(state=state):
                write_book(sibling, [('1' * 64, state, 7)])
                blocked = self.block('translation_request_unresolved', self.client(), [segment()])
                self.assertEqual(blocked['blocking_state'], state)
        self.opener.assert_not_called()
        self.load_key.assert_not_called()

    def test_journal_key_blocks_after_source_path_changes_without_sqlite(self):
        self.adopt()
        admit_rate_card(self.root)
        batch = [segment('old-wire-key')]
        history = self.root / 'receipt-history'
        history.mkdir()
        _, local_id = write_receipt(history, billing='usage_verified', request_key=self.wire_key(batch))
        moved = history.rename(self.root / 'moved-history')
        denied = self.block('translation_request_already_recorded', self.client(), batch)
        self.assertEqual((denied['blocking_job_id'], denied['blocking_request_id']), (moved.name, local_id))
        self.opener.assert_not_called()
        self.load_key.assert_not_called()

    def test_corrupt_symlinked_or_removed_history_fails_closed(self):
        admit_rate_card(self.root)
        ledger = write_book(self.job, [('1' * 64, 'succeeded', 7)])
        self.adopt()
        sibling = self.root / JOBS[1]
        sibling.mkdir()
        bad = sibling / 'request-ledger.json'
        bad.write_text('{"schema_version":1,"requests":{"truncated"')
        self.block('translation_ledger_unavailable', self.client(), [segment()])
        bad.unlink()
        bad.symlink_to(ledger)
        self.block('translation_ledger_unavailable', self.client(), [segment()])
        bad.unlink()
        journal, _ = write_receipt(sibling)
        journal.write_text('{}')
        self.block('translation_ledger_unavailable', self.client(), [segment()])
        journal.unlink()
        journal.symlink_to(ledger)
        self.block('translation_ledger_unavailable', self.client(), [segment()])
        journal.unlink()
        ledger.rename(self.job / 'other-source.json')
        self.block('translation_ledger_unavailable', self.client(), [segment()])
        self.opener.assert_not_called()
        self.load_key.assert_not_called()

    def test_existing_sqlite_and_read_only_sources_share_the_history_view(self):
        admit_rate_card(self.root)
        create(self.job, 22_000_000, prior_microunits=CONSERVED_PRIOR_MICROUNITS, clock=lambda: NOW)
        old_batch = [segment('previous-sqlite')]
        budget = TranslationBudget(self.job)
        with closing(budget._connect()) as db, db:
            db.execute('INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)', (
                self.wire_key(old_batch), 'c' * 64, ONE_CAP, 'completed', '{}', NOW,
            ))
        ledger = write_book(self.job, [('b' * 64, 'succeeded', 53)])
        receipt, _ = write_receipt(self.job, billing='usage_verified')
        before = {path: path.read_bytes() for path in (budget.path, ledger, receipt)}
        with closing(budget._connect(readonly=True)) as db:
            adopted, history = budget._history(db)
        self.assertFalse(adopted)
        self.assertEqual(sum(row.reserved or 0 for row in history), ONE_CAP + 53)
        self.block('translation_request_already_recorded', self.client(), old_batch)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.opener.assert_not_called()
        self.load_key.assert_not_called()

    def test_no_wallet_zero_prior_and_protected_targets(self):
        admit_rate_card(self.root)
        self.block('translation_allowance_missing', self.client(), [segment()])
        self.assertFalse((self.job / TranslationBudget.FILENAME).exists())
        with self.assertRaises(ValueError):
            self.adopt(prior_microunits=0)
        with self.assertRaises(ValueError):
            self.adopt(cap_microunits=85_610_000)
        create(self.job, 22_000_000, prior_microunits=0, clock=lambda: NOW)
        self.block('translation_allowance_invalid', self.client(), [segment()])

        fake_home = self.root / 'home'
        for relative in (
            '音频/中文播客工作台/jobs', 'shengjian-book-jobs-isolated/food-is-medicine-mozaffarian-2026-11-13',
        ):
            with self.subTest(root=relative), patch.object(Path, 'home', return_value=fake_home):
                protected = fake_home / 'Desktop' / relative / 'job'
                protected.mkdir(parents=True)
                alias = self.root / 'alias'
                alias.symlink_to(protected.parent, target_is_directory=True)
                with self.assertRaises(ValueError):
                    self.adopt(protected)
                with self.assertRaises(ValueError):
                    self.adopt(alias / protected.name, isolation_root=protected.parent)
                self.assertEqual(list(protected.iterdir()), [])
                alias.unlink()
        self.opener.assert_not_called()
        self.load_key.assert_not_called()
