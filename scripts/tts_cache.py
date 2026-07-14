"""Content-addressed, provider-isolated cache for synthesized fragments."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable

from tts_backends import SynthesisRequest


class FragmentCache:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: Path,
        backend_fingerprint: dict[str, object],
        suffix: str,
    ) -> None:
        self.root = root
        self.backend_fingerprint = backend_fingerprint
        self.suffix = suffix

    def key_for(self, request: SynthesisRequest) -> str:
        payload = {
            "schema": self.SCHEMA_VERSION,
            "backend": self.backend_fingerprint,
            "text": request.text,
            "voice_slot": request.voice_slot,
            "style": request.style,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def path_for(self, request: SynthesisRequest) -> Path:
        key = self.key_for(request)
        backend = str(self.backend_fingerprint.get("backend", "unknown"))
        model = str(self.backend_fingerprint.get("model", "unknown"))
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{backend}-{model}")
        return self.root / safe / key[:2] / f"{key}{self.suffix}"

    def get(
        self,
        request: SynthesisRequest,
        validator: Callable[[Path], bool],
    ) -> Path | None:
        path = self.path_for(request)
        if not path.is_file():
            return None
        if path.stat().st_size > 0 and validator(path):
            return path
        path.unlink(missing_ok=True)
        return None

    def store(self, request: SynthesisRequest, generated: Path) -> Path:
        if not generated.is_file() or generated.stat().st_size == 0:
            raise RuntimeError("拒绝缓存空音频片段")
        destination = self.path_for(request)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.part"
        )
        try:
            shutil.copy2(generated, staged)
            os.replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
        return destination
