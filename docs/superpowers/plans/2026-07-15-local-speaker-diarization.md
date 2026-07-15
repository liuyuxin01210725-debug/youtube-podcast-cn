# Local Speaker Diarization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Temporarily download a YouTube episode's source audio, identify anonymous speakers locally with sherpa-onnx, align them to atomic English caption cues, and produce a TTS-ready Chinese speaker sequence while always deleting the source media.

**Architecture:** Separate atomic caption parsing, backend-neutral alignment, cache persistence, temporary media ownership, and sherpa execution. The main process owns the YouTube job and invokes a pinned isolated sherpa environment; only timestamp metadata persists, while compressed audio and mono WAV live in a job-scoped temporary directory removed on every exit path.

**Tech Stack:** Python 3.10+, standard-library `unittest`, `yt-dlp==2026.3.17`, FFmpeg, `sherpa-onnx==1.13.2`, `soundfile==0.14.0`, JSON metadata, macOS arm64.

## Global Constraints

- The default backend is local `sherpa-onnx`; it uses no LLM tokens, cloud quota, Hugging Face account, or access token.
- The supported target for the first release is macOS arm64, including Apple M4 with 16 GB memory.
- YouTube URL requests default to `--diarize auto`; local Chinese text and article inputs never invoke diarization.
- `auto` falls back atomically to the current Agent text-based speaker workflow; `required` stops before translation/TTS; `off` never downloads source audio.
- Source audio and converted mono 16 kHz WAV must be deleted after success, failure, cancellation, or `KeyboardInterrupt`. Inability to verify cleanup is a command failure.
- Persist only diarization turns, caption assignments, an editable speaker map, hashes, fingerprints, parameters, and warnings. Never persist raw audio, WAV, speaker embeddings, cookies, signed media URLs, or API keys.
- Preserve atomic VTT cues until speaker assignment. A speaker change is always a hard grouping boundary.
- Anonymous clusters do not identify names, roles, biological sex, or gender identity. `男声` and `女声` describe synthesis voices only.
- User edits to `speaker-map.json` are authoritative and are never overwritten.
- `.speakers.txt` must contain exactly one label for every final translated paragraph.
- The Reverb diarization model is forbidden because its non-commercial restriction conflicts with possible commercial use.
- Default assets are `pyannote/segmentation-3.0` under MIT and the 3D-Speaker ERes2Net embedding model under Apache-2.0.
- Automated tests must not access YouTube, PyPI, GitHub, model hosts, paid APIs, or TTS providers.

## File Structure

- Create `scripts/podcast_captions.py`: atomic VTT parsing, rolling-caption deduplication, and speaker-aware grouping.
- Create `scripts/diarization_core.py`: backend-neutral diarization types, overlap alignment, ambiguity rules, bounds, and quality gates.
- Create `scripts/diarization_cache.py`: cache fingerprints, atomic persistence, speaker-map preservation, and `.speakers.txt` rendering.
- Create `scripts/diarization_runtime.py`: isolated runtime discovery/setup, backend subprocess invocation, and model fingerprinting.
- Create `scripts/sherpa_diarization_worker.py`: the only module importing `sherpa_onnx` and `soundfile`.
- Create `scripts/setup_diarization.py`: pinned environment/model installation and offline smoke validation.
- Create `scripts/temporary_audio.py`: audio-only download, FFmpeg conversion, hashing, and verified cleanup.
- Create `scripts/diarization_models.json`: exact URLs, extracted file hashes, upstream licenses, and schema version.
- Create `requirements-diarization.txt` and `bin/setup-podcast-diarization.sh`.
- Modify `scripts/podcast_cn.py`: orchestrate captions, cache, diarization, fallback, translation, CLI flags, and reporting.
- Modify `scripts/tts-cn.py`: honor explicit synthesis voice numbers in generated labels.
- Modify `bin/podcast-cn.sh`, `install.sh`, `.gitignore`, `Makefile`, `README.md`, and `skills/podcast-translate/SKILL.md`.
- Create `tests/test_podcast_captions.py`, `tests/test_diarization_core.py`, `tests/test_diarization_cache.py`, `tests/test_diarization_runtime.py`, `tests/test_temporary_audio.py`, and `tests/fixtures/diarization-silence.wav`.
- Modify `tests/test_podcast_cn.py`, `tests/test_tts_cn.py`, and `tests/test_public_package.py`.

---

### Task 1: Preserve Atomic Caption Cues Until Speaker Assignment

**Files:**
- Create: `scripts/podcast_captions.py`
- Create: `tests/test_podcast_captions.py`
- Modify: `scripts/podcast_cn.py:1-210`
- Modify: `tests/test_podcast_cn.py:1-70`
- Modify: `Makefile:3-19`

**Interfaces:**
- Consumes: raw WebVTT text and optional `dict[int, str]` keyed by `CaptionPiece.source_index`.
- Produces: `CaptionPiece`, `SubtitleSegment`, `parse_vtt_pieces(text)`, `group_caption_pieces(pieces, speakers=None, ambiguous_indexes=None)`, and compatibility wrapper `parse_vtt(text)`.

- [ ] **Step 1: Write failing atomic-caption and hard-boundary tests**

