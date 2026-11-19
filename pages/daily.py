"""Local SEC collection, Pages snapshot preparation and optional Ollama extraction."""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from http.client import HTTPException
import json
from pathlib import Path
import shutil
import sys
import tempfile
from zoneinfo import ZoneInfo

import manage
from manage import digest, load, write_atomic
from reports import report

PAGES = Path(__file__).resolve().parent
PROJECT = PAGES.parent
PRIVATE = PROJECT / 'register_data' / 'pages_daily'
sys.path[:0] = [str(PROJECT / 'nvidia_register'), str(PAGES / 'finance'), str(PROJECT)]
import companies
import pipeline
from demo_history import presentation
from nvidia_sync import locked
from refresh import generate, add_indicators
import daily_highlights as highlights


def save(path, obj):
    write_atomic(path, (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode())


class Sources:
    """Share one live submissions response per URL per pass; archive accession-specific documents on disk."""
    def __init__(self, get, directory):
        self.get, self.directory, self.feeds = get, directory, {}

    def __call__(self, url):
        if url.startswith('https://data.sec.gov/submissions/'):
            if url not in self.feeds:
                self.feeds[url] = self.get(url)
            return self.feeds[url]
        if not url.startswith('https://www.sec.gov/Archives/edgar/data/'):
            raise ValueError('Unexpected SEC source URL')
        file = self.directory / sha256(url.encode()).hexdigest()
        if file.is_file():
            return file.read_bytes()
        raw = self.get(url)
        head = raw[:16000].lower()
        if b'<html' not in head or b'undeclared automated tool' in head or b'request rate threshold exceeded' in head:
            raise ValueError('Expected the SEC filing HTML: '+url)
        write_atomic(file, raw)
        return raw


def quarterly(symbol, index, sources, day):
    records, incidents = pipeline.scan(symbol, sources, day, index)
    history = presentation(records)
    extra = generate(history, sources.directory, fetch_bytes=sources, indexes={symbol:index})
    add_indicators(history, extra)
    return history[symbol], incidents


def recover(folder):
    # A killed WSL process may leave a prepared transaction. Reapplying these bytes is idempotent.
    pending = folder/'prepared'
    if not pending.exists():
        return
    for path in sorted(pending.rglob('*')):
        if path.is_file():
            write_atomic(PAGES/path.relative_to(pending),path.read_bytes())
    pending.rename(folder/'applied')
    shutil.rmtree(folder/'applied')


def apply_updates(folder, snapshots, day, filings, failures=None):
    with manage.lock('reports'), manage.lock('reviews'):
        if (folder/'applied').is_dir():
            shutil.rmtree(folder/'applied')
        recover(folder)
        data, dates = manage.inputs()
        manifest = load(manage.DATA/'reports.json')
        changed = []
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            stage=Path(tmp)/'files'
            for sym, rows in snapshots.items():
                if digest(rows)==digest(data[sym]):
                    continue
                if day.isoformat()<dates[sym]:
                    raise ValueError('Clock precedes the saved data date for '+sym)
                stamp=day.isoformat()
                entry={'input':digest([stamp,rows])}
                try:
                    books={lang:report(sym,rows,stamp,lang) for lang in ('ja','en')}
                except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
                    if failures is None: raise
                    failures[sym+'/reports']=str(exc)
                    continue
                for lang,payload in books.items():
                    write_atomic(stage/'reports'/f'{sym}-{lang}.xlsx',payload)
                    entry[lang]=sha256(payload).hexdigest()
                manifest[sym]=entry
                dates[sym]=stamp
                data[sym]=rows
                changed.append(sym)
            if changed:
                for name,obj in [('quarters',data),('as-of',dates),('reports',manifest)]:
                    save(stage/'data'/(name+'.json'),obj)
            old=load(manage.DATA/'filings.json') if (manage.DATA/'filings.json').is_file() else {}
            if filings and old!=filings:
                save(stage/'data/filings.json',filings)
            if stage.exists():
                stage.rename(folder/'prepared')
                recover(folder)
        return changed


def collect_filing_jobs(symbol, day, sources, last, baseline, queue, published):
    # Include the boundary date on every run, including after PC sleep.
    saved=last.get("filings_date",baseline)
    previous=set(last.get("observed_8k", []))
    start=date.fromisoformat(saved)
    end=day
    if start>end:
        raise ValueError('Clock precedes the previous successful check')
    filings=companies.collect(symbol,end,sources,start=min(start,day-timedelta(days=89)))['rows']
    latest=filings[0]['accession_number'] if filings else None
    for row in filings:
        acc=row['accession_number']
        metadata=highlights.metadata(row)
        published.setdefault(symbol,{})[acc]=metadata
        key=symbol+'/'+acc
        new_in_window = bool(last.get('filings_date')) and acc not in previous and row['filing_date'] >= (day-timedelta(days=89)).isoformat()
        if row['filing_date'] < saved and acc!=latest and key not in queue and not new_in_window:
            continue
        if key not in queue or queue[key]['record']!=row:
            queue[key]={'record':row,'state':'pending'}
    last.update(filings_date=day.isoformat(),observed_8k=[r["accession_number"] for r in filings])


def run(contact, *, day=None, get=None, no_qwen=False, retry=False):
    day=day or datetime.now(ZoneInfo('America/New_York')).date()
    with locked(PRIVATE):
        with manage.lock('reports'), manage.lock('reviews'):
            if (PRIVATE/'applied').exists(): shutil.rmtree(PRIVATE/'applied')
            recover(PRIVATE)
        sources=Sources(get or pipeline.fetcher(contact),PROJECT/'register_data/quarter_sources')
        state_file=PRIVATE/'last-check.json'
        state=load(state_file) if state_file.is_file() else {}
        queue_file=PRIVATE/'queue.json'
        queue=load(queue_file) if queue_file.is_file() else {}
        pubfile=PAGES/'data/filings.json'
        published=load(pubfile) if pubfile.is_file() else {}
        current,dates=manage.inputs()
        summary={'checked_at':datetime.now(timezone.utc).isoformat(),'errors':{},'incidents':{},'changed':[]}
        snapshots={}
        for sym in sorted(companies.ISSUERS):
            last=state.setdefault(sym,{})
            try:
                collect_filing_jobs(sym,day,sources,last,dates[sym],queue,published)
            except (OSError,HTTPException,ValueError,KeyError,TypeError) as exc:
                summary['errors'][sym+'/filings']=str(exc)
            try:
                if day.isoformat()<max(dates[sym],last.get('financial_date','')):
                    raise ValueError('Clock precedes the saved financial snapshot')
                index=pipeline.filings(sym,day,sources)
                signature=digest(index)
                if not index: raise ValueError('No quarterly filing index available')
                if signature!=last.get('index') or digest(current[sym])!=last.get('output'):
                    rows,incidents=quarterly(sym,index,sources,day)
                    if rows[-1]['period']<current[sym][-1]['period']:
                        raise ValueError('Latest fiscal quarter would move backwards')
                    snapshots[sym]=rows
                    last.update(index=signature,output=digest(rows),incidents=incidents)
                if last.get('incidents'):
                    summary['incidents'][sym]=last['incidents']
                last['financial_date']=day.isoformat()
            except (OSError,HTTPException,ValueError,KeyError,TypeError,ArithmeticError,SyntaxError) as exc:
                summary['errors'][sym+'/financials']=str(exc)
        # Do not advance feed cursors until queued records and financial updates are recoverable.
        save(queue_file,queue)
        summary['changed']=apply_updates(PRIVATE,snapshots,day,published,summary['errors'])
        manage.build()
        save(state_file,state)
        save(PRIVATE/'status.json',summary)
        highlights.index_page(PRIVATE,queue,summary)

        def checkpoint():
            save(queue_file,queue)
            highlights.index_page(PRIVATE,queue,summary)
        if not no_qwen:
            highlights.process_queue(PRIVATE,queue,sources,retry,checkpoint,summary)
        summary['qwen']=dict(Counter(x['state'] for x in queue.values()))
        save(PRIVATE/'status.json',summary)
        highlights.index_page(PRIVATE,queue,summary)
        print(json.dumps({k:summary[k] for k in ('changed','errors','qwen')},ensure_ascii=False),flush=True)
        return int(bool(summary['errors']))


def approve(sym, acc, sid, expected):
    with locked(PRIVATE), manage.lock('reviews'):
        job=load(PRIVATE/'queue.json')[sym+'/'+acc]
        entry=job['candidate']
        section=entry['sections'][sid]
        if section['sourceHash']!=job['version'] or digest(section)!=expected:
            raise ValueError('Candidate changed. Open the local review again.')
        dest=manage.DATA/'approved-highlights.json'
        approved=load(dest) if dest.is_file() else {}
        filing=approved.setdefault(sym,{}).setdefault(acc,highlights.metadata(job['record']))
        filing['sections']={k:v for k,v in filing['sections'].items() if v['sourceHash']==job['version']}
        filing['sections'][sid]=dict(section,state='reviewed')
        save(dest,approved)
    manage.build()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--contact')
    group.add_argument('--approve',nargs=4,metavar=('TICKER','ACCESSION','SECTION','HASH'))
    parser.add_argument('--no-qwen',action='store_true')
    parser.add_argument('--retry-review',action='store_true',help='Retry sections previously flagged for manual inspection.')
    args=parser.parse_args()
    if args.approve:
        approve(*args.approve)
        raise SystemExit(0)
    raise SystemExit(run(args.contact,no_qwen=args.no_qwen,retry=args.retry_review))
