import copy
import hashlib
import unittest
import structured_events as m


class StructuredEventChecks(unittest.TestCase):
    """Synthetic error scenarios, separate from the authored NVIDIA examples."""
    def setUp(self):
        self.acc = '0000000001-26-000001'
        self.block = ('On March 1, 2026, Example Corporation appointed Alex Sample, '
                      'effective May 4, 2026. On March 2, 2026, Example Corporation entered '
                      'into an agreement to acquire Target Corporation. The transaction is '
                      'expected to close in the first half of 2027, subject to approvals.')
        h = hashlib.sha256(self.block.encode()).hexdigest()
        self.ctx = {self.acc: {'source_sha256': 'a' * 64, 'filing_date': '2026-03-03',
                              'status': 'sections_ready', 'section': {
                                  'section_id': 'section-01', 'text_sha256': h,
                                  'block_start': 41, 'blocks': [self.block]}}}
        def f(v): return {'value': v, 'block_numbers': [41]}
        self.row = {'accession_number': self.acc, 'source_sha256': 'a' * 64,
                    'section_id': 'section-01', 'section_sha256': h, 'as_of_date': '2026-03-03',
                    'extraction_status': 'events_found', 'missing_information': [], 'events': [{
                        'event_id': 'event-1', 'event_type': f('appointment'), 'subject': f('Alex Sample'),
                        'organization': f('Example Corporation'), 'summary_ja': f('任命を決定。'),
                        'status_as_of_filing': f('pending_effective'), 'notes_ja': None, 'conditions': [],
                        'dates': [self.dt('decision', '2026-03-01', 'reported'), self.dt('effective', '2026-05-04', 'scheduled')]}]}
        self.payload = {'schema_version': m.VERSION, 'filings': [self.row]}
        self.event = self.row['events'][0]

    @staticmethod
    def dt(role, value, certainty, precision='day'):
        return {'role': role, 'value': value, 'certainty': certainty, 'precision': precision, 'block_numbers': [41]}

    def check(self): return m.validate(self.payload, self.ctx)
    def assert_code(self, code):
        result = self.check()
        self.assertFalse(result['mechanical_checks_passed'])
        self.assertIn(code, {e['code'] for e in result['errors']})

    def test_supported_dates_names_and_evidence(self):
        r = self.check()
        self.assertTrue(r['mechanical_checks_passed'], r['errors'])
        self.assertFalse(r['semantic_correctness_verified'])
        self.assertEqual(r['human_review_status'], 'pending')
        self.assertTrue(r['resolved_evidence'])

    def test_fabricated_person(self):
        self.event['subject']['value'] = 'Absent Person'
        self.assert_code('name_not_in_evidence')

    def test_date_absent_from_source(self):
        self.event['dates'][0]['value'] = '2026-02-28'
        self.assert_code('date_not_in_evidence')

    def test_impossible_calendar_date(self):
        self.event['dates'][0]['value'] = '2026-02-30'
        self.assert_code('date_not_in_evidence')

    def test_decision_and_effective_dates_swapped(self):
        self.event['dates'][0]['value'], self.event['dates'][1]['value'] = '2026-05-04', '2026-03-01'
        self.assert_code('event_date_after_filing')

    def test_half_year_is_not_an_exact_deadline(self):
        self.event['dates'] = [self.dt('expected_completion', '2027-H1', 'expected', 'half_year')]
        self.assertTrue(self.check()['mechanical_checks_passed'])
        self.event['dates'] = [self.dt('expected_completion', '2027-06-30', 'expected')]
        self.assert_code('date_not_in_evidence')

    def test_scheduled_appointment_not_completed(self):
        self.event['status_as_of_filing']['value'] = 'completed_as_reported'
        self.assert_code('completion_conflicts_with_schedule')

    def test_future_operational_dates_cannot_be_marked_reported(self):
        for role in ('effective', 'transition_end', 'expected_completion'):
            with self.subTest(role=role):
                self.event['dates'] = [self.dt(role, '2026-05-04', 'reported')]
                self.assert_code('future_date_marked_reported')
                for certainty in ('scheduled', 'expected'):
                    self.event['dates'][0]['certainty'] = certainty
                    self.assertTrue(self.check()['mechanical_checks_passed'])

    def test_reported_effective_date_before_or_on_filing_is_not_rejected(self):
        for filing_date in ('2026-05-04', '2026-05-05'):
            with self.subTest(filing_date=filing_date):
                self.row['as_of_date'] = filing_date
                self.ctx[self.acc]['filing_date'] = filing_date
                self.event['dates'] = [self.dt('effective', '2026-05-04', 'reported')]
                self.event['status_as_of_filing']['value'] = 'completed_as_reported'
                self.assertTrue(self.check()['mechanical_checks_passed'])

    def test_events_found_and_processing_status_must_agree(self):
        self.row['extraction_status'] = 'needs_review'
        self.assert_code('status_event_mismatch')

    def test_expected_acquisition_not_completed(self):
        self.event['event_type']['value'] = 'acquisition_agreement'
        self.event['status_as_of_filing']['value'] = 'completed_as_reported'
        self.event['dates'] = [self.dt('expected_completion', '2027-H1', 'expected', 'half_year')]
        self.assert_code('completion_conflicts_with_schedule')

    def test_evidence_outside_section(self):
        self.event['summary_ja']['block_numbers'] = [999]
        self.assert_code('invalid_block')

    def test_binding_to_wrong_document_version(self):
        self.row['source_sha256'] = 'b' * 64
        self.assert_code('source_binding_mismatch')

    def test_changed_missing_or_conflicting_original_is_not_accepted(self):
        for status in ('source_changed', 'source_missing', 'source_conflict'):
            with self.subTest(status=status):
                self.ctx[self.acc]['status'] = status
                self.assert_code('source_unavailable')

    def test_omitted_document(self):
        self.ctx['0000000001-26-000002'] = copy.deepcopy(self.ctx[self.acc])
        self.assert_code('filing_set_mismatch')

    def test_duplicate_event_is_rejected(self):
        self.row['events'].append(copy.deepcopy(self.event))
        self.assert_code('duplicate_event')

    def test_unexpected_approval_field_is_rejected(self):
        self.event['human_approved'] = True
        self.assert_code('schema_error')

    def test_unknown_date_is_kept_unknown(self):
        self.event['dates'][1] = {'role': 'effective', 'value': None, 'precision': None,
                                  'certainty': 'unknown', 'block_numbers': []}
        self.assertTrue(self.check()['mechanical_checks_passed'])
        self.event['dates'][1]['precision'] = 'day'
        self.assert_code('unknown_date_inconsistent')

    def test_no_event_output_stays_pending_review(self):
        self.row['events'] = []
        self.row['extraction_status'] = 'needs_review'
        r = self.check()
        self.assertTrue(r['mechanical_checks_passed'])
        self.assertEqual(r['human_review_status'], 'pending')
        self.assertTrue(r['review_items'])

    def test_wrong_semantics_is_not_claimed_verified(self):
        # Existing names and valid pointers cannot establish the meaning of a summary.
        self.event['summary_ja']['value'] = '契約は無条件で既に完了した。'
        r = self.check()
        self.assertTrue(r['mechanical_checks_passed'])
        self.assertFalse(r['semantic_correctness_verified'])
        self.assertEqual(r['human_review_status'], 'pending')


if __name__ == '__main__':
    unittest.main()
