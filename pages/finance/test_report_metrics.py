from datetime import date
import unittest
from report_metrics import quarter_metric, read_report


def d(iso):
    return date.fromisoformat(iso)


def intake(start, end, val):
    return {(d(start),d(end)): {'value': val}}


class ReportMetricsTest(unittest.TestCase):
    def test_subtraction_requires_same_start_and_previous_quarter_end(self):
        ytd = intake('2025-01-01','2025-09-30',70)
        prev = intake('2025-01-01','2025-06-30',45)
        self.assertEqual(quarter_metric(ytd,prev,d('2025-09-30'),d('2025-06-30'),3)['value'],25)
        self.assertIsNone(quarter_metric(ytd,prev,d('2025-09-30'),d('2025-03-31'),3))
        self.assertIsNone(quarter_metric(ytd,intake('2025-01-02','2025-06-30',45),d('2025-09-30'),d('2025-06-30'),3))
        self.assertIsNone(quarter_metric(ytd,prev,d('2025-09-30'),d('2025-06-30'),1))

    def test_direct_signed_amount_preferred_over_differences(self):
        facts = intake('2025-01-01','2025-09-30',70)
        facts.update(intake('2025-07-01','2025-09-30',-12))
        prev = intake('2025-01-01','2025-06-30',82)
        item = quarter_metric(facts,prev,d('2025-09-30'),d('2025-06-30'),3)
        self.assertEqual(item, {'value':-12, 'method':'direct','sources':[{'value':-12}]})

    def test_fact_tag_unit_dimensions_and_source_locator(self):
        ctx = '<context id="{}"><entity><identifier>320193</identifier>{}</entity><period><startDate>2025-01-01</startDate><endDate>2025-03-31</endDate></period></context>'
        row = '<tr><td>Operating income</td><td><nonFraction name="us-gaap:OperatingIncomeLoss" contextRef="{}" unitRef="{}" scale="3" sign="-" id="{}">{}</nonFraction></td></tr>'
        body = (ctx.format('consolidated','') + ctx.format('by-region','<segment/>')
                + '<unit id="usd"><measure>iso4217:USD</measure></unit>'
                + '<unit id="eur"><measure>iso4217:EUR</measure></unit><table>'
                + row.format('consolidated','usd','F1','2,000') + row.format('by-region','usd','F2','800')
                + row.format('consolidated','eur','F3','499') + '</table>')
        values = read_report('AAPL','https://example.test/report',('<filing>'+body+'</filing>').encode())
        s = values['operating'][d('2025-01-01'),d('2025-03-31')]
        self.assertEqual(s['value'],-2)
        self.assertEqual(s['anchor'],'F1')
        self.assertEqual(s['row'],'Operating income')


if __name__ == '__main__':
    unittest.main()
