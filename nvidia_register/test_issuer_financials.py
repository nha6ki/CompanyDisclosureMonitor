import json
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import companies
import issuer_financials as issuers
from filing_pair import METRICS, read_values
from test_companies import route


class IssuerFinancialsTest(unittest.TestCase):
    def test_current_quarter_under_its_own_group(self):
        raw = b"""Amazon.com, Inc. Consolidated Statements of Operations (in millions, except per share data) (unaudited)
        <table><tr><td></td><td colspan="2">Nine Months Ended September 30,</td>
        <td colspan="2">Three Months Ended September 30,</td></tr>
        <tr><td></td><td>2025</td><td>2026</td><td>2026</td><td>2025</td></tr>
        <tr><td>Total net sales</td><td>$80</td><td>$90</td><td>$31</td><td>$29</td></tr>
        <tr><td>Diluted earnings per share</td><td>$7</td><td>$9</td><td>$2</td><td>$1</td></tr></table>"""
        table = partial(issuers.statement_table, symbol="AMZN")
        money = partial(issuers.value_from_grid, symbol="AMZN")
        self.assertEqual(read_values(raw, table, money), [('31', ''), ('2', '')])
        for bad in [raw.replace(b'Three Months Ended', b'Nine Months Ended'),
                    raw.replace(b'in millions', b'in thousands'), raw + raw]:
            self.assertTrue(all(value is None for value, _ in read_values(bad, table, money)))

    def test_apple_footnotes_and_share_counts_are_excluded(self):
        names = [('Net sales:', ''),('Total net sales (1)', '124'),('Cost of sales:', ''),
                 ('Earnings per share:', ''),('Basic', '$5'),('Diluted', '$4'),
                 ('Shares used in computing earnings per share:', ''),('Diluted','5000'),('Total net sales','999')]
        rows = [[(0,1,label),(1,2,val)] for label,val in names]
        self.assertEqual([issuers.value_from_grid(rows,(1,2),name,'AAPL') for name in METRICS], ['124','4'])

    def test_tesla_slide_matches_period_not_column_position(self):
        raw = b"""<font>In millions of USD or shares as applicable, except per share data
        Q3-2026 Q3-2025 REVENUES Automotive sales 5 7 Total revenues 12 15 COST OF REVENUES
        Net income per share of common stock attributable to common stockholders Basic $ 3 $ 2
        Diluted $ 1.30 $ 1.23 Weighted average shares used in computing net income per share of common stock
        Basic 10 11 Diluted 11 12 S T A T E M E N T O F O P E R A T I O N S (Unaudited) 27</font>"""
        def values(doc):
            return [value for value, _ in read_values(doc, issuers.tesla_slide, issuers.value_from_slide)]
        self.assertEqual(values(raw), ['12','1.30'])
        self.assertEqual(values(raw.replace(b'$ 1.30 $ 1.23', b'$ 1.23')), ['12',None])
        self.assertEqual(values(raw.replace(b'Q3-2026', b'Q2-2026')), [None,None])

    def test_company_index_links_and_override_isolation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            reg = SimpleNamespace(root=root,token='token')
            _, _, index = route(reg, '/companies')
            for ticker in companies.ISSUERS:
                self.assertIn(('/company/'+ticker+'?lang=ja').encode(), index)
            for ticker, pair in issuers.PAIRS.items():
                path = pair.local_result(root)
                path.parent.mkdir(exist_ok=True,parents=True)
                path.write_text(json.dumps({'marker':ticker+'-local'}))
            for ticker in companies.ISSUERS:
                status, _, html = route(reg,'/company/'+ticker)
                self.assertEqual(status,'200 OK')
                for other in issuers.PAIRS:
                    self.assertEqual((other+'-local').encode() in html, other == ticker)
