"""Offline / fake-network tests for authorization vs rate freshness.

Loads the frozen 19:00 official fact pack and the current admitted constants.
Does not mutate POLICY_EXPIRES_AT, does not rewind the production clock, does
not reset 21.13 / unknown, and does not open a real provider request.
"""
from __future__ import annotations

import io
import json
import pathlib
import socket
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))

import podcast_cn
import translation_admission as admission
from translation_budget import (
    ENDPOINT,
    INPUT_MICRO_CNY,
    MODEL,
    MODEL_REVISION,
    OUTPUT_MICRO_CNY,
    POLICY,
    POLICY_EXPIRES_AT,
    POLICY_VERIFIED_AT,
)

FIX = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'rate-card-snapshot-2026-11-20-1900.json'
# Byte-identical copy of the 19:00 evidence pack (sha recorded in the test).
FACT_PACK_SHA256 = '373988fa996f38c4406d8439bcf550a7a78eeeed0d7eb598522190de86171944'
# Production admitted expiry, already in the past at 19:00 retrieval and still past now.
FROZEN_POLICY_EXPIRES_AT = 1795135740  # 2026-11-20 08:49:00 +0800


def _block_network(self_or_cls=None, *args, **kwargs):
    raise AssertionError('offline test must not open a network connection')


class FakeNetworkTests(unittest.TestCase):
    def setUp(self):
        self._socket_patch = patch.object(socket, 'create_connection', side_effect=_block_network)
        self._socket_patch.start()

    def tearDown(self):
        self._socket_patch.stop()

    def test_expired_freshness_blocks_even_with_scope(self):
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(True, source='existing-batch-authorization'),
                card=admission.RateCard(fresh=False),
            ),
            admission.DENY_FRESHNESS,
        )

    def test_missing_scope_blocks_even_with_fresh_card(self):
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(False),
                card=admission.RateCard(fresh=True),
            ),
            admission.DENY_AUTHORIZATION,
        )

    def test_bound_cache_reuse_does_not_need_either_gate(self):
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(False),
                card=admission.RateCard(fresh=False),
                cache_hit=True,
            ),
            admission.REUSE_BOUND_CACHE,
        )

    def test_fresh_matching_card_does_not_auto_open_spend(self):
        # A just-fetched official page is a fact pack, not a new allowance.
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(True, source='existing-batch-authorization'),
                card=admission.RateCard(fresh=True, model_matches=True),
            ),
            admission.DENY_UNTIL_RUNTIME_ADMISSION,
        )

    def test_model_mismatch_is_freshness_failure(self):
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(True),
                card=admission.RateCard(fresh=True, model_matches=False),
            ),
            admission.DENY_FRESHNESS,
        )


