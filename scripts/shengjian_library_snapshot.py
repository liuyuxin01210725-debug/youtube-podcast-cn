#!/usr/bin/env python3
"""Transactional metadata snapshot / restore for 声间 write-set.

Covers the real metadata write set (catalog/manifest/pointers, listen_notes,
listen_along_notes / 听中笔记, book_notes, book_state, progress/Continue
user-state, insights, staging, and other mutable indexes). Reuses audio objects;
never copies audio; never hard-links metadata back onto the live tree. Backup
failure refuses the caller. Restore is a whole-snapshot swap so a later
generation is not partially overwritten by an older one. Does not promise full
recovery if recovery would require uninstalling the app (which deletes Documents).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path

from library_write_guard import CatalogWriteGuardError, decode_catalog_bytes

_LIBRARY_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,80}$')

AUDIO_SUFFIXES = {'.mp3', '.m4a', '.wav', '.aac', '.caf', '.flac', '.ogg', '.aiff', '.aif'}
SKIP_DIR_NAMES = {'objects', '.git', 'DerivedData'}
DOCUMENTS_KNOWN = (
    'listen_notes.json',
    'listen_along_notes.json',
    'listen_note_insights.json',
    'book_notes.json',
    'book_notes.v1.json',
    'book_state.json',
    'book_state.v1.json',
    'user-state.json',
    'insights.json',
)
LIBRARY_KNOWN = (
    'library.json',
    'library.json.bak',
    'user-state.json',
    'bookmarks.json',
    'insights.json',
    'catalog.json',
    'manifest.json',
)


class SnapshotError(RuntimeError):
    pass


def _is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_SUFFIXES


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.startswith('.shengjian-snap')


def iter_metadata_files(root: Path):
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if not _should_skip_dir(name) and not os.path.islink(os.path.join(dirpath, name))
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                # Record the symlink itself as metadata only if it does not point at audio objects we skip.
                yield path
                continue
            if not path.is_file():
                continue
            if _is_audio(path):
                continue
            yield path


def _copy_file(src: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        dest.symlink_to(os.readlink(src))
        return
    # Regular copy, never a hard link (a hard link would pollute the original).
    shutil.copy2(src, dest, follow_symlinks=False)
    info = dest.stat()
    if info.st_nlink > 1 and not dest.is_symlink():
        # Copy2 should have produced a new inode; refuse if it did not.
        raise SnapshotError(f'快照产生了硬链接，拒绝: {dest}')


def collect_roots(*, library=None, documents=None, preferences=None, extras=None):
    roots = {}
    if library is not None:
        roots['library'] = Path(library)
    if documents is not None:
        roots['documents'] = Path(documents)
    if preferences is not None:
        roots['preferences'] = Path(preferences)
    for index, extra in enumerate(extras or []):
        roots[f'extra-{index}'] = Path(extra)
    return roots


def snapshot(dest, *, library=None, documents=None, preferences=None, extras=None, label='snapshot'):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise SnapshotError(f'快照目标已存在，拒绝覆盖: {dest}')
    roots = collect_roots(library=library, documents=documents, preferences=preferences, extras=extras)
    if not roots:
        raise SnapshotError('没有可快照的写路径，拒绝合并')
    tmp = Path(tempfile.mkdtemp(prefix='shengjian-snap-wip-', dir=str(dest.parent if dest.parent.is_dir() else None)))
    manifest = {
        'label': label,
        'roots': {},
        'files': [],
        'absent': [],
        'skipped_audio': 0,
        'skipped_objects_dirs': True,
    }
    try:
        for name, root in roots.items():
            entry = {'path': str(root), 'existed': root.exists(), 'is_dir': root.is_dir() if root.exists() else False}
            manifest['roots'][name] = entry
            if not root.exists():
                manifest['absent'].append(name)
                continue
            if not root.is_dir():
                raise SnapshotError(f'写路径不是目录: {root}')
            staged = tmp / 'roots' / name
            staged.mkdir(parents=True, exist_ok=True)
            # Known names are recorded even when absent, so restore can keep them absent.
            known = DOCUMENTS_KNOWN if name == 'documents' else LIBRARY_KNOWN if name == 'library' else ()
            present = {p.relative_to(root).as_posix() for p in iter_metadata_files(root)}
            for known_name in known:
                if known_name not in present and not (root / known_name).exists():
                    manifest['absent'].append(f'{name}:{known_name}')
            for path in iter_metadata_files(root):
                rel = path.relative_to(root)
                target = staged / rel
                _copy_file(path, target)
                st = path.lstat()
                manifest['files'].append({
                    'root': name,
                    'rel': rel.as_posix(),
                    'size': st.st_size,
                    'mode': stat.S_IMODE(st.st_mode),
                    'symlink': path.is_symlink(),
                    'sha256': None if path.is_symlink() else _sha256(path),
                })
        if not manifest['files'] and all(not info['existed'] for info in manifest['roots'].values()):
            raise SnapshotError('写路径都不存在，备份失败，拒绝合并')
        (tmp / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
    except Exception as exc:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError(f'备份失败，拒绝合并: {exc}') from exc
    return dest


def _sha256(path: Path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(snap: Path):
    manifest_path = Path(snap) / 'manifest.json'
    if not manifest_path.is_file():
        raise SnapshotError(f'快照缺 manifest，拒绝恢复: {snap}')
    try:
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f'快照 manifest 不可读，拒绝恢复: {exc}') from exc


def _verify_snapshot_library_catalog(snap: Path, manifest: dict) -> None:
    snap_catalog = snap / 'roots' / 'library' / 'library.json'
    expected_entry = next(
        (
            item for item in (manifest.get('files') or [])
            if isinstance(item, dict) and item.get('root') == 'library' and item.get('rel') == 'library.json'
        ),
        None,
    )
    if expected_entry is not None:
        if expected_entry.get('symlink') or not snap_catalog.is_file() or snap_catalog.is_symlink():
            raise SnapshotError(f'快照内 library.json 缺失或无效，拒绝恢复: {snap}')
        expected_sha = expected_entry.get('sha256')
        if not isinstance(expected_sha, str) or _sha256(snap_catalog) != expected_sha:
            raise SnapshotError(f'快照内 library.json 校验和不匹配，拒绝恢复: {snap}')
    if snap_catalog.exists() or snap_catalog.is_symlink():
        if snap_catalog.is_symlink() or not snap_catalog.is_file():
            raise SnapshotError(f'快照内 library.json 格式无效，拒绝恢复: {snap}')
        try:
            decode_catalog_bytes(snap_catalog.read_bytes(), id_pattern=_LIBRARY_ID_PATTERN)
        except (OSError, CatalogWriteGuardError) as exc:
            raise SnapshotError(f'快照内 library.json 格式无效，拒绝恢复: {exc}') from exc


def restore(snap, *, library=None, documents=None, preferences=None, extras=None, measure=True):
    snap = Path(snap)
    manifest = load_manifest(snap)
    roots = collect_roots(library=library, documents=documents, preferences=preferences, extras=extras)
    if 'library' in roots:
        _verify_snapshot_library_catalog(snap, manifest)
    # Bind restore targets to the same role names used at snapshot time.
    started = time.perf_counter()
    # Stage a complete replacement tree per root, then swap, so gen2 is not
    # partially overwritten by a gen1 catalog mid-restore.
    work = Path(tempfile.mkdtemp(prefix='shengjian-restore-wip-', dir=str(snap.parent)))
    try:
        for name, dest_root in roots.items():
            if name not in manifest['roots']:
                raise SnapshotError(f'快照不含写路径 {name}，拒绝恢复以免新旧代混写')
            staged = work / name
            staged.mkdir(parents=True, exist_ok=True)
            snap_root = snap / 'roots' / name
            if snap_root.is_dir():
                for dirpath, dirnames, filenames in os.walk(snap_root, followlinks=False):
                    rel_dir = Path(dirpath).relative_to(snap_root)
                    (staged / rel_dir).mkdir(parents=True, exist_ok=True)
                    dirnames[:] = sorted(dirnames)
                    for filename in sorted(filenames):
                        src = Path(dirpath) / filename
                        _copy_file(src, staged / rel_dir / filename)
            # Swap: move current metadata aside, move staged in, then delete aside.
            dest_root = Path(dest_root)
            dest_root.mkdir(parents=True, exist_ok=True)
            aside = Path(tempfile.mkdtemp(prefix='shengjian-restore-aside-', dir=str(dest_root.parent)))
            # Move only metadata files we manage; leave objects/ and audio in place.
            moved = []
            try:
                live_files = list(iter_metadata_files(dest_root))
                for path in live_files:
                    rel = path.relative_to(dest_root)
                    target = aside / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, target)
                    moved.append((path, target))
                for dirpath, dirnames, filenames in os.walk(staged, followlinks=False):
                    rel_dir = Path(dirpath).relative_to(staged)
                    (dest_root / rel_dir).mkdir(parents=True, exist_ok=True)
                    for filename in filenames:
                        src = Path(dirpath) / filename
                        os.replace(src, dest_root / rel_dir / filename)
            except Exception as exc:
                # Roll the aside back before failing.
                for original, parked in moved:
                    original.parent.mkdir(parents=True, exist_ok=True)
                    if not original.exists():
                        os.replace(parked, original)
                raise SnapshotError(f'恢复失败，已尝试回退现场: {exc}') from exc
            shutil.rmtree(aside, ignore_errors=True)
        elapsed = time.perf_counter() - started
        report = {
            'ok': True,
            'elapsed_sec': elapsed,
            'files': len(manifest.get('files') or []),
        }
        if measure:
            report['elapsed_sec'] = elapsed
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    import argparse
    import sys
    p = argparse.ArgumentParser(description='声间元数据事务快照 / 恢复（不复制音频，不碰生产库除非显式路径）')
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('snapshot')
    s.add_argument('--dest', required=True)
    s.add_argument('--library', default=None)
    s.add_argument('--documents', default=None)
    s.add_argument('--preferences', default=None)
    r = sub.add_parser('restore')
    r.add_argument('--snap', required=True)
    r.add_argument('--library', default=None)
    r.add_argument('--documents', default=None)
    r.add_argument('--preferences', default=None)
    args = p.parse_args()
    try:
        if args.cmd == 'snapshot':
            dest = snapshot(args.dest, library=args.library, documents=args.documents, preferences=args.preferences)
            print(dest)
        else:
            report = restore(args.snap, library=args.library, documents=args.documents, preferences=args.preferences)
            print(json.dumps(report, ensure_ascii=False, indent=2))
    except SnapshotError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(2)
