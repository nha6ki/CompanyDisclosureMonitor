from contextlib import closing
from copy import deepcopy
import csv
from datetime import date
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import demo_history
import pipeline
import quarter_evidence
from test_companies import route


class QuarterWorkflowTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.reg = SimpleNamespace(root=self.root,token='token')
        self.db = pipeline.connect(self.root)
        self.addCleanup(self.db.close)
        self.row = next(r for r in pipeline.records(self.db) if r['symbol']=='NVDA')

    def test_seed_and_filtered_export(self):
        self.assertEqual(len(list(pipeline.records(self.db))),56)
        for status in ('照合待ち','訂正・重複','比較値なし','一致'):
            query = urlencode({'status':status})
            code,_,body = route(self.reg,'/workflow.csv',query)
            self.assertEqual(code,'200 OK')
            lines = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
            self.assertTrue(lines)
            self.assertTrue(all(row['照合結果']==status for row in lines))
        code,_,body = route(self.reg,'/workflow.csv','symbol=GOOGL&start=2026-09-30&end=2026-09-30')
        self.assertEqual(code,'200 OK')
        lines=list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
        self.assertEqual([row['決算発表値'] for row in lines],['127577','2.12'])
        self.assertEqual({row['定期報告提出日'] for row in lines},{'2026-10-29'})
        self.assertEqual(route(self.reg,'/workflow','start=2026-01-01&end=2025-01-01')[0],'400 Bad Request')

    def test_review_retries_conflict_and_reopening(self):
        r = self.row
        form = dict(token='token',symbol=r['symbol'],period=r['period'],version=r['version'],previous=0,
                    state='確認済み',reviewer='Reviewer',note='=reviewed <OK>')
        for _ in range(2):
            self.assertTrue(route(self.reg,'/workflow-review',form=form)[0].startswith('303'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],1)
        self.assertEqual(route(self.reg,'/workflow-review',form=dict(form,note='stale'))[0],'400 Bad Request')
        query = urlencode({'symbol':'NVDA','state':'確認済み'})
        self.assertIn(b'&lt;OK&gt;',route(self.reg,'/workflow',query)[2])
        self.assertIn(b"'=reviewed <OK>",route(self.reg,'/workflow.csv',query)[2])
        with self.db:
            stored=json.loads(self.db.execute('SELECT data FROM quarters WHERE symbol=? AND period=?',
                                              (r['symbol'],r['period'])).fetchone()[0])
            changed=deepcopy(stored)
            changed['report_date']='2026-11-20'
            pipeline.save(self.db,'NVDA','2026-11-20',[changed],[])
        self.assertEqual(next(x for x in pipeline.records(self.db) if x['symbol']=='NVDA')['state'],'再確認')
        self.assertEqual(route(self.reg,'/workflow-review',form=form)[0],'400 Bad Request')

    def test_runtime_history_does_not_publish_local_notes(self):
        pipeline.review(self.db,dict(self.row,state='確認済み',reviewer='private-reviewer',note='private-note'))
        code,_,body=route(self.reg,'/company/NVDA')
        self.assertEqual(code,'200 OK')
        self.assertNotIn(b'private-note',body)
        self.assertNotIn(b'private-reviewer',body)
        data=json.loads(body.decode().split(demo_history.MARKER)[1].split('</script>')[0])
        self.assertEqual(data['NVDA'][-1]['state'],'確認済み')
        seed=json.loads(pipeline.SEED.read_text())['issuers']
        expected=demo_history.presentation(r for v in seed.values() for r in v['rows'])
        embedded=json.loads(demo_history.TEMPLATE.read_text().split(demo_history.MARKER)[1].split('</script>')[0])
        self.assertEqual(embedded,expected)

    def test_offline_issuer_does_not_erase_good_data(self):
        before=list(pipeline.records(self.db))
        with patch.object(pipeline,'scan',side_effect=TimeoutError('timeout')):
            result=pipeline.sync(self.root,lambda _:b'',date(2026,11,20),['NVDA'])
        self.assertIn('error',result['NVDA'])
        self.assertEqual(list(pipeline.records(self.db)),before)
        self.assertEqual(tuple(self.db.execute("SELECT asof,attempted,error FROM syncs WHERE symbol='NVDA'").fetchone()),
                         ('2026-11-19','2026-11-20','timeout'))

    def test_periodic_context_units_and_ambiguity(self):
        raw='''<html xmlns:ix="ix" xmlns:x="x"><x:context id="q"><x:entity><x:identifier>1652044</x:identifier></x:entity>
        <x:period><x:startDate>2026-07-01</x:startDate><x:endDate>2026-09-30</x:endDate></x:period></x:context>
        <x:unit id="u"><x:measure>iso4217:USD</x:measure></x:unit>
        <ix:nonFraction name="us-gaap:Revenues" contextRef="q" unitRef="u" scale="3">12000</ix:nonFraction></html>'''
        def check(data,sym='GOOGL'):
            return quarter_evidence.periodic(data.encode(),sym,date(2026,9,30))[0]
        self.assertEqual(check(raw),['12.000',None])
        for bad in [raw.replace('2026-07-01','2026-01-01'),raw.replace('</x:entity>','<x:segment/></x:entity>'),
                    raw.replace('iso4217:USD','iso4217:EUR'),raw.replace('1652044','789019'),
                    raw.replace('</html>',raw[raw.index('<ix:nonFraction'):].replace('12000','11000'))]:
            self.assertEqual(check(bad),[None,None])

    def test_later_filing_updates_pending_row_without_duplicate(self):
        entries = [
            ('8-K','2026-10-29','0001018724-26-000038','release.htm','2.02,9.01','2026-10-29'),
            ('10-Q','2026-10-30','0001018724-26-000039','report.htm','','2026-09-30')]
        fields = ['form','filingDate','accessionNumber','primaryDocument','items','reportDate']
        feed = {'cik':1018724,'filings':{'recent':dict(zip(fields,map(list,zip(*entries)))),'files':[]}}
        release = b"""<html>Amazon.com, Inc. Consolidated Statements of Operations (in millions, except per share data) (unaudited)
            <table><tr><td></td><td colspan="2">Three Months Ended September 30,</td></tr>
            <tr><td></td><td>2025</td><td>2026</td></tr>
            <tr><td>Total net sales</td><td>$29</td><td>$31</td></tr>
            <tr><td>Diluted earnings per share</td><td>$1</td><td>$2</td></tr></table></html>"""
        report = b"""<html xmlns:x="x" xmlns:ix="ix"><x:context id="q"><x:entity><x:identifier>1018724</x:identifier></x:entity>
            <x:period><x:startDate>2026-07-01</x:startDate><x:endDate>2026-09-30</x:endDate></x:period></x:context>
            <x:unit id="u"><x:measure>iso4217:USD</x:measure></x:unit><x:unit id="e"><x:divide>
            <x:unitNumerator><x:measure>iso4217:USD</x:measure></x:unitNumerator>
            <x:unitDenominator><x:measure>xbrli:shares</x:measure></x:unitDenominator></x:divide></x:unit>
            <ix:nonFraction name="us-gaap:Revenues" contextRef="q" unitRef="u" scale="6">31</ix:nonFraction>
            <ix:nonFraction name="us-gaap:EarningsPerShareDiluted" contextRef="q" unitRef="e">2</ix:nonFraction></html>"""
        docs={'release.htm':b'<html><table><tr><td>99.1</td><td><a href="ex99.htm">Release</a></td></tr></table></html>',
              'ex99.htm':release,'report.htm':report}
        calls=[]
        def get(url):
            calls.append(url)
            return json.dumps(feed).encode() if url.endswith('.json') else docs[url.rsplit('/',1)[1]]
        with TemporaryDirectory() as fresh,closing(pipeline.connect(Path(fresh),seed=False)) as db:
            versions = []
            for day,expected in [(29,'照合待ち'),(30,'一致'),(30,'一致')]:
                rows,issues = pipeline.scan('AMZN',get,date(2026,10,day))
                with db:
                    pipeline.save(db,'AMZN',f'2026-10-{day}',rows,issues)
                row=next(r for r in pipeline.records(db) if r['period']=='FY2026Q3')
                self.assertEqual([m['status'] for m in row['metrics']],[expected]*2)
                self.assertEqual(len(list(pipeline.records(db))),8)
                versions.append(row['version'])
                if day==29:
                    self.assertFalse(any(u.endswith('report.htm') for u in calls))
            self.assertNotEqual(versions[0],versions[1])
            self.assertEqual(versions[1],versions[2])

    def test_new_quarter_keeps_only_eight_visible_and_preserves_history(self):
        r=json.loads(self.db.execute("SELECT data FROM quarters WHERE symbol='NVDA' ORDER BY period DESC LIMIT 1").fetchone()[0])
        with self.db:
            pipeline.save(self.db,'NVDA','2027-02-20',[dict(r,period='FY2027Q4')],[])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quarters WHERE symbol='NVDA'").fetchone()[0],9)
        self.assertEqual(len([r for r in pipeline.records(self.db) if r['symbol']=='NVDA']),8)
