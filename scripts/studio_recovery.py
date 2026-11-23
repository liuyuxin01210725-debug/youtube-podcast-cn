"""Private draft checkpoints and bounded request receipts for Studio podcasts."""

import builtins
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile
import urllib.error

from podcast_captions import PodcastPipelineError

_PROVIDER_ID = re.compile(
    r'(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|req[-_][0-9a-f]{16,48})',
    re.IGNORECASE | re.ASCII,
)
_ERROR_CODES = {
    'invalid_request_error', 'authentication_error', 'invalid_api_key',
    'model_not_found', 'insufficient_balance', 'rate_limit_exceeded',
    'server_error', 'overloaded_error', 'service_unavailable',
}
_MAX_REPLY = 2 * 1024 * 1024
_MAX_ERROR = 4096
_LOCAL_REQUEST_ID = re.compile(r'[0-9a-f]{64}', re.ASCII)
# Reviewed socket-deny implementation. All three entry points raise locally
# without DNS, a socket, or any delegation to the replaced implementation.
SOCKET_DENY_HOOK_SHA256 = '859244944285c0aa6ee9459a929b2f4eade49187418574d8fa94c2ec716c12f2'
LOCAL_SOCKET_DENIED = {
    'exception_type': 'OSError', 'phase': 'opener.open', 'reason': 'local_socket_hook_denied',
    'hook_sha256': SOCKET_DENY_HOOK_SHA256,
}


class StudioRecoveryError(PodcastPipelineError):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


def require_translation_network(*, fake_opener=False):
    # A site hook can install an audit-only block or replace the socket APIs.
    # Inspect the loaded interpreter without creating a socket or resolving DNS.
    site_block = any(
        getattr(sys.modules.get(name), 'NETWORK_DISABLED', False) is True
        for name in ('sitecustomize', 'usercustomize')
    )
    for name in ('socket', 'create_connection', 'getaddrinfo'):
        api = getattr(socket, name)
        if fake_opener and type(api).__module__ == 'unittest.mock':
            continue  # Existing in-process tests pair these mocks with fake openers.
        if (getattr(api, '__module__', None) != 'socket'
                or getattr(api, '__qualname__', None) != name
                or getattr(api, '__globals__', vars(socket)) is not vars(socket)):
            break
    else:
        if not site_block:
            return
    code = 'translation_network_disabled'
    raise StudioRecoveryError(
        code, '当前解释器的网络接口被替换或禁用，未读取翻译凭据，未发出请求',
        {'code': code},
    )


