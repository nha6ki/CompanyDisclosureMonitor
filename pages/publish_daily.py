"""Publish validated public data via the existing main-branch Pages workflow."""
import argparse
from hashlib import sha1
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import manage

ROOT = manage.ROOT.parent
REPO = 'nha6ki/CompanyDisclosureMonitor'
REMOTE = f'https://github.com/{REPO}.git'
# Both the user's initial live site and the distributed input files came from this release.
BASE = '6acf33b613e5fd6eb35f303f154873d894bbc2fd'
PRIVATE = ROOT / 'register_data/pages_daily'
SWITCH = 'auto-publish.enabled'
GIT = ('git', '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential')
DATA_FILES = ('quarters', 'as-of', 'reports', 'filings', 'reviews', 'approved-highlights', 'extraction-states')
FILES = tuple('pages/data/' + name + '.json' for name in DATA_FILES) + tuple(
    f'pages/reports/{symbol}-{lang}.xlsx' for symbol in sorted(manage.SYMBOLS) for lang in ('ja', 'en'))
OPTIONAL = {'pages/data/' + name + '.json' for name in ('filings', 'approved-highlights', 'extraction-states')}


def command(*args, cwd=ROOT):
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=dict(os.environ, GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0'))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'{args[0]} failed ({result.returncode})')
    return result.stdout.strip()


def revision_files(directory, ref):
    lines = command('git', 'ls-tree', '-r', ref, '--', *FILES, cwd=directory).splitlines()
    blobs = {}
    for line in lines:
        spec, name = line.split('\t', 1)
        mode, _, blob = spec.split()
        if mode != '100644':
            raise ValueError('Unexpected file type: ' + name)
        blobs[name] = blob
    return blobs


def save(path, value):
    manage.write_atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def dispatch():
    payload = command('gh', 'api', '--method', 'POST', '-H', 'X-GitHub-Api-Version: 2026-03-10',
                      f'repos/{REPO}/actions/workflows/pages.yml/dispatches', '-f', 'ref=main')
    return str(json.loads(payload)['workflow_run_id'])


def publish(private):
    receipt = private / 'publish-checkpoint.json'
    state = manage.load(receipt) if receipt.exists() else {'baseline': revision_files(ROOT, BASE)}
    with manage.lock('reports'), manage.lock('reviews'):
        inputs = {}
        for name in FILES:
            path = ROOT / name
            if path.is_symlink():
                raise ValueError('Unexpected symlink: ' + name)
            if path.exists() or name not in OPTIONAL:
                inputs[name] = path.read_bytes()
    # Git blob IDs allow comparison with main without modifying the operator's working tree.
    local = {name: sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
             for name, data in inputs.items()}
    baseline = state['baseline']
    sending = state.get('sending', {})
    changed = [name for name in FILES if local.get(name) != baseline.get(name) or name in sending]
    if not changed and not state.get('pending'):
        return {'state':'unchanged'}

    if changed:
        with tempfile.TemporaryDirectory(dir=private) as temp:
            repo = Path(temp) / 'release'
            command(*GIT, 'clone', '--quiet', '--branch', 'main', '--single-branch', '--depth', '1', REMOTE, str(repo))
            remote = revision_files(repo, 'HEAD')
            staged = []
            for name in changed:
                if remote.get(name) == local.get(name):
                    continue
                if remote.get(name) != baseline.get(name) and not (name in sending and remote.get(name) == sending[name]):
                    raise ValueError('GitHub main also changed this file; reconcile it locally: ' + name)
                dest = repo / name
                if name in inputs:
                    dest.write_bytes(inputs[name])
                else:
                    dest.unlink()
                staged.append(name)
            # Build the actual tree that GitHub will deploy, including any unrelated changes on main.
            command(sys.executable, 'pages/manage.py', 'build', cwd=repo)
            if staged:
                command('git', 'add', '--', *staged, cwd=repo)
                command('git', '-c', 'user.name=Disclosure Monitor', '-c', 'user.email=disclosure-monitor@localhost',
                        'commit', '-qm', 'Update public disclosure data', cwd=repo)
                # Record both possible remote states before the network write. A lost reply can be retried.
                base = dict(baseline)
                for name in changed:
                    if name in remote:
                        base[name] = remote[name]
                    else:
                        base.pop(name, None)
                save(receipt, dict(baseline=base, sending={name:local.get(name) for name in changed}, pending=True))
                command(*GIT, 'push', 'origin', 'HEAD:refs/heads/main', cwd=repo)
            state = dict(baseline=local, pending=True)
            save(receipt, state)

    run = state.get('run')
    if run:
        info = json.loads(command('gh', 'run', 'view', run, '-R', REPO, '--json', 'status,conclusion'))
        if info['status'] == 'completed' and info['conclusion'] != 'success':
            run = None
    if not run:
        run = dispatch()
        state['run'] = run
        save(receipt, state)
    command('gh', 'run', 'watch', run, '-R', REPO, '--exit-status')
    save(receipt, {'baseline':local})
    return {'state':'published', 'run_url':f'https://github.com/{REPO}/actions/runs/{run}'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--enable', action='store_true')
    mode.add_argument('--disable', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from nvidia_sync import locked
    with locked(PRIVATE):
        switch = PRIVATE / SWITCH
        if args.disable:
            switch.unlink(missing_ok=True)
            print('自動公開を停止しました。日次収集は継続します。')
        else:
            if args.enable:
                if command('gh', 'api', 'repos/'+REPO, '--jq', '.permissions.push') != 'true':
                    sys.exit('GitHubへの書き込み権限を確認してください。')
                manage.write_atomic(switch, b'enabled\n')
            if not switch.exists():
                sys.exit('自動公開の有効化には --enable を指定してください。')
            print(json.dumps(publish(PRIVATE), ensure_ascii=False))
