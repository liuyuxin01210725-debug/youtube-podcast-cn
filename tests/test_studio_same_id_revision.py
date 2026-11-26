"""Same-ID publication with short, locally generated media and isolated catalogs."""

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_publish_icloud as publisher
import studio_push_iphone as push
from studio_recovery import fingerprint
from temporary_audio import sha256_file
from test_studio_push_iphone_d3 import StubDevicectl

LAUREN = '3658be8afa4f588a95849949ddf79cc6'


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def fixture_render(job, version):
    job.mkdir(parents=True, exist_ok=True)
    rate = 24000
    wav = job / 'synthetic.wav'
    frames = array('h', (int(4000 * math.sin(2 * math.pi * (300 + 100 * version) * n / rate))
                         for n in range(rate)))
    with wave.open(str(wav), 'wb') as sample:
        sample.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        sample.writeframes(frames.tobytes())
    output = job / 'output.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(wav), str(output)], check=True)
    source = {'kind': 'web', 'value': 'https://example.invalid/listening-fixture'}
    lock = job / 'source-identity.json'
    write(lock, {**source, 'sha256': sha256_file(wav), 'bytes': wav.stat().st_size, 'duration_ms': 1000})
    write(job / 'config.json', {'source': source, 'expected_speakers': 1})
    write(job / 'state.json', {'title': f'修订夹具第 {version} 版'})
    document = {'speakers': {'host': {'voice_slot': 'male_1'}}, 'references': {}, 'segments': [
        {'id': 'seg-000001', 'speaker_id': 'host', 'start_ms': 0, 'end_ms': 1000,
         'text': f'Synthetic fixture {version}', 'chinese': f'第 {version} 版译稿', 'ambiguous': False},
    ]}
    write(job / 'document.json', document)
    write(job / 'render-identity.json', {
        'schema_version': 1, 'sample_rate': rate, 'audio_sha256': sha256_file(output),
        'document_sha256': fingerprint(document), 'source_identity_sha256': sha256_file(lock),
        'timeline': [{'id': 'seg-000001', 'start_frame': 0, 'end_frame': rate}],
    })


@pytest.fixture(autouse=True)
def _stub_audio_preflight(monkeypatch):
    monkeypatch.setattr(publisher, '_require_audio_preflight', lambda *a, **k: None)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr('studio_artwork.attach_cover', lambda *args, **kwargs: None)
    library = tmp_path / 'library'
    job = tmp_path / 'jobs' / LAUREN
    fixture_render(job, 1)
    first = publisher.publish_job(job, library)
    legacy = Path(first['episode_dir'])
    assert legacy == library / 'objects' / first['media_revision']['sha256']
    assert first['media_revision']['previous_path'] == ''
    write(legacy / 'extract.json', {
        'packet_id': 'first', 'work_id': LAUREN, 'schema_id': 'ferment', 'mode': 'deep',
        'transcript_hash': 'a' * 64, 'status': 'ready',
        'methods': [{'id': 'm1', 'status': 'confirmed'}],
        'actions': [{'id': 'a1', 'kind': 'try_later', 'item_id': 'm1'}],
    })
    index = library / 'library.json'
    payload = json.loads(index.read_bytes())
    payload['settings'] = {'tag': 'keep'}
    payload['episodes'][0].update(show='original show', annotations={'reviewed': True}, resume_seconds=55)
    write(index, payload)
    old = {path.name: path.read_bytes() for path in legacy.iterdir()}
    fixture_render(job, 2)
    return job, library, legacy, index, old


def test_same_id_switches_only_catalog_and_retains_immutable_history(isolated):
    job, library, legacy, index, old = isolated
    result = publisher.publish_job(job, library)
    current = Path(result['episode_dir'])
    snapshot = json.loads(index.read_bytes())
    [row] = snapshot['episodes']
    revision = row['media_revision']
    assert row['id'] == LAUREN and snapshot['settings'] == {'tag': 'keep'}
    assert row['show'] == 'original show' and row['annotations'] == {'reviewed': True}
    assert revision['path'] == current.relative_to(library).as_posix()
    assert revision['previous_path'] == legacy.relative_to(library).as_posix()
    assert row['title'] == '修订夹具第 2 版'
    assert revision['resume_policy'] == 'reset_unmapped' and 'resume_seconds' not in row
    assert revision['files'] == {path.name: sha256_file(path) for path in current.iterdir()}
    for name in ('output.mp3', *publisher.RENDER_FILES):
        assert (current / name).read_bytes() == (job / name).read_bytes()
    assert (current / 'output.mp3').read_bytes() != old['output.mp3']
    assert (current / 'document.json').read_bytes() != old['document.json']
    assert {path.name: path.read_bytes() for path in legacy.iterdir()} == old
    ideas = json.loads((current / 'extract.json').read_bytes())
    assert ideas['status'] == 'stale'
    assert ideas['methods'] == [{'id': 'm1', 'status': 'confirmed'}]
    assert ideas['actions'] == [{'id': 'a1', 'kind': 'try_later', 'item_id': 'm1'}]
    assert publisher.rebuild_library(library)['episodes'] == [row]

    fixture_render(job, 3)
    immutable = {p.name: p.read_bytes() for p in current.iterdir()}
    third = publisher.publish_job(job, library)
    assert third['media_revision']['previous_path'] == revision['path']
    assert {p.name: p.read_bytes() for p in current.iterdir()} == immutable
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == old


