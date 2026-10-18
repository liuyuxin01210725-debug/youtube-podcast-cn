# 中文播客工作台

## Global Constraints
- Upgrade this MIT project; reuse existing translation, diarization, credential handling. Do not copy GPL implementation.
- User wants complete English YouTube audio/video to natural Chinese dialogue, benchmark 跨国串门儿计划. No summarization, invented dialogue, identity inference or promise of identical quality.
- Apple Silicon, 16 GB RAM. Models run sequentially in isolated processes. Secrets remain keychain/environment only. Local server binds loopback, validates origin and restricts files to jobs.
- Every segment must succeed before atomically publishing MP3. Resume caches fingerprint content, voice and model; source temporary audio removed on terminal outcomes. Chinese UI and actionable errors.

## Task 1: Complete conversion engine
Implement new studio pipeline modules and worker APIs, preserving legacy CLI. Accept exact YouTube URL or existing local audio/video, acquire audio in job temp dir, subtitle-first with chunked local ASR fallback, stable speaker alignment, complete batched translation using existing client. Choose clean per-speaker source references with matching English text. Implement local MLX Qwen3 Base voice cloning, and existing Edge/Qwen system voice alternatives. Support preview and full audio, editable segment Chinese text/speaker mapping, resumable per-segment audio, bounded-memory ffmpeg concatenation, progress JSON, cancellation and cleanup. Supply command-line JSON job config interface and documentation of backend contracts for Task 2. Add meaningful failure/cache/reference/ASR tests. No new web UI in this task. Controller installs MLX environment and tests hardware while implementer codes. Default ASR mlx-whisper small English (timestamps), default clone mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit; environment .speech-venv Python 3.12. Worker gets precise runtime APIs from controller after probing.

## Task 2: Local Chinese workbench
Implement loopback server and polished static Chinese UI over Task 1. User can paste YouTube URLs (batch), select/upload local audio or video, choose original voice clone or system voices, create queued jobs and view true stage progress. Show English and editable Chinese segments with speaker mapping, preview first short section, export full MP3/transcript, retry/resume and cancel. Queue serial to fit RAM. Local browser no credentials. Preserve completed output on re-render failure; clearly distinguish preview from finished. Add launcher .command and setup/runtime checks; README accurate. Validate API paths/security/state transitions with meaningful tests. User should open launcher and use tool without terminal commands.

## Task 3: Verify and deliver
Real short English two-speaker audio -> ASR -> translation -> cloned Chinese audio; verify decodable complete audio and cached rerun. Exercise UI via Computer Use when available. Review full changes independently, fix findings, install desktop launcher and provide clickable sample/output. Do not publish or push. State verified scope and quality limits honestly.
