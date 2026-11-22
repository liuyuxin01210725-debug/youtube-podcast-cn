#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from shengjian_library_snapshot import SnapshotError, restore, snapshot


def sha256(path):
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def build_library(root):
    library = root / 'library'
    documents = root / 'documents'
    (library / 'objects').mkdir(parents=True)
    (library / 'episodes' / 'ep-keep').mkdir(parents=True)
    documents.mkdir()
    catalog = {
        'schema': 'private-listen.library.v2',
        'generation': 2,
        'unknown_future_field': {'keep': True},
        'episodes': [{'id': 'ep-keep', 'pending': {'state': 'ready'}, 'revision': 'abc'}],
    }
    (library / 'library.json').write_text(json.dumps(catalog, ensure_ascii=False), encoding='utf-8')
    (library / 'user-state.json').write_text(json.dumps({
        'progress': {'ep-keep': {'seconds': 12.5, 'finished': False}},
        'continueDismissed': [],
        'continueTouched': {'ep-keep': 1},
        'starred': ['ep-keep'],
        'extra_unknown': 7,
    }), encoding='utf-8')
    (library / 'episodes' / 'ep-keep' / 'manifest.json').write_text('{"pointer": "objects/x"}\n', encoding='utf-8')
    (library / 'episodes' / 'ep-keep' / 'output.mp3').write_bytes(b'AUDIO-ORIGINAL')
    (library / 'objects' / 'x.mp3').write_bytes(b'OBJECT-ORIGINAL')
    (documents / 'listen_notes.json').write_text(json.dumps({'ep-keep': {'body': '听后感', 'rev': 3}}, ensure_ascii=False), encoding='utf-8')
    (documents / 'book_notes.json').write_text(json.dumps({'b1': 'note'}, ensure_ascii=False), encoding='utf-8')
    (documents / 'book_state.json').write_text(json.dumps({'b1': {'page': 4}}, ensure_ascii=False), encoding='utf-8')
    return library, documents


