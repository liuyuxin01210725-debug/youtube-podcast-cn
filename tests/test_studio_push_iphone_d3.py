import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_push_iphone as push


def encoded(index):
    return json.dumps(index, ensure_ascii=False).encode('utf-8')


class StubDevicectl:
    def __init__(self, index):
        self.indexes = {'phone-1': index}
        self.listings = {}
        self.assets = {}
        self.events = []
        self.fail_read_after_write = False
        self.fail_copy = None
        self.corrupt_copy = None
        self.fail_commit = None

    def __call__(self, command, timeout):
        def arg(name):
            return command[command.index(name) + 1]

        def reply(result):
            Path(arg('--json-output')).write_bytes(encoded({'result': result}))
            return 0, '', ''

        assert command[:2] == ['xcrun', 'devicectl']
        if command[2:4] == ['list', 'devices']:
            self.events.append(('devices',))
            return reply({'devices': [
                {'identifier': ident, 'hardwareProperties': {'platform': 'iOS', 'reality': 'physical'}}
                for ident in self.indexes
            ]})
        device = arg('--device')
        action = command[2:5]
        if action == ['device', 'info', 'apps']:
            assert arg('--bundle-id') == push.BUNDLE_ID
            self.events.append(('apps', device))
            return reply({'apps': [{'bundleIdentifier': push.BUNDLE_ID}]})

        assert arg('--domain-type') == 'appDataContainer'
        assert arg('--domain-identifier') == push.BUNDLE_ID
        if action == ['device', 'copy', 'from']:
            destination = Path(arg('--destination'))
            source = arg('--source')
            if source != f'{push.DEVICE_LIBRARY}/library.json':
                self.events.append(('read-asset', device, source))
                if (device, source) not in self.assets:
                    return 1, '', 'No such file or directory'
                destination.write_bytes(self.assets[device, source])
                return 0, '', ''
            self.events.append(('from', device, destination))
            data = self.indexes[device]
            if data is None:
                return 1, '', 'No such file or directory'
            if data == 'success-without-file':
                return 0, '', ''
            destination.write_bytes(data)
            if self.fail_read_after_write:
                return 1, '', 'transfer interrupted'
            return 0, '', ''
        if action == ['device', 'info', 'files']:
            folder = arg('--subdirectory')
            assert '--no-recurse' in command and '--filter' not in command and '--search' not in command
            self.events.append(('files', device, folder))
            result = self.listings.get((device, folder))
            if result is None:
                return 1, '', 'No such file or directory'
            return reply(result)
        if action == ['device', 'copy', 'to']:
            target = arg('--destination')
            assert target.startswith((f'{push.DEVICE_LIBRARY}/', 'Documents/.push-staging/')), command
            payload = Path(arg('--source')).read_bytes()
            self.events.append(('to', device, target, payload))
            if target == self.fail_copy:
                self.assets[device, target] = payload[:1]
                return 1, '', 'transfer interrupted'
            self.assets[device, target] = b'corrupted transfer' if target == self.corrupt_copy else payload
            return 0, '', ''
        pytest.fail(f'unexpected devicectl invocation: {command}')

    def commit(self, device, merged, snapshot):
        assert self.indexes[device] == snapshot
        self.events.append(('atomic', device, merged, snapshot))
        if self.fail_commit:
            return self.fail_commit
        self.indexes[device] = merged
        return None


def record(number):
    return {'id': f'ep-{number:02d}', 'title': f'节目 {number}',
            'created_at': f'2026-11-19T{number // 60:02d}:{number % 60:02d}:00Z', 'file': push.AUDIO_NAME}


SIDECARS = ('document.json', 'source-identity.json', 'render-identity.json')
LEGACY_FILES = ('output.mp3', *SIDECARS, 'episode.json')
LEGACY_TRANSFERS = ['to'] * len(LEGACY_FILES)


def revision_manifest(files):
    hashes = {name: hashlib.sha256(files[name]).hexdigest() for name in sorted(files)}
    digest = hashlib.sha256((json.dumps(hashes, indent=2) + '\n').encode()).hexdigest()
    return hashes, digest


