import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_publish_icloud as publisher


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def fixture_job(tmp_path, ident, title, timestamp, youtube_id):
    job = tmp_path / ident
    job.mkdir()
    write_json(job / 'state.json', {'title': title, 'status': 'complete', 'action': 'render'})
    write_json(job / 'metadata.json', {'video_id': youtube_id})
    audio = job / 'output.mp3'
    audio.write_bytes(b'fixture audio')
    os.utime(audio, (timestamp, timestamp))
    return job


def listed_episodes(library):
    listing = json.loads((library / 'library.json').read_text(encoding='utf-8'))
    return {episode['id']: episode for episode in listing['episodes']}


def test_publish_preserves_heterogeneous_records_and_overlays_refreshed_core(monkeypatch, tmp_path):
    library = tmp_path / 'library'
    monkeypatch.setattr(publisher, 'probe_duration_sec', lambda path: 125.5)
    monkeypatch.setattr('studio_artwork.attach_cover', lambda *args, **kwargs: None)
    jobs = [
        fixture_job(tmp_path, 'sleep-01', '睡眠与记忆', 1_000, 'SleepSource'),
        fixture_job(tmp_path, 'diet-02', '饮食与运动', 2_000, 'DietSource1'),
        fixture_job(tmp_path, 'rest-03', '恢复的节奏', 3_000, 'RestSource1'),
    ]
    for job in jobs:
        publisher.publish_job(job, library)

    old = listed_episodes(library)
    old['sleep-01'].update(show='Huberman Lab', lens_ids=['sleep', 'learning'],
                           annotations={'editor': {'verified': True, 'tags': ['访谈', None]}})
    old['diet-02'].update(show='The Proof', lens_ids=['nutrition'],
                          guests=['Simon Hill'], discovery={'sections': [{'score': 0, 'topics': []}]})
    old['rest-03'].update(show='The Drive', lens_ids=[], custom={'pinned': False, 'note': None})
    extension = {'schema_version': 1, 'settings': {'pinned': False}}
    write_json(library / 'library.json', {**extension, 'episodes': list(old.values())})

    fresh = fixture_job(tmp_path, 'fresh-04', '新一期', 4_000, 'FreshSource')
    publisher.publish_job(fresh, library)
    after = listed_episodes(library)
    assert all(json.loads((library / 'library.json').read_bytes())[key] == value
               for key, value in extension.items())
    assert list(after) == ['fresh-04', 'rest-03', 'diet-02', 'sleep-01']
    for ident, record in old.items():
        assert record['id'] == record['source_job_id'] == ident
        assert after[ident] == record

    target = jobs[1]
    write_json(target / 'state.json', {'title': '饮食与运动：更新版', 'status': 'complete'})
    write_json(target / 'metadata.json', {'video_id': 'DietSource2'})
    os.utime(target / 'output.mp3', (5_000, 5_000))
    monkeypatch.setattr(publisher, 'probe_duration_sec', lambda path: 149.75)
    publisher.publish_job(target, library)
    updated = listed_episodes(library)
    assert updated == {**after, target.name: {
        **after[target.name],
        'title': '饮食与运动：更新版',
        'created_at': datetime.fromtimestamp(5_000, timezone.utc).isoformat(timespec='seconds'),
        'duration_sec': 149.75,
        'youtube_id': 'DietSource2',
    }}
    assert next(iter(updated)) == target.name


@pytest.mark.parametrize('id_key', ['episode_id', 'source_job_id'])
def test_rebuild_normalizes_legacy_episode_identifiers(tmp_path, id_key):
    library = tmp_path / 'library'
    ident = 'kept'
    episode = library / 'episodes' / ident
    episode.mkdir(parents=True)
    data = {'title': '目录中的节目', 'created_at': '2026-11-20T00:00:00+00:00'}
    write_json(episode / 'episode.json', data)
    record = {**data, id_key: ident, 'show': '旧节目', 'lens_ids': ['sleep'],
              'custom': {'pinned': False, 'note': None}}
    write_json(library / 'library.json', {'episodes': [record]})

    publisher.rebuild_library(library)

    assert listed_episodes(library) == {
        ident: {**record, 'id': ident, 'source_job_id': ident,
                'duration_sec': None, 'file': 'output.mp3'},
    }


