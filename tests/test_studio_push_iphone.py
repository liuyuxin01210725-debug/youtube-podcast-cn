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
from studio_publish_icloud import publish_job

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
    """Answers the three devicectl subcommands the push uses and records copies."""

    def __init__(self, devices=(), installed=True, fail_copy=None, list_code=0):
        self.devices = list(devices)
        self.installed = installed
        self.fail_copy = fail_copy
        self.list_code = list_code
        self.copies = []
        self.commands = []

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
        if command[2:5] == ['device', 'copy', 'to']:
            device = command[command.index('--device') + 1]
            destination = command[command.index('--destination') + 1]
            source = command[command.index('--source') + 1]
            if self.fail_copy and self.fail_copy in destination:
                return 1, '', 'ERROR: The device is locked.'
            self.copies.append((device, Path(source).name, destination, timeout))
            return 0, '', ''
        return 1, '', 'unexpected'

    @staticmethod
    def assert_shape(command):
        assert command[:2] == ['xcrun', 'devicectl'], command
        if command[2:5] == ['device', 'copy', 'to']:
            assert command[command.index('--domain-type') + 1] == 'appDataContainer'
            assert command[command.index('--domain-identifier') + 1] == BUNDLE_ID


def published_library(root, ident='07c1dddbdc684968b69199ad4f32e86b', cover=True):
    library = Path(root) / 'library'
    episode = library / 'episodes' / ident
    episode.mkdir(parents=True)
    (episode / 'output.mp3').write_bytes(b'mp3')
    if cover:
        (episode / 'cover.jpg').write_bytes(b'jpg')
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
            self.assertEqual(names, [f'episodes/{ident}/output.mp3', f'episodes/{ident}/cover.jpg', f'episodes/{ident}/episode.json'])

    def test_copies_episode_then_library_into_app_documents(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([WIRED])
            summary = push_episodes([episode], library, runner=fake)
            self.assertTrue(summary['ok'])
            ident = episode.name
            self.assertEqual([copy[2] for copy in fake.copies], [
                f'{DEVICE_LIBRARY}/episodes/{ident}/output.mp3',
                f'{DEVICE_LIBRARY}/episodes/{ident}/cover.jpg',
                f'{DEVICE_LIBRARY}/episodes/{ident}/episode.json',
                f'{DEVICE_LIBRARY}/library.json',
            ])
            self.assertTrue(DEVICE_LIBRARY.startswith('Documents/'))
            self.assertTrue(all(copy[0] == 'AAAA-WIRED' for copy in fake.copies))
            self.assertGreater(fake.copies[0][3], fake.copies[-1][3])

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
            self.assertTrue(push_episodes([episode], library, runner=fake)['ok'])

    def test_copy_failure_stops_before_listing_that_episode(self):
        with tempfile.TemporaryDirectory() as root:
            library, episode = published_library(root)
            fake = FakeDevicectl([WIRED], fail_copy='output.mp3')
            summary = push_episodes([episode], library, runner=fake)
            self.assertFalse(summary['ok'])
            self.assertIn('locked', summary['reason'])
            self.assertEqual(fake.copies, [])

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
    def make_job(self, root, ident='07c1dddbdc684968b69199ad4f32e86b'):
        job = Path(root) / ident
        job.mkdir()
        (job / 'state.json').write_text(json.dumps({'title': '马克·库班 · The Diary Of A CEO'}), encoding='utf-8')
        (job / 'output.mp3').write_bytes(b'full-audio')
        return job

    def test_after_publish_records_status_and_never_raises(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as library:
            job = self.make_job(root)
            with patch('studio_publish_icloud.shutil.which', return_value=None), patch('studio_artwork.download_image', return_value=None):
                result = publish_job(job, library)
            fake = FakeDevicectl([WIRED])
            lines = []
            with patch.dict(os.environ, {'PRIVATE_LISTEN_DEVICE_PUSH': '1'}), patch.object(studio_push_iphone, 'devicectl_available', return_value=True):
                summary = push_after_publish(result, job, runner=fake, announce=lambda text, **_: lines.append(text))
            self.assertTrue(summary['ok'])
            self.assertIn('不取经的小刘', lines[0])
            status = json.loads((job / STATUS_NAME).read_text(encoding='utf-8'))
            self.assertTrue(status['ok'])
            self.assertIn(f'{DEVICE_LIBRARY}/library.json', [copy[2] for copy in fake.copies])
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
            (engine.job / 'document.json').write_text('{}', encoding='utf-8')
            (engine.job / 'state.json').write_text(json.dumps({'title': '马克·库班'}), encoding='utf-8')
            mp3 = engine.job / 'output.mp3'
            mp3.write_bytes(b'full-audio')
            config = {'source': {'kind': 'youtube', 'value': 'https://www.youtube.com/watch?v=MM-C3JqCXBk'}}
            seen = []

            def fake_push(result, job_dir, **_):
                progress = json.loads((Path(job_dir) / 'progress.json').read_text(encoding='utf-8'))
                seen.append((progress['stage'], result['id']))
                raise RuntimeError('cable pulled')

            with patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': library}), patch.object(engine, 'render', return_value=mp3), patch('studio_push_iphone.push_after_publish', side_effect=fake_push):
                self.assertEqual(engine.run(config, 'render'), mp3)
            self.assertEqual(seen, [('complete', engine.job.name)])
            self.assertTrue(json.loads((engine.job / 'private-listen.json').read_text(encoding='utf-8'))['ok'])

    def test_engine_preview_never_pushes(self):
        from studio_engine import StudioEngine
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(Path(root) / 'job-preview')
            (engine.job / 'document.json').write_text('{}', encoding='utf-8')
            preview = engine.job / 'preview.mp3'
            preview.write_bytes(b'preview')
            config = {'source': {'kind': 'youtube', 'value': 'https://www.youtube.com/watch?v=MM-C3JqCXBk'}}
            with patch.object(engine, 'render', return_value=preview), patch('studio_push_iphone.push_after_publish') as push:
                engine.run(config, 'render', preview=2)
            push.assert_not_called()

    def test_server_retry_pushes_after_successful_publish(self):
        from studio_server import Studio
        with tempfile.TemporaryDirectory() as jobs, tempfile.TemporaryDirectory() as library:
            studio = Studio(jobs)
            job = Path(jobs) / 'abc123'
            job.mkdir()
            (job / 'output.mp3').write_bytes(b'audio')
            with patch.dict(os.environ, {'PRIVATE_LISTEN_LIBRARY': library}), patch('studio_push_iphone.push_after_publish') as push, redirect_stdout(io.StringIO()):
                studio.retry_private_listen(job)
            push.assert_called_once()
            self.assertEqual(push.call_args.args[0]['id'], 'abc123')
            self.assertEqual(Path(push.call_args.args[1]), job)

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
    def test_all_pushes_every_library_episode(self):
        with tempfile.TemporaryDirectory() as root:
            library, _ = published_library(root, 'job-a')
            second = library / 'episodes' / 'job-b'
            second.mkdir()
            (second / 'output.mp3').write_bytes(b'mp3')
            fake = FakeDevicectl([WIRED])
            with patch.object(studio_push_iphone, 'devicectl_available', return_value=True), redirect_stdout(io.StringIO()):
                code = studio_push_iphone.main(['--all', '--library', str(library)], runner=fake)
            self.assertEqual(code, 0)
            copied = [copy[2] for copy in fake.copies]
            self.assertIn(f'{DEVICE_LIBRARY}/episodes/job-a/output.mp3', copied)
            self.assertIn(f'{DEVICE_LIBRARY}/episodes/job-b/output.mp3', copied)
            self.assertEqual(copied[-1], f'{DEVICE_LIBRARY}/library.json')

    def test_bad_episode_id_is_rejected(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(studio_push_iphone.main(['--episode-id', '../etc']), 1)


if __name__ == '__main__':
    unittest.main()
