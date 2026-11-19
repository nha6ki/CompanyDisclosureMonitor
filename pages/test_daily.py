"""Local-only regressions for release changes, independent retries and human approval."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import date
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import daily
import daily_highlights as hl
import llm_runner
import manage
import test_structured_events


class DailyUpdates(unittest.TestCase):
    def setUp(self):
        stack=ExitStack()
        self.addCleanup(stack.close)
        top=Path(stack.enter_context(tempfile.TemporaryDirectory()))
        pages=top/'pages'
        shutil.copytree(manage.ROOT,pages,ignore=shutil.ignore_patterns('*.lock','__pycache__'))
        self.folder=top/'register_data/pages_daily'
        self.folder.mkdir(parents=True)
        for obj,key,value in [(manage,'ROOT',pages),(manage,'DATA',pages/'data'),(manage,'OUTPUT',top/'_site'),
                              (daily,'PAGES',pages),(daily,'PRIVATE',self.folder)]:
            stack.enter_context(patch.object(obj,key,value))
        stack.enter_context(redirect_stdout(io.StringIO()))
        self.day=date(2026,11,20)
        self.fixture=test_structured_events.StructuredEventChecks()
        self.fixture.setUp()
        acc=self.fixture.acc
        self.row=dict(ticker='AAPL',accession_number=acc,filing_date='2026-03-03',form='8-K',
                      document_url='https://www.sec.gov/Archives/edgar/data/320193/000000000126000001/test.htm',api_items=['5.02'])
        self.raw=('<html><body><p>Item 5.02 Departure of Directors or Certain Officers</p><p>'+self.fixture.block+'</p></body></html>').encode()
        self.calls=0

    def extract(self,job,bad=False):
        def send(config,body):
            self.calls+=1
            doc=json.loads(body['messages'][1]['content'])['documents'][0]
            self.assertEqual(doc['issuer'],'Apple')
            candidate=deepcopy(self.fixture.row)
            for key in ('accession_number','source_sha256','section_sha256','section_id','as_of_date'):
                candidate[key]=doc[key]
            num=next(b['block_number'] for b in doc['blocks'] if 'Alex Sample' in b['text'])
            result=json.dumps({'schema_version':llm_runner.events.VERSION,'filings':[candidate]},ensure_ascii=False)
            result=result.replace('[41]','['+str(num)+']').replace('任命を決定。','テスト専用の候補。')
            if bad:result=result.replace('Alex Sample','unsubstantiated name')
            return {'choices':[{'finish_reason':'stop','message':{'content':result}}]}
        hl.extract(self.folder,job,lambda _:self.raw,hl.local_config(),runner=lambda *a,**k:llm_runner.run(*a,**k,sender=send))

    def test_private_candidate_and_explicit_approval(self):
        job={'record':self.row}
        self.extract(job)
        self.assertEqual(job['state'],'done')
        self.extract(job)
        self.assertEqual(self.calls,1)
        key='AAPL/'+self.row['accession_number']
        queue={key:job}
        daily.save(self.folder/'queue.json',queue)
        manage.build()
        hl.index_page(self.folder,queue,{'checked_at':'test','errors':{}})
        actual=(manage.OUTPUT/'index.html').read_text()
        self.assertNotIn('テスト専用の候補。',actual)
        self.assertIn('テスト専用の候補。',(self.folder/'review/detail.html').read_text())
        sid,section=next(iter(job['candidate']['sections'].items()))
        with self.assertRaises(ValueError): daily.approve('AAPL',self.fixture.acc,sid,'old-hash')
        daily.approve('AAPL',self.fixture.acc,sid,manage.digest(section))
        self.assertIn('テスト専用の候補。',(manage.OUTPUT/'index.html').read_text())

    def test_invalid_candidate_and_model_offline(self):
        job={'record':self.row}
        self.extract(job,bad=True)
        self.assertEqual(job['state'],'manual')
        self.assertFalse(job['candidate']['sections'])
        summary={'errors':{}}
        job['state']='retry'
        with patch.object(hl,'model_ready',side_effect=OSError('offline')):
            hl.process_queue(self.folder,{'x':job},None,False,lambda:None,summary)
        self.assertIn('Qwen',summary['errors'])
        self.assertEqual(job['state'],'retry')

    def test_change_only_one_issuer_and_recheck(self):
        history,dates=manage.inputs()
        reviews=(manage.DATA/'reviews.json').read_bytes()
        before={p.name:p.read_bytes() for p in (manage.ROOT/'reports').glob('*.xlsx')}
        rows=history['AAPL']
        rows[-1]['quarterlySha']='f'*64
        self.assertEqual(daily.apply_updates(self.folder,{'AAPL':rows},self.day,{}),['AAPL'])
        manage.build()
        _,latest=manage.decisions()
        self.assertEqual(manage.official(rows[-1],latest['AAPL',rows[-1]['period']]),'再確認')
        self.assertEqual((manage.DATA/'reviews.json').read_bytes(),reviews)
        changed={p.name for p in (manage.ROOT/'reports').glob('*.xlsx') if p.read_bytes()!=before[p.name]}
        self.assertEqual(changed,{'AAPL-en.xlsx','AAPL-ja.xlsx'})
        self.assertEqual(daily.apply_updates(self.folder,{'AAPL':rows},self.day,{}),[])

    def test_amendment_boundary_and_late_index(self):
        amended=dict(self.row,form='8-K/A',filing_date='2026-11-19',accession_number='0000000001-26-000002')
        late=dict(self.row,filing_date='2026-11-17',accession_number='0000000001-26-000003')
        last={};q={};pub={}
        with patch.object(daily.companies,'collect',return_value={'rows':[amended,self.row]}):
            daily.collect_filing_jobs('AAPL',self.day,None,last,'2026-11-19',q,pub)
        self.assertIn('AAPL/'+amended['accession_number'],q)
        with patch.object(daily.companies,'collect',return_value={'rows':[amended,late,self.row]}):
            daily.collect_filing_jobs('AAPL',self.day,None,last,'2026-11-19',q,pub)
        self.assertIn('AAPL/'+late['accession_number'],q)

    def test_interrupted_update_is_replayed(self):
        prepared=self.folder/'prepared/data'
        prepared.mkdir(parents=True)
        (prepared/'filings.json').write_text('{}')
        daily.recover(self.folder)
        self.assertEqual((manage.DATA/'filings.json').read_text(),'{}')
        daily.recover(self.folder)
