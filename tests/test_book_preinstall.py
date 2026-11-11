"""Additional offline failure-window journeys; real-cache tests require an explicit copied job."""
import importlib.util,json,os,shutil,sys,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from book_pipeline import *
from book_import import digest,import_source,BookImportError
spec=importlib.util.spec_from_file_location('book_fixture',ROOT/'fixtures/book-integration/generate_sample.py');fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
class BudgetRestartJourneys(unittest.TestCase):
 def setUp(self):self.t=tempfile.TemporaryDirectory();self.job=Path(self.t.name)/'job';self.job.mkdir();n=datetime.now(timezone.utc);self.a={'translation':{'provider':'test','model':'test','verified_at':n.isoformat(),'expires_at':(n+timedelta(hours=1)).isoformat(),'max_requests':3,'max_characters':100,'max_total_cost_microunits':30,'evidence_ref':'offline fixture, not billing authorization','no_new_charges_verified':True}}
 def tearDown(self):self.t.cleanup()
 def test_submitted_restart_retains_reservation(self):
  l=Ledger(self.job,self.a);l.begin('translation','request',10,'test','test',20);before=l.path.read_bytes()
  with self.assertRaises(BudgetBlocked):Ledger(self.job,self.a).begin('translation','other',1,'test','test',1)
  self.assertEqual(l.path.read_bytes(),before)
 def test_cancel_does_not_release_submitted_or_uncertain(self):
  for state in ['submitted','uncertain']:
   job=self.job/state;job.mkdir();l=Ledger(job,self.a);l.begin('translation','one',10,'test','test',20)
   if state=='uncertain':l.uncertain('one')
   before=l.path.read_bytes();write(job/'cancel',{'at':'fixture'})
   with self.assertRaises(RuntimeError):Ledger(job,self.a).begin('translation','two',1,'test','test',1)
   self.assertEqual(l.path.read_bytes(),before)
 def test_expired_allowance_stops_without_ledger(self):
  self.a['translation']['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
  with self.assertRaises(BudgetBlocked):Ledger(self.job,self.a).begin('translation','one',1,'test','test',1)
  self.assertFalse((self.job/'request-ledger.json').exists())
 def test_editing_cached_target_invalidates_review(self):
  source=fixture.make(self.job/'input.epub');s=create_job(source,self.job/'content',rights=fixture.RIGHTS);b=s['chapters'][0]['blocks'][0]
  t={'source_hash':b['source_text_hash'],'text':'第一章：花园','target_hash':digest('第一章：花园')}
  tr={'translation_revision':'r','blocks':{b['id']:t}};review={'translation_revision':'r','extraction_revision':s['extraction_revision'],'blocks':{b['id']:{'source_hash':t['source_hash'],'target_hash':t['target_hash'],'verdict':'pass','reviewer':'offline function fixture','evidence':'hash consistency only, not a release review'}}}
  self.assertTrue(reviewed(s,tr,review,b['id']))
  t['text']='silently replaced after review'
  self.assertFalse(reviewed(s,tr,review,b['id']))

class MalformedResourceJourneys(unittest.TestCase):
 def setUp(self):self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);self.source=fixture.make(self.root/'source.epub')
 def tearDown(self):self.t.cleanup()
 def test_duplicate_zip_member_rejected(self):
  import zipfile,warnings
  with warnings.catch_warnings():
   warnings.simplefilter('ignore')
   with zipfile.ZipFile(self.source,'a') as z:z.writestr('OEBPS/ch1.xhtml',fixture.CH1)
  with self.assertRaises(BookImportError):import_source(self.source,rights=fixture.RIGHTS)
 def test_missing_resource_and_invalid_spine_rejected(self):
  for changed in [fixture.OPF.replace('href="ch2.xhtml"','href="missing.xhtml"'),fixture.OPF.replace('idref="two"','idref="unknown"')]:
   fixture.make(self.source,{'OEBPS/content.opf':changed})
   with self.assertRaises(BookImportError):import_source(self.source,rights=fixture.RIGHTS)
 def test_ambiguous_manifest_alias_rejected(self):
  fixture.make(self.source,{'OEBPS/content.opf':fixture.OPF.replace('</manifest>','<item id="alias" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>')})
  with self.assertRaises(BookImportError):import_source(self.source,rights=fixture.RIGHTS)
 def test_broken_note_is_partial_not_lost_silently(self):
  fixture.make(self.source,{'OEBPS/ch1.xhtml':fixture.CH1.replace('notes.xhtml#n1','notes.xhtml#missing')})
  r=import_source(self.source,rights=fixture.RIGHTS)
  self.assertTrue(any(i['kind']=='unresolved_reference' for i in r['inventory']['unresolved']));self.assertEqual(r['inventory']['readable_text_units'],7)
 def test_mid_package_copy_failure_leaves_no_incoming_revision(self):
  job=self.root/'job';create_job(self.source,job,rights=fixture.RIGHTS)
  old=package_job(job);old_hashes={str(p.relative_to(old)):sha(p) for p in old.rglob('*') if p.is_file()}
  source=read(job/'source.json');source['title']='new package fixture title';write(job/'source.json',source)
  actual=shutil.copyfile;count=0
  def interrupted(*args,**kw):
   nonlocal count
   count+=1
   if count==2:raise OSError('fixture interrupted copy, not disk failure')
   return actual(*args,**kw)
  with patch('book_pipeline.shutil.copyfile',side_effect=interrupted):
   with self.assertRaises(OSError):package_job(job)
  self.assertEqual(list((job/'packages').iterdir()),[old])
  self.assertEqual({str(p.relative_to(old)):sha(p) for p in old.rglob('*') if p.is_file()},old_hashes)

