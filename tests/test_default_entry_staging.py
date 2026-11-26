"""Publish through the product hooks and consume by the native App store."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_studio_same_id_revision import fixture_render, publisher, push, write
from test_studio_push_iphone_d3 import encoded


class DocumentsTransport:
    """Replace only the devicectl process, backed by temporary App Documents."""

    def __init__(self, documents, run):
        self.documents = documents
        self.run = run
        self.copies = []
        self.interrupt = False

    def __call__(self, command, *args, **kwargs):
        if command[:2] != ['xcrun', 'devicectl']:
            return self.run(command, *args, **kwargs)

        def arg(name):
            return command[command.index(name) + 1]

        def device_path(relative):
            parts = Path(relative).parts
            assert parts[0] == 'Documents' and '..' not in parts
            return self.documents.joinpath(*parts[1:])

        def reply(result):
            Path(arg('--json-output')).write_bytes(encoded({'result': result}))

        action = command[2:5]
        error = ''
        if action == ['list', 'devices', '--json-output']:
            reply({'devices': [{'identifier': 'offline-phone',
                                'hardwareProperties': {'platform': 'iOS', 'reality': 'physical'}}]})
        elif action == ['device', 'info', 'apps']:
            reply({'apps': [{'bundleIdentifier': push.BUNDLE_ID}]})
        else:
            assert arg('--device') == 'offline-phone'
            assert arg('--domain-identifier') == push.BUNDLE_ID
            assert arg('--domain-type') == 'appDataContainer'
            if action == ['device', 'info', 'files']:
                folder = arg('--subdirectory')
                path = device_path(folder)
                if path.is_dir():
                    reply({'files': [{'relativePath': f'{folder}/{item.name}'} for item in path.iterdir()]})
                else:
                    error = 'No such directory'
            elif action == ['device', 'copy', 'from']:
                source = device_path(arg('--source'))
                if source.is_file():
                    shutil.copyfile(source, arg('--destination'))
                else:
                    error = 'No such file'
            elif action == ['device', 'copy', 'to']:
                relative = arg('--destination')
                assert relative.startswith('Documents/.push-staging/')
                dest = device_path(relative)
                assert not dest.exists()
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.copies.append(dest)
                if self.interrupt and dest.name == 'document.json':
                    dest.write_bytes(b'interrupted')
                    error = 'transfer interrupted'
                else:
                    shutil.copyfile(arg('--source'), dest)
            else:
                pytest.fail(f'unexpected command: {command}')
        return subprocess.CompletedProcess(command, int(bool(error)), '', error)


@pytest.mark.parametrize(('entry', 'failure', 'outcome'), [
    ('publish-cli', None, 'commit'),
    ('studio-engine', None, 'commit'),
    ('publish-cli', 'transfer', 'ignored'),
    ('publish-cli', 'asset', 'incompleteRevision'),
    ('publish-cli', 'manifest', 'rejectedIngest'),
    ('publish-cli', 'empty-ready', 'rejectedIngest'),
    ('publish-cli', 'predecessor', 'staleCatalog'),
])
def test_default_publish_and_app_consume(tmp_path, monkeypatch, entry, failure, outcome):
    probe = os.environ.get('PRIVATE_LISTEN_SNAPSHOT_PROBE')
    if not probe:
        pytest.skip('requires native LibrarySnapshotProbe')
    library = tmp_path / 'library'
    documents = tmp_path / 'Documents'
    documents.mkdir()
    app_library = documents / publisher.LIBRARY_NAME
    index = app_library / 'library.json'
    job = tmp_path / 'jobs' / 'default-entry'
    home = tmp_path / 'backup-home'
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('PRIVATE_LISTEN_LIBRARY', str(library))
    monkeypatch.setenv(push.ENV_SWITCH, '1')
    monkeypatch.setattr('studio_artwork.attach_cover', lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, '_require_audio_preflight', lambda *a, **k: None)
    transport = DocumentsTransport(documents, subprocess.run)
    monkeypatch.setattr(subprocess, 'run', transport)

    def consume(expected):
        subprocess.run([probe, '--consume-staging', str(documents), expected], check=True)

    def offer(version, route):
        fixture_render(job, version)
        if route == 'studio-engine':
            from studio_engine import StudioEngine

            engine = StudioEngine(job)
            monkeypatch.setattr(engine, 'render', lambda *args, **kwargs: job / 'output.mp3')
            assert engine.run(json.loads((job / 'config.json').read_bytes()), 'render') == job / 'output.mp3'
        else:
            assert publisher.main(['--job-dir', str(job)]) == 0
        return json.loads((job / push.STATUS_NAME).read_bytes())

    def staged(status):
        assert not status['ok'] and status.get('staged'), status
        tx = documents / '.push-staging' / status['devices'][0]['transaction']
        assert transport.copies[-2:] == [tx / 'manifest.json', tx / 'ready']
        assert not (tx / 'receipt.json').exists()
        return tx

    first = staged(offer(1, entry))
    assert not index.exists()
    consume('commit')
    assert (first / 'receipt.json').is_file()
    catalog = json.loads(index.read_bytes())
    [old] = catalog['episodes']
    assert old['id'] == job.name
    assert old['media_revision']['previous_path'] == ''
    old_folder = app_library / old['media_revision']['path']
    old_files = {path.name: path.read_bytes() for path in old_folder.iterdir()}
    assert old_files['output.mp3'] == (job / 'output.mp3').read_bytes()
    catalog['phone_settings'] = {'enabled': True}
    old['phone_only'] = {'url': 'https://example.invalid/设备'}
    write(index, catalog)
    before = index.read_bytes()

    transport.interrupt = failure == 'transfer'
    offered = offer(2, entry)
    [published] = json.loads((library / 'library.json').read_bytes())['episodes']
    assert published['id'] == job.name and published['media_revision']['previous_path'] == old['media_revision']['path']
    assert index.read_bytes() == before
    assert Path(offered['devices'][0]['backup']).read_bytes() == before
    if failure == 'transfer':
        assert not offered['ok'] and not offered['staged'] and 'transfer interrupted' in offered['reason']
        assert transport.copies[-1].name == 'document.json'
        assert list((documents / '.push-staging').glob('*/ready')) == [first / 'ready']
    else:
        second = staged(offered)
        manifest = second / 'manifest.json'
        body = json.loads(manifest.read_bytes())
        [package] = body['packages']
        assert package['revision'] == published['media_revision']['sha256']
        assert package['previous_path'] == old['media_revision']['path']
        if failure == 'asset':
            asset = second / 'objects' / package['revision'] / 'document.json'
            data = asset.read_bytes()
            asset.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        elif failure == 'manifest':
            manifest.write_bytes(manifest.read_bytes() + b' ')
        elif failure == 'empty-ready':
            (second / 'ready').write_bytes(b'')
        elif failure == 'predecessor':
            package['previous_path'] = f'episodes/{job.name}'
            write(manifest, body)
            (second / 'ready').write_text(hashlib.sha256(manifest.read_bytes()).hexdigest())

    consume(outcome)
    assert {path.name: path.read_bytes() for path in old_folder.iterdir()} == old_files
    if failure:
        assert index.read_bytes() == before
        assert list((documents / '.push-staging').glob('*/receipt.json')) == [first / 'receipt.json']
        return

    assert (second / 'receipt.json').is_file()
    catalog = json.loads(index.read_bytes())
    [landed] = catalog['episodes']
    revision = landed['media_revision']
    assert landed['id'] == job.name and landed['title'] == published['title']
    assert catalog['phone_settings'] == {'enabled': True} and landed['phone_only'] == old['phone_only']
    assert revision['previous_path'] == old['media_revision']['path']
    assert revision['files'] == published['media_revision']['files']
    source = library / published['media_revision']['path']
    for name in revision['files']:
        assert (app_library / revision['path'] / name).read_bytes() == (source / name).read_bytes()