def test_rebuild_limits_metadata_to_the_identified_valid_episode(tmp_path):
    library = tmp_path / 'library'
    root = library / 'episodes'
    data = {'title': '目录中的节目', 'created_at': '2026-11-20T00:00:00+00:00'}
    for ident in ('kept', 'other', 'invalid name'):
        episode = root / ident
        episode.mkdir(parents=True)
        write_json(episode / 'episode.json', data)
    (root / 'linked').symlink_to(root / 'kept', target_is_directory=True)
    (root / 'invalid-record').mkdir()
    write_json(root / 'invalid-record' / 'episode.json', {**data, 'file': 'other.mp3'})
    write_json(library / 'library.json', {'episodes': [
        {'id': 'kept', 'episode_id': 'other', 'source_job_id': 'other', 'show': '只属于 kept'},
        {'id': 'missing', 'source_job_id': 'other', 'show': '已删除'},
        {'id': 'linked', 'show': '符号链接'},
        {'id': 'invalid-record', 'show': '非法记录'},
        {'id': None, 'episode_id': 'other', 'source_job_id': 'kept', 'lens_ids': ['nutrition']},
    ]})

    publisher.rebuild_library(library)

    assert listed_episodes(library) == {
        'kept': {**data, 'id': 'kept', 'episode_id': 'other', 'source_job_id': 'kept',
                 'show': '只属于 kept', 'duration_sec': None, 'file': 'output.mp3'},
        'other': {**data, 'id': 'other', 'episode_id': 'other', 'source_job_id': 'other',
                  'lens_ids': ['nutrition'], 'duration_sec': None, 'file': 'output.mp3'},
    }


@pytest.mark.parametrize('prior', [
    pytest.param(b'{', id='broken'),
    pytest.param(b'\xff', id='invalid-utf8'),
    pytest.param(b'[]', id='array'),
    pytest.param(b'null', id='null'),
    pytest.param(b'{"episodes": {}}', id='invalid-episodes'),
    pytest.param(b'{}', id='missing-episodes'),
    pytest.param(b'{"episodes": [null]}', id='null-record'),
    pytest.param(b'{"episodes": ["non-object record"]}', id='string-record'),
    pytest.param(b'{"episodes": [{"episode_id": "invalid name", "source_job_id": "other"}]}', id='invalid-id'),
    pytest.param(b'{"episodes": [{"id": ["other"], "source_job_id": "other"}]}', id='invalid-id-type'),
    pytest.param(b'{"episodes": [{"id": "kept"}, {"episode_id": "kept"}]}', id='duplicate-id'),
    pytest.param(b'{"episodes": [], "episodes": []}', id='duplicate-field'),
    pytest.param(b'{"episodes": [], "extension": 1e400}', id='non-finite-extension'),
    pytest.param(b'{"episodes": ' + b'[' * 2000 + b']' * 2000 + b'}', id='too-deep'),
    pytest.param(b'{"episodes": ' + b'1' * 5000 + b'}', id='integer-limit'),
])
def test_rebuild_refuses_unusable_existing_index_without_overwriting(tmp_path, prior):
    library = tmp_path / 'library'
    episode = library / 'episodes' / 'kept'
    episode.mkdir(parents=True)
    listing = library / 'library.json'
    listing.write_bytes(prior)
    data = {'title': '仍在目录的节目', 'created_at': '2026-11-20T00:00:00+00:00'}
    write_json(episode / 'episode.json', data)

    with pytest.raises(publisher.PublishError, match='现有资料库索引格式无效'):
        publisher.rebuild_library(library)
    assert listing.read_bytes() == prior


def test_rebuild_from_missing_index(tmp_path):
    library = tmp_path / 'library'
    episode = library / 'episodes' / 'kept'
    episode.mkdir(parents=True)
    data = {'title': '仍在目录的节目', 'created_at': '2026-11-20T00:00:00+00:00'}
    write_json(episode / 'episode.json', data)

    publisher.rebuild_library(library)

    assert listed_episodes(library) == {
        'kept': {**data, 'id': 'kept', 'source_job_id': 'kept',
                 'duration_sec': None, 'file': 'output.mp3'},
    }


@pytest.mark.parametrize('error', [PermissionError('offline read failure'), FileNotFoundError('lost after lstat')])
def test_rebuild_read_failure_preserves_bytes_then_merges_after_access_returns(monkeypatch, tmp_path, error):
    library = tmp_path / 'library'
    episode = library / 'episodes' / 'kept'
    episode.mkdir(parents=True)
    listing = library / 'library.json'
    data = {'title': '更新后的标题', 'created_at': '2026-11-20T00:00:00+00:00'}
    write_json(episode / 'episode.json', data)
    extension = {'show': '原来节目', 'lens_ids': ['sleep'], 'custom': {'pinned': True}}
    write_json(listing, {'episodes': [{'id': 'kept', 'title': '原来标题', **extension}], 'theme': 'default'})
    before = listing.read_bytes()
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == listing:
            raise error
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, 'read_text', unreadable)
        with pytest.raises(publisher.PublishError, match='无法读取现有资料库索引'):
            publisher.rebuild_library(library)
        assert listing.read_bytes() == before

    assert publisher.rebuild_library(library)['theme'] == 'default'
    assert listed_episodes(library)['kept'] == {
        **extension, **data, 'id': 'kept', 'source_job_id': 'kept',
        'duration_sec': None, 'file': 'output.mp3',
    }
