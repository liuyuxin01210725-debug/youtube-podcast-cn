#!/usr/bin/env python3
"""Build a local, non-exportable simulator library from read-only backups.

Sanitizes private prose in listen notes, book notes, and insights. Preserves
UTF-16 length, newlines, punctuation, IDs, types, and status. Copies the gen2
catalog, referenced objects, previous_path trees, book_state, preferences, and
push-staging unchanged. Never writes back to the source backups. Recalculates
no object or catalog hashes. The output is for this machine only and must not
be uploaded or committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path


LETTER_BMP = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"
LETTER_ASTRAL = "\U0001F600"

DEFAULT_CATALOG_DIR = Path(
    "/Users/liuyuxin/Documents/bot-evolution-review/shengjian-unified-backend-20261120/"
    "evidence/device-accept-t568u-0c259eeb-20261122/t594u-slot/shengjian-pre"
)
DEFAULT_OBJECTS_ROOT = Path(
    "/Users/liuyuxin/Library/Application Support/PrivateListen/device-full-backups/"
    "t569u-pre-install-0c259eeb/20261122T101436-0800/Documents/中文播客私人库"
)
DEFAULT_INSIGHTS = Path(
    "/Users/liuyuxin/Library/Application Support/PrivateListen/device-full-backups/"
    "t569u-pre-install-0c259eeb/20261122T101436-0800/Documents/listen_note_insights.json"
)
DEFAULT_PUSH_STAGING = Path(
    "/Users/liuyuxin/Documents/bot-evolution-review/shengjian-unified-backend-20261120/"
    "evidence/device-accept-t568u-0c259eeb-20261122/t570u-activate/post-activate/"
    "Documents/.push-staging"
)

TEXT_KEYS = ("body", "headline", "summary")
LIST_TEXT_KEYS = ("tags", "keywords")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def keep_char(char: str) -> bool:
    if char in "\n\r\t":
        return True
    category = unicodedata.category(char)
    return category[0] in "MNPSZ"


def sanitize_text(text: str) -> str:
    if not text:
        return text
    out: list[str] = []
    cursor = 0
    for char in text:
        if keep_char(char):
            out.append(char)
            continue
        units = utf16_len(char)
        if units == 1:
            out.append(LETTER_BMP[cursor % len(LETTER_BMP)])
            cursor += 1
        elif units == 2:
            out.append(LETTER_ASTRAL)
        else:
            out.append((LETTER_ASTRAL * (units // 2)) + (LETTER_BMP[0] if units % 2 else ""))
    result = "".join(out)
    if utf16_len(result) != utf16_len(text):
        raise RuntimeError("UTF-16 length changed during sanitization")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_tree(root: Path) -> dict[str, tuple[int, int, int]]:
    rows: dict[str, tuple[int, int, int]] = {}
    if not root.exists():
        return rows
    if root.is_file():
        stat = root.stat()
        rows[str(root)] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        return rows
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        rows[str(path)] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return rows


def clone_or_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        raise FileExistsError(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["cp", "-cR", str(src), str(dst)], capture_output=True, text=True)
    if result.returncode == 0:
        return
    if src.is_dir():
        shutil.copytree(src, dst, copy_function=shutil.copy2)
    else:
        shutil.copy2(src, dst)


def load_json(path: Path):
    return json.loads(path.read_text())


def dump_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def sanitize_note_map(notes: dict) -> int:
    changed = 0
    for record in notes.values():
        if not isinstance(record, dict):
            continue
        for key in TEXT_KEYS:
            if isinstance(record.get(key), str):
                cleaned = sanitize_text(record[key])
                if cleaned != record[key]:
                    record[key] = cleaned
                    changed += 1
        for key in LIST_TEXT_KEYS:
            values = record.get(key)
            if not isinstance(values, list):
                continue
            cleaned_list = []
            for item in values:
                if isinstance(item, str):
                    cleaned = sanitize_text(item)
                    if cleaned != item:
                        changed += 1
                    cleaned_list.append(cleaned)
                else:
                    cleaned_list.append(item)
            record[key] = cleaned_list
    return changed


def unique_search_needle(titles: list[tuple[str, str]]) -> tuple[str, str]:
    """Return (episode_id, needle) where needle matches exactly one title."""
    body = [(episode_id, title) for episode_id, title in titles if title]
    for episode_id, title in sorted(body, key=lambda row: (-len(row[1]), row[0])):
        if sum(title in other or other in title for _, other in body) == 1:
            return episode_id, title
    for size in range(4, 32):
        seen: dict[str, list[str]] = {}
        for episode_id, title in body:
            if len(title) < size:
                continue
            for index in range(0, len(title) - size + 1):
                token = title[index : index + size]
                if token.isspace():
                    continue
                seen.setdefault(token, []).append(episode_id)
        unique = [(token, ids[0]) for token, ids in seen.items() if len(set(ids)) == 1]
        unique.sort(key=lambda row: (-len(row[0]), row[0]))
        if unique:
            token, episode_id = unique[0]
            return episode_id, token
    if not body:
        raise SystemExit("catalog has no titles to use as a local search needle")
    return body[0]


def referenced_paths(catalog: dict, objects_root: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for episode in catalog.get("episodes") or []:
        revision = episode.get("media_revision") or {}
        for key in ("path", "previous_path"):
            relative = revision.get(key)
            if not relative:
                continue
            path = objects_root / relative
            if path in seen:
                continue
            seen.add(path)
            found.append(path)
        # Legacy rows without media_revision still live under episodes/<id>.
        episode_id = episode.get("id")
        if episode_id and not (episode.get("media_revision") or {}).get("path"):
            path = objects_root / "episodes" / episode_id
            if path not in seen:
                seen.add(path)
                found.append(path)
    return found


def write_notice(output: Path) -> None:
    (output / "DO-NOT-EXPORT.txt").write_text(
        "仅本机保结构测试库，不可外传。原始备份未改。对象载荷未改哈希。"
        "不得上传、入库或发给模型。\n",
        encoding="utf-8",
    )


def select_regression_ids(catalog: dict, notes: dict, progress: dict) -> dict:
    episodes = {row["id"]: row for row in catalog.get("episodes") or [] if row.get("id")}
    titles = [(row["id"], row.get("title") or "") for row in catalog.get("episodes") or []]
    search_id, needle = unique_search_needle(titles)
    note_lengths = sorted(
        (
            (utf16_len((record or {}).get("body") or ""), episode_id)
            for episode_id, record in (notes or {}).items()
            if episode_id in episodes
        ),
        reverse=True,
    )
    long_note_id = note_lengths[0][1] if note_lengths else search_id
    unfinished: list[str] = []
    for key, value in (progress or {}).items():
        if not isinstance(value, dict) or "." in key or value.get("finished"):
            continue
        if key in episodes:
            unfinished.append(key)
    unfinished_id = unfinished[0] if unfinished else ""
    return {
        "search_episode_id": search_id,
        "search_needle": needle,
        "long_note_episode_id": long_note_id,
        "long_note_utf16_len": note_lengths[0][0] if note_lengths else 0,
        "unfinished_episode_id": unfinished_id,
        "unfinished_title": (episodes.get(unfinished_id) or {}).get("title") or "",
        "unfinished_duration_sec": (episodes.get(unfinished_id) or {}).get("duration_sec"),
        "unfinished_count": len(unfinished),
    }


def load_progress(plist_path: Path) -> dict:
    import plistlib

    values = plistlib.loads(plist_path.read_bytes())
    raw = values.get("PrivateListen.progress")
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(raw)
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {}


def build(output: Path, catalog_dir: Path, objects_root: Path, insights_path: Path, push_staging: Path) -> dict:
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    catalog_path = catalog_dir / "library.json"
    catalog = load_json(catalog_path)
    schema = catalog.get("schema")
    generation = catalog.get("generation")
    if schema != "private-listen.library.v2" or generation != 2:
        raise SystemExit(f"refuse: catalog is not gen2 (schema={schema!r}, generation={generation!r})")

    source_prints = {
        "catalog": fingerprint_tree(catalog_path),
        "objects": fingerprint_tree(objects_root),
        "insights": fingerprint_tree(insights_path),
        "push_staging": fingerprint_tree(push_staging) if push_staging.exists() else {},
    }

    documents = output / "Documents"
    library = documents / "中文播客私人库"
    library.mkdir(parents=True)
    clone_or_copy(catalog_path, library / "library.json")

    copied = []
    missing = []
    for src in referenced_paths(catalog, objects_root):
        dst = library / src.relative_to(objects_root)
        if not src.exists():
            missing.append(str(src.relative_to(objects_root)))
            continue
        clone_or_copy(src, dst)
        copied.append(str(src.relative_to(objects_root)))

    if push_staging.exists():
        clone_or_copy(push_staging, documents / ".push-staging")

    notes_changed = 0
    listen_src = catalog_dir / "listen_notes.json"
    listen = load_json(listen_src)
    notes_changed += sanitize_note_map(listen.get("notes") or {})
    dump_json(documents / "listen_notes.json", listen)

    book_src = catalog_dir / "book_notes.v1.json"
    if book_src.exists():
        book_notes = load_json(book_src)
        notes_changed += sanitize_note_map(book_notes.get("notes") or {})
        dump_json(documents / "book_notes.v1.json", book_notes)

    book_state = catalog_dir / "book_state.v1.json"
    if book_state.exists():
        clone_or_copy(book_state, documents / "book_state.v1.json")

    insights_changed = 0
    if insights_path.exists():
        insights = load_json(insights_path)
        insights_changed = sanitize_note_map(insights.get("insights") or {})
        dump_json(documents / "listen_note_insights.json", insights)

    plist_src = catalog_dir / "com.liuyuxin.privatelisten.plist"
    prefs = output / "Library" / "Preferences" / "com.liuyuxin.privatelisten.plist"
    if plist_src.exists():
        clone_or_copy(plist_src, prefs)

    progress = load_progress(plist_src) if plist_src.exists() else {}
    selection = select_regression_ids(catalog, listen.get("notes") or {}, progress)
    dump_json(output / "selection.json", selection)
    write_notice(output)

    after = {
        "catalog": fingerprint_tree(catalog_path),
        "objects": fingerprint_tree(objects_root),
        "insights": fingerprint_tree(insights_path),
        "push_staging": fingerprint_tree(push_staging) if push_staging.exists() else {},
    }
    if after != source_prints:
        raise SystemExit("source backups changed during sanitizer run; aborting")

    catalog_sha = sha256_file(library / "library.json")
    source_catalog_sha = sha256_file(catalog_path)
    manifest = {
        "notice": "仅本机保结构测试库，不可外传",
        "catalog_schema": schema,
        "catalog_generation": generation,
        "catalog_sha256": catalog_sha,
        "source_catalog_sha256": source_catalog_sha,
        "catalog_unchanged": catalog_sha == source_catalog_sha,
        "book_state_unchanged": (
            sha256_file(documents / "book_state.v1.json") == sha256_file(book_state)
            if book_state.exists()
            else None
        ),
        "plist_unchanged": sha256_file(prefs) == sha256_file(plist_src) if plist_src.exists() else None,
        "objects_copied": len(copied),
        "objects_missing": missing,
        "notes_text_fields_changed": notes_changed,
        "insights_text_fields_changed": insights_changed,
        "transactions": sorted((catalog.get("transactions") or {}).keys()),
        "episode_count": len(catalog.get("episodes") or []),
        "source_backups_unchanged": True,
    }
    dump_json(output / "MANIFEST.json", manifest)
    if not manifest["catalog_unchanged"] or missing:
        raise SystemExit(f"catalog copy or object paths failed: {manifest}")
    return {"manifest": manifest, "selection": selection}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    parser.add_argument("--objects-root", type=Path, default=DEFAULT_OBJECTS_ROOT)
    parser.add_argument("--insights", type=Path, default=DEFAULT_INSIGHTS)
    parser.add_argument("--push-staging", type=Path, default=DEFAULT_PUSH_STAGING)
    args = parser.parse_args()
    result = build(
        args.output.resolve(),
        args.catalog_dir,
        args.objects_root,
        args.insights,
        args.push_staging,
    )
    json.dump(
        {"manifest": result["manifest"], "selection_keys": sorted(result["selection"])},
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
