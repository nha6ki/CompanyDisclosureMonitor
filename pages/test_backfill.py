from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backfill
import daily
import daily_highlights as highlights
import manage


class BackfillChecks(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        pages = root/'pages'
        shutil.copytree(manage.ROOT, pages, ignore=shutil.ignore_patterns('*.lock', '__pycache__'))
        self.private = root/'register_data/pages_daily'
        for target, props in ((daily, {'PAGES':pages, 'PRIVATE':self.private}),
                              (manage, {'ROOT':pages, 'DATA':pages/'data', 'OUTPUT':root/'_site'})):
            stack.enter_context(patch.multiple(target, **props))
        stack.enter_context(redirect_stdout(io.StringIO()))

    def test_quarter_only_and_new_daily_filings_are_included(self):
        manage.build()
        rows = highlights.ledger_records((manage.OUTPUT/'index.html').read_text(), manage.inputs()[0])
        acc = '0000320193-25-000007'
        previous_size = len(rows)
        self.assertIn('AAPL/'+acc, rows)
        self.assertEqual({r['ticker'] for r in rows.values()}, manage.SYMBOLS)
        recent = '0000320193-26-123456'
        daily.save(manage.DATA/'filings.json', {'AAPL':{recent:dict(date='2026-11-19', form='8-K/A',
            url='https://www.sec.gov/Archives/edgar/data/320193/000032019326123456/test.htm',
            items=['8.01'], sections={})}})
        self.assertEqual(backfill.run(None, prepare=True), 0)
        queue = manage.load(self.private/'queue.json')
        self.assertEqual(len(queue), previous_size+1)
        self.assertEqual(queue['AAPL/'+recent]['record']['form'], '8-K/A')
        # Preserve the daily task's richer Item list and completed work.
        queue['AAPL/'+acc]['record']['api_items'] = ['2.02', '9.01']
        queue['AAPL/'+acc]['state'] = 'attachment'
        daily.save(self.private/'queue.json', queue)
        with patch.object(highlights, 'model_ready', side_effect=OSError('offline')):
            self.assertEqual(backfill.run('test@example.invalid'), 1)
        report = manage.load(self.private/'review/backfill-results.json')
        self.assertEqual(report['attachment_only'], 1)
        self.assertEqual(report['outstanding'], previous_size)
        self.assertIn('offline', report['errors']['Qwen'])
        self.assertFalse((manage.OUTPUT/'backfill-results.json').exists())

    def test_only_local_narrative_routes_expose_private_detail(self):
        backfill.run(None, prepare=True)
        handler = object.__new__(manage.PreviewHandler)
        handler.directory = str(manage.OUTPUT)
        self.assertEqual(Path(handler.translate_path('/?lang=ja&company=AAPL&tab=events')),
                         self.private/'review/detail.html')
        self.assertEqual(Path(handler.translate_path('/local-review/backfill-results.json')),
                         self.private/'review/backfill-results.json')
        self.assertEqual(Path(handler.translate_path('/?lang=ja&company=AAPL')), manage.OUTPUT)