@pytest.fixture
def revisions(tmp_path):
    library = tmp_path / 'library'
    first = record(40)
    ident = first['id']
    legacy = library / 'episodes' / ident
    before = encoded({'episodes': [first]})
    old = {'output.mp3': b'v1 audio', 'episode.json': encoded(first),
           **{name: encoded({'version': 1, 'file': name}) for name in SIDECARS}}
    row = {**first, 'title': '新版', 'artwork': 'cover.png', 'artwork_kind': 'provided'}
    files = {'output.mp3': b'v2 audio', 'episode.json': encoded(row),
             'cover.png': b'v2 cover', 'extract.json': b'v2 extract',
             **{name: encoded({'version': 2, 'file': name}) for name in SIDECARS}}
    hashes, digest = revision_manifest(files)
    path = f'revisions/{ident}/{digest}'
    row['media_revision'] = {'path': path, 'sha256': digest, 'files': hashes,
                             'previous_path': f'episodes/{ident}', 'resume_policy': 'reset_unmapped'}
    current = library / path
    for folder, assets in ((legacy, old), (current, files)):
        folder.mkdir(parents=True)
        for name, data in assets.items():
            (folder / name).write_bytes(data)
    (library / 'library.json').write_bytes(encoded({'episodes': [row]}))
    fake = StubDevicectl(before)
    fake.assets = {('phone-1', f'{push.DEVICE_LIBRARY}/episodes/{ident}/{name}'): data
                   for name, data in old.items()}
    return library, row, files, fake


@pytest.mark.parametrize('request_kind', ['legacy-selector', 'revision-dir', 'hook', 'cli-id', 'cli-all'])
def test_current_revision_uses_snapshot_path_and_every_declared_file(tmp_path, revisions, request_kind, monkeypatch):
    library, row, files, fake = revisions
    revision = row['media_revision']
    old_assets = fake.assets.copy()
    options = dict(runner=fake, backup_root=tmp_path / 'backups', atomic_commit=fake.commit)
    if request_kind == 'hook':
        monkeypatch.setenv(push.ENV_SWITCH, '1')
        result = push.push_after_publish({**row, 'library': str(library),
                                         'episode_dir': str(library / revision['path'])},
                                        announce=None, **options)
    elif request_kind.startswith('cli'):
        target = ['--all'] if request_kind == 'cli-all' else ['--episode-id', row['id']]
        if request_kind == 'cli-all':
            # A revision-only library must not depend on the legacy directory.
            (library / 'episodes').rename(tmp_path / 'legacy-on-mac')
        result = {'ok': push.main([*target, '--library', str(library)], **options) == 0}
    else:
        folder = revision['previous_path'] if request_kind == 'legacy-selector' else revision['path']
        result = push.push_episodes([library / folder], library, **options)
    assert result['ok'], result
    assert json.loads(fake.indexes['phone-1'])['episodes'] == [row]
    assert {target: data for _, _, target, data in writes(fake)[:-1]} == {
        f'{push.DEVICE_LIBRARY}/{revision["path"]}/{name}': data for name, data in files.items()
    }
    assert writes(fake)[-1][0] == 'atomic'
    assert {key: fake.assets[key] for key in old_assets} == old_assets


def test_catalog_change_during_transfer_does_not_mix_revision_rows_or_bytes(tmp_path, revisions):
    library, row, files, fake = revisions
    path = row['media_revision']['path']

    def change_library(command, timeout):
        if command[2:4] == ['list', 'devices']:
            (library / 'library.json').write_bytes(encoded({'episodes': [record(40)]}))
            (library / path / 'output.mp3').write_bytes(b'changed after verification')
        return fake(command, timeout)

    result = push.push_episodes([row['id']], library, runner=change_library,
                                backup_root=tmp_path / 'backups', atomic_commit=fake.commit)
    assert result['ok']
    assert json.loads(fake.indexes['phone-1'])['episodes'] == [row]
    assert fake.assets['phone-1', f'{push.DEVICE_LIBRARY}/{path}/output.mp3'] == files['output.mp3']


def test_retry_verifies_already_current_media_without_overwriting_it(tmp_path, revisions):
    library, row, _, fake = revisions
    revision = row['media_revision']
    assert push_delta(tmp_path, (library, {}), fake, row['id'])['ok']
    before = fake.indexes['phone-1']
    assets = fake.assets.copy()
    fake.events.clear()
    assert push_delta(tmp_path, (library, {}), fake, row['id'])['ok']
    assert [event[0] for event in writes(fake)] == ['atomic']
    assert {event[2] for event in fake.events if event[0] == 'read-asset'} == {
        f'{push.DEVICE_LIBRARY}/{revision["path"]}/{name}' for name in revision['files']
    }
    assert fake.assets == assets
    assert json.loads(fake.indexes['phone-1'])['episodes'] == json.loads(before)['episodes']


