import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import studio_push_iphone
from studio_push_iphone import (
    BUNDLE_ID,
    DEVICE_LIBRARY,
    STATUS_NAME,
    episode_files,
    push_after_publish,
    push_enabled,
    push_episodes,
    reachable_devices,
)
from studio_publish_icloud import RENDER_FILES, publish_job

EPISODE_RESOURCES = ['output.mp3', 'cover.jpg', *sorted(RENDER_FILES), 'episode.json']
PUSH_EVENTS = ['backup', *(['copy'] * len(EPISODE_RESOURCES)), 'cas']

WIRED = {
    'identifier': 'AAAA-WIRED',
    'connectionProperties': {'pairingState': 'paired', 'tunnelState': 'disconnected', 'transportType': 'wired'},
    'deviceProperties': {'name': '不取经的小刘'},
    'hardwareProperties': {'platform': 'iOS', 'reality': 'physical', 'deviceType': 'iPhone', 'udid': '0001'},
}
AWAY = {
    'identifier': 'BBBB-AWAY',
    'connectionProperties': {'pairingState': 'paired', 'tunnelState': 'unavailable'},
    'deviceProperties': {'name': '旧手机'},
    'hardwareProperties': {'platform': 'iOS', 'reality': 'physical'},
}
WATCH = {
    'identifier': 'CCCC-WATCH',
    'connectionProperties': {'pairingState': 'paired', 'tunnelState': 'connected'},
    'deviceProperties': {'name': '手表'},
    'hardwareProperties': {'platform': 'watchOS', 'reality': 'physical'},
}
SIMULATOR = {
    'identifier': 'DDDD-SIM',
    'connectionProperties': {'pairingState': 'paired', 'tunnelState': 'connected'},
    'deviceProperties': {'name': '模拟器'},
    'hardwareProperties': {'platform': 'iOS', 'reality': 'virtual'},
}
WIFI = {
    'identifier': 'EEEE-WIFI',
    'connectionProperties': {'pairingState': 'paired', 'tunnelState': 'connected', 'transportType': 'localNetwork'},
    'deviceProperties': {'name': '无线'},
    'hardwareProperties': {'platform': 'iOS', 'reality': 'physical'},
}


class FakeDevicectl:
    """Synthetic phone with a readable index and a test-only CAS callback."""

    def __init__(self, devices=(), installed=True, fail_copy=None, list_code=0):
        self.devices = list(devices)
        self.installed = installed
        self.fail_copy = fail_copy
        self.list_code = list_code
        self.copies = []
        self.assets = {}
        self.commands = []
        self.events = []
        self.index = b'{"episodes":[{"id":"phone-only","show":"Existing Show"}]}\n'

    def __call__(self, command, timeout):
        self.commands.append(command)
        self.assert_shape(command)
        if '--json-output' in command:
            output = Path(command[command.index('--json-output') + 1])
            if command[2:4] == ['list', 'devices']:
                output.write_text(json.dumps({'result': {'devices': self.devices}}), encoding='utf-8')
                return self.list_code, '', 'boom' if self.list_code else ''
            if command[2:5] == ['device', 'info', 'apps']:
                if self.installed is None:
                    return 1, '', 'ERROR: could not connect'
                apps = [{'bundleIdentifier': BUNDLE_ID}] if self.installed else []
                output.write_text(json.dumps({'result': {'apps': apps}}), encoding='utf-8')
                return 0, '', ''
        if command[2:5] == ['device', 'copy', 'from']:
            source = command[command.index('--source') + 1]
            backup = Path(command[command.index('--destination') + 1])
            if source == f'{DEVICE_LIBRARY}/library.json':
                backup.write_bytes(self.index)
                self.events.append('read-commit' if 'cas' in self.events else 'backup')
            else:
                backup.write_bytes(self.assets[source])
            return 0, '', ''
        if command[2:5] == ['device', 'copy', 'to']:
            device = command[command.index('--device') + 1]
            destination = command[command.index('--destination') + 1]
            source = command[command.index('--source') + 1]
            if self.fail_copy and self.fail_copy in destination:
                return 1, '', 'ERROR: The device is locked.'
            assert 'backup' in self.events
            assert destination != f'{DEVICE_LIBRARY}/library.json'
            self.assets[destination] = Path(source).read_bytes()
            self.copies.append((device, Path(source).name, destination, timeout))
            self.events.append('copy')
            return 0, '', ''
        return 1, '', 'unexpected'

    def atomic_commit(self, device, merged, expected):
        assert device == 'AAAA-WIRED'
        assert self.events[0] == 'backup'
        self.events.append('cas')
        if self.index != expected:
            return 'snapshot changed'
        self.index = merged
        return None

    @staticmethod
    def assert_shape(command):
        assert command[:2] == ['xcrun', 'devicectl'], command
        if command[2:4] == ['device', 'copy']:
            assert command[command.index('--domain-type') + 1] == 'appDataContainer'
            assert command[command.index('--domain-identifier') + 1] == BUNDLE_ID


