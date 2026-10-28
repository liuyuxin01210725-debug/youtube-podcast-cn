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
    @staticmethod
    def speaker_document(count):
        return {'segments': [{'id': str(i), 'chinese': '你好', 'speaker_id': f's{i}'} for i in range(count)],
                'speakers': {speaker: {'voice_slot': slot} for speaker, slot in assign_studio_voices([f's{i}' for i in range(count)]).items()},
                'references': {}, 'voices_confirmed': True}

    def test_expected_two_rejects_six_before_any_tts_even_when_confirmed(self):
        for backend in ('edge', 'clone'):
            for preview in (None, 1, 2):
                with self.subTest(backend=backend, preview=preview), tempfile.TemporaryDirectory() as root:
                    engine = StudioEngine(root)
                    doc = self.speaker_document(6)
                    with patch('studio_engine.build_backend') as build, patch.object(engine, 'worker') as worker:
                        with self.assertRaisesRegex(ValueError, '声明 2 人，实际 6 人'):
                            engine.render({'tts': backend, 'expected_speakers': 2}, doc, preview, accept_unreviewed=True)
                        build.assert_not_called()
                        worker.assert_not_called()
                        self.assertFalse((engine.job / 'audio-cache').exists())

    def test_two_people_cannot_share_a_voice_even_when_confirmed(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            doc = self.speaker_document(2)
            doc['speakers']['s1']['voice_slot'] = 'male_1'
            with patch('studio_engine.build_backend') as build:
                with self.assertRaisesRegex(ValueError, '音色硬闸'):
                    engine.render({'expected_speakers': 2}, doc, accept_unreviewed=True)
                build.assert_not_called()

    def test_diarization_count_gate_precedes_translation(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            pieces = [CaptionPiece(0, 1000, 'Hello', 0)]
            turns = [DiarizationTurn(i * 1000, (i + 1) * 1000, f's{i}') for i in range(6)]
            with patch.object(engine, 'transcribe', return_value=(pieces, 'subtitles')), patch.object(engine, 'diarize_with_progress', return_value=turns) as diarize, patch('studio_engine.create_translation_client') as translate:
                with self.assertRaisesRegex(ValueError, '声明 2 人，实际 6 人'):
                    engine.prepare({'expected_speakers': 2}, Path('unused.wav'))
                self.assertEqual(diarize.call_args.args[1].exact, 2)
                translate.assert_not_called()

    def test_count_failure_is_recoverable_and_keeps_existing_output(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            (engine.job / 'document.json').write_text(json.dumps(self.speaker_document(6)))
            (engine.job / 'output.mp3').write_bytes(b'previous')
            with patch.object(engine, 'publish_private_library') as publish:
                with self.assertRaisesRegex(ValueError, '人数硬闸'):
                    engine.run({'source': {}, 'expected_speakers': 2}, 'render', accept_unreviewed=True)
                publish.assert_not_called()
            progress = json.loads((engine.job / 'progress.json').read_text())
            self.assertEqual(progress['error_code'], 'speaker_gate_failed')
            self.assertIn('修正说话人分配后重试', progress['message'])
            self.assertEqual((engine.job / 'output.mp3').read_bytes(), b'previous')

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
                clone = {'tts': 'clone', 'expected_speakers': 1}
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
                engine.render({'tts':'edge','expected_speakers':1,'style':'conversation'}, doc)
                self.assertEqual(len(list((engine.job / 'audio-cache').glob('*.wav'))),1)
                engine.render({'tts':'edge','expected_speakers':1,'style':'documentary'}, doc)
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
                engine.render({'tts': 'edge', 'expected_speakers': 1}, doc)

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
    def test_expected_two_with_two_slots_renders_even_with_review_override(self):
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
                output = engine.render({'tts': 'edge', 'expected_speakers': 2}, doc, accept_unreviewed=True)
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
                engine.render({'tts': 'edge', 'expected_speakers': 1}, doc)
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

    def test_transcribe_saves_chinese_title_before_subtitle_failure(self):
        url = 'https://www.youtube.com/watch?v=Jk7RAkFN4vk'
        info = {
            'id': 'Jk7RAkFN4vk',
            'title': 'Creatine Expert: Creatine Is The Secret To Weight Loss',
            'channel': 'The Diary Of A CEO',
            'webpage_url': url,
        }
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            engine.temp.mkdir()
            wav = engine.temp / 'source.wav'
            with wave.open(str(wav), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b'\0\0' * 1600)
            write = engine.job / 'state.json'
            write.write_text(json.dumps({'title': url, 'status': 'running'}), encoding='utf-8')
            with patch('studio_engine.fetch_video_info', return_value=info), patch('studio_engine.download_english_subtitles', side_effect=PodcastPipelineError('no captions')), patch('studio_publish_icloud.translate_episode_topic', return_value='肌酸专家：肌酸是减肥的秘密'), patch.object(engine, 'ffmpeg', side_effect=Cancelled('stop')):
                with self.assertRaises(Cancelled):
                    engine.transcribe(wav, {'source': {'kind': 'youtube', 'value': url}, 'provider': 'deepseek'})
            metadata = json.loads((engine.job / 'metadata.json').read_text(encoding='utf-8'))
            state = json.loads((engine.job / 'state.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['source_title'], info['title'])
            self.assertEqual(metadata['display_title'], '肌酸专家：肌酸是减肥的秘密 · The Diary Of A CEO')
            self.assertEqual(state['title'], metadata['display_title'])
            self.assertNotIn('http', state['title'])

    def test_book_prepare_skips_diarization_and_refuses_one_whole_file(self):
        paragraph = '这是用来填满一块听书的句子。' * 40
        chunk = '\n\n'.join([paragraph] * 8)
        text = '第一章 开端\n\n' + chunk + '\n\n第二章 后来\n\n' + chunk
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            path = engine.job / 'source.txt'
            path.write_text(text, encoding='utf-8')
            config = {
                'content_type': 'book', 'expected_speakers': 1, 'tts': 'edge',
                'diarization': 'skipped', 'book_language': 'zh',
                'source': {'kind': 'text', 'value': str(path)},
            }
            with patch.object(engine, 'diarize_with_progress') as diarize, patch.object(engine, 'acquire') as acquire, patch('studio_engine.create_translation_client') as translate, patch('studio_engine.build_backend') as build, patch.object(engine, 'publish_private_library') as publish:
                engine.run(config, 'prepare')
                diarize.assert_not_called()
                acquire.assert_not_called()
                translate.assert_not_called()
                build.assert_not_called()
                publish.assert_not_called()
            book = json.loads((engine.job / 'book.json').read_text())
            blocks = [block for chapter in book['chapters'] for block in chapter['blocks']]
            self.assertGreaterEqual(len(book['chapters']), 2)
            self.assertGreaterEqual(len(blocks), 2)
            self.assertEqual(book['narrator']['voice_slot'], 'male_1')
            self.assertFalse(book['whole_book_file'])
            document = json.loads((engine.job / 'document.json').read_text())
            self.assertEqual({segment['speaker_id'] for segment in document['segments']}, {'narrator'})
            self.assertEqual(len(document['segments']), len(blocks))
            progress = json.loads((engine.job / 'progress.json').read_text())
            self.assertEqual(progress['chapter_id'], book['progress']['chapter_id'])
            self.assertEqual(progress['block_id'], book['progress']['block_id'])
            self.assertEqual(progress['offset_chars'], 0)
            self.assertFalse((engine.job / 'output.mp3').exists())
            with patch.object(engine, 'diarize_with_progress') as diarize, patch('studio_engine.build_backend') as build, patch.object(engine, 'publish_private_library') as publish:
                with self.assertRaisesRegex(ValueError, '整本一条'):
                    engine.run(config, 'run')
                diarize.assert_not_called()
                build.assert_not_called()
                publish.assert_not_called()
            failed = json.loads((engine.job / 'progress.json').read_text())
            self.assertEqual(failed['stage'], 'failed')
            self.assertEqual(failed['chapter_id'], book['progress']['chapter_id'])
            self.assertEqual(failed['offset_chars'], 0)
            self.assertIn('整本一条', failed['message'])
            self.assertFalse((engine.job / 'output.mp3').exists())

    def test_book_gate_blocks_a_second_voice_and_never_fetches_a_url(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            path = engine.job / 'source.txt'
            path.write_text('短。', encoding='utf-8')
            with patch.object(engine, 'diarize_with_progress') as diarize, patch.object(engine, 'acquire') as acquire:
                with self.assertRaisesRegex(ValueError, '禁止声纹'):
                    engine.prepare({'content_type': 'book', 'expected_speakers': 1}, Path('unused.wav'))
                with self.assertRaisesRegex(ValueError, '不会从链接抓书'):
                    engine.run({'content_type': 'book', 'source': {'kind': 'text', 'value': 'https://example.com/book.txt'}}, 'prepare')
                with self.assertRaisesRegex(ValueError, '书听只允许 1'):
                    engine.run({'content_type': 'book', 'expected_speakers': 2, 'source': {'kind': 'text', 'value': str(path)}}, 'prepare')
                diarize.assert_not_called()
                acquire.assert_not_called()
            self.assertFalse((engine.job / 'book.json').exists())
            self.assertFalse((engine.job / 'output.mp3').exists())
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            with patch.object(engine, 'diarize_with_progress') as diarize, patch.object(engine, 'acquire') as acquire:
                with self.assertRaisesRegex(ValueError, '不会下载或抓取'):
                    engine.run({'content_type': 'book', 'source': {'kind': 'youtube', 'value': 'https://youtu.be/abcdefghijk'}}, 'prepare')
                diarize.assert_not_called()
                acquire.assert_not_called()

    def test_sponsor_window_is_not_translated_and_does_not_add_a_voice(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            pieces = [
                CaptionPiece(0, 1800, 'What did you learn from that year?', 0),
                CaptionPiece(2400, 4000, 'This episode is sponsored by Athletic Greens.', 1),
                CaptionPiece(4600, 6200, 'Visit the site slash offer and use code DOAC.', 2),
                CaptionPiece(6800, 8400, 'What happened next in the lab?', 3),
                CaptionPiece(9000, 10800, 'We kept the experiment running.', 4),
            ]
            turns = [
                DiarizationTurn(0, 1800, 'host'),
                DiarizationTurn(2400, 4000, 'adread'),
                DiarizationTurn(4600, 6200, 'host'),
                DiarizationTurn(6800, 8400, 'host'),
                DiarizationTurn(9000, 10800, 'guest'),
            ]
            seen = []

            class Client:
                translation_identity = 'test'

                def translate(self, batch):
                    seen.extend(segment.text for segment in batch)
                    return {segment.id: '译' for segment in batch}

            with patch.object(engine, 'transcribe', return_value=(pieces, 'asr')), \
                    patch.object(engine, 'diarize_with_progress', return_value=turns) as diarize, \
                    patch.object(engine, 'duration_ms', return_value=12000), \
                    patch.object(engine, 'ffmpeg') as ffmpeg, \
                    patch.object(engine, 'credential', return_value='test-key'), \
                    patch('studio_engine.create_translation_client', return_value=Client()):
                document = engine.prepare({'expected_speakers': 2, 'content_type': 'conversation'}, Path('unused.wav'))
            self.assertEqual(diarize.call_args.args[1].exact, 2)
            ffmpeg.assert_not_called()
            self.assertEqual(seen, [
                'What did you learn from that year?',
                'What happened next in the lab?',
                'We kept the experiment running.',
            ])
            ads = [segment for segment in document['segments'] if segment['is_ad']]
            self.assertEqual(len(ads), 2)
            self.assertEqual(ads[0]['chinese'], '')
            self.assertEqual(ads[0]['folded_from'], 'adread')
            self.assertEqual(set(document['speakers']), {'host', 'guest'})
            self.assertTrue(all(item['voice_slot'].startswith('male_') for item in document['speakers'].values()))
            spoken = [segment['text'] for segment in document['segments'] if not segment['is_ad']]
            self.assertEqual(spoken, seen)

    def test_three_real_speakers_still_fail_the_gate(self):
        with tempfile.TemporaryDirectory() as root:
            engine = StudioEngine(root)
            pieces = [
                CaptionPiece(0, 1000, 'Hello from the host.', 0),
                CaptionPiece(1000, 2000, 'Hello from the guest.', 1),
                CaptionPiece(2000, 3000, 'Hello from the producer.', 2),
            ]
            turns = [DiarizationTurn(i * 1000, (i + 1) * 1000, speaker) for i, speaker in enumerate(('host', 'guest', 'producer'))]
            with patch.object(engine, 'transcribe', return_value=(pieces, 'asr')), \
                    patch.object(engine, 'diarize_with_progress', return_value=turns), \
                    patch.object(engine, 'duration_ms') as duration, \
                    patch('studio_engine.create_translation_client') as translate:
                with self.assertRaisesRegex(ValueError, '声明 2 人，实际 3 人'):
                    engine.prepare({'expected_speakers': 2}, Path('unused.wav'))
                translate.assert_not_called()
                duration.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_render_drops_sponsor_lines(self):
        calls = []

        class FakeBackend:
            file_suffix = '.wav'

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
                    {'id': 's1', 'chinese': '你好', 'speaker_id': 'host', 'is_ad': False},
                    {'id': 's2', 'chinese': '这段广告不该被读出来', 'speaker_id': 'host', 'is_ad': True},
                    {'id': 's3', 'chinese': '继续聊', 'speaker_id': 'guest', 'is_ad': False},
                ],
                'speakers': {'host': {'voice_slot': 'male_1'}, 'guest': {'voice_slot': 'male_2'}},
                'references': {},
            }
            with patch('studio_engine.build_backend', return_value=FakeBackend()):
                output = engine.render({'tts': 'edge', 'expected_speakers': 2}, doc)
            self.assertEqual(calls, ['你好', '继续聊'])
            self.assertTrue(output.is_file())
            self.assertTrue(doc['segments'][1]['is_ad'])

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