@pytest.mark.parametrize('fault', ['missing-sidecar', 'wrong-hash', 'wrong-manifest', 'escape',
                                    'linked-directory', 'linked-file', 'interrupted', 'corrupt-copy', 'ready-only'])
def test_bad_revision_never_replaces_old_index_or_media(tmp_path, revisions, fault):
    library, row, files, fake = revisions
    revision = row['media_revision']
    current = library / revision['path']
    sidecar = current / 'document.json'
    before = fake.indexes['phone-1']
    old = fake.assets.copy()
    if fault == 'missing-sidecar':
        sidecar.unlink()
    elif fault == 'wrong-hash':
        (current / 'extract.json').write_bytes(b'corrupted optional resource')
    elif fault == 'wrong-manifest':
        changed = b'changed bytes with correctly recomputed individual hash'
        (current / 'extract.json').write_bytes(changed)
        revision['files']['extract.json'] = hashlib.sha256(changed).hexdigest()
    elif fault == 'escape':
        revision['path'] = '../outside'
    elif fault == 'linked-directory':
        outside = tmp_path / 'outside'
        current.rename(outside)
        current.symlink_to(outside, target_is_directory=True)
    elif fault == 'linked-file':
        outside = tmp_path / 'outside'
        sidecar.rename(outside)
        sidecar.symlink_to(outside)
    elif fault == 'interrupted':
        fake.fail_copy = f'{push.DEVICE_LIBRARY}/{revision["path"]}/document.json'
    elif fault == 'corrupt-copy':
        fake.corrupt_copy = f'{push.DEVICE_LIBRARY}/{revision["path"]}/document.json'

    def ready_only(device, merged, snapshot):
        assert snapshot == fake.indexes[device]
        fake.events.append(('ready', device, merged))
        return None  # A buggy adapter reports acceptance before committing.

    (library / 'library.json').write_bytes(encoded({'episodes': [row]}))
    commit = ready_only if fault == 'ready-only' else fake.commit
    result = push_delta(tmp_path, (library, {}), fake, row['id'], atomic_commit=commit)
    assert not result['ok']
    assert fake.indexes['phone-1'] == before
    assert {key: fake.assets[key] for key in old} == old
    if fault == 'ready-only':
        assert 'library.json' in result['reason']
        assert any(event[0] == 'ready' for event in fake.events)
    else:
        assert all(event[0] != 'atomic' for event in fake.events)
    if fault == 'interrupted':
        assert 'transfer interrupted' in result['reason']
        assert fake.assets['phone-1', fake.fail_copy] != files['document.json']
        fake.fail_copy = None
        assert push_delta(tmp_path, (library, {}), fake, row['id'])['ok']
        assert json.loads(fake.indexes['phone-1'])['episodes'] == [row]
        assert all(fake.assets['phone-1', f'{push.DEVICE_LIBRARY}/{revision["path"]}/{name}'] == data
                   for name, data in files.items())


@pytest.fixture(autouse=True)
def device_guard(monkeypatch):
    monkeypatch.delenv(push.ENV_SWITCH, raising=False)
    monkeypatch.setattr(push, 'devicectl_available', lambda: True)

    def no_subprocess(*args, **kwargs):
        pytest.fail('device subprocess must be stubbed')

    monkeypatch.setattr(push.subprocess, 'run', no_subprocess)


@pytest.fixture
def staging(tmp_path):
    library = tmp_path / 'staging'
    entries = {f'ep-{number:02d}': record(number) for number in range(36, 62)}
    entries['ep-37'].update(created_at='2026-11-20T00:00:00+00:00', title='更新标题',
                            annotations={'review': {'tags': ['新版'], 'editor': 'staging'}, 'fresh': True})
    entries['ep-40'].update(created_at='2026-11-22T00:00:00+02:00')
    entries['ep-41'].update(created_at='2026-11-21T23:00:00Z')
    entries['ep-41']['source_job_id'] = entries['ep-41'].pop('id')
    for ident, item in entries.items():
        episode = library / 'episodes' / ident
        episode.mkdir(parents=True)
        (episode / push.AUDIO_NAME).write_bytes(ident.encode())
        (episode / 'episode.json').write_bytes(encoded(item))
        for name in SIDECARS:
            (episode / name).write_bytes(encoded({'id': ident, 'file': name}))
    (library / 'library.json').write_bytes(encoded({'episodes': list(entries.values())}))
    return library, entries