class SnapshotTests(unittest.TestCase):
    def test_snapshot_skips_audio_and_restore_is_transactional(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library, documents = build_library(root)
            audio_hash = sha256(library / 'objects' / 'x.mp3')
            snap = root / 'snap'
            snapshot(snap, library=library, documents=documents, label='test')
            manifest = json.loads((snap / 'manifest.json').read_text(encoding='utf-8'))
            rels = {item['rel'] for item in manifest['files']}
            self.assertIn('library.json', rels)
            self.assertIn('user-state.json', rels)
            self.assertIn('listen_notes.json', {item['rel'] for item in manifest['files'] if item['root'] == 'documents'})
            self.assertIn('book_notes.json', {item['rel'] for item in manifest['files'] if item['root'] == 'documents'})
            self.assertIn('book_state.json', {item['rel'] for item in manifest['files'] if item['root'] == 'documents'})
            self.assertTrue(any(item['rel'].endswith('manifest.json') for item in manifest['files']))
            self.assertFalse(any(item['rel'].endswith('.mp3') for item in manifest['files']))
            # No hard links back to the live tree.
            live_catalog = library / 'library.json'
            snap_catalog = snap / 'roots' / 'library' / 'library.json'
            self.assertNotEqual(live_catalog.stat().st_ino, snap_catalog.stat().st_ino)

            # Fault: overwrite gen2 with gen1, drop notes, leave audio.
            (library / 'library.json').write_text(json.dumps({
                'schema': 'private-listen.library.v1',
                'generation': 1,
                'episodes': [],
            }), encoding='utf-8')
            (documents / 'listen_notes.json').unlink()
            (documents / 'listen_notes.json').write_text('{}', encoding='utf-8')
            (library / 'user-state.json').write_text('{"progress": {}}', encoding='utf-8')
            (library / 'objects' / 'x.mp3').write_bytes(b'OBJECT-ORIGINAL')  # still intact

            started = time.perf_counter()
            report = restore(snap, library=library, documents=documents)
            elapsed = report['elapsed_sec']
            wall = time.perf_counter() - started
            restored = json.loads((library / 'library.json').read_text(encoding='utf-8'))
            self.assertEqual(restored['generation'], 2)
            self.assertEqual(restored['unknown_future_field'], {'keep': True})
            notes = json.loads((documents / 'listen_notes.json').read_text(encoding='utf-8'))
            self.assertEqual(notes['ep-keep']['body'], '听后感')
            state = json.loads((library / 'user-state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['progress']['ep-keep']['seconds'], 12.5)
            self.assertEqual(state['extra_unknown'], 7)
            self.assertEqual((library / 'objects' / 'x.mp3').read_bytes(), b'OBJECT-ORIGINAL')
            self.assertEqual((library / 'episodes' / 'ep-keep' / 'output.mp3').read_bytes(), b'AUDIO-ORIGINAL')
            self.assertEqual(sha256(library / 'objects' / 'x.mp3'), audio_hash)
            self.assertGreaterEqual(elapsed, 0)
            self.assertLess(wall, 30)  # measured, not a 1s promise
            print(f'MEASURED_RESTORE_SEC={elapsed:.6f}')

    def test_snapshot_failure_refuses_and_backup_required(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dest = root / 'already'
            dest.mkdir()
            with self.assertRaises(SnapshotError):
                snapshot(dest, library=root / 'missing-lib')
            with self.assertRaises(SnapshotError):
                snapshot(root / 'snap')  # no roots

    def test_copy_of_real_sanitized_metadata_roundtrip(self):
        env = os.environ.get('SHENGJIAN_SANITIZED_LIBRARY_COPY', '')
        source = Path(env) if env else Path()
        if not env or not source.is_dir():
            self.skipTest('set SHENGJIAN_SANITIZED_LIBRARY_COPY to a local isolated copy')
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lib_src = source / 'Documents' / '中文播客私人库'
            doc_src = source / 'Documents'
            pref_src = source / 'Library' / 'Preferences'
            if not (lib_src / 'library.json').is_file():
                self.skipTest('sanitized copy has no library.json at the expected Documents path')
            library = root / 'library'
            documents = root / 'documents'
            preferences = root / 'preferences'
            library.mkdir()
            documents.mkdir()
            preferences.mkdir()
            # Metadata only; do not copy objects/audio, do not touch the source tree.
            shutil.copy2(lib_src / 'library.json', library / 'library.json')
            for name in ('user-state.json', 'bookmarks.json', 'insights.json'):
                if (lib_src / name).is_file():
                    shutil.copy2(lib_src / name, library / name)
            for rel_root in ('episodes', 'revisions'):
                if not (lib_src / rel_root).is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(lib_src / rel_root, followlinks=False):
                    dirnames[:] = [d for d in dirnames if d != 'objects']
                    rel = Path(dirpath).relative_to(lib_src)
                    (library / rel).mkdir(parents=True, exist_ok=True)
                    for filename in filenames:
                        if Path(filename).suffix.lower() in {'.mp3', '.m4a', '.wav', '.aac', '.caf'}:
                            continue
                        shutil.copy2(Path(dirpath) / filename, library / rel / filename)
            for name in ('listen_notes.json', 'listen_note_insights.json',
                         'book_notes.json', 'book_notes.v1.json',
                         'book_state.json', 'book_state.v1.json'):
                if (doc_src / name).is_file():
                    shutil.copy2(doc_src / name, documents / name)
            if pref_src.is_dir():
                for plist in pref_src.glob('*.plist'):
                    shutil.copy2(plist, preferences / plist.name)
            # Marker audio that must survive restore.
            (library / 'objects').mkdir()
            (library / 'objects' / 'keep.mp3').write_bytes(b'REAL-COPY-AUDIO')
            before = (library / 'library.json').read_text(encoding='utf-8')
            snap = root / 'snap'
            t0 = time.perf_counter()
            snapshot(snap, library=library, documents=documents, preferences=preferences)
            (library / 'library.json').write_text('{"generation": 1}', encoding='utf-8')
            report = restore(snap, library=library, documents=documents, preferences=preferences)
            t1 = time.perf_counter()
            after = json.loads((library / 'library.json').read_text(encoding='utf-8'))
            self.assertNotEqual(after.get('generation'), 1)
            self.assertEqual((library / 'library.json').read_text(encoding='utf-8'), before)
            self.assertEqual((library / 'objects' / 'keep.mp3').read_bytes(), b'REAL-COPY-AUDIO')
            print(f'MEASURED_SANITIZED_METADATA_RESTORE_SEC={report["elapsed_sec"]:.6f}')
            print(f'MEASURED_SANITIZED_METADATA_WALL_SEC={t1 - t0:.6f}')


if __name__ == '__main__':
    unittest.main()