```python
class AtomicCaptionTests(unittest.TestCase):
    def test_preserves_deduplicated_atomic_cues(self) -> None:
        pieces = podcast_captions.parse_vtt_pieces(
            FIXTURE.read_text(encoding="utf-8")
        )
        self.assertEqual([piece.source_index for piece in pieces], [0, 1, 2, 3, 4])
        self.assertEqual(pieces[0].text, "Hello & welcome")
        self.assertEqual(pieces[1].text, "to the show.")

    def test_speaker_change_is_a_hard_segment_boundary(self) -> None:
        pieces = [
            CaptionPiece(0, 1000, "Hello", 0),
            CaptionPiece(1000, 2000, "there.", 1),
        ]
        segments = group_caption_pieces(
            pieces, {0: "SPEAKER_00", 1: "SPEAKER_01"}
        )
        self.assertEqual([segment.text for segment in segments], ["Hello", "there."])
        self.assertEqual(
            [segment.speaker_id for segment in segments],
            ["SPEAKER_00", "SPEAKER_01"],
        )

    def test_non_diarized_wrapper_preserves_existing_output(self) -> None:
        segments = parse_vtt(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(
            [segment.text for segment in segments],
            [
                "Hello & welcome to the show.",
                "Today we talk health.",
                "Exactly.",
            ],
        )
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `python3 tests/test_podcast_captions.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'podcast_captions'`.

- [ ] **Step 3: Extract the current parser and add explicit atomic/grouped models**

```python
@dataclass(frozen=True)
class CaptionPiece:
    start_ms: int
    end_ms: int
    text: str
    source_index: int


@dataclass(frozen=True)
class SubtitleSegment:
    id: str
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str | None = None
    ambiguous: bool = False
```

Move the existing timing regex, HTML cleanup, timestamp conversion, comparable-token logic, and rolling-prefix removal into `podcast_captions.py` unchanged. `parse_vtt_pieces` assigns `source_index` only after empty/repeated cues are removed.

Implement grouping with these exact rules:

1. Flush before a cue when the silence gap exceeds 2000 ms.
2. Flush before a cue when its assigned speaker differs from the current speaker.
3. Flush after a cue when the paragraph ends in sentence punctuation or reaches 360 characters.
4. Mark a grouped segment ambiguous when any member index is in `ambiguous_indexes`.
5. When `speakers` is supplied, require every cue index; otherwise raise `ValueError("字幕 cue 缺少 speaker 分配")`.
6. Generate stable IDs `seg-000001` upward after grouping.
7. `parse_vtt(text)` equals `group_caption_pieces(parse_vtt_pieces(text))`.

Import and re-export the models/functions from `scripts/podcast_cn.py` so existing references such as `podcast_cn.SubtitleSegment` remain valid. Insert `ROOT / "scripts"` into the test import path before dynamic imports.

- [ ] **Step 4: Run focused and existing podcast tests**

Run: `python3 tests/test_podcast_captions.py -v && .venv/bin/python tests/test_podcast_cn.py -v`

Expected: both commands end in `OK`; the existing fixture still produces three legacy segments.

- [ ] **Step 5: Register the test and commit**

```make
test-podcast:
	@$(PYTHON) tests/test_podcast_captions.py
	@$(PYTHON) tests/test_podcast_cn.py
```

```bash
git add scripts/podcast_captions.py scripts/podcast_cn.py tests/test_podcast_captions.py tests/test_podcast_cn.py Makefile
git commit -m "refactor: preserve atomic podcast captions"
```

---

### Task 2: Implement Backend-Neutral Alignment and Quality Gates

**Files:**
- Create: `scripts/diarization_core.py`
- Create: `tests/test_diarization_core.py`
- Modify: `Makefile:12-18`

**Interfaces:**
- Consumes: `list[CaptionPiece]`, ordered `list[DiarizationTurn]`, `SpeakerBounds`, and episode duration.
- Produces: `DiarizationBackend`, `DiarizationTurn`, `SpeakerAssignment`, `SpeakerBounds`, `DiarizationQuality`, `align_caption_pieces`, and `validate_diarization`.

- [ ] **Step 1: Write failing overlap, tie, gap, smoothing, overlap-speech, and quality tests**

```python
class AlignmentTests(unittest.TestCase):
    def test_selects_largest_total_overlap(self) -> None:
        piece = CaptionPiece(1000, 3000, "hello", 0)
        turns = [
            DiarizationTurn(900, 1700, "SPEAKER_00"),
            DiarizationTurn(1700, 3100, "SPEAKER_01"),
        ]
        assignment = align_caption_pieces([piece], turns)[0]
        self.assertEqual(assignment.speaker_id, "SPEAKER_01")
        self.assertEqual(assignment.overlap_ms, 1300)

    def test_tie_is_deterministic_and_ambiguous(self) -> None:
        piece = CaptionPiece(0, 1000, "hello", 0)
        turns = [
            DiarizationTurn(0, 500, "SPEAKER_01"),
            DiarizationTurn(500, 1000, "SPEAKER_00"),
        ]
        assignment = align_caption_pieces([piece], turns)[0]
        self.assertEqual(assignment.speaker_id, "SPEAKER_00")
        self.assertTrue(assignment.ambiguous)

    def test_uses_nearest_turn_only_within_1500_ms(self) -> None:
        pieces = [
            CaptionPiece(2000, 2300, "near", 0),
            CaptionPiece(4000, 4300, "far", 1),
        ]
        assignments = align_caption_pieces(
            pieces, [DiarizationTurn(0, 1000, "SPEAKER_00")]
        )
        self.assertEqual(assignments[0].speaker_id, "SPEAKER_00")
        self.assertIsNone(assignments[1].speaker_id)

    def test_rejects_pathological_fragmentation(self) -> None:
        turns = [
            DiarizationTurn(i * 1000, i * 1000 + 300, f"SPEAKER_{i % 2:02d}")
            for i in range(101)
        ]
        with self.assertRaisesRegex(DiarizationQualityError, "切换过于频繁"):
            validate_diarization(
                turns, duration_ms=60_000, bounds=SpeakerBounds()
            )
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `python3 tests/test_diarization_core.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'diarization_core'`.

- [ ] **Step 3: Implement immutable types and deterministic alignment**

