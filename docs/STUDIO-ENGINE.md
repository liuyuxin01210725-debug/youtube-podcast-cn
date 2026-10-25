# Studio engine contract

Run `.venv/bin/python scripts/studio_engine.py --job-dir /absolute/job --config /absolute/config.json [--action prepare|render|run] [--preview 3]`.
The caller must serialize runs per job and serialize ML jobs globally on a 16 GB Mac. Use an absolute owned job directory. No HTTP server is introduced here.

Config (credentials must never be in this file):

```json
{"source":{"kind":"local","value":"/absolute/English.wav"},"provider":"deepseek","tts":"edge","speaker_count":2}
```

Use `source.kind=youtube` and the exact video URL. Optional `tts` is `edge` (default), `clone`, or `qwen3-api`; `style` is `conversation` or `documentary`. Optional `clone_model`, `asr_model` override defaults. Official defaults resolve pinned, already downloaded HF snapshots, with no model network downloads during jobs. `.speech-venv/bin/python` runs MLX; legacy `.venv` runs the orchestrator, `.diarization-venv` remains managed by the existing runtime. Translation credentials resolve environment then the existing macOS keychain service/account; never pass keys via arguments.

`prepare` writes `document.json` without synthesizing. `run` prepares if needed then synthesizes; `render` reuses a prepared document. Render never retranslates edited Chinese text. To change source/translation provider, create a new job or explicitly run prepare. A job's source is immutable by caller contract. `--preview N` renders the first N segments to `preview.mp3`; the default full export is `output.mp3`.

Document schema version 1 contains `segments` (stable id, start_ms, end_ms, English `text`, editable `chinese`, editable `speaker_id`, `ambiguous`), `speakers` (id -> editable voice_slot), `references` (speaker id -> path, sha256, exact matching English text/start/end), and transcript_source. Source references are derived only from unambiguous complete cues wholly inside a single speaker turn without overlapping other speakers. Missing clean reference stops cloning with an actionable message; no invented speaker identity or automatic fallback. UI may let users replace a reference with another job-owned WAV and matching English text. Paths are validated to remain within the job.

Every segment cache key fingerprints Chinese text, speaker voice settings, model/backend fingerprint and reference bytes/text. Audio must decode before reuse. Changing one segment/reference only regenerates affected audio. Complete MP3 is atomically published after every selected segment and final export validate; prior output remains intact on failure. `audio-cache/`, `translations/`, `references/` persist for resume. References are explicit persistent voice samples; `temporary/` contains source/ASR media and is always removed on success, failure or cancellation. Caller should offer deletion of the whole job to remove reference samples and caches.

`progress.json` is atomic: stage, completed, total, updated_at, stage_started_at, optional eta_seconds / subtitle_fallback_reason / segment_id / output / message. Stages: transcript, diarization, asr, translation, synthesis, export, complete, failed, cancelled. Failure messages deliberately exclude arbitrary SDK exception text to protect secrets. For debugging, Python API exceptions remain available to a trusted caller; redact before UI display. Translation requests use existing bounded retries; cancellation is checked between requests. Edge/Qwen API calls may finish the current network request before cancellation completes.

Cancel by creating `job/cancel` or SIGTERM/SIGINT. Remove `cancel` before resume. Child process groups are terminated and source temp media cleaned. Exit 130 cancelled, 1 failed, 0 complete. Never SIGKILL the orchestrator if cleanup is required. Export concat uses file-backed ffmpeg inputs; whole-episode PCM is never held in Python RAM.

ASR uses fixed 60 second local chunks and absolute timestamps. A word crossing a hard boundary may be less accurate; this release does not claim a word-perfect transcript. Subtitle-first acquisition falls back to local ASR when English tracks are unavailable/unusable. Diarization remains mandatory; ambiguous assignments are exposed for review, and never used as clone references.

Terminal failures retain `failed_stage` and a bounded `error_code` (`source_access_denied`, `network_unavailable`, `model_missing`, `dependency_missing`, `invalid_input`, `process_failed`, or stage-specific failure). Only recognized stderr patterns map to fixed actionable Chinese messages; raw subprocess/provider diagnostics never enter progress. YouTube429/login failures suggest waiting or importing an already downloaded local file. No browser-cookie or keychain-permission automation is performed.

Reference generation uses full original VTT cues independently of rolling-caption deduplication used by translation; overlapping raw cues are never concatenated into repeated reference text. Anonymous speakers receive successive configured system voice slots (`male_1`, `male_2`, `male_3`, `female_1`, `female_2`); these are synthesis presets, not inferred identities or genders. The five-slot pool cycles if more speakers are present and remains user-editable. Selected style is part of every synthesis cache identity.

### 长访谈的声纹参数

任务 `config.json` 可设置 `diarization_threshold`（默认 0.5，合法范围 0–2，不含边界）。这个参数传给隔离的 sherpa worker，仅影响当前任务重新准备译稿时的聚类。数值需要结合原声证据校验，不能通过提高阈值或指定人数来掩盖真实的不同说话人。已完成译稿不会因修改该值自动重算。

2026-10-18 实测 YouTube MM-C3JqCXBk：默认阈值发生过度拆分；0.9 能完成整期分析，但仍有短句分类错误，需要对照清晰参考原声复核。此参数不代表自动声纹识别质量已达到商业配音服务。