def test_real_publisher_pushes_legacy_then_revision_with_catalog_readback(tmp_path, monkeypatch):
    monkeypatch.setattr('studio_artwork.attach_cover', lambda *args, **kwargs: None)
    monkeypatch.setattr(push, 'devicectl_available', lambda: True)
    monkeypatch.setenv(push.ENV_SWITCH, '1')
    library = tmp_path / 'library'
    job = tmp_path / 'jobs' / 'publisher-chain'
    phone = 'phone-1'
    fake = StubDevicectl(None)
    fake.listings[phone, push.DEVICE_LIBRARY] = {'files': []}

    def sync(published, commit):
        return push.push_after_publish(published, job, runner=fake, announce=None,
                                       backup_root=tmp_path / 'backups', atomic_commit=commit)

    fixture_render(job, 1)
    first = publisher.publish_job(job, library)
    [first_row] = json.loads((library / 'library.json').read_bytes())['episodes']
    legacy = Path(first['episode_dir'])
    assert legacy == library / 'objects' / first_row['media_revision']['sha256']
    assert first_row['media_revision']['previous_path'] == ''
    result = sync(first, fake.commit)
    assert result['ok'], result
    assert json.loads(fake.indexes[phone])['episodes'] == [first_row]
    old_media = fake.assets.copy()
    assert old_media == {(phone, f'{push.DEVICE_LIBRARY}/{first_row["media_revision"]["path"]}/{p.name}'): p.read_bytes()
                         for p in legacy.iterdir()}

    # Add device-local metadata. The App probe checks this also survives the commit.
    catalog = json.loads(fake.indexes[phone])
    local_fields = {'show': 'Fixture show', 'lens_ids': ['fixture']}
    catalog['episodes'][0].update(local_fields)
    before = fake.indexes[phone] = json.dumps(catalog).encode()

    fixture_render(job, 2)
    second = publisher.publish_job(job, library)
    [row] = json.loads((library / 'library.json').read_bytes())['episodes']
    revision = row['media_revision']
    current = Path(second['episode_dir'])
    assert revision == second['media_revision']
    assert current == library / revision['path'] == library / 'objects' / revision['sha256']
    new_files = {p.name: p.read_bytes() for p in current.iterdir()}
    hashes = {name: hashlib.sha256(new_files[name]).hexdigest() for name in sorted(new_files)}
    assert revision['files'] == hashes
    manifest = publisher.original_pending.json_bytes(hashes)
    assert manifest.endswith(b'\n') and hashlib.sha256(manifest).hexdigest() == revision['sha256']

    fake.events.clear()
    fake.fail_copy = f'{push.DEVICE_LIBRARY}/{revision["path"]}/document.json'
    failed_copy = sync(second, fake.commit)
    assert not failed_copy['ok'] and 'transfer interrupted' in failed_copy['reason']
    assert fake.indexes[phone] == before
    assert all(fake.assets[key] == data for key, data in old_media.items())
    assert all(event[0] != 'atomic' for event in fake.events)
    fake.fail_copy = None

    def commit(device, merged, expected):
        assert device == phone and expected == before == fake.indexes[phone]
        probe = os.environ.get('PRIVATE_LISTEN_SNAPSHOT_PROBE')
        if probe:
            # The existing macOS probe runs the App's actual local CAS and decodes
            # the pinned old audio handle after commit, in synthetic Documents.
            documents = tmp_path / 'device' / 'Documents'
            for (_, target), data in fake.assets.items():
                path = documents.parent / target
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            listing = documents / publisher.LIBRARY_NAME / 'library.json'
            listing.write_bytes(expected)
            (documents / 'incoming.json').write_bytes(merged)
            subprocess.run([probe, '--publisher-only', str(documents)], check=True)
            landed = listing.read_bytes()
        else:
            # Model the App re-encoding JSON when the native probe is unavailable.
            landed = json.dumps(json.loads(merged), sort_keys=True, separators=(',', ':')).encode()
        assert landed != merged
        return fake.commit(device, landed, expected)

    result = sync(second, commit)
    assert result['ok'], result
    assert json.loads(fake.indexes[phone])['episodes'] == [{**row, **local_fields}]
    assert fake.assets == {**old_media, **{
        (phone, f'{push.DEVICE_LIBRARY}/{revision["path"]}/{name}'): data
        for name, data in new_files.items()
    }}