def push_delta(tmp_path, staging, fake, *requested, **options):
    library, _ = staging
    options.setdefault('backup_root', tmp_path / 'backups')
    options.setdefault('atomic_commit', fake.commit)
    return push.push_episodes([library / 'episodes' / name for name in requested],
                              library, runner=fake, **options)


def writes(fake):
    return [event for event in fake.events if event[0] in {'to', 'atomic'}]


def test_merge_phone_40_with_requested_delta_from_staging_26(tmp_path, staging):
    library, entries = staging
    phone_records = [dict(record(number), custom={'track': [number, {'saved': False}]}) for number in range(40)]
    refreshed = phone_records[37]
    refreshed['episode_id'] = refreshed.pop('id')
    refreshed.update(annotations={'review': {'tags': ['旧版'], 'verified': True}, 'local': {'note': None}},
                     user={'played': True, 'marker': {'position': 42}})
    phone_records[39].update(episode_id='ep-40', source_job_id='ep-41')
    phone = {'updated_at': '2026-11-19T03:00:00Z', 'user_data': {'session': {'kept': [1, None]}},
             'episodes': phone_records}
    phone_bytes = encoded(phone)
    fake = StubDevicectl(phone_bytes)

    episode = library / 'episodes' / 'ep-37'
    extras = {'cover.png': b'png', 'extract.live.json': b'live', 'extract.lkg.json': b'lkg',
              'extract.json': b'extract', 'transcript_zh.json': b'transcript', 'point-listen.json': b'points',
              'assets/配图.jpg': b'artwork'}
    (episode / 'assets').mkdir()
    for name, contents in extras.items():
        (episode / name).write_bytes(contents)
    existing = {**extras, **{name: (episode / name).read_bytes() for name in LEGACY_FILES}}
    fake.assets.update({('phone-1', f'{push.DEVICE_LIBRARY}/episodes/ep-37/{name}'): contents
                        for name, contents in existing.items()})
    (episode / '.partial').write_bytes(b'partial')
    (episode / 'scratch.tmp').write_bytes(b'temporary')
    outside = tmp_path / 'secret.txt'
    outside.write_bytes(b'secret')
    (episode / 'link.txt').symlink_to(outside)
    (episode / 'link-dir').symlink_to(tmp_path, target_is_directory=True)
    (episode / 'bad\\name').write_bytes(b'bad path')

    start = datetime.now(timezone.utc).replace(microsecond=0)
    result = push_delta(tmp_path, staging, fake, 'ep-37', 'ep-40', 'ep-41')

    assert result['ok']
    assert [event[0] for event in fake.events[:3]] == ['devices', 'apps', 'from']
    assert [event[0] for event in fake.events[-2:]] == ['atomic', 'from']
    backup = Path(result['devices'][0]['backup'])
    assert backup.read_bytes() == phone_bytes
    assert backup == fake.events[2][2]
    assert re.fullmatch(r'\d{8}T\d{6}\.\d{6}Z-.+', backup.parent.name)
    assert backup.stat().st_mode & 0o777 == 0o600

    assert len(phone_records) == 40 and len(entries) == 26
    merged = json.loads(fake.indexes['phone-1'])
    assert start <= datetime.fromisoformat(merged['updated_at']) <= datetime.now(timezone.utc)
    assert merged['user_data'] == phone['user_data']
    merged_ids = [row.get('id') or row.get('episode_id') or row.get('source_job_id') for row in merged['episodes']]
    expected_ids = {f'ep-{number:02d}' for number in (*range(40), 40, 41)}
    assert len(merged_ids) == len(expected_ids) == 42
    assert set(merged_ids) == expected_ids
    assert merged_ids[:3] == ['ep-41', 'ep-40', 'ep-37']
    merged_by_id = dict(zip(merged_ids, merged['episodes']))
    for number, row in enumerate(phone_records):
        if number != 37:
            assert merged_by_id[f'ep-{number:02d}'] == row
    assert merged_by_id['ep-40'] == entries['ep-40']
    assert merged_by_id['ep-41'] == entries['ep-41']
    assert merged_by_id['ep-37'] == {
        **refreshed, **entries['ep-37'],
        'annotations': {'review': {'tags': ['新版'], 'verified': True, 'editor': 'staging'},
                        'local': {'note': None}, 'fresh': True},
    }

    copies = [(target.removeprefix(f'{push.DEVICE_LIBRARY}/'), payload)
              for _, _, target, payload in writes(fake)[:-1]]
    expected = {}
    for ident in ('ep-40', 'ep-41'):
        for name in LEGACY_FILES:
            expected[f'episodes/{ident}/{name}'] = (library / 'episodes' / ident / name).read_bytes()
    assert len(copies) == len(expected)
    assert dict(copies) == expected
    assert copies[0][0] == 'episodes/ep-40/output.mp3'
    assert copies[-1][0] == 'episodes/ep-41/episode.json'
    assert {event[2] for event in fake.events if event[0] == 'read-asset' and '/ep-37/' in event[2]} == {
        f'{push.DEVICE_LIBRARY}/episodes/ep-37/{name}' for name in existing
    }


