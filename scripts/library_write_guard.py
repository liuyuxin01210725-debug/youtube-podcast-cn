"""Shared fail-closed write guard for 声间 library.json catalog writes."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import stat

_IGNORED_EMPTY_NAMES = frozenset({'.DS_Store', '.localized'})


class CatalogWriteGuardError(ValueError):
    """写盘硬熔断错误。继承 ValueError 便于推机侧直接兼容；消息脱敏可稳定匹配。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CatalogBaseline:
    """已通过读取或空库核实的旧目录基线。禁止调用方随意伪造未核实的 None。"""

    old_index: bytes | None
    prior: dict
    old_ids: frozenset[str]
    verified_empty: bool

    @property
    def old_count(self) -> int:
        return len(self.old_ids)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f'duplicate key: {key}')
        obj[key] = value
    return obj


def _reject_constant(token: str):
    raise ValueError(f'invalid JSON constant: {token}')


def decode_catalog_bytes(raw: bytes, *, id_pattern: re.Pattern[str]) -> tuple[dict, dict[str, dict]]:
    """Strictly decode and validate a library.json payload."""
    try:
        text = raw.decode('utf-8')
        prior = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(prior, dict):
            raise ValueError('catalog root must be an object')
        json.dumps(prior, allow_nan=False, ensure_ascii=False).encode('utf-8')
        episodes = prior.get('episodes')
        if not isinstance(episodes, list):
            raise ValueError('episodes must be a list')
        by_id: dict[str, dict] = {}
        for item in episodes:
            if not isinstance(item, dict):
                raise ValueError('episode entry must be an object')
            ident = next(
                (item[key] for key in ('id', 'episode_id', 'source_job_id') if item.get(key) is not None),
                None,
            )
            if not isinstance(ident, str) or not id_pattern.fullmatch(ident) or ident in by_id:
                raise ValueError('invalid or duplicate episode id')
            by_id[ident] = item
        return prior, by_id
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise CatalogWriteGuardError('现有资料库索引格式无效', code='catalog_corrupt') from None


def _read_regular_file_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CatalogWriteGuardError('现有资料库索引格式无效', code='catalog_corrupt')
        with os.fdopen(fd, 'rb', closefd=True) as stream:
            fd = -1
            return stream.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _has_icloud_placeholder(folder: Path, target_name: str | None = None) -> bool:
    try:
        entries = os.listdir(folder)
    except OSError:
        return True
    if target_name is not None:
        expected = f'.{target_name}.icloud'.casefold()
        return any(name.casefold() == expected for name in entries)
    return any(name.casefold().endswith('.icloud') for name in entries)


def verify_empty_local_library(library: Path) -> bool:
    """Return True only when the local/iCloud library is provably a brand-new empty directory."""
    try:
        if library.is_symlink():
            return False
        parent = library.parent
        if parent.is_symlink() or not parent.is_dir():
            return False
        parent_entries = os.listdir(parent)
        placeholder = f'.{library.name}.icloud'.casefold()
        if any(name.casefold() == placeholder for name in parent_entries):
            return False
        if not (library.exists() or library.is_symlink()):
            target = library.name.casefold()
            return not any(name.casefold() == target for name in parent_entries)
        if not library.is_dir():
            return False
        entries = [name for name in os.listdir(library) if name not in _IGNORED_EMPTY_NAMES]
        return len(entries) == 0
    except OSError:
        return False


def ensure_no_evicted_library_placeholder(library: Path) -> None:
    """Reject mkdir if the library folder itself was evicted to an iCloud placeholder."""
    if library.is_symlink():
        raise CatalogWriteGuardError('现有资料库索引缺失且目录未核实为空，拒绝写入', code='catalog_missing_unverified_empty')
    if library.exists():
        return
    parent = library.parent
    try:
        if parent.is_symlink() or not parent.is_dir():
            raise CatalogWriteGuardError('现有资料库索引缺失且目录未核实为空，拒绝写入', code='catalog_missing_unverified_empty')
        parent_entries = os.listdir(parent)
    except OSError:
        raise CatalogWriteGuardError('现有资料库索引缺失且目录未核实为空，拒绝写入', code='catalog_missing_unverified_empty') from None
    placeholder = f'.{library.name}.icloud'.casefold()
    if any(name.casefold() == placeholder for name in parent_entries):
        raise CatalogWriteGuardError('现有资料库索引缺失且目录未核实为空，拒绝写入', code='catalog_missing_unverified_empty')


