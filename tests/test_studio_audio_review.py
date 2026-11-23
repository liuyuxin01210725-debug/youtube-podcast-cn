import hashlib
import json
from unittest.mock import Mock

import pytest

from studio_document import (
    AUDIO_REVIEW_EVIDENCE_FIELDS, OFFICIAL_EVIDENCE_FIELDS, SEGMENT_CONFIRM_FIELDS,
    audio_review_evidence_hash, segment_evidence_hash,
)
from studio_engine import StudioEngine, StudioInputError
from studio_server import Studio, read, write


AUDIO_EVIDENCE = {
    'evidence_type': 'audio_clip_review', 'source_audio_sha256': 'a' * 64,
    'clip_sha256': 'b' * 64, 'clip_target_start_ms': 0, 'clip_target_end_ms': 1500,
    'review_response_sha256': 'c' * 64, 'review_source': 'Fixture reviewer',
    'review_basis': 'model_assisted_audio_review', 'review_summary': ' 核对短音中的当前说话人。 ',
}
OFFICIAL_EVIDENCE = {
    'official_source_sha256': 'legacy-fixture', 'official_speaker': 'Fixture guest',
    'official_locator': 'turn 2', 'official_excerpt': 'Guest.',
}
NORMALIZED_WAV_SHA = 'd' * 64


@pytest.fixture
def review_job(tmp_path):
    studio = Studio(tmp_path / 'jobs', recover=False)
    job = studio.root / 'synthetic-review'
    job.mkdir()
    write(job / 'state.json', {'status': 'complete'})
    write(job / 'source-identity.json', {
        'kind': 'local', 'input_sha256': AUDIO_EVIDENCE['source_audio_sha256'],
        'sha256': NORMALIZED_WAV_SHA, 'bytes': 8192, 'duration_ms': 2000,
    })
    write(job / 'document.json', {
        'speakers': {'SPEAKER_00': {'voice_slot': 'male_1'}, 'SPEAKER_01': {'voice_slot': 'male_2'}},
        'segments': [
            {'id': 'seg-000001', 'speaker_id': 'SPEAKER_01', 'start_ms': 0, 'end_ms': 1000,
             'text': 'Host.', 'chinese': '主持人。', 'ambiguous': True},
            {'id': 'seg-000002', 'speaker_id': 'SPEAKER_00', 'start_ms': 1000, 'end_ms': 2000,
             'text': 'Guest.', 'chinese': '嘉宾。', 'ambiguous': True},
        ],
        'voices_confirmed': False,
    })
    return studio, job


def document_version(job):
    raw = (job / 'document.json').read_bytes()
    return hashlib.sha256(raw).hexdigest(), json.loads(raw)


def audio_item(digest, segment, **changes):
    evidence = {**AUDIO_EVIDENCE, **changes}
    return {'id': segment['id'], 'speaker_id': segment['speaker_id'], **evidence,
            'evidence_hash': audio_review_evidence_hash(digest, segment, evidence)}


def official_item(digest, segment, **extra):
    return {'id': segment['id'], 'speaker_id': segment['speaker_id'], **OFFICIAL_EVIDENCE, **extra,
            'evidence_hash': segment_evidence_hash(digest, segment, OFFICIAL_EVIDENCE)}


def confirm(studio, job, digest, *items):
    return studio.confirm_segments(job.name, {'document_sha256': digest, 'items': list(items)})


def assert_rejected(studio, job, digest, item, reason):
    paths = [job / 'document.json', job / 'state.json']
    before = [path.read_bytes() for path in paths]
    result = confirm(studio, job, digest, item)
    assert result['applied'] == []
    assert result['rejected'] == [{'id': item['id'], 'reason': reason}]
    assert [path.read_bytes() for path in paths] == before


