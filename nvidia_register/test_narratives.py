import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import narratives


class NarrativeLedgerTests(unittest.TestCase):
    def test_release_omits_pending_payload_but_preserves_zero_candidate_status(self):
        source = dict(id='section-01', item='5.02', state='pending', count=0, model=narratives.MODEL,
                      sourceHash='abc', sectionHash='def', events=[], blocks=[], missing=['private note'])
        filing = {'sections': {'section-01': source}}
        template = narratives.MARKER + '{}</script>'
        def section(private):
            html = narratives.embed(template, {'NVDA': {'example': filing}}, include_pending=private)
            data = json.loads(html[len(narratives.MARKER):-len('</script>')])
            return data['NVDA']['example']['sections']['section-01']
        self.assertNotIn('events', section(False))
        self.assertEqual(section(False)['count'], 0)
        self.assertEqual(section(True)['events'], [])
        self.assertNotIn('missing', section(False))
        source['state'] = 'reviewed'
        self.assertEqual(section(False)['events'], [])

    def test_saved_coverage_is_issuer_scoped(self):
        with TemporaryDirectory() as tmp:
            filings = narratives.collect(Path(tmp))
        self.assertEqual({symbol for symbol,batch in filings.items()
                          if any(f['sections'] for f in batch.values())}, {'NVDA'})
        results = {a: sum(s['count'] for s in f['sections'].values())
                   for a,f in filings['NVDA'].items() if f['sections']}
        self.assertEqual(sorted(results.values()), [1, 1, 2])

    def test_source_or_paragraph_mismatch_is_rejected(self):
        ident = dict(accession_number='a', section_id='s', source_sha256='1',
                     section_sha256='2', as_of_date='2026-04-27')
        field = {'block_numbers': [9], 'value': 'v'}
        event = dict.fromkeys(('subject','event_type','summary_ja','organization','status_as_of_filing'),field)
        event.update(dates=[], conditions=[], notes_ja=None)
        filing = dict(ident, events=[event], missing_information=[])
        source = dict(ident, item='5.02', blocks=[{'block_number':8, 'text':'first'}])
        with self.assertRaises(ValueError):
            narratives.section(filing, source, 'pending')
        source['blocks'].append({'block_number':9, 'text':'second'})
        self.assertEqual(narratives.section(filing, source, 'pending')['blocks'],source['blocks'][1:])
        with self.assertRaises(ValueError):
            narratives.section(filing, dict(source, section_sha256='new'), 'pending')
