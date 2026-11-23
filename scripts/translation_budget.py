"""Durable admission and safe receipts for Studio's paid translation transport."""

from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import urllib.error
import uuid

from studio_recovery import (
    LOCAL_SOCKET_DENIED, StudioRecoveryError, TranslationJournal,
    _read_regular_bytes, _save, _sync_dir, decode_json_object, fingerprint, podcast_job_lock,
    require_translation_network, translation_transport_failure,
)


ENDPOINT = 'https://api.deepseek.com'
MODEL = 'deepseek-flash'
MODEL_REVISION = 'DeepSeek-V4.1-Flash-1017'
POLICY = 'deepseek-flash-cny-2026-11-19-v1'
FEE_MODE = 'cny_token_only'
# Official 2026-11-19 pricing and context snapshot. No smaller tokenizer or
# server framing bound is assumed. Charge every input token at peak cache miss.
CONTEXT_TOKENS = 1_048_576
INPUT_MICRO_CNY = 2
OUTPUT_MICRO_CNY = 8
OUTPUT_TOKENS = {'translation_chunk': 4096, 'translation_title': 512}
POLICY_VERIFIED_AT = 1795049340  # 2026-11-19 00:49 UTC
POLICY_EXPIRES_AT = POLICY_VERIFIED_AT + 86400
# Explicit t631 acceptance of this exact public observation. These absolute
# dates never move when another page is fetched or an allowance is created.
EPISODE_VERIFIED_AT = 1795403956
EPISODE_EXPIRES_AT = 1795490356
_EPISODE_OBSERVATION = (Path(__file__).resolve().parents[1] / 'tests/fixtures'
                        / 'rate-card-observation-2026-11-23-1119.json')
_EPISODE_OBSERVATION_SHA = '8f004e07584930b23e48ba46ba8a12d9d5fd6f1aeadf0ccd95291ff6a4bf7e49'
_EPISODE_ROOT = Path.home() / 'Desktop/音频/中文播客工作台/isolated-t629-jobs'
_EPISODE_CAPS = {
    'd1e5daab08174fd2be2b3868d14c7cd2': 620_000,
    'de91a4b274894fa29edebb2cb90c654d': 800_000,
    '2a8a996b3c5a433f8a06619ad45c56eb': 1_030_000,
    '1efe42d270884ae8b8df07f9c2e9de8a': 840_000,
}
# Conservative reserved history, neither an invoice nor the book's prepaid cap.
CONSERVED_PRIOR_MICROUNITS = 21_130_000
RESPONSE_LIMIT = 512 * 1024
ERROR_BODY_LIMIT = 4096

_MESSAGES = {
    'translation_policy_unverified': '翻译模型、端点或资费上限未经核实，未发出请求',
    'translation_allowance_missing': '此任务缺少明确的翻译额度，未发出请求',
    'translation_allowance_invalid': '此任务的翻译额度已过期或无法核实，未发出请求',
    'translation_admission_scope': '此任务未纳入已授权的翻译批次，未发出请求',
    'translation_admission_invalid': '无法核实翻译费率承认记录或请求合同，未发出请求',
    'translation_admission_stale': '官方翻译费率观测不在技术有效窗内，未发出请求',
    'translation_budget_exhausted': '此任务额度不足以覆盖本次请求的最高费用，未发出请求',
    'translation_ledger_unavailable': '无法确认翻译账本状态，请先核对记录',
    'translation_request_unresolved': '此任务仍有待核对的翻译请求，已保留额度',
    'translation_request_already_recorded': '此翻译请求已有记录，请先核对记录与缓存',
    'translation_recovery_invalid': '未能核实翻译请求在发送前失败，原请求记录已保留',
    'translation_continuity_invalid': '无法核实既成释放与原始记录的关系，或现有证据仍支持原恢复',
    'translation_credential_missing': '此任务未配置翻译凭据，未发出请求',
    'translation_transport_unknown': '翻译请求结果未知，已保留额度，请先核对记录',
    'translation_http_error': '翻译服务返回 HTTP 错误，已保留额度，请先核对记录',
    'translation_response_unknown': '翻译服务返回无法核验，已保留额度，请先核对记录',
    'translation_model_unknown': '翻译返回的模型无法核实，已保留额度，请先核对记录',
    'translation_usage_unknown': '翻译用量无法核实，已保留额度，请先核对记录',
    'translation_finish_unknown': '翻译未确认正常完成，已保留额度，请先核对记录',
    'translation_invalid_structure': '翻译返回的结构或段落顺序不符合要求',
}

_PROVIDER_CODES = {
    'invalid_request_error', 'authentication_error', 'invalid_api_key',
    'model_not_found', 'insufficient_balance', 'rate_limit_exceeded',
    'server_error', 'overloaded_error', 'service_unavailable',
}
_PROVIDER_ID = re.compile(
    r'(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|req[-_][0-9a-f]{16,48})',
    re.IGNORECASE | re.ASCII,
)
_HISTORY_ID = re.compile(r'[0-9a-f]{64}')
_HISTORY_RECEIPT = re.compile(r'[0-9a-f]{64}\.json')
_HELD_STATES = ('submitted', 'uncertain', 'reconciliation_required', 'reconciled_no_remote_completion')


class TranslationRequestError(StudioRecoveryError):
    """Only bounded, allowlisted evidence may cross a job or UI boundary."""

    def __init__(self, code, evidence):
        super().__init__(code, _MESSAGES[code], {**evidence, 'code': code})


def supports_policy(provider, model, base_url):
    return (provider, model, base_url) == ('deepseek', MODEL, ENDPOINT)


@contextmanager
def _lock_jobs_root(root):
    # Serialize across existing per-job wallets without opening another wallet.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _mode(path):
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return 0


def _read_history_file(path):
    raw = _read_regular_bytes(path)
    return decode_json_object(raw), hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class _HistoricalRequest:
    key: str
    local_id: str
    state: str
    reserved: int | None
    unresolved: bool
    receipt: str | None = None
    resumes: str | None = None


