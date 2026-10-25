import http.client, json, sys, tempfile, threading, time, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from studio_server import Studio, Handler, ThreadingHTTPServer, write, read

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.studio=Studio(self.tmp.name)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.http.studio=self.studio
        threading.Thread(target=self.http.serve_forever,daemon=True).start(); self.port=self.http.server_port
    def tearDown(self):
        self.studio.stop=True; self.http.shutdown(); self.http.server_close(); self.tmp.cleanup()
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

    def test_source_restrictions_and_atomic_validation(self):
        for value in ('file:///etc/passwd','https://evil.example/a','https://youtube.com.evil/a'):
            with self.assertRaises(ValueError): self.studio.create([{'kind':'youtube','value':value}],{})
        with self.assertRaises(ValueError): self.studio.create([{'kind':'local','value':'/etc/passwd'}],{})
        with self.assertRaises(ValueError): self.studio.create([{'kind':'youtube','value':'https://youtu.be/a'}],{'tts':'bad'})
        self.assertEqual(self.studio.list(),[])
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
