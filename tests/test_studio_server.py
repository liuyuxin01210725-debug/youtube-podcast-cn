import hashlib, http.client, json, sys, tempfile, threading, time, unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from studio_server import Studio, Handler, ThreadingHTTPServer, write, read
from studio_document import OFFICIAL_EVIDENCE_FIELDS, segment_evidence_hash

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
    def review_job(self):
        ident = self.create()
        self.studio.action(ident, 'cancel')
        path = self.studio.job(ident)
        write(path / 'document.json', {
            'segments': [
                {'id': f'seg-{99 + i:06d}', 'speaker_id': 'SPEAKER_02' if i != 1 else 'SPEAKER_00',
                 'start_ms': i * 1000, 'end_ms': (i + 1) * 1000, 'text': f'Turn {i}',
                 'chinese': f'第{i}句', 'ambiguous': True}
                for i in range(3)
            ],
            'speakers': {'SPEAKER_02': {'voice_slot': 'male_1'}, 'SPEAKER_00': {'voice_slot': 'male_2'}},
            'references': {}, 'voices_confirmed': True,
        })
        return ident, path
    def review_item(self, document_sha256, segment):
        item = {
            'id': segment['id'], 'speaker_id': segment['speaker_id'],
            'official_source_sha256': hashlib.sha256(b'official transcript').hexdigest(),
            'official_speaker': 'Michael Aquino', 'official_locator': 'turn 22',
            'official_excerpt': 'An official excerpt. 官方原文。',
        }
        item['evidence_hash'] = segment_evidence_hash(document_sha256, segment, item)
        return item

    def test_confirm_segments_official_golden_hash_and_replayable_audit(self):
        ident, path = self.review_job()
        before = read(path / 'document.json')
        digest = hashlib.sha256((path / 'document.json').read_bytes()).hexdigest()
        valid = self.review_item(digest, before['segments'][0])
        self.assertNotIn('evidence_type', valid)
        self.assertEqual(valid['evidence_hash'], '332998236c8a66e711a477b83566d387472a35ca01e7e67763719c23feff5c66')
        invalid = self.review_item(digest, before['segments'][1])
        invalid['evidence_hash'] = '0' * 64
        status, raw = self.request(f'/api/jobs/{ident}/confirm-segments', {
            'document_sha256': digest, 'items': [valid, invalid],
        })
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(body['applied'], [valid['id']])
        self.assertEqual(body['rejected'], [{'id': invalid['id'], 'reason': 'evidence_hash_mismatch'}])
        after = read(path / 'document.json')
        expected = deepcopy(before)
        expected['segments'][0]['ambiguous'] = False
        self.assertEqual(after['segments'], expected['segments'])
        self.assertEqual(after['speakers'], before['speakers'])
        self.assertTrue(after['voices_confirmed'])
        audit, = after['segment_confirmations']
        self.assertEqual(audit['document_sha256'], digest)
        self.assertEqual(audit['evidence_type'], 'official_transcript')
        self.assertEqual(audit['evidence_hash'], valid['evidence_hash'])
        self.assertEqual(audit['hash_version'], 1)
        for field in OFFICIAL_EVIDENCE_FIELDS:
            self.assertEqual(audit[field], valid[field])
        for field in ('id', 'speaker_id', 'start_ms', 'end_ms', 'text'):
            self.assertEqual(audit[field], before['segments'][0][field])
        self.assertEqual(segment_evidence_hash(audit['document_sha256'], audit, audit), audit['evidence_hash'])
        self.assertEqual(body['document_sha256'], hashlib.sha256((path / 'document.json').read_bytes()).hexdigest())
        self.assertNotEqual(body['document_sha256'], digest)
        self.studio.action(ident, 'render')
        state = read(path / 'state.json')
        self.assertFalse(state['accept_unreviewed'])
        self.assertNotIn('--accept-unreviewed', self.studio.engine_args(path, state))

    def test_confirm_segments_stale_or_missing_document_hash_changes_nothing(self):
        ident, path = self.review_job()
        digest = hashlib.sha256((path / 'document.json').read_bytes()).hexdigest()
        document = read(path / 'document.json')
        item = self.review_item(digest, document['segments'][0])
        # Same JSON data, different bytes: only the exact on-disk digest is valid.
        (path / 'document.json').write_text(json.dumps(document, ensure_ascii=False))
        snapshot = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
        for wrong in (digest, digest.upper(), '0' * 64, None):
            with self.subTest(document_sha256=wrong):
                status, raw = self.request(f'/api/jobs/{ident}/confirm-segments', {
                    'document_sha256': wrong, 'items': [item],
                })
                self.assertEqual(status, 400)
                self.assertIn('document_sha256', json.loads(raw)['error'])
                self.assertEqual({p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}, snapshot)

    def test_confirm_segments_rejects_wrong_hash_speaker_and_missing_segment(self):
        ident, path = self.review_job()
        document = read(path / 'document.json')
        digest = hashlib.sha256((path / 'document.json').read_bytes()).hexdigest()
        valid = self.review_item(digest, document['segments'][0])
        cases = [
            ({**valid, 'evidence_hash': '0' * 64}, 'evidence_hash_mismatch'),
            ({**valid, 'speaker_id': 'SPEAKER_00'}, 'speaker_id_mismatch'),
            ({**valid, 'id': 'seg-999999'}, 'segment_not_found'),
        ]
        before = (path / 'document.json').read_bytes()
        state = (path / 'state.json').read_bytes()
        for item, reason in cases:
            with self.subTest(reason=reason):
                status, raw = self.request(f'/api/jobs/{ident}/confirm-segments', {
                    'document_sha256': digest, 'items': [item],
                })
                self.assertEqual(status, 200)
                body = json.loads(raw)
                self.assertEqual(body['applied'], [])
                self.assertEqual(body['rejected'], [{'id': item['id'], 'reason': reason}])
                self.assertEqual((path / 'document.json').read_bytes(), before)
                self.assertEqual((path / 'state.json').read_bytes(), state)

    def test_confirm_segments_requires_each_nonempty_official_evidence_field(self):
        ident, path = self.review_job()
        document = read(path / 'document.json')
        digest = hashlib.sha256((path / 'document.json').read_bytes()).hexdigest()
        valid = self.review_item(digest, document['segments'][0])
        before = (path / 'document.json').read_bytes()
        for field in OFFICIAL_EVIDENCE_FIELDS:
            for value in (None, '', ' \n ', 22):
                with self.subTest(field=field, value=value):
                    item = {**valid, field: value}
                    if value is None:
                        del item[field]
                    else:
                        # Even a matching hash cannot substitute for required evidence.
                        item['evidence_hash'] = segment_evidence_hash(digest, document['segments'][0], item)
                    status, raw = self.request(f'/api/jobs/{ident}/confirm-segments', {
                        'document_sha256': digest, 'items': [item],
                    })
                    self.assertEqual(status, 200)
                    body = json.loads(raw)
                    self.assertEqual(body['applied'], [])
                    self.assertEqual(body['rejected'], [{'id': item['id'], 'reason': f'invalid_{field}'}])
                    self.assertEqual((path / 'document.json').read_bytes(), before)

    def test_confirm_segments_empty_or_ids_only_cannot_confirm_all(self):
        ident, path = self.review_job()
        digest = hashlib.sha256((path / 'document.json').read_bytes()).hexdigest()
        snapshot = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
        for payload in ({'document_sha256': digest}, {'document_sha256': digest, 'items': []}):
            status, raw = self.request(f'/api/jobs/{ident}/confirm-segments', payload)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw), {'ok': True, 'applied': [], 'rejected': [], 'document_sha256': digest})
        for items in ([{'id': 'seg-000099'}], ['seg-000099'], [None], [{}]):
            status, raw = self.request(f'/api/jobs/{ident}/confirm-segments', {'document_sha256': digest, 'items': items})
            self.assertEqual(status, 200)
            body = json.loads(raw)
            self.assertEqual(body['applied'], [])
            self.assertEqual(len(body['rejected']), 1)
        for payload in ({}, {'document_sha256': digest, 'confirm_all': True},
                        {'document_sha256': digest, 'items': None}, {'document_sha256': digest, 'items': 'all'}):
            self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-segments', payload)[0], 400)
        self.assertEqual({p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}, snapshot)

    def test_confirm_segments_respects_csrf_and_job_editability(self):
        ident, path = self.review_job()
        digest = hashlib.sha256((path / 'document.json').read_bytes()).hexdigest()
        item = self.review_item(digest, read(path / 'document.json')['segments'][0])
        payload = {'document_sha256': digest, 'items': [item]}
        before = (path / 'document.json').read_bytes()
        self.assertEqual(self.request(f'/api/jobs/{ident}/confirm-segments', payload, {'X-CSRF-Token': 'bad'})[0], 400)
        for job_status in ('queued', 'running', 'cancelling'):
            state = read(path / 'state.json')
            state['status'] = job_status
            write(path / 'state.json', state)
            status, raw = self.request(f'/api/jobs/{ident}/confirm-segments', payload)
            self.assertEqual(status, 400)
            self.assertIn('等待任务停止', json.loads(raw)['error'])
            self.assertEqual((path / 'document.json').read_bytes(), before)
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
    def test_bulk_confirmation_rejected_and_row_edits_remain_available(self):
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
        after_move = read(p / 'document.json')
        self.assertEqual(after_move['segments'][1]['speaker_id'], 's0')
        self.assertFalse(after_move['voices_confirmed'])
        before = (p / 'document.json').read_bytes()
        code, body = self.request(f'/api/jobs/{ident}/confirm-speakers', {})
        self.assertEqual(code, 400)
        self.assertIn('confirm-segments', json.loads(body)['error'])
        self.assertEqual((p / 'document.json').read_bytes(), before)
        self.assertTrue(all(s['ambiguous'] for s in after_move['segments']))
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
        state_before = (p / 'state.json').read_bytes()
        for action in ('render', 'run'):
            code, raw = self.request(f'/api/jobs/{ident}/action', {
                'action': action, 'preview': True, 'accept_unreviewed': True,
            })
            self.assertEqual(code, 400)
            self.assertIn('confirm-segments', json.loads(raw)['error'])
            self.assertEqual((p / 'state.json').read_bytes(), state_before)
        self.assertNotIn('--accept-unreviewed', self.studio.engine_args(p, {'accept_unreviewed': True}))
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
