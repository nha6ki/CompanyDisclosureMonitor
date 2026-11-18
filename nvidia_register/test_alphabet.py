import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace

import alphabet
import microsoft
from test_companies import route


def statement(*, split, revenue="111", eps="1.23", year="2026"):
    # Place the nine-month group first so an absolute column index would fail.
    if split:
        headers = ('<tr><td></td><td colspan="2">Nine Months Ended</td>'
                   '<td colspan="2">Three Months Ended</td></tr>'
                   '<tr><td></td><td colspan="2">September 30,</td><td colspan="2">September 30,</td></tr>')
    else:
        headers = ('<tr><td></td><td colspan="2">Year To Date September 30,</td>'
                   '<td colspan="2">Quarter Ended September 30,</td></tr>')
    return (f'Alphabet Inc. CONSOLIDATED STATEMENTS OF INCOME (In millions, except per share amounts, unaudited)'
            f'<table>{headers}<tr><td></td><td>2025</td><td>2026</td><td>{year}</td><td>2025</td></tr>'
            f'<tr><td>Revenues</td><td>$900</td><td>$990</td><td>${revenue}</td><td>$883</td></tr>'
            f'<tr><td>Basic net income per common share</td><td>$8</td><td>$9</td><td>$8</td><td>$8</td></tr>'
            f'<tr><td>Diluted net income per common share (Note 12)</td><td>$8</td><td>$9</td>'
            f'<td>${eps}</td><td>$8</td></tr></table>').encode()


class AlphabetTest(unittest.TestCase):
    def test_selects_quarter_and_current_year_in_both_layouts(self):
        for split in (False, True):
            with self.subTest(split=split):
                self.assertEqual(alphabet.read_values(statement(split=split)), [('111', ''), ('1.23', '')])

    def test_incorrect_period_or_units_require_review(self):
        raw = statement(split=True)
        for bad in [raw.replace(b'September 30,', b'June 30,'), raw.replace(b'In millions', b'In billions'),
                    statement(split=True, year='2025')]:
            self.assertTrue(all(value is None for value, _ in alphabet.read_values(bad)))

    def test_compares_sources_and_keeps_an_unreadable_value_missing(self):
        primary = b'<table><tr><td>99.1</td><td><a href="result.htm">Release</a></td></tr></table>'
        docs = {alphabet.PRIMARY:primary, alphabet.QUARTERLY:statement(split=True, revenue="112",eps="")}
        record = alphabet.compare(lambda url: docs.get(url,statement(split=False)))
        self.assertEqual([f['status'] for f in record['facts']], ['不一致', '要確認'])
        self.assertIsNone(record['facts'][1]['detail'])
        self.assertEqual(record['facts'][1]['value'], '1.23')
        self.assertNotEqual(record['sha'], record['quarterlySha'])

    def test_local_results_are_isolated_between_issuers(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            reg = SimpleNamespace(root=root)
            local = alphabet.PAIR.local_result(root)
            local.parent.mkdir(parents=True)
            local.write_text(json.dumps({'marker':'local-alphabet-proof'}))
            self.assertNotEqual(local, microsoft.PAIR.local_result(root))
            for path,query,expected in [('/company/GOOGL','',True),('/demo','company=GOOGL',True),
                                        ('/company/MSFT','',False),('/company/NVDA','',False)]:
                status, _, page = route(reg,path,query)
                self.assertEqual(status, '200 OK')
                self.assertEqual(b'local-alphabet-proof' in page, expected)