@pytest.mark.parametrize(('index', 'error'), [
    pytest.param(None, '读取手机索引失败', id='copy-from-failure'),
    pytest.param('success-without-file', '未取得普通备份文件', id='missing-local-backup'),
    pytest.param(b'{', 'Expecting', id='corrupt'),
    pytest.param(b'\xff', 'utf-8', id='invalid-utf8'),
    pytest.param(b'{"episodes": {}}', 'episodes 数组', id='wrong-index-schema'),
    pytest.param(b'{"episodes": [{"id": "ep-01", "guests": [5]}]}', 'guests', id='wrong-entry-schema'),
    pytest.param(b'{"episodes": [{"id": {}, "episode_id": "ep-01"}]}', '有效 ID', id='invalid-priority-id'),
    pytest.param(b'{"episodes": [{"id": "ep-01"}, {"episode_id": "ep-01"}]}', 'ID 重复', id='duplicate-id'),
    pytest.param(b'{"episodes": [{"id": "ep-01", "data": {"tag": 1, "tag": 2}}]}', '字段重复', id='duplicate-json-key'),
])
def test_unusable_phone_index_closes_without_writes(tmp_path, staging, index, error):
    fake = StubDevicectl(index)
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert not result['ok']
    assert error in result['reason']
    assert writes(fake) == []
    if isinstance(index, bytes):
        assert Path(result['devices'][0]['backup']).read_bytes() == index


def test_effective_key_change_after_overlay_is_rejected(tmp_path, staging):
    library, entries = staging
    entries['ep-37'].update(id=None, source_job_id='ep-37')
    (library / 'library.json').write_bytes(encoded({'episodes': list(entries.values())}))
    fake = StubDevicectl(encoded({'episodes': [{'id': 'ep-37', 'episode_id': 'ep-40'}]}))
    result = push_delta(tmp_path, staging, fake, 'ep-37')
    assert not result['ok']
    assert '合并后的节目 ID 冲突' in result['reason']
    assert writes(fake) == []


def test_interrupted_download_with_local_bytes_is_never_an_empty_library(tmp_path, staging):
    fake = StubDevicectl(b'incomplete')
    fake.fail_read_after_write = True
    fake.listings['phone-1', push.DEVICE_LIBRARY] = {'files': []}
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert not result['ok']
    assert 'transfer interrupted' in result['reason']
    assert Path(result['devices'][0]['backup']).read_bytes() == b'incomplete'
    assert [event[0] for event in fake.events] == ['devices', 'apps', 'from']


def test_staging_conflicting_ids_fail_even_outside_requested_delta(tmp_path, staging):
    library, entries = staging
    records = [*entries.values(), {'episode_id': 'ep-36', 'title': '冲突'}]
    (library / 'library.json').write_bytes(encoded({'episodes': records}))
    fake = StubDevicectl(None)
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert not result['ok']
    assert '节目 ID 重复：ep-36' in result['reason']
    assert fake.events == []


