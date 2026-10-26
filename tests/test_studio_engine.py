import json
import sys
import tempfile
import wave
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_engine import (select_preview_segments, StudioEngine, Cancelled, asr_pieces, select_references, fingerprint, raw_reference_pieces, classify_failure, StudioError, user_facing_message, clean_tts_text, speakable_tts, SILENCE_FRAMES, SILENCE_RATE)
from diarization_cache import assign_studio_voices, VOICE_ORDER
from studio_preview import preview_limit
from podcast_cn import DeepSeekConfigurationError, PodcastPipelineError
from tts_backends import TTSBackendError
from podcast_captions import CaptionPiece
from diarization_core import SpeakerAssignment, DiarizationTurn

class StudioTests(unittest.TestCase):
    def test_job_threshold_only_applies_to_diarization_worker(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            engine.diarization_threshold = 0.9
            with patch.object(engine, 'command') as command:
                engine.runner(['python', '/tmp/sherpa_diarization_worker.py', '--audio', 'a.wav'])
                self.assertEqual(command.call_args.args[0][-2:], ['--cluster-threshold', '0.9'])
                engine.runner(['ffmpeg', '-version'])
                self.assertEqual(command.call_args.args[0], ['ffmpeg', '-version'])
            with self.assertRaises(ValueError):
                engine.prepare({'diarization_threshold': 2}, Path('unused.wav'))

    def test_asr_timestamps_and_empty_filter(self):
        result = asr_pieces([{'start': 1, 'end': 3, 'text': ' Hello '}, {'start': 3, 'end': 3, 'text': 'bad'}], 60000)
        self.assertEqual((result[0].start_ms, result[0].end_ms, result[0].text), (61000, 63000, 'Hello'))
        self.assertEqual(len(result), 1)

    def test_reference_rejects_overlap_and_ambiguity(self):
        p = [CaptionPiece(1000, 5000, 'Full matching English sentence.', 0)]
        a = [SpeakerAssignment(0, 's1', 4000, 1., False, 'overlap')]
        turns = [DiarizationTurn(0, 6000, 's1')]
        self.assertEqual(select_references(p, a, turns)['s1']['text'], p[0].text)
        self.assertEqual(select_references(p, a, turns + [DiarizationTurn(2000, 3000, 's2')]), {})
        self.assertEqual(select_references(p, [SpeakerAssignment(0, 's1', 4000, .5, True, 'overlap')], turns), {})

    def test_cache_changes_for_text_voice_reference(self):
        base = {'text':'你好','voice':'a','reference':'abc','model':'m'}
        for key in base:
            self.assertNotEqual(fingerprint(base), fingerprint({**base, key: 'changed'}))

    def test_cancel_and_failure_remove_source(self):
        for cancel in (True, False):
            with tempfile.TemporaryDirectory() as root:
                engine = StudioEngine(root)
                engine.temp.mkdir()
                (engine.temp / 'source.wav').write_bytes(b'private')
                if cancel:
                    (engine.job / 'cancel').touch()
                with patch.object(engine, 'acquire', side_effect=RuntimeError('secret')):
                    with self.assertRaises(Cancelled if cancel else RuntimeError):
                        engine.run({'source':{}}, 'prepare')
                self.assertFalse(engine.temp.exists())
                progress = json.loads((engine.job / 'progress.json').read_text())
                self.assertEqual(progress['stage'], 'cancelled' if cancel else 'failed')
                if not cancel:
                    self.assertEqual(progress['failed_stage'], 'source')
                self.assertNotIn('secret', json.dumps(progress))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_render_resume_and_one_segment_edit(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            engine.temp.mkdir()
            ref = engine.job / 'ref.wav'
            def audio(path):
                with wave.open(str(path), 'wb') as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(24000)
                    out.writeframes(b'\0\0' * 2400)
            audio(ref)
            doc = {'segments':[{'id':'s1','chinese':'你好','speaker_id':'a'}, {'id':'s2','chinese':'再见','speaker_id':'a'}], 'speakers':{'a':{'voice_slot':'male_1'}}, 'references':{'a':{'path':'ref.wav','text':'Hello'}}}
            with patch.object(engine, 'worker', side_effect=lambda payload: audio(payload['output'])) as worker:
                clone = {'tts': 'clone'}
                output = engine.render(clone, doc)
                self.assertTrue(output.is_file())
                self.assertEqual(worker.call_count, 2)
                engine.render(clone, doc)
                self.assertEqual(worker.call_count, 2)
                doc['segments'][1]['chinese'] = '明天见'
                engine.render(clone, doc)
                self.assertEqual(worker.call_count, 3)
                doc['segments'][1]['chinese'] = ''
                prior = output.read_bytes()
                with self.assertRaises(ValueError):
                    engine.render(clone, doc)
                self.assertEqual(prior, output.read_bytes())

    def test_safe_error_classification(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('studio_engine.subprocess.Popen', side_effect=FileNotFoundError) as spawn:
                with self.assertRaises(StudioError):
                    StudioEngine(root).command(['yt-dlp', '--version'])
                self.assertEqual(spawn.call_args.args[0], [sys.executable, '-m', 'yt_dlp', '--version'])
        for raw, code in [('HTTP Error 429 secret_token', 'source_access_denied'), ('Sign in to confirm you are not a bot secret', 'source_access_denied'), ('LocalEntryNotFoundError: cannot find an appropriate cached snapshot secret', 'model_missing'), ('ModuleNotFoundError: No module named secret', 'dependency_missing'), ('Connection refused secret', 'network_unavailable')]:
            error = classify_failure(raw)
            self.assertEqual(error.code, code)
            self.assertNotIn('secret', str(error))
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            with patch('studio_engine.subprocess.Popen', side_effect=FileNotFoundError('secret')):
                with self.assertRaises(StudioError) as context:
                    engine.command(['missing-python'])
                self.assertEqual(context.exception.code, 'dependency_missing')
                self.assertNotIn('secret', str(context.exception))
            with patch.object(engine, 'acquire', side_effect=classify_failure('HTTP Error 429 secret')):
                with self.assertRaises(StudioError):
                    engine.run({'source':{}}, 'prepare')
            progress = json.loads((engine.job / 'progress.json').read_text())
            self.assertEqual(progress['failed_stage'], 'source')
            self.assertEqual(progress['error_code'], 'source_access_denied')
            self.assertNotIn('secret', json.dumps(progress))

    def test_rolling_caption_reference_uses_full_cue(self):
        vtt = 'WEBVTT\n\n00:00.000 --> 00:03.000\nHello there\n\n00:03.000 --> 00:07.000\nHello there my friend\n'
        from podcast_captions import parse_vtt_pieces
        self.assertEqual(parse_vtt_pieces(vtt)[1].text, 'my friend')
        full = raw_reference_pieces(vtt)
        assignments = [SpeakerAssignment(0,'a',3000,.5,True,'overlap'), SpeakerAssignment(1,'a',4000,1.,False,'overlap')]
        refs = select_references(full, assignments, [DiarizationTurn(0,8000,'a')])
        self.assertEqual(refs['a']['text'], 'Hello there my friend')
        self.assertEqual(refs['a']['start_ms'], 3000)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_selected_style_invalidates_system_voice_cache(self):
        class FakeBackend:
            file_suffix = '.wav'
            def fingerprint(self): return {'model':'fake'}
            async def synthesize(self, request, path):
                with wave.open(str(path), 'wb') as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(24000)
                    out.writeframes(b'\0\0' * 2400)
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            engine.temp.mkdir()
            doc = {'segments':[{'id':'s1','chinese':'你好','speaker_id':'a'}], 'speakers':{'a':{'voice_slot':'male_1'}}, 'references':{}}
            with patch('studio_engine.build_backend', return_value=FakeBackend()):
                engine.render({'tts':'edge','style':'conversation'}, doc)
                self.assertEqual(len(list((engine.job / 'audio-cache').glob('*.wav'))),1)
                engine.render({'tts':'edge','style':'documentary'}, doc)
                self.assertEqual(len(list((engine.job / 'audio-cache').glob('*.wav'))),2)

    def test_owned_paths(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                StudioEngine(root).owned('../escape.wav')

    def test_render_blocks_while_speaker_assignment_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            doc = {
                'segments': [
                    {'id': 's1', 'chinese': '你好', 'speaker_id': 'a', 'ambiguous': True},
                ],
                'speakers': {'a': {'voice_slot': 'male_1'}},
                'references': {},
            }
            with self.assertRaisesRegex(ValueError, '说话人待核对'):
                engine.render({'tts': 'edge'}, doc)

    def test_studio_voices_start_with_male_slots(self):
        slots = assign_studio_voices([
            'SPEAKER_00', 'SPEAKER_00', 'SPEAKER_01', 'SPEAKER_02',
            'SPEAKER_03', 'SPEAKER_04', 'SPEAKER_05',
        ])
        self.assertEqual(VOICE_ORDER[0], 'female_1')
        self.assertEqual(list(slots.values()), [
            'male_1', 'male_2', 'male_3', 'female_1', 'female_2', 'male_1',
        ])

    def test_clean_tts_text_drops_markers_and_bare_punctuation(self):
        self.assertEqual(clean_tts_text('>> 你好。'), '你好。')
        self.assertEqual(clean_tts_text('你好[音乐]世界'), '你好世界')
        self.assertEqual(clean_tts_text('（笑）肌酸。'), '肌酸。')
        self.assertEqual(clean_tts_text('>>'), '')
        self.assertEqual(clean_tts_text('？'), '？')
        self.assertFalse(speakable_tts(clean_tts_text('？')))
        self.assertFalse(speakable_tts(clean_tts_text('。')))
        self.assertFalse(speakable_tts(clean_tts_text('[Music]')))
        self.assertTrue(speakable_tts(clean_tts_text('>> 肌酸。')))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_accept_unreviewed_renders_many_ambiguous_segments(self):
        class FakeBackend:
            file_suffix = '.mp3'
            def fingerprint(self):
                return {'model': 'fake'}
            async def synthesize(self, request, path):
                with wave.open(str(path), 'wb') as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(24000)
                    out.writeframes(b'\0\0' * 2400)
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            engine.temp.mkdir()
            doc = {
                'segments': [
                    {'id': f's{i}', 'chinese': f'第{i}句', 'speaker_id': 'a' if i % 2 == 0 else 'b', 'ambiguous': True}
                    for i in range(8)
                ],
                'speakers': {'a': {'voice_slot': 'male_1'}, 'b': {'voice_slot': 'male_2'}},
                'references': {},
            }
            with patch('studio_engine.build_backend', return_value=FakeBackend()):
                output = engine.render({'tts': 'edge'}, doc, accept_unreviewed=True)
            self.assertTrue(output.is_file())
            self.assertTrue(all(segment['ambiguous'] for segment in doc['segments']))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_edge_writes_silence_instead_of_speaking_bare_punctuation(self):
        calls = []
        class FakeBackend:
            file_suffix = '.mp3'
            def fingerprint(self):
                return {'model': 'fake'}
            async def synthesize(self, request, path):
                calls.append(request.text)
                with wave.open(str(path), 'wb') as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(24000)
                    out.writeframes(b'\0\0' * 2400)
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            engine.temp.mkdir()
            doc = {
                'segments': [
                    {'id': 'punct', 'chinese': '？', 'speaker_id': 'a'},
                    {'id': 'words', 'chinese': '>> 你好[音乐]', 'speaker_id': 'a'},
                ],
                'speakers': {'a': {'voice_slot': 'male_1'}},
                'references': {},
            }
            with patch('studio_engine.build_backend', return_value=FakeBackend()):
                engine.render({'tts': 'edge'}, doc)
            self.assertEqual(calls, ['你好'])
            frames = []
            for path in (engine.job / 'audio-cache').glob('*.wav'):
                with wave.open(str(path), 'rb') as clip:
                    self.assertEqual(clip.getframerate(), SILENCE_RATE)
                    frames.append(clip.getnframes())
            self.assertIn(SILENCE_FRAMES, frames)

    def test_incomplete_export_keeps_previous(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            engine.temp.mkdir()
            prior = engine.job / 'output.mp3'
            prior.write_bytes(b'prior')
            doc = {'segments':[{'id':'s1','chinese':'','speaker_id':'a'}], 'speakers':{}, 'references':{}}
            with self.assertRaises(ValueError):
                engine.render({}, doc)
            self.assertEqual(prior.read_bytes(), b'prior')


    def test_preview_prefers_distinct_speakers(self):
        segments = [
            {'id': 'a', 'speaker_id': 's0', 'chinese': '一'},
            {'id': 'b', 'speaker_id': 's0', 'chinese': '二'},
            {'id': 'c', 'speaker_id': 's1', 'chinese': '三'},
            {'id': 'd', 'speaker_id': 's2', 'chinese': '四'},
        ]
        chosen = select_preview_segments(segments, 3)
        self.assertEqual([s['id'] for s in chosen], ['a', 'c', 'd'])
        self.assertEqual([s['speaker_id'] for s in chosen], ['s0', 's1', 's2'])

    def test_preview_limit_covers_speakers_up_to_five(self):
        two = [
            {'id': 'a', 'speaker_id': 's0', 'chinese': '短'},
            {'id': 'b', 'speaker_id': 's0', 'chinese': '更长的一句'},
            {'id': 'c', 'speaker_id': 's1', 'chinese': '另一人'},
        ]
        self.assertEqual(preview_limit(two), 2)
        self.assertEqual(
            [s['id'] for s in select_preview_segments(two, preview_limit(two))],
            ['b', 'c'],
        )
        six = [
            {'id': str(i), 'speaker_id': f's{i}', 'chinese': '啊' * (i + 1)}
            for i in range(6)
        ]
        self.assertEqual(preview_limit(six), 5)
        chosen = select_preview_segments(six, preview_limit(six))
        self.assertEqual(len(chosen), 5)
        self.assertEqual(len({s['speaker_id'] for s in chosen}), 5)
        self.assertNotIn('s5', {s['speaker_id'] for s in chosen})

    def test_known_errors_reach_progress_and_secrets_do_not(self):
        cases = [
            DeepSeekConfigurationError('缺少 DEEPSEEK_API_KEY,请先配置 DeepSeek API Key'),
            TTSBackendError('系统音色服务暂时拒绝连接。请升级 edge-tts'),
        ]
        for error in cases:
            with tempfile.TemporaryDirectory() as root:
                engine = StudioEngine(root)
                source = Path(root) / 'source.wav'
                source.write_bytes(b'x')
                with patch.object(engine, 'acquire', side_effect=error):
                    with self.assertRaises(type(error)):
                        engine.run({'source': {'kind': 'local', 'value': str(source)}}, 'prepare')
                progress = json.loads((engine.job / 'progress.json').read_text())
                self.assertEqual(progress['message'], str(error))
        leaked = PodcastPipelineError('失败 https://evil.example/file?token=secret')
        self.assertIsNone(user_facing_message(leaked))
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            source = Path(root) / 'source.wav'
            source.write_bytes(b'x')
            with patch.object(engine, 'acquire', side_effect=leaked):
                with self.assertRaises(PodcastPipelineError):
                    engine.run({'source': {'kind': 'local', 'value': str(source)}}, 'prepare')
            progress = json.loads((engine.job / 'progress.json').read_text())
            self.assertNotIn('secret', progress['message'])
            self.assertNotIn('https://', progress['message'])

    def test_private_video_has_a_fixed_message(self):
        error = classify_failure('ERROR: Private video. Sign in if you have been granted access to this video')
        self.assertEqual(error.code, 'source_access_denied')
        self.assertIn('私享', str(error))
        self.assertNotIn('granted', str(error))

    def test_valid_audio_does_not_hide_missing_ffmpeg(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            target = engine.job / 'clip.wav'
            target.write_bytes(b'x' * 64)
            with patch.object(engine, 'command', side_effect=StudioError('dependency_missing', '找不到运行程序')):
                with self.assertRaises(StudioError) as caught:
                    engine.valid_audio(target)
            self.assertEqual(caught.exception.code, 'dependency_missing')
            with patch.object(engine, 'command', side_effect=StudioError('process_failed', '处理程序执行失败')):
                self.assertFalse(engine.valid_audio(target))


if __name__ == '__main__': unittest.main()