```python
@dataclass(frozen=True)
class SpeakerBounds:
    exact: int | None = None
    minimum: int = 1
    maximum: int = 6

    def __post_init__(self) -> None:
        if self.exact is not None and not 1 <= self.exact <= 6:
            raise ValueError("speaker-count 必须在 1 到 6 之间")
        if not 1 <= self.minimum <= self.maximum <= 6:
            raise ValueError("speaker 范围必须满足 1 <= min <= max <= 6")


@dataclass(frozen=True)
class DiarizationTurn:
    start_ms: int
    end_ms: int
    speaker_id: str


@dataclass(frozen=True)
class SpeakerAssignment:
    source_index: int
    speaker_id: str | None
    overlap_ms: int
    confidence: float
    ambiguous: bool
    method: str


@dataclass(frozen=True)
class DiarizationQuality:
    detected_speakers: int
    turn_count: int
    unassigned_count: int
    ambiguous_count: int


class DiarizationBackend(Protocol):
    name: str
    model_fingerprint: dict[str, object]

    def diarize(
        self, audio_path: Path, speaker_bounds: SpeakerBounds
    ) -> list[DiarizationTurn]:
        raise NotImplementedError
```

The alignment algorithm is exact:

1. Sum positive intersection milliseconds by speaker for each cue.
2. Sort candidates by `(-overlap_ms, speaker_id)`; ties therefore select the lexically smaller ID.
3. Confidence equals winner overlap divided by all speaker-overlap milliseconds.
4. Direct overlap is ambiguous when the top-two difference is at most 15% of total overlap, confidence is below 0.65, or summed overlap exceeds 115% of cue duration.
5. With no overlap, choose the nearest turn only when the gap is at most 1500 ms; mark method `nearest` and ambiguous `True`.
6. In a second pass, an unassigned cue lasting at most 800 ms may inherit a speaker only when its immediately adjacent assignments are direct-overlap, non-ambiguous, and equal; mark method `neighbor` and ambiguous `True`.
7. All other cues remain unassigned. Never smooth across a speaker change.

- [ ] **Step 4: Implement quality validation with fixed thresholds**

```python
def validate_diarization(
    turns: list[DiarizationTurn],
    *,
    duration_ms: int,
    bounds: SpeakerBounds,
    assignments: list[SpeakerAssignment] | None = None,
) -> DiarizationQuality:
    if duration_ms <= 0:
        raise DiarizationQualityError("音频时长无效")
    if not turns:
        raise DiarizationQualityError("没有检测到语音片段")
    if any(
        turn.start_ms < 0
        or turn.end_ms <= turn.start_ms
        or turn.end_ms > duration_ms + 250
        for turn in turns
    ):
        raise DiarizationQualityError("说话人时间段超出音频范围")
    speakers = {turn.speaker_id for turn in turns}
    minimum = bounds.exact if bounds.exact is not None else bounds.minimum
    maximum = bounds.exact if bounds.exact is not None else bounds.maximum
    if not minimum <= len(speakers) <= maximum:
        raise DiarizationQualityError("检测到的说话人数超出配置范围")
    max_turns = max(100, math.ceil(duration_ms / 60_000) * 40)
    if len(turns) > max_turns:
        raise DiarizationQualityError("说话人切换过于频繁")
    assignment_list = assignments or []
    unassigned = sum(item.speaker_id is None for item in assignment_list)
    if assignment_list and unassigned / len(assignment_list) > 0.20:
        raise DiarizationQualityError("超过 20% 的字幕无法对齐到语音")
    return DiarizationQuality(
        detected_speakers=len(speakers),
        turn_count=len(turns),
        unassigned_count=unassigned,
        ambiguous_count=sum(item.ambiguous for item in assignment_list),
    )
```

- [ ] **Step 5: Run the focused tests and commit**

Run: `python3 tests/test_diarization_core.py -v`

Expected: all alignment and quality tests end in `OK`.

```bash
git add scripts/diarization_core.py tests/test_diarization_core.py Makefile
git commit -m "feat: align captions with diarization turns"
```

---

### Task 3: Persist Safe Metadata and Generate an Editable Speaker Sequence

**Files:**
- Create: `scripts/diarization_cache.py`
- Create: `tests/test_diarization_cache.py`
- Modify: `scripts/tts-cn.py:240-275`
- Modify: `tests/test_tts_cn.py:87-111`
- Modify: `.gitignore`
- Modify: `Makefile:12-18`

**Interfaces:**
- Consumes: backend fingerprint, video identity, `SpeakerBounds`, caption pieces, turns, assignments, grouped segments, and an existing user map.
- Produces: `DiarizationCache`, `load_or_create_speaker_map`, `render_speaker_sequence`, and labels selecting `female_1`, `male_1`, `female_2`, or `male_2`.

- [ ] **Step 1: Write failing invalidation, privacy, map-preservation, and count tests**

```python
class DiarizationCacheTests(unittest.TestCase):
    def test_valid_turn_cache_round_trips(self) -> None:
        cache.store_turns(
            self.identity, self.bounds, self.fingerprint, self.turns, "audio-sha"
        )
        self.assertEqual(
            cache.load_turns(self.identity, self.bounds, self.fingerprint),
            self.turns,
        )

    def test_model_or_bounds_change_invalidates_turn_cache(self) -> None:
        cache.store_turns(
            self.identity, self.bounds, self.fingerprint, self.turns, "audio-sha"
        )
        self.assertIsNone(
            cache.load_turns(
                self.identity, SpeakerBounds(exact=2), self.fingerprint
            )
        )
        self.assertIsNone(
            cache.load_turns(
                self.identity, self.bounds, {"backend": "changed"}
            )
        )

    def test_user_map_edits_are_preserved(self) -> None:
        mapping = load_or_create_speaker_map(
            path, ["SPEAKER_00", "SPEAKER_01"]
        )
        mapping["SPEAKER_00"]["display_name"] = "主持人"
        write_speaker_map(path, mapping)
        loaded = load_or_create_speaker_map(
            path, ["SPEAKER_00", "SPEAKER_01"]
        )
        self.assertEqual(loaded["SPEAKER_00"]["display_name"], "主持人")

    def test_sequence_is_one_to_one(self) -> None:
        labels = render_speaker_sequence(segments, mapping)
        self.assertEqual(labels, ["说话人甲-女声1", "说话人乙-男声1"])
        self.assertEqual(len(labels), len(segments))
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `python3 tests/test_diarization_cache.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'diarization_cache'`.

- [ ] **Step 3: Implement exact cache schemas and invalidation**

`diarization.json` schema version 1 contains `alignment_version`, `source_identity` (`video_id` and `duration_ms` only), `backend_fingerprint`, serialized speaker bounds, `source_audio_sha256`, turns, creation time, and warnings.

`speaker-assignments.json` schema version 1 contains the SHA-256 of canonical JSON made from each cue's `start_ms`, `end_ms`, `text`, and `source_index`; it also contains `alignment_version` and every `SpeakerAssignment`.

Required behavior:

- wrong schema, malformed JSON, field type error, source mismatch, bounds mismatch, or fingerprint mismatch deletes that cache file and returns a miss;
- changed VTT invalidates assignments only;
- all writes use a sibling `.tmp` file and `Path.replace`;
- no source URL, signed URL, API key, media path, raw audio, or embedding value is serialized.

- [ ] **Step 4: Implement generic mapping and exact TTS voice labels**

```python
VOICE_ORDER = ("female_1", "male_1", "female_2", "male_2")
CHINESE_ORDINALS = ("甲", "乙", "丙", "丁", "戊", "己")


