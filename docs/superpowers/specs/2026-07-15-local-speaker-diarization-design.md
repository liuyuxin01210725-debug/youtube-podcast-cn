# Local Speaker Diarization Design

**Date:** 2026-07-15
**Status:** Approved design; implementation not started
**Project:** YouTube Podcast to Chinese Audio

## 1. Goal

Add optional local speaker diarization to the YouTube URL pipeline so that
speaker changes are derived from the original audio timeline instead of being
guessed paragraph by paragraph from translated text.

The normal user experience remains one request:

```text
把这个 YouTube 播客转成中文音频
```

For a YouTube URL, the tool may temporarily download audio, identify anonymous
speakers, align those speakers with timestamped subtitles, translate the
speaker-safe segments, and generate the existing `.speakers.txt` file. The
downloaded source audio and converted WAV must always be deleted after analysis.

## 2. Product Decisions

- The first local backend is **sherpa-onnx**.
- Processing is local and free. It does not consume LLM tokens, DashScope
  quota, or a cloud diarization API.
- The backend must run on macOS arm64, including the user's Apple M4 with 16 GB
  memory.
- Model assets are downloaded lazily on first diarization use and reused.
- No Hugging Face account or access token is required for the default backend.
- The implementation keeps a backend boundary so MLX Sortformer can be tested
  later without changing caption alignment or TTS code.
- WhisperX is not part of this feature because YouTube VTT captions already
  provide text and timestamps; running ASR again would add weight and failure
  modes without serving the current need.
- The sherpa-onnx Reverb diarization model is excluded because its stated
  non-commercial restriction conflicts with possible future commercial use.
  The default manifest must contain only model assets whose upstream licenses
  permit the repository's intended public and commercial use.

Primary references:

