from html.parser import HTMLParser
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from wsgiref.util import setup_testing_defaults

from app import application
import microsoft


def table(revenue="92,279", eps="$4.92", heading="INCOME STATEMENTS", end="September 30"):
    # Prior-year figures intentionally precede the current year.
    rows = [
        [f'Three Months Ended {end},', '2025', '2026'],
        ['Product', '$1', '$2'], ['Total revenue', '77,673', revenue],
        ['Earnings per share:'], ['Basic', '$3.73', '$4.93'], ['Diluted', '$3.72', eps],
        ['Weighted average shares outstanding:'], ['Diluted', '7,466', '7,446']
    ]
    body = ''.join('<tr>' + ''.join('<td>'+str(cell)+'</td>' for cell in r) + '</tr>' for r in rows)
    return (heading+' (In millions, except per share amounts) (Unaudited)<table>'+body+'</table>').encode()


def primary():
    return b'<table><tr><td>99.1</td><td><a href="msft-ex99_1.htm">Press release</a></td></tr></table>'


class ScriptData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.text = ''

    def handle_starttag(self, tag, attrs):
        self.active = tag == 'script' and dict(attrs).get('id') == 'msft-record'

    def handle_endtag(self, tag):
        self.active = False

    def handle_data(self, value):
        if self.active:
            self.text += value


class MicrosoftTest(unittest.TestCase):
    def test_uses_year_columns_and_scopes_diluted_to_eps(self):
        self.assertEqual(microsoft.read_values(table()), [('92279',''), ('4.92','')])

    def test_period_ambiguity_unit_and_missing_metric(self):
        raw = table()
        for invalid in [raw+raw, table(end='June 30'), raw.replace(b'2026',b'2024'),
                        raw.replace(b'In millions',b'In thousands')]:
            self.assertTrue(all(value is None for value, _ in microsoft.read_values(invalid)))
        values = microsoft.read_values(raw.replace(b'Total revenue',b'Segment revenue'))
        self.assertIsNone(values[0][0])
        self.assertEqual(values[1], ('4.92',''))

    def test_validates_exhibit_and_compares_independently(self):
        with self.assertRaises(ValueError):
            microsoft.exhibit_link(primary().replace(b'msft-ex99_1.htm', b'../foreign.htm'))
        documents = {microsoft.PRIMARY: primary(), microsoft.QUARTERLY: table(revenue='92,280',eps='')}
        record = microsoft.compare(lambda url: documents.get(url, table()))
        self.assertEqual([f['status'] for f in record['facts']], ['不一致','要確認'])
        self.assertEqual(record['facts'][1]['value'], '4.92')
        self.assertIsNone(record['facts'][1]['detail'])
        self.assertNotEqual(record['sha'], record['quarterlySha'])

    def test_shared_routes_bundle_and_local_result(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            reg = SimpleNamespace(root=root)
            def get(path):
                env = {}
                setup_testing_defaults(env)
                env.update(PATH_INFO=path, HTTP_HOST='localhost:8774', QUERY_STRING='', **{'wsgi.input': io.BytesIO()})
                status = []
                page = b''.join(application(reg,8774)(env,lambda s,h: status.append(s))).decode()
                self.assertEqual(status, ['200 OK'])
                return page
            pages = [get(path) for path in ('/demo','/company/NVDA','/company/MSFT')]
            for page in pages:
                data = ScriptData()
                data.feed(page)
                record = json.loads(data.text)
                self.assertEqual(record['period'], 'FY2027Q1')
                self.assertEqual([f['value'] for f in record['facts']], ['92279','4.92'])
            record['facts'][0]['reason'] = '</script><p>fixture</p>'
            file = microsoft.local_result(root)
            file.parent.mkdir(parents=True)
            file.write_text(json.dumps(record))
            rendered = get('/company/MSFT')
            self.assertNotIn('</script><p>fixture', rendered)
            data = ScriptData()
            data.feed(rendered)
            self.assertEqual(json.loads(data.text), record)