def default_speaker_record(index: int) -> dict[str, str]:
    return {
        "role": f"说话人{CHINESE_ORDINALS[index]}",
        "display_name": "",
        "voice_slot": VOICE_ORDER[index % len(VOICE_ORDER)],
        "mapping_confidence": "unknown",
    }


def label_for_record(record: dict[str, str]) -> str:
    base = record["display_name"].strip() or record["role"].strip()
    suffix = {
        "female_1": "女声1",
        "female_2": "女声2",
        "male_1": "男声1",
        "male_2": "男声2",
    }[record["voice_slot"]]
    return f"{base}-{suffix}"
```

Canonicalize speaker IDs by first turn appearance before creating defaults. Validate existing user records but never replace them; append only newly detected IDs. Removed IDs remain in the user file and are ignored for the current episode.

- [ ] **Step 5: Make TTS honor explicit voice numbers and preserve legacy labels**

```python
_EXPLICIT_VOICE_SLOT = re.compile(
    r"(?:^|[-_])(?P<gender>男|女)声(?P<number>[12])$"
)


def voice_slot_for_label(label: str, assigned: dict[str, str]) -> str:
    if label in assigned:
        return assigned[label]
    explicit = _EXPLICIT_VOICE_SLOT.search(label)
    if explicit:
        prefix = "male" if explicit.group("gender") == "男" else "female"
        slot = f"{prefix}_{explicit.group('number')}"
    elif "男" in label:
        used = sum(value.startswith("male") for value in assigned.values())
        slot = ("male_1", "male_2")[used % 2]
    elif "女" in label:
        used = sum(value.startswith("female") for value in assigned.values())
        slot = ("female_1", "female_2")[used % 2]
    elif "主持" in label:
        used = sum(value.startswith("female") for value in assigned.values())
        slot = ("female_1", "female_2")[used % 2]
    else:
        used = sum(value.startswith("male") for value in assigned.values())
        slot = ("male_1", "male_2")[used % 2]
    assigned[label] = slot
    return slot
