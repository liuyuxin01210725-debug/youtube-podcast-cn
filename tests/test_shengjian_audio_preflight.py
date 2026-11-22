#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from shengjian_audio_preflight import (
    PREVIEW_TARGET_SEC,
    probe,
    refuse_if_blocked_or_review,
    run_preflight,
    sha256_file,
    which_ffmpeg,
    write_preview,
    preview_offsets,
)


def require_ffmpeg():
    try:
        return which_ffmpeg()
    except Exception as exc:
        raise unittest.SkipTest(f'ffmpeg/ffprobe required: {exc}')


def ffmpeg_write(path, filtergraph, duration, ffmpeg):
    result = subprocess.run(
        [ffmpeg, '-y', '-f', 'lavfi', '-i', filtergraph, '-t', str(duration),
         '-ac', '1', '-ar', '44100', '-c:a', 'libmp3lame', '-q:a', '4', str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not path.is_file():
        raise AssertionError(result.stderr[-500:])
    return path


class AudioPreflightTests(unittest.TestCase):
    def setUp(self):
        self.ffmpeg, self.ffprobe = require_ffmpeg()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_file_is_blocked(self):
        bad = self.root / 'bad.mp3'
        bad.write_bytes(b'not an audio file at all')
        report = run_preflight(bad, output_dir=self.root / 'out-bad')
        self.assertEqual(report['verdict'], 'BLOCK')
        self.assertFalse(report['publish_ready'])
        self.assertFalse(report['audio_quality_pass'])
        self.assertTrue(any(item['code'] == 'decode' for item in report['findings']))
        with self.assertRaises(Exception):
            refuse_if_blocked_or_review(report)

    def test_intentional_pause_below_threshold_is_not_damage(self):
        # 1s tone + 4.5s silence + 1s tone: a legal pause, not a >5s silence.
        path = self.root / 'pause.mp3'
        parts = self.root / 'parts'
        parts.mkdir()
        a = ffmpeg_write(parts / 'a.mp3', 'sine=frequency=440', 1.0, self.ffmpeg)
        s = ffmpeg_write(parts / 's.mp3', 'anullsrc=r=44100:cl=mono', 4.5, self.ffmpeg)
        b = ffmpeg_write(parts / 'b.mp3', 'sine=frequency=440', 1.0, self.ffmpeg)
        listing = parts / 'concat.txt'
        listing.write_text(f"file '{a}'\nfile '{s}'\nfile '{b}'\n", encoding='utf-8')
        result = subprocess.run(
            [self.ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', str(listing),
             '-c:a', 'libmp3lame', '-q:a', '4', str(path)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = run_preflight(path, output_dir=self.root / 'out-pause')
        silences = [item for item in report['findings'] if item.get('code') == 'long_silence']
        self.assertEqual(silences, [])
        # May still be REVIEW due to no duration baseline; must not BLOCK as damaged.
        self.assertNotEqual(report['verdict'], 'BLOCK')
        self.assertFalse(report['audio_quality_pass'])

    def test_long_silence_is_review_not_block(self):
        path = self.root / 'long-silence.mp3'
        ffmpeg_write(path, 'anullsrc=r=44100:cl=mono', 7.0, self.ffmpeg)
        report = run_preflight(path, output_dir=self.root / 'out-sil')
        self.assertEqual(report['verdict'], 'REVIEW')
        self.assertTrue(any(item.get('code') == 'long_silence' for item in report['findings']))
        self.assertFalse(any(item.get('code') == 'long_silence' and item.get('severity') == 'BLOCK' for item in report['findings']))
        with self.assertRaises(Exception) as ctx:
            refuse_if_blocked_or_review(report)
        self.assertIn('不能直接发布 ready', str(ctx.exception))

    def test_uncalibrated_char_count_is_review_not_pass(self):
        path = self.root / 'tone.mp3'
        ffmpeg_write(path, 'sine=frequency=440', 3.0, self.ffmpeg)
        report = run_preflight(path, output_dir=self.root / 'out-chars', character_count=1200, baseline=None)
        self.assertEqual(report['verdict'], 'REVIEW')
        item = next(i for i in report['findings'] if i.get('code') == 'duration_vs_chars')
        self.assertEqual(item['severity'], 'REVIEW')
        self.assertIn('没有校准', item['reason'])
        self.assertFalse(report['publish_ready'])
        self.assertFalse(report['audio_quality_pass'])

    def test_short_preview_is_honest_and_not_padded(self):
        path = self.root / 'short.mp3'
        ffmpeg_write(path, 'sine=frequency=330', 8.0, self.ffmpeg)
        report = run_preflight(path, output_dir=self.root / 'out-short')
        preview = report['preview']
        self.assertTrue(preview['honest_short'])
        self.assertFalse(preview['padded'])
        self.assertLess(preview['output_duration_sec'], 45)
        # Allow encoder padding of well under a second, never pad up to the 45s target.
        self.assertLess(preview['output_duration_sec'], 12)
        self.assertEqual(len(preview['segments']), 1)
        self.assertAlmostEqual(preview['segments'][0]['duration_sec'], report['source_info']['duration_sec'], delta=0.25)

    def test_existing_real_samples_preflight_is_not_quality_pass(self):
        short_27 = Path(os.environ.get('SHENGJIAN_SAMPLE_SHORT27', ''))
        short_45 = Path(os.environ.get('SHENGJIAN_SAMPLE_SHORT45', ''))
        if not short_27.is_file() or not short_45.is_file():
            self.skipTest('set SHENGJIAN_SAMPLE_SHORT27 and SHENGJIAN_SAMPLE_SHORT45 to local copies')
        out = self.root / 'real-samples'
        reports = {}
        for label, source in (('short27', short_27), ('short45', short_45)):
            report = run_preflight(source, output_dir=out / label)
            self.assertFalse(report['audio_quality_pass'])
            self.assertFalse(report['publish_ready'])
            self.assertTrue(report['ear_required'])
            self.assertIn(report['verdict'], {'REVIEW', 'CLEAR', 'BLOCK'})
            preview = report['preview']
            self.assertTrue(Path(preview['path']).is_file())
            src_hash = sha256_file(source)
            self.assertEqual(preview['source_sha256'], src_hash)
            if label == 'short27':
                self.assertTrue(preview['honest_short'])
                self.assertLess(preview['output_duration_sec'], 45)
            if label == 'short45':
                self.assertFalse(preview['honest_short'])
                self.assertEqual(len(preview['segments']), 3)
                roles = [seg['role'] for seg in preview['segments']]
                self.assertEqual(roles, ['head', 'mid', 'tail'])
                duration = report['source_info']['duration_sec']
                self.assertGreaterEqual(duration, PREVIEW_TARGET_SEC)
                # Offsets should cover the head, a centered mid, and the true tail.
                self.assertAlmostEqual(preview['segments'][0]['start_sec'], 0.0, places=3)
                self.assertAlmostEqual(preview['segments'][2]['end_sec'], duration, delta=0.05)
            reports[label] = {
                'verdict': report['verdict'],
                'source_sha256': src_hash,
                'duration_sec': report['source_info']['duration_sec'],
                'preview_path': preview['path'],
                'preview_sha256': sha256_file(preview['path']),
                'preview_duration_sec': preview['output_duration_sec'],
                'segments': preview['segments'],
                'report_path': report['report_path'],
                'findings': [item.get('code') for item in report['findings']],
            }
        print('REAL_SAMPLE_PREFLIGHT=' + json.dumps(reports, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
