import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import manage


class LocalReviews(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)/'data'
        shutil.copytree(manage.DATA, data, ignore=shutil.ignore_patterns('*.lock'))
        for key,path in [('DATA',data),('OUTPUT',Path(temp.name)/'_site')]:
            changed = patch.object(manage,key,path)
            changed.start()
            self.addCleanup(changed.stop)

    def state(self):
        history, latest = manage.decisions()
        row = next(r for r in manage.inputs()[0]['AAPL'] if r['period']=='FY2026Q4')
        return manage.official(row,latest.get(('AAPL','FY2026Q4'))),len(history),manage.digest(row)

    def command(self, action, expect=None):
        with redirect_stdout(StringIO()):
            manage.review(SimpleNamespace(command=action,symbol='aapl',period='fy2026q4',expect=expect))

    def test_review_repeated_then_evidence_changed(self):
        old_count=self.state()[1]
        self.command('reviewed')
        self.command('reviewed')
        status,count,version=self.state()
        self.assertEqual((status,count),('確認済み',old_count+1))
        self.command('pending')
        self.assertEqual(self.state()[0],'未確認')
        quarters=manage.inputs()[0]
        next(r for r in quarters['AAPL'] if r['period']=='FY2026Q4')['sha']='new-digest'
        (manage.DATA/'quarters.json').write_text(json.dumps(quarters))
        self.assertEqual(self.state()[0],'再確認')
        with self.assertRaises(ValueError): self.command('reviewed',version)
        with self.assertRaises(ValueError): manage.build()
        self.command('reviewed',self.state()[2])
        self.assertEqual(self.state()[0],'確認済み')

    def test_release_contains_only_public_artifacts(self):
        with redirect_stdout(StringIO()): manage.build()
        self.assertEqual(len(list(manage.OUTPUT.rglob('*.xlsx'))),14)
        self.assertEqual({p.name for p in manage.OUTPUT.iterdir()},{'index.html','.nojekyll','reports'})
        html=(manage.OUTPUT/'index.html').read_text()
        self.assertNotIn('/api/quarter-review',html)
        self.assertNotIn('"saved_at":',html)
        self.assertNotIn('@@HISTORY@@',html)
        self.assertIn('./reports/',html)
        (manage.OUTPUT/'private.txt').touch()
        with self.assertRaises(ValueError): manage.build()


if __name__=='__main__': unittest.main()