def translation_transport_failure(error, phase):
    """Keep typed diagnostics without copying exception strings or arguments."""
    import ssl
    from http.client import IncompleteRead, RemoteDisconnected

    def classify(exc):
        actual = type(exc)
        builtin = getattr(builtins, actual.__name__, None) is actual
        for kind, reason in (
            (urllib.error.URLError, 'url_open_failed'),
            (socket.gaierror, 'dns_lookup_failed'),
            (ssl.SSLCertVerificationError, 'tls_certificate_verification_failed'),
            (ssl.SSLError, 'tls_failed'),
            (RemoteDisconnected, 'remote_disconnected'),
            (IncompleteRead, 'incomplete_response'),
            (TimeoutError, 'timed_out'),
            (ConnectionError, 'connection_failed'),
            (PermissionError, 'operation_denied'),
            (OSError, 'os_error'),
        ):
            if isinstance(exc, kind):
                return (actual.__name__ if builtin else kind.__name__), reason
        return (actual.__name__ if builtin else 'Exception'), 'transport_failed'

    category, reason = classify(error)
    underlying = error.reason if isinstance(error, urllib.error.URLError) else error
    if (phase == 'opener.open' and type(underlying) is OSError
            and underlying.args == ('network disabled in precommit verification',)):
        hook = sys.modules.get('sitecustomize')
        if (hook is not None and getattr(hook, '_DeniedSocket', None) is socket.socket
                and getattr(hook, '_deny', None) is socket.create_connection is socket.getaddrinfo):
            try:
                if hashlib.sha256(Path(hook.__file__).read_bytes()).hexdigest() == SOCKET_DENY_HOOK_SHA256:
                    denial_frames = (hook._DeniedSocket.__init__.__code__, hook._deny.__code__)
                    frame = underlying.__traceback__
                    while frame is not None:
                        if frame.tb_frame.f_code in denial_frames:
                            return LOCAL_SOCKET_DENIED.copy()
                        frame = frame.tb_next
            except (AttributeError, OSError, TypeError):
                pass
    if underlying is not error and isinstance(underlying, OSError):
        _, reason = classify(underlying)
    diagnostic = {'exception_type': category, 'phase': phase, 'reason': reason}
    number = underlying.errno if isinstance(underlying, OSError) else None
    if type(number) is int and -(2**31) <= number < 2**31:
        diagnostic['errno'] = number
        if reason in ('os_error', 'connection_failed'):
            diagnostic['reason'] = errno.errorcode.get(number, reason)
    return diagnostic


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def podcast_job_lock(job):
    # Separate open descriptions contend across threads and processes. Never
    # remove the lock file, since another worker may already have opened it.
    fd = os.open(job / '.podcast-run.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StudioRecoveryError('job_busy', '这个播客任务仍在处理') from None
        yield
    finally:
        os.close(fd)


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(path, value, *, exclusive=False):
    _save_bytes(path, json.dumps(value, ensure_ascii=False).encode('utf-8'), exclusive=exclusive)


def _save_bytes(path, raw, *, exclusive=False):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_regular_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(errno.EINVAL, 'Expected a regular file')
        return stream.read()


def decode_json(value):
    def object_from_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('duplicate JSON fields')
        return result
    return json.loads(value, object_pairs_hook=object_from_pairs)


def decode_json_object(value):
    result = decode_json(value)
    if not isinstance(result, dict):
        raise ValueError('expected object')
    return result


def load_source(path, binding, reference_hash):
    try:
        if path.is_symlink():
            raise ValueError
        envelope = decode_json_object(path.read_bytes())
        draft = envelope['draft']
        if envelope['binding'] != binding or envelope['digest'] != fingerprint(draft):
            raise ValueError
        if any(reference_hash(item['path']) != item['sha256'] for item in draft['references'].values()):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise StudioRecoveryError(
            'translation_source_mismatch', '续做来源、分段或参考样本无法核验，已有稿件和缓存已保留',
        ) from None
    return draft


def save_source(path, binding, draft):
    _save(path, {'binding': binding, 'digest': fingerprint(draft), 'draft': draft})


def _safe_id(value, request):
    if not isinstance(value, str) or not _PROVIDER_ID.fullmatch(value):
        return None
    # Never trust free-form provider fields. Also reject echoes from the
    # request, including the credentials, URL query and prompt.
    credential = request.get_header('Authorization', '').removeprefix('Bearer ')
    if (value in request.full_url or value.encode('ascii') in request.data
            or (credential and (credential in value or value in credential))
            or any(value in header for _, header in request.header_items())):
        return None
    return value


def _status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _usage(payload):
    raw = payload.get('usage')
    if not isinstance(raw, dict):
        return None
    names = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    result = {name: raw.get(name) for name in names}
    if not all(type(value) is int and 0 <= value <= 2**53 for value in result.values()):
        return None
    if result['prompt_tokens'] + result['completion_tokens'] != result['total_tokens']:
        return None
    return result


class TranslationJournal:
    """Serialize request evidence under the owning Studio podcast run lock."""

    def __init__(self, job):
        self.path = job / 'translation-requests'
        self.last_evidence = None

    def request_id(self, url, body):
        return hashlib.sha256(
            str(self.path.parent).encode() + b'\0' + url.encode() + b'\0' + body,
        ).hexdigest()

    @staticmethod
    def receipt_name(segment_ids, resumes=None):
        return f'{fingerprint([segment_ids, resumes] if resumes else segment_ids)}.json'

    def preflight(self, url, body, segment_ids, *, recovered=None):
        if self.path.is_symlink():
            raise StudioRecoveryError('translation_journal_invalid', '翻译记录目录无法核验')
        # Keep the batch slot distinct from the exact wire hash, so changing
        # the prompt or endpoint cannot resubmit a previously recorded batch.
        local_id = wire_id = self.request_id(url, body)
        resumes = None
        seen = set()
        while True:
            record = self.path / self.receipt_name(segment_ids, resumes)
            try:
                record.lstat()
            except FileNotFoundError:
                break
            except OSError:
                raise StudioRecoveryError('translation_journal_invalid', '翻译记录无法核验') from None
            original = self._existing_reference(record, segment_ids)
            if (recovered is not None and original.get('local_request_id') == local_id
                    and record.name not in seen and recovered(record.name, local_id)):
                seen.add(record.name)
                resumes = record.name
                local_id = fingerprint([wire_id, resumes])
                continue
            raise StudioRecoveryError(
                'translation_request_already_recorded',
                '此块已有请求记录，缺少可复用译文，需核对原请求后再续做',
                {'stage': 'translation', 'segment_ids': segment_ids,
                 'request_state': 'blocked_existing', 'receipt': record.name, **original},
            )
        evidence = {
            'stage': 'translation', 'segment_ids': segment_ids, 'local_request_id': local_id,
            'request_state': 'submitted', 'completion_state': 'unknown', 'billing_state': 'unknown',
            'http_status': None, 'provider_request_id': None, 'provider_response_id': None,
            'provider_error_code': None, 'error_body_summary': None, 'usage': None,
            'finish_reason': None,
        }
        if resumes is not None:
            evidence['resumes_receipt'] = resumes
        return record, evidence

    def confirm_usage(self):
        # The budget verified the reported usage against the request's bounds.
        # This records usage evidence and leaves the entire reservation in place.
        receipt = {**self.last_evidence, 'billing_state': 'usage_verified'}
        name = receipt.pop('receipt')
        _save(self.path / name, receipt)
        self.last_evidence = {**receipt, 'receipt': name}

    @staticmethod
    def _existing_reference(record, segment_ids):
        # The receipt is private but mutable on disk. Surface only bounded,
        # validated reference fields and never retry when they cannot be read.
        try:
            fd = os.open(record, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return {}
                raw = stream.read(_MAX_ERROR + 1)
            if len(raw) > _MAX_ERROR:
                return {}
            prior = decode_json_object(raw)
        except (OSError, ValueError, RecursionError):
            return {}
        local_id = prior.get('local_request_id')
        state = prior.get('request_state')
        if (
            prior.get('segment_ids') != segment_ids
            or not isinstance(local_id, str) or not _LOCAL_REQUEST_ID.fullmatch(local_id)
            or state not in ('submitted', 'uncertain', 'response_received', 'not_sent')
        ):
            return {}
        provider_id = prior.get('provider_request_id')
        return {
            'local_request_id': local_id,
            'original_request_state': state,
            'http_status': _status(prior.get('http_status')),
            'provider_request_id': (
                provider_id if isinstance(provider_id, str) and _PROVIDER_ID.fullmatch(provider_id) else None
            ),
        }

    def exchange(self, url, body, *, timeout, opener, segment_ids, make_request,
                 response_limit=_MAX_REPLY, before_open=None, request_key=None, recovered=None):
        self.last_evidence = None
        record, evidence = self.preflight(url, body, segment_ids, recovered=recovered)
        if request_key is not None:
            evidence['request_key'] = request_key
        request = make_request()
        if not self.path.exists():
            self.path.mkdir()
            _sync_dir(self.path.parent)
        if evidence.get('resumes_receipt') is not None:
            _save(record, evidence, exclusive=True)
        else:
            _save(record, evidence)

        def fail(code, message):
            evidence['request_state'] = 'uncertain'
            _save(record, evidence)
            return StudioRecoveryError(code, message, {**evidence, 'receipt': record.name})

        def remember_http(response, status):
            evidence['http_status'] = _status(status)
            headers = getattr(response, 'headers', None)
            if headers is not None:
                evidence['provider_request_id'] = _safe_id(headers.get('x-request-id'), request)

        if before_open is not None:
            try:
                before_open()
            except StudioRecoveryError:
                evidence.update(request_state='not_sent', completion_state='not_started', billing_state='no_charge')
                _save(record, evidence)
                raise

        phase = 'opener.open'
        try:
            opened = opener(request, timeout=timeout)
            phase = 'response.read'
            with opened as response:
                remember_http(response, getattr(response, 'status', None))
                raw = response.read(response_limit + 1)
        except urllib.error.HTTPError as error:
            remember_http(error, error.code)
            evidence['error_body_summary'] = 'omitted'
            try:
                with error:
                    body = error.read(_MAX_ERROR + 1)
                detail = decode_json_object(body).get('error') if len(body) <= _MAX_ERROR else None
                code = detail.get('code') if isinstance(detail, dict) else None
                credential = request.get_header('Authorization', '').removeprefix('Bearer ')
                if (isinstance(code, str) and code in _ERROR_CODES and code not in credential
                        and (not credential or credential not in code)):
                    evidence['provider_error_code'] = code
                    evidence['error_body_summary'] = 'allowlisted_code'
            except Exception:
                pass
            raise fail('translation_http_error', '翻译请求遇到 HTTP 错误，结果待核对') from None
        except Exception as error:
            evidence['transport_failure'] = translation_transport_failure(error, phase)
            raise fail('translation_transport_unknown', '翻译请求结果未知，需先核对原请求') from None

        try:
            payload = decode_json_object(raw) if len(raw) <= response_limit else None
        except (TypeError, ValueError, RecursionError):
            payload = None
        if evidence['http_status'] != 200 or payload is None:
            raise fail('translation_response_unknown', '翻译服务响应无法核验，需先核对原请求')
        evidence['provider_response_id'] = _safe_id(payload.get('id'), request)
        evidence['usage'] = _usage(payload)
        choices = payload.get('choices')
        if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
            reason = choices[0].get('finish_reason')
            if reason in ('stop', 'length', 'content_filter', 'tool_calls', 'insufficient_system_resource', 'aborted'):
                evidence['finish_reason'] = reason
        if evidence['finish_reason'] != 'stop':
            raise fail('translation_finish_unknown', '翻译未确认正常结束，需先核对原请求')
        evidence.update(request_state='response_received', completion_state='provider_reported_stop')
        _save(record, evidence)
        self.last_evidence = {**evidence, 'receipt': record.name}
        return io.BytesIO(raw)