def load_local_catalog_baseline(
    library: Path,
    *,
    id_pattern: re.Pattern[str],
    reader=None,
    listing: Path | None = None,
) -> CatalogBaseline:
    """Fail-closed read of a catalog listing, or verified empty library."""
    listing = library / 'library.json' if listing is None else listing
    try:
        mode = listing.lstat().st_mode
    except FileNotFoundError:
        if verify_empty_local_library(library):
            return CatalogBaseline(
                old_index=None,
                prior={},
                old_ids=frozenset(),
                verified_empty=True,
            )
        raise CatalogWriteGuardError('现有资料库索引缺失且目录未核实为空，拒绝写入', code='catalog_missing_unverified_empty') from None
    except OSError:
        raise CatalogWriteGuardError('无法读取现有资料库索引', code='catalog_unreadable') from None

    if not stat.S_ISREG(mode):
        raise CatalogWriteGuardError('现有资料库索引格式无效', code='catalog_corrupt')

    try:
        raw = reader(listing) if reader is not None else _read_regular_file_bytes(listing)
    except CatalogWriteGuardError:
        raise
    except OSError:
        raise CatalogWriteGuardError('无法读取现有资料库索引', code='catalog_unreadable') from None
    except ValueError:
        raise CatalogWriteGuardError('现有资料库索引格式无效', code='catalog_corrupt') from None

    prior, by_id = decode_catalog_bytes(raw, id_pattern=id_pattern)
    return CatalogBaseline(
        old_index=raw,
        prior=prior,
        old_ids=frozenset(by_id),
        verified_empty=False,
    )


def build_remote_catalog_baseline(
    raw: bytes | None,
    *,
    verified_empty: bool,
    id_pattern: re.Pattern[str],
) -> CatalogBaseline:
    """Build a verified baseline for a remote/device catalog."""
    if raw is None:
        if verified_empty is True:
            return CatalogBaseline(
                old_index=None,
                prior={},
                old_ids=frozenset(),
                verified_empty=True,
            )
        raise CatalogWriteGuardError('现有手机资料库索引缺失且未核实为空，拒绝写入', code='catalog_missing_unverified_empty')
    prior, by_id = decode_catalog_bytes(raw, id_pattern=id_pattern)
    return CatalogBaseline(
        old_index=raw,
        prior=prior,
        old_ids=frozenset(by_id),
        verified_empty=False,
    )


def verify_catalog_transition(
    baseline: CatalogBaseline,
    new_bytes: bytes,
    *,
    id_pattern: re.Pattern[str],
) -> tuple[dict, dict[str, dict]]:
    """Enforce Gate 1 (no shrink / no dropped IDs) and Gate 2 (verified baseline)."""
    if baseline.old_index is None and not baseline.verified_empty:
        raise CatalogWriteGuardError('缺少现有资料库索引基线，拒绝写入', code='catalog_baseline_missing')
    try:
        prior, new_by_id = decode_catalog_bytes(new_bytes, id_pattern=id_pattern)
    except CatalogWriteGuardError:
        raise CatalogWriteGuardError('待写入资料库索引格式无效，拒绝写入', code='catalog_candidate_invalid') from None
    new_ids = frozenset(new_by_id)
    if len(new_ids) < baseline.old_count:
        raise CatalogWriteGuardError(
            f'拒绝写入资料库索引：新目录集数（{len(new_ids)}）少于现有目录（{baseline.old_count}）',
            code='catalog_episode_count_shrink',
        )
    missing = baseline.old_ids - new_ids
    if missing:
        raise CatalogWriteGuardError(
            f'拒绝写入资料库索引：新目录缺少现有节目（缺 {len(missing)} 集）',
            code='catalog_episode_dropped',
        )
    return prior, new_by_id


