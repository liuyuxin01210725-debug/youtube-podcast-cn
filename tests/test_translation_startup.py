"""Exercise the default verification and translation startup in fresh interpreters."""

from contextlib import closing
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_translation_budget import NOW, create

from translation_budget import TranslationBudget


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'tests/run_translation_checks.py'


class StartupTests(unittest.TestCase):
    def run_python(self, *args, env=None):
        return subprocess.run(
            [sys.executable, *args], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30,
        )

    def test_runner_keeps_hook_paths_out_of_the_caller_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'sitecustomize.py').write_text('ambient_fixture = True\n')
            with patch.dict(os.environ, PYTHONPATH=directory):
                before = os.environ.copy()
                result = self.run_python(
                    '-I', '-B', RUNNER,
                    'tests.test_translation_startup.OfflineRunnerProbe.verify_network_block',
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(os.environ.copy() == before, 'runner changed the caller environment')
                # The next ordinary interpreter still sees exactly the caller's path.
                result = self.run_python('-s', '-B', '-c', '''
import os
import sitecustomize
assert sitecustomize.ambient_fixture
assert os.environ['PYTHONPATH'] == os.path.dirname(sitecustomize.__file__)
''')
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_inherited_site_hook_blocks_cli_and_translation_before_credentials_or_reserve(self):
        self.assert_site_hook_blocks_translation('DeniedSocket')

    def test_site_hook_replacing_socket_with_a_function_still_reports_the_startup_code(self):
        self.assert_site_hook_blocks_translation('reject')

    def assert_site_hook_blocks_translation(self, constructor):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'sitecustomize.py').write_text(f'''
import socket

def reject(*args, **kwargs):
    raise AssertionError('unexpected use of disabled socket API')

class DeniedSocket(socket.socket):
    __init__ = reject

socket.socket = {constructor}
socket.create_connection = socket.getaddrinfo = reject
''')
            job = root / 'synthetic-job'
            job.mkdir()
            create(job, clock=lambda: NOW)
            ledger = job / TranslationBudget.FILENAME
            before = ledger.read_bytes()
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), str(ROOT / 'scripts'))))
            cli_job = root / 'cli-job'
            cli = self.run_python(
                '-s', '-B', '-u', 'scripts/studio_engine.py',
                '--job-dir', cli_job, '--config', root / 'unused-config.json', '--action', 'prepare',
                env=env,
            )
            self.assertEqual(cli.returncode, 1, cli.stderr)
            self.assertIn('translation_network_disabled', cli.stderr)
            self.assertNotIn('translation_transport_unknown', cli.stderr)
            self.assertFalse(cli_job.exists())
            result = self.run_python('-s', '-B', '-c', '''
import pathlib
import sitecustomize
import socket
import sys

import podcast_cn
from studio_recovery import StudioRecoveryError, TranslationJournal
from translation_budget import ENDPOINT, MODEL, TranslationBudget, request_translation

assert socket.socket is getattr(sitecustomize, sys.argv[3])
job = pathlib.Path(sys.argv[1])
budget = TranslationBudget(job, clock=lambda: int(sys.argv[2]))
journal = TranslationJournal(job)
calls = []

def unexpected(*args, **kwargs):
    calls.append(True)
    raise AssertionError('unexpected credential read, reserve or dispatch')

def deny_network(event, args):
    if event.startswith('socket.') or event == 'urllib.Request':
        unexpected()

sys.addaudithook(deny_network)
budget.reserve = podcast_cn.os.getenv = unexpected
for entry in (
    lambda: podcast_cn.create_translation_client('deepseek'),
    lambda: podcast_cn.create_translation_client('deepseek', budget=budget, request_journal=journal,
                                                  credential_loader=unexpected, model=MODEL, base_url=ENDPOINT),
    lambda: request_translation(
        provider='deepseek', model=MODEL, base_url=ENDPOINT, messages=[],
        stage='translation_chunk', timeout=1, budget=budget, load_key=unexpected,
        opener=unexpected, validate_content=str, validation_error=ValueError,
        journal=journal, segment_ids=['a'],
    ),
):
    try:
        entry()
    except StudioRecoveryError as error:
        assert error.code == 'translation_network_disabled', error.code
    else:
        raise AssertionError('translation entry should fail closed')
assert not calls
''', job, str(NOW), constructor, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((job / 'translation-requests').exists())
            self.assertEqual(ledger.read_bytes(), before)
            with closing(sqlite3.connect(ledger)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone(), (0,))


class OfflineRunnerProbe(unittest.TestCase):
    # Called by name in a nested runner, so discovery does not run it by itself.
    def verify_network_block(self):
        self.assertNotIn(os.environ['PYTHONPATH'], sys.path)
        self.assertFalse(getattr(sys.modules.get('sitecustomize'), 'ambient_fixture', False))
        with self.assertRaisesRegex(AssertionError, r'blocked socket\.__new__'):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Exercise the real opener with a local data URL, which needs no socket.
        import urllib.request
        with self.assertRaisesRegex(AssertionError, r'blocked urllib\.Request'):
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open('data:,offline')
