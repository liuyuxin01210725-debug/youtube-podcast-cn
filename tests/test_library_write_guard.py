import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import library_write_guard as guard
import shengjian_library_snapshot as snapshot_mod
import studio_original_pending as pending
import studio_publish_icloud as publisher
import studio_push_iphone as push


@pytest.fixture(autouse=True)
def _isolate_library_and_publish_hooks(monkeypatch, tmp_path):
    monkeypatch.setenv('PRIVATE_LISTEN_LIBRARY', str(tmp_path / 'forbidden-default'))
    monkeypatch.setattr(publisher, '_require_audio_preflight', lambda *a, **k: None)
    monkeypatch.setattr(publisher, '_snapshot_library_or_refuse', lambda *a, **k: None)
    monkeypatch.setattr(publisher, '_restore_library_snapshot', lambda *a, **k: None)
    monkeypatch.setattr(publisher, '_discard_library_snapshot', lambda *a, **k: None)
    monkeypatch.setattr(publisher, 'probe_duration_sec', lambda *a, **k: 60.0)
    monkeypatch.setattr('studio_artwork.attach_cover', lambda *a, **k: None)


def _encode(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _ep(ident: str, *, created_at: str = '2026-11-20T00:00:00+00:00', title: str | None = None) -> dict:
    return {
        'id': ident,
        'title': title or f'节目 {ident}',
        'created_at': created_at,
        'duration_sec': 60.0,
        'source_job_id': ident,
        'file': 'output.mp3',
    }


def _fixture_job(root: Path, ident: str, *, timestamp: int = 1_000, title: str | None = None) -> Path:
    job = root / ident
    job.mkdir(parents=True, exist_ok=True)
    (job / 'state.json').write_text(
        json.dumps({'title': title or f'标题 {ident}', 'status': 'complete', 'action': 'render'}, ensure_ascii=False),
        encoding='utf-8',
    )
    (job / 'metadata.json').write_text(json.dumps({'video_id': f'Vid{ident}'}, ensure_ascii=False), encoding='utf-8')
    (job / 'transcript_zh.json').write_text(
        json.dumps({'segments': [{'id': 'seg-1', 'zh': '测试译稿'}]}, ensure_ascii=False),
        encoding='utf-8',
    )
    audio = job / 'output.mp3'
    audio.write_bytes(b'fixture-audio-' + ident.encode('utf-8'))
    os.utime(audio, (timestamp, timestamp))
    return job


def _fake_pending_bound(inputs: Path):
    inputs.mkdir(parents=True, exist_ok=True)
    audio_bytes = b'fake-m4a-audio'
    (inputs / pending.AUDIO).write_bytes(audio_bytes)
    audio_sha = hashlib.sha256(audio_bytes).hexdigest()
    raw_sha = hashlib.sha256(b'{}').hexdigest()
    source = {
        'title': '测试原音',
        'show': '测试节目',
        'host': '主播',
        'guest': '嘉宾',
        'source_url': pending.EPISODE_URL,
        'audio_url': 'https://example.invalid/audio.m4a',
    }
    proofs = {pending.META: b'{}'}
    asr = {'status': 'asr_unreviewed', 'segment_count': 1}
    units = [{'id': 0, 'start': 0.0, 'end': 5.0, 'text': '第一句', 'status': 'pending_review'}]
    return source, proofs, audio_sha, raw_sha, 10.0, asr, units


def _seed_library(library: Path, ids: list[str]) -> bytes:
    library.mkdir(parents=True, exist_ok=True)
    rows = []
    for idx, ident in enumerate(ids):
        ep_dir = library / 'episodes' / ident
        ep_dir.mkdir(parents=True, exist_ok=True)
        row = _ep(ident, created_at=f'2026-11-20T00:0{idx}:00+00:00')
        (ep_dir / 'episode.json').write_bytes(_encode(row))
        (ep_dir / 'output.mp3').write_bytes(b'audio-' + ident.encode('utf-8'))
        rows.append(row)
    raw = _encode({'episodes': rows})
    (library / 'library.json').write_bytes(raw)
    return raw


# ---------------------------------------------------------------------------
# A. 闸 2：读不到 / 解析失败 / 非普通文件 / 未核实为空 → 拒绝且不改写
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('bad_bytes', [
    b'{',
    b'{"episodes": [], "extra": NaN}',
    b'{"episodes": [], "episodes": []}',
    b'{"episodes": [{"id": "ep-01"}, {"episode_id": "ep-01"}]}',
])
def test_guard_refuses_corrupt_json_and_preserves_bytes(tmp_path, bad_bytes):
    library = tmp_path / 'lib'
    library.mkdir()
    listing = library / 'library.json'
    listing.write_bytes(bad_bytes)

    with pytest.raises(publisher.PublishError, match='现有资料库索引格式无效'):
        publisher.rebuild_library(library)
    assert listing.read_bytes() == bad_bytes

    job = _fixture_job(tmp_path / 'jobs', 'ep-01')
    with pytest.raises(publisher.PublishError, match='现有资料库索引格式无效'):
        publisher.publish_job(job, library)
    assert listing.read_bytes() == bad_bytes

    with pytest.raises(publisher.PublishError, match='缺少现有资料库索引基线'):
        publisher._write_snapshot(listing, {}, {'ep-01': _ep('ep-01')}, old_index=None)
    assert listing.read_bytes() == bad_bytes

    inputs = tmp_path / 'inputs'
    bound = _fake_pending_bound(inputs)
    with pytest.raises(pending.IntakeError, match='现有资料库索引格式无效'):
        pending.package(inputs, library, bound)
    assert listing.read_bytes() == bad_bytes


def test_guard_refuses_unreadable_index_oserror(monkeypatch, tmp_path):
    library = tmp_path / 'lib'
    before = _seed_library(library, ['ep-01'])
    listing = library / 'library.json'

    def deny(_path):
        raise PermissionError('denied')

    monkeypatch.setattr(publisher, '_read_library_bytes', deny)
    with pytest.raises(publisher.PublishError, match='无法读取现有资料库索引'):
        publisher.rebuild_library(library)
    assert listing.read_bytes() == before


def test_guard_refuses_symlink_or_directory_index(tmp_path):
    lib_sym = tmp_path / 'lib_sym'
    lib_sym.mkdir()
    target = tmp_path / 'outside.json'
    target.write_bytes(_encode({'episodes': []}))
    (lib_sym / 'library.json').symlink_to(target)

    with pytest.raises(publisher.PublishError, match='现有资料库索引格式无效'):
        publisher.rebuild_library(lib_sym)
    assert (lib_sym / 'library.json').is_symlink()

    lib_dir = tmp_path / 'lib_dir'
    lib_dir.mkdir()
    (lib_dir / 'library.json').mkdir()
    with pytest.raises(publisher.PublishError, match='现有资料库索引格式无效'):
        publisher.rebuild_library(lib_dir)
    assert (lib_dir / 'library.json').is_dir()


def test_guard_refuses_missing_index_when_episodes_exist(tmp_path):
    library = tmp_path / 'lib'
    ep_dir = library / 'episodes' / 'ep-01'
    ep_dir.mkdir(parents=True)
    (ep_dir / 'episode.json').write_bytes(_encode(_ep('ep-01')))
    listing = library / 'library.json'

    with pytest.raises(publisher.PublishError, match='未核实为空'):
        publisher.rebuild_library(library)
    assert not listing.exists()

    job = _fixture_job(tmp_path / 'jobs', 'ep-02')
    with pytest.raises(publisher.PublishError, match='未核实为空'):
        publisher.publish_job(job, library)
    assert not listing.exists()

    inputs = tmp_path / 'inputs'
    bound = _fake_pending_bound(inputs)
    with pytest.raises(pending.IntakeError, match='未核实为空'):
        pending.package(inputs, library, bound)
    assert not listing.exists()


def test_guard_refuses_missing_index_when_icloud_placeholder_exists(tmp_path):
    library = tmp_path / 'lib'
    library.mkdir()
    (library / '.library.json.icloud').write_bytes(b'placeholder')

    with pytest.raises(publisher.PublishError, match='未核实为空'):
        publisher.rebuild_library(library)
    assert not (library / 'library.json').exists()

    evicted_parent = tmp_path / 'parent'
    evicted_parent.mkdir()
    (evicted_parent / '.evicted-lib.icloud').write_bytes(b'placeholder')
    evicted_lib = evicted_parent / 'evicted-lib'
    with pytest.raises(publisher.PublishError, match='未核实为空'):
        publisher.rebuild_library(evicted_lib)
    assert not evicted_lib.exists()


def test_guard_refuses_write_without_verified_baseline(tmp_path):
    library = tmp_path / 'lib'
    library.mkdir()
    listing = library / 'library.json'

    with pytest.raises(publisher.PublishError, match='缺少现有资料库索引基线'):
        publisher._write_snapshot(listing, {}, {'ep-01': _ep('ep-01')}, old_index=None)
    assert not listing.exists()

    unverified = guard.CatalogBaseline(old_index=None, prior={}, old_ids=frozenset(), verified_empty=False)
    with pytest.raises(guard.CatalogWriteGuardError) as exc:
        guard.verify_catalog_transition(unverified, _encode({'episodes': [_ep('ep-01')]}), id_pattern=publisher._JOB_ID)
    assert exc.value.code == 'catalog_baseline_missing'


def test_push_iphone_refuses_unreadable_or_corrupt_phone_index(tmp_path):
    from tests.test_studio_push_iphone_d3 import StubDevicectl, encoded

    library = tmp_path / 'lib'
    _seed_library(library, ['ep-01'])

    # 1) Missing index on phone + .library.json.icloud placeholder
    fake_icloud = StubDevicectl(None)
    fake_icloud.listings['phone-1', push.DEVICE_LIBRARY] = {'files': [{'name': '.library.json.icloud'}]}
    res1 = push.push_episodes(['ep-01'], library, runner=fake_icloud, backup_root=tmp_path / 'b1', atomic_commit=fake_icloud.commit)
    assert not res1['ok']
    assert not any(ev[0] == 'atomic' for ev in fake_icloud.events)

    # 2) Corrupt JSON on phone
    fake_corrupt = StubDevicectl(b'{corrupt')
    res2 = push.push_episodes(['ep-01'], library, runner=fake_corrupt, backup_root=tmp_path / 'b2', atomic_commit=fake_corrupt.commit)
    assert not res2['ok']
    assert not any(ev[0] == 'atomic' for ev in fake_corrupt.events)
    assert fake_corrupt.indexes['phone-1'] == b'{corrupt'

    # 3) Missing index on phone + episodes/ present (not empty)
    fake_nonempty = StubDevicectl(encoded({'episodes': [_ep('ep-01')]}))
    fake_nonempty.indexes['phone-1'] = None
    fake_nonempty.listings['phone-1', push.DEVICE_LIBRARY] = {'files': [{'name': 'episodes'}]}
    res3 = push.push_episodes(['ep-01'], library, runner=fake_nonempty, backup_root=tmp_path / 'b3', atomic_commit=None)
    assert not res3['ok'] and not res3.get('staged')


# ---------------------------------------------------------------------------
# B. 闸 1：新清单集数变少 / 丢旧集 → 拒绝且旧目录保持原集数
# ---------------------------------------------------------------------------


def test_guard_refuses_shrink_from_many_to_one(tmp_path):
    library = tmp_path / 'lib'
    ids = [f'ep-0{i}' for i in range(1, 6)]
    before = _seed_library(library, ids)
    listing = library / 'library.json'
    prior = json.loads(before)

    with pytest.raises(publisher.PublishError, match='集数.*少于'):
        publisher._write_snapshot(listing, prior, {'ep-01': _ep('ep-01')}, old_index=before)
    assert listing.read_bytes() == before
    assert len(json.loads(listing.read_bytes())['episodes']) == 5


def test_rebuild_refuses_shrink_when_episodes_partially_synced(tmp_path):
    library = tmp_path / 'lib'
    ids = [f'ep-0{i}' for i in range(1, 6)]
    before = _seed_library(library, ids)
    listing = library / 'library.json'
    # Remove 4 episode directories to simulate partial iCloud sync.
    for ident in ids[1:]:
        for p in (library / 'episodes' / ident).iterdir():
            p.unlink()
        (library / 'episodes' / ident).rmdir()

    with pytest.raises(publisher.PublishError, match='集数.*少于'):
        publisher.rebuild_library(library)
    assert listing.read_bytes() == before
    assert len(json.loads(listing.read_bytes())['episodes']) == 5


def test_guard_refuses_same_count_when_existing_episode_dropped(tmp_path):
    library = tmp_path / 'lib'
    before = _seed_library(library, ['ep-01', 'ep-02'])
    listing = library / 'library.json'
    prior = json.loads(before)

    with pytest.raises(publisher.PublishError, match='缺少现有节目'):
        publisher._write_snapshot(
            listing,
            prior,
            {'ep-01': _ep('ep-01'), 'ep-03': _ep('ep-03')},
            old_index=before,
        )
    assert listing.read_bytes() == before


def test_guard_refuses_duplicate_ids_masquerading_as_count(tmp_path):
    library = tmp_path / 'lib'
    before = _seed_library(library, ['ep-01', 'ep-02'])
    baseline = guard.load_local_catalog_baseline(library, id_pattern=publisher._JOB_ID)
    forged = _encode({'episodes': [{'id': 'ep-01'}, {'episode_id': 'ep-01'}]})

    with pytest.raises(guard.CatalogWriteGuardError) as exc:
        guard.verify_catalog_transition(baseline, forged, id_pattern=publisher._JOB_ID)
    assert exc.value.code == 'catalog_candidate_invalid'


def test_push_iphone_commit_refuses_shrinking_merged_catalog(monkeypatch, tmp_path):
    from tests.test_studio_push_iphone_d3 import StubDevicectl, encoded

    library = tmp_path / 'lib'
    ids = [f'ep-0{i}' for i in range(1, 6)]
    _seed_library(library, ids)
    # Sidecars plus on-device copies so the shrink fuse is reached
    # after asset verification, not short-circuited by a missing file.
    for ident in ids:
        ep_dir = library / 'episodes' / ident
        for name in publisher.RENDER_FILES:
            (ep_dir / name).write_bytes(b'{}')

    phone_before = encoded({'episodes': [_ep(i) for i in ids]})
    fake = StubDevicectl(phone_before)
    for ident in ids:
        ep_dir = library / 'episodes' / ident
        for path in ep_dir.iterdir():
            fake.assets['phone-1', f'{push.DEVICE_LIBRARY}/episodes/{ident}/{path.name}'] = path.read_bytes()
    monkeypatch.setattr(push, '_merged_index', lambda *_a, **_k: encoded({'episodes': [_ep('ep-01')]}))

    res = push.push_episodes(['ep-01'], library, runner=fake, backup_root=tmp_path / 'b', atomic_commit=fake.commit)
    assert not res['ok']
    assert '少于' in res['reason']
    assert not any(ev[0] == 'atomic' for ev in fake.events)
    assert fake.indexes['phone-1'] == phone_before


# ---------------------------------------------------------------------------
# C. 合法写入必须通过
# ---------------------------------------------------------------------------


def test_verified_empty_library_allows_first_write_0_to_n(tmp_path):
    library = tmp_path / 'lib-empty'
    library.mkdir()
    (library / '.DS_Store').write_bytes(b'finder')

    job = _fixture_job(tmp_path / 'jobs', 'ep-01')
    res = publisher.publish_job(job, library)
    assert res['id'] == 'ep-01'
    listed = json.loads((library / 'library.json').read_bytes())
    assert [row['id'] for row in listed['episodes']] == ['ep-01']

    # Direct _write_snapshot 0 -> N on verified empty library
    library2 = tmp_path / 'lib-empty-n'
    library2.mkdir()
    baseline2 = guard.load_local_catalog_baseline(library2, id_pattern=publisher._JOB_ID)
    assert baseline2.verified_empty is True
    publisher._write_snapshot(
        library2 / 'library.json',
        {},
        {'ep-01': _ep('ep-01'), 'ep-02': _ep('ep-02')},
        old_index=None,
        baseline=baseline2,
    )
    listed2 = json.loads((library2 / 'library.json').read_bytes())
    assert {row['id'] for row in listed2['episodes']} == {'ep-01', 'ep-02'}

    # Remote verified empty 0 -> 1 push
    from tests.test_studio_push_iphone_d3 import StubDevicectl
    for name in publisher.RENDER_FILES:
        (library / 'episodes' / 'ep-01' / name).write_bytes(b'{}')
    fake = StubDevicectl(None)
    fake.listings['phone-1', push.DEVICE_LIBRARY] = {'files': []}
    push_res = push.push_episodes(['ep-01'], library, runner=fake, backup_root=tmp_path / 'bp', atomic_commit=fake.commit)
    assert push_res['ok']
    assert [row['id'] for row in json.loads(fake.indexes['phone-1'])['episodes']] == ['ep-01']


def test_same_id_revision_preserves_count_and_passes(tmp_path):
    library = tmp_path / 'lib'
    _seed_library(library, ['ep-01', 'ep-02', 'ep-03'])

    job = _fixture_job(tmp_path / 'jobs', 'ep-02', timestamp=5_000, title='修订版标题')
    (library / 'episodes' / 'ep-02' / 'output.mp3').write_bytes((job / 'output.mp3').read_bytes())
    (library / 'episodes' / 'ep-02' / 'transcript_zh.json').write_bytes((job / 'transcript_zh.json').read_bytes())
    publisher.publish_job(job, library)

    listed = json.loads((library / 'library.json').read_bytes())
    by_id = {row['id']: row for row in listed['episodes']}
    assert set(by_id) == {'ep-01', 'ep-02', 'ep-03'}
    assert by_id['ep-02']['title'] == '修订版标题'


def test_append_episode_increases_count_and_passes(monkeypatch, tmp_path):
    library = tmp_path / 'lib'
    _seed_library(library, ['ep-01', 'ep-02', 'ep-03'])

    job = _fixture_job(tmp_path / 'jobs', 'ep-04', timestamp=4_000)
    publisher.publish_job(job, library)
    listed = json.loads((library / 'library.json').read_bytes())
    assert len(listed['episodes']) == 4

    inputs = tmp_path / 'inputs'
    bound = _fake_pending_bound(inputs)

    def fake_media_cmd(_tool, args):
        Path(args[-1]).write_bytes(b'fake-mp3')
        return ''

    monkeypatch.setattr(pending, 'media_command', fake_media_cmd)
    monkeypatch.setattr(
        pending,
        'media_info',
        lambda _p, codec: {
            'codec': codec,
            'container_duration_s': 10.0,
            'decoded_timeline_duration_s': 10.0,
            'decoded_duration_s': 10.0,
            'sample_rate_hz': 44100,
            'channels': 1,
            'decoded_gap_count': 0,
            'decoded_gap_samples': 0,
            'decoded_samples_per_channel': 441000,
        },
    )
    monkeypatch.setattr(pending, 'validate_coverage', lambda *_a, **_k: None)

    result, _dest = pending.package(inputs, library, bound)
    assert result == 'created'
    listed_after = json.loads((library / 'library.json').read_bytes())
    assert len(listed_after['episodes']) == 5
    assert pending.EPISODE_ID in {row['id'] for row in listed_after['episodes']}


# ---------------------------------------------------------------------------
# D. TOCTOU、静态安全红线与快照加固
# ---------------------------------------------------------------------------


def test_toctou_index_changed_between_read_and_write_refused(tmp_path):
    # 1) Non-empty CAS drift
    library = tmp_path / 'lib1'
    before = _seed_library(library, ['ep-01'])
    baseline = guard.load_local_catalog_baseline(library, id_pattern=publisher._JOB_ID)
    drifted = _encode({'episodes': [_ep('ep-01'), _ep('ep-99')]})
    (library / 'library.json').write_bytes(drifted)

    with pytest.raises(guard.CatalogWriteGuardError) as exc:
        guard.atomic_write_guarded_catalog(
            library / 'library.json',
            _encode({'episodes': [_ep('ep-01'), _ep('ep-02')]}),
            baseline=baseline,
            id_pattern=publisher._JOB_ID,
        )
    assert exc.value.code == 'catalog_baseline_changed'
    assert (library / 'library.json').read_bytes() == drifted

    # 2) Verified empty library suddenly gains unknown episode directory
    library2 = tmp_path / 'lib2'
    library2.mkdir()
    empty_baseline = guard.load_local_catalog_baseline(library2, id_pattern=publisher._JOB_ID)
    (library2 / 'episodes' / 'ep-999').mkdir(parents=True)
    with pytest.raises(guard.CatalogWriteGuardError) as exc2:
        guard.atomic_write_guarded_catalog(
            library2 / 'library.json',
            _encode({'episodes': [_ep('ep-01')]}),
            baseline=empty_baseline,
            id_pattern=publisher._JOB_ID,
        )
    assert exc2.value.code == 'catalog_baseline_changed'
    assert not (library2 / 'library.json').exists()


def test_no_remove_existing_content_flag_in_push_scripts():
    scripts_dir = Path(__file__).resolve().parents[1] / 'scripts'
    for name in ('studio_push_iphone.py', 'push_staging_adapter.py', 'library_write_guard.py'):
        text = (scripts_dir / name).read_text(encoding='utf-8')
        assert '--remove-existing-content' not in text


def test_snapshot_restore_refuses_corrupt_or_mismatched_library_json(tmp_path):
    library = tmp_path / 'lib'
    before = _seed_library(library, ['ep-01', 'ep-02'])
    snap_dir = tmp_path / 'snap'
    snapshot_mod.snapshot(snap_dir, library=library, label='test')

    # Tamper snapshot library.json
    (snap_dir / 'roots' / 'library' / 'library.json').write_bytes(b'{tampered')
    with pytest.raises(snapshot_mod.SnapshotError, match='校验和不匹配'):
        snapshot_mod.restore(snap_dir, library=library)
    assert (library / 'library.json').read_bytes() == before
