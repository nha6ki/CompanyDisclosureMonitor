"""Synthetic cases exercise extraction and failure handling, not source accuracy."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import extract_evidence as mod


def document(item, title, *paragraphs):
    return ('<html><body><h2>Item ' + item + '. ' + title + '</h2>' +
            ''.join('<p>' + p + '</p>' for p in paragraphs) + '</body></html>').encode()


class ExtractionTests(unittest.TestCase):
    def test_new_names_and_dates_are_not_triggers(self):
        raw = document('8.01', 'Other Events.',
                       'On January 8, 2030, Example Ltd entered into a definitive agreement to acquire Test Systems.',
                       'The closing is subject to the satisfaction of regulatory approval conditions.')
        result = mod.extract(raw)
        self.assertEqual([c['rule_id'] for c in result['candidates']],
                         ['acquisition_agreement', 'completion_condition'])

    def test_quotes_reconstruct_from_recorded_offsets(self):
        raw = document('5.02', 'Departure of Directors or Certain Officers.',
                       'On March 3, 2031, the Board accepted the resignation of Morgan Lee as President.',
                       'The Board appointed Casey Hill to serve as interim Chief Executive Officer until a successor is selected.')
        blocks = mod.visible_blocks(raw)
        result = mod.extract(raw)
        self.assertEqual(result['candidate_count'], 2)
        for c in result['candidates']:
            text = blocks[c['block_number'] - 1]
            self.assertEqual(c['quote'], text[c['char_start']:c['char_end']])
            self.assertLessEqual(len(c['quote'].split()), 12)
            self.assertEqual(c['paragraph_sha256'], hashlib.sha256(text.encode()).hexdigest())

    def test_hidden_content_is_excluded(self):
        raw = b'''<html><body><h2>Item 5.02. Departure of Directors or Certain Officers.</h2>
        <div style="display: none">The Board accepted the resignation of A.</div>
        <ix:hidden>The Board accepted the resignation of B.</ix:hidden>
        <script>The Board accepted the resignation of C.</script><p>No relevant personnel announcement.</p></body></html>'''
        self.assertEqual(mod.extract(raw)['status'], 'no_candidate')

    def test_table_split_heading_and_inline_markup(self):
        raw = b'''<table><tr><td>Item 8.01.</td><td>Other Events.</td></tr></table>
        <p>Example entered into a <b>definitive agreement</b> to acquire Sample Corp.</p>'''
        result = mod.extract(raw)
        self.assertEqual(result['text_items'], ['8.01'])
        self.assertEqual(result['candidates'][0]['rule_id'], 'acquisition_agreement')

    def test_negation_and_hypothetical_are_not_positive_candidates(self):
        for text in ['The Board has not accepted the resignation of Alex.',
                     'If the Board accepted the resignation of Alex, another vote would be needed.',
                     'A rumor states that the Board accepted the resignation of Alex.']:
            with self.subTest(text=text):
                self.assertEqual(mod.extract(document('5.02', 'Departure of Directors.', text))['status'], 'no_candidate')

    def test_forward_looking_and_risk_sections_are_excluded(self):
        for heading in ['Forward-Looking Statements', 'Risks Related to the Transaction']:
            raw = document('8.01', 'Other Events.', 'General introductory text.', heading,
                           'Example entered into a definitive agreement to acquire Sample.')
            self.assertEqual(mod.extract(raw)['status'], 'no_candidate')

    def test_new_item_resets_forward_looking_exclusion(self):
        raw = (document('8.01', 'Other Events.', 'Forward-Looking Statements') +
               document('5.02', 'Departure of Directors.', 'The Board accepted the resignation of Alex.'))
        self.assertEqual(mod.extract(raw)['candidates'][0]['item'], '5.02')

    def test_wrong_item_and_unknown_heading_do_not_inherit_prior_section(self):
        for item, title in [('9.01', 'Financial Statements and Exhibits.'), ('6.99', 'Unrecognized Heading.')]:
            raw = (document('5.02', 'Departure of Directors.', 'Unrelated introductory text.') +
                   document(item, title, 'The Board accepted the resignation of Alex.'))
            self.assertEqual(mod.extract(raw)['status'], 'no_candidate')

    def test_no_rule_match_is_explicit(self):
        result = mod.extract(document('8.01', 'Other Events.', 'The company published its annual sustainability report.'))
        self.assertEqual(result['status'], 'no_candidate')
        self.assertEqual(result['candidate_count'], 0)

    def test_one_candidate_per_block_and_limit(self):
        raw = document('5.02', 'Departure of Directors.',
                       'The Board accepted the resignation of Alex and appointed Casey to serve as interim Chief Executive Officer.',
                       'Jordan notified the Company of an intention to retire next year.',
                       'The Company appointed Sam effective upon the commencement of employment.')
        result = mod.extract(raw)
        self.assertEqual(result['candidate_count'], 2)
        self.assertEqual(len({c['block_number'] for c in result['candidates']}), 2)

    def test_payment_cap_quote_keeps_amount(self):
        result = mod.extract(document('1.01', 'Entry into a Material Definitive Agreement.',
                                      'Example’s aggregate payment obligation is cumulatively capped at $7 billion for the initial commitment.'))
        self.assertEqual(result['candidates'][0]['rule_id'], 'payment_cap')
        self.assertIn('$7 billion', result['candidates'][0]['quote'])


class SourceTests(unittest.TestCase):
    def test_missing_and_changed_source_never_emit_candidates(self):
        row = {'accession_number': '0000000001-30-000001', 'sha256': '0' * 64, 'api_items': ['5.02']}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(mod.inspect_file(row, root)['status'], 'source_missing')
            target = root / row['accession_number'] / 'primary.html'
            target.parent.mkdir()
            target.write_bytes(document('5.02', 'Departure of Directors.', 'The Board accepted the resignation of Alex.'))
            result = mod.inspect_file(row, root)
            self.assertEqual(result['status'], 'source_changed')
            self.assertEqual(result['candidates'], [])

    def test_exact_source_hash_and_item_difference(self):
        raw = (document('8.01', 'Other Events.', 'Example entered into a definitive agreement to acquire Sample.') +
               document('9.01', 'Financial Statements and Exhibits.', '104 Cover page data.'))
        row = {'accession_number': '0000000001-30-000001', 'sha256': hashlib.sha256(raw).hexdigest(), 'api_items': ['8.01']}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); target = root / row['accession_number'] / 'primary.html'
            target.parent.mkdir(); target.write_bytes(raw)
            result = mod.inspect_file(row, root)
            self.assertEqual(result['status'], 'candidates_found')
            self.assertEqual(result['items_only_in_body'], ['9.01'])

    def test_access_denied_stops_remaining_requests(self):
        rows = [{'accession_number': str(i), 'document_url': 'https://www.sec.gov/test'} for i in range(2)]
        for code in [403, 429]:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                with patch.object(mod, 'urlopen', side_effect=HTTPError(rows[0]['document_url'], code, 'denied', {}, None)) as opener:
                    with self.assertRaisesRegex(ValueError, str(code)):
                        mod.fetch_sources(rows, Path(temp), 'test@example.com')
                    self.assertEqual(opener.call_count, 1)

    def test_manifest_scope_and_duplicate_checks(self):
        manifest = json.loads((mod.ROOT / 'filings.json').read_text())
        self.assertEqual(len(mod.validate_manifest(manifest)), 5)
        manifest['filings'][0]['ticker'] = 'OTHER'
        with self.assertRaises(ValueError): mod.validate_manifest(manifest)
        manifest = json.loads((mod.ROOT / 'filings.json').read_text())
        manifest['filings'][-1] = manifest['filings'][0]
        with self.assertRaises(ValueError): mod.validate_manifest(manifest)


if __name__ == '__main__':
    unittest.main()