class FrozenFactPackAndCurrentConstantsTests(unittest.TestCase):
    def setUp(self):
        self._socket_patch = patch.object(socket, 'create_connection', side_effect=_block_network)
        self._socket_patch.start()
        self.pack = admission.load_fact_pack(FIX)

    def tearDown(self):
        self._socket_patch.stop()

    def test_fixture_is_the_1900_fact_pack_and_expiry_is_unchanged(self):
        import hashlib
        digest = hashlib.sha256(FIX.read_bytes()).hexdigest()
        self.assertEqual(digest, FACT_PACK_SHA256)
        self.assertEqual(POLICY_EXPIRES_AT, FROZEN_POLICY_EXPIRES_AT)
        self.assertEqual(POLICY_EXPIRES_AT, POLICY_VERIFIED_AT + 86400)
        self.assertEqual(POLICY, 'deepseek-flash-cny-2026-11-19-v1')
        self.assertEqual((MODEL, MODEL_REVISION, ENDPOINT), (
            'deepseek-flash', 'DeepSeek-V4.1-Flash-1017', 'https://api.deepseek.com',
        ))
        self.assertEqual((INPUT_MICRO_CNY, OUTPUT_MICRO_CNY), (2, 8))
        self.assertEqual(self.pack['runtime_status']['POLICY_EXPIRES_AT'], POLICY_EXPIRES_AT)
        self.assertTrue(self.pack['runtime_status']['freshness_expired_at_retrieval'])
        self.assertTrue(self.pack['no_paid_request'])

    def test_1900_official_observation_still_matches_admitted_peak_rates(self):
        self.assertTrue(admission.observed_matches_admitted(self.pack))
        observed = self.pack['observed_deepseek_flash']
        self.assertEqual(observed['api_name'], MODEL)
        self.assertEqual(observed['input_cache_miss']['peak'], 2.0)
        self.assertEqual(observed['output']['peak'], 8.0)

    def test_current_wall_clock_still_sees_expired_admitted_card(self):
        now = time.time()
        self.assertGreaterEqual(now, POLICY_EXPIRES_AT)
        self.assertFalse(admission.admitted_card_is_fresh(now))
        # Observing matching prices does not refresh the admitted card.
        card = admission.rate_card_from_fact_pack(
            self.pack, now=now, requested_model=MODEL,
        )
        self.assertTrue(card.model_matches and card.rates_match and card.endpoint_matches)
        self.assertFalse(card.fresh)
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(True, source='existing-batch-authorization'),
                card=card,
            ),
            admission.DENY_FRESHNESS,
        )

    def test_client_default_deepseek_chat_cannot_enter_paid_policy(self):
        self.assertEqual(podcast_cn.get_provider_config('deepseek').default_model, 'deepseek-chat')
        self.assertEqual(admission.NON_POLICY_CLIENT_DEFAULT_MODEL, 'deepseek-chat')
        self.assertNotEqual(podcast_cn.get_provider_config('deepseek').default_model, MODEL)
        card = admission.rate_card_from_fact_pack(
            self.pack, now=POLICY_VERIFIED_AT + 1,
            requested_model=podcast_cn.get_provider_config('deepseek').default_model,
        )
        # Even inside the original 24h window, the client default is a mismatch.
        self.assertFalse(card.model_matches)
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(True, source='existing-batch-authorization'),
                card=card,
            ),
            admission.DENY_FRESHNESS,
        )

    def test_assistant_rate_window_is_not_user_authorization_expiry(self):
        # Missing scope is deny_authorization even when imagining a still-fresh card.
        # Expired card with an existing batch scope is deny_freshness, not a voided grant.
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(False, source='no-batch-scope'),
                card=admission.RateCard(fresh=True, model_matches=True),
            ),
            admission.DENY_AUTHORIZATION,
        )
        expired = admission.rate_card_from_fact_pack(
            self.pack, now=time.time(), requested_model=MODEL,
        )
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(True, source='existing-batch-authorization'),
                card=expired,
            ),
            admission.DENY_FRESHNESS,
        )

    def test_does_not_treat_fact_pack_retrieval_as_new_admission(self):
        # The pack itself says it must not reopen paid requests or extend expiry.
        note = self.pack['comparison_to_admitted_snapshot_2026-11-19']['note']
        self.assertIn('does not extend POLICY_EXPIRES_AT', note)
        self.assertIn('must not be used to reopen paid requests', note)
        # A successful observation at 19:00 plus scope is still not auto-spend.
        # Use a synthetic in-window clock only on a *copy* of RateCard fields,
        # never by assigning POLICY_EXPIRES_AT.
        imaginary_fresh = admission.RateCard(
            fresh=True, model_matches=True, endpoint_matches=True, rates_match=True,
        )
        self.assertEqual(
            admission.admit_new_spend(
                scope=admission.AuthorizationScope(True, source='existing-batch-authorization'),
                card=imaginary_fresh,
            ),
            admission.DENY_UNTIL_RUNTIME_ADMISSION,
        )


if __name__ == '__main__':
    unittest.main()