def test_backup_failure_prevents_copy_and_commit(tmp_path, staging):
    fake = StubDevicectl(encoded({'episodes': []}))
    unusable_root = tmp_path / 'unusable'
    unusable_root.write_text('file rather than a directory')
    result = push_delta(tmp_path, staging, fake, 'ep-40', backup_root=unusable_root)
    assert not result['ok']
    assert [event[0] for event in fake.events] == ['devices', 'apps']


def test_mid_transfer_failure_never_calls_atomic_commit(tmp_path, staging):
    before = encoded({'episodes': [record(1)]})
    fake = StubDevicectl(before)
    fake.fail_copy = f'{push.DEVICE_LIBRARY}/episodes/ep-41/episode.json'
    result = push_delta(tmp_path, staging, fake, 'ep-40', 'ep-41')
    assert not result['ok']
    assert 'transfer interrupted' in result['reason']
    assert [event[0] for event in writes(fake)] == LEGACY_TRANSFERS * 2
    assert fake.indexes['phone-1'] == before


def test_commit_error_is_reported_as_failure(tmp_path, staging):
    before = encoded({'episodes': []})
    fake = StubDevicectl(before)
    fake.fail_commit = 'snapshot changed'
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert not result['ok']
    assert 'snapshot changed' in result['reason']
    assert [event[0] for event in writes(fake)] == [*LEGACY_TRANSFERS, 'atomic']
    assert fake.indexes['phone-1'] == before


@pytest.mark.parametrize('case', ['equivalent-number', 'changed-title', 'boolean-as-number', 'duplicate-key'])
def test_commit_readback_requires_the_complete_catalog(tmp_path, staging, case):
    fake = StubDevicectl(encoded({'episodes': [], 'enabled': True, 'scale': 1.0}))

    def commit(device, merged, snapshot):
        row = json.loads(merged)
        if case == 'changed-title':
            row['episodes'][0]['title'] = 'different title'
        elif case == 'boolean-as-number':
            row['enabled'] = 1
        elif case == 'equivalent-number':
            row['scale'] = 1
        landed = encoded(row)
        if case == 'duplicate-key':
            landed = b'{"enabled": false, ' + landed[1:]
        return fake.commit(device, landed, snapshot)

    result = push_delta(tmp_path, staging, fake, 'ep-40', atomic_commit=commit)
    assert result['ok'] == (case == 'equivalent-number'), result
    if not result['ok']:
        assert 'library.json' in result['reason']


def test_default_staging_backs_up_and_does_not_write_the_phone_catalog(tmp_path, staging):
    before = encoded({'episodes': [record(1)]})
    fake = StubDevicectl(before)
    result = push_delta(tmp_path, staging, fake, 'ep-40', atomic_commit=None)
    assert not result['ok']
    assert result['staged']
    assert Path(result['devices'][0]['backup']).read_bytes() == before
    assert fake.indexes['phone-1'] == before
    assert all(event[0] == 'to' and event[2].startswith('Documents/.push-staging/') for event in writes(fake))
    assert writes(fake)[-1][2].endswith('/ready')


def test_disabled_push_does_not_even_check_for_devicectl(monkeypatch, tmp_path, staging):
    monkeypatch.setenv(push.ENV_SWITCH, '0')
    monkeypatch.setattr(push, 'devicectl_available', lambda: pytest.fail('availability checked while disabled'))
    fake = StubDevicectl(None)
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert result['skipped'] and not result['ok']
    assert fake.events == []
    assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize(('folder', 'listing'), [
    (push.DEVICE_LIBRARY, {'files': []}),
    ('Documents', {'files': [{'relativePath': 'Documents/listen_notes.json'}]}),
])
def test_verified_first_empty_library_can_commit_requested_delta(tmp_path, staging, folder, listing):
    fake = StubDevicectl(None)
    fake.listings['phone-1', folder] = listing
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert result['ok']
    assert ('files', 'phone-1', folder) in fake.events
    assert fake.events[2][0] == 'from'
    assert [event[0] for event in writes(fake)] == [*LEGACY_TRANSFERS, 'atomic']
    assert writes(fake)[-1][3] is None
    assert json.loads(fake.indexes['phone-1'])['episodes'] == [staging[1]['ep-40']]
    assert 'backup' not in result['devices'][0]
    assert {target: contents for (_, target), contents in fake.assets.items()} == {
        f'{push.DEVICE_LIBRARY}/episodes/ep-40/{name}':
        (staging[0] / 'episodes' / 'ep-40' / name).read_bytes() for name in LEGACY_FILES
    }