```

Add tests for `说话人甲-女声2 -> female_2`, `说话人乙-男声1 -> male_1`, and unchanged `主持人-女` / `嘉宾-男` behavior.

- [ ] **Step 6: Run cache and TTS tests, then commit**

Run: `python3 tests/test_diarization_cache.py -v && .venv/bin/python tests/test_tts_cn.py -v`

Expected: both commands end in `OK`.

```bash
git add scripts/diarization_cache.py scripts/tts-cn.py tests/test_diarization_cache.py tests/test_tts_cn.py .gitignore Makefile
git commit -m "feat: persist diarization metadata and voice maps"
```

---

### Task 4: Build the Pinned Isolated sherpa-onnx Runtime

**Files:**
- Create: `requirements-diarization.txt`
- Create: `scripts/diarization_models.json`
- Create: `scripts/setup_diarization.py`
- Create: `scripts/diarization_runtime.py`
- Create: `scripts/sherpa_diarization_worker.py`
- Create: `bin/setup-podcast-diarization.sh`
- Create: `tests/test_diarization_runtime.py`
- Create: `tests/fixtures/diarization-silence.wav`
- Modify: `.gitignore`
- Modify: `Makefile:3-19`

**Interfaces:**
- Consumes: mono 16 kHz WAV, `SpeakerBounds`, pinned manifest, and progress callback.
- Produces: `backend_fingerprint()`, `ensure_diarization_runtime()`, `run_sherpa_backend(audio_path, bounds)`, and standalone setup.

- [ ] **Step 1: Write failing manifest, reuse, subprocess, and adapter tests**

```python
class RuntimeTests(unittest.TestCase):
    def test_manifest_excludes_reverb_and_has_hashes_and_licenses(self) -> None:
        manifest = load_model_manifest(ROOT / "scripts/diarization_models.json")
        serialized = json.dumps(manifest).casefold()
        self.assertNotIn("reverb", serialized)
        self.assertEqual(manifest["sherpa_onnx_version"], "1.13.2")
        for asset in manifest["assets"]:
            self.assertRegex(asset["target_sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(asset["license"], {"MIT", "Apache-2.0"})

    def test_valid_runtime_and_models_use_no_network_or_pip(self) -> None:
        runtime = ensure_diarization_runtime(
            runtime_root=prepared_runtime,
            model_root=prepared_models,
            opener=mock.Mock(side_effect=AssertionError("no network")),
            runner=mock.Mock(side_effect=AssertionError("no subprocess")),
        )
        self.assertEqual(runtime.python, prepared_runtime / "bin/python")

    def test_parent_passes_exact_speaker_count(self) -> None:
        run_sherpa_backend(
            audio,
            SpeakerBounds(exact=2),
            runtime=runtime,
            runner=fake_runner,
        )
        self.assertIn("--num-speakers", calls[0])
        self.assertIn("2", calls[0])
```

- [ ] **Step 2: Run the test and verify missing runtime files**

Run: `python3 tests/test_diarization_runtime.py -v`

Expected: FAIL because runtime modules and manifest do not exist.

- [ ] **Step 3: Add exact dependency and model manifests**

`requirements-diarization.txt`:

```text
sherpa-onnx==1.13.2
soundfile==0.14.0
```

`scripts/diarization_models.json`:

```json
{
  "schema_version": 1,
  "backend": "sherpa-onnx",
  "sherpa_onnx_version": "1.13.2",
  "model_bundle": "pyannote-3.0_3dspeaker-eres2net-base_v1",
  "assets": [
    {
      "name": "segmentation",
      "url": "https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/resolve/main/model.onnx",
      "archive_member": null,
      "target": "segmentation.onnx",
      "target_sha256": "10a438c2e0d90ed5f5da545cec2244d887315f6dbbbf1d3d564d00745b01952e",
      "license": "MIT",
      "license_url": "https://huggingface.co/pyannote/segmentation-3.0"
    },
    {
      "name": "embedding",
      "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
      "archive_member": null,
      "target": "embedding.onnx",
      "target_sha256": "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b",
      "license": "Apache-2.0",
      "license_url": "https://github.com/modelscope/3D-Speaker/blob/main/LICENSE"
    }
  ]
}
```

Verify every final target against its manifest hash. A mismatch deletes staging and raises `DiarizationSetupError`; it never publishes a partial model directory.

- [ ] **Step 4: Generate and verify the local WAV fixture**

Create exactly one second of signed 16-bit mono silence at 16 kHz as `tests/fixtures/diarization-silence.wav`. Verify:

```python
with wave.open(str(FIXTURE), "rb") as wav:
    self.assertEqual(wav.getnchannels(), 1)
    self.assertEqual(wav.getframerate(), 16_000)
    self.assertEqual(wav.getsampwidth(), 2)
    self.assertEqual(wav.getnframes(), 16_000)
```

The fixture is 32,044 bytes, contains no speech or personal data, and is used only to validate audio loading and adapter boundaries with a fake sherpa result.

- [ ] **Step 5: Implement atomic lazy setup and fingerprinting**

```python
@dataclass(frozen=True)
class DiarizationRuntime:
    python: Path
    model_dir: Path
    segmentation_model: Path
    embedding_model: Path
    fingerprint: dict[str, object]


def default_model_root(manifest: dict[str, object]) -> Path:
    base = Path(
        os.getenv("XDG_CACHE_HOME", str(Path.home() / "Library/Caches"))
    )
    return (
        base
        / "youtube-podcast-cn"
        / "diarization"
        / str(manifest["model_bundle"])
    )


def backend_fingerprint(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "backend": "sherpa-onnx",
        "runtime": manifest["sherpa_onnx_version"],
        "model_bundle": manifest["model_bundle"],
        "models": {
            asset["name"]: asset["target_sha256"]
            for asset in manifest["assets"]
        },
        "cluster_threshold": 0.5,
        "min_duration_on": 0.3,
        "min_duration_off": 0.5,
    }
```

`setup_diarization.py` performs these operations in order:

1. Accept macOS arm64 and Python 3.10 through 3.14 only.
2. Create `.diarization-venv.partial-` followed by `os.getpid()` beside `.diarization-venv`.
3. Run `python -m venv`, then `pip install --requirement requirements-diarization.txt`.
4. Download assets into a model staging directory with a 120-second timeout.
5. Reject redirects outside `github.com`, `githubusercontent.com`, and `huggingface.co`, then write each response to a staging file.
6. Verify every staging file is nonempty and matches SHA-256 before renaming it to its manifest target.
7. Run the isolated worker with `--smoke-test`; it constructs and validates sherpa config without processing audio.
8. Write `runtime-state.json` containing requirements and manifest hashes.
9. Atomically rename completed staging directories. On error, remove staging only and preserve any valid installed runtime.

- [ ] **Step 6: Implement the sherpa worker and parent subprocess**

The worker builds this exact configuration:

```python
config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
            model=str(segmentation_model)
        )
    ),
    embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(embedding_model)
    ),
    clustering=sherpa_onnx.FastClusteringConfig(
        num_clusters=num_speakers if num_speakers is not None else -1,
        threshold=0.5,
    ),
    min_duration_on=0.3,
    min_duration_off=0.5,
)
if not config.validate():
    raise RuntimeError("sherpa-onnx 模型配置无效")
diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
audio, sample_rate = soundfile.read(
    audio_path, dtype="float32", always_2d=True
)
if sample_rate != 16_000 or audio.shape[1] != 1:
    raise RuntimeError("声纹输入必须是 16 kHz 单声道 WAV")
result = diarizer.process(
    audio[:, 0], callback=progress_callback
).sort_by_start_time()
```

Convert sherpa seconds with `round(value * 1000)` and IDs with `f"SPEAKER_{speaker:02d}"`. Write one JSON object atomically to required `--output`. Progress goes to stderr. The parent validates return code and every field; user errors contain only the last 500 stderr characters and never command lines or signed URLs.

The sherpa callback prints integer progress from 0 through 100 to stderr and returns 0 while processing should continue. The parent may terminate the worker on cancellation; the temporary-audio owner still performs cleanup.

- [ ] **Step 7: Add setup wrapper, ignores, tests, and commit**

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/setup_diarization.py" "$@"
```

Ignore `.diarization-venv/` and `.diarization-venv.partial-*/`. Add `!tests/fixtures/diarization-silence.wav` after the existing `*.wav` rule so the single audited fixture can be committed while all generated WAV files remain ignored.

