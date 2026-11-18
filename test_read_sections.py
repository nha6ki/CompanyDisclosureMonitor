import hashlib
from pathlib import Path
import tempfile
import unittest

import read_sections as reader


def html(*parts):
    return ('<html><body>' + ''.join(parts) + '</body></html>').encode()


class SectionTests(unittest.TestCase):
    def test_no_event_keyword_required(self):
        result = reader.split_sections(html('<h2>Item 8.01. Other Events.</h2>',
                                            '<p>A novel business event is described without a supported keyword.</p>'))
        self.assertEqual(result['status'], 'sections_ready')
        self.assertIn('novel business event', result['sections'][0]['text'])

    def test_dates_conditions_negation_and_outlook_are_retained(self):
        raw = html('<h2>Item 5.02. Departure of Directors or Certain Officers.</h2>',
                   '<p>The appointment is not effective until October 9, 2030.</p>',
                   '<h3>Forward-Looking Statements</h3>',
                   '<p>If regulatory consent is delayed, the start date may change.</p>')
        text = reader.split_sections(raw)['sections'][0]['text']
        self.assertIn('not effective until October 9, 2030', text)
        self.assertIn('If regulatory consent is delayed', text)

    def test_shareholder_vote_item_and_table_cell_order(self):
        raw = html('<h2>Item 5.07. Submission of Matters to a Vote of Security Holders.</h2>',
                   '<p>Proposal A was approved.</p>',
                   '<table><tr><td>For</td><td>100</td></tr><tr><td>Against</td><td>7</td></tr></table>')
        result = reader.split_sections(raw)
        self.assertEqual(result['sections'][0]['item'], '5.07')
        self.assertEqual(result['sections'][0]['blocks'][-4:], ['For', '100', 'Against', '7'])

    def test_split_item_number_and_title(self):
        raw = html('<table><tr><td>Item</td><td>5.07.</td><td>Submission of Matters to a Vote of Security Holders.</td></tr></table>',
                   '<p>The stockholders voted on a proposal.</p>')
        r = reader.split_sections(raw)
        self.assertEqual(r['status'], 'sections_ready')
        self.assertEqual(len(r['sections']), 1)
        self.assertIn('The stockholders voted', r['sections'][0]['text'])

    def test_cross_reference_is_not_a_new_section(self):
        raw = html('<h2>Item 2.03. Creation of a Direct Financial Obligation.</h2>',
                   '<p>Item 1.01 above is incorporated by reference.</p>')
        r = reader.split_sections(raw)
        self.assertEqual([s['item'] for s in r['sections']], ['2.03'])
        self.assertIn('Item 1.01 above', r['sections'][0]['text'])

    def test_inline_heading_and_body_not_dropped(self):
        raw = html('<p><b>Item 8.01. Other Events.</b> The company announced a new project.</p>')
        r = reader.split_sections(raw)
        self.assertIn('The company announced a new project.', r['sections'][0]['text'])

    def test_unknown_item_is_preserved_and_flagged(self):
        raw = html('<h2>Item 8.01. Other Events.</h2><p>Known content.</p>',
                   '<h2>Item 6.99. Unfamiliar Heading.</h2><p>Unfamiliar content.</p>')
        r = reader.split_sections(raw)
        self.assertEqual(r['status'], 'review_required')
        self.assertEqual([s['item'] for s in r['sections']], ['8.01', '6.99'])
        self.assertIn('Unfamiliar content.', r['sections'][1]['text'])
        self.assertNotIn('Unfamiliar content.', r['sections'][0]['text'])

    def test_no_heading_preserves_unsegmented_document(self):
        r = reader.split_sections(html('<p>A document with no recognized Item heading.</p>'))
        self.assertEqual(r['status'], 'unsegmented')
        self.assertEqual(r['unsegmented_blocks'], ['A document with no recognized Item heading.'])

    def test_repeated_headings_remain_distinct(self):
        raw = html('<h2>Item 8.01. Other Events.</h2><p>First content.</p>',
                   '<h2>Item 8.01. Other Events.</h2><p>Second content.</p>')
        r = reader.split_sections(raw)
        self.assertEqual(len(r['sections']), 2)
        self.assertEqual(len({s['section_id'] for s in r['sections']}), 2)
        self.assertTrue(r['partition_check_passed'])

    def test_hidden_header_and_signature_boundaries(self):
        raw = html('<div hidden>Item 5.02. Departure of Directors.</div>',
                   '<h2>Item 8.01. Other Events.</h2><p>Visible content.</p>',
                   '<p>SIGNATURE</p><p>Signed by the officer.</p>')
        r = reader.split_sections(raw)
        self.assertEqual([s['item'] for s in r['sections']], ['8.01'])
        self.assertNotIn('Signed by the officer', r['sections'][0]['text'])

    def test_missing_or_changed_source_does_not_produce_sections(self):
        row = {'accession_number': '0000000001-30-000001', 'sha256': '0' * 64, 'api_items': ['8.01']}
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            self.assertEqual(reader.inspect_source(row, [p])['status'], 'source_missing')
            path = p / row['accession_number'] / 'primary.html'
            path.parent.mkdir(); path.write_bytes(b'changed source')
            r = reader.inspect_source(row, [p])
            self.assertEqual(r['status'], 'source_changed')
            self.assertEqual(r['sections'], [])

    def test_metadata_differences_do_not_delete_body_section(self):
        raw = html('<h2>Item 8.01. Other Events.</h2><p>Content.</p>',
                   '<h2>Item 9.01. Financial Statements and Exhibits.</h2><p>104 Cover page data.</p>')
        row = {'accession_number': '0000000001-30-000001', 'sha256': hashlib.sha256(raw).hexdigest(), 'api_items': ['8.01']}
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); path = p / row['accession_number'] / 'primary.html'
            path.parent.mkdir(); path.write_bytes(raw)
            r = reader.inspect_source(row, [p])
            self.assertEqual(r['status'], 'sections_ready')
            self.assertEqual(r['items_only_in_body'], ['9.01'])
            self.assertEqual(len(r['sections']), 2)


if __name__ == '__main__':
    unittest.main()