def test_failure_after_staged_revision_leaves_old_snapshot_and_bytes(isolated, monkeypatch):
    job, library, legacy, index, old = isolated
    before = index.read_bytes()
    real_write = publisher._write_snapshot
    inspected = []

    def fail(listing, prior, records, **kwargs):
        assert listing == index and index.read_bytes() == before
        assert kwargs.get('old_index') == before and kwargs.get('v2_write') is True
        assert {p.name: p.read_bytes() for p in legacy.iterdir()} == old
        row = records[LAUREN]['media_revision']
        staged = library / row['path']
        assert staged.is_dir()
        assert (staged / 'output.mp3').read_bytes() == (job / 'output.mp3').read_bytes()
        assert (staged / 'document.json').read_bytes() == (job / 'document.json').read_bytes()
        inspected.append(staged)
        raise OSError('injected before snapshot replacement')

    with monkeypatch.context() as patch:
        patch.setattr(publisher, '_write_snapshot', fail)
        with pytest.raises(publisher.PublishError, match='injected before snapshot'):
            publisher.publish_job(job, library)
    assert len(inspected) == 1
    assert index.read_bytes() == before
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == old
    assert not list(library.glob('.render-*'))
    assert publisher.rebuild_library(library)['episodes'][0]['media_revision']['path'] == legacy.relative_to(library).as_posix()
    monkeypatch.setattr(publisher, '_write_snapshot', real_write)
    retried = publisher.publish_job(job, library)
    assert Path(retried['episode_dir']) == inspected[0]


@pytest.mark.parametrize('failure', ['broken', 'unreadable'])
def test_existing_catalog_is_fail_closed_before_revision_staging(isolated, monkeypatch, failure):
    job, library, legacy, index, old = isolated
    before_objects = sorted(p.name for p in (library / 'objects').iterdir())
    if failure == 'broken':
        index.write_bytes(b'{"episodes": false}')
    else:
        original_read = publisher._read_library_bytes

        def denied(path):
            if path == index:
                raise PermissionError('synthetic read refusal')
            return original_read(path)

        monkeypatch.setattr(publisher, '_read_library_bytes', denied)
    before = index.read_bytes()
    with pytest.raises(publisher.PublishError, match='现有资料库索引'):
        publisher.publish_job(job, library)
    assert index.read_bytes() == before
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == old
    assert sorted(p.name for p in (library / 'objects').iterdir()) == before_objects
    assert not (library / 'revisions').exists()
    assert not list(library.glob('.render-*'))


@pytest.mark.parametrize('corruption', ['audio', 'translation', 'timeline', 'source', 'overflow', 'pending'])
def test_mismatched_or_unreviewed_staged_media_cannot_replace_revision(isolated, corruption):
    job, library, legacy, index, old = isolated
    before = index.read_bytes()
    before_objects = sorted(p.name for p in (library / 'objects').iterdir())
    receipt_path = job / 'render-identity.json'
    receipt = json.loads(receipt_path.read_bytes())
    if corruption == 'audio':
        # Keep decodable media, but bind the receipt to different bytes.
        receipt['audio_sha256'] = sha256_file(legacy / 'output.mp3')
    elif corruption == 'timeline':
        receipt['timeline'][0]['start_frame'] = 1
    elif corruption == 'source':
        receipt['source_identity_sha256'] = sha256_file(legacy / 'source-identity.json')
    else:
        path = job / 'document.json'
        doc = json.loads(path.read_bytes())
        if corruption == 'overflow':
            doc['segments'][0]['end_ms'] = 1001
            receipt['document_sha256'] = fingerprint(doc)
        elif corruption == 'pending':
            doc['segments'][0]['ambiguous'] = True
            receipt['document_sha256'] = fingerprint(doc)
        else:
            doc['segments'][0]['chinese'] = '不配套的版本'
        write(path, doc)
    write(receipt_path, receipt)
    with pytest.raises(publisher.PublishError, match='无法核验|禁止发布'):
        publisher.publish_job(job, library)
    assert index.read_bytes() == before
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == old
    assert sorted(p.name for p in (library / 'objects').iterdir()) == before_objects
    assert not (library / 'revisions').exists()