def test_audio_clip_review_audit_and_canonical_domain_binding(review_job):
    studio, job = review_job
    digest, document = document_version(job)
    segment = document['segments'][0]
    item = audio_item(digest, segment)
    binding = {'evidence_schema': 'audio_clip_review:v1', 'document_sha256': digest,
               **{field: segment[field] for field in SEGMENT_CONFIRM_FIELDS}, **AUDIO_EVIDENCE}
    canonical = json.dumps(binding, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    assert item['evidence_hash'] == hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    for field, value in AUDIO_EVIDENCE.items():
        modified = {**AUDIO_EVIDENCE, field: value + (1 if type(value) is int else ' ')}
        assert audio_review_evidence_hash(digest, segment, modified) != item['evidence_hash']
    wrong_domain = segment_evidence_hash(digest, segment, OFFICIAL_EVIDENCE)
    assert_rejected(studio, job, digest, {**item, 'evidence_hash': wrong_domain}, 'evidence_hash_mismatch')
    assert confirm(studio, job, digest, item)['applied'] == [item['id']]
    saved = read(job / 'document.json')
    assert [row['ambiguous'] for row in saved['segments']] == [False, True]
    assert saved['voices_confirmed'] is False
    audit, = saved['segment_confirmations']
    assert {key: audit[key] for key in AUDIO_EVIDENCE} == AUDIO_EVIDENCE
    assert audit['evidence_type'] == 'audio_clip_review'
    assert audit['review_basis'] == 'model_assisted_audio_review'
    assert audit['evidence_hash'] == audio_review_evidence_hash(audit['document_sha256'], audit, audit)
    assert audit['evidence_hash'] == item['evidence_hash']


@pytest.mark.parametrize('change', ['document_bytes', 'text', 'start_ms', 'end_ms'])
def test_audio_clip_review_rejects_stale_document_or_segment_fingerprint(review_job, change):
    studio, job = review_job
    digest, document = document_version(job)
    row = document['segments'][0]
    item = audio_item(digest, row)
    original = row.copy()
    if change == 'document_bytes':
        document['segments'][1]['chinese'] = '修改其他段。'
    else:
        row[change] = 'Changed.' if change == 'text' else row[change] + 1
    write(job / 'document.json', document)
    before = (job / 'document.json').read_bytes()
    with pytest.raises(ValueError, match='document_sha256'):
        confirm(studio, job, digest, item)
    assert (job / 'document.json').read_bytes() == before
    current, _ = document_version(job)
    assert_rejected(studio, job, current, item, 'evidence_hash_mismatch')
    if change != 'document_bytes':
        assert_rejected(studio, job, current, audio_item(current, original), 'evidence_hash_mismatch')


@pytest.mark.parametrize('mode', [None, 'official_transcript', 'audio_clip_review'], ids=['default', 'official', 'audio'])
def test_confirm_segments_rejects_mixed_mode_and_unknown_keys(review_job, mode):
    studio, job = review_job
    digest, document = document_version(job)
    segment = document['segments'][0]
    if mode == 'audio_clip_review':
        item = audio_item(digest, segment)
        foreign = OFFICIAL_EVIDENCE_FIELDS
    else:
        item = official_item(digest, segment, **({'evidence_type': mode} if mode else {}))
        foreign = [field for field in AUDIO_REVIEW_EVIDENCE_FIELDS if field != 'evidence_type']
    for field in [*foreign, 'self_certified']:
        for value in ('', None):
            assert_rejected(studio, job, digest, {**item, field: value}, 'unexpected_item_fields')
    for value in (None, '', 'unknown', [], False):
        assert_rejected(studio, job, digest, {**item, 'evidence_type': value}, 'invalid_evidence_type')


@pytest.mark.parametrize('field,value', [
    ('clip_target_start_ms', -1), ('clip_target_start_ms', False),
    ('clip_target_start_ms', 0.0), ('clip_target_end_ms', -1), ('clip_target_end_ms', True),
    ('clip_target_end_ms', 0), ('clip_target_end_ms', '1500'),
    ('review_source', ' \n '), ('review_source', 'x' * 129), ('review_source', 7),
    ('review_summary', ' \t '), ('review_summary', '字' * 501), ('review_summary', []),
    ('review_basis', 'human_ear_confirmed'),
])
def test_audio_clip_review_rejects_invalid_window_or_review_metadata(review_job, field, value):
    studio, job = review_job
    digest, document = document_version(job)
    assert_rejected(studio, job, digest, audio_item(digest, document['segments'][0], **{field: value}),
                    f'invalid_{field}')


@pytest.mark.parametrize('field', ['source_audio_sha256', 'clip_sha256', 'review_response_sha256'])
@pytest.mark.parametrize('value', ['a' * 63, 'A' * 64, ' ' + 'a' * 63, 'fake', 'a' * 64 + '\n', None],
                         ids=['short', 'uppercase', 'space', 'fake', 'newline', 'null'])
def test_audio_clip_review_rejects_invalid_sha256(review_job, field, value):
    studio, job = review_job
    digest, document = document_version(job)
    assert_rejected(studio, job, digest, audio_item(digest, document['segments'][0], **{field: value}),
                    f'invalid_{field}')


def test_audio_clip_review_requires_all_fields_and_matches_original_input_lock(review_job):
    studio, job = review_job
    digest, document = document_version(job)
    segment = document['segments'][0]
    for field in AUDIO_REVIEW_EVIDENCE_FIELDS:
        item = audio_item(digest, segment)
        del item[field]
        reason = 'unexpected_item_fields' if field == 'evidence_type' else f'invalid_{field}'
        assert_rejected(studio, job, digest, item, reason)
    for wrong in (NORMALIZED_WAV_SHA, 'e' * 64):
        assert_rejected(studio, job, digest, audio_item(digest, segment, source_audio_sha256=wrong),
                        'source_audio_sha256_mismatch')


@pytest.mark.parametrize('keep_wav_lock', [True, False], ids=['wav-only', 'no-lock'])
def test_audio_clip_review_accepts_well_formed_source_hash_without_original_lock(review_job, keep_wav_lock):
    studio, job = review_job
    lock = job / 'source-identity.json'
    if keep_wav_lock:
        write(lock, {'kind': 'youtube', 'sha256': NORMALIZED_WAV_SHA})
    else:
        lock.unlink()
    digest, document = document_version(job)
    item = audio_item(digest, document['segments'][0])
    assert confirm(studio, job, digest, item)['applied'] == [item['id']]


def test_official_confirmation_retains_untyped_historical_audit(review_job):
    studio, job = review_job
    digest, document = document_version(job)
    first, second = document['segments']
    audit = {**{field: first[field] for field in SEGMENT_CONFIRM_FIELDS}, **official_item(digest, first),
             'hash_version': 1, 'confirmed_at': 1, 'document_sha256': digest}
    first['ambiguous'] = False
    document['segment_confirmations'] = [audit]
    write(job / 'document.json', document)
    current, _ = document_version(job)
    item = official_item(current, second, evidence_type='official_transcript')
    assert confirm(studio, job, current, item)['applied'] == [item['id']]
    legacy, explicit = read(job / 'document.json')['segment_confirmations']
    assert legacy == audit
    assert 'evidence_type' not in legacy
    assert segment_evidence_hash(legacy['document_sha256'], legacy, legacy) == legacy['evidence_hash']
    assert explicit['evidence_type'] == 'official_transcript'


def test_render_gate_requires_every_pending_segment_and_blocks_legacy_override(review_job, monkeypatch):
    studio, job = review_job
    digest, document = document_version(job)
    first, second = document['segments']
    engine = StudioEngine(job)
    build = Mock(side_effect=RuntimeError('fixture stopped at backend boundary'))
    monkeypatch.setattr('studio_engine.build_backend', build)
    with pytest.raises(ValueError, match='confirm-segments'):
        studio.confirm_speakers(job.name)
    assert confirm(studio, job, digest)['applied'] == []
    assert_rejected(studio, job, digest, {'id': second['id'], 'speaker_id': second['speaker_id']},
                    'invalid_official_source_sha256')
    assert document_version(job) == (digest, document)
    for preview in (None, 1):
        with pytest.raises(StudioInputError, match='2 段说话人待核对'):
            engine.render({}, document, preview, accept_unreviewed=True)
    assert confirm(studio, job, digest, official_item(digest, first))['applied'] == [first['id']]
    digest, document = document_version(job)
    for preview in (None, 1):
        with pytest.raises(StudioInputError, match='1 段说话人待核对'):
            engine.render({}, document, preview)
    build.assert_not_called()
    assert confirm(studio, job, digest, audio_item(digest, second))['applied'] == [second['id']]
    reviewed = read(job / 'document.json')
    assert all(row['ambiguous'] is False for row in reviewed['segments'])
    for preview in (None, 1):
        with pytest.raises(RuntimeError, match='fixture stopped at backend boundary'):
            engine.render({}, reviewed, preview)
    assert build.call_count == 2


def test_update_document_double_flip_preserves_ambiguity_then_audio_confirms_new_speaker(review_job):
    studio, job = review_job
    _, initial = document_version(job)
    initial['voices_confirmed'] = True
    initial['segments'][1]['ambiguous'] = False
    write(job / 'document.json', initial)
    target = initial['segments'][0]['id']
    for new_speaker in ('SPEAKER_00', 'SPEAKER_01', 'SPEAKER_00'):
        rows = read(job / 'document.json')['segments']
        # The document POST must ignore a caller's attempt to write the flag.
        rows[0].update(speaker_id=new_speaker, ambiguous=False)
        rows[1].update(speaker_id='SPEAKER_01' if new_speaker == 'SPEAKER_00' else 'SPEAKER_00',
                       ambiguous=True)
        studio.update_document(job.name, {'segments': rows})
        saved = read(job / 'document.json')
        assert saved['voices_confirmed'] is False
        assert saved['segments'][0]['speaker_id'] == new_speaker
        assert [row['ambiguous'] for row in saved['segments']] == [True, False]
        assert 'segment_confirmations' not in saved
    digest, saved = document_version(job)
    item = audio_item(digest, saved['segments'][0])
    assert_rejected(studio, job, digest, {**item, 'speaker_id': 'SPEAKER_01'}, 'speaker_id_mismatch')
    assert confirm(studio, job, digest, item)['applied'] == [target]
    reviewed = read(job / 'document.json')
    assert reviewed['segments'][0]['ambiguous'] is False
    assert reviewed['voices_confirmed'] is False
    assert reviewed['segment_confirmations'][0]['speaker_id'] == 'SPEAKER_00'
    assert reviewed['segment_confirmations'][0]['evidence_hash'] == item['evidence_hash']
