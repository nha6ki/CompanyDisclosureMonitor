from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_daily as pub


class PublicationTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.remote = root/'remote.git'
        self.local = root/'inputs'
        self.private = root/'private'
        self.private.mkdir()
        self.git = pub.command
        self.git('git','clone','-q','--bare',str(pub.ROOT),str(self.remote))
        self.git('git','--git-dir',str(self.remote),'update-ref','refs/heads/main',pub.BASE)
        self.git('git','clone','-q','--branch','main',str(self.remote),str(self.local))
        self.work = self.local/'pages/data/extraction-states.json'
        self.runs = []
        self.fail_watch = False
        self.conclusion = 'success'

        def checked(*args, **kwargs):
            if args[0] == 'gh':
                if args[1] == 'api':
                    self.runs.append(str(len(self.runs)+1))
                    return json.dumps({'workflow_run_id':self.runs[-1]})
                if args[2] == 'view':
                    return json.dumps(dict(status='completed',conclusion=self.conclusion))
                if args[2] == 'watch':
                    if self.fail_watch:
                        raise RuntimeError('deployment interrupted')
                    return ''
                self.fail('unexpected gh command')
            return self.git(*args, **kwargs)

        stack.enter_context(patch.multiple(pub, ROOT=self.local, REMOTE=str(self.remote), command=checked))
        stack.enter_context(patch.object(pub.manage, 'DATA', self.local/'pages/data'))

    def test_publish_and_noop(self):
        self.assertEqual(pub.publish(self.private)['state'],'unchanged')
        (self.local/'pages/data/unrelated.txt').write_text('local only')
        self.work.write_text('{"AAPL/test":"empty"}')
        self.assertEqual(pub.publish(self.private)['state'],'published')
        self.assertEqual(pub.publish(self.private)['state'],'unchanged')
        names=self.git('git','--git-dir',str(self.remote),'diff','--name-only',pub.BASE,'main')
        self.assertEqual(names,'pages/data/extraction-states.json')
        self.assertEqual(len(self.runs),1)

    def test_retry_does_not_require_new_data(self):
        self.work.write_text('{}')
        self.fail_watch=True
        with self.assertRaisesRegex(RuntimeError,'interrupted'):
            pub.publish(self.private)
        self.fail_watch=False
        self.conclusion='failure'
        self.assertEqual(pub.publish(self.private)['state'],'published')
        self.assertEqual(len(self.runs),2)
        self.assertEqual(self.git('git','--git-dir',str(self.remote),'rev-list','--count',pub.BASE+'..main'),'1')

    def test_preserves_conflicting_remote_edit(self):
        self.work.write_text('{"first":"manual"}')
        pub.publish(self.private)
        (self.private/'publish-checkpoint.json').unlink()
        self.work.write_text('{"second":"empty"}')
        with self.assertRaisesRegex(ValueError,'main also changed'):
            pub.publish(self.private)
        self.assertEqual(self.git('git','--git-dir',str(self.remote),'show','main:pages/data/extraction-states.json'),'{"first":"manual"}')
