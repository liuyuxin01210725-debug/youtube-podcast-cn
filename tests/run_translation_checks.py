"""Offline verification in a fresh interpreter. Run with python -I -B."""

from pathlib import Path
import socket
import sys
import unittest


def deny_network(event, args):
    if event == 'socket.__new__' and args[1] == socket.AF_UNIX:
        return  # The local asyncio wakeup pipe uses socketpair.
    if event.startswith('socket.') or event == 'urllib.Request':
        raise AssertionError(f'offline verification blocked {event}')


# Also installed when multiprocessing's spawn imports this entry point.
if __name__ in ('__main__', '__mp_main__'):
    sys.addaudithook(deny_network)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == '__main__':
    unittest.main(module=None, argv=sys.argv if len(sys.argv) > 1 else [
        sys.argv[0], '-v',
        'tests.test_translation_startup',
        'tests.test_translation_budget',
        'tests.test_translation_admission',
        'tests.test_translation_runtime_admission',
        'tests.test_translation_history_adoption',
        'tests.test_translation_pre_send_recovery',
        'tests.test_studio_recovery',
        'tests.test_studio_engine',
    ])