def test_first_legacy_package_requires_sidecars(tmp_path, staging):
    library, _ = staging
    (library / 'episodes' / 'ep-40' / 'source-identity.json').unlink()
    fake = StubDevicectl(encoded({'episodes': []}))
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert not result['ok'] and 'source-identity.json' in result['reason']
    assert not fake.events


@pytest.mark.parametrize(('folder', 'listing'), [
    (push.DEVICE_LIBRARY, {'files': [{'relativePath': f'{push.DEVICE_LIBRARY}/episodes'}]}),
    (push.DEVICE_LIBRARY, {'files': [{'name': 'library.json'}]}),
    (push.DEVICE_LIBRARY, {'files': [{'unexpectedField': 'unknown'}]}),
    ('Documents', {'files': [{'name': push.LIBRARY_NAME}]}),
    ('Documents', {'unexpectedField': []}),
])
def test_error_text_and_unverified_emptiness_cannot_authorize_writes(tmp_path, staging, folder, listing):
    fake = StubDevicectl(None)
    fake.listings['phone-1', folder] = listing
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert not result['ok']
    assert '读取手机索引失败' in result['reason']
    assert writes(fake) == []


def test_every_device_uses_its_own_snapshot(tmp_path, staging):
    first = encoded({'episodes': [record(1)]})
    second = encoded({'episodes': [record(2)]})
    fake = StubDevicectl(first)
    fake.indexes['phone-2'] = second
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert all(device['ok'] for device in result['devices'])
    assert [Path(device['backup']).read_bytes() for device in result['devices']] == [first, second]
    assert [event[3] for event in writes(fake) if event[0] == 'atomic'] == [first, second]
    sequence = ['apps', 'from', *(['to', 'read-asset'] * len(LEGACY_FILES)), 'atomic', 'from']
    assert [event[0] for event in fake.events] == ['devices', *sequence, *sequence]


def test_requested_directory_outside_staging_or_symlink_is_rejected(tmp_path, staging):
    library, _ = staging
    outside = tmp_path / 'ep-40'
    outside.mkdir()
    (outside / push.AUDIO_NAME).write_bytes(b'outside')
    fake = StubDevicectl(None)
    result = push.push_episodes([outside], library, runner=fake, atomic_commit=fake.commit)
    assert not result['ok'] and not fake.events

    episode = library / 'episodes' / 'ep-40'
    episode.rename(library / 'episodes' / 'moved')
    episode.symlink_to(outside, target_is_directory=True)
    result = push_delta(tmp_path, staging, fake, 'ep-40')
    assert not result['ok'] and not fake.events


def _make_objects_revision_library(tmp_path: Path, entries: list[tuple[str, str, dict[str, bytes]]]):
    library = tmp_path / 'obj-lib'
    library.mkdir(parents=True, exist_ok=True)
    rows = []
    for ep_id, prev_path, extra_files in entries:
        base_files = {
            push.AUDIO_NAME: b'obj-audio-' + ep_id.encode(),
            'episode.json': encoded({'id': ep_id, 'title': ep_id}),
            'document.json': encoded({'segments': []}),
            'source-identity.json': encoded({}),
            'render-identity.json': encoded({}),
            **extra_files,
        }
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(base_files.items())}
        rev_sha = push.digest(push.json_bytes(hashes))
        pkg_dir = library / 'objects' / rev_sha
        pkg_dir.mkdir(parents=True, exist_ok=True)
        for name, raw in base_files.items():
            (pkg_dir / name).write_bytes(raw)
        rows.append({
            'id': ep_id,
            'title': ep_id,
            'created_at': '2026-11-26T00:00:00+00:00',
            'source_job_id': ep_id,
            'file': push.AUDIO_NAME,
            'media_revision': {
                'sha256': rev_sha,
                'path': f'objects/{rev_sha}',
                'previous_path': prev_path,
                'resume_policy': 'reset_unmapped',
                'files': hashes,
            },
        })
    (library / 'library.json').write_bytes(encoded({'schema': 'private-listen.library.v2', 'generation': 4, 'episodes': rows}))
    return library, rows


