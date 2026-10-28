import http.client, json, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from studio_server import Studio, Handler, ThreadingHTTPServer, write, read

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.studio=Studio(self.tmp.name)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.http.studio=self.studio
        self.metadata_patch=patch('studio_server.lookup_source_metadata', return_value=None)
        self.topic_patch=patch('studio_server.lookup_chinese_topic', return_value=None)
        self.metadata_patch.start(); self.topic_patch.start()
        threading.Thread(target=self.http.serve_forever,daemon=True).start(); self.port=self.http.server_port
    def tearDown(self):
        self.studio.stop=True; self.http.shutdown(); self.http.server_close(); self.tmp.cleanup()
        self.topic_patch.stop(); self.metadata_patch.stop()
    def request(self,path,data=None,headers=None,method=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port)
        h={'X-CSRF-Token':self.studio.token}; h.update(headers or {})
        c.request(method or ('POST' if data is not None else 'GET'),path,json.dumps(data).encode() if data is not None else None,h)
        r=c.getresponse(); result=(r.status,r.read()); c.close(); return result
    def create(self):
        return self.studio.create([{'kind':'youtube','value':'https://youtu.be/abc'}],{'action':'prepare'})[0]
    def test_origin_host_csrf_and_traversal(self):
        self.assertEqual(self.request('/api/jobs',{}, {'Origin':'https://evil.example'})[0],400)
        self.assertEqual(self.request('/api/jobs',{}, {'Host':'evil.example'})[0],400)
        self.assertEqual(self.request('/api/jobs',{}, {'X-CSRF-Token':'bad'})[0],400)
        self.assertEqual(self.request('/api/jobs/../../etc/files/passwd')[0],404)
        self.assertEqual(self.request('/api/session')[0],200)
    def test_preview_command_covers_up_to_five_speakers(self):
        ident = self.create()
        self.studio.action(ident, 'cancel')
        p = self.studio.job(ident)
        segments = [
            {'id': str(i), 'chinese': '你好', 'speaker_id': f's{i}', 'text': 'Hi'}
            for i in range(6)
        ]
        write(p / 'document.json', {
            'segments': segments,
            'speakers': {f's{i}': {'voice_slot': 'male_1'} for i in range(6)},
            'references': {},
        })
        args = self.studio.engine_args(p, {'action': 'render', 'preview': True})
        self.assertEqual(args[args.index('--preview') + 1], '5')
        segments = segments[:2]
        write(p / 'document.json', {
            'segments': segments,
            'speakers': {f's{i}': {'voice_slot': 'male_1'} for i in range(2)},
            'references': {},
        })
        args = self.studio.engine_args(p, {'action': 'render', 'preview': True})
        self.assertEqual(args[args.index('--preview') + 1], '2')

    def test_new_jobs_default_to_system_voice(self):
        ident = self.create()
        self.assertEqual(read(self.studio.job(ident) / 'config.json')['tts'], 'edge')
        self.assertEqual(read(self.studio.job(ident) / 'config.json')['expected_speakers'], 2)
        self.assertTrue(read(self.studio.job(ident) / 'config.json')['prefer_male_slots'])

    def test_expected_speaker_defaults_overrides_and_atomic_validation(self):
        sources = [{'kind': 'web', 'value': 'https://example.com/solo'}]
        ident = self.studio.create(sources, {'content_type': 'monologue'})[0]
        self.assertEqual(read(self.studio.job(ident) / 'config.json')['expected_speakers'], 1)
        ident = self.studio.create(sources, {'expected_speakers': 3})[0]
        self.assertEqual(read(self.studio.job(ident) / 'config.json')['expected_speakers'], 3)
        self.assertTrue(read(self.studio.job(ident) / 'config.json')['prefer_male_slots'])
        ident = self.studio.create(sources, {'expected_speakers': 4})[0]
        self.assertFalse(read(self.studio.job(ident) / 'config.json')['prefer_male_slots'])
        ident = self.studio.create([{**sources[0], 'expected_speakers': 4}], {'expected_speakers': 2})[0]
        self.assertEqual(read(self.studio.job(ident) / 'config.json')['expected_speakers'], 4)
        for invalid in (0, 6, True, 2.0, '2'):
            before = set(self.studio.root.iterdir())
            with self.assertRaises(ValueError):
                self.studio.create([sources[0], {**sources[0], 'value': 'https://example.com/b', 'expected_speakers': invalid}], {})
            self.assertEqual(set(self.studio.root.iterdir()), before)

    def test_confirm_voices_cannot_bypass_count_or_slot_gate_and_count_can_be_fixed(self):
        ident = self.create()
        self.studio.action(ident, 'cancel')
        p = self.studio.job(ident)
        doc = {'segments': [{'id': str(i), 'speaker_id': f's{i}', 'chinese': '你好'} for i in range(6)],
               'speakers': {f's{i}': {'voice_slot': 'male_1'} for i in range(6)}}
        write(p / 'document.json', doc)
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-voices', {})[0], 400)
        self.assertFalse(read(p / 'document.json').get('voices_confirmed', False))
        doc['segments'] = doc['segments'][:2]
        write(p / 'document.json', doc)
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-voices', {})[0], 400)
        doc['speakers']['s1']['voice_slot'] = 'male_2'
        write(p / 'document.json', doc)
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-voices', {})[0], 200)
        self.assertEqual(self.request(f'/api/jobs/{ident}/expected-speakers', {'expected_speakers': 1})[0], 200)
        self.assertEqual(read(p / 'config.json')['expected_speakers'], 1)
        self.assertFalse(read(p / 'document.json')['voices_confirmed'])
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-voices', {})[0], 400)
        self.assertEqual(self.request(f'/api/jobs/{ident}/expected-speakers', {'expected_speakers': 2})[0], 200)
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-voices', {})[0], 200)


    def test_youtube_create_stores_chinese_title_not_the_url(self):
        url = 'https://www.youtube.com/watch?v=Jk7RAkFN4vk'
        english = 'Creatine Expert: Creatine Is The Secret To Weight Loss'
        meta = {'source_title': english, 'channel': 'The Diary Of A CEO', 'video_id': 'Jk7RAkFN4vk', 'webpage_url': url}
        with patch('studio_server.lookup_source_metadata', return_value=meta), patch('studio_server.lookup_chinese_topic', return_value='肌酸专家：肌酸是减肥的秘密') as topic:
            ident = self.studio.create([{'kind': 'youtube', 'value': url}], {'action': 'prepare', 'provider': 'deepseek'})[0]
        job = self.studio.job(ident)
        state = read(job / 'state.json')
        metadata = read(job / 'metadata.json')
        self.assertEqual(metadata['source_title'], english)
        self.assertEqual(state['title'], '肌酸专家：肌酸是减肥的秘密 · The Diary Of A CEO')
        self.assertEqual(metadata['display_title'], state['title'])
        self.assertNotIn('http', state['title'])
        self.assertNotIn('youtube.com', state['title'])
        self.assertIn('Creatine', topic.call_args.args[0])
        self.assertNotIn('Diary Of A CEO', topic.call_args.args[0])

    def test_youtube_create_without_metadata_does_not_store_the_url(self):
        url = 'https://www.youtube.com/watch?v=Jk7RAkFN4vk'
        ident = self.studio.create([{'kind': 'youtube', 'value': url}], {'action': 'prepare'})[0]
        title = read(self.studio.job(ident) / 'state.json')['title']
        self.assertEqual(title, 'Jk7RAkFN4vk')
        self.assertNotIn('http', title.casefold())
        self.assertNotIn('youtube', title.casefold())

    def test_source_restrictions_and_atomic_validation(self):
        for value in ('file:///etc/passwd','https://evil.example/a','https://youtube.com.evil/a'):
            with self.assertRaises(ValueError): self.studio.create([{'kind':'youtube','value':value}],{})
        with self.assertRaises(ValueError): self.studio.create([{'kind':'local','value':'/etc/passwd'}],{})
        with self.assertRaises(ValueError): self.studio.create([{'kind':'youtube','value':'https://youtu.be/a'}],{'tts':'bad'})
        with self.assertRaises(ValueError): self.studio.create([{'kind':'web','value':'http://example.com/a'}],{'action':'run'})
        with self.assertRaises(ValueError): self.studio.create([{'kind':'web','value':'https://127.0.0.1/secret'}],{'action':'run'})
        self.assertEqual(self.studio.list(),[])
        ident = self.studio.create([{'kind':'web','value':'https://example.com/p/hello-world?utm_source=share'}], {'action':'run','tts':'edge'})[0]
        config = read(self.studio.job(ident) / 'config.json')
        state = read(self.studio.job(ident) / 'state.json')
        self.assertEqual(config['source'], {'kind':'web','value':'https://example.com/p/hello-world'})
        self.assertEqual(config['tts'], 'edge')
        self.assertEqual(state['action'], 'run')
        self.assertNotIn('http', state['title'])
    def test_queue_cancel_recovery_and_edit(self):
        ident=self.create(); p=self.studio.job(ident)
        self.studio.action(ident,'cancel'); self.assertEqual(self.studio.info(p)['status'],'cancelled')
        self.studio.action(ident,'prepare'); restored=Studio(self.tmp.name)
        self.assertEqual(restored.info(p)['status'],'interrupted')
        write(p/'document.json',{'segments':[{'id':'a','chinese':'你好','speaker_id':'s','text':'Hello'}],'speakers':{'s':{'voice_slot':'male_1'}},'references':{}})
        (p/'preview.mp3').write_bytes(b'preview')
        self.studio.migrate_assets(p)
        self.assertEqual(self.request(f'/api/jobs/{ident}/document',{'segments':[{'id':'a','chinese':'您好','speaker_id':'s'}]})[0],200)
        self.assertTrue(self.studio.info(p)['stale']); self.assertEqual(read(p/'document.json')['segments'][0]['chinese'],'您好')
        self.assertEqual(self.request(f'/api/jobs/{ident}/document',{'segments':[]})[0],400)
        (p/'output.mp3').symlink_to('/etc/hosts')
        self.assertEqual(self.request(f'/api/jobs/{ident}/files/output.mp3')[0],400)
    def test_asset_revisions_are_independent_and_prepare_invalidates(self):
        ident=self.create(); p=self.studio.job(ident)
        self.studio.action(ident,'cancel')
        doc={'segments':[{'id':'a','chinese':'甲','speaker_id':'s','text':'A'}],'speakers':{'s':{'voice_slot':'male_1'}},'references':{}}
        write(p/'document.json',doc)
        for n in ('preview.mp3','output.mp3'): (p/n).write_bytes(b'A')
        self.studio.migrate_assets(p)
        self.assertFalse(self.studio.info(p)['stale'])
        doc['segments'][0]['chinese']='乙';write(p/'document.json',doc)
        self.assertEqual(self.studio.info(p)['asset_stale'],{'preview.mp3':True,'output.mp3':True})
        state=read(p/'state.json');state['asset_revisions']['output.mp3']=self.studio.revision(p);write(p/'state.json',state)
        self.assertEqual(self.studio.info(p)['asset_stale'],{'preview.mp3':True,'output.mp3':False})
        state['asset_revisions']['preview.mp3']=self.studio.revision(p,True);write(p/'state.json',state)
        doc['segments'][0]['chinese']='丙';write(p/'document.json',doc)
        state['asset_revisions']['preview.mp3']=self.studio.revision(p,True);write(p/'state.json',state)
        self.assertEqual(self.studio.info(p)['asset_stale'],{'preview.mp3':False,'output.mp3':True})
        # A successful new preparation can change document synthesis inputs independently of edits.
        doc['segments'][0]['chinese']='重新准备';write(p/'document.json',doc)
        self.assertTrue(all(self.studio.info(p)['asset_stale'].values()))
    def test_backend_switch_preserves_source_document_and_invalidates_audio(self):
        ident=self.create();p=self.studio.job(ident);self.studio.action(ident,'cancel')
        config=read(p/'config.json');config['tts']='clone';write(p/'config.json',config)
        doc={'segments':[{'id':'a','chinese':'你好','speaker_id':'s'}],'speakers':{'s':{'voice_slot':'male_1'}},'references':{}}
        write(p/'document.json',doc);(p/'output.mp3').write_bytes(b'audio');self.studio.migrate_assets(p)
        source=read(p/'config.json')['source'];before=(p/'document.json').read_bytes()
        self.assertEqual(self.request(f'/api/jobs/{ident}/voice',{'tts':'edge'})[0],200)
        self.assertEqual(read(p/'config.json')['source'],source);self.assertEqual((p/'document.json').read_bytes(),before)
        self.assertTrue(self.studio.info(p)['asset_stale']['output.mp3'])
        self.assertEqual(self.request(f'/api/jobs/{ident}/voice',{'tts':'qwen3-api'})[0],400)
        self.studio.action(ident,'render')
        self.assertEqual(self.request(f'/api/jobs/{ident}/voice',{'tts':'clone'})[0],400)
    def test_bounded_upload(self):
        c=http.client.HTTPConnection('127.0.0.1',self.port)
        c.request('POST','/api/uploads',b'abc',{'X-CSRF-Token':self.studio.token,'X-Filename':'test.wav'})
        r=c.getresponse(); self.assertEqual(r.status,200); ident=json.loads(r.read())['id']; c.close()
        self.assertEqual((Path(self.tmp.name)/'.uploads'/ident).read_bytes(),b'abc')
        self.assertEqual(self.request('/api/uploads',{}, {'X-Filename':'bad.exe'})[0],400)
    def test_serial_subprocess_and_completed_file_preserved(self):
        script=Path(self.tmp.name)/'fake.py'
        script.write_text('import sys,time\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index("--job-dir")+1]); (p/"started").write_text(str(time.time()));time.sleep(.1);(p/"finished").write_text(str(time.time()))\n')
        self.studio.engine=[sys.executable,str(script)]
        one=self.create(); two=self.studio.create([{'kind':'youtube','value':'https://youtu.be/def'}],{})[0]
        (self.studio.job(one)/'output.mp3').write_bytes(b'old')
        t=threading.Thread(target=self.studio.worker,daemon=True); t.start()
        deadline=time.time()+5
        while time.time()<deadline and any(j['status']!='complete' for j in self.studio.list()): time.sleep(.05)
        self.assertTrue(all(j['status']=='complete' for j in self.studio.list()))
        p1=self.studio.job(one); p2=self.studio.job(two)
        self.assertGreaterEqual(float((p2/'started').read_text()),float((p1/'finished').read_text()))
        self.assertEqual((p1/'output.mp3').read_bytes(),b'old')
        self.studio.stop=True;t.join(2)
    def test_batch_confirm_and_risk_continue_leave_a_path_through_review(self):
        ident = self.create()
        self.studio.action(ident, 'cancel')
        p = self.studio.job(ident)
        segments = [
            {'id': str(i), 'chinese': '你好', 'speaker_id': 's0' if i % 2 == 0 else 's1', 'ambiguous': True, 'text': 'Hi'}
            for i in range(6)
        ]
        write(p / 'document.json', {
            'segments': segments,
            'speakers': {'s0': {'voice_slot': 'female_1'}, 's1': {'voice_slot': 'female_2'}},
            'references': {},
            'voices_confirmed': True,
        })
        edited = [{'id': s['id'], 'chinese': '你好呀' if s['id'] == '0' else s['chinese'], 'speaker_id': s['speaker_id']} for s in segments]
        self.assertEqual(self.request(f'/api/jobs/{ident}/document', {'segments': edited, 'voices': {'s0': 'female_1', 's1': 'female_2'}})[0], 200)
        kept = read(p / 'document.json')
        self.assertTrue(all(segment['ambiguous'] for segment in kept['segments']))
        self.assertEqual(kept['segments'][0]['chinese'], '你好呀')
        moved = [dict(item) for item in edited]
        moved[1]['speaker_id'] = 's0'
        self.assertEqual(self.request(f'/api/jobs/{ident}/document', {'segments': moved, 'voices': {'s0': 'female_1', 's1': 'female_2'}})[0], 200)
        after_move = read(p / 'document.json')['segments']
        self.assertFalse(after_move[1]['ambiguous'])
        self.assertTrue(after_move[0]['ambiguous'])
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-speakers', {})[0], 200)
        self.assertTrue(all(not segment.get('ambiguous') for segment in read(p / 'document.json')['segments']))
        doc = read(p / 'document.json')
        for segment in doc['segments']:
            segment['ambiguous'] = True
        write(p / 'document.json', doc)
        self.assertEqual(self.request(f'/api/jobs/{ident}/voice-slots', {'scheme': 'other'})[0], 400)
        self.assertEqual(self.request(f'/api/jobs/{ident}/voice-slots', {'scheme': 'male_first'})[0], 200)
        voices = read(p / 'document.json')
        self.assertEqual(voices['speakers']['s0']['voice_slot'], 'male_1')
        self.assertEqual(voices['speakers']['s1']['voice_slot'], 'male_2')
        self.assertFalse(voices['voices_confirmed'])
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-voices', {})[0], 200)
        self.assertTrue(read(p / 'document.json')['voices_confirmed'])
        self.assertEqual(self.request(f'/api/jobs/{ident}/document', {'segments': [{'id': s['id'], 'chinese': s['chinese'], 'speaker_id': s['speaker_id']} for s in read(p / 'document.json')['segments']], 'voices': {'s0': 'female_1', 's1': 'male_2'}})[0], 200)
        self.assertFalse(read(p / 'document.json')['voices_confirmed'])
        self.assertEqual(self.request(f'/api/jobs/{ident}/action', {'action': 'render', 'preview': True, 'accept_unreviewed': True})[0], 200)
        state = read(p / 'state.json')
        self.assertTrue(state['accept_unreviewed'])
        args = self.studio.engine_args(p, state)
        self.assertIn('--accept-unreviewed', args)
        self.assertTrue(self.studio.info(p)['accept_unreviewed'])
        self.studio.action(ident, 'cancel')
        self.assertEqual(self.request(f'/api/jobs/{ident}/action', {'action': 'render', 'preview': True, 'accept_unreviewed': 'true'})[0], 200)
        self.assertFalse(read(p / 'state.json')['accept_unreviewed'])

    def test_book_job_is_one_narrator_and_not_a_scraped_url(self):
        folder = self.studio.root / '.uploads'
        folder.mkdir()
        name = 'ab' * 16 + '.txt'
        (folder / name).write_text('第一章\n\n你好。\n', encoding='utf-8')
        ident = self.studio.create([{'kind': 'upload', 'value': name, 'title': '示例书.txt'}], {'content_type': 'book', 'action': 'prepare'})[0]
        config = read(self.studio.job(ident) / 'config.json')
        self.assertEqual(config['content_type'], 'book')
        self.assertEqual(config['expected_speakers'], 1)
        self.assertEqual(config['diarization'], 'skipped')
        self.assertEqual(config['tts'], 'edge')
        self.assertEqual(config['source']['kind'], 'text')
        self.assertTrue(config['source']['value'].endswith('.txt'))
        self.assertNotIn('diarization_threshold', config)
        self.assertEqual(read(self.studio.job(ident) / 'state.json')['title'], '示例书')
        jobs_before = {p.name for p in self.studio.root.iterdir() if p.is_dir() and p.name != '.uploads'}
        with self.assertRaisesRegex(ValueError, '不会从网上抓书'):
            self.studio.create([{'kind': 'youtube', 'value': 'https://youtu.be/abc'}], {'content_type': 'book'})
        other = 'cd' * 16 + '.txt'
        (folder / other).write_text('你好', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '书听只允许 1'):
            self.studio.create([{'kind': 'upload', 'value': other}], {'content_type': 'book', 'expected_speakers': 2})
        audio = 'ef' * 16 + '.mp3'
        (folder / audio).write_bytes(b'not-audio')
        with self.assertRaisesRegex(ValueError, 'txt'):
            self.studio.create([{'kind': 'upload', 'value': audio}], {'content_type': 'book'})
        prose = '12' * 16 + '.txt'
        (folder / prose).write_text('不是播客', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '文本请用内容类型'):
            self.studio.create([{'kind': 'upload', 'value': prose}], {})
        self.assertEqual({p.name for p in self.studio.root.iterdir() if p.is_dir() and p.name != '.uploads'}, jobs_before)
        self.studio.action(ident, 'cancel')
        with self.assertRaisesRegex(ValueError, '书听只允许 1'):
            self.studio.set_expected_speakers(ident, 2)
        self.assertEqual(read(self.studio.job(ident) / 'config.json')['expected_speakers'], 1)
        with self.assertRaisesRegex(ValueError, '系统音色'):
            self.studio.voice(ident, 'clone')
        self.assertEqual(read(self.studio.job(ident) / 'config.json')['tts'], 'edge')
        write(self.studio.job(ident) / 'document.json', {
            'segments': [
                {'id': 'a', 'speaker_id': 'narrator', 'chinese': '你好'},
                {'id': 'b', 'speaker_id': 'other', 'chinese': '你好'},
            ],
            'speakers': {'narrator': {'voice_slot': 'male_1'}, 'other': {'voice_slot': 'male_2'}},
        })
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-voices', {})[0], 400)
        self.assertFalse(read(self.studio.job(ident) / 'document.json').get('voices_confirmed', False))

    def test_upload_accepts_user_text_and_rejects_pdf(self):
        import http.client

        def upload(filename, body):
            connection = http.client.HTTPConnection('127.0.0.1', self.port)
            connection.request('POST', '/api/uploads', body, {
                'X-CSRF-Token': self.studio.token,
                'X-Filename': filename,
            })
            response = connection.getresponse()
            payload = response.read()
            status = response.status
            connection.close()
            return status, payload

        status, payload = upload('notes.txt', '第一章\n你好'.encode())
        self.assertEqual(status, 200)
        self.assertIn(b'.txt', payload)
        status, payload = upload('book.pdf', b'%PDF-1.4')
        self.assertEqual(status, 400)
        self.assertIn('txt', payload.decode())

    def test_info_surfaces_subtitle_fallback_reason(self):
        with tempfile.TemporaryDirectory() as root:
            studio = Studio(root, engine=['/bin/true'])
            job = Path(root) / 'jobfb'
            job.mkdir()
            (job / 'config.json').write_text(json.dumps({
                'source': {'kind': 'youtube', 'value': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'},
                'provider': 'deepseek', 'tts': 'edge', 'style': 'conversation',
            }))
            (job / 'state.json').write_text(json.dumps({'title': 't', 'status': 'running', 'updated_at': 1}))
            (job / 'progress.json').write_text(json.dumps({'stage': 'asr', 'completed': 1, 'total': 4, 'updated_at': 1}))
            (job / 'debug-subtitle-fallback.json').write_text(json.dumps({'reason': '英文字幕文件为空或无法解析', 'updated_at': 1}))
            info = studio.info(job)
            self.assertEqual(info['progress']['subtitle_fallback_reason'], '英文字幕文件为空或无法解析')


if __name__=='__main__': unittest.main()