def _external_history(job):
    """Read the book ledger and Studio receipts without changing either source."""
    files, requests = {}, []
    ledger = job / 'request-ledger.json'
    if _mode(ledger):
        value, digest = _read_history_file(ledger)
        rows = value.get('requests')
        if not (type(value.get('schema_version')) is int and value['schema_version'] == 1
                and isinstance(rows, dict)):
            raise ValueError('Invalid book ledger')
        book_requests = []
        for key, row in rows.items():
            if not isinstance(row, dict) or not _HISTORY_ID.fullmatch(key):
                raise ValueError('Invalid book request')
            state, amount = row.get('state'), row.get('reserved_cost_microunits')
            if (state not in (*_HELD_STATES, 'succeeded', 'abandoned_unusable_output')
                    or type(amount) is not int or not 0 <= amount < 2**63):
                raise ValueError('Invalid book reservation')
            book_requests.append(_HistoricalRequest(key, key, state, amount, state in _HELD_STATES))
        files[ledger.name] = [digest, [
            {field: value for field, value in vars(row).items() if field != 'resumes'}
            for row in book_requests
        ]]
        requests.extend(book_requests)

    journal = job / 'translation-requests'
    mode = _mode(journal)
    if mode and not stat.S_ISDIR(mode):
        raise ValueError('Invalid journal directory')
    if mode:
        for path in journal.iterdir():
            value, digest = _read_history_file(path)
            ids = value.get('segment_ids')
            local_id, state = value.get('local_request_id'), value.get('request_state')
            billing, completion = value.get('billing_state'), value.get('completion_state')
            key = value.get('request_key', path.stem)
            resumes = value.get('resumes_receipt')
            if (
                not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids)
                or (resumes is not None
                    and (not isinstance(resumes, str) or not _HISTORY_RECEIPT.fullmatch(resumes)))
                or path.name != TranslationJournal.receipt_name(ids, resumes)
                or not isinstance(local_id, str) or not _HISTORY_ID.fullmatch(local_id)
                or not isinstance(key, str) or not _HISTORY_ID.fullmatch(key)
                or state not in ('submitted', 'uncertain', 'response_received', 'not_sent')
                or billing not in ('unknown', 'no_charge', 'usage_verified')
                or completion not in ('unknown', 'not_started', 'provider_reported_stop')
            ):
                raise ValueError('Invalid journal receipt')
            unresolved = state in _HELD_STATES or 'unknown' in (billing, completion)
            # The journal has no amount. A paired SQLite reservation counts once.
            row = _HistoricalRequest(key, local_id, state, None, unresolved, path.name, resumes)
            # Keep the on-disk adoption snapshot format compatible.
            snapshot_row = {field: field_value for field, field_value in vars(row).items()
                            if field != 'resumes' or resumes is not None}
            files[f'{journal.name}/{path.name}'] = [digest, [snapshot_row]]
            requests.append(row)
    return files, requests


_NO_REMOTE_FIELDS = ('http_status', 'provider_request_id', 'provider_response_id',
                     'usage', 'provider_error_code', 'finish_reason', 'response_model')
_UNSENT = {'request_state': 'not_sent', 'billing_state': 'no_charge', 'completion_state': 'not_started'}
_UNKNOWN = {'request_state': 'uncertain', 'billing_state': 'unknown', 'completion_state': 'unknown'}
_DISPATCH_DENIALS = ('translation_allowance_invalid', 'translation_admission_stale',
                     'translation_admission_invalid', 'translation_admission_scope')
_PRE_SEND_PROOFS = ('recorded_before_open', 'recorded_transport')


def _has_remote_evidence(record):
    return (any(record.get(field) is not None for field in _NO_REMOTE_FIELDS)
            or record.get('error_body_summary') not in (None, ''))


def _supports_recorded_recovery(findings, wallet, original):
    """Check the evidence retained by a completed pre-send recovery."""
    if _has_remote_evidence(original):
        return False
    code = wallet.get('code')
    if (code in _DISPATCH_DENIALS
            and findings == {'kind': 'recorded_before_open', 'code': code}):
        return all(original.get(field) == value for field, value in _UNSENT.items())
    return (
        findings == {'kind': 'recorded_transport', 'transport_failure': LOCAL_SOCKET_DENIED}
        and code == 'translation_transport_unknown'
        and all(original.get(field) == value for field, value in _UNKNOWN.items())
        and all(record.get('transport_failure') == LOCAL_SOCKET_DENIED for record in (wallet, original))
    )


