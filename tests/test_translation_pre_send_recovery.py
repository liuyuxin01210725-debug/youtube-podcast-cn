"""Recovery tests use disposable jobs and fake openers."""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from tests.test_translation_budget import NOW, ONE_CAP, TEST_KEY, create, reply, segment
from podcast_cn import create_translation_client
from studio_recovery import LOCAL_SOCKET_DENIED, StudioRecoveryError, TranslationJournal, fingerprint
from translation_budget import ENDPOINT, MODEL, TranslationBudget

# Same reviewed denial mechanism, applied only inside a temporary fixture.
HOOK = """import socket
class _DeniedSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise OSError('network disabled in precommit verification')
def _deny(*args, **kwargs):
    raise OSError('network disabled in precommit verification')
socket.socket = _DeniedSocket
socket.create_connection = _deny
socket.getaddrinfo = _deny
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PreSendRecoveryTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        self.job = self.root / 'fixture-job'
        self.job.mkdir()
        self.now = NOW
        clock = patch('translation_budget.time.time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        create(self.job, ONE_CAP + 17, prior_microunits=17)
        self.budget = TranslationBudget(self.job)
        self.journal = TranslationJournal(self.job)
        self.hook = self.root / 'sitecustomize.py'
        self.hook.write_text(HOOK)
        os.utime(self.hook, (NOW - 100, NOW - 100))

    def translator(self, opener, *, runtime=False):
        return create_translation_client(
            'deepseek', credential_loader=lambda: TEST_KEY, opener=opener,
            budget=self.budget, request_journal=self.journal,
            **({} if runtime else {'model': MODEL, 'base_url': ENDPOINT}),
        )

    def recover(self, evidence):
        return self.budget.recover_pre_send(isolation_root=self.root, evidence=evidence)

    def inspect(self, evidence):
        return self.budget.inspect_pre_send_recovery(isolation_root=self.root, evidence=evidence)

    def continuity(self, evidence, audit_digest, *, apply=False):
        return self.budget.recovery_continuity(
            isolation_root=self.root, request_key=evidence['request_key'],
            audit_sha256=audit_digest, apply=apply,
        )

    def assert_continuity_denied(self, evidence, audit_digest):
        before = self.budget.path.read_bytes()
        for apply in (False, True):
            with self.assertRaises(StudioRecoveryError) as rejected:
                self.continuity(evidence, audit_digest, apply=apply)
            self.assertEqual(rejected.exception.code, 'translation_continuity_invalid')
        self.assertEqual(self.budget.path.read_bytes(), before)

    def fail_initial(self, *, typed=True, runtime=False):
        with ExitStack() as stack:
            def opener(request, timeout):
                if not typed:
                    raise URLError(OSError('synthetic historical detail'))
                hook = types.ModuleType('sitecustomize')
                hook.__file__ = str(self.hook)
                stack.enter_context(patch.dict(sys.modules, sitecustomize=hook))
                stack.enter_context(patch.multiple(
                    socket, socket=socket.socket, create_connection=socket.create_connection,
                    getaddrinfo=socket.getaddrinfo,
                ))
                exec(compile(HOOK, self.hook, 'exec'), vars(hook))
                try:
                    socket.create_connection()
                except OSError as error:
                    raise URLError(error)
            receipt, evidence = self.record_failure(opener, remove_transport=not typed, runtime=runtime)
        if typed:
            self.assertEqual(json.loads(receipt.read_bytes())['transport_failure'], LOCAL_SOCKET_DENIED)
        return receipt, evidence

    def record_failure(self, opener, *, remove_transport=False, code='translation_transport_unknown', runtime=False):
        with self.assertRaises(StudioRecoveryError) as failed:
            self.translator(opener, runtime=runtime).translate([segment()])
        self.assertEqual(failed.exception.code, code)
        old = failed.exception.evidence
        with closing(self.budget._connect()) as db, db:
            (key,), = db.execute('SELECT request_key FROM requests')
            if remove_transport:
                old.pop('transport_failure')
                db.execute('UPDATE requests SET evidence = ?', (json.dumps(old),))
        receipt, = self.journal.path.glob('*.json')
        if remove_transport:
            undecorated = json.loads(receipt.read_bytes())
            undecorated.pop('transport_failure')
            receipt.write_text(json.dumps(undecorated))
        return receipt, {
            'schema_version': 1, 'job': str(self.job), 'request_key': key,
            'receipt': receipt.name, 'receipt_sha256': sha(receipt),
            'proof': {'kind': 'recorded_transport'},
        }

    def legacy_proof(self, receipt, evidence):
        saved = json.loads(receipt.read_bytes())
        failure = self.job / 'debug-failure.json'
        failure.write_text(json.dumps({
            'stage': 'translation', 'exc_type': 'TranslationRequestError', 'updated_at': NOW + 0.5,
            'translation_request': {
                **saved, 'receipt': receipt.name, 'code': 'translation_transport_unknown',
            },
        }))
        scripts = str(self.root / 'scripts')
        pythonpath = [scripts, str(self.hook.parent)]
        witness = self.root / 'fixture-witness.json'
        witness.write_text(json.dumps({
            'job_id': self.job.name, 'receipt': receipt.name, 'receipt_sha256': sha(receipt),
            'request_key': evidence['request_key'], 'local_id': saved['local_request_id'],
            'pythonpath_entries': pythonpath, 'sitecustomize_sha256': sha(self.hook),
            'error_code': 'translation_transport_unknown', 'before_open_passed': True,
            'connection_established': False,
        }))
        return {
            'kind': 'inherited_socket_hook', 'hook_path': str(self.hook),
            'receipt': receipt.name, 'pythonpath': pythonpath, 'inherited_pythonpath': pythonpath[1:],
            'continuous_exported_shell': True, 'observer_loaded_socket_hook': True,
            'python_executable': str(self.root / 'bin/python'), 'python_flags': ['-u'],
            'prepare_script': str(Path(scripts) / 'studio_engine.py'), 'action': 'prepare',
            'started_at': NOW - 1, 'ended_at': NOW + 1, 'observed_at': NOW + 2,
            'witness_path': str(witness), 'witness_sha256': sha(witness),
            'failure_sha256': sha(failure),
        }

    def assert_recovery_denied(self, evidence):
        before = self.budget.path.read_bytes()
        receipt = self.journal.path / evidence['receipt']
        original = receipt.read_bytes()
        for apply in (self.inspect, self.recover):
            with self.assertRaisesRegex(StudioRecoveryError, '未能核实') as rejected:
                apply(evidence)
            self.assertEqual(rejected.exception.code, 'translation_recovery_invalid')
        self.assertEqual(self.budget.path.read_bytes(), before)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertFalse((self.job / 'translation-recoveries').exists())

    def test_recorded_proof_releases_reservation_and_one_linked_attempt(self):
        receipt, evidence = self.fail_initial()
        original = receipt.read_bytes()
        original_record = json.loads(original)
        opener = Mock(side_effect=lambda request, timeout: reply(json.loads(request.data)))
        translator = self.translator(opener)
        with self.assertRaisesRegex(StudioRecoveryError, '此块已有请求记录'):
            translator.translate([segment()])
        with self.assertRaisesRegex(StudioRecoveryError, '此任务仍有待核对的翻译请求'):
            translator.translate([segment('other')])
        opener.assert_not_called()
        expected = self.inspect(evidence)
        self.assertFalse((self.job / 'translation-recoveries').exists())
        self.assertEqual(self.recover(evidence), expected)
        self.assertEqual(self.recover(evidence), expected)
        self.assertEqual(expected['released_microunits'], ONE_CAP)
        self.assertEqual(expected['prior_microunits'], 17)
        audit, = (self.job / 'translation-recoveries').glob('*.json')
        self.assertEqual(json.loads(audit.read_bytes()), expected)
        self.assert_continuity_denied(evidence, sha(audit))
        with self.assertRaises(StudioRecoveryError):
            translator.translate([segment(text='different synthetic source')])
        opener.assert_not_called()
        self.assertEqual(translator.translate([segment()]), {'a': '合成译文'})
        with self.assertRaises(StudioRecoveryError):
            translator.translate([segment()])
        opener.assert_called_once()
        self.assertEqual(receipt.read_bytes(), original)
        successor, = set(self.journal.path.glob('*.json')) - {receipt}
        resumed = json.loads(successor.read_bytes())
        self.assertEqual(resumed['resumes_receipt'], receipt.name)
        self.assertEqual(resumed['segment_ids'], original_record['segment_ids'])
        for field in ('request_key', 'local_request_id'):
            self.assertNotEqual(resumed[field], original_record[field])
        with closing(self.budget._connect(readonly=True)) as db:
            _, history = self.budget._history(db)
            self.assertEqual(sum(row.reserved or 0 for row in history), ONE_CAP)
            self.assertEqual(db.execute('SELECT prior_microunits FROM allowance').fetchone(), (17,))

    def test_remote_evidence_in_either_artifact_blocks_recovery(self):
        receipt, evidence = self.fail_initial()
        initial_receipt = json.loads(receipt.read_bytes())
        with closing(self.budget._connect()) as db:
            (initial_wallet,), = db.execute('SELECT evidence FROM requests')
            for field, value in (
                ('provider_request_id', '1' * 32), ('provider_response_id', '2' * 32),
                ('http_status', 429), ('usage', {'total_tokens': 1}),
            ):
                with self.subTest(artifact='wallet', field=field):
                    with db:
                        db.execute('UPDATE requests SET evidence = ?',
                                   (json.dumps({**json.loads(initial_wallet), field: value}),))
                    self.assert_recovery_denied(evidence)
                    with db:
                        db.execute('UPDATE requests SET evidence = ?', (initial_wallet,))
                with self.subTest(artifact='journal', field=field):
                    receipt.write_text(json.dumps({**initial_receipt, field: value}))
                    self.assert_recovery_denied({**evidence, 'receipt_sha256': sha(receipt)})
                    receipt.write_text(json.dumps(initial_receipt))
                    evidence['receipt_sha256'] = sha(receipt)

    def test_forged_launch_witness_with_matching_artifacts_cannot_release(self):
        receipt, evidence = self.fail_initial(typed=False)
        proof = self.legacy_proof(receipt, evidence)
        self.assert_recovery_denied({**evidence, 'proof': proof})
        opener = Mock(side_effect=AssertionError('uncertain successor'))
        with self.assertRaises(StudioRecoveryError):
            self.translator(opener).translate([segment()])
        opener.assert_not_called()

    def test_launch_witness_without_original_failure_cannot_release(self):
        receipt, evidence = self.fail_initial(typed=False)
        proof = self.legacy_proof(receipt, evidence)
        (self.job / 'debug-failure.json').unlink()
        self.assert_recovery_denied({**evidence, 'proof': proof})

    def test_untyped_or_one_sided_transport_diagnostic_cannot_release(self):
        def opener(request, timeout):
            raise URLError(OSError('synthetic open error'))

        receipt, evidence = self.record_failure(opener)
        original = json.loads(receipt.read_bytes())
        self.assertIsNone(original['http_status'])
        self.assertNotEqual(original['transport_failure'], LOCAL_SOCKET_DENIED)
        self.assert_recovery_denied(evidence)
        receipt.write_text(json.dumps({**original, 'transport_failure': LOCAL_SOCKET_DENIED}))
        self.assert_recovery_denied({**evidence, 'receipt_sha256': sha(receipt)})

    def legacy_release_with_successor(self, *, findings=None):
        receipt, evidence = self.fail_initial()
        audit = self.recover(evidence)
        fake_success = lambda request, timeout: reply(json.loads(request.data))
        self.assertEqual(self.translator(fake_success).translate([segment()]), {'a': '合成译文'})
        successor, = set(self.journal.path.glob('*.json')) - {receipt}
        successor_bytes = successor.read_bytes()

        # Recreate a pre-upgrade journal and wallet on an already linked fixture.
        untyped = json.loads(receipt.read_bytes())
        del untyped['transport_failure']
        receipt.write_text(json.dumps(untyped))
        proof = self.legacy_proof(receipt, evidence)
        evidence.update(proof=proof, receipt_sha256=sha(receipt))
        audit.update(findings=findings or {'kind': 'inherited_socket_hook', 'failure_sha256': proof['failure_sha256']},
                     proof_sha256=fingerprint(proof), receipt_sha256=sha(receipt))
        audit_path = self.job / 'translation-recoveries' / receipt.name
        audit_path.write_text(json.dumps(audit))
        with closing(self.budget._connect()) as db, db:
            (raw,), = db.execute('SELECT evidence FROM requests WHERE request_key = ?',
                                 (evidence['request_key'],))
            old = json.loads(raw)
            del old['transport_failure']
            old['recovery_audit_sha256'] = sha(audit_path)
            db.execute('UPDATE requests SET evidence = ? WHERE request_key = ?',
                       (json.dumps(old), evidence['request_key']))
        return receipt, audit_path, successor, evidence

    def test_existing_legacy_audit_preserves_successor_but_cannot_authorize_dispatch(self):
        receipt, audit_path, successor, evidence = self.legacy_release_with_successor()
        original = {path: path.read_bytes() for path in (self.budget.path, receipt, audit_path, successor)}
        for apply in (self.inspect, self.recover):
            with self.assertRaises(StudioRecoveryError) as rejected:
                apply(evidence)
            self.assertEqual(rejected.exception.code, 'translation_recovery_invalid')
        self.assertFalse(self.budget._can_resume(receipt.name, json.loads(original[audit_path])['local_id']))
        self.assert_continuity_denied(evidence, '0' * 64)
        audit_digest = sha(audit_path)
        pending = self.continuity(evidence, audit_digest)
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(pending['history_microunits'], 2 * ONE_CAP)
        self.assertEqual(pending['excess_over_cap_microunits'], ONE_CAP)
        self.assertEqual(pending['record']['billing_state'], 'unknown')
        self.assertEqual(pending['record']['completion_state'], 'unknown')
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        with closing(self.budget._connect(readonly=True)) as db:
            allowance = db.execute('SELECT * FROM allowance').fetchall()
            wallet = db.execute('SELECT * FROM requests').fetchall()
        self.assertEqual(self.continuity(evidence, audit_digest, apply=True), {**pending, 'status': 'recorded'})
        original[self.budget.path] = self.budget.path.read_bytes()
        for apply in (True, True, False):
            self.assertEqual(self.continuity(evidence, audit_digest, apply=apply),
                             {**pending, 'status': 'already_recorded'})
        opener = Mock(side_effect=AssertionError('unexpected dispatch'))
        with self.assertRaises(StudioRecoveryError) as duplicate:
            self.translator(opener).translate([segment()])
        self.assertEqual(duplicate.exception.code, 'translation_request_already_recorded')
        with self.assertRaises(StudioRecoveryError) as blocked:
            self.translator(opener).translate([segment('other')])
        self.assertEqual(blocked.exception.code, 'translation_request_unresolved')
        opener.assert_not_called()
        with closing(self.budget._connect(readonly=True)) as db:
            _, history = self.budget._history(db)
            self.assertEqual(db.execute('SELECT * FROM allowance').fetchall(), allowance)
            self.assertEqual(db.execute('SELECT * FROM requests').fetchall(), wallet)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM recovery_continuity').fetchone(), (1,))
        self.assertEqual(sum(row.reserved or 0 for row in history), 2 * ONE_CAP)
        self.assertEqual(json.loads(original[successor])['http_status'], 200)
        self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_recognized_label_without_recorded_evidence_does_not_unlock(self):
        receipt, audit, _, evidence = self.legacy_release_with_successor(
            findings={'kind': 'recorded_transport', 'transport_failure': LOCAL_SOCKET_DENIED},
        )
        self.assertFalse(self.budget._can_resume(receipt.name, json.loads(audit.read_bytes())['local_id']))
        self.assertEqual(self.continuity(evidence, sha(audit))['status'], 'pending')

    def test_continuity_rollback_and_replay(self):
        receipt, audit, successor, evidence = self.legacy_release_with_successor()
        original = {path: path.read_bytes() for path in (self.budget.path, receipt, audit, successor)}
        apply_continuity = self.budget._continuity

        def interrupt(db, key, digest, apply):
            self.assertEqual(apply_continuity(db, key, digest, apply)['status'], 'recorded')
            raise OSError('fixture fault before commit')

        with patch.object(self.budget, '_continuity', side_effect=interrupt):
            with self.assertRaises(StudioRecoveryError) as fault:
                self.continuity(evidence, sha(audit), apply=True)
        self.assertEqual(fault.exception.code, 'translation_continuity_invalid')
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(self.continuity(evidence, sha(audit))['status'], 'pending')
        self.assertEqual(self.continuity(evidence, sha(audit), apply=True)['status'], 'recorded')

    def test_audit_file_without_sqlite_commit_cannot_unlock_and_can_be_retried(self):
        receipt, evidence = self.fail_initial()
        original = receipt.read_bytes()
        from translation_budget import _save

        def save_then_stop(*args, **kwargs):
            _save(*args, **kwargs)
            raise OSError('synthetic interruption')

        with patch('translation_budget._save', side_effect=save_then_stop):
            with self.assertRaisesRegex(StudioRecoveryError, '未能核实'):
                self.recover(evidence)
        audit, = (self.job / 'translation-recoveries').iterdir()
        self.assert_continuity_denied(evidence, sha(audit))
        with closing(self.budget._connect(readonly=True)) as db:
            self.assertEqual(db.execute('SELECT state FROM requests').fetchone(), ('uncertain',))
        opener = Mock(side_effect=AssertionError('uncertain successor'))
        with self.assertRaises(StudioRecoveryError):
            self.translator(opener).translate([segment()])
        opener.assert_not_called()
        self.assertEqual(self.recover(evidence), json.loads(audit.read_bytes()))
        self.assertEqual(receipt.read_bytes(), original)
        audit.unlink()
        with self.assertRaises(StudioRecoveryError):
            self.translator(opener).translate([segment()])
        opener.assert_not_called()

    def test_existing_before_open_denial_can_be_audited(self):
        from studio_recovery import _save

        def save_then_expire(path, value, **kwargs):
            _save(path, value, **kwargs)
            self.now = NOW + 60

        opener = Mock(side_effect=AssertionError('expired dispatch'))
        with patch('studio_recovery._save', side_effect=save_then_expire):
            receipt, evidence = self.record_failure(opener, code='translation_allowance_invalid')
        opener.assert_not_called()
        self.now = NOW
        proof = {**evidence, 'proof': {'kind': 'recorded_before_open'}}
        self.assertEqual(self.recover(proof)['original_request_state'], 'not_sent')
        self.assert_continuity_denied(evidence, sha(self.job / 'translation-recoveries' / receipt.name))
        self.assertEqual(json.loads(receipt.read_bytes())['request_state'], 'not_sent')
        translator = self.translator(lambda request, timeout: reply(json.loads(request.data)))
        self.assertEqual(translator.translate([segment()]), {'a': '合成译文'})

    def test_recovered_batch_still_needs_current_runtime_admission(self):
        from tests.test_translation_runtime_admission import EXPIRES, JOBS, OBSERVED
        from translation_admission import admit_rate_card
        from translation_budget import CONSERVED_PRIOR_MICROUNITS, POLICY_EXPIRES_AT

        self.job = self.root / JOBS[0]
        self.job.mkdir()
        self.budget = TranslationBudget(self.job)
        self.journal = TranslationJournal(self.job)
        create(self.job, 22_000_000, prior_microunits=CONSERVED_PRIOR_MICROUNITS,
               expires_at=POLICY_EXPIRES_AT)
        self.now = OBSERVED + 1
        admit_rate_card(self.root)
        receipt, evidence = self.fail_initial(runtime=True)
        original = receipt.read_bytes()
        self.assertEqual(self.recover(evidence)['prior_microunits'], CONSERVED_PRIOR_MICROUNITS)

        opener = Mock(side_effect=lambda request, timeout: reply(json.loads(request.data)))
        translator = self.translator(opener, runtime=True)
        self.now = EXPIRES
        with self.assertRaises(StudioRecoveryError) as stale:
            translator.translate([segment()])
        self.assertEqual(stale.exception.code, 'translation_admission_stale')
        opener.assert_not_called()
        self.now = OBSERVED + 1
        self.assertEqual(translator.translate([segment()]), {'a': '合成译文'})
        self.assertEqual(receipt.read_bytes(), original)
        opener.assert_called_once()
