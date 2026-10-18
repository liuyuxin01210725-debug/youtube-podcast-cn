import json
import sys
import tempfile
import wave
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from studio_engine import StudioEngine, Cancelled, asr_pieces, select_references, fingerprint, raw_reference_pieces, classify_failure, StudioError
from podcast_captions import CaptionPiece
from diarization_core import SpeakerAssignment, DiarizationTurn

class StudioTests(unittest.TestCase):
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
                output = engine.render({}, doc)
                self.assertTrue(output.is_file())
                self.assertEqual(worker.call_count, 2)
                engine.render({}, doc)
                self.assertEqual(worker.call_count, 2)
                doc['segments'][1]['chinese'] = '明天见'
                engine.render({}, doc)
                self.assertEqual(worker.call_count, 3)
                doc['segments'][1]['chinese'] = ''
                prior = output.read_bytes()
                with self.assertRaises(ValueError):
                    engine.render({}, doc)
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

if __name__ == '__main__': unittest.main()