@unittest.skipUnless(os.getenv('BOOK_REAL_CACHE_JOB'),'Explicit real-cache job not provided; no provider calls permitted')
class RealCacheRestartJourneys(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.job=Path(self.t.name)/'job';shutil.copytree(os.environ['BOOK_REAL_CACHE_JOB'],self.job,ignore=shutil.ignore_patterns('packages'));self.out=Path(os.environ['BOOK_REAL_CACHE_JOB']).parent
  self.allowance=read(self.out/'sample-allowance.json');self.glossary=read(self.out/'sample-glossary.json');self.old=read(self.job/'translations.json');source=read(self.job/'source.json');self.bid=source['chapters'][-1]['blocks'][-1]['id'];self.key=self.old['blocks'][self.bid]['request_key']
  self.net=patch('urllib.request.urlopen',side_effect=AssertionError('PROVIDER NETWORK FORBIDDEN'));self.network=self.net.start();self.env=patch.dict(os.environ,{'DEEPSEEK_API_KEY':'OFFLINE-PROBE-NOT-A-KEY'});self.env.start()
 def tearDown(self):self.env.stop();self.net.stop();self.t.cleanup()
 def remove_result_and_checkpoint(self):
  tr=read(self.job/'translations.json');tr['blocks'].pop(self.bid);write(self.job/'translations.json',tr)
  p=self.job/'translation-checkpoints'/self.key
  if p.exists():p.rename(self.job/'saved-old-checkpoint')
 def test_crash_after_provider_response_recovers_without_resubmit(self):
  self.remove_result_and_checkpoint();before=(self.job/'request-ledger.json').read_bytes()
  r=translate_job(self.job,'deepseek','deepseek-flash',self.allowance,self.glossary)
  self.assertEqual(r,self.old);self.assertEqual((self.job/'request-ledger.json').read_bytes(),before);self.network.assert_not_called()
 def test_submitted_uncertain_and_reconcile_cache_never_resubmit(self):
  self.remove_result_and_checkpoint()
  for state in ['submitted','uncertain','reconciliation_required']:
   ledger=read(self.job/'request-ledger.json');ledger['requests'][self.key]['state']=state;write(self.job/'request-ledger.json',ledger);before=(self.job/'request-ledger.json').read_bytes()
   with self.assertRaises(Exception):translate_job(self.job,'deepseek','deepseek-flash',self.allowance,self.glossary)
   self.assertEqual((self.job/'request-ledger.json').read_bytes(),before)
  self.network.assert_not_called()
 def test_cancel_stops_before_client(self):
  write(self.job/'cancel',{'fixture':True})
  with patch('book_pipeline.OpenAICompatibleTranslationClient') as client:
   with self.assertRaises(RuntimeError):translate_job(self.job,'deepseek','deepseek-flash',self.allowance,self.glossary)
   client.assert_not_called()
  self.network.assert_not_called()
 def test_tampered_cached_target_is_not_silently_reused(self):
  tr=read(self.job/'translations.json');tr['blocks'][self.bid]['text']='changed text without updating its recorded hash';write(self.job/'translations.json',tr)
  with self.assertRaises(ValueError):translate_job(self.job,'deepseek','deepseek-flash',self.allowance,self.glossary)
  self.network.assert_not_called()
if __name__=='__main__':unittest.main()