Run: `python3 tests/test_diarization_runtime.py -v && bash -n bin/setup-podcast-diarization.sh`

Expected: tests end in `OK`; shell syntax exits 0; tests use no network.

```bash
git add requirements-diarization.txt scripts/diarization_models.json scripts/setup_diarization.py scripts/diarization_runtime.py scripts/sherpa_diarization_worker.py bin/setup-podcast-diarization.sh tests/test_diarization_runtime.py tests/fixtures/diarization-silence.wav .gitignore Makefile
git commit -m "feat: add isolated sherpa diarization runtime"
```

---

### Task 5: Download and Delete Source Audio Safely

**Files:**
- Create: `scripts/temporary_audio.py`
- Create: `tests/test_temporary_audio.py`
- Modify: `Makefile:12-18`

**Interfaces:**
- Consumes: exact YouTube URL, job directory, runner, and callback `analyze(wav_path) -> T`.
- Produces: `TemporaryAudioResult[T]` containing callback value, source-audio SHA-256, and verified cleanup status; no media path escapes the callback.

- [ ] **Step 1: Write failing command-safety and cleanup tests**

```python
class TemporaryAudioTests(unittest.TestCase):
    def test_download_is_audio_only_and_ffmpeg_is_mono_16k(self) -> None:
        result = with_temporary_audio(
            URL, job_dir, analyze, runner=fake_runner
        )
        download, convert = calls
        self.assertIn("--no-playlist", download)
        self.assertIn("bestaudio/best", download)
        self.assertNotIn("--write-info-json", download)
        self.assertEqual(convert[convert.index("-ac") + 1], "1")
        self.assertEqual(convert[convert.index("-ar") + 1], "16000")
        self.assertEqual(result.cleanup_status, "deleted")

    def test_cleanup_runs_after_success_failure_and_interrupt(self) -> None:
        for error in (None, RuntimeError("backend"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__ if error else "success"):
                run_case(error)
                self.assertEqual(
                    list(job_dir.glob(".diarization-media-*")), []
                )

    def test_cleanup_failure_is_command_failure(self) -> None:
        with mock.patch("shutil.rmtree", side_effect=OSError("busy")):
            with self.assertRaisesRegex(
                TemporaryAudioCleanupError, "临时原音频删除失败"
            ):
                with_temporary_audio(
                    URL, job_dir, analyze, runner=fake_runner
                )
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `python3 tests/test_temporary_audio.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'temporary_audio'`.

- [ ] **Step 3: Implement job-scoped media ownership**

```python
@dataclass(frozen=True)
class TemporaryAudioResult(Generic[T]):
    value: T
    source_audio_sha256: str
    cleanup_status: Literal["deleted"]


def with_temporary_audio(
    url: str,
    job_dir: Path,
    analyze: Callable[[Path], T],
    *,
    runner: Runner = subprocess.run,
) -> TemporaryAudioResult[T]:
    temp_dir = Path(
        tempfile.mkdtemp(prefix=".diarization-media-", dir=job_dir)
    )
    primary_error: BaseException | None = None
    value: T
    source_hash = ""
    try:
        source = download_audio_only(url, temp_dir, runner=runner)
        source_hash = sha256_file(source)
        wav = convert_to_mono_16k(
            source, temp_dir / "analysis.wav", runner=runner
        )
        value = analyze(wav)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            shutil.rmtree(temp_dir)
            if temp_dir.exists():
                raise OSError("目录仍然存在")
        except OSError as cleanup_error:
            message = f"临时原音频删除失败:{temp_dir}"
            if primary_error is not None:
                message = f"处理失败且{message}"
            raise TemporaryAudioCleanupError(message) from cleanup_error
    return TemporaryAudioResult(value, source_hash, "deleted")
```

Use these exact command contracts:

```python
download_args = [
    "yt-dlp",
    "--no-playlist",
    "--no-write-info-json",
    "--no-write-thumbnail",
    "--format",
    "bestaudio/best",
    "--output",
    str(temp_dir / "source.%(ext)s"),
    url,
]
ffmpeg_args = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel",
    "error",
    "-y",
    "-i",
    str(source),
    "-vn",
    "-ac",
    "1",
    "-ar",
    "16000",
    "-c:a",
    "pcm_s16le",
    str(wav),
]
```

After yt-dlp, accept exactly one nonempty regular file matching `source.*`; reject symlinks and paths outside `temp_dir`. Errors may contain the last 500 stderr characters but never commands or remote URLs.

- [ ] **Step 4: Run the tests and commit**

Run: `python3 tests/test_temporary_audio.py -v`

Expected: every success/failure/interrupt/cleanup case ends in `OK`.

```bash
git add scripts/temporary_audio.py tests/test_temporary_audio.py Makefile
git commit -m "feat: analyze temporary source audio safely"
```

---

### Task 6: Integrate Diarization, Cache Reuse, Fallback, and Reporting

**Files:**
- Modify: `scripts/podcast_cn.py:1-911`
- Modify: `tests/test_podcast_cn.py:681-1140`
- Modify: `bin/podcast-cn.sh:1-86`

**Interfaces:**
- Consumes: `--diarize`, resolved `SpeakerBounds`, caption pieces, cache, runtime backend, and temporary-audio owner.
- Produces: `PreparedEpisode` with optional `speakers_path`, `speaker_map_path`, and `DiarizationReport`.

- [ ] **Step 1: Write failing pipeline and CLI tests**

```python
class DiarizationPipelineTests(unittest.TestCase):
    def test_success_writes_one_to_one_speakers(self) -> None:
        prepared = prepare_episode(
            URL,
            output_root,
            runner=fake_youtube_runner,
            translator=fake_translator,
            diarize="required",
            diarization_backend=fake_backend,
        )
        labels = prepared.speakers_path.read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(labels), translated_segment_count)
        self.assertEqual(prepared.diarization_report.status, "success")

    def test_second_run_uses_cache_without_media_or_backend(self) -> None:
        prepare_episode(
            URL,
            output_root,
            runner=fake_runner,
            translator=translator,
            diarize="required",
            diarization_backend=backend,
        )
        again = prepare_episode(
            URL,
            output_root,
            runner=fake_runner,
            translator=no_translation_allowed,
            diarize="required",
            diarization_backend=no_backend_allowed,
        )
        self.assertEqual(again.diarization_report.status, "cache_hit")

    def test_auto_falls_back_as_one_whole_episode(self) -> None:
        prepared = prepare_episode(
            URL,
            output_root,
            runner=fake_runner,
            translator=translator,
            diarize="auto",
            diarization_backend=raising_backend,
        )
        self.assertIsNone(prepared.speakers_path)
        self.assertEqual(prepared.diarization_report.status, "fallback")

    def test_required_stops_before_translation(self) -> None:
        with self.assertRaises(DiarizationBackendError):
            prepare_episode(
                URL,
                output_root,
                runner=fake_runner,
                translator=translation_mock,
                diarize="required",
                diarization_backend=raising_backend,
            )
        translation_mock.assert_not_called()
