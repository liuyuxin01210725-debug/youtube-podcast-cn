"""The November 23 acknowledgement is finite, episode-scoped and offline."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_translation_budget import ONE_CAP, TEST_KEY, reply, segment
import podcast_cn
import studio_recovery
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
        self.opener = Mock(side_effect=lambda req, timeout: reply(json.loads(req.data)))
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
                self.denial('translation_budget_exhausted', lambda: client.translate([segment()]))
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

    def test_sufficient_synthetic_allowance_only_sends_once(self):
        # Exercise successful transport without enlarging any reviewed job.
        self.job = self.root / ('b' * 32)
        self.job.mkdir()
        with patch.dict(rates._EPISODE_CAPS, {self.job.name: ONE_CAP}):
            self.provision(ONE_CAP)
            client = self.client()
            self.assertEqual(client.translate([segment()]), {'a': '合成译文'})
            with closing(client._budget._connect(readonly=True)) as db:
                amount, raw = db.execute('SELECT reserved_microunits, evidence FROM requests').fetchone()
                self.assertEqual(amount, ONE_CAP)
                self.assertEqual(json.loads(raw)['rate_observation_sha256'], rates._EPISODE_OBSERVATION_SHA)
            with self.assertRaises(TranslationRequestError) as error:
                client.translate([segment('next')])
            self.assertEqual(error.exception.code, 'translation_budget_exhausted')
        self.key.assert_called_once()
        self.opener.assert_called_once()

    def test_exact_predecessor_still_checks_draft_and_references(self):
        path = self.job / 'translation-source.json'
        draft = {'references': {'s': {'path': 'ref', 'sha256': 'test-digest'}}}
        studio_recovery.save_source(path, 'old', draft)
        predecessor = hashlib.sha256(path.read_bytes()).hexdigest()

        def load(ref, accepted=None):
            return studio_recovery.load_source(path, 'new', lambda name: ref, predecessor_sha256=accepted)

        self.assertEqual(load('test-digest', predecessor), draft)
        for ref, accepted in [('test-digest', None), ('changed', predecessor)]:
            with self.assertRaises(studio_recovery.StudioRecoveryError):
                load(ref, accepted)
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaises(studio_recovery.StudioRecoveryError):
            load('test-digest', predecessor)
