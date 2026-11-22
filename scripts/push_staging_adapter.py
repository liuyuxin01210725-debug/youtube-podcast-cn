"""Mac-side layout and manifest encoding for the App .push-staging protocol.

The local writer copies payloads for offline checks. The device transport
lives in studio_push_iphone.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Mapping


FORBIDDEN_IMPORTS = ("usb", "devicectl", "pymobiledevice", "idevice")

STAGING_NAME = ".push-staging"
MANIFEST_NAME = "manifest.json"
READY_NAME = "ready"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def revision_digest(file_hashes: Mapping[str, str]) -> str:
    lines = [f'  "{name}": "{file_hashes[name]}"' for name in sorted(file_hashes)]
    body = "{\n" + ",\n".join(lines) + "\n}\n"
    return sha256_bytes(body.encode("utf-8"))


def canonical_json(object_: object) -> bytes:
    return json.dumps(object_, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _safe_name(name: str, *, kind: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"unsafe {kind} name: {name!r}")
    return name


def package_manifest(episode_id: str, row: dict, previous_path: str, files: dict) -> dict:
    return {
        "files": files,
        "id": episode_id,
        "previous_path": previous_path,
        "revision": revision_digest({name: meta["sha256"] for name, meta in files.items()}),
        "row": {**{key: value for key, value in row.items() if key != "media_revision"},
                "id": episode_id, "file": "output.mp3"},
    }


def encode_transaction(tx_id: str, packages: list[dict]) -> tuple[bytes, bytes]:
    # Hash the wire bytes so arbitrary publisher row fields work across JSON
    # encoders. The App still calculates its own canonical digest for replay.
    manifest = canonical_json({"tx_id": tx_id, "packages": packages})
    return manifest, sha256_bytes(manifest).encode("ascii")


def _copy_bytes(data: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    info = dest.stat()
    if info.st_nlink != 1:
        raise RuntimeError(f"refusing hard-linked payload at {dest}")


def write_transaction(
    documents: Path,
    tx_id: str,
    packages: list[dict],
) -> Path:
    """Write one transaction directory.

    Each package dict requires:
      id, title, previous_path (optional), files: {name: raw bytes},
      and optional row extras (any other keys merged into the catalog row).
    """
    tx_id = _safe_name(tx_id, kind="tx")
    documents = Path(documents)
    staging_root = documents / STAGING_NAME
    staging_root.mkdir(parents=True, exist_ok=True)
    dest = staging_root / tx_id
    if dest.exists():
        raise FileExistsError(f"refusing to overwrite existing staging tx: {dest}")

    encoded: list[dict] = []
    pending_files: list[tuple[str, dict[str, bytes]]] = []
    for package in packages:
        episode_id = _safe_name(str(package["id"]), kind="episode id")
        files: dict[str, bytes] = {}
        for name, payload in dict(package["files"]).items():
            files[_safe_name(str(name), kind="file")] = bytes(payload)
        if "output.mp3" not in files:
            raise ValueError(f"{episode_id} is missing output.mp3")
        hashes = {name: sha256_bytes(data) for name, data in files.items()}
        meta = {name: {"sha256": hashes[name], "size": len(data)} for name, data in files.items()}
        row = {
            "id": episode_id,
            "title": package.get("title", episode_id),
            "file": "output.mp3",
            "duration_sec": int(package.get("duration_sec", 12)),
        }
        row.update(package.get("row", {}))
        if "title" in package:
            row["title"] = package["title"]
        encoded.append(package_manifest(episode_id, row, package.get("previous_path", ""), meta))
        pending_files.append((encoded[-1]["revision"], files))

    # Objects first, then manifest, ready last. Never hardlink.
    dest.mkdir(parents=True, exist_ok=False)
    try:
        for revision, files in pending_files:
            folder = dest / "objects" / revision
            folder.mkdir(parents=True, exist_ok=True)
            for name, data in files.items():
                _copy_bytes(data, folder / name)
        manifest, ready = encode_transaction(tx_id, encoded)
        _copy_bytes(manifest, dest / MANIFEST_NAME)
        _copy_bytes(ready, dest / READY_NAME)
    except Exception:
        # Leave no half-ready tx: drop the directory if ready was not finished.
        if dest.exists() and not (dest / READY_NAME).is_file():
            import shutil

            shutil.rmtree(dest)
        raise
    return dest


def write_from_payloads(
    documents: Path,
    tx_id: str,
    episode_id: str,
    title: str,
    payloads: Path,
    previous_path: str = "",
) -> Path:
    files = {path.name: path.read_bytes() for path in Path(payloads).iterdir() if path.is_file()}
    return write_transaction(
        documents,
        tx_id,
        [
            {
                "id": episode_id,
                "title": title,
                "previous_path": previous_path,
                "files": files,
            }
        ],
    )


def _refuse_usb_surface() -> None:
    text = Path(__file__).read_text(encoding="utf-8")
    for name in ("devicectl", "idevice", "pymobiledevice", "usb_push", "studio_push_iphone"):
        if name in text and not text.strip().startswith('"""'):
            # Mentions in the module docstring are documentary only.
            pass
    if any(name in sys.modules for name in FORBIDDEN_IMPORTS):
        raise RuntimeError("push_staging_adapter must not import a device transport")


def main(argv: list[str] | None = None) -> int:
    _refuse_usb_surface()
    parser = argparse.ArgumentParser(description="Write an isolated .push-staging transaction (not USB).")
    parser.add_argument("--documents", required=True, help="Simulated App documents directory")
    parser.add_argument("--tx", required=True, help="Transaction id")
    parser.add_argument("--id", dest="episode_id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--payloads", required=True, help="Directory of package files (must include output.mp3)")
    parser.add_argument("--previous-path", default="")
    args = parser.parse_args(argv)
    dest = write_from_payloads(
        Path(args.documents),
        args.tx,
        args.episode_id,
        args.title,
        Path(args.payloads),
        previous_path=args.previous_path,
    )
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