```

CLI tests cover default `auto`, `off`, `required`, `--speaker-count 2`, min/max bounds, invalid range, exact-count conflict, help, and final Chinese status.

- [ ] **Step 2: Run integration tests and verify new expectations fail**

Run: `.venv/bin/python tests/test_podcast_cn.py -v`

Expected: FAIL because the API/CLI does not accept diarization settings.

- [ ] **Step 3: Add orchestration types and CLI arguments**

```python
@dataclass(frozen=True)
class DiarizationReport:
    status: Literal["success", "cache_hit", "fallback", "off"]
    backend: str | None
    detected_speakers: int
    aligned_segments: int
    ambiguous_segments: int
    temporary_audio: Literal["deleted", "not_created"]
    warning: str | None = None


@dataclass(frozen=True)
class PreparedEpisode:
    video_id: str
    source_title: str
    transcript_path: Path
    state_path: Path
    speakers_path: Path | None = None
    speaker_map_path: Path | None = None
    diarization_report: DiarizationReport | None = None
```

Add `--diarize {auto,off,required}` defaulting to `auto`, plus `--speaker-count`, `--min-speakers`, and `--max-speakers`. Reject exact count combined with either range flag. Resolve omitted range values to 1 and 6.

- [ ] **Step 4: Integrate the required state machine**

`prepare_episode` order is fixed:

1. Fetch metadata and load/download VTT.
2. Parse `CaptionPiece`; do not group yet.
3. For `off`, group without speakers and set report `off/not_created`.
4. For `auto` or `required`, load the model fingerprint without installing runtime.
5. Load valid `diarization.json`. On miss, lazily ensure runtime, run backend inside `with_temporary_audio`, validate turns, and publish turn cache only after verified cleanup.
6. Load assignments only when cue hash matches; otherwise align, quality-check, and publish `speaker-assignments.json`.
7. On setup/media/backend/alignment/quality error, `required` re-raises before translation. `auto` discards all partial diarization data for the current run, groups without speakers, records one warning, and continues to existing Agent fallback.
8. On success, require every cue to have a speaker, then group with speaker boundaries.
9. Load/create `speaker-map.json`, render `transcript.zh.speakers.txt`, and verify nonblank line count equals grouped segment count.
10. Write `subtitles.en.jsonl`, translate/resume, render transcript, and recheck speaker-line count against final transcript paragraphs.

Add job phases `diarization_ready`, `speaker_alignment_ready`, and `speaker_sequence_ready`. State JSON retains only existing small source metadata.

- [ ] **Step 5: Print actionable progress and results**

Success output:

```text
英文标题:Test episode
中文字幕:/path/transcript.zh.md
说话人序列:/path/transcript.zh.speakers.txt
说话人识别:成功（2 人，42 段，3 段需留意）
临时原音频:已删除
```

Cache hit ends with `临时原音频:已删除（本次命中缓存，未重新下载）`. `off` prints `说话人识别:已关闭` and `临时原音频:未下载`. Automatic fallback prints `说话人识别:已回退到内容判断（安全错误摘要）` and omits speaker-sequence path.

Progress goes to stderr: `准备本地声纹模型`, `下载临时原音频`, `转换 16 kHz WAV`, `本地识别说话人`, `对齐字幕`, `删除临时原音频`. Never print subprocess commands or remote media URLs.

- [ ] **Step 6: Run pipeline, wrapper, and regression tests, then commit**

Run: `.venv/bin/python tests/test_podcast_cn.py -v && bash -n bin/podcast-cn.sh && make test`

Expected: tests end in `OK`, shell syntax exits 0, and `make test` prints `All tests passed.`

```bash
git add scripts/podcast_cn.py tests/test_podcast_cn.py bin/podcast-cn.sh
git commit -m "feat: integrate local speaker diarization"
```

---

### Task 7: Make Installation, Skill Routing, and Privacy Documentation Directly Usable

**Files:**
- Modify: `install.sh:1-64`
- Modify: `README.md:1-147`
- Modify: `skills/podcast-translate/SKILL.md:1-381`
- Modify: `tests/test_public_package.py:14-130`
- Modify: `Makefile:3-19`

**Interfaces:**
- Consumes: completed CLI modes and result lines.
- Produces: zero-new-wording default Skill flow, recovery commands, public licensing/privacy boundaries, and portability tests.

- [ ] **Step 1: Write failing public-package and Skill-contract tests**

```python
def test_readme_documents_local_diarization_and_cleanup(self) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    self.assertIn("--diarize auto", readme)
    self.assertIn("--diarize off", readme)
    self.assertIn("--diarize required", readme)
    self.assertIn("临时原音频", readme)
    self.assertIn("不会保留", readme)

