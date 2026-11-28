"""Offline acceptance for the selected-original command (no real fixtures)."""
import contextlib
import functools
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.gettempdir()).resolve()
sys.path.insert(0, str(ROOT / "scripts"))
FFMPEG, FFPROBE = (shutil.which(name) for name in ("ffmpeg", "ffprobe"))
MEDIA = (FFMPEG, FFPROBE)
COMMANDS = []
FORBIDDEN = []
_audit_active = False
IDENT = "mostprise-69320d8b4ef12f9fe9f8e1fe"
PREFIX = "mostprise-"
AUDIO = PREFIX + "original.m4a"
RAW = PREFIX + "original-asr-raw.json"
LOCK = PREFIX + "source-lock.json"
RECEIPT = PREFIX + "asr-receipt.json"
OFFICIAL = PREFIX + "official-audio-binding.json"
META = "INPUT-METADATA.json"


def forbid(name, *args, **kwargs):
    FORBIDDEN.append(name)
    raise AssertionError(f"forbidden entrypoint: {name}")


def audit(event, args):
    if not _audit_active:
        return
    if event.startswith("socket.") or event in ("os.system", "os.posix_spawn", "os.exec"):
        forbid(event)
    if event == "subprocess.Popen":
        executable, argv, _, _ = args
        if executable not in MEDIA or not isinstance(argv, (list, tuple)):
            forbid(event)
        if "-protocol_whitelist" not in argv or argv[argv.index("-protocol_whitelist") + 1] != "file":
            forbid("media protocol")
        COMMANDS.append(tuple(argv))


def setUpModule():
    global _audit_active
    sys.addaudithook(audit)
    _audit_active = True


def tearDownModule():
    global _audit_active
    _audit_active = False
    encodes = sum("libmp3lame" in cmd for cmd in COMMANDS)
    print(f"offline guard totals: forbidden attempts={len(FORBIDDEN)}; "
          f"allowlisted FFmpeg/FFprobe calls={len(COMMANDS)}; MP3 encodes={encodes}",
          flush=True)
    if FORBIDDEN:
        raise AssertionError("a forbidden entrypoint was attempted")


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def load(path):
    return json.loads(path.read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root):
    return {str(p.relative_to(root)): (
        ("link", os.readlink(p)) if p.is_symlink()
        else ("file", digest(p), p.stat().st_mtime_ns) if p.is_file()
        else ("directory",)
    ) for p in root.rglob("*")}


class OriginalPendingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("studio_original_pending"),
                             "standalone original-only admission command is absent")
        import studio_original_pending
        self.intake = studio_original_pending
        self.assertTrue(all(MEDIA), "existing ffmpeg and ffprobe required")
        self.assertTrue(WORK.is_dir(), "create the isolated synthetic workspace first")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target in ("urllib.request.urlopen", "podcast_cn.create_translation_client",
                       "podcast_cn.translate_with_resume", "studio_publish_icloud.translate_episode_topic",
                       "studio_engine.StudioEngine.prepare", "studio_engine.StudioEngine.run",
                       "tts_backends.build_backend", "tts_backends.EdgeTTSBackend.synthesize",
                       "tts_backends.Qwen3APIBackend.synthesize"):
            stack.enter_context(patch(target, side_effect=functools.partial(forbid, target)))
        base = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=WORK)))
        stack.enter_context(patch.object(self.intake, "EVIDENCE", base))
        self.base = base
        self.source, self.library = base / "inputs", base / "library"
        self.source.mkdir()
        self.library.mkdir()
        self.episode = self.library / "episodes" / IDENT
        self.catalog = self.library / "library.json"
        self.old = self.library / "episodes" / "old"
        self.old.mkdir(parents=True)
        self.old_record = {"id": "old", "title": "旧节目", "file": "output.mp3", "extra": [1, 2]}
        dump(self.catalog, {"episodes": [self.old_record], "unknown": {"keep": True}})
        for name in ("output.mp3", "episode.json", "notes.json", "stars.json", "progress.json"):
            (self.old / name).write_bytes(b"baseline file stays byte-identical")
        self.wav = base / "tone.wav"
        with wave.open(str(self.wav), "wb") as sound:
            sound.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            sound.writeframes((b"\0\x10" * 64 + b"\0\xf0" * 64) * 345 + b"\0\0" * 240)
        subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-protocol_whitelist", "file",
                        "-i", str(self.wav), "-c:a", "aac", str(self.source / AUDIO)], check=True)
        self.raw = {
            "source_audio_sha256": "",
            "source_duration_s": 1.007,
            "model_local": "/synthetic/whisper-small-mlx",
            "offline": True, "new_downloads": False, "paid_calls": 0,
            "result": {"language": "zh", "segments": [
                {"id": 0, "start": 0.0, "end": 0.4, "text": " 这是合成第一句。"},
                {"id": 1, "start": 0.4, "end": 1.1, "text": "这是合成第二句。"},
            ]},
        }
        self.source_info = {
            "id": IDENT,
            "title": "合成节目标题",
            "show": "有个鲸喜MOSTPRISE",
            "host": "Henrik",
            "guest": "Yikai",
            "source_url": "https://www.xiaoyuzhoufm.com/episode/69320d8b4ef12f9fe9f8e1fe",
            "audio_url": "https://example.invalid/mostprise.m4a",
        }
        self.pins = {}
        stack.enter_context(patch.object(self.intake, "TRUSTED_METADATA", self.pins))
        self.seal()

    def seal(self):
        audio_hash = digest(self.source / AUDIO)
        self.raw["source_audio_sha256"] = audio_hash
        dump(self.source / RAW, self.raw)
        raw_hash = digest(self.source / RAW)
        n, duration = len(self.raw["result"]["segments"]), self.raw["source_duration_s"]
        source = dict(self.source_info)
        source.update(duration_s=duration, audio={"sha256": audio_hash,
                      "bytes": (self.source / AUDIO).stat().st_size},
                      asr_evidence={"raw_sha256": raw_hash, "segment_count": n,
                                    "source_audio_binding": True})
        dump(self.source / LOCK, source)
        dump(self.source / RECEIPT, {
            "model": self.raw["model_local"], "offline": True, "paid_calls": 0,
            "audio_sha256": audio_hash, "audio_duration_s": duration,
            "segments": n, "first_start_s": 0.0, "last_end_s": 1.1, "asr_sha256": raw_hash,
            "not_translation": True, "not_listening_proof": True, "not_ready_or_published": True,
        })
        dump(self.source / OFFICIAL, {
            "official_html_sha256": "a" * 64,
            "supplied_audio_filename_found_in_official_html": True,
            "matching_public_metadata_snippets": [self.source_info["audio_url"]],
        })
        dump(self.source / META, {**self.source_info, "duration_sec": duration,
             "original_bytes": source["audio"]["bytes"], "original_sha256": audio_hash,
             "asr_raw_sha256": raw_hash, "asr_segments": n, "raw_asr_max_end_sec": 1.1})
        self.pins.update({name: digest(self.source / name) for name in (META, LOCK, RECEIPT, OFFICIAL)})

    def admit(self, **kwargs):
        return self.intake.admit(self.source, self.library, **kwargs)

    def assert_rejected_without_writes(self, **kwargs):
        before, media = snapshot(self.library), len(COMMANDS)
        with self.assertRaises(self.intake.IntakeError):
            self.admit(**kwargs)
        self.assertEqual(snapshot(self.library), before)
        self.assertEqual(len(COMMANDS), media)

    def test_import_preserves_source_binds_pending_units_and_unions_catalog(self):
        old = snapshot(self.old)
        inputs = snapshot(self.source)
        record = self.admit(confirmed=True)
        self.assertEqual(self.episode, Path(record["episode_dir"]))
        self.assertEqual(snapshot(self.source), inputs)
        self.assertEqual(snapshot(self.old), old)
        catalog = load(self.catalog)
        self.assertEqual(catalog["unknown"], {"keep": True})
        entry = load(self.episode / "episode.json")
        self.assertEqual(catalog["episodes"], [self.old_record, entry])
        self.assertEqual((entry["id"], entry["file"], entry["language"]), (IDENT, "output.mp3", "zh"))
        self.assertEqual((entry["show"], entry["host"], entry["guests"]),
                         ("有个鲸喜MOSTPRISE", "Henrik", ["Yikai"]))
        self.assertEqual(entry["source_url"], self.source_info["source_url"])
        self.assertRegex(entry["created_at"], r"T\d\d:\d\d:\d\d\+08:00$")
        self.assertEqual(entry["title"], "合成节目标题 · 有个鲸喜MOSTPRISE（逐段待核）")
        self.assertEqual(entry["audio_kind"], "original_audio")
        self.assertEqual(entry["transcript_status"], "asr_unreviewed")
        self.assertEqual(entry["status"], "pending_review")
        self.assertFalse({"source_job_id", "ready", "complete", "translated", "tts"} & entry.keys())
        self.assertFalse((self.episode / "transcript.json").exists())
        self.assertFalse((self.episode / "extract.json").exists())
        manifest = load(self.episode / entry["manifest"])
        self.assertEqual(manifest["media"]["source"]["codec"], "aac")
        self.assertEqual(manifest["media"]["playback"]["codec"], "mp3")
        self.assertEqual(manifest["media"]["playback"]["format"], "mp3")
        self.assertGreater(manifest["media"]["playback"]["decoded_frames"], 1)
        self.assertEqual(manifest["media"]["source"]["decoded_samples_per_channel"],
                         manifest["media"]["playback"]["decoded_samples_per_channel"])
        self.assertEqual(manifest["asr"]["raw_max_end_s"], 1.1)
        self.assertEqual(manifest["asr"]["clamped_end_count"], 1)
        for name in (AUDIO, RAW, LOCK, RECEIPT, OFFICIAL, META):
            self.assertEqual(digest(self.episode / name), digest(self.source / name))
        for name, expected in manifest["sha256"].items():
            self.assertEqual(digest(self.episode / name), expected)
        self.assertEqual({p.name for p in self.episode.iterdir()}, {*manifest["sha256"], "manifest.json"})
        transcript = load(self.episode / entry["transcript"])
        self.assertEqual(transcript["source_audio_sha256"], digest(self.source / AUDIO))
        self.assertEqual(transcript["playback_audio_sha256"], digest(self.episode / "output.mp3"))
        self.assertEqual(transcript["raw_asr_sha256"], digest(self.source / RAW))
        self.assertEqual([unit["end"] for unit in transcript["segments"]], [0.4, 1.007])
        self.assertEqual([unit["text"] for unit in transcript["segments"]],
                         [" 这是合成第一句。", "这是合成第二句。"])
        self.assertTrue(all(u["status"] == "pending_review" and "speaker" not in u
                            for u in transcript["segments"]))

    def test_duplicate_is_read_only_and_runs_no_media_subprocess(self):
        self.admit(confirmed=True)
        dump(self.episode / "notes.json", {"personal": "合成笔记"})
        before, commands = snapshot(self.library), len(COMMANDS)
        self.assertEqual(self.admit(confirmed=True)["result"], "already_present")
        self.assertEqual(snapshot(self.library), before)
        self.assertEqual(len(COMMANDS), commands)

    def test_small_source_timestamp_gap_keeps_playback_clock(self):
        audio = self.source / AUDIO
        audio.unlink()
        subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-protocol_whitelist", "file",
                        "-i", str(self.wav), "-af", r"asetpts=PTS+gte(N\,22050)*2044",
                        "-c:a", "aac", str(audio)], check=True)
        duration = subprocess.check_output(
            [FFPROBE, "-v", "error", "-protocol_whitelist", "file", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", str(audio)], text=True)
        self.raw["source_duration_s"] = float(duration)
        self.seal()
        self.assertEqual(self.admit(confirmed=True)["result"], "created")
        manifest = load(self.episode / "manifest.json")
        source, playback = (manifest["media"][key] for key in ("source", "playback"))
        self.assertEqual(source["decoded_gap_samples"], 2044)
        self.assertEqual(source["decoded_gap_count"], 1)
        self.assertEqual(playback["decoded_gap_count"], 0)
        self.assertEqual(playback["decoded_samples_per_channel"], source["decoded_end_sample_pts"])
        self.assertEqual(playback["decoded_samples_per_channel"] - source["decoded_samples_per_channel"], 2044)

    def test_rejects_unconfirmed_tampered_and_missing_metadata_and_blobs(self):
        self.assert_rejected_without_writes()
        for name in (META, LOCK, OFFICIAL, RECEIPT, AUDIO, RAW):
            with self.subTest(name=name):
                path = self.source / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assert_rejected_without_writes(confirmed=True)
                path.unlink()
                self.assert_rejected_without_writes(confirmed=True)
                path.write_bytes(original)

    def test_rejects_emma_wrong_language_malformed_timings_and_cross_binding(self):
        self.source_info["id"] = "emma"
        self.seal()
        self.assert_rejected_without_writes(confirmed=True)
        self.source_info["id"] = IDENT
        self.raw["result"]["language"] = "en"
        self.seal()
        self.assert_rejected_without_writes(confirmed=True)
        self.raw["result"]["language"] = "zh"
        self.raw["result"]["segments"][1]["start"] = True
        self.seal()
        self.assert_rejected_without_writes(confirmed=True)
        self.raw["result"]["segments"][1]["start"] = 0.4
        self.seal()
        receipt = load(self.source / RECEIPT)
        receipt["audio_sha256"] = "8" * 64
        dump(self.source / RECEIPT, receipt)
        self.pins[RECEIPT] = digest(self.source / RECEIPT)
        self.assert_rejected_without_writes(confirmed=True)
        self.seal()
        official = load(self.source / OFFICIAL)
        official["matching_public_metadata_snippets"] = ["https://example.invalid/elsewhere.m4a"]
        dump(self.source / OFFICIAL, official)
        self.pins[OFFICIAL] = digest(self.source / OFFICIAL)
        self.assert_rejected_without_writes(confirmed=True)

    def test_rejects_corrupt_or_colliding_catalog_and_input_destination_links(self):
        previous = self.catalog.read_bytes()
        for payload in ('{', '{"episodes": [{"id":"../escape"}]}',
                        '{"episodes": [{"id":"old"}, {"id":"old"}]}',
                        json.dumps({"episodes": [{"id": IDENT}]})):
            with self.subTest(payload=payload):
                self.catalog.write_text(payload)
                self.assert_rejected_without_writes(confirmed=True)
        self.catalog.write_bytes(previous)
        self.catalog.unlink()
        external = self.base / "external"
        external.write_bytes(previous)
        self.catalog.symlink_to(external)
        self.assert_rejected_without_writes(confirmed=True)
        self.assertEqual(external.read_bytes(), previous)
        self.catalog.unlink()
        self.catalog.write_bytes(previous)
        self.episode.symlink_to(self.old)
        self.assert_rejected_without_writes(confirmed=True)
        self.episode.unlink()
        incoming = self.source / OFFICIAL
        external.write_bytes(incoming.read_bytes())
        incoming.unlink()
        incoming.symlink_to(external)
        self.assert_rejected_without_writes(confirmed=True)

    def test_rejects_escape_target_and_protected_package_even_if_index_lost(self):
        target = self.base / "linked-library"
        target.symlink_to(self.library, target_is_directory=True)
        for path in (target, self.base / ".." / self.base.name / "library", self.source, ROOT):
            with self.subTest(path=path):
                before = snapshot(self.library)
                with self.assertRaises(self.intake.IntakeError):
                    self.intake.admit(self.source, path, confirmed=True)
                self.assertEqual(snapshot(self.library), before)
        self.admit(confirmed=True)
        dump(self.catalog, {"episodes": [self.old_record]})
        (self.episode / AUDIO).write_bytes(b"conflicting source asset")
        self.assert_rejected_without_writes(confirmed=True)

    def test_invalid_encoded_media_rolls_back(self):
        (self.source / AUDIO).write_bytes(b"this is not audio")
        self.seal()
        before = snapshot(self.library)
        with self.assertRaises(self.intake.IntakeError):
            self.admit(confirmed=True)
        self.assertEqual(snapshot(self.library), before)

    def test_transcode_and_commit_failures_leave_existing_library_unchanged(self):
        before = snapshot(self.library)
        run, rename, replace = subprocess.run, os.rename, os.replace
        guarded = self.intake.atomic_write_guarded_catalog
        def transcode_failure(args, **kwargs):
            if "libmp3lame" in args:
                raise subprocess.CalledProcessError(1, args)
            return run(args, **kwargs)
        for target, original in (("subprocess.run", transcode_failure),
                                 ("os.rename", rename), ("os.replace", replace),
                                 ("studio_original_pending.atomic_write_guarded_catalog", guarded)):
            with self.subTest(target=target):
                def fail(*args, **kwargs):
                    if target == "os.rename" and Path(args[1]) == self.episode:
                        raise OSError("injected episode rename error")
                    if target == "os.replace" and Path(args[1]) == self.catalog:
                        raise OSError("injected catalog replace error")
                    if target.endswith("atomic_write_guarded_catalog"):
                        raise OSError("injected catalog staging error")
                    return original(*args, **kwargs)
                with patch(target, side_effect=fail), self.assertRaises(self.intake.IntakeError):
                    self.admit(confirmed=True)
                self.assertEqual(snapshot(self.library), before)
        self.assertEqual(self.admit(confirmed=True)["result"], "created")

    def test_rollback_does_not_create_catalog_or_episodes_in_blank_target(self):
        self.library = self.base / "blank-library"
        self.library.mkdir()
        with patch("os.replace", side_effect=OSError), self.assertRaises(self.intake.IntakeError):
            self.admit(confirmed=True)
        self.assertEqual(snapshot(self.library), {})

    def test_interruption_between_renames_recovers_verified_episode(self):
        before = self.catalog.read_bytes()
        class Interrupted(BaseException):
            pass
        with patch("os.replace", side_effect=Interrupted):
            with self.assertRaises(Interrupted):
                self.admit(confirmed=True)
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertTrue((self.episode / "episode.json").is_file())
        before = snapshot(self.episode)
        commands = len(COMMANDS)
        self.assertEqual(self.admit(confirmed=True)["result"], "catalog_recovered")
        self.assertEqual(snapshot(self.episode), before)
        self.assertEqual(load(self.catalog)["episodes"][-1], load(self.episode / "episode.json"))
        self.assertEqual(len(COMMANDS), commands)

    def test_concurrent_same_source_never_encodes_twice(self):
        before = sum("libmp3lame" in cmd for cmd in COMMANDS)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.admit(confirmed=True)["result"], range(3)))
        self.assertCountEqual(results, ["created", "already_present", "already_present"])
        self.assertEqual(sum("libmp3lame" in cmd for cmd in COMMANDS), before + 1)
        self.assertEqual(len(load(self.catalog)["episodes"]), 2)

    def test_cli_requires_explicit_inputs_target_and_confirmation(self):
        before = snapshot(self.library)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.intake.main(["--inputs", str(self.source), "--library", str(self.library)])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(snapshot(self.library), before)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.intake.main(["--inputs", str(self.source), "--library", str(self.library),
                                     "--confirm-selected-original"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["result"], "created")
