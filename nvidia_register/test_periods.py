import unittest

import periods


class CoverageTest(unittest.TestCase):
    def test_missing_quarter_does_not_shorten_delivery(self):
        requested=periods.requested('FY2025Q4','FY2027Q3')
        entries=[]
        for quarter in requested:
            if quarter=='FY2026Q2':
                continue
            # A discovered document with a failed parser is a different outcome.
            entries.append(dict(period=quarter,acc=quarter,url='',primary='',error='fixture'))
        result=dict(first=requested[0],last=requested[-1],entries=entries,unmapped=[])
        rows=periods.rows(result)
        self.assertEqual(len(rows),16)
        self.assertEqual([(r[0],r[1]) for r in rows if r[2]=='不足'],
                         [('FY2026Q2',name) for name in periods.METRICS])
        self.assertEqual(sum(r[2]=='抽出失敗' for r in rows),14)
