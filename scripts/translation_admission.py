"""Offline evidence and durable, batch-scoped translation rate admission."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import time

from studio_recovery import _save, decode_json_object, fingerprint
from translation_budget import (
    CONTEXT_TOKENS,
    ENDPOINT,
    FEE_MODE,
    INPUT_MICRO_CNY,
    MODEL,
    MODEL_REVISION,
    OUTPUT_MICRO_CNY,
    POLICY,
    POLICY_EXPIRES_AT,
    POLICY_VERIFIED_AT,
    TranslationRequestError,
)

REUSE_BOUND_CACHE = 'reuse_bound_cache'
DENY_AUTHORIZATION = 'deny_authorization'
DENY_FRESHNESS = 'deny_freshness'
DENY_UNTIL_RUNTIME_ADMISSION = 'deny_until_runtime_admission'

# Client default that must not enter the paid translation policy.
NON_POLICY_CLIENT_DEFAULT_MODEL = 'deepseek-chat'

# Peak uncached rates already admitted in deepseek-flash-cny-2026-11-19-v1.
# Units: CNY per 1,000,000 tokens; same integers as INPUT_MICRO_CNY / OUTPUT_MICRO_CNY.
ADMITTED_PEAK_INPUT_CNY_PER_MILLION = INPUT_MICRO_CNY
ADMITTED_PEAK_OUTPUT_CNY_PER_MILLION = OUTPUT_MICRO_CNY


@dataclass(frozen=True)
class AuthorizationScope:
    """User-granted batch scope only. Public-page refresh cannot set this True."""

    allowed: bool
    source: str = ''


@dataclass(frozen=True)
class RateCard:
    """Freshness of the *admitted* policy card, plus identity/rate match."""

    fresh: bool
    model_matches: bool = True
    endpoint_matches: bool = True
    rates_match: bool = True


def load_fact_pack(path):
    return json.loads(Path(path).read_text())


def observed_matches_admitted(pack):
    """Return True when the frozen official observation still matches admitted peak rates."""
    observed = pack['observed_deepseek_flash']
    comparison = pack['comparison_to_admitted_snapshot_2026-11-19']
    runtime = pack['runtime_status']
    return (
        observed['api_name'] == MODEL
        and observed['model_revision'] == MODEL_REVISION
        and observed['endpoint'] == ENDPOINT
        and observed['input_cache_miss']['peak'] == ADMITTED_PEAK_INPUT_CNY_PER_MILLION
        and observed['output']['peak'] == ADMITTED_PEAK_OUTPUT_CNY_PER_MILLION
        and comparison['official_page_still_matches_that_pair'] is True
        and comparison['admitted_policy'] == POLICY
        and comparison['admitted_peak_uncached_input_and_output_cny_per_million']
        == [ADMITTED_PEAK_INPUT_CNY_PER_MILLION, ADMITTED_PEAK_OUTPUT_CNY_PER_MILLION]
        and runtime['POLICY_VERIFIED_AT'] == POLICY_VERIFIED_AT
        and runtime['POLICY_EXPIRES_AT'] == POLICY_EXPIRES_AT
        and pack.get('no_paid_request') is True
    )


def admitted_card_is_fresh(now):
    """Wall-clock freshness of the already-admitted card. Never rewrites expiry."""
    return POLICY_VERIFIED_AT <= now < POLICY_EXPIRES_AT


def rate_card_from_fact_pack(pack, *, now, requested_model=None):
    """Build a RateCard from a readable fact pack and the current admitted constants.

    Observing that official peak prices still match does not make an expired
    admitted card fresh. requested_model, when given, must be the paid policy
    model; the client default deepseek-chat is a mismatch.
    """
    matches = observed_matches_admitted(pack)
    model_matches = matches if requested_model is None else (
        requested_model == MODEL and matches
    )
    return RateCard(
        fresh=admitted_card_is_fresh(now) and matches,
        model_matches=model_matches,
        endpoint_matches=matches,
        rates_match=matches,
    )


def admit_new_spend(*, scope, card, cache_hit=False):
    """Legacy evidence comparison, independent of the transport admission gate."""
    if cache_hit:
        return REUSE_BOUND_CACHE
    if not scope.allowed:
        return DENY_AUTHORIZATION
    if not (
        card.fresh
        and card.model_matches
        and card.endpoint_matches
        and card.rates_match
    ):
        return DENY_FRESHNESS
    return DENY_UNTIL_RUNTIME_ADMISSION


_FIXTURE = Path(__file__).resolve().parents[1] / 'tests/fixtures/rate-runtime-2026-11-20-2211'
_OBSERVATION = 'rate-card-observation-2026-11-20-2211.json'
_OBSERVATION_SHA = 'b23ff05a44b5304e84ae3aee24539045faeade7fe0fdbf0b218e66a9f38e5597'
_BATCH = 'authorized-batch-t1294-jobs.json'
_BATCH_SHA = '7d629bc78a9b2d7ac84e23161a99abaf11f73042d4d91c56207a16ffe6e30b30'
_BASE_HEAD = '3783241f0c81c50e22c9c6d5a92bd839ea22fcf3'
_TTL = 86400
# t1294u names the old selected-batch cap and remaining cap in CNY.
_LIFTED_LEGACY_CAPS = (22_000_000, 870_000)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _source_hashes():
    directory = Path(__file__).resolve().parent
    return {name: _sha256((directory / name).read_bytes()) for name in (
        'translation_admission.py', 'translation_budget.py', 'podcast_cn.py',
    )}


_LOADED_SOURCE_HASHES = _source_hashes()


def _pinned_json(name, digest):
    raw = (_FIXTURE / name).read_bytes()
    if _sha256(raw) != digest:
        raise ValueError('Evidence digest mismatch')
    return decode_json_object(raw)


def _batch():
    return _pinned_json(_BATCH, _BATCH_SHA)


def _record(jobs_root, batch):
    observation = _pinned_json(_OBSERVATION, _OBSERVATION_SHA)
    granted = observation['authorized_batch']
    for name in ('source', 'source_recorded_at', 'source_sha256', 'source_bytes', 'job_ids'):
        if batch[name] != granted[name]:
            raise ValueError('Authorization source mismatch')
    for source in observation['sources']:
        raw = (_FIXTURE / source['file']).read_bytes()
        if len(raw) != source['bytes'] or _sha256(raw) != source['sha256']:
            raise ValueError('Official source digest mismatch')

    extracted = observation['extracted']
    contract = {
        'api_name': MODEL, 'model_revision': MODEL_REVISION, 'endpoint': ENDPOINT,
        'fee_mode': FEE_MODE, 'context_tokens': CONTEXT_TOKENS,
        'thinking': 'disabled', 'thinking_admitted': False,
        'peak_uncached_input_cny_per_million': INPUT_MICRO_CNY,
        'peak_output_cny_per_million': OUTPUT_MICRO_CNY,
    }
    observed_at = observation['observed_at_unix']
    if not (
        all(extracted[name] == contract[name] for name in ('api_name', 'model_revision', 'endpoint', 'fee_mode'))
        and extracted['input_cache_miss']['peak'] == INPUT_MICRO_CNY
        and extracted['output']['peak'] == OUTPUT_MICRO_CNY
        and extracted['thinking'] == 'disabled in current request contract; thinking is a distinct official fee mode and is not admitted here'
        and observation['ttl_seconds'] == _TTL
        and observation['fact_expires_at_unix'] == observed_at + _TTL
        and _source_hashes() == _LOADED_SOURCE_HASHES
    ):
        raise ValueError('Runtime contract mismatch')
    return {
        'schema_version': 1,
        'jobs_root': str(jobs_root),
        'source': {'base_head': _BASE_HEAD, 'sha256': _LOADED_SOURCE_HASHES},
        'observation_sha256': _OBSERVATION_SHA,
        'observed_at_unix': observed_at,
        'ttl_seconds': _TTL,
        'fact_expires_at_unix': observed_at + _TTL,
        'official_sources': observation['sources'],
        'contract': contract,
        'authorization': {
            **granted, 'manifest_sha256': _BATCH_SHA,
            'lifted_legacy_caps_microunits': list(_LIFTED_LEGACY_CAPS),
        },
    }


def _read_record(path):
    # The accepted record is a small regular file in the local jobs root.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Invalid admission file')
        raw = stream.read(32_769)
    if len(raw) > 32_768:
        raise ValueError('Oversized admission file')
    return raw


@dataclass(frozen=True)
class RuntimeAdmission:
    """A handle to one exact persisted record, rechecked at each spend gate."""

    FILENAME = 'translation-runtime-admission.json'
    jobs_root: Path
    record_sha256: str

    @classmethod
    def load(cls, jobs_root):
        jobs_root = Path(jobs_root).resolve()
        try:
            return cls(jobs_root, _sha256(_read_record(jobs_root / cls.FILENAME)))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # Keep cache identity available while failing closed at admission.
            return cls(jobs_root, '')

    @classmethod
    def for_job(cls, job):
        """Select the explicit batch mapping only for an already-selected job."""
        handle = cls.load(job.parent)
        if handle is not None:
            try:
                if job.name in _batch()['job_ids']:
                    return handle
            except (OSError, ValueError):
                pass
        return None

    def check(self, job, identity, clock, evidence):
        try:
            batch = _batch()
        except (OSError, ValueError):
            raise TranslationRequestError('translation_admission_scope', evidence) from None
        if self.jobs_root != job.parent or job.name not in batch['job_ids']:
            raise TranslationRequestError('translation_admission_scope', evidence)
        try:
            record = _record(self.jobs_root, batch)
            raw = _read_record(self.jobs_root / self.FILENAME)
            actual = decode_json_object(raw)
            if _sha256(raw) != self.record_sha256:
                raise ValueError('Persisted admission changed')
            if fingerprint(actual.get('authorization')) != fingerprint(record['authorization']):
                raise TranslationRequestError('translation_admission_scope', evidence)
            if fingerprint(actual) != fingerprint(record):
                raise ValueError('Persisted admission mismatch')
            if identity != ('deepseek', MODEL, ENDPOINT):
                raise ValueError('Request contract mismatch')
        except (OSError, ValueError, RecursionError):
            raise TranslationRequestError('translation_admission_invalid', evidence) from None
        if not record['observed_at_unix'] <= clock() < record['fact_expires_at_unix']:
            raise TranslationRequestError('translation_admission_stale', evidence)
        return {
            'schema_version': record['schema_version'],
            'record_sha256': self.record_sha256,
            'job_id': job.name,
            'source_sha256': batch['source_sha256'],
            'authorization_refs': [entry['ref'] for entry in record['authorization']['authorization_refs']],
            'lifted_legacy_caps_microunits': record['authorization']['lifted_legacy_caps_microunits'],
            'explanation': record['authorization']['scope_note'],
            'remaining_work': record['authorization']['authorization_refs'][1]['note'],
            'observed_at_unix': record['observed_at_unix'],
            'fact_expires_at_unix': record['fact_expires_at_unix'],
        }


def admit_rate_card(jobs_root):
    """Explicit local operator action for the reviewed 22:11 observation."""
    jobs_root = Path(jobs_root).resolve(strict=True)
    record = _record(jobs_root, _batch())
    if not record['observed_at_unix'] <= time.time() < record['fact_expires_at_unix']:
        raise ValueError('Rate observation is outside its technical window')
    _save(jobs_root / RuntimeAdmission.FILENAME, record)
    return RuntimeAdmission.load(jobs_root)