class TranslationBudget:
    """An explicitly provisioned, path-bound allowance and its request history.

    The Studio and CLI only open an existing ledger. A trusted local operator
    supplies the actual allowance and prior usage separately from job inputs.
    """

    FILENAME = 'translation-budget.sqlite3'

    def __init__(self, job, *, clock=None):
        self._given_job = Path(job)
        self.job = self._given_job.resolve()
        self.path = self.job / self.FILENAME
        self._clock = clock if clock is not None else lambda: time.time()

    @property
    def episode_cap(self):
        if self.job.parent == _EPISODE_ROOT and not self._given_job.is_symlink():
            return _EPISODE_CAPS.get(self.job.name)
        return None

    def _policy_window(self):
        if self.episode_cap is None:
            return POLICY_VERIFIED_AT, POLICY_EXPIRES_AT
        raw = _read_regular_bytes(_EPISODE_OBSERVATION)
        if hashlib.sha256(raw).hexdigest() != _EPISODE_OBSERVATION_SHA:
            raise ValueError('Unrecognized episode rate observation')
        pack = decode_json_object(raw)
        card = pack['extracted_deepseek_flash']
        if (pack['retrieved_at_unix'] != EPISODE_VERIFIED_AT
                or pack['comparison_to_admitted_snapshot_2026-11-19']['admitted_policy'] != POLICY
                or (card['api_name'], card['model_revision'], card['endpoint']) != (MODEL, MODEL_REVISION, ENDPOINT)
                or (card['input_cache_miss']['peak'], card['output']['peak']) != (INPUT_MICRO_CNY, OUTPUT_MICRO_CNY)):
            raise ValueError('Episode rate contract mismatch')
        return EPISODE_VERIFIED_AT, EPISODE_EXPIRES_AT

    def _current(self, verified_at, expires_at):
        try:
            start, end = self._policy_window()
        except (OSError, ValueError):
            return False
        return start <= verified_at <= self._clock() < expires_at <= end

    @property
    def _ledger_job(self):
        return str(self.job)

    @classmethod
    def provision(cls, job, *, authorization_id, fee_mode, cap_microunits,
                  prior_microunits, verified_at, expires_at, clock=None):
        """Create a standalone legacy allowance. Existing history requires adoption."""
        budget = cls(job, clock=clock)
        with _lock_jobs_root(budget.job.parent):
            if any(_mode(budget.job / name) for name in ('request-ledger.json', 'translation-requests')):
                raise ValueError('Use explicit adoption for an existing history')
            budget._provision(authorization_id, fee_mode, cap_microunits, prior_microunits, verified_at, expires_at)

    @classmethod
    def adopt(cls, job, *, isolation_root, authorization_id, fee_mode, cap_microunits,
              prior_microunits, verified_at, expires_at, clock=None):
        """Bind one isolated job to its own history and the batch's conserved prior."""
        from translation_admission import _LIFTED_LEGACY_CAPS

        budget = cls(job, clock=clock)
        root = budget._isolated_root(isolation_root)
        if (
            type(prior_microunits) is not int or prior_microunits < CONSERVED_PRIOR_MICROUNITS
            or cap_microunits not in _LIFTED_LEGACY_CAPS
        ):
            raise ValueError('Adoption requires conserved prior and a reviewed legacy cap')
        with _lock_jobs_root(root):
            snapshot, _ = _external_history(budget.job)
            budget._provision(authorization_id, fee_mode, cap_microunits, prior_microunits,
                              verified_at, expires_at, snapshot)
        return budget

    def _isolated_root(self, isolation_root):
        root = Path(isolation_root).resolve(strict=True)
        desktop = Path.home() / 'Desktop'
        protected = (
            desktop / '音频' / '中文播客工作台' / 'jobs',
            desktop / 'shengjian-book-jobs-isolated' / 'food-is-medicine-mozaffarian-2026-11-13',
        )
        if (
            self.job.parent != root or Path(isolation_root).is_symlink()
            or self._given_job.is_symlink() or not self.job.is_dir()
            or any(self.job.is_relative_to(path.resolve()) for path in protected)
        ):
            raise ValueError('An isolated job outside the protected roots is required')
        return root

    def _provision(self, authorization_id, fee_mode, cap, prior, verified_at, expires_at, snapshot=None):
        start, end = self._policy_window()
        if not (
            isinstance(authorization_id, str) and _PROVIDER_ID.fullmatch(authorization_id)
            and fee_mode == FEE_MODE
            and all(type(value) is int for value in (cap, prior, verified_at, expires_at))
            and 0 <= prior < 2**63 and 0 < cap < 2**63
            and (snapshot is not None or prior <= cap)
            and (self.episode_cap is None or (prior == 0 and cap <= self.episode_cap and snapshot is None))
            and start <= verified_at < expires_at <= end
            and (snapshot is not None or self._current(verified_at, expires_at))
            and not self._given_job.is_symlink()
        ):
            raise ValueError('Invalid translation allowance')
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with closing(self._connect()) as db, db:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE allowance (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    job TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    fee_mode TEXT NOT NULL,
                    cap_microunits INTEGER NOT NULL,
                    prior_microunits INTEGER NOT NULL,
                    verified_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE requests (
                    request_key TEXT NOT NULL UNIQUE,
                    local_id TEXT NOT NULL UNIQUE,
                    reserved_microunits INTEGER NOT NULL CHECK (reserved_microunits > 0),
                    state TEXT NOT NULL CHECK (state IN ('submitted', 'uncertain', 'completed')),
                    evidence TEXT NOT NULL,
                    started_at INTEGER NOT NULL
                );
            """)
            db.execute('INSERT INTO allowance VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)', (
                self._ledger_job, POLICY, authorization_id, fee_mode, cap, prior, verified_at, expires_at,
            ))
            if snapshot is not None:
                db.execute('CREATE TABLE adopted_history (snapshot TEXT NOT NULL)')
                db.execute('INSERT INTO adopted_history VALUES (?)', (json.dumps(snapshot),))

    def _connect(self, *, readonly=False):
        if self._given_job.is_symlink() or not stat.S_ISREG(_mode(self.path)):
            raise sqlite3.OperationalError('Invalid ledger path')
        for suffix in ('-journal', '-wal', '-shm'):
            mode = _mode(self.path.with_name(self.path.name + suffix))
            if mode and (suffix != '-journal' or not stat.S_ISREG(mode)):
                raise sqlite3.OperationalError('Invalid ledger sidecar')
        mode = 'ro' if readonly else 'rw'
        db = sqlite3.connect(f'{self.path.as_uri()}?mode={mode}', uri=True, timeout=10)
        try:
            if db.execute('PRAGMA journal_mode').fetchone() != ('delete',):
                raise sqlite3.OperationalError('Unsupported ledger journal mode')
            if not readonly:
                db.execute('PRAGMA synchronous=EXTRA')
                db.execute('PRAGMA fullfsync=ON')
            return db
        except BaseException:
            db.close()
            raise

    def _check_dispatch(self, admission, identity, expires_at, evidence):
        if admission is None:
            start = EPISODE_VERIFIED_AT if self.episode_cap is not None else POLICY_VERIFIED_AT
            if not self._current(start, expires_at):
                raise TranslationRequestError('translation_allowance_invalid', evidence)
            return
        from translation_admission import RuntimeAdmission

        if type(admission) is not RuntimeAdmission:
            raise TranslationRequestError('translation_admission_invalid', evidence)
        return admission.check(self.job, identity, self._clock, evidence)

    def _audit_path(self, slot):
        directory = self.job / 'translation-recoveries'
        mode = _mode(directory)
        if mode and not stat.S_ISDIR(mode):
            raise ValueError('Invalid recovery directory')
        return directory / slot

    def _history(self, db=None, *, recovery_index=None, continuity_index=None):
        snapshot = None
        requests, recoveries, resumed = [], set(), []
        files, external = _external_history(self.job)
        journal = {row.receipt: row for row in external if row.receipt is not None}
        if db is not None:
            continuity = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'recovery_continuity'").fetchone():
                continuity = dict(db.execute('SELECT request_key, record FROM recovery_continuity'))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'adopted_history'").fetchone():
                (raw,), = db.execute('SELECT snapshot FROM adopted_history').fetchall()
                snapshot = decode_json_object(raw)
            for key, local_id, amount, state, raw in db.execute(
                'SELECT request_key, local_id, reserved_microunits, state, evidence FROM requests'
            ):
                if (
                    not isinstance(key, str) or not _HISTORY_ID.fullmatch(key)
                    or not isinstance(local_id, str)
                    or not (_HISTORY_ID.fullmatch(local_id) or _PROVIDER_ID.fullmatch(local_id))
                    or type(amount) is not int or amount <= 0
                    or state not in ('submitted', 'uncertain', 'completed')
                    or not isinstance(raw, str)
                ):
                    raise ValueError('Invalid wallet request')
                receipt = decode_json_object(raw)
                unresolved = (
                    state in _HELD_STATES or 'unknown' in (receipt.get('billing_state'), receipt.get('completion_state'))
                    or any(receipt.get(field) for field in ('billing_uncertain', 'completion_uncertain'))
                )
                slot = receipt.get('receipt')
                if slot is not None and (not isinstance(slot, str) or not _HISTORY_RECEIPT.fullmatch(slot)):
                    raise ValueError('Invalid wallet receipt identity')
                resumes = receipt.get('resumes_receipt')
                if resumes is not None:
                    resumed.append((slot, key, local_id, resumes))
                marker = receipt.get('recovery_audit_sha256')
                if marker is not None:
                    original = journal.get(slot)
                    if (state != 'completed' or unresolved or _has_remote_evidence(receipt)
                            or any(receipt.get(field) != value for field, value in _UNSENT.items())
                            or receipt.get('request_key') != key or receipt.get('local_request_id') != local_id
                            or original is None or (original.key, original.local_id) != (key, local_id)):
                        raise ValueError('Invalid recovered wallet request')
                    audit, digest = _read_history_file(self._audit_path(slot))
                    original_record, original_digest = _read_history_file(self.job / 'translation-requests' / slot)
                    if (digest != marker or type(audit.get('schema_version')) is not int
                            or audit['schema_version'] != 1
                            or audit.get('request_key') != key or audit.get('local_id') != local_id
                            or audit.get('receipt') != slot or audit.get('job') != self._ledger_job
                            or type(audit.get('released_microunits')) is not int
                            or audit.get('released_microunits') != amount
                            or original.state not in ('uncertain', 'not_sent')
                            or audit.get('original_request_state') != original.state
                            or audit.get('disposition') != _UNSENT
                            or audit.get('receipt_sha256') != original_digest
                            or original_digest != files[f'translation-requests/{slot}'][0]
                            or original_record.get('segment_ids') != receipt.get('segment_ids')):
                        raise ValueError('Recovery audit or original receipt changed')
                    recoveries.add(slot)
                    unresolved = not _supports_recorded_recovery(audit.get('findings'), receipt, original_record)
                    if unresolved:
                        # Project the original amount even before the explicit audit is recorded.
                        # The old receipt and the appended audit do not add separate amounts.
                        record = {
                            'schema_version': 1, 'job': self._ledger_job, 'request_key': key,
                            'local_id': local_id, 'receipt': slot, 'receipt_sha256': original_digest,
                            'recovery_audit_sha256': digest,
                            'released_wallet_evidence_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                            'reason': 'insufficient_evidence_for_executed_recovery',
                            'retained_microunits': amount,
                            'billing_state': 'unknown', 'completion_state': 'unknown',
                        }
                        recorded = key in continuity
                        if recorded and continuity.pop(key) != json.dumps(record, sort_keys=True):
                            raise ValueError('Continuity record or its source changed')
                        if continuity_index is not None:
                            continuity_index[key] = (record, recorded)
                        state = 'uncertain'
                    else:
                        if recovery_index is not None:
                            recovery_index[slot] = local_id
                        state, amount = 'not_sent', 0
                requests.append(_HistoricalRequest(key, local_id, state, amount, unresolved, slot, resumes))
            if continuity:
                raise ValueError('Unlinked recovery continuity record')
        if snapshot is not None and any(files.get(path) != record for path, record in snapshot.items()):
            raise ValueError('Adopted history was changed or removed')
        # Every recovered successor is still subject to the ordinary history
        # checks. A reserved successor might exist before its receipt is written.
        for slot, key, local_id, predecessor in resumed:
            if (not isinstance(predecessor, str) or predecessor not in recoveries or slot is None
                    or (slot in journal and (journal[slot].key, journal[slot].local_id, journal[slot].resumes)
                        != (key, local_id, predecessor))):
                raise ValueError('Invalid resumed wallet request')
        wallet_attempts = set(resumed)
        for row in external:
            if row.resumes is not None and (row.receipt, row.key, row.local_id, row.resumes) not in wallet_attempts:
                raise ValueError('Resumed receipt is missing its reservation')
            if row.receipt not in recoveries:
                requests.append(row)
        return snapshot is not None, requests

    def _can_resume(self, slot, local_id):
        # A file left by a crash before the SQLite commit grants no permission.
        try:
            with closing(self._connect(readonly=True)) as db, db:
                db.execute('BEGIN')
                recovered = {}
                self._history(db, recovery_index=recovered)
                return recovered.get(slot) == local_id
        except (OSError, sqlite3.Error, ValueError, RecursionError):
            return False

    def _recovery_candidate(self, db, evidence):
        if type(evidence['schema_version']) is not int or evidence['schema_version'] != 1:
            raise ValueError('Wrong evidence schema')
        if evidence['job'] != self._ledger_job:
            raise ValueError('Wrong evidence job')
        key, slot = evidence['request_key'], evidence['receipt']
        if not (_HISTORY_ID.fullmatch(key) and _HISTORY_RECEIPT.fullmatch(slot)):
            raise ValueError('Invalid recovery identity')
        proof = evidence['proof']
        if proof['kind'] not in _PRE_SEND_PROOFS:
            raise ValueError('Unsupported form of pre-send evidence')
        (job, prior), = db.execute('SELECT job, prior_microunits FROM allowance WHERE singleton = 1')
        if job != self._ledger_job:
            raise ValueError('Wrong allowance job')
        recovery_index = {}
        self._history(db, recovery_index=recovery_index)
        (local_id, reserved, state, raw), = db.execute(
            'SELECT local_id, reserved_microunits, state, evidence '
            'FROM requests WHERE request_key = ?', (key,),
        )
        old = decode_json_object(raw)
        receipt, digest = _read_history_file(self.job / 'translation-requests' / slot)
        was_denied = proof['kind'] == 'recorded_before_open'
        expected = _UNSENT if was_denied else _UNKNOWN
        if not (digest == evidence['receipt_sha256']
                and receipt.get('request_key') == key
                and receipt.get('local_request_id') == old.get('local_request_id') == local_id
                and old.get('receipt', slot if was_denied else None) == slot
                and old.get('request_key', key if was_denied else None) == key
                and receipt['segment_ids'] == old.get('segment_ids')
                and all(receipt.get(field) == value for field, value in expected.items())
                and not any(_has_remote_evidence(record) for record in (old, receipt))):
            raise ValueError('Wallet or journal does not match a pre-send candidate')
        if state == 'completed' and 'recovery_audit_sha256' in old:
            audit, _ = _read_history_file(self._audit_path(slot))  # Already validated by _history.
            if recovery_index.get(slot) != local_id or audit['proof_sha256'] != fingerprint(proof):
                raise ValueError('Recovery already recorded with different evidence')
            return audit, None
        if was_denied:
            if (state != 'completed' or old.get('code') not in _DISPATCH_DENIALS
                    or any(old.get(field, value) != value for field, value in _UNSENT.items())
                    or any(old.get(field) is not False for field in ('billing_uncertain', 'completion_uncertain'))):
                raise ValueError('No recorded before-open denial')
            findings = {'kind': 'recorded_before_open', 'code': old['code']}
        elif (state != 'uncertain' or old.get('code') != 'translation_transport_unknown'
              or any(old.get(field) != value for field, value in _UNKNOWN.items())):
            raise ValueError('Request is not an uncertain transport failure')
        else:
            if any(record.get('transport_failure') != LOCAL_SOCKET_DENIED for record in (old, receipt)):
                raise ValueError('No recorded local socket refusal')
            findings = {'kind': 'recorded_transport', 'transport_failure': LOCAL_SOCKET_DENIED}
        return {
            'schema_version': 1, 'job': self._ledger_job, 'request_key': key, 'local_id': local_id,
            'receipt': slot, 'receipt_sha256': digest, 'prior_microunits': prior,
            'released_microunits': reserved, 'disposition': _UNSENT,
            'original_request_state': receipt['request_state'],
            'original_wallet_evidence_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'proof_sha256': fingerprint(proof), 'findings': findings,
        }, old

    def inspect_pre_send_recovery(self, *, isolation_root, evidence):
        """Apply the recovery predicate without creating a lock, audit, or ledger write."""
        try:
            self._isolated_root(isolation_root)
            with closing(self._connect(readonly=True)) as db, db:
                db.execute('BEGIN')
                return self._recovery_candidate(db, evidence)[0]
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RecursionError, OverflowError):
            raise TranslationRequestError('translation_recovery_invalid', {}) from None

    def recovery_continuity(self, *, isolation_root, request_key, audit_sha256, apply=False):
        """Inspect or append a local hold for a linked, insufficient historical release."""
        try:
            root = self._isolated_root(isolation_root)
            if not all(isinstance(value, str) and _HISTORY_ID.fullmatch(value)
                       for value in (request_key, audit_sha256)):
                raise ValueError('Invalid continuity identity')
            with (podcast_job_lock(self.job) if apply else nullcontext()), \
                    (_lock_jobs_root(root) if apply else nullcontext()), \
                    closing(self._connect(readonly=not apply)) as db, db:
                db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
                return self._continuity(db, request_key, audit_sha256, apply)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RecursionError, OverflowError):
            raise TranslationRequestError('translation_continuity_invalid', {}) from None

    def _continuity(self, db, request_key, audit_sha256, apply):
        (job, policy, fee_mode, cap, prior), = db.execute(
            'SELECT job, policy, fee_mode, cap_microunits, prior_microunits FROM allowance WHERE singleton = 1'
        )
        if ((job, policy, fee_mode) != (self._ledger_job, POLICY, FEE_MODE)
                or type(cap) is not int or cap <= 0 or type(prior) is not int or prior < 0):
            raise ValueError('Invalid continuity allowance')
        index = {}
        _, history = self._history(db, continuity_index=index)
        record, recorded = index[request_key]
        if record['recovery_audit_sha256'] != audit_sha256:
            raise ValueError('Wrong recovery audit digest')
        status = 'already_recorded' if recorded else 'pending'
        if apply and not recorded:
            db.execute(
                'CREATE TABLE IF NOT EXISTS recovery_continuity ('
                'request_key TEXT NOT NULL PRIMARY KEY, record TEXT NOT NULL)'
            )
            db.execute('INSERT INTO recovery_continuity VALUES (?, ?)',
                       (request_key, json.dumps(record, sort_keys=True)))
            status = 'recorded'
        total = sum(row.reserved for row in history if row.reserved is not None)
        return {
            'status': status, 'record': record,
            'cap_microunits': cap, 'prior_microunits': prior, 'history_microunits': total,
            'accounted_microunits': prior + total, 'excess_over_cap_microunits': max(0, prior + total - cap),
        }

    def recover_pre_send(self, *, isolation_root, evidence):
        """Append a pre-send audit and release only the matching reservation.

        Evidence v1 is a structured lookup package: absolute
        job, request_key, receipt, receipt_sha256 and proof. The proof kind
        recorded_transport checks the wallet and journal's typed diagnostic.
        recorded_before_open checks an already recorded dispatch denial.
        Pass the same package to inspect_pre_send_recovery for a read-only check.
        """
        try:
            root = self._isolated_root(isolation_root)
            with podcast_job_lock(self.job), _lock_jobs_root(root), closing(self._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                audit, old = self._recovery_candidate(db, evidence)
                if old is None:
                    return audit
                path = self._audit_path(audit['receipt'])
                path.parent.mkdir(mode=0o700, exist_ok=True)
                _sync_dir(self.job)
                try:
                    _save(path, audit, exclusive=True)
                except FileExistsError:
                    pass  # Recover an interrupted write of this exact audit.
                saved, digest = _read_history_file(path)
                if saved != audit:
                    raise ValueError('Conflicting recovery audit')
                _sync_dir(path.parent)
                expected_state = 'uncertain' if audit['original_request_state'] == 'uncertain' else 'completed'
                result = db.execute(
                    "UPDATE requests SET state = 'completed', evidence = ? "
                    "WHERE request_key = ? AND local_id = ? AND state = ?",
                    (json.dumps({**old, **_UNSENT, 'billing_uncertain': False, 'completion_uncertain': False,
                                 'request_key': audit['request_key'], 'receipt': audit['receipt'],
                                 'recovery_audit_sha256': digest}),
                     audit['request_key'], audit['local_id'], expected_state),
                )
                if result.rowcount != 1:
                    raise ValueError('Uncertain reservation changed')
                return audit
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RecursionError, OverflowError):
            raise TranslationRequestError('translation_recovery_invalid', {}) from None

    @staticmethod
    def _check_history(requests, key, evidence, *, slot=None, journal_id=None):
        for row in requests:
            if row.unresolved:
                raise TranslationRequestError('translation_request_unresolved', {
                    **evidence, 'blocking_request_id': row.local_id, 'blocking_state': row.state,
                })
        for row in requests:
            if row.key == key or (row.receipt is not None and (row.receipt == slot or row.local_id == journal_id)):
                raise TranslationRequestError('translation_request_already_recorded', {
                    **evidence, 'blocking_request_id': row.local_id, 'blocking_state': row.state,
                })

    def _check_other_jobs(self, key, evidence, journal_request):
        for sibling in self.job.parent.iterdir():
            if sibling == self.job:
                continue
            mode = sibling.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError('Invalid sibling job path')
            if not stat.S_ISDIR(mode):
                continue
            budget = TranslationBudget(sibling)
            with (closing(budget._connect(readonly=True)) if _mode(budget.path) else nullcontext()) as db:
                _, requests = budget._history(db)
            journal_id = TranslationJournal(sibling).request_id(*journal_request) if journal_request else None
            self._check_history(requests, key, {**evidence, 'blocking_job_id': sibling.name},
                                journal_id=journal_id)

    def _allowance(self, db, event, evidence):
        allowance = db.execute(
            'SELECT job, policy, fee_mode, cap_microunits, prior_microunits, '
            'verified_at, expires_at, authorization_id FROM allowance WHERE singleton = 1'
        ).fetchone()
        if not allowance or allowance[:3] != (self._ledger_job, POLICY, FEE_MODE):
            raise TranslationRequestError('translation_allowance_invalid', evidence)
        cap, prior, verified_at, expires_at, authorization_id = allowance[3:]
        cap_lifted = event is not None and cap in event['lifted_legacy_caps_microunits']
        if not (
            isinstance(authorization_id, str) and _PROVIDER_ID.fullmatch(authorization_id)
            and all(type(n) is int for n in (cap, prior, verified_at, expires_at))
            and 0 <= prior < 2**63 and 0 < cap < 2**63
            and (prior <= cap or cap_lifted)
            and (event is None or prior >= CONSERVED_PRIOR_MICROUNITS)
            and (self.episode_cap is None or (event is None and cap <= self.episode_cap and prior == 0))
            and (POLICY_VERIFIED_AT <= verified_at < expires_at <= POLICY_EXPIRES_AT
                 if event is not None else self._current(verified_at, expires_at))
        ):
            raise TranslationRequestError('translation_allowance_invalid', evidence)
        return cap, prior, expires_at, cap_lifted

    def check_episode_allowance(self):
        """Check the scoped wallet before selecting the policy model by default."""
        if self.episode_cap is None:
            raise TranslationRequestError('translation_admission_scope', {})
        try:
            if not _mode(self.path):
                raise TranslationRequestError('translation_allowance_missing', {})
            with closing(self._connect(readonly=True)) as db:
                self._allowance(db, None, {})
        except (OSError, sqlite3.Error, ValueError):
            raise TranslationRequestError('translation_ledger_unavailable', {}) from None

    def reserve(self, key, amount, evidence, load_key, *, admission=None, identity=None,
                journal_slot=None, journal_request=None, wire_key=None):
        if admission is None and self.episode_cap is None and not self._current(POLICY_VERIFIED_AT, POLICY_EXPIRES_AT):
            raise TranslationRequestError('translation_allowance_invalid', evidence)
        try:
            if not _mode(self.path):
                if admission is not None:
                    self._check_dispatch(admission, identity, POLICY_EXPIRES_AT, evidence)
                raise TranslationRequestError('translation_allowance_missing', evidence)
            with _lock_jobs_root(self.job.parent), closing(self._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                event = None
                if admission is not None:
                    event = self._check_dispatch(admission, identity, POLICY_EXPIRES_AT, evidence)
                cap, prior, expires_at, cap_lifted = self._allowance(db, event, evidence)
                recoveries = {}
                adopted, history = self._history(db, recovery_index=recoveries)
                if adopted and admission is None:
                    raise TranslationRequestError('translation_admission_scope', evidence)
                resumes = evidence.get('resumes_receipt')
                if resumes is not None:
                    predecessor = next((row for row in history if row.receipt == resumes), None)
                    if (predecessor is None or resumes not in recoveries or wire_key is None
                            or journal_request is None
                            or key != fingerprint([wire_key, resumes])
                            or journal_slot != TranslationJournal.receipt_name(evidence['segment_ids'], resumes)
                            or evidence['local_request_id'] != fingerprint([
                                TranslationJournal(self.job).request_id(*journal_request), resumes,
                            ])
                            or predecessor.key != (fingerprint([wire_key, predecessor.resumes])
                                                   if predecessor.resumes else wire_key)):
                        raise TranslationRequestError('translation_request_unresolved', evidence)
                self._check_history(history, key, evidence, slot=journal_slot)
                if admission is not None:
                    self._check_other_jobs(wire_key or key, evidence, journal_request)
                total = sum(row.reserved for row in history if row.reserved is not None)
                limit = 2**63 - 1 if cap_lifted else cap
                if total + prior + amount > limit:
                    raise TranslationRequestError('translation_budget_exhausted', evidence)
                self._check_dispatch(admission, identity, expires_at, evidence)
                try:
                    credential = load_key()
                except Exception:
                    raise TranslationRequestError('translation_credential_missing', evidence) from None
                if not isinstance(credential, str) or not credential.strip():
                    raise TranslationRequestError('translation_credential_missing', evidence)
                self._check_dispatch(admission, identity, expires_at, evidence)
                if admission is not None:
                    evidence['admission_event'] = {
                        **event, 'prior_microunits': prior, 'history_microunits': total,
                        'legacy_cap_microunits': cap, 'legacy_cap_lifted': cap_lifted,
                        'reserved_microunits': amount,
                    }
                if self.episode_cap is not None:
                    evidence['rate_observation_sha256'] = _EPISODE_OBSERVATION_SHA
                db.execute('INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)', (
                    key, evidence['local_request_id'], amount, 'submitted',
                    json.dumps({**evidence, 'billing_uncertain': True, 'completion_uncertain': True}),
                    int(self._clock()),
                ))
            return credential, expires_at
        except (OSError, sqlite3.Error, ValueError, RecursionError):
            raise TranslationRequestError('translation_ledger_unavailable', evidence) from None

    def finish(self, state, evidence):
        try:
            with closing(self._connect()) as db, db:
                result = db.execute(
                    "UPDATE requests SET state = ?, evidence = ? WHERE local_id = ? AND state = 'submitted'",
                    (state, json.dumps(evidence), evidence['local_request_id']),
                )
                if result.rowcount != 1:
                    raise sqlite3.OperationalError('Reservation missing')
        except (OSError, sqlite3.Error):
            raise TranslationRequestError('translation_ledger_unavailable', evidence) from None


def open_translation_request(request, *, timeout):
    import urllib.request

    class _RejectRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
    return opener.open(request, timeout=timeout)


def _safe_id(value, credential, body):
    if (isinstance(value, str) and _PROVIDER_ID.fullmatch(value)
            and credential not in value and value not in credential
            and value.encode('ascii') not in body):
        return value
    return None


def _status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _read_json(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Response exceeds local bound')
    return decode_json_object(data)


def _http_error(error, evidence, credential, raw_body):
    evidence['http_status'] = _status(error.code)
    evidence['provider_retryable'] = (
        error.code in (408, 429) or 500 <= (evidence['http_status'] or 0) <= 599
    )
    if error.headers is not None:
        evidence['provider_request_id'] = _safe_id(error.headers.get('x-request-id'), credential, raw_body)
    evidence['error_body_summary'] = 'unavailable_or_invalid'
    try:
        with error:
            payload = _read_json(error, ERROR_BODY_LIMIT)
    except Exception:
        return
    if evidence['provider_request_id'] is None:
        evidence['provider_request_id'] = _safe_id(payload.get('request_id'), credential, raw_body)
    detail = payload.get('error')
    code = detail.get('code') if isinstance(detail, dict) else None
    if isinstance(code, str) and code in _PROVIDER_CODES and credential not in code:
        evidence['provider_error_code'] = code
    evidence['error_body_summary'] = 'allowlisted_code' if evidence['provider_error_code'] else 'details_omitted'


def _usage(payload):
    raw = payload.get('usage')
    if not isinstance(raw, dict):
        return None
    # Preserve only bounded numeric fields, including representable invalid
    # values for reconciliation. Missing or malformed fields cannot validate.
    return {
        name: value for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')
        if type(value := raw.get(name)) is int and abs(value) <= 2**53
    }


def request_translation(*, provider, model, base_url, messages, stage, timeout,
                        budget, load_key, opener, validate_content, validation_error,
                        journal=None, segment_ids=None, admission=None):
    """Return validated content only after a durable safe response receipt."""
    require_translation_network(fake_opener=opener is not open_translation_request)
    import urllib.request

    evidence = {
        'stage': stage, 'local_request_id': str(uuid.uuid4()),
        'http_status': None, 'provider_request_id': None, 'provider_response_id': None,
        'provider_error_code': None, 'error_body_summary': None, 'provider_retryable': None,
        'safe_to_retry': False, 'billing_uncertain': False, 'completion_uncertain': False, 'usage': None,
        'finish_reason': None, 'response_model': None,
    }
    if not supports_policy(provider, model, base_url):
        raise TranslationRequestError('translation_policy_unverified', evidence)
    if budget is None:
        raise TranslationRequestError('translation_allowance_missing', evidence)
    if journal is not None and journal.path.parent.resolve() != budget.job:
        raise TranslationRequestError('translation_admission_scope', evidence)
    max_tokens = OUTPUT_TOKENS[stage]
    body = json.dumps({
        'model': MODEL, 'messages': messages, 'response_format': {'type': 'json_object'},
        'temperature': 0.2, 'max_tokens': max_tokens, 'thinking': {'type': 'disabled'},
    }, ensure_ascii=False).encode('utf-8')
    url = f'{ENDPOINT}/chat/completions'
    wire_key = key = hashlib.sha256(stage.encode('ascii') + b'\0' + body).hexdigest()
    journal_slot = None
    if journal is not None:
        # Check the stable batch slot before budget admission or credential lookup.
        receipt, pending = journal.preflight(url, body, segment_ids, recovered=budget._can_resume)
        journal_slot = receipt.name
        evidence.update(local_request_id=pending['local_request_id'], segment_ids=segment_ids)
        resumes = pending.get('resumes_receipt')
        if resumes is not None:
            evidence['resumes_receipt'] = resumes
            key = fingerprint([wire_key, resumes])
        if admission is not None or resumes is not None:
            evidence['receipt'] = receipt.name
    elif segment_ids is not None:
        journal_slot = TranslationJournal.receipt_name(segment_ids)
    amount = CONTEXT_TOKENS * INPUT_MICRO_CNY + max_tokens * OUTPUT_MICRO_CNY
    evidence.update(policy=POLICY, input_token_bound=CONTEXT_TOKENS, output_token_bound=max_tokens)
    identity = (provider, model, base_url)
    credential, expires_at = budget.reserve(
        key, amount, evidence, load_key, admission=admission, identity=identity,
        journal_slot=journal_slot, journal_request=(url, body) if segment_ids is not None else None,
        wire_key=wire_key,
    )
    evidence.update(billing_uncertain=True, completion_uncertain=True)

    def fail(code):
        error = TranslationRequestError(code, evidence)
        budget.finish('uncertain', error.evidence)
        return error

    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Authorization': f'Bearer {credential}', 'Content-Type': 'application/json'},
    )

    dispatch_denial = None

    def check_dispatch_deadline():
        nonlocal dispatch_denial
        try:
            budget._check_dispatch(admission, identity, expires_at, evidence)
        except TranslationRequestError as error:
            unsent = {**evidence, 'billing_uncertain': False, 'completion_uncertain': False}
            if admission is not None:
                unsent.update(request_state='not_sent', billing_state='no_charge', completion_state='not_started')
            dispatch_denial = TranslationRequestError(error.code, unsent)
            raise dispatch_denial from None

    def merge_journal(receipt):
        evidence.update({field: value for field, value in receipt.items() if field != 'stage'})

    payload = phase = None
    try:
        if journal is not None:
            response = journal.exchange(
                url, body, timeout=timeout, opener=opener, segment_ids=segment_ids,
                make_request=lambda: request, response_limit=RESPONSE_LIMIT,
                before_open=check_dispatch_deadline, request_key=key, recovered=budget._can_resume,
            )
            merge_journal(journal.last_evidence)
        else:
            check_dispatch_deadline()
            phase = 'opener.open'
            response = opener(request, timeout=timeout)
        phase = 'response.read'
        with response as opened:
            if journal is None:
                evidence['http_status'] = _status(getattr(opened, 'status', None))
                headers = getattr(opened, 'headers', None)
                if headers is not None:
                    evidence['provider_request_id'] = _safe_id(headers.get('x-request-id'), credential, body)
            if evidence['http_status'] == 200:
                try:
                    payload = _read_json(opened, RESPONSE_LIMIT)
                except (ValueError, TypeError, RecursionError):
                    payload = None
    except urllib.error.HTTPError as error:
        _http_error(error, evidence, credential, body)
        raise fail('translation_http_error') from None
    except StudioRecoveryError as error:
        if error is dispatch_denial:
            budget.finish('completed', error.evidence)
            raise
        if phase is not None:
            evidence['transport_failure'] = translation_transport_failure(error, phase)
            raise fail('translation_transport_unknown') from None
        if error.evidence is not None:
            merge_journal(error.evidence)
        if error.code == 'translation_http_error':
            status = evidence['http_status'] or 0
            evidence['provider_retryable'] = status in (408, 429) or 500 <= status <= 599
        if error.code in _MESSAGES:
            raise fail(error.code) from None
        budget.finish('uncertain', {**evidence, 'code': error.code})
        raise
    except Exception as error:
        if phase is not None:
            evidence['transport_failure'] = translation_transport_failure(error, phase)
        raise fail('translation_transport_unknown') from None

    if evidence['http_status'] != 200 or payload is None:
        raise fail('translation_response_unknown')
    evidence['provider_response_id'] = _safe_id(payload.get('id'), credential, body)
    evidence['usage'] = usage = _usage(payload)
    resolved = payload.get('model')
    if resolved not in (MODEL, MODEL_REVISION):
        raise fail('translation_model_unknown')
    evidence['response_model'] = resolved
    if not (
        usage is not None and len(usage) == 3
        and 0 < usage['prompt_tokens'] <= CONTEXT_TOKENS
        and 0 < usage['completion_tokens'] <= max_tokens
        and usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens']
        and usage['total_tokens'] <= CONTEXT_TOKENS
    ):
        raise fail('translation_usage_unknown')
    choices = payload.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise fail('translation_finish_unknown')
    choice = choices[0]
    reason = choice.get('finish_reason')
    if reason in ('stop', 'length', 'content_filter', 'tool_calls', 'insufficient_system_resource', 'aborted'):
        evidence['finish_reason'] = reason
    if reason != 'stop':
        raise fail('translation_finish_unknown')

    if journal is not None:
        try:
            journal.confirm_usage()
        except OSError:
            raise fail('translation_ledger_unavailable') from None
        evidence['billing_state'] = journal.last_evidence['billing_state']
    evidence.update(billing_uncertain=False, completion_uncertain=False)
    try:
        validated = validate_content(choice['message']['content'])
    except (KeyError, ValueError, TypeError, RecursionError, validation_error):
        budget.finish('completed', {**evidence, 'code': 'translation_invalid_structure'})
        raise validation_error(_MESSAGES['translation_invalid_structure']) from None
    budget.finish('completed', {**evidence, 'code': 'translation_completed'})
    return validated


def _main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='Inspect or record local translation recovery continuity')
    parser.add_argument('action', choices=('inspect', 'apply'))
    parser.add_argument('--isolation-root', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--request-key', required=True)
    parser.add_argument('--audit-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        report = TranslationBudget(args.job).recovery_continuity(
            isolation_root=args.isolation_root, request_key=args.request_key,
            audit_sha256=args.audit_sha256, apply=args.action == 'apply',
        )
    except StudioRecoveryError as error:
        print(json.dumps({'status': 'rejected', 'code': error.code}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