def published_library(root, ident='07c1dddbdc684968b69199ad4f32e86b', cover=True):
    library = Path(root) / 'library'
    episode = library / 'episodes' / ident
    episode.mkdir(parents=True)
    (episode / 'output.mp3').write_bytes(b'mp3')
    if cover:
        (episode / 'cover.jpg').write_bytes(b'jpg')
    for name in RENDER_FILES:
        (episode / name).write_text('{}', encoding='utf-8')
    (episode / 'episode.json').write_text(json.dumps({'id': ident, 'title': '马克·库班 · The Diary Of A CEO', 'created_at': '2026-10-27T00:00:00+00:00', 'file': 'output.mp3'}), encoding='utf-8')
    (library / 'library.json').write_text(json.dumps({'episodes': [{'id': ident}]}), encoding='utf-8')
    return library, episode


class DeviceDiscoveryTests(unittest.TestCase):
    def test_only_paired_physical_reachable_ios_devices_wired_first(self):
        fake = FakeDevicectl([WIFI, AWAY, WATCH, SIMULATOR, WIRED])
        devices = reachable_devices(fake)
        self.assertEqual([device['id'] for device in devices], ['AAAA-WIRED', 'EEEE-WIFI'])
        self.assertEqual(devices[0]['name'], '不取经的小刘')

    def test_list_failure_means_no_devices(self):
        self.assertEqual(reachable_devices(FakeDevicectl([WIRED], list_code=1)), [])

    def test_runner_errors_do_not_leak_temp_json(self):
        before = set(Path(tempfile.gettempdir()).glob('privatelisten-devicectl-*'))
        reachable_devices(FakeDevicectl([WIRED]))
        self.assertEqual(set(Path(tempfile.gettempdir()).glob('privatelisten-devicectl-*')), before)


class PushTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(studio_push_iphone, 'devicectl_available', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_episode_files_put_audio_first_and_json_last(self):
        with tempfile.TemporaryDirectory() as root:
            _, episode = published_library(root)
            (episode / '.output.mp3.123.partial').write_bytes(b'x')
            names = [destination for _, destination in episode_files(episode)]
            ident = episode.name
            self.assertEqual(names, [f'episodes/{ident}/{name}' for name in EPISODE_RESOURCES])

    def test_backup_then_episode_resources_then_injected_atomic_commit(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([WIRED])
            original = fake.index
            summary = push_episodes([episode], library, runner=fake, backup_root=Path(root) / 'backups',
                                    atomic_commit=fake.atomic_commit)
            self.assertTrue(summary['ok'])
            self.assertEqual(Path(summary['devices'][0]['backup']).read_bytes(), original)
            self.assertEqual(fake.events, [*PUSH_EVENTS, 'read-commit'])
            self.assertCountEqual(json.loads(fake.index)['episodes'], [
                {'id': 'phone-only', 'show': 'Existing Show'}, {'id': episode.name},
            ])
            ident = episode.name
            self.assertEqual([copy[2] for copy in fake.copies], [
                f'{DEVICE_LIBRARY}/episodes/{ident}/{name}' for name in EPISODE_RESOURCES
            ])
            self.assertTrue(DEVICE_LIBRARY.startswith('Documents/'))
            self.assertTrue(all(copy[0] == 'AAAA-WIRED' for copy in fake.copies))
            self.assertGreater(fake.copies[0][3], fake.copies[-1][3])

    def test_snapshot_mismatch_never_reports_index_committed(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([WIRED])
            original = fake.index
            moved_index = b'{"episodes":[{"id":"phone-new"}]}'

            def reject_stale_snapshot(device, merged, expected):
                self.assertEqual(expected, original)
                fake.index = moved_index
                return fake.atomic_commit(device, merged, expected)

            summary = push_episodes([episode], library, runner=fake, backup_root=Path(root) / 'backups',
                                    atomic_commit=reject_stale_snapshot)
            self.assertFalse(summary['ok'])
            self.assertIn('snapshot changed', summary['reason'])
            self.assertEqual(fake.index, moved_index)
            self.assertEqual(Path(summary['devices'][0]['backup']).read_bytes(), original)
            self.assertEqual(fake.events, PUSH_EVENTS)

    def test_no_device_is_soft(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([AWAY])
            summary = push_episodes([episode], library, runner=fake)
            self.assertFalse(summary['ok'])
            self.assertEqual(summary['reason'], '没有连着的 iPhone')
            self.assertEqual(fake.copies, [])

    def test_app_missing_skips_device(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([WIRED], installed=False)
            summary = push_episodes([episode], library, runner=fake)
            self.assertFalse(summary['ok'])
            self.assertEqual(summary['devices'][0]['error'], '没有安装私人听')
            self.assertEqual(fake.copies, [])

    def test_unknown_install_state_still_tries_copy(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([WIRED], installed=None)
            summary = push_episodes([episode], library, runner=fake, backup_root=Path(root) / 'backups',
                                    atomic_commit=fake.atomic_commit)
            self.assertTrue(summary['ok'])
            self.assertEqual(fake.events, [*PUSH_EVENTS, 'read-commit'])

    def test_copy_failure_stops_before_listing_that_episode(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([WIRED], fail_copy='output.mp3')
            original = fake.index
            summary = push_episodes([episode], library, runner=fake, backup_root=Path(root) / 'backups',
                                    atomic_commit=fake.atomic_commit)
            self.assertFalse(summary['ok'])
            self.assertIn('locked', summary['reason'])
            self.assertEqual(fake.copies, [])
            self.assertEqual(fake.events, ['backup'])
            self.assertEqual(fake.index, original)
            self.assertEqual(Path(summary['devices'][0]['backup']).read_bytes(), original)

    def test_episode_without_audio_is_not_pushed(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            (episode / 'output.mp3').unlink()
            fake = FakeDevicectl([WIRED])
            summary = push_episodes([episode], library, runner=fake)
            self.assertFalse(summary['ok'])
            self.assertEqual(fake.commands, [])

    def test_without_devicectl_nothing_runs(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([WIRED])
            with patch.object(studio_push_iphone, 'devicectl_available', return_value=False):
                summary = push_episodes([episode], library, runner=fake)
            self.assertEqual(summary['reason'], '这台机器没有 xcrun devicectl')
            self.assertEqual(fake.commands, [])


class SwitchTests(unittest.TestCase):
    def test_default_icloud_library_pushes_and_temp_library_does_not(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as other:
            env = {key: value for key, value in os.environ.items() if key not in ('PRIVATE_LISTEN_LIBRARY', 'PRIVATE_LISTEN_DEVICE_PUSH')}
            with patch.dict(os.environ, env, clear=True), patch('studio_publish_icloud.Path.home', return_value=Path(home)):
                real = Path(home) / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs' / '中文播客私人库'
                real.mkdir(parents=True)
                self.assertTrue(push_enabled(real))
                self.assertFalse(push_enabled(Path(other)))
                with patch.dict(os.environ, {'PRIVATE_LISTEN_DEVICE_PUSH': '0'}):
                    self.assertFalse(push_enabled(real))
            with patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': other}):
                os.environ.pop('PRIVATE_LISTEN_DEVICE_PUSH', None)
                self.assertFalse(push_enabled(Path(other)))
                with patch.dict(os.environ, {'PRIVATE_LISTEN_DEVICE_PUSH': '1'}):
                    self.assertTrue(push_enabled(Path(other)))


class HookTests(unittest.TestCase):
    _SHORT_MP3_BYTES: bytes | None = None

    @classmethod
    def _short_mp3(cls) -> bytes:
        if cls._SHORT_MP3_BYTES is None:
            import subprocess
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / 'tone.mp3'
                subprocess.run(
                    ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=24000:cl=mono',
                     '-t', '1', '-c:a', 'libmp3lame', str(out)],
                    check=True,
                )
                cls._SHORT_MP3_BYTES = out.read_bytes()
        return cls._SHORT_MP3_BYTES

    def setUp(self):
        p = patch('studio_publish_icloud._require_audio_preflight', return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def make_job(self, root, ident='07c1dddbdc684968b69199ad4f32e86b'):
        job = Path(root) / ident
        job.mkdir()
        (job / 'state.json').write_text(json.dumps({'title': '马克·库班 · The Diary Of A CEO'}), encoding='utf-8')
        (job / 'output.mp3').write_bytes(self._short_mp3())
        (job / 'transcript_zh.json').write_text(
            json.dumps({'segments': [{'id': 'seg-1', 'zh': '马克·库班测试译稿。'}]}, ensure_ascii=False),
            encoding='utf-8',
        )
        return job

    def render_fixture(self, job):
        from studio_engine import fingerprint, sha256_file

        config = {'source': {'kind': 'youtube', 'value': 'https://www.youtube.com/watch?v=MM-C3JqCXBk'},
                  'expected_speakers': 1}
        doc = {'segments': [{'id': 'seg-000001', 'speaker_id': 'host', 'start_ms': 0, 'end_ms': 1000,
                             'chinese': '马克·库班测试译稿。'}],
               'speakers': {'host': {'voice_slot': 'male_1'}}}
        lock = job / 'source-identity.json'
        fixture = {
            'config.json': config,
            'document.json': doc,
            lock.name: {**config['source'], 'sha256': 'a' * 64, 'bytes': 100, 'duration_ms': 1000},
        }
        for name, data in fixture.items():
            (job / name).write_text(json.dumps(data), encoding='utf-8')
        mp3 = job / 'output.mp3'
        if mp3.exists():
            (job / 'render-identity.json').write_text(json.dumps({
                'schema_version': 1, 'sample_rate': 24000,
                'audio_sha256': sha256_file(mp3), 'document_sha256': fingerprint(doc),
                'source_identity_sha256': sha256_file(lock),
                'timeline': [{'id': 'seg-000001', 'start_frame': 0, 'end_frame': 24000}],
            }), encoding='utf-8')
        return config

    def test_after_publish_records_status_and_never_raises(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root)
            self.render_fixture(job)
            with (
                patch('studio_publish_icloud.probe_duration_sec', return_value=1.0),
                patch('studio_artwork.attach_cover', return_value=None),
            ):
                result = publish_job(job, library)
            fake = FakeDevicectl([WIRED])
            lines = []
            original = fake.index
            with (
                patch.dict(os.environ, {'PRIVATE_LISTEN_DEVICE_PUSH': '1'}),
                patch.object(studio_push_iphone, 'devicectl_available', return_value=True),
                patch.object(Path, 'home', return_value=Path(root)),
            ):
                summary = push_after_publish(result, job, runner=fake, announce=lambda text, **_: lines.append(text))
            self.assertFalse(summary['ok'])
            self.assertTrue(summary['staged'])
            self.assertIn('等待 App 前台校验和提交', lines[0])
            status = json.loads((job / STATUS_NAME).read_text(encoding='utf-8'))
            self.assertFalse(status['ok'])
            self.assertTrue(status['staged'])
            self.assertEqual(Path(status['devices'][0]['backup']).read_bytes(), original)
            self.assertEqual(fake.index, original)
            self.assertEqual(fake.events[0], 'backup')
            self.assertTrue(all(target.startswith('Documents/.push-staging/') for _, _, target, _ in fake.copies))
            self.assertTrue(fake.copies[-1][2].endswith('/ready'))
            self.assertTrue(json.loads((job / 'private-listen.json').read_text(encoding='utf-8'))['ok'])

            def explode(command, timeout):
                raise RuntimeError('devicectl crashed')

            with patch.dict(os.environ, {'PRIVATE_LISTEN_DEVICE_PUSH': '1'}), patch.object(studio_push_iphone, 'devicectl_available', return_value=True):
                summary = push_after_publish(result, job, runner=explode, announce=lambda *a, **k: None)
            self.assertFalse(summary['ok'])
            self.assertIn('devicectl crashed', summary['reason'])
            self.assertTrue(json.loads((job / 'private-listen.json').read_text(encoding='utf-8'))['ok'])

    def test_temp_library_is_skipped_without_running_devicectl(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root)
            fake = FakeDevicectl([WIRED])
            with patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': library}):
                os.environ.pop('PRIVATE_LISTEN_DEVICE_PUSH', None)
                summary = push_after_publish({'library': library, 'episode_dir': str(Path(library) / 'episodes' / job.name)}, job, runner=fake, announce=None)
            self.assertTrue(summary['skipped'])
            self.assertEqual(fake.commands, [])

    def test_engine_pushes_after_complete_and_survives_push_errors(self):
        from studio_engine import StudioEngine
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            engine = StudioEngine(Path(root) / '07c1dddbdc684968b69199ad4f32e86b')
            (engine.job / 'state.json').write_text(json.dumps({'title': '马克·库班'}), encoding='utf-8')
            mp3 = engine.job / 'output.mp3'
            mp3.write_bytes(self._short_mp3())
            config = self.render_fixture(engine.job)
            seen = []

            def fake_push(result, job_dir, **_):
                progress = json.loads((Path(job_dir) / 'progress.json').read_text(encoding='utf-8'))
                seen.append((progress['stage'], result['id']))
                raise RuntimeError('cable pulled')

            with (
                patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': library}),
                patch.object(engine, 'render', return_value=mp3),
                patch('studio_publish_icloud.probe_duration_sec', return_value=1.0),
                patch('studio_push_iphone.push_after_publish', side_effect=fake_push),
            ):
                self.assertEqual(engine.run(config, 'render'), mp3)
            self.assertEqual(seen, [('complete', engine.job.name)])
            self.assertTrue(json.loads((engine.job / 'private-listen.json').read_text(encoding='utf-8'))['ok'])

    def test_engine_preview_never_pushes(self):
        from studio_engine import StudioEngine
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(Path(root) / 'job-preview')
            preview = engine.job / 'preview.mp3'
            preview.write_bytes(b'preview')
            config = self.render_fixture(engine.job)
            with patch.object(engine, 'render', return_value=preview), patch('studio_push_iphone.push_after_publish') as push:
                engine.run(config, 'render', preview=2)
            push.assert_not_called()

    def test_server_does_not_publish_or_push_missing_receipt_after_engine_exit(self):
        from studio_server import Studio, write
        with tempfile.TemporaryDirectory() as jobs, tempfile.TemporaryDirectory() as library:
            for exit_code in (0, 1):
                with self.subTest(exit_code=exit_code):
                    studio = Studio(jobs, engine=['/bin/true'], recover=False)
                    ident = f'job-{exit_code}'
                    job = Path(jobs) / ident
                    job.mkdir()
                    write(job / 'state.json', {'status': 'queued', 'action': 'render'})
                    (job / 'output.mp3').write_bytes(b'old or independently rendered fixture audio')

                    def finish(_):
                        studio.stop = True
                        return exit_code, False

                    with (
                        patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': library}),
                        patch('studio_server.subprocess.Popen') as launch,
                        patch('studio_server.wait_for_engine', side_effect=finish),
                        patch('studio_publish_icloud.publish_job') as publish,
                        patch('studio_push_iphone.push_after_publish') as push,
                    ):
                        studio.worker()
                    command = launch.call_args.args[0]
                    self.assertEqual(command[command.index('--publish-library') + 1], str(Path(library).resolve()))
                    publish.assert_not_called()
                    push.assert_not_called()
                    self.assertFalse((Path(library) / 'library.json').exists())

    def test_cli_publish_offers_device_copy_unless_disabled(self):
        from studio_publish_icloud import main
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root)
            with patch('studio_push_iphone.push_after_publish') as push, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--job-dir', str(job), '--library', library]), 0)
                push.assert_called_once()
                push.reset_mock()
                self.assertEqual(main(['--job-dir', str(job), '--library', library, '--no-device']), 0)
                push.assert_not_called()


class CliTests(unittest.TestCase):
    def test_all_stages_every_library_episode_and_leaves_catalog_for_app(self):
        with tempfile.TemporaryDirectory() as root:
            library, _ = published_library(root, 'job-a')
            published_library(root, 'job-b', cover=False)
            (library / 'library.json').write_text('{"episodes":[{"id":"job-a"},{"id":"job-b"}]}')
            fake = FakeDevicectl([WIRED])
            original = fake.index
            stdout = io.StringIO()
            with (
                patch.object(studio_push_iphone, 'devicectl_available', return_value=True),
                patch.object(studio_push_iphone, 'push_episodes', wraps=push_episodes) as push,
                patch.object(Path, 'home', return_value=Path(root)),
                redirect_stdout(stdout),
            ):
                code = studio_push_iphone.main(['--all', '--library', str(library)], runner=fake)
            self.assertEqual(code, 0)
            self.assertIn('等待 App 前台校验和提交', stdout.getvalue())
            push.assert_called_once_with(None, library, runner=fake, backup_root=None, atomic_commit=None)
            manifest_path = next(path for path in fake.assets if path.endswith('/manifest.json'))
            self.assertEqual([item['id'] for item in json.loads(fake.assets[manifest_path])['packages']],
                             ['job-a', 'job-b'])
            self.assertEqual(fake.events[0], 'backup')
            self.assertTrue(fake.copies[-1][2].endswith('/ready'))
            self.assertEqual(fake.index, original)

    def test_bad_episode_id_is_rejected(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(studio_push_iphone.main(['--episode-id', '../etc']), 1)


if __name__ == '__main__':
    unittest.main()
