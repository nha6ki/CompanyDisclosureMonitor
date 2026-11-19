"""Prepare GitHub Pages and manage quarterly reviews locally. All commands are offline."""
import argparse
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
from contextlib import contextmanager
import fcntl

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
OUTPUT = ROOT.parent / '_site'
SYMBOLS = {'AAPL','AMZN','GOOGL','META','MSFT','NVDA','TSLA'}


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return sha256(data.encode()).hexdigest()


def inputs():
    quarters = load(DATA / 'quarters.json')
    dates = load(DATA / 'as-of.json')
    if set(quarters) != SYMBOLS or set(dates) != SYMBOLS:
        raise ValueError('Company list differs between the snapshots.')
    for symbol, rows in quarters.items():
        periods = [r['period'] for r in rows]
        if not periods or periods != sorted(set(periods)) or any('state' in r for r in rows):
            raise ValueError(f'Invalid or unsorted quarters for {symbol}.')
    return quarters, dates


def decisions():
    history = load(DATA / 'reviews.json')
    latest = {}
    for item in history:
        key = (item['symbol'], item['period'])
        previous = latest.get(key)
        if item['revision'] != (previous['revision'] + 1 if previous else 1):
            raise ValueError(f'Invalid review sequence for {key}.')
        if item['state'] not in ('未確認', '確認済み') or not re.fullmatch(r'(?:legacy:)?[a-f0-9]{64}', item['evidence']):
            raise ValueError(f'Invalid review at {key}.')
        latest[key] = item
    return history, latest


def official(row, decision):
    if decision is None:
        return '未確認'
    return decision['state'] if decision['evidence'] == digest(row) else '再確認'


def write_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        try:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.chmod(tmp.name, 0o644)
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


@contextmanager
def lock(name):
    with (DATA / (name + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def review(args):
    with lock('reviews'):
        rows = inputs()[0].get(args.symbol.upper(), [])
        row = next((r for r in rows if r['period'] == args.period.upper()), None)
        if row is None:
            raise ValueError('Unknown company or fiscal quarter.')
        history, latest = decisions()
        key = (args.symbol.upper(), row['period'])
        decision = latest.get(key)
        version = digest(row)
        if args.command == 'show':
            print(*key, official(row, decision), version)
            return
        if args.expect and args.expect != version:
            raise ValueError('Evidence has changed. Check the quarter again.')
        state = '確認済み' if args.command == 'reviewed' else '未確認'
        if official(row, decision) != state:
            history.append(dict(symbol=key[0], period=key[1], revision=decision['revision'] + 1 if decision else 1,
                                state=state, evidence=version, saved_at=datetime.now(timezone.utc).isoformat(timespec='seconds')))
            write_atomic(DATA / 'reviews.json', (json.dumps(history, ensure_ascii=False, indent=2)+'\n').encode())
        print(*key, state)


def script_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', r'\u003c')


def build():
    with lock('build'), lock('reports'), lock('reviews'):
        build_snapshot()


def build_snapshot():
    quarters, dates = inputs()
    _, latest = decisions()
    manifest = load(DATA / 'reports.json')
    documents = {}
    for symbol, rows in quarters.items():
        entry = manifest[symbol]
        if entry['input'] != digest([dates[symbol], rows]):
            raise ValueError(f'{symbol} financial data changed. Regenerate the Excel reports with pages/reports.py.')
        for lang in ('ja', 'en'):
            name = f'{symbol}-{lang}.xlsx'
            content = (ROOT / 'reports' / name).read_bytes()
            if sha256(content).hexdigest() != entry[lang]:
                raise ValueError('Report checksum mismatch: ' + name)
            documents['reports/' + name] = content
        for row in rows:
            row['state'] = official(row, latest.get((symbol,row['period'])))
    template = (ROOT / 'index.template.html').read_text(encoding='utf-8')
    for marker, content in {
        '@@AS_OF@@':script_json(dates), '@@HISTORY@@':script_json(quarters),
        '// @include review-demo.js':(ROOT/'review-demo.js').read_text(encoding='utf-8'),
        '// @include financials.js':(ROOT/'financials.js').read_text(encoding='utf-8'),
        '/* @include financials.css */':(ROOT/'financials.css').read_text(encoding='utf-8')
    }.items():
        if template.count(marker) != 1:
            raise ValueError('Missing or duplicate template marker: ' + marker)
        template = template.replace(marker, content)
    documents.update({'index.html':template.encode(), '.nojekyll':b''})
    if OUTPUT.is_symlink() or (OUTPUT/'reports').is_symlink():
        raise ValueError('Unexpected output symlink.')
    for path in OUTPUT.rglob('*'):
        if path.is_symlink() or (path.is_file() and path.relative_to(OUTPUT).as_posix() not in documents):
            raise ValueError('Unexpected file in publication directory: ' + str(path))
    for name, content in documents.items():
        write_atomic(OUTPUT / name, content)
    print('Built',sum(map(len,quarters.values())),'quarters and',len(documents)-2,'Excel reports in',OUTPUT,flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('reviewed', 'pending', 'show'):
        p = sub.add_parser(command)
        p.add_argument('symbol')
        p.add_argument('period')
        if command != 'show':
            p.add_argument('--expect',help='Save only if the evidence matches the hash returned by show.')
    sub.add_parser('build')
    preview = sub.add_parser('preview')
    preview.add_argument('--port',type=int,default=8765)
    args = parser.parse_args()
    try:
        if args.command in ('reviewed','pending','show'):
            review(args)
            return
        build()
        if args.command == 'preview':
            handler = partial(SimpleHTTPRequestHandler, directory=str(OUTPUT))
            with ThreadingHTTPServer(('127.0.0.1',args.port), handler) as server:
                print(f'http://127.0.0.1:{server.server_port}/?lang=ja',flush=True)
                server.serve_forever()
    except (ValueError,KeyError,OSError) as error:
        parser.exit(1, f'{error}\n')
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
