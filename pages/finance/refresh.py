"""Regenerate the published indicators from the monitor's SEC filing byte cache.

Run: python3 pages/finance/refresh.py QUARTER_HISTORY.json CACHE_DIRECTORY
The cache contains sha256(url)-named source documents and optional TICKER.json
filing indexes (also covering the quarter preceding the eight-quarter window).
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from release_metrics import read_release
from report_metrics import build_report_metrics


PAGES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PAGES))
sys.path.insert(0, str(PAGES.parent / 'nvidia_register'))
from demo_history import presentation
from manage import DATA, SYMBOLS, lock, write_atomic



def generate(history, cache, *, fetch_bytes=None, indexes=None):
    def fetch(url):
        return fetch_bytes(url) if fetch_bytes else (cache / sha256(url.encode()).hexdigest()).read_bytes()

    snapshot = {}
    for symbol, records in history.items():
        index = cache / (symbol + '.json')
        filings = indexes[symbol] if indexes is not None else (json.loads(index.read_text()) if index.exists() else [])
        reports = build_report_metrics(symbol, records, fetch, filings)
        entries = {}
        for record in records:
            period = record['period']
            exhibit = {}
            if record.get('url'):
                raw = fetch(record['url'])
                if sha256(raw).hexdigest() != record['sha']:
                    raise ValueError(f'{symbol} {period}: earnings release hash differs')
                for key, point in read_release(symbol, record, raw).items():
                    source = dict(point, url=record['url'], form='Exhibit 99.1')
                    exhibit[key] = dict(value=point['value'], method='direct', sources=[source])
                reported = record['facts'][0]['value']
                if exhibit.get('revenue') and reported is not None:
                    scale = 1000 if record['facts'][0]['unit'] == 'USD billions' else 1
                    if float(reported) * scale != exhibit['revenue']['value']:
                        raise ValueError(f'{symbol} {period}: conflicting release revenue')
            report = reports[period]
            forms = {r['url']:r['form'] for r in filings}
            forms.update({r['quarterly']:r['form'] for r in records if r.get('quarterly')})
            for item in report.values():
                for source in item['sources']:
                    source['form'] = forms.get(source['url'], 'SEC')
            entries[period] = dict(release=exhibit, report=report,
                                  releaseSha=record['sha'], reportSha=record.get('quarterlySha'))
        snapshot[symbol] = entries
    return snapshot


def add_indicators(history, extra):
    for sym, rows in history.items():
        for d in rows:
            d.pop('state')
            e = extra[sym][d['period']]
            release, report = e['release'].get('operating'), e['report'].get('operating')
            if d['candidates']: status = '訂正・重複'
            elif not d['quarterly']: status = '照合待ち'
            elif not release: status = '抽出失敗'
            elif not report: status = '比較値なし'
            elif report['method'] == 'difference': status = '累計差額と一致' if release['value']==report['value'] else '累計差額と差異'
            else: status = '一致' if release['value']==report['value'] else '不一致'
            d['facts'].append(dict(metric='Operating income', summaryRow=release['sources'][0]['row'] if release else 'Operating income',
               unit='USD millions',value=str(release['value']) if release else None, detail=str(report['value']) if report else None,
               detailUnit='USD millions', row=' − '.join(s['tag']+' · '+s['column'] for s in report['sources']) if report else '',
               column=d['end'],status=status,reason=d['facts'][0]['reason'] if status in ('訂正・重複','照合待ち') else ''))
            d['financials']={key:e[key] for key in ('release','report')}


def update(seed, cache):
    issuers = seed['issuers']
    if set(issuers) != SYMBOLS:
        raise ValueError('Expected a complete seven-company quarterly snapshot.')
    history = presentation(r for issuer in issuers.values() for r in issuer['rows'])
    extra = generate(history, cache)
    add_indicators(history, extra)
    with lock('reports'), lock('reviews'):
        for name,payload in [('quarters',history),('as-of',{s:seed['asof'] for s in history})]:
            write_atomic(DATA/(name+'.json'),(json.dumps(payload,ensure_ascii=False,indent=2)+'\n').encode())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('quarter_history', type=Path)
    parser.add_argument('cache', type=Path)
    args = parser.parse_args()
    update(json.loads(args.quarter_history.read_text()),args.cache)