def _verify_empty_library_pre_commit(
    library: Path,
    new_by_id: dict[str, dict],
    *,
    allowed_temp_names: frozenset[str],
) -> None:
    if library.is_symlink() or not library.is_dir():
        raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')
    if _has_icloud_placeholder(library.parent, library.name) or _has_icloud_placeholder(library):
        raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')
    allowed_top = {'episodes', 'revisions', 'objects', *_IGNORED_EMPTY_NAMES, *allowed_temp_names}
    try:
        top_entries = os.listdir(library)
    except OSError:
        raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed') from None
    for name in top_entries:
        if name not in allowed_top:
            raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')

    episodes_dir = library / 'episodes'
    if episodes_dir.exists() or episodes_dir.is_symlink():
        if episodes_dir.is_symlink() or not episodes_dir.is_dir():
            raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')
        try:
            ep_entries = os.listdir(episodes_dir)
        except OSError:
            raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed') from None
        for name in ep_entries:
            if name in _IGNORED_EMPTY_NAMES:
                continue
            if name not in new_by_id:
                raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')

    referenced_paths: set[str] = set()
    for row in new_by_id.values():
        rev = row.get('media_revision')
        if isinstance(rev, dict) and isinstance(rev.get('path'), str):
            referenced_paths.add(rev['path'])

    for root_name in ('objects', 'revisions'):
        sub_dir = library / root_name
        if not (sub_dir.exists() or sub_dir.is_symlink()):
            continue
        if sub_dir.is_symlink() or not sub_dir.is_dir():
            raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')
        try:
            entries = [name for name in os.listdir(sub_dir) if name not in _IGNORED_EMPTY_NAMES]
        except OSError:
            raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed') from None
        if root_name == 'objects':
            for sha in entries:
                if f'objects/{sha}' not in referenced_paths:
                    raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')
        else:
            for ident in entries:
                ident_dir = sub_dir / ident
                if ident_dir.is_symlink() or not ident_dir.is_dir():
                    raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')
                try:
                    rev_entries = [name for name in os.listdir(ident_dir) if name not in _IGNORED_EMPTY_NAMES]
                except OSError:
                    raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed') from None
                for sha in rev_entries:
                    if f'revisions/{ident}/{sha}' not in referenced_paths:
                        raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')


def _default_sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_guarded_catalog(
    listing: Path,
    payload_bytes: bytes,
    *,
    baseline: CatalogBaseline,
    id_pattern: re.Pattern[str],
    reader=None,
    allowed_temp_names: frozenset[str] = frozenset(),
    sync_dir=None,
) -> None:
    """Verify transition, re-check baseline on disk (CAS), and atomically replace library.json."""
    _, new_by_id = verify_catalog_transition(baseline, payload_bytes, id_pattern=id_pattern)
    library = listing.parent

    if baseline.old_index is not None:
        try:
            mode = listing.lstat().st_mode
        except FileNotFoundError:
            raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed') from None
        except OSError:
            raise CatalogWriteGuardError('无法读取现有资料库索引', code='catalog_unreadable') from None
        if not stat.S_ISREG(mode):
            raise CatalogWriteGuardError('现有资料库索引格式无效', code='catalog_corrupt')
        try:
            actual = reader(listing) if reader is not None else _read_regular_file_bytes(listing)
        except CatalogWriteGuardError:
            raise
        except OSError:
            raise CatalogWriteGuardError('无法读取现有资料库索引', code='catalog_unreadable') from None
        except ValueError:
            raise CatalogWriteGuardError('现有资料库索引格式无效', code='catalog_corrupt') from None
        if actual != baseline.old_index:
            raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')
    else:
        if listing.exists() or listing.is_symlink():
            raise CatalogWriteGuardError('资料库索引已变化，请重新核对再发布', code='catalog_baseline_changed')
        _verify_empty_library_pre_commit(
            library,
            new_by_id,
            allowed_temp_names=allowed_temp_names,
        )

    tmp = listing.with_name(f'.{listing.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        try:
            with os.fdopen(fd, 'wb', closefd=True) as handle:
                fd = -1
                handle.write(payload_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        os.replace(tmp, listing)
        (sync_dir or _default_sync_dir)(library)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