- [sherpa-onnx repository](https://github.com/k2-fsa/sherpa-onnx)
- [sherpa-onnx speaker diarization documentation](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/index.html)
- [pyannote Community-1 model card](https://huggingface.co/pyannote/speaker-diarization-community-1)
- [MLX-Audio diarization documentation](https://github.com/Blaizzy/mlx-audio#voice-activity-detection--speaker-diarization-vad)

## 3. Non-goals

- Do not separate the original recording into one audio file per person.
- Do not replace YouTube subtitles with Whisper transcription.
- Do not claim that voice clustering identifies a person's name, role, gender,
  or gender identity.
- Do not upload source audio or speaker embeddings to any external service.
- Do not retain source audio after diarization.
- Do not make cloud diarization an automatic paid fallback.
- Do not promise perfect handling of overlapping speech, very short
  interruptions, music, or heavily compressed audio.

## 4. Architecture

The feature is inserted into the YouTube preparation path before translation:

```text
YouTube URL
  -> metadata + raw VTT cues
  -> temporary audio-only download
  -> temporary mono 16 kHz WAV
  -> local diarization turns
  -> cue-to-speaker alignment
  -> merge cues only while speaker stays the same
  -> stable subtitle segments
  -> translation
  -> transcript.zh.md + speakers.txt
  -> EdgeTTS or Qwen3-TTS
```

This order is required. The current VTT parser can merge adjacent cues into a
larger subtitle segment. If diarization were applied after that merge, a single
segment could already contain two speakers. The new path must preserve atomic
VTT cues, assign a speaker to each cue, and prevent merging across a speaker
boundary.

Existing translation and TTS contracts remain unchanged after stable segments
and the speaker sequence are produced.

## 5. Components and Interfaces

### 5.1 Atomic caption parsing

Introduce an atomic caption model:

```python
CaptionPiece(
    start_ms: int,
    end_ms: int,
    text: str,
    source_index: int,
)
```

`parse_vtt_pieces` preserves the smallest deduplicated VTT cues. The existing
`parse_vtt` remains as a compatibility wrapper for the non-diarized path.

After speaker assignment, adjacent pieces may be grouped using the current
sentence-ending, silence, and character limits only when their assigned speaker
is identical. A speaker change is always a hard segment boundary.

### 5.2 Diarization backend

Provider-specific code sits behind a small interface:

```python
DiarizationBackend
  name: str
  model_fingerprint: dict[str, object]
  diarize(audio_path, speaker_bounds) -> list[DiarizationTurn]
```

```python
DiarizationTurn(
    start_ms: int,
    end_ms: int,
    speaker_id: str,
)
```

The sherpa backend uses a sherpa-onnx speaker segmentation model, a compatible
speaker embedding model, and clustering. It outputs anonymous IDs such as
`SPEAKER_00` and `SPEAKER_01`.

The backend accepts an exact speaker count when the user provides one. Otherwise
the normal automatic bounds are one to six speakers. Results outside the bounds
or with clearly pathological fragmentation fail the quality gate.

### 5.3 Caption alignment

For each caption piece, compute its time overlap with every diarization turn.
Sum overlap duration by speaker and select the speaker with the largest total.

```python
SpeakerAssignment(
    source_index: int,
    speaker_id: str,
    overlap_ms: int,
    confidence: float,
    ambiguous: bool,
)
```

`confidence` is the winning speaker's overlap divided by total speech overlap.
The assignment is ambiguous when the top two speakers are close, when no turn
overlaps, or when the caption substantially overlaps simultaneous speakers.

Fallback rules are deterministic:

1. Use direct overlap when available.
2. For a small timestamp gap, use the nearest turn within 1.5 seconds.
3. A very short isolated cue may inherit a speaker only when both neighboring
   confident cues agree.
4. Do not aggressively smooth low-confidence cues across a real speaker change.

Ambiguity is recorded rather than hidden.

### 5.4 Role and voice mapping

Diarization identifies only anonymous clusters. A separate mapping step reviews
video metadata, names, self-introductions, and a small set of representative
caption excerpts once per cluster:

```json
{
  "SPEAKER_00": {
    "role": "主持人",
    "display_name": "",
    "voice_slot": "female_1",
    "mapping_confidence": "medium"
  }
}
```

When identity and gender are known from reliable public context, the label may
use the existing role and gender convention. When they are unknown, the tool
assigns a distinct synthesis voice without claiming a biological gender, for
example `说话人甲-女声` and `说话人乙-男声`.

User edits to `speaker-map.json` are authoritative and must never be overwritten
automatically. The final `.speakers.txt` is derived atomically from grouped
segments and this mapping, with exactly one label per translated segment.

## 6. Download, Cleanup, and Privacy

The media download is audio-only and uses `--no-playlist`. It is written inside
a dedicated temporary directory under the job directory. FFmpeg converts it to
the backend's required mono 16 kHz WAV in the same temporary directory.

Cleanup is mandatory:

- A `try/finally` boundary owns both the compressed source audio and WAV.
- Success, provider failure, alignment failure, keyboard interruption, and
  cancellation all delete both files.
- Temporary media is never placed in the repository, transcript directory, or
  long-term cache.
- Logs print filenames and status but not signed media URLs or private cookies.
- The tool downloads only media the user asks it to process and retains no copy
  of the original recording.

The final report always states `临时原音频：已删除`. If cleanup cannot be
verified, the command fails with the remaining path instead of claiming success.

## 7. Cache Design

Only small derived metadata is cached under the existing per-video job directory.

`diarization.json` contains:

- schema and alignment algorithm versions;
- video ID, duration, and non-secret source identity;
- diarization backend, model files, versions, and parameters;
- optional source-audio SHA-256 computed before deletion;
- anonymous diarization turns;
- creation time and warnings.

`speaker-assignments.json` contains:

- the raw VTT piece hash;
- alignment algorithm version;
- assignments, confidence values, and ambiguity flags.

`speaker-map.json` contains only the editable role and synthesis-voice mapping.

Cache behavior:

- A valid turn cache avoids both audio download and diarization on rerun.
- A changed VTT invalidates only caption assignments, not diarization turns.
- A changed backend, model, model checksum, speaker-count setting, or audio
  identity invalidates the turn cache.
- Corrupt or incomplete cache files are removed and rebuilt.
- All cache publication is atomic.
- No raw audio, WAV, speaker embedding, cookie, access token, or media URL is
  persisted.

## 8. User Experience and Modes

The normal Skill flow uses `auto` and requires no new wording.

Internal CLI modes:

```text
--diarize auto       default for YouTube URLs
--diarize off        skip source-audio analysis
--diarize required   fail instead of falling back
--speaker-count N    exact known count
--min-speakers N
--max-speakers N
```

Behavior:

- `auto`: use diarization when available. On setup, download, model, or quality
  failure, report the reason and fall back to the current Agent text-based
  speaker sequence.
- `off`: never download source audio.
- `required`: any diarization or quality failure stops before TTS.
- Local Chinese text and article inputs never invoke diarization.

Natural-language routing:

- `把这个 YouTube 播客转成中文音频` -> `auto`
- `不要分析原音频` -> `off`
- `必须准确区分说话人` -> `required`
- `这是两个人的播客` -> exact speaker count of two

Progress output includes download, audio preparation, diarization percentage,
caption alignment, cache publication, and verified cleanup.

The final report includes detected speaker count, aligned segment count,
ambiguous segment count, whether fallback occurred, and cleanup status.

## 9. Installation and Model Management

Diarization dependencies are isolated from the main translation/TTS virtual
environment in a dedicated runtime environment. This prevents sherpa and ONNX
dependencies from destabilizing the existing pipeline.

The first diarization run invokes a setup helper that:

1. selects a tested compatible Python interpreter;
2. creates the isolated environment;
3. installs pinned diarization dependencies;
4. downloads an exact model manifest from official release assets;
5. verifies SHA-256 checksums and records upstream licenses;
6. runs a local no-network smoke test;
7. publishes the completed model directory atomically.

The default model cache is outside the repository under the user's cache
directory. Partial downloads are safe to delete and are never treated as valid.
If no compatible interpreter is available, setup stops with one exact recovery
command; it does not silently modify the system Python.

## 10. Failure and Quality Gates

The tool rejects diarization results when any of these conditions hold:

- invalid, negative, reversed, or out-of-duration turns;
- no speech turns for nonempty captions;
- detected speaker count outside configured bounds;
- excessive speaker switching that indicates pathological clustering;
- an unacceptable fraction of captions with no nearby turn;
- final speaker sequence length differs from final transcript paragraph count;
- temporary media cleanup cannot be verified.

In `auto`, a rejected result is never partially mixed with text guesses. The
entire episode falls back to text-based speaker assignment. In `required`, the
episode stops. A warning explains the reason in both cases.

Overlapping speech and low-confidence assignments are allowed only when they are
explicitly counted and reported. The system must not describe the output as
perfectly identified.

## 11. Testing Strategy

Unit tests use synthetic time intervals and do not access YouTube or model hosts.
They cover:

- exact overlap winner, ties, no overlap, nearby-turn fallback, and overlapping
  speech;
- speaker-change boundaries and preservation of caption text;
- rolling-caption deduplication before assignment;
- cache fingerprint changes and corrupt cache recovery;
- speaker-map preservation and deterministic voice-slot assignment;
- speaker sequence count matching translation and TTS paragraph counts;
- `auto`, `off`, and `required` routing;
- abnormal speaker counts and fragmentation quality gates;
- cleanup after success, every failure stage, cancellation, and interruption;
- a second run proving no media download or model call occurs;
- command safety, including `--no-playlist` and no persistent source media.

A small redistributable local audio fixture covers the sherpa adapter without
network access. Paid APIs, YouTube, and live model downloads are forbidden in
the automated suite.

Real acceptance testing uses three user-authorized multi-speaker YouTube
episodes:

- a clean two-person studio interview;
- a fast conversation with short interruptions;
- a three-or-four-person episode or an episode with overlapping speech.

For each, compare the previous text-only speaker sequence with diarization.
The feature passes the listening gate when at least two of three episodes need
fewer manual speaker corrections, no episode regresses severely, all temporary
media is deleted, and a repeated run avoids redownload and rediarization.

## 12. Completion Criteria

The feature is complete only when:

- the default backend runs locally on the target Apple M4 Mac without an account
  or API token;
- normal URL requests use `auto` without new user commands;
- source audio and WAV are verified deleted on every exit path;
- a valid cache prevents repeated media download and diarization;
- diarization is applied before caption grouping;
- `.speakers.txt` and translated segments are strictly one-to-one;
- anonymous clusters are not misrepresented as known identities or genders;
- existing DeepSeek/Qwen translation and Edge/Qwen TTS tests remain green;
- all automated tests pass without network access;
- the three-episode real listening gate passes.