def test_objects_lookup_copy_to_app_and_sidecar_staging(tmp_path, monkeypatch):
    # 5a & 5d: single match with previous_path == "" and previous_path == "episodes/<id>", plus sidecars
    sidecars = {
        'transcript_zh.json': encoded({'segments': [{'id': 's1', 'zh': '译稿'}]}),
        'extract.json': encoded({'packet_id': 'p1', 'work_id': 'ep-obj-1'}),
        'extract.live.json': encoded({'packet_id': 'p2', 'work_id': 'ep-obj-1'}),
        'point-listen.json': encoded({'work_id': 'ep-obj-1'}),
    }
    library, rows = _make_objects_revision_library(
        tmp_path,
        [
            ('ep-obj-1', '', sidecars),
            ('ep-obj-2', 'episodes/ep-obj-2', {}),
        ],
    )
    fake = StubDevicectl(None)
    fake.listings['phone-1', push.DEVICE_LIBRARY] = {'files': []}
    monkeypatch.setenv(push.ENV_SWITCH, '1')
    rev1 = rows[0]['media_revision']
    rev2 = rows[1]['media_revision']
    res1 = push.push_episodes([library / rev1['path']], library, runner=fake, backup_root=tmp_path / 'b1')
    assert res1['staged'] and res1['episodes'] == ['ep-obj-1']
    for name in sidecars:
        assert ('phone-1', f"Documents/.push-staging/{res1['devices'][0]['transaction']}/{rev1['path']}/{name}") in fake.assets

    res2 = push.push_episodes([library / rev2['path']], library, runner=fake, backup_root=tmp_path / 'b2')
    assert res2['staged'] and res2['episodes'] == ['ep-obj-2']

    # 5a: 0 matches & >1 ambiguous matches fail-closed
    missing_sha = '0' * 64
    res_miss = push.push_episodes([library / 'objects' / missing_sha], library, runner=fake, backup_root=tmp_path / 'b3')
    assert not res_miss['ok'] and not res_miss.get('staged')

    dup_lib, dup_rows = _make_objects_revision_library(
        tmp_path / 'dup',
        [('dup-1', '', {}), ('dup-2', '', {})],
    )
    # Force dup-2 to share dup-1's objects/<sha>
    dup_catalog = json.loads((dup_lib / 'library.json').read_bytes())
    dup_catalog['episodes'][1]['media_revision'] = dup_catalog['episodes'][0]['media_revision']
    (dup_lib / 'library.json').write_bytes(encoded(dup_catalog))
    dup_sha = dup_rows[0]['media_revision']['sha256']
    res_ambig = push.push_episodes([dup_lib / 'objects' / dup_sha], dup_lib, runner=fake, backup_root=tmp_path / 'b4')
    assert not res_ambig['ok'] and '歧义' in res_ambig['reason']

    # 5b: push_after_publish passes id + expected_revisions to disambiguate & guard drift
    res_disambig = push.push_after_publish(
        {'id': 'dup-1', 'episode_dir': str(dup_lib / 'objects' / dup_sha), 'library': str(dup_lib)},
        runner=fake, announce=None, backup_root=tmp_path / 'b5',
    )
    assert res_disambig['staged']
    res_drift = push.push_after_publish(
        {'id': 'dup-1', 'episode_dir': str(dup_lib / 'objects' / ('1' * 64)), 'library': str(dup_lib)},
        runner=fake, announce=None, backup_root=tmp_path / 'b6',
    )
    assert not res_drift['ok'] and not res_drift.get('staged')

    # 5c: copy_to_app strict path checks
    src_file = tmp_path / 'asset.bin'
    src_file.write_bytes(b'x')
    ok_runner = lambda cmd, timeout: (0, '', '')
    valid_sha = 'a' * 64
    assert push.copy_to_app('dev', src_file, f'objects/{valid_sha}/transcript_zh.json', runner=ok_runner) is None
    assert push.copy_to_app('dev', src_file, f'objects/{valid_sha}/output.mp3', runner=ok_runner) is None
    for bad_dest in (
        'objects/not-a-sha/output.mp3',
        f'objects/{"A" * 64}/output.mp3',
        f'objects/{valid_sha}/sub/a.json',
        f'objects/{valid_sha}/.hidden',
        f'objects/{valid_sha}/a.tmp',
        'objects/../episodes/x/output.mp3',
    ):
        assert push.copy_to_app('dev', src_file, bad_dest, runner=ok_runner) == '目标不是安全的节目资源路径'
