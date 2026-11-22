import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from book_import import import_source,BookImportError,digest
from book_pipeline import create_job,package_job,Ledger,BudgetBlocked,write,read,reading_document,apply_review,lock
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
if __name__=='__main__':unittest.main()
