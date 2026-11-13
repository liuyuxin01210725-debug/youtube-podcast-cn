import importlib.util
import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from book_import import import_source,BookImportError,digest
from book_pipeline import create_job,package_job,Ledger,BudgetBlocked,write,read,reading_document,apply_review,lock,amend_translation,reviewed,translate_job,synthesize_job,main,PROMPT_VERSION
from book_pipeline import sha,chapter_manifest,abandon_unusable_output,substitute_request_key
from tts_backends import SynthesisUsage,SynthesisRequest,build_backend
spec=importlib.util.spec_from_file_location('book_fixture',ROOT/'fixtures/book-integration/generate_sample.py');fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class BookIntegrationTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.source=fixture.make(self.root/'sample.epub');self.job=self.root/'job'
    def tearDown(self):self.tmp.cleanup()
    def ingest(self):return import_source(self.source,rights=fixture.RIGHTS)
    def test_spine_order_note_and_table_not_lost(self):
        b=self.ingest();self.assertEqual(len(b['chapters']),2);self.assertIn('Garden',b['chapters'][0]['title']);self.assertEqual(b['inventory']['unresolved'],[])
        blocks=[x for c in b['chapters'] for x in c['blocks']];self.assertEqual(len(blocks),7);self.assertEqual(len({x['id'] for x in blocks}),7)
        note=b['chapters'][0]['blocks'][-1];self.assertEqual(note['kind'],'footnote');self.assertEqual(note['source_anchor']['href'],'OEBPS/notes.xhtml');self.assertEqual(note['source_anchor']['fragment'],'n1')
        table=next(x for x in blocks if x['kind']=='table');self.assertIn('North | 3',table['text']);self.assertIn('South | 0',table['text'])
        self.assertFalse(any('Notebook first' in x['text'] for x in blocks));self.assertTrue(all(x['chinese']=='' for x in b['narrator_document']['segments']))
    def test_nested_layout_is_partial_and_words_do_not_glue(self):
        fixture.make(self.source,{'OEBPS/ch1.xhtml':fixture.CH1.replace('</body>','<blockquote><p>Alpha</p><p>Beta</p></blockquote></body>')})
        b=self.ingest();self.assertTrue(any(x['kind']=='nested_block_layout' for x in b['inventory']['unresolved']))
        self.assertTrue(any('Alpha Beta' in x['text'] for c in b['chapters'] for x in c['blocks']))
    def test_clean_inventory_alone_does_not_pass_source_review(self):
        create_job(self.source,self.job,rights=fixture.RIGHTS)
        m=read(package_job(self.job)/'manifest.json');self.assertEqual(m['gates']['source'],'inventory_ready_review_pending')
    def test_same_bytes_parser_change_is_new_extraction(self):
        first=self.ingest()
        with patch('book_import.PARSER_VERSION','new-parser'):second=self.ingest()
        self.assertEqual(first['source_byte_hash'],second['source_byte_hash']);self.assertNotEqual(first['extraction_revision'],second['extraction_revision'])
    def test_rights_required(self):
        with self.assertRaises(BookImportError):import_source(self.source)
    def test_zip_traversal_rejected(self):
        fixture.make(self.source,{'../escape':'bad'})
        with self.assertRaises(BookImportError):self.ingest()
    def test_external_entity_rejected(self):
        fixture.make(self.source,{'OEBPS/ch1.xhtml':'<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]>'+fixture.CH1})
        with self.assertRaises(BookImportError):self.ingest()
    def test_encryption_not_removed(self):
        fixture.make(self.source,{'META-INF/encryption.xml':'<encryption/>'})
        with self.assertRaises(BookImportError):self.ingest()
    def test_active_content_rejected(self):
        fixture.make(self.source,{'OEBPS/ch1.xhtml':fixture.CH1.replace('</body>','<script>alert(1)</script></body>')})
        with self.assertRaises(BookImportError):self.ingest()
    def test_math_and_image_partial_not_pass(self):
        fixture.make(self.source,{'OEBPS/ch1.xhtml':fixture.CH1.replace('</body>','<p><img src="remote.png" alt="unverified diagram"/><math>x</math></p></body>')})
        b=self.ingest();self.assertGreaterEqual(len(b['inventory']['unresolved']),2)
        create_job(self.source,self.job,rights=fixture.RIGHTS);m=read(package_job(self.job)/'manifest.json');self.assertEqual(m['gates']['source'],'partial');self.assertFalse(m['whole_book_verified'])
    def test_source_import_does_not_overwrite_existing(self):
        b=create_job(self.source,self.job,rights=fixture.RIGHTS);before=(self.job/'source.json').read_bytes()
        with self.assertRaises(ValueError):create_job(self.source,self.job,rights=fixture.RIGHTS)
        self.assertEqual(before,(self.job/'source.json').read_bytes())
    def test_draft_package_not_audio_or_translation_pass(self):
        create_job(self.source,self.job,rights=fixture.RIGHTS);p=package_job(self.job);m=read(p/'manifest.json')
        self.assertEqual(m['ready_chapter_ids'],[]);self.assertEqual(m['gates']['translation'],'unreviewed');self.assertFalse(m['whole_book_verified']);self.assertEqual(p,package_job(self.job))
    def test_fixture_review_cannot_publish_audio(self):
        s=create_job(self.source,self.job,rights=fixture.RIGHTS);tr={'extraction_revision':s['extraction_revision'],'translation_revision':'fixture-translation','blocks':{b['id']:{'source_hash':b['source_text_hash'],'text':'结构测试译文','target_hash':digest('结构测试译文')} for c in s['chapters'] for b in c['blocks']}}
        write(self.job/'translations.json',tr);review={'fixture_only':True,'extraction_revision':s['extraction_revision'],'translation_revision':tr['translation_revision'],'blocks':{bid:{'source_hash':t['source_hash'],'target_hash':t['target_hash'],'verdict':'pass','evidence':'unit test only','reviewer':'fixture'} for bid,t in tr['blocks'].items()}};write(self.job/'review.json',review)
        m=read(package_job(self.job)/'manifest.json');self.assertEqual(m['gates']['translation'],'unreviewed')
        rd=reading_document(s,tr);self.assertIn('章后注释。',rd['chapters'][0]['blocks'][-1]['text'])
    def test_unbound_review_rejected(self):
        s=create_job(self.source,self.job,rights=fixture.RIGHTS);write(self.job/'translations.json',{'translation_revision':'a','blocks':{}});write(self.root/'review.json',{'translation_revision':'b'})
        with self.assertRaises(ValueError):apply_review(self.job,self.root/'review.json')
    def test_lock_prevents_parallel_submit(self):
        with lock(self.job):
            with self.assertRaises(BlockingIOError):
                with lock(self.job):pass
    def allowance(self):
        n=datetime.now(timezone.utc);return {'translation':{'provider':'test','model':'test','verified_at':n.isoformat(),'expires_at':(n+timedelta(hours=1)).isoformat(),'max_requests':2,'max_characters':100,'evidence_ref':'TEST ONLY - no API calls','no_new_charges_verified':True}}
    def test_unknown_budget_blocks_before_submit(self):
        with self.assertRaises(BudgetBlocked):Ledger(self.job,None).begin('translation','one',1,'test','test')
        self.assertFalse((self.job/'request-ledger.json').exists())
    def test_uncertain_survives_cancel_restart_and_blocks_other_key(self):
        self.job.mkdir();a=self.allowance();l=Ledger(self.job,a);l.begin('translation','one',10,'test','test');l.uncertain('one');write(self.job/'cancel',{'cancel':True})
        self.assertEqual(read(self.job/'request-ledger.json')['requests']['one']['reserved_characters'],10)
        (self.job/'cancel').unlink()
        with self.assertRaises(BudgetBlocked):Ledger(self.job,a).begin('translation','two',1,'test','test')
    def test_character_ceiling_counts_completed_requests(self):
        self.job.mkdir();a=self.allowance();l=Ledger(self.job,a);l.begin('translation','one',80,'test','test');l.finish('one',{'input_tokens':3})
        with self.assertRaises(BudgetBlocked):l.begin('translation','two',21,'test','test')
    def test_same_request_not_reissued_after_success(self):
        self.job.mkdir();l=Ledger(self.job,self.allowance());l.begin('translation','one',1,'test','test');l.finish('one',{})
        with self.assertRaises(BudgetBlocked):l.begin('translation','one',1,'test','test')
    def test_cash_reservations_include_succeeded_and_uncertain(self):
        self.job.mkdir();a=self.allowance();a['translation']['max_total_cost_microunits']=20
        l=Ledger(self.job,a);l.begin('translation','one',1,'test','test',15);l.finish('one',{})
        with self.assertRaises(BudgetBlocked):l.begin('translation','two',1,'test','test',6)
        l.begin('translation','two',1,'test','test',5);l.uncertain('two')
        self.assertEqual(sum(x['reserved_cost_microunits'] for x in read(l.path)['requests'].values()),20)
    def test_reconciliation_state_blocks_new_requests(self):
        self.job.mkdir();a=self.allowance();l=Ledger(self.job,a);l.begin('translation','one',1,'test','test')
        l.data['requests']['one']['state']='reconciliation_required';write(l.path,l.data)
        with self.assertRaises(BudgetBlocked):Ledger(self.job,a).begin('translation','two',1,'test','test')
    def recovery_allowance(self):
        a=self.tts_allowance();a['tts'].update(model='edge-tts',max_total_cost_microunits=0);return a
    @contextmanager
    def recovery_backend(self,expected_texts=(),fail_at=None):
        with self.scope_backend(expected_texts,fail_at) as backend:
            backend.model='edge-tts';yield backend
    def uncertain_edge_fragment(self):
        self.job.mkdir(exist_ok=True);key=digest(['offline bad fragment']);ledger=Ledger(self.job,self.recovery_allowance())
        ledger.begin('tts',key,8,'edge','edge-tts');ledger.uncertain(key)
        ledger.data['requests'][key]['original_extra']={'exception':'SocketTimeoutError','unknown_usage':None};write(ledger.path,ledger.data)
        partial=self.job/'audio-fragments'/(key+'.partial.mp3');partial.parent.mkdir(exist_ok=True);partial.write_bytes(b'')
        return key
    def reconciliation(self,key):
        parent=read(self.job/'request-ledger.json')['requests'][key]
        outputs=[]
        for suffix in ['.mp3','.partial.mp3']:
            path='audio-fragments/'+key+suffix;target=self.job/path
            outputs.append({'path':path,'size':target.stat().st_size if target.exists() else None,'sha256':sha(target) if target.exists() else None})
        e={'parent_key':key,'parent_record_sha256':digest(parent),'attempt':1,'editor':'offline recovery operator',
           'reason':'Abandon unusable local output after timeout','evidence':'Self-authored offline fixture, both paths inspected; no usable local audio',
           'exception':'SocketTimeoutError','submitted_at':parent['submitted_at'],'finished_at':parent['finished_at'],
           'remote_result':'unknown','provider_lookup':'no_per_request_lookup','proof_of_non_completion':False,'refund_claimed':False,'mark_succeeded':False,'no_usable_local_audio':True,
           'zero_cost_evidence':{'provider':'edge','model':'edge-tts','max_total_cost_microunits':0,'api_key':None,'no_new_charges_verified':True,'paid_tts_blocked':True,'evidence_ref':parent['allowance_evidence']},'local_outputs':outputs}
        path=self.root/'reconciliation.json';write(path,e);return path
    def assert_abandon_rejected(self,path):
        before=(self.job/'request-ledger.json').read_bytes()
        with self.assertRaises((ValueError,BudgetBlocked)):abandon_unusable_output(self.job,path)
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
    def test_abandon_preserves_full_uncertain_event_and_all_reservations(self):
        key=self.uncertain_edge_fragment();original=deepcopy(read(self.job/'request-ledger.json')['requests'][key]);path=self.reconciliation(key)
        with patch('book_pipeline.build_backend',side_effect=AssertionError('No TTS')), patch.object(Ledger,'begin',side_effect=AssertionError('No begin')), patch('book_pipeline.urllib.request.urlopen',side_effect=AssertionError('No network')):
            result=abandon_unusable_output(self.job,path)
        records=read(self.job/'request-ledger.json')['requests'];self.assertEqual(set(records),{key})
        self.assertEqual(result['state'],'abandoned_unusable_output');self.assertNotEqual(result['state'],'succeeded')
        for field,value in original.items():
            if field!='state':self.assertEqual(result[field],value)
        self.assertEqual(result['events'][0]['prior_record'],original);self.assertEqual(result['events'][0]['reconciliation'],read(path))
        self.assertEqual(result['remote_result'],'unknown');self.assertEqual(result['provider_lookup'],'no_per_request_lookup')
        self.assertEqual(result['substitute_key'],digest(['book-edge-unusable-output-substitute-v1',key,1]));self.assertNotEqual(result['substitute_key'],key)
        self.assertEqual(result['substitute_attempt'],1);self.assertIsNotNone(datetime.fromisoformat(result['authorized_at']).tzinfo)
        a=self.recovery_allowance();a['tts']['max_characters']=original['reserved_characters']
        with self.assertRaisesRegex(BudgetBlocked,'character ceiling'):Ledger(self.job,a).begin('tts',result['substitute_key'],1,'edge','edge-tts')
        a=self.recovery_allowance();a['tts']['max_requests']=1
        with self.assertRaisesRegex(BudgetBlocked,'character ceiling'):Ledger(self.job,a).begin('tts',result['substitute_key'],1,'edge','edge-tts')
        with self.assertRaisesRegex(BudgetBlocked,'never blindly resubmit'):Ledger(self.job,self.recovery_allowance()).begin('tts',key,1,'edge','edge-tts')
        Ledger(self.job,self.recovery_allowance()).begin('tts','unrelated-new-key',1,'edge','edge-tts')
        self.assertEqual(sum(r['reserved_characters'] for r in read(self.job/'request-ledger.json')['requests'].values()),9)
    def test_abandon_rejects_paid_unknown_nonzero_and_non_uncertain_parents(self):
        key=self.uncertain_edge_fragment();baseline=read(self.job/'request-ledger.json')
        for change in [{'provider':'qwen3-api'},{'provider':'unknown'},{'model':'unknown'},{'reserved_cost_microunits':1},{'reserved_cost_microunits':None},{'stage':'translation'},{'state':'succeeded'},{'state':'submitted'},{'state':'reconciliation_required'}]:
            with self.subTest(change=change):
                data=deepcopy(baseline);data['requests'][key].update(change);write(self.job/'request-ledger.json',data)
                self.assert_abandon_rejected(self.reconciliation(key))
    def test_abandon_requires_bound_zero_cost_facts(self):
        key=self.uncertain_edge_fragment();path=self.reconciliation(key);valid=read(path)
        for field in valid['zero_cost_evidence']:
            with self.subTest(missing=field):
                e=deepcopy(valid);del e['zero_cost_evidence'][field];write(path,e);self.assert_abandon_rejected(path)
        for field,value in [('max_total_cost_microunits',1),('max_total_cost_microunits',False),('api_key','paid-secret'),('paid_tts_blocked',False),('provider','qwen3-api'),('no_new_charges_verified',False),('evidence_ref','different allowance')]:
            with self.subTest(field=field,value=value):
                e=deepcopy(valid);e['zero_cost_evidence'][field]=value;write(path,e);self.assert_abandon_rejected(path)
    def test_abandon_missing_or_contradictory_evidence_is_atomic(self):
        key=self.uncertain_edge_fragment();path=self.reconciliation(key);valid=read(path)
        for field in valid:
            with self.subTest(missing=field):
                e=deepcopy(valid);del e[field];write(path,e);self.assert_abandon_rejected(path)
        for field,value in [('parent_key',digest('different')),('parent_record_sha256',digest('different')),('editor',' '),('reason',''),('evidence',''),('exception',''),('submitted_at','yesterday'),('finished_at','2000-01-01T00:00:00+00:00'),('remote_result','not_completed'),('provider_lookup','available'),('proof_of_non_completion',True),('refund_claimed',True),('mark_succeeded',True),('no_usable_local_audio',False)]:
            with self.subTest(field=field,value=value):
                e=deepcopy(valid);e[field]=value;write(path,e);self.assert_abandon_rejected(path)
        for field,value in [('path','audio-fragments/wrong.partial.mp3'),('size',12),('sha256',digest('wrong'))]:
            e=deepcopy(valid);e['local_outputs'][1][field]=value;write(path,e);self.assert_abandon_rejected(path)
        # An existing different uncertain key cannot be selected with this block's evidence.
        data=read(self.job/'request-ledger.json');other=digest('different existing key');data['requests'][other]=deepcopy(data['requests'][key]);write(self.job/'request-ledger.json',data)
        e=deepcopy(valid);e['parent_key']=other;write(path,e);self.assert_abandon_rejected(path)
    def test_abandon_rejects_nonempty_or_checksummed_local_audio(self):
        key=self.uncertain_edge_fragment();path=self.reconciliation(key)
        partial=self.job/'audio-fragments'/(key+'.partial.mp3');partial.write_bytes(b'possibly complete audio')
        self.assert_abandon_rejected(path);self.assert_abandon_rejected(self.reconciliation(key))
        partial.write_bytes(b'');complete=partial.with_name(key+'.mp3');complete.write_bytes(b'COMPLETE')
        write(self.job/'fragment-checksums.json',{key:sha(complete)});self.assert_abandon_rejected(self.reconciliation(key))
    def test_abandon_allows_both_missing_paths_with_explicit_evidence(self):
        key=self.uncertain_edge_fragment();(self.job/'audio-fragments'/(key+'.partial.mp3')).unlink()
        result=abandon_unusable_output(self.job,self.reconciliation(key));self.assertEqual(result['state'],'abandoned_unusable_output')
    def test_abandon_appends_events_and_rejects_a_prior_authorization_event(self):
        key=self.uncertain_edge_fragment();data=read(self.job/'request-ledger.json');prior={'type':'operator_inspection','evidence':'offline inspection'}
        data['requests'][key]['events']=[prior];write(self.job/'request-ledger.json',data);original=deepcopy(data['requests'][key])
        result=abandon_unusable_output(self.job,self.reconciliation(key))
        self.assertEqual(result['events'][0],prior);self.assertEqual(result['events'][1]['prior_record'],original)
        data['requests'][key]['events']=[{'type':'abandon_unusable_output','substitute_key':substitute_request_key(key,1)}];write(self.job/'request-ledger.json',data)
        self.assert_abandon_rejected(self.reconciliation(key))
    def test_abandon_rejects_repeats_attempt_two_and_existing_substitute(self):
        key=self.uncertain_edge_fragment();baseline=read(self.job/'request-ledger.json');path=self.reconciliation(key);valid=read(path)
        for attempt in [0,2,-1,'1',True,None]:
            e=deepcopy(valid);e['attempt']=attempt;write(path,e);self.assert_abandon_rejected(path)
        for change in [{'substitute_key':substitute_request_key(key,1)},{'substitute_attempt':1},{'authorized_at':'already authorized'}]:
            data=deepcopy(baseline);data['requests'][key].update(change);write(self.job/'request-ledger.json',data);self.assert_abandon_rejected(self.reconciliation(key))
        data=deepcopy(baseline);data['requests'][substitute_request_key(key,1)]=dict(data['requests'][key],state='succeeded');write(self.job/'request-ledger.json',data);self.assert_abandon_rejected(self.reconciliation(key))
        write(self.job/'request-ledger.json',baseline);path=self.reconciliation(key);abandon_unusable_output(self.job,path);self.assert_abandon_rejected(path)
        self.assert_abandon_rejected(self.reconciliation(key));self.assertEqual(len(read(self.job/'request-ledger.json')['requests']),1)
    def test_abandon_cli_records_recovery_without_provider_or_begin(self):
        key=self.uncertain_edge_fragment();path=self.reconciliation(key)
        with patch.object(sys,'argv',['book_pipeline.py','abandon-unusable-output','--job',str(self.job),'--reconciliation',str(path)]), patch('sys.stdout',new_callable=io.StringIO) as output, patch('book_pipeline.build_backend',side_effect=AssertionError('No TTS')), patch.object(Ledger,'begin',side_effect=AssertionError('No begin')):
            main()
        result=json.loads(output.getvalue());self.assertEqual(result['state'],'abandoned_unusable_output');self.assertEqual(len(read(self.job/'request-ledger.json')['requests']),1)
    def translated_job(self,complete=False):
        s=create_job(self.source,self.job,rights=fixture.RIGHTS)
        config={'provider':'test','model':'test','base_url':'https://invalid.test','prompt_version':PROMPT_VERSION,'max_output_tokens':None,'thinking':'disabled','glossary':{},'context_strategy':'previous block source+translation, up to 800 characters each'}
        blocks=[b for c in s['chapters'] for b in c['blocks']]
        if not complete:blocks=blocks[:2]
        tr={'extraction_revision':s['extraction_revision'],'config':config,'config_identity':digest([s['extraction_revision'],config]),'blocks':{b['id']:{'source_hash':b['source_text_hash'],'text':'结构测试译文'+str(n),'target_hash':digest('结构测试译文'+str(n)),'request_key':digest(['offline-fixture',b['id']])} for n,b in enumerate(blocks)}}
        tr['translation_revision']=digest([tr['config_identity'],tr['blocks']]);write(self.job/'translations.json',tr)
        key=tr['blocks'][blocks[0]['id']]['request_key'];ledger=Ledger(self.job,self.allowance());ledger.begin('translation',key,1,'test','test');ledger.finish(key,{'fixture_only':True})
        return s,tr
    def amendment(self,s,tr,bid,text='第一章：花园。'):
        path=self.root/'amendment.json'
        write(path,{'extraction_revision':s['extraction_revision'],'translation_revision':tr['translation_revision'],'editor':'offline test editor','blocks':{bid:{'source_hash':tr['blocks'][bid]['source_hash'],'from_target_hash':tr['blocks'][bid]['target_hash'],'text':text,'reason':'Correct the sample chapter heading','evidence':'Compared the self-authored sample heading with the replacement; test only'}}})
        return path
    def test_amend_translation_preserves_provider_facts(self):
        s,tr=self.translated_job();a,b=tr['blocks'];path=self.amendment(s,tr,a);item=read(path)['blocks'][a]
        ledger_before=(self.job/'request-ledger.json').read_bytes();source_before=(self.job/'source.json').read_bytes()
        with patch('book_pipeline.OpenAICompatibleTranslationClient',side_effect=AssertionError('No translation client')), patch('book_pipeline.build_backend',side_effect=AssertionError('No TTS backend')), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No request')), patch('book_pipeline.urllib.request.urlopen',side_effect=AssertionError('No network')):
            result=amend_translation(self.job,path)
        target=result['blocks'][a];old=tr['blocks'][a]
        self.assertEqual(result,read(self.job/'translations.json'));self.assertEqual(target['text'],item['text']);self.assertEqual(target['target_hash'],digest(item['text']))
        self.assertEqual(target['request_key'],old['request_key']);self.assertEqual(target['source_hash'],old['source_hash'])
        self.assertIs(target['editorial_revision'],True);self.assertIs(target['not_a_provider_return'],True)
        self.assertIsNotNone(datetime.fromisoformat(target['amended_at']).tzinfo)
        self.assertEqual(target['editor'],read(path)['editor'])
        for field in ['reason','evidence']:self.assertEqual(target[field],item[field])
        self.assertEqual(target['from_target_hash'],old['target_hash']);self.assertEqual(target['provider_target_hash'],old['target_hash'])
        self.assertNotEqual(result['translation_revision'],tr['translation_revision']);self.assertEqual(result['translation_revision'],digest([tr['config_identity'],result['blocks']]))
        self.assertEqual(result['blocks'][b],tr['blocks'][b])
        for field in ['config','config_identity','extraction_revision']:self.assertEqual(result[field],tr[field])
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger_before);self.assertEqual((self.job/'source.json').read_bytes(),source_before)
        self.assertFalse((self.job/'provider-responses').exists());self.assertFalse((self.job/'review.json').exists());self.assertFalse((self.job/'audio.json').exists())
    def test_later_amendment_retains_original_provider_hash(self):
        s,tr=self.translated_job();bid=next(iter(tr['blocks']))
        first=amend_translation(self.job,self.amendment(s,tr,bid));second=amend_translation(self.job,self.amendment(s,first,bid,'第一章：花园'))
        self.assertEqual(second['blocks'][bid]['provider_target_hash'],tr['blocks'][bid]['target_hash'])
        self.assertEqual(second['blocks'][bid]['from_target_hash'],first['blocks'][bid]['target_hash'])
        self.assertEqual(second['blocks'][bid]['request_key'],tr['blocks'][bid]['request_key']);self.assertIs(second['blocks'][bid]['not_a_provider_return'],True)
        self.assertEqual(second['translation_revision'],digest([tr['config_identity'],second['blocks']]))
    def test_amend_invalidates_all_reviews_and_requires_explicit_rebind(self):
        s,tr=self.translated_job();a,b=tr['blocks']
        review={'extraction_revision':s['extraction_revision'],'translation_revision':tr['translation_revision'],'blocks':{bid:{'source_hash':t['source_hash'],'target_hash':t['target_hash'],'verdict':'pass','reviewer':'offline editorial test reviewer','evidence':'Compared self-authored source and fixture translation for this binding test; not a release review'} for bid,t in tr['blocks'].items()}}
        old_file=self.root/'old-review.json';write(old_file,review);apply_review(self.job,old_file)
        self.assertTrue(all(reviewed(s,tr,review,bid) for bid in [a,b]));before=(self.job/'review.json').read_bytes()
        amended=amend_translation(self.job,self.amendment(s,tr,a))
        self.assertEqual((self.job/'review.json').read_bytes(),before)
        self.assertTrue(all(not reviewed(s,amended,review,bid) for bid in [a,b]))
        with self.assertRaisesRegex(ValueError,'Review revisions differ'):apply_review(self.job,old_file)
        self.assertEqual((self.job/'review.json').read_bytes(),before)
        rebound=deepcopy(review);rebound['translation_revision']=amended['translation_revision'];rebound['blocks'][a]['target_hash']=amended['blocks'][a]['target_hash']
        for item in rebound['blocks'].values():item.update(reviewer='offline re-reviewer',evidence='Rechecked the amended heading and unchanged sample block against source; test only')
        new_file=self.root/'new-review.json';write(new_file,rebound);apply_review(self.job,new_file)
        self.assertEqual(rebound['blocks'][b]['target_hash'],review['blocks'][b]['target_hash'])
        self.assertTrue(all(reviewed(s,amended,read(self.job/'review.json'),bid) for bid in [a,b]))
    def test_amend_rejections_leave_translations_unchanged(self):
        s,tr=self.translated_job();bid=next(iter(tr['blocks']));path=self.amendment(s,tr,bid);valid=read(path);cases=[]
        for field,value in [('extraction_revision','stale'),('translation_revision','stale'),('editor',' \t'),('blocks',{})]:
            data=deepcopy(valid);data[field]=value;cases.append((field,data))
        for field,value in [('from_target_hash','wrong'),('source_hash','wrong'),('text',''),('text',' \n\t'),('text',tr['blocks'][bid]['text']),('reason',' '),('evidence',' ')]:
            data=deepcopy(valid);data['blocks'][bid][field]=value;cases.append((field+'='+repr(value),data))
        for field in ['extraction_revision','translation_revision','editor','blocks']:
            data=deepcopy(valid);del data[field];cases.append(('missing '+field,data))
        for field in ['text','reason','evidence','source_hash','from_target_hash']:
            data=deepcopy(valid);del data['blocks'][bid][field];cases.append(('missing '+field,data))
        data=deepcopy(valid);data['blocks']['unknown-block']=deepcopy(data['blocks'][bid]);cases.append(('unknown block after valid block',data))
        untranslated=next(b['id'] for c in s['chapters'] for b in c['blocks'] if b['id'] not in tr['blocks'])
        data=deepcopy(valid);data['blocks']={untranslated:deepcopy(data['blocks'][bid])};cases.append(('untranslated source block',data))
        before=(self.job/'translations.json').read_bytes();ledger_before=(self.job/'request-ledger.json').read_bytes()
        for label,data in cases:
            with self.subTest(label=label):
                write(path,data)
                with self.assertRaises(ValueError):amend_translation(self.job,path)
                self.assertEqual((self.job/'translations.json').read_bytes(),before);self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger_before)
    def test_amend_rejects_inconsistent_current_hashes(self):
        s,tr=self.translated_job();bid=next(iter(tr['blocks']));path=self.amendment(s,tr,bid);valid=read(path)
        for field in ['text','target_hash','source_hash']:
            with self.subTest(field=field):
                current=deepcopy(tr);current['blocks'][bid][field]='tampered';write(self.job/'translations.json',current)
                data=deepcopy(valid);data['blocks'][bid]['from_target_hash']=current['blocks'][bid]['target_hash'];write(path,data)
                before=(self.job/'translations.json').read_bytes()
                with self.assertRaisesRegex(ValueError,'[Ss]ource hash differs|Current target hash differs'):amend_translation(self.job,path)
                self.assertEqual((self.job/'translations.json').read_bytes(),before)
    def test_translate_skips_amended_blocks_without_resubmission(self):
        s,tr=self.translated_job(complete=True);bid=next(iter(tr['blocks']));amended=amend_translation(self.job,self.amendment(s,tr,bid));before=(self.job/'request-ledger.json').read_bytes()
        with patch('book_pipeline.OpenAICompatibleTranslationClient') as client, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No resubmission')) as begin, patch('book_pipeline.translate_with_resume',side_effect=AssertionError('No translation')) as translate, patch('book_pipeline.urllib.request.urlopen',side_effect=AssertionError('No network')) as network:
            dummy=client.return_value;dummy.provider='test';dummy.model='test';dummy.base_url='https://invalid.test';dummy.translate.side_effect=AssertionError('No client call')
            result=translate_job(self.job,'test','test',self.allowance())
            begin.assert_not_called();translate.assert_not_called();dummy.translate.assert_not_called();network.assert_not_called()
        self.assertEqual(result,amended);self.assertIs(result['blocks'][bid]['editorial_revision'],True)
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),before);self.assertFalse((self.job/'provider-responses').exists());self.assertFalse((self.job/'translation-checkpoints').exists())
    def test_amend_does_not_silently_resynthesize_existing_audio(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));a,b=[c['id'] for c in s['chapters']]
        parts=self.scope_fragments(s,tr,[a,b])
        with self.scope_backend([text for key,text in parts]):first=synthesize_job(self.job,'edge',self.tts_allowance())
        files=self.audio_bytes(first);ledger=(self.job/'request-ledger.json').read_bytes()
        amended=amend_translation(self.job,self.amendment(s,tr,s['chapters'][0]['blocks'][0]['id']))
        self.assertNotEqual(first['reading_revision'],reading_document(s,amended)['reading_revision'])
        self.rebind_review(s,amended,[b])
        with self.scope_backend() as backend, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No TTS request')) as begin:
            with self.assertRaisesRegex(ValueError,'Unreviewed/failed chapter cannot be narrated'):synthesize_job(self.job,'edge',self.tts_allowance())
            second=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[b])
            package=read(package_job(self.job)/'manifest.json')
            backend.synthesize.assert_not_called();begin.assert_not_called()
        self.assertEqual(set(second['chapters']),{b});self.assertEqual(second['chapters'][b],first['chapters'][b])
        self.assertEqual(self.audio_bytes(first),files);self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
        self.assertEqual(second['invalidated_chapters'][0]['audio'],first['chapters'][a])
        self.assertEqual(second['reading_revision'],first['reading_revision']) # original audit value is not forged
        self.assertEqual(second['reading_revision_snapshot'],reading_document(s,amended)['reading_revision'])
        self.assertEqual(package['ready_chapter_ids'],[b]);self.assertFalse(package['whole_book_verified'])
        self.assertIsNone(package['chapters'][0]['audio'])
        self.rebind_review(s,amended)
        new_parts=self.scope_fragments(s,amended,[a])
        with self.scope_backend([text for key,text in new_parts]):
            third=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[a]);current_package=read(package_job(self.job)/'manifest.json')
        self.assertEqual(set(third['chapters']),{a,b});self.assertEqual(third['chapters'][b],first['chapters'][b])
        self.assertNotEqual(third['chapters'][a]['sha256'],first['chapters'][a]['sha256'])
        self.assertNotEqual(third['chapters'][a]['path'],first['chapters'][a]['path'])
        self.assertEqual(current_package['ready_chapter_ids'],[a,b])
        self.assertNotIn(first['chapters'][a]['sha256'],{c['audio']['sha256'] for c in current_package['chapters']})
        self.assertFalse(current_package['whole_book_verified'])
        self.assertEqual(self.audio_bytes(first),files) # recovery copies survive replacement
        records=read(self.job/'request-ledger.json')['requests']
        self.assertEqual({k for k,r in records.items() if r['stage']=='tts'},{k for k,t in parts+new_parts})
    def test_amend_cli_prints_updated_translations(self):
        s,tr=self.translated_job();bid=next(iter(tr['blocks']));path=self.amendment(s,tr,bid)
        with patch.object(sys,'argv',['book_pipeline.py','amend','--job',str(self.job),'--amendment',str(path)]), patch('sys.stdout',new_callable=io.StringIO) as output:
            main()
        result=json.loads(output.getvalue());self.assertEqual(result,read(self.job/'translations.json'));self.assertIs(result['blocks'][bid]['editorial_revision'],True)
    def scope_job(self,reviewed_indexes=(1,)):
        s,tr=self.translated_job(complete=True)
        bids={b['id'] for n,c in enumerate(s['chapters']) if n in reviewed_indexes for b in c['blocks']}
        review={'extraction_revision':s['extraction_revision'],'translation_revision':tr['translation_revision'],'blocks':{bid:{'source_hash':t['source_hash'],'target_hash':t['target_hash'],'verdict':'pass','reviewer':'offline scope test reviewer','evidence':'Checked self-authored fixture bindings; not a release review'} for bid,t in tr['blocks'].items() if bid in bids}}
        path=self.root/'scope-review.json';write(path,review);apply_review(self.job,path);return s,tr
    def tts_allowance(self):
        a=self.allowance()['translation'];a.update(provider='edge',model='offline',max_requests=100,max_characters=10000);return {'tts':a}
    def scope_fragments(self,s,tr,chapter_ids):
        rd=reading_document(s,tr);config={'backend':'offline-edge','voice_slot':'male_1','resolved_voice':'offline-voice','style':'documentary'};parts=[]
        for c in rd['chapters']:
            if c['id'] not in chapter_ids:continue
            admission=digest(chapter_manifest(s,tr,c,config,SimpleNamespace(max_chars=4,file_suffix='.mp3')))
            for b in c['blocks']:
                for n,offset in enumerate(range(0,len(b['text']),4)):
                    text=b['text'][offset:offset+4];parts.append((digest([admission,b['source_block_id'],n,text]),text))
        return parts
    @contextmanager
    def scope_backend(self,expected_texts=(),fail_at=None):
        # Stub media encoding/decoding only; files, checksums, ledger and package gates stay real.
        def concatenate(args,**kwargs):
            self.assertEqual(args[0],'ffmpeg');listing=Path(args[args.index('-i')+1])
            parts=[listing.parent/line[len("file '"):-1] for line in listing.read_text().splitlines()]
            Path(args[-1]).write_bytes(b''.join(p.read_bytes() for p in parts))
        with patch('book_pipeline.build_backend') as build, patch('book_pipeline.duration',return_value=1.0), patch('book_pipeline.subprocess.run',side_effect=concatenate), patch('book_pipeline.urllib.request.urlopen',side_effect=AssertionError('No network')):
            backend=build.return_value;backend.name='edge';backend.model='offline';backend.max_chars=4;backend.file_suffix='.mp3'
            backend.fingerprint.return_value='offline-edge';backend.resolve_voice.return_value='offline-voice'
            async def synthesize(request,target):
                n=backend.synthesize.call_count-1;self.assertLess(n,len(expected_texts),'Unexpected TTS request');self.assertEqual(request.text,expected_texts[n])
                if n==fail_at:raise RuntimeError('Offline TTS fragment failure')
                target.write_bytes(b'OFFLINE TEST: '+request.text.encode());return SynthesisUsage(request_id='offline-'+str(n))
            backend.synthesize.side_effect=synthesize
            yield backend
            self.assertEqual(backend.synthesize.call_count,len(expected_texts))
    def rebind_review(self,s,tr,chapter_ids=None):
        bids={b['id'] for c in s['chapters'] if chapter_ids is None or c['id'] in chapter_ids for b in c['blocks']}
        review={'extraction_revision':s['extraction_revision'],'translation_revision':tr['translation_revision'],
                'blocks':{bid:{'source_hash':t['source_hash'],'target_hash':t['target_hash'],'verdict':'pass',
                               'reviewer':'offline cache test reviewer','evidence':'Self-authored fixture bindings only; not a real content review'} for bid,t in tr['blocks'].items() if bid in bids}}
        path=self.root/'cache-review.json';write(path,review);apply_review(self.job,path)
    def audio_bytes(self,state):
        paths=[a['path'] for a in state['chapters'].values()]+[f['path'] for a in state['chapters'].values() for f in a['fragments']]
        return {p:(self.job/p).read_bytes() for p in paths}
    def interrupted_recovery_chapter(self,legacy=False):
        s,tr=self.scope_job(reviewed_indexes=(0,1));a,b=[c['id'] for c in s['chapters']]
        if legacy:
            state=self.legacy_audio(s,tr,[b]);rd=reading_document(s,tr);chapter=rd['chapters'][0];parts=[]
            for block in chapter['blocks']:
                for n,offset in enumerate(range(0,len(block['text']),4)):
                    value=block['text'][offset:offset+4];parts.append((digest([rd['reading_revision'],state['config'],block['source_block_id'],n,value]),value))
        else:
            with self.recovery_backend([t for k,t in self.scope_fragments(s,tr,[b])]):state=synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[b])
            parts=self.scope_fragments(s,tr,[a])
        self.assertGreater(len(parts),3);ledger=Ledger(self.job,self.recovery_allowance());checks=read(self.job/'fragment-checksums.json')
        for key,text in parts[:2]:
            ledger.begin('tts',key,len(text),'edge','edge-tts');ledger.finish(key,{'request_id':'offline prefix'})
            target=self.job/'audio-fragments'/(key+'.mp3');target.write_bytes(b'OFFLINE PREFIX: '+text.encode());checks[key]=sha(target)
        write(self.job/'fragment-checksums.json',checks);key,text=parts[2];ledger.begin('tts',key,len(text),'edge','edge-tts');ledger.uncertain(key)
        (self.job/'audio-fragments'/(key+'.partial.mp3')).write_bytes(b'')
        return s,tr,a,b,parts,state
    def test_abandoned_fragment_resumes_once_keeps_prefix_tail_and_unrelated_cache(self):
        s,tr,a,b,parts,state=self.interrupted_recovery_chapter();parent=parts[2][0]
        records_before=read(self.job/'request-ledger.json')['requests'];checks_before=read(self.job/'fragment-checksums.json');files_before=self.audio_bytes(state)
        prefix_before={k:(self.job/'audio-fragments'/(k+'.mp3')).read_bytes() for k,t in parts[:2]}
        with self.assertRaisesRegex(BudgetBlocked,'Unresolved request holds budget'):Ledger(self.job,self.recovery_allowance()).begin('tts','another',1,'edge','edge-tts')
        result=abandon_unusable_output(self.job,self.reconciliation(parent));substitute=result['substitute_key'];authorized=deepcopy(result);begins=[];original_begin=Ledger.begin
        def track(ledger,stage,key,*args,**kwargs):
            begins.append(key);return original_begin(ledger,stage,key,*args,**kwargs)
        with self.recovery_backend([t for k,t in parts[2:]]), patch.object(Ledger,'begin',new=track):
            recovered=synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
        self.assertEqual(begins,[substitute]+[k for k,t in parts[3:]]);self.assertNotIn(parent,begins)
        self.assertEqual([f['request_key'] for f in recovered['chapters'][a]['fragments']],[k for k,t in parts[:2]]+[substitute]+[k for k,t in parts[3:]])
        records=read(self.job/'request-ledger.json')['requests'];self.assertEqual(records[parent],authorized)
        self.assertEqual(records[substitute]['state'],'succeeded');self.assertEqual(records[substitute]['parent_key'],parent)
        for key,item in records_before.items():
            if key!=parent:self.assertEqual(json.dumps(records[key],sort_keys=True).encode(),json.dumps(item,sort_keys=True).encode())
        self.assertEqual(self.audio_bytes(state),files_before);self.assertEqual(recovered['chapters'][b],state['chapters'][b])
        for key,value in checks_before.items():self.assertEqual(read(self.job/'fragment-checksums.json')[key],value)
        for key,value in prefix_before.items():self.assertEqual((self.job/'audio-fragments'/(key+'.mp3')).read_bytes(),value)
        ledger_bytes=(self.job/'request-ledger.json').read_bytes();checksum_bytes=(self.job/'fragment-checksums.json').read_bytes()
        with self.recovery_backend() as backend, patch.object(Ledger,'begin',side_effect=AssertionError('No checkpoint/afterword request')):
            self.assertEqual(synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a]),recovered)
            self.assertEqual(synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[b]),recovered)
            package=read(package_job(self.job)/'manifest.json');backend.synthesize.assert_not_called()
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger_bytes);self.assertEqual((self.job/'fragment-checksums.json').read_bytes(),checksum_bytes)
        self.assertFalse(package['whole_book_verified'])
    def test_substitute_failure_remains_uncertain_and_never_authorizes_attempt_two(self):
        s,tr,a,b,parts,state=self.interrupted_recovery_chapter();parent=parts[2][0];authorized=abandon_unusable_output(self.job,self.reconciliation(parent));substitute=authorized['substitute_key']
        with self.recovery_backend([parts[2][1]],fail_at=0):
            with self.assertRaisesRegex(RuntimeError,'Offline TTS fragment failure'):synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
        data=read(self.job/'request-ledger.json');self.assertEqual(data['requests'][parent],authorized);self.assertEqual(data['requests'][substitute]['state'],'uncertain')
        self.assertNotIn(parts[3][0],data['requests']);self.assertNotIn(substitute_request_key(parent,2),data['requests'])
        for state in ['uncertain','submitted','reconciliation_required']:
            with self.subTest(state=state):
                data['requests'][substitute]['state']=state;write(self.job/'request-ledger.json',data);before=(self.job/'request-ledger.json').read_bytes()
                with self.recovery_backend(), patch.object(Ledger,'begin',side_effect=AssertionError('No second begin')):
                    with self.assertRaisesRegex(BudgetBlocked,'Substitute request .*stop chapter'):synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
                self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
        self.assert_abandon_rejected(self.reconciliation(parent));self.assert_abandon_rejected(self.reconciliation(substitute))
    def test_substitute_cache_requires_a_succeeded_ledger_record(self):
        s,tr,a,b,parts,state=self.interrupted_recovery_chapter();parent=parts[2][0];substitute=abandon_unusable_output(self.job,self.reconciliation(parent))['substitute_key']
        target=self.job/'audio-fragments'/(substitute+'.mp3');target.write_bytes(b'ORPHAN WITHOUT A SUCCEEDED REQUEST')
        checks=read(self.job/'fragment-checksums.json');checks[substitute]=sha(target);write(self.job/'fragment-checksums.json',checks)
        begins=[];original_begin=Ledger.begin
        def track(ledger,stage,key,*args,**kwargs):
            begins.append(key);return original_begin(ledger,stage,key,*args,**kwargs)
        with self.recovery_backend([t for k,t in parts[2:]]), patch.object(Ledger,'begin',new=track):synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
        self.assertEqual(begins,[substitute]+[k for k,t in parts[3:]]);self.assertEqual(read(self.job/'request-ledger.json')['requests'][substitute]['state'],'succeeded')
        self.assertNotEqual(target.read_bytes(),b'ORPHAN WITHOUT A SUCCEEDED REQUEST')
    def test_substitute_checkpoint_reuses_success_before_later_tail_interruption(self):
        s,tr,a,b,parts,state=self.interrupted_recovery_chapter();parent=parts[2][0];substitute=abandon_unusable_output(self.job,self.reconciliation(parent))['substitute_key'];original_begin=Ledger.begin
        def stop_before_tail(ledger,stage,key,*args,**kwargs):
            if key==parts[3][0]:raise RuntimeError('offline stop before tail begin')
            return original_begin(ledger,stage,key,*args,**kwargs)
        with self.recovery_backend([parts[2][1]]), patch.object(Ledger,'begin',new=stop_before_tail):
            with self.assertRaisesRegex(RuntimeError,'offline stop before tail begin'):synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
        self.assertEqual(read(self.job/'request-ledger.json')['requests'][substitute]['state'],'succeeded');begins=[]
        # A succeeded substitute with missing/corrupt media cannot be resubmitted.
        target=self.job/'audio-fragments'/(substitute+'.mp3');media=target.read_bytes();checks=read(self.job/'fragment-checksums.json');before=(self.job/'request-ledger.json').read_bytes()
        for problem in ['missing','corrupt','checksum']:
            with self.subTest(problem=problem):
                target.write_bytes(media);write(self.job/'fragment-checksums.json',checks)
                if problem=='missing':target.unlink()
                elif problem=='corrupt':target.write_bytes(b'CORRUPT')
                else:write(self.job/'fragment-checksums.json',{k:v for k,v in checks.items() if k!=substitute})
                with self.recovery_backend():
                    with self.assertRaisesRegex(BudgetBlocked,'Request key already recorded: succeeded'):synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
                self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
        target.write_bytes(media);write(self.job/'fragment-checksums.json',checks)
        def track(ledger,stage,key,*args,**kwargs):
            begins.append(key);return original_begin(ledger,stage,key,*args,**kwargs)
        with self.recovery_backend([t for k,t in parts[3:]]), patch.object(Ledger,'begin',new=track):synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
        self.assertEqual(begins,[k for k,t in parts[3:]])
    def test_abandon_preserves_unreviewed_and_other_unresolved_gates(self):
        s,tr,a,b,parts,state=self.interrupted_recovery_chapter();parent=parts[2][0];abandon_unusable_output(self.job,self.reconciliation(parent))
        self.rebind_review(s,tr,[b])
        with self.recovery_backend(), patch.object(Ledger,'begin',side_effect=AssertionError('No unreviewed begin')):
            with self.assertRaisesRegex(ValueError,'Unreviewed/failed chapter'):synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
        self.rebind_review(s,tr)
        ledger=Ledger(self.job,self.recovery_allowance());ledger.begin('tts','another-unresolved-key',1,'edge','edge-tts');ledger.uncertain('another-unresolved-key')
        with self.recovery_backend():
            with self.assertRaisesRegex(BudgetBlocked,'Unresolved request holds budget'):synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
    def test_abandoned_legacy_fragment_preserves_whole_book_keys_and_cache_admission(self):
        s,tr,a,b,parts,state=self.interrupted_recovery_chapter(legacy=True);parent=parts[2][0];substitute=abandon_unusable_output(self.job,self.reconciliation(parent))['substitute_key'];begins=[];original_begin=Ledger.begin
        unrelated={p:(self.job/p).read_bytes() for p in [state['chapters'][b]['path']]+['audio-fragments/'+k+'.mp3' for k in read(self.job/'fragment-checksums.json')]};records_before=deepcopy(read(self.job/'request-ledger.json')['requests'])
        def track(ledger,stage,key,*args,**kwargs):
            begins.append(key);return original_begin(ledger,stage,key,*args,**kwargs)
        with self.recovery_backend([t for k,t in parts[2:]]), patch.object(Ledger,'begin',new=track):result=synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a])
        self.assertEqual(begins,[substitute]+[k for k,t in parts[3:]]);self.assertEqual(result['chapters'][a]['fragment_key_scheme'],'whole-book-v1')
        for path,value in unrelated.items():self.assertEqual((self.job/path).read_bytes(),value)
        self.assertEqual(result['chapters'][b]['sha256'],state['chapters'][b]['sha256'])
        for key,record in records_before.items():self.assertEqual(read(self.job/'request-ledger.json')['requests'][key],record)
        before=(self.job/'request-ledger.json').read_bytes()
        with self.recovery_backend(), patch.object(Ledger,'begin',side_effect=AssertionError('No legacy duplicate')):
            self.assertEqual(synthesize_job(self.job,'edge',self.recovery_allowance(),chapter_ids=[a,b]),result)
            self.assertFalse(read(package_job(self.job)/'manifest.json')['whole_book_verified'])
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
    def test_synthesize_single_reviewed_chapter_is_partial(self):
        s,tr=self.scope_job();ch1,ch2=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,[ch2]);keys={key for key,text in parts}
        with self.scope_backend([text for key,text in parts]), patch('book_pipeline.reviewed',wraps=reviewed) as checked:
            result=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[ch2])
            self.assertEqual([call.args[3] for call in checked.call_args_list],[b['id'] for b in s['chapters'][1]['blocks']])
            manifest=read(package_job(self.job)/'manifest.json')
        self.assertEqual(set(result['chapters']),{ch2});self.assertEqual(set(read(self.job/'audio.json')['chapters']),{ch2})
        self.assertEqual(read(self.job/'reading.json'),reading_document(s,tr));self.assertEqual(result['reading_revision'],reading_document(s,tr)['reading_revision'])
        self.assertEqual({p.name for p in (self.job/'chapter-audio').iterdir()},{ch2+'.mp3',ch2+'.concat.txt'})
        self.assertFalse((self.job/'chapter-audio'/(ch1+'.mp3')).exists())
        self.assertEqual({p.stem for p in (self.job/'audio-fragments').iterdir()},keys);self.assertEqual(set(read(self.job/'fragment-checksums.json')),keys)
        self.assertEqual({key for key,r in read(self.job/'request-ledger.json')['requests'].items() if r['stage']=='tts'},keys)
        self.assertEqual(manifest['ready_chapter_ids'],[ch2]);self.assertEqual(manifest['gates']['audio'],'missing_or_partial')
        self.assertEqual(manifest['release_state'],'partial_or_listening_pending');self.assertFalse(manifest['whole_book_verified']);self.assertFalse(manifest['declared_scope_verified'])
    def test_synthesize_unreviewed_selected_chapter_rejected(self):
        s,tr=self.scope_job();before=(self.job/'request-ledger.json').read_bytes()
        with self.scope_backend() as backend, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No TTS reservation')) as begin:
            with self.assertRaisesRegex(ValueError,'Unreviewed/failed chapter cannot be narrated'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[s['chapters'][0]['id']])
            backend.synthesize.assert_not_called();begin.assert_not_called()
        self.assertFalse((self.job/'audio.json').exists());self.assertFalse((self.job/'chapter-audio').exists());self.assertFalse((self.job/'audio-fragments').exists())
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
    def test_synthesize_invalid_scopes_fail_before_backend_or_writes(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];bid=s['chapters'][1]['blocks'][0]['id']
        cases=[([cid,'unknown-chapter'],'Unknown chapter ID'),([bid],'Unknown chapter ID'),([''],'Chapter ID must be a nonempty string'),([' \t'],'Chapter ID must be a nonempty string'),([None],'Chapter ID must be a nonempty string'),([[]],'Chapter ID must be a nonempty string'),([],'Chapter scope'),(cid,'Chapter scope'),([cid,cid],'Duplicate chapter ID')]
        before={str(p.relative_to(self.job)):p.read_bytes() for p in self.job.rglob('*') if p.is_file()}
        with patch('book_pipeline.build_backend',side_effect=AssertionError('No backend creation')) as build, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No TTS reservation')) as begin, patch('book_pipeline.urllib.request.urlopen',side_effect=AssertionError('No network')):
            for scope,message in cases:
                with self.subTest(scope=scope):
                    with self.assertRaisesRegex(ValueError,message):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=scope)
                    self.assertEqual({str(p.relative_to(self.job)):p.read_bytes() for p in self.job.rglob('*') if p.is_file()},before)
            build.assert_not_called();begin.assert_not_called()
    def test_synthesize_scope_still_requires_tts_allowance(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];before=(self.job/'request-ledger.json').read_bytes()
        with patch('book_pipeline.build_backend',side_effect=AssertionError('No backend creation')) as build, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No TTS reservation')) as begin:
            for allowance in [None,{},self.allowance(),{'tts':{}}]:
                with self.subTest(allowance=allowance):
                    with self.assertRaisesRegex(BudgetBlocked,'TTS allowance not verified'):synthesize_job(self.job,'edge',allowance,chapter_ids=[cid])
            with self.assertRaisesRegex(BudgetBlocked,'Paid TTS cost adapter is not validated'):synthesize_job(self.job,'qwen3-api',self.tts_allowance(),chapter_ids=[cid])
            build.assert_not_called();begin.assert_not_called()
        self.assertFalse((self.job/'audio.json').exists());self.assertFalse((self.job/'audio-fragments').exists());self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
    def test_synthesize_default_and_cli_without_scope_still_reject(self):
        s,tr=self.scope_job();path=self.root/'tts-allowance.json';write(path,self.tts_allowance());before=(self.job/'request-ledger.json').read_bytes()
        with self.scope_backend() as backend, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No TTS reservation')) as begin:
            with self.assertRaisesRegex(ValueError,'Unreviewed/failed chapter cannot be narrated'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=None)
            with patch.object(sys,'argv',['book_pipeline.py','synthesize','--job',str(self.job),'--tts','edge','--allowance',str(path)]):
                with self.assertRaisesRegex(ValueError,'Unreviewed/failed chapter cannot be narrated'):main()
            backend.synthesize.assert_not_called();begin.assert_not_called()
        self.assertFalse((self.job/'audio.json').exists());self.assertFalse((self.job/'chapter-audio').exists());self.assertFalse((self.job/'audio-fragments').exists())
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
    def test_synthesize_fragment_failure_retains_checkpoint_and_uncertainty(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];parts=self.scope_fragments(s,tr,[cid]);self.assertGreater(len(parts),2)
        with self.scope_backend([text for key,text in parts[:2]],fail_at=1):
            with self.assertRaisesRegex(RuntimeError,'Offline TTS fragment failure'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
        self.assertFalse((self.job/'audio.json').exists());self.assertFalse((self.job/'chapter-audio'/(cid+'.mp3')).exists())
        checks=read(self.job/'fragment-checksums.json');self.assertEqual(set(checks),{parts[0][0]});self.assertEqual(checks[parts[0][0]],sha(self.job/'audio-fragments'/(parts[0][0]+'.mp3')))
        records={key:r for key,r in read(self.job/'request-ledger.json')['requests'].items() if r['stage']=='tts'}
        self.assertEqual(set(records),{key for key,text in parts[:2]});self.assertEqual(records[parts[0][0]]['state'],'succeeded');self.assertEqual(records[parts[1][0]]['state'],'uncertain')
        self.assertEqual(records[parts[1][0]]['reserved_characters'],len(parts[1][1]));self.assertIn('reservation retained',records[parts[1][0]]['reason'])
        before=(self.job/'request-ledger.json').read_bytes()
        with self.assertRaisesRegex(BudgetBlocked,'Unresolved request holds budget'):Ledger(self.job,self.tts_allowance()).begin('tts','another-fragment',1,'edge','offline')
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
    def test_synthesize_scope_checkpoint_does_not_double_consume(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];parts=self.scope_fragments(s,tr,[cid])
        with self.scope_backend([text for key,text in parts]):first=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
        names=['audio.json','request-ledger.json','fragment-checksums.json',first['chapters'][cid]['path']];before={name:(self.job/name).read_bytes() for name in names}
        with self.scope_backend() as backend, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No second reservation')) as begin:
            second=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid]);backend.synthesize.assert_not_called();begin.assert_not_called()
        self.assertEqual(second,first);self.assertEqual(sha(self.job/second['chapters'][cid]['path']),first['chapters'][cid]['sha256'])
        self.assertEqual({name:(self.job/name).read_bytes() for name in names},before)
    def test_synthesize_scope_leaves_existing_unselected_audio_untouched(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));ch1,ch2=[c['id'] for c in s['chapters']];parts1=self.scope_fragments(s,tr,[ch1]);parts2=self.scope_fragments(s,tr,[ch2])
        with self.scope_backend([text for key,text in parts1]):first=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[ch1])
        names=[first['chapters'][ch1]['path'],'chapter-audio/'+ch1+'.concat.txt']+['audio-fragments/'+key+'.mp3' for key,text in parts1]
        before={name:((self.job/name).read_bytes(),(self.job/name).stat().st_mtime_ns) for name in names};records=read(self.job/'request-ledger.json')['requests']
        with self.scope_backend([text for key,text in parts2]), patch('book_pipeline.reviewed',wraps=reviewed) as checked:
            second=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[ch2])
            self.assertEqual([call.args[3] for call in checked.call_args_list],[b['id'] for b in s['chapters'][1]['blocks']])
        self.assertEqual(set(second['chapters']),{ch1,ch2});self.assertEqual(second['chapters'][ch1],first['chapters'][ch1]);self.assertEqual(second['reading_revision'],first['reading_revision'])
        self.assertEqual({name:((self.job/name).read_bytes(),(self.job/name).stat().st_mtime_ns) for name in names},before)
        current=read(self.job/'request-ledger.json')['requests'];self.assertTrue(all(current[key]==record for key,record in records.items()))
        self.assertEqual({key for key,r in current.items() if r['stage']=='tts'},{key for key,text in parts1+parts2})
    def test_synthesize_cli_repeated_chapters_follow_source_order(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));ids=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,ids);path=self.root/'tts-allowance.json';write(path,self.tts_allowance())
        with self.scope_backend([text for key,text in parts]), patch.object(sys,'argv',['book_pipeline.py','synthesize','--job',str(self.job),'--tts','edge','--allowance',str(path),'--chapter',ids[1],'--chapter',ids[0]]), patch('sys.stdout',new_callable=io.StringIO) as output:
            main()
        result=json.loads(output.getvalue());self.assertEqual(list(result['chapters']),ids);self.assertEqual(result,read(self.job/'audio.json'))
    def test_unchanged_two_chapters_reuse_without_requests_or_reencoding(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));ids=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,ids)
        with self.scope_backend([t for k,t in parts]):first=synthesize_job(self.job,'edge',self.tts_allowance())
        before=self.audio_bytes(first);metadata={p:(self.job/p).stat().st_mtime_ns for p in before}
        ledger=(self.job/'request-ledger.json').read_bytes();audio=(self.job/'audio.json').read_bytes()
        with self.scope_backend() as backend, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No new request')) as begin:
            for scope in [[ids[1]],ids,None]:
                self.assertEqual(synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=scope),first)
            backend.synthesize.assert_not_called();begin.assert_not_called()
        self.assertEqual(self.audio_bytes(first),before);self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
        self.assertEqual({p:(self.job/p).stat().st_mtime_ns for p in before},metadata)
        self.assertEqual((self.job/'audio.json').read_bytes(),audio)
    def test_block_reordering_invalidates_only_its_chapter(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));a,b=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,[a,b])
        with self.scope_backend([t for k,t in parts]):first=synthesize_job(self.job,'edge',self.tts_allowance())
        before=self.audio_bytes(first);ledger=(self.job/'request-ledger.json').read_bytes()
        s['chapters'][0]['blocks'].reverse();write(self.job/'source.json',s)
        with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No new request')):
            result=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[b]);package=read(package_job(self.job)/'manifest.json')
        self.assertEqual(set(result['chapters']),{b});self.assertEqual(result['chapters'][b],first['chapters'][b])
        self.assertEqual(package['ready_chapter_ids'],[b]);self.assertIsNone(package['chapters'][0]['audio'])
        self.assertEqual(self.audio_bytes(first),before);self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
    def test_package_independently_excludes_changed_chapter_without_synthesis(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));a,b=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,[a,b])
        with self.scope_backend([t for k,t in parts]):first=synthesize_job(self.job,'edge',self.tts_allowance())
        files=self.audio_bytes(first);ledger=(self.job/'request-ledger.json').read_bytes()
        amended=amend_translation(self.job,self.amendment(s,tr,s['chapters'][0]['blocks'][0]['id']));self.rebind_review(s,amended,[b])
        with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No package request')):
            package=read(package_job(self.job)/'manifest.json')
        self.assertEqual(package['ready_chapter_ids'],[b]);self.assertIsNone(package['chapters'][0]['audio'])
        self.assertEqual(package['chapters'][1]['audio'],first['chapters'][b]);self.assertFalse(package['whole_book_verified'])
        self.assertEqual(self.audio_bytes(first),files);self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
    def test_cancel_still_blocks_a_fully_cached_chapter(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];parts=self.scope_fragments(s,tr,[cid])
        with self.scope_backend([t for k,t in parts]):state=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
        audio=(self.job/'audio.json').read_bytes();ledger=(self.job/'request-ledger.json').read_bytes();files=self.audio_bytes(state)
        write(self.job/'cancel',{'at':'offline fixture'})
        with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No cancelled request')):
            with self.assertRaisesRegex(RuntimeError,'Book job cancelled'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
        self.assertEqual((self.job/'audio.json').read_bytes(),audio);self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger);self.assertEqual(self.audio_bytes(state),files)
    def assert_voice_change_invalidates(self,voice_slot='male_1',resolved_voice='offline-voice'):
        s,tr=self.scope_job(reviewed_indexes=(0,1));a,b=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,[a,b])
        with self.scope_backend([t for k,t in parts]):first=synthesize_job(self.job,'edge',self.tts_allowance())
        before=self.audio_bytes(first);tail=self.scope_fragments(s,tr,[b]);records=read(self.job/'request-ledger.json')['requests']
        with self.scope_backend([t for k,t in tail]) as backend:
            backend.resolve_voice.return_value=resolved_voice
            result=synthesize_job(self.job,'edge',self.tts_allowance(),voice_slot=voice_slot,chapter_ids=[b])
            package=read(package_job(self.job)/'manifest.json')
        self.assertEqual(set(result['chapters']),{b});self.assertEqual(package['ready_chapter_ids'],[b])
        self.assertNotEqual(result['chapters'][b]['admission_hash'],first['chapters'][b]['admission_hash'])
        self.assertEqual(self.audio_bytes(first),before)
        current=read(self.job/'request-ledger.json')['requests'];self.assertTrue(all(current[k]==r for k,r in records.items()))
        self.assertEqual(len(current)-len(records),len(tail))
    def test_voice_slot_change_requires_new_audio(self):
        self.assert_voice_change_invalidates(voice_slot='female_1')
    def test_resolved_voice_change_requires_new_audio(self):
        self.assert_voice_change_invalidates(resolved_voice='different-offline-voice')
    def test_unused_glossary_change_does_not_invalidate_reading(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));ids=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,ids)
        with self.scope_backend([t for k,t in parts]):first=synthesize_job(self.job,'edge',self.tts_allowance())
        before=self.audio_bytes(first);ledger=(self.job/'request-ledger.json').read_bytes()
        tr['config']['glossary']={'Term absent from both chapters':'未出现的术语'}
        tr['config_identity']=digest([s['extraction_revision'],tr['config']]);tr['translation_revision']=digest([tr['config_identity'],tr['blocks']]);write(self.job/'translations.json',tr)
        self.rebind_review(s,tr)
        self.assertNotEqual(first['reading_revision'],reading_document(s,tr)['reading_revision'])
        with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No new request')):
            result=synthesize_job(self.job,'edge',self.tts_allowance());package=read(package_job(self.job)/'manifest.json')
        self.assertEqual(result['chapters'],first['chapters']);self.assertEqual(package['ready_chapter_ids'],ids)
        self.assertEqual(self.audio_bytes(first),before);self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
        self.assertFalse(package['whole_book_verified'])
    def test_real_footnote_reading_prefix_change_invalidates_one_chapter(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));a,b=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,[a,b])
        with self.scope_backend([t for k,t in parts]):first=synthesize_job(self.job,'edge',self.tts_allowance())
        note=s['chapters'][0]['blocks'][-1];self.assertEqual(note['kind'],'footnote');note['kind']='p';write(self.job/'source.json',s)
        current=reading_document(s,tr);self.assertNotEqual(current['chapters'][0]['blocks'][-1]['text'],first['chapters'][a]['dependency_manifest']['blocks'][-1]['reading_text'])
        with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No new request')):
            result=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[b])
        self.assertEqual(set(result['chapters']),{b});self.assertEqual(result['chapters'][b],first['chapters'][b])
    def test_checkpoint_tail_retries_only_unsubmitted_fragments(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];parts=self.scope_fragments(s,tr,[cid]);original_begin=Ledger.begin;attempted=[]
        def stop_before_request(ledger,stage,key,*args,**kwargs):
            attempted.append(key)
            if len(attempted)==3:raise RuntimeError('Offline interruption before submit')
            return original_begin(ledger,stage,key,*args,**kwargs)
        with self.scope_backend([t for k,t in parts[:2]]), patch.object(Ledger,'begin',new=stop_before_request):
            with self.assertRaisesRegex(RuntimeError,'interruption before submit'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
        self.assertFalse((self.job/'audio.json').exists());self.assertFalse((self.job/'chapter-audio'/(cid+'.mp3')).exists())
        checks=read(self.job/'fragment-checksums.json');completed={k:(self.job/'audio-fragments'/(k+'.mp3')).read_bytes() for k in checks}
        records=deepcopy(read(self.job/'request-ledger.json')['requests']);resumed=[]
        def resume_request(ledger,stage,key,*args,**kwargs):
            resumed.append(key);return original_begin(ledger,stage,key,*args,**kwargs)
        with self.scope_backend([t for k,t in parts[2:]]), patch.object(Ledger,'begin',new=resume_request):
            result=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
        self.assertEqual(resumed,[k for k,t in parts[2:]])
        self.assertEqual({k:(self.job/'audio-fragments'/(k+'.mp3')).read_bytes() for k in checks},completed)
        current=read(self.job/'request-ledger.json')['requests'];self.assertTrue(all(current[k]==r for k,r in records.items()))
        self.assertEqual([f['request_key'] for f in result['chapters'][cid]['fragments']],[k for k,t in parts])
        self.assertEqual(set(read(self.job/'fragment-checksums.json')),{k for k,t in parts})
    def test_source_target_and_review_mismatches_cannot_narrate(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];bid=s['chapters'][1]['blocks'][0]['id'];review=read(self.job/'review.json')
        for mismatch in ['source','source_binding','target','review_hash','review_revision','fixture_only','failed']:
            with self.subTest(mismatch=mismatch):
                ss,tt,rr=deepcopy(s),deepcopy(tr),deepcopy(review)
                if mismatch=='source':ss['chapters'][1]['blocks'][0]['text']+=' changed'
                elif mismatch=='source_binding':tt['blocks'][bid]['source_hash']='wrong'
                elif mismatch=='target':tt['blocks'][bid]['text']+=' changed'
                elif mismatch=='review_hash':rr['blocks'][bid]['target_hash']='wrong'
                elif mismatch=='review_revision':rr['translation_revision']='old'
                elif mismatch=='fixture_only':rr['fixture_only']=True
                else:rr['blocks'][bid]['verdict']='fail'
                write(self.job/'source.json',ss);write(self.job/'translations.json',tt);write(self.job/'review.json',rr)
                with self.scope_backend() as backend, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No unreviewed request')):
                    with self.assertRaisesRegex(ValueError,'Unreviewed/failed chapter cannot be narrated'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
                    backend.synthesize.assert_not_called()
                self.assertFalse((self.job/'audio.json').exists());self.assertFalse((self.job/'audio-fragments').exists())
    def test_default_still_stops_at_later_unreviewed_chapter(self):
        s,tr=self.scope_job(reviewed_indexes=(0,));a,b=[c['id'] for c in s['chapters']];parts=self.scope_fragments(s,tr,[a])
        with self.scope_backend([t for k,t in parts]):
            with self.assertRaisesRegex(ValueError,'Unreviewed/failed chapter cannot be narrated'):synthesize_job(self.job,'edge',self.tts_allowance())
        first=read(self.job/'audio.json');self.assertEqual(set(first['chapters']),{a});before=self.audio_bytes(first)
        with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No duplicate request')):
            with self.assertRaisesRegex(ValueError,'Unreviewed/failed chapter cannot be narrated'):synthesize_job(self.job,'edge',self.tts_allowance())
            package=read(package_job(self.job)/'manifest.json')
        self.assertEqual(self.audio_bytes(first),before);self.assertEqual(package['ready_chapter_ids'],[a]);self.assertFalse(package['whole_book_verified'])
    def legacy_audio(self,s,tr,chapter_ids):
        # Construct the pre-manifest format independently, including its original ledger keys.
        rd=reading_document(s,tr);config={'backend':'offline-edge','voice_slot':'male_1','resolved_voice':'offline-voice','style':'documentary'}
        state={'config':config,'reading_revision':rd['reading_revision'],'chapters':{}};checks={};ledger=Ledger(self.job,self.tts_allowance())
        for c in rd['chapters']:
            if c['id'] not in chapter_ids:continue
            anchors=[];media=b'';start=0.0
            for b in c['blocks']:
                for n,offset in enumerate(range(0,len(b['text']),4)):
                    value=b['text'][offset:offset+4];key=digest([rd['reading_revision'],config,b['source_block_id'],n,value])
                    path=self.job/'audio-fragments'/(key+'.mp3');path.parent.mkdir(exist_ok=True);path.write_bytes(b'OLD OFFLINE: '+value.encode())
                    checks[key]=sha(path);media+=path.read_bytes();ledger.begin('tts',key,len(value),'edge','offline');ledger.finish(key,{'fixture_only':True})
                    anchors.append({'source_block_id':b['source_block_id'],'reading_character_start':offset,'reading_character_end':offset+len(value),'media_start_seconds':start,'duration_seconds':1.0,'alignment':'measured audio fragment only; not word/sentence alignment'});start+=1.0
            path=self.job/'chapter-audio'/(c['id']+'.mp3');path.parent.mkdir(exist_ok=True);path.write_bytes(media)
            state['chapters'][c['id']]={'path':str(path.relative_to(self.job)),'sha256':sha(path),'duration_seconds':1.0,'audio_revision':digest([rd['reading_revision'],config,sha(path)]),'fragments':anchors,'audibility_review':'not_yet_listened'}
        state['audio_revision']=digest(state);write(self.job/'audio.json',state);write(self.job/'fragment-checksums.json',checks);return state
    def test_legacy_migration_reuses_original_keys_across_unrelated_amend(self):
        s,tr=self.scope_job(reviewed_indexes=(0,1));a,b=[c['id'] for c in s['chapters']];old=self.legacy_audio(s,tr,[b])
        files={str(p.relative_to(self.job)):(p.read_bytes(),p.stat().st_mtime_ns) for folder in ['chapter-audio','audio-fragments'] for p in (self.job/folder).iterdir()}
        ledger=(self.job/'request-ledger.json').read_bytes();checks=(self.job/'fragment-checksums.json').read_bytes()
        amended=amend_translation(self.job,self.amendment(s,tr,s['chapters'][0]['blocks'][0]['id']));self.rebind_review(s,amended,[b])
        with self.scope_backend() as backend, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No migrated request')) as begin:
            result=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[b])
            self.assertEqual(synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[b]),result)
            package=read(package_job(self.job)/'manifest.json');backend.synthesize.assert_not_called();begin.assert_not_called()
        entry=result['chapters'][b];provenance=entry['migration_provenance']
        self.assertEqual(provenance['prior_reading_revision'],old['reading_revision']);self.assertEqual(provenance['prior_audio_revision'],old['chapters'][b]['audio_revision'])
        self.assertEqual(provenance['prior_mp3_sha256'],old['chapters'][b]['sha256']);self.assertIsNotNone(datetime.fromisoformat(provenance['verified_at']).tzinfo)
        self.assertEqual(len(provenance['verified_fields']),5);self.assertEqual(entry['admission_hash'],digest(entry['dependency_manifest']))
        self.assertEqual(entry['audio_revision'],old['chapters'][b]['audio_revision']);self.assertEqual(result['reading_revision'],old['reading_revision'])
        self.assertEqual(result['reading_revision_snapshot'],reading_document(s,amended)['reading_revision'])
        self.assertEqual({p:((self.job/p).read_bytes(),(self.job/p).stat().st_mtime_ns) for p in files},files)
        self.assertEqual(set(files),{str(p.relative_to(self.job)) for folder in ['chapter-audio','audio-fragments'] for p in (self.job/folder).iterdir()})
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger);self.assertEqual((self.job/'fragment-checksums.json').read_bytes(),checks)
        self.assertTrue(all(f['request_key'] in read(self.job/'fragment-checksums.json') for f in entry['fragments']))
        self.assertEqual(package['ready_chapter_ids'],[b]);self.assertFalse(package['whole_book_verified'])
    def test_package_can_migrate_matching_legacy_chapter(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];old=self.legacy_audio(s,tr,[cid]);ledger=(self.job/'request-ledger.json').read_bytes()
        with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No package request')):
            package=read(package_job(self.job)/'manifest.json');again=read(package_job(self.job)/'manifest.json')
        self.assertEqual(package,again);self.assertEqual(package['ready_chapter_ids'],[cid]);self.assertFalse(package['whole_book_verified'])
        self.assertEqual(package['chapters'][1]['audio']['sha256'],old['chapters'][cid]['sha256'])
        self.assertIn('migration_provenance',read(self.job/'audio.json')['chapters'][cid]);self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
    def test_legacy_changed_text_is_not_forged_or_automatically_resynthesized(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];old=self.legacy_audio(s,tr,[cid]);original=(self.job/'audio.json').read_bytes()
        bid=s['chapters'][1]['blocks'][0]['id'];amended=amend_translation(self.job,self.amendment(s,tr,bid));self.rebind_review(s,amended,[cid])
        ledger=(self.job/'request-ledger.json').read_bytes()
        with self.scope_backend() as backend, patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No request')):
            with self.assertRaisesRegex(ValueError,'Legacy audio migration verification failed'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
            self.assertEqual((self.job/'audio.json').read_bytes(),original)
            package=read(package_job(self.job)/'manifest.json')
            with self.assertRaisesRegex(ValueError,'Legacy audio migration verification failed'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
            backend.synthesize.assert_not_called()
        self.assertEqual(package['ready_chapter_ids'],[]);self.assertFalse(package['whole_book_verified']);self.assertIsNone(package['chapters'][1]['audio'])
        state=read(self.job/'audio.json');self.assertEqual(state['chapters'],{});retained=state['invalidated_chapters'][0]['audio']
        self.assertEqual(retained,old['chapters'][cid]);self.assertNotIn('admission_hash',retained);self.assertEqual(state['reading_revision'],old['reading_revision'])
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
    def test_legacy_migration_requires_order_voice_ranges_sha_and_checksums(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];old=self.legacy_audio(s,tr,[cid]);checks=read(self.job/'fragment-checksums.json')
        for mismatch in ['order','voice','ranges','mp3_sha','fragment_checksum','audio_revision','missing_fragments']:
            with self.subTest(mismatch=mismatch):
                ss,state,cc=deepcopy(s),deepcopy(old),deepcopy(checks)
                if mismatch=='order':ss['chapters'][1]['blocks'].reverse()
                elif mismatch=='voice':state['config']['resolved_voice']='wrong'
                elif mismatch=='ranges':state['chapters'][cid]['fragments'][0]['reading_character_end']-=1
                elif mismatch=='mp3_sha':state['chapters'][cid]['sha256']='wrong'
                elif mismatch=='fragment_checksum':cc[next(iter(cc))]='wrong'
                elif mismatch=='audio_revision':state['chapters'][cid]['audio_revision']='wrong'
                else:state['chapters'][cid]['fragments'].pop()
                write(self.job/'source.json',ss);write(self.job/'audio.json',state);write(self.job/'fragment-checksums.json',cc);before=(self.job/'audio.json').read_bytes()
                with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No request')):
                    with self.assertRaisesRegex(ValueError,'Legacy audio migration verification failed'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
                self.assertEqual((self.job/'audio.json').read_bytes(),before);self.assertNotIn('dependency_manifest',read(self.job/'audio.json')['chapters'][cid])
    def test_corrupt_modern_files_never_reuse_or_resubmit_recorded_keys(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];parts=self.scope_fragments(s,tr,[cid])
        with self.scope_backend([t for k,t in parts]):state=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
        files=self.audio_bytes(state);ledger=(self.job/'request-ledger.json').read_bytes()
        for path in [state['chapters'][cid]['path'],state['chapters'][cid]['fragments'][0]['path']]:
            with self.subTest(path=path):
                write(self.job/'audio.json',state)
                for name,data in files.items():(self.job/name).write_bytes(data)
                (self.job/path).write_bytes(b'corrupt')
                with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No blind resubmit')):
                    with self.assertRaisesRegex(ValueError,'Audio cache verification failed'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
                    package=read(package_job(self.job)/'manifest.json')
                self.assertEqual(package['ready_chapter_ids'],[]);self.assertFalse(package['whole_book_verified'])
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
    def test_modern_manifest_hash_and_fragment_bindings_cannot_be_forged(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];parts=self.scope_fragments(s,tr,[cid])
        with self.scope_backend([t for k,t in parts]):original=synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
        ledger=(self.job/'request-ledger.json').read_bytes()
        for mismatch in ['manifest','admission_hash','fragment_key','fragment_path','fragment_sha','audio_revision']:
            with self.subTest(mismatch=mismatch):
                state=deepcopy(original);entry=state['chapters'][cid]
                if mismatch=='manifest':entry['dependency_manifest']['blocks'][0]['reading_text']+=' forged'
                elif mismatch=='admission_hash':entry['admission_hash']='forged'
                elif mismatch=='audio_revision':entry['audio_revision']='forged'
                else:entry['fragments'][0][{'fragment_key':'request_key','fragment_path':'path','fragment_sha':'sha256'}[mismatch]]='forged'
                write(self.job/'audio.json',state);before=(self.job/'audio.json').read_bytes()
                with self.scope_backend(), patch('book_pipeline.Ledger.begin',side_effect=AssertionError('No forged request')):
                    with self.assertRaisesRegex(ValueError,'Audio cache verification failed'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
                    self.assertEqual((self.job/'audio.json').read_bytes(),before)
                    package=read(package_job(self.job)/'manifest.json')
                self.assertEqual(package['ready_chapter_ids'],[]);self.assertFalse(package['whole_book_verified'])
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),ledger)
    def test_unknown_tts_and_missing_rights_are_still_blocked(self):
        s,tr=self.scope_job();cid=s['chapters'][1]['id'];before=(self.job/'request-ledger.json').read_bytes()
        with patch('book_pipeline.build_backend',side_effect=AssertionError('No backend')) as build:
            with self.assertRaises(BudgetBlocked):synthesize_job(self.job,'unknown-provider',self.tts_allowance(),chapter_ids=[cid])
            s['rights']=dict(s['rights'],external_processing=False);write(self.job/'source.json',s)
            with self.assertRaisesRegex(BudgetBlocked,'rights'):synthesize_job(self.job,'edge',self.tts_allowance(),chapter_ids=[cid])
            build.assert_not_called()
        self.assertEqual((self.job/'request-ledger.json').read_bytes(),before);self.assertFalse((self.job/'audio.json').exists())
    def timeout_edge_job(self):
        fixture.make(self.source,{'OEBPS/ch2.xhtml':'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Afterword</title></head><body><p id="afterword">A single offline test paragraph.</p></body></html>'})
        s,tr=self.scope_job();cid=s['chapters'][1]['id']
        text=reading_document(s,tr)['chapters'][1]['blocks'][0]['text']
        self.assertEqual(len(reading_document(s,tr)['chapters'][1]['blocks']),1)
        allowance=self.tts_allowance();allowance['tts']['model']='edge-tts'
        return s,tr,cid,text,allowance
    def test_book_edge_receives_180_once(self):
        s,tr,cid,text,allowance=self.timeout_edge_job()
        async def save(output):Path(output).write_bytes(b'OFFLINE TEST AUDIO')
        def concatenate(args,**kwargs):Path(args[-1]).write_bytes(b'OFFLINE TEST CHAPTER')
        with patch('tts_backends.edge_tts.Communicate') as communicate, patch('book_pipeline.duration',return_value=1.0), patch('book_pipeline.subprocess.run',side_effect=concatenate):
            communicate.return_value.save=AsyncMock(side_effect=save)
            state=synthesize_job(self.job,'edge',allowance,chapter_ids=[cid])
            communicate.assert_called_once_with(text,build_backend('edge').resolve_voice('male_1'),receive_timeout=180)
            communicate.return_value.save.assert_awaited_once()
            self.assertEqual(state,synthesize_job(self.job,'edge',allowance,chapter_ids=[cid]))
            communicate.assert_called_once();communicate.return_value.save.assert_awaited_once()
        self.assertIn(cid,state['chapters'])
        records=[r for r in read(self.job/'request-ledger.json')['requests'].values() if r['stage']=='tts']
        self.assertEqual(len(records),1);self.assertEqual(records[0]['state'],'succeeded')
    def test_book_edge_socket_timeout_is_uncertain_without_retry(self):
        from aiohttp.client_exceptions import SocketTimeoutError
        s,tr,cid,text,allowance=self.timeout_edge_job();error=SocketTimeoutError('Timeout on reading data from socket')
        with patch('tts_backends.edge_tts.Communicate') as communicate:
            communicate.return_value.save=AsyncMock(side_effect=error)
            with self.assertRaises(SocketTimeoutError) as caught:synthesize_job(self.job,'edge',allowance,chapter_ids=[cid])
            self.assertIs(caught.exception,error)
            communicate.assert_called_once_with(text,build_backend('edge').resolve_voice('male_1'),receive_timeout=180)
            communicate.return_value.save.assert_awaited_once()
        records=[r for r in read(self.job/'request-ledger.json')['requests'].values() if r['stage']=='tts']
        self.assertEqual(len(records),1);self.assertEqual(records[0]['state'],'uncertain');self.assertNotIn('events',records[0])
        self.assertFalse((self.job/'chapter-audio'/(cid+'.mp3')).exists());self.assertFalse((self.job/'fragment-checksums.json').exists())
        self.assertNotIn(cid,read(self.job/'audio.json')['chapters'] if (self.job/'audio.json').exists() else {})
        self.assertEqual(read(package_job(self.job)/'manifest.json')['ready_chapter_ids'],[])
    def test_non_book_edge_keeps_library_receive_timeout_default(self):
        import asyncio
        backend=build_backend('edge');request=SynthesisRequest('离线测试。','male_1','documentary')
        with patch('tts_backends.edge_tts.Communicate') as communicate:
            communicate.return_value.save=AsyncMock()
            output=self.root/'non-book.mp3';asyncio.run(backend.synthesize(request,output))
            communicate.assert_called_once_with(request.text,backend.resolve_voice(request.voice_slot))
            communicate.return_value.save.assert_awaited_once_with(str(output))
    def test_book_edge_timeout_does_not_change_chapter_admission(self):
        from book_pipeline import narration_config
        s,tr=self.scope_job();default=build_backend('edge');book=build_backend('edge',book_synthesis=True)
        self.assertEqual(default.fingerprint(),book.fingerprint())
        before=narration_config(default,'male_1','documentary');after=narration_config(book,'male_1','documentary')
        self.assertEqual(before,after)
        for c in reading_document(s,tr)['chapters']:
            with self.subTest(chapter=c['id']):
                original=chapter_manifest(s,tr,c,before,default);updated=chapter_manifest(s,tr,c,after,book)
                self.assertNotIn('receive_timeout',json.dumps(updated));self.assertNotIn('book_synthesis',json.dumps(updated))
                self.assertEqual(original,updated);self.assertEqual(digest(original),digest(updated))
    def test_ledger_and_non_edge_backend_match_pre_timeout_source_bytes(self):
        import ast,hashlib
        # Exact class-byte hashes from the protected pre-timeout dirty backup.
        for file,name,expected in [('book_pipeline.py','Ledger','4d5aa8e7dee1fccebf8c1d63683cb706f41755db6d8dd46cd7b84977cb67fb58'),('tts_backends.py','Qwen3APIBackend','5c7758a45e36eba984f49b25d3cc71340fe2abb187a4e5a929245462eed98eb1')]:
            with self.subTest(source=name):
                raw=(ROOT/'scripts'/file).read_bytes();lines=raw.splitlines(keepends=True)
                node=next(n for n in ast.parse(raw).body if isinstance(n,ast.ClassDef) and n.name==name)
                self.assertEqual(hashlib.sha256(b''.join(lines[node.lineno-1:node.end_lineno])).hexdigest(),expected)
if __name__=='__main__':unittest.main()