def test_skill_uses_generated_speakers_or_explicit_fallback(self) -> None:
    skill = (
        ROOT / "skills/podcast-translate/SKILL.md"
    ).read_text(encoding="utf-8")
    self.assertIn("说话人序列:", skill)
    self.assertIn("已回退到内容判断", skill)
    self.assertIn("--speaker-count 2", skill)

def test_public_manifest_excludes_noncommercial_models(self) -> None:
    manifest = (
        ROOT / "scripts/diarization_models.json"
    ).read_text(encoding="utf-8").casefold()
    self.assertNotIn("reverb", manifest)
    self.assertNotIn("non-commercial", manifest)
```

- [ ] **Step 2: Run public tests and verify documentation failures**

Run: `python3 tests/test_public_package.py -v`

Expected: FAIL on missing diarization documentation and Skill routing.

- [ ] **Step 3: Update Skill routing without adding user work**

The normal command remains:

```bash
bash "__PODCAST_TRANSLATE_ROOT__/bin/podcast-cn.sh" "$YOUTUBE_URL"
```

Exact routing:

- default wording uses implicit `auto`;
- `不要分析原音频` appends `--diarize off`;
- `必须准确区分说话人` appends `--diarize required`;
- `这是两个人的播客` appends `--speaker-count 2`;
- stdout `说话人序列:` means pass that file directly to `bin/tts-cn.sh --speakers` and skip manual paragraph-by-paragraph speaker work;
- stdout `已回退到内容判断` means run the existing Agent speaker section unchanged;
- generic `男声/女声` labels describe synthesis voice only;
- reliable names/roles may be written to `speaker-map.json`; rerunning regenerates the sequence while preserving the edit.

- [ ] **Step 4: Update README, installer output, and safety checks**

README documents:

- first use lazily creates `.diarization-venv` and downloads about 47 MB of reusable models;
- local processing consumes no translation tokens or DashScope quota;
- source media is temporary and deleted, while timing metadata remains;
- `auto/off/required`, exact speaker count, and recovery command `bash bin/setup-podcast-diarization.sh`;
- sherpa runtime Apache-2.0, pyannote segmentation MIT, and 3D-Speaker Apache-2.0;
- overlap/music/short-interruption/anonymous-identity limits;
- user responsibility for YouTube terms and copyright.

`install.sh` continues installing only the main environment. Its success message explains lazy diarization and the explicit setup command; normal installation does not download sherpa or models.

- [ ] **Step 5: Run public, shell, and full tests, then commit**

Run: `python3 tests/test_public_package.py -v && bash -n install.sh bin/setup-podcast-diarization.sh && make test`

Expected: public tests end in `OK`, shell syntax exits 0, and full suite prints `All tests passed.`

```bash
git add install.sh README.md skills/podcast-translate/SKILL.md tests/test_public_package.py Makefile
git commit -m "docs: ship zero-friction speaker diarization"
```

---

### Task 8: Verify Offline Behavior and the Three-Episode Listening Gate

**Files:**
- Modify: `skills/podcast-translate/ISSUES.md` only when a reproducible product defect is found.
- Create locally, never commit: `$HOME/Desktop/音频/声纹分离验收/acceptance.json`

**Interfaces:**
- Consumes: completed feature and three user-authorized YouTube episodes.
- Produces: fresh automated evidence and a private listening record; no source media is retained.

- [ ] **Step 1: Run the full suite with network denial**

```bash
env   HTTP_PROXY=http://127.0.0.1:9   HTTPS_PROXY=http://127.0.0.1:9   NO_PROXY=localhost,127.0.0.1   make test
```

Expected: exit 0 and `All tests passed.`, proving automated tests need no network or paid service.

- [ ] **Step 2: Run real local setup and smoke validation**

```bash
bash bin/setup-podcast-diarization.sh
.diarization-venv/bin/python scripts/sherpa_diarization_worker.py   --segmentation-model "$HOME/Library/Caches/youtube-podcast-cn/diarization/pyannote-3.0_3dspeaker-eres2net-base_v1/segmentation.onnx"   --embedding-model "$HOME/Library/Caches/youtube-podcast-cn/diarization/pyannote-3.0_3dspeaker-eres2net-base_v1/embedding.onnx"   --smoke-test
```

Expected: both model hashes verify and smoke exits 0 without account/token.

- [ ] **Step 3: Process three authorized episodes twice**

Choose a clean two-person studio interview, a fast conversation with short interruptions, and a three-or-four-person or overlapping-speech episode. Keep URLs and output private. For each URL:

```bash
bash bin/podcast-cn.sh "$YOUTUBE_URL" --diarize required
bash bin/podcast-cn.sh "$YOUTUBE_URL" --diarize required
```

First run must report speakers, ambiguity, and `临时原音频:已删除`. Second run must report cache reuse and show no media/backend progress. Verify no temporary directory remains:

```bash
find "$HOME/Desktop/音频/.podcast-jobs"   -type d -name '.diarization-media-*' -print
```

Expected: no output.

- [ ] **Step 4: Record the private listening comparison**

Listen to the first 10 minutes and two known speaker-change sections per episode. Record real integer correction counts in this shape:

```json
{
  "episodes": [
    {
      "profile": "clean-two-person",
      "manual_corrections_before": 4,
      "manual_corrections_after": 1,
      "severe_regression": false,
      "temporary_media_deleted": true,
      "second_run_cache_hit": true
    }
  ]
}
```

The gate passes only when at least two of three episodes need fewer corrections, none regresses severely, all temporary media is deleted, and every second run avoids redownload/rediarization.

- [ ] **Step 5: Run final repository and privacy verification**

```bash
git diff --check
git status --short
python3 tests/test_public_package.py -v
make test
```

Expected: no whitespace errors; only intended files changed; all tests pass. Confirm no WAV other than the 32,044-byte silence fixture, downloaded source media, API key, personal absolute path, or private acceptance URL is staged.

The feature is ready for branch completion only after the automated suite and three-episode listening gate both pass.
