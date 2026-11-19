"""Discover, reconcile and retain the latest eight fiscal quarters per SEC issuer."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from http.client import HTTPException
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urljoin
from xml.etree.ElementTree import ParseError
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companies import ISSUERS
from earnings import atomic
from filing_pair import METRICS, UNITS, exhibit_link
from nvidia_sync import filing_rows, locked
from periods import fetcher
from quarter_evidence import anchors, label, ordinal, periodic, release


SEED = Path(__file__).parent / "examples" / "quarter_history.json"
FORMS = {"8-K","8-K/A","10-Q","10-Q/A","10-K","10-K/A"}
PROBLEMS = ("照合待ち","添付未特定","比較値なし","抽出失敗","不一致","訂正・重複","開示未検出")
DECISIONS = ("未確認","確認済み","要対応")


def digest(obj):
    return sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def filings(symbol, day, get):
    cik = ISSUERS[symbol][1]
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    feed = json.loads(get(url))
    if int(feed["cik"]) != cik:
        raise ValueError("CIK不一致")
    start = day - timedelta(days=805)
    pages = [feed["filings"]["recent"]]
    for archive in feed["filings"]["files"]:
        if archive['filingFrom'] <= day.isoformat() and archive['filingTo'] >= start.isoformat():
            if not re.fullmatch(fr"CIK{cik:010d}-submissions-\d+\.json",archive['name']):
                raise ValueError("SEC履歴名が不正です")
            pages.append(json.loads(get(urljoin(url,archive['name']))))
    indexed = {}
    for page in pages:
        ends = dict(zip(page["accessionNumber"],page["reportDate"],strict=True))
        for form,r in filing_rows(page,start,cik,symbol,FORMS,day):
            if form.startswith("8-K") and "2.02" not in r["api_items"] and (form!="8-K/A" or r["api_items"]):
                continue
            acc = r["accession_number"]
            row = dict(form=form, day=r["filing_date"], acc=acc,url=r["document_url"], end=ends[acc],items=r["api_items"])
            if acc in indexed and indexed[acc]!=row:
                raise ValueError("提出番号のメタデータが競合しています")
            indexed[acc] = row
    return sorted(indexed.values(),key=lambda r:(r["day"],r["acc"]))


def scan(symbol, get, day, filing_index=None):
    groups = defaultdict(lambda: {"releases":[], "reports":[]})
    issues = []
    for source in filings(symbol,day,get) if filing_index is None else filing_index:
        r = dict(source)
        if not r['form'].startswith("8-K"):
            end = date.fromisoformat(r['end'])
            key = ordinal(symbol,end)
            groups[key]['reports'].append(r)
            continue
        raw = get(r['url'])
        try:
            if r['form'].endswith('/A') and "2.02" not in r['items']:
                raise ValueError("訂正8-Kの対象項目を要確認")
            r['exhibit'] = exhibit_link(raw,r['url'])
            exhibit = get(r['exhibit'])
            # Tesla publishes a delivery-only notice before each earnings release.
            if symbol == "TSLA" and re.search(rb'(?:production and deliveries|vehicle deliveries and storage deployments|produced over.{0,100}vehicles)',exhibit,re.I|re.S) and re.search(rb'will (?:post|release).{0,45}financial results',exhibit,re.I|re.S):
                continue
            end, values, reasons = release(exhibit,symbol,r['day'])
            key = ordinal(symbol,end)
            groups[key]['releases'].append(dict(r,end=end.isoformat(),values=values,reasons=reasons,sha=sha256(exhibit).hexdigest()))
        except (ValueError,KeyError) as exc:
            issues.append(dict(symbol=symbol,**r,reason=str(exc)))
    if not groups:
        raise ValueError("対象期間内の決算提出を特定できません")
    latest = max(groups)
    rows=[]
    for key in range(latest-7,latest+1):
        src=groups[key]
        releases, reports = src['releases'], src['reports']
        originals = [r for r in reports if not r["form"].endswith("/A")]
        q = originals[0] if len(originals) == 1 else {}
        x = releases[0] if len(releases)==1 else {}
        all_sources = [*releases,*reports]
        ends = {r['end'] for r in all_sources}
        end = min(ends) if ends else next(d.isoformat() for d,k in anchors(symbol,key//4) if k==key)
        conflict = (len(ends)>1 or len(reports)>1 or len(releases)>1 or
                    any(r['form'].endswith('/A') for r in all_sources))
        qv,qr,ql = [None,None],["",""],["",""]
        qsha = ""
        if q and not conflict:
            raw = get(q['url'])
            qsha = sha256(raw).hexdigest()
            try:
                qv,qr,ql = periodic(raw,symbol,date.fromisoformat(end))
            except (ParseError,ValueError,ArithmeticError) as exc:
                qr = [f"XBRL解析失敗: {exc}"]*2
        xv=x.get('values',[None,None])
        lines=[]
        for i,name in enumerate(METRICS):
            if conflict:
                status,why="訂正・重複","提出候補の訂正・重複または期末日不一致"
            elif not x and not q:
                status,why="開示未検出","四半期に対応する提出を特定できません。期末日は暦上の目安です"
            elif not x:
                status,why="添付未特定","決算添付を確認してください"
            elif xv[i] is None:
                status,why="抽出失敗",x['reasons'][i]
            elif not q:
                status,why="照合待ち","同じ期末の10-Qまたは10-Kを待っています"
            elif qv[i] is None:
                status = "比較値なし" if q['form']=="10-K" and qr[i]=="対象3か月の連結XBRL値なし" else "抽出失敗"
                why=qr[i]
            else:
                status="一致" if Decimal(xv[i])==Decimal(qv[i]) else "不一致"
                why="" if status=="一致" else "決算添付と定期報告書の値が異なります"
            lines.append(dict(metric=name,unit=UNITS[i],release=xv[i],comparison=qv[i],status=status,reason=why,locator=ql[i]))
        rows.append(dict(symbol=symbol,period=label(key),end=end,estimated=not ends,
                    filing=x.get('url',''),accession=x.get('acc',''),exhibit=x.get('exhibit',''),release_date=x.get('day',''),
                    report=q.get('url',''),report_acc=q.get('acc',''),form=q.get('form',''),report_date=q.get('day',''),
                    sha=x.get('sha',''),report_sha=qsha,metrics=lines,
                    candidates=[dict(acc=r['acc'],form=r['form'],url=r['url']) for r in all_sources] if conflict else []))
    return rows,issues


def connect(root, seed=True):
    directory=root/'register_data'
    directory.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(directory/'quarters.sqlite3',timeout=45)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS quarters(symbol TEXT, period TEXT, version TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(symbol,period));
    CREATE TABLE IF NOT EXISTS incidents(symbol TEXT, accession TEXT, data TEXT NOT NULL, PRIMARY KEY(symbol,accession));
    CREATE TABLE IF NOT EXISTS syncs(symbol TEXT PRIMARY KEY, asof TEXT NOT NULL, attempted TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, symbol TEXT, period TEXT, version TEXT, state TEXT, reviewer TEXT, note TEXT, stamp TEXT);
    CREATE INDEX IF NOT EXISTS decision_lookup ON decisions(symbol,period,id);
    """)
    if seed and not db.execute('SELECT 1 FROM syncs LIMIT 1').fetchone():
        baseline=json.loads(SEED.read_text())
        with db:
            db.execute('BEGIN IMMEDIATE')
            for sym, result in baseline['issuers'].items():
                if not db.execute("SELECT 1 FROM syncs WHERE symbol=?",(sym,)).fetchone():
                    save(db,sym,baseline['asof'],result['rows'],result['issues'])
    return db


def save(db,sym,asof,rows,issues):
    for row in rows:
        db.execute("""INSERT INTO quarters VALUES(?,?,?,?) ON CONFLICT(symbol,period)
            DO UPDATE SET version=excluded.version,data=excluded.data WHERE version!=excluded.version""",
            (sym,row['period'],digest(row),json.dumps(row,ensure_ascii=False)))
    db.execute("DELETE FROM incidents WHERE symbol=?",(sym,))
    db.executemany("INSERT INTO incidents VALUES(?,?,?)",[(sym,i['acc'],json.dumps(i,ensure_ascii=False)) for i in issues])
    db.execute("""INSERT INTO syncs VALUES(?,?,?,'') ON CONFLICT(symbol) DO UPDATE SET
               asof=excluded.asof,attempted=excluded.attempted,error=''""",(sym,asof,asof))


def sync(root, get, day=None, symbols=ISSUERS):
    day = day or datetime.now(ZoneInfo('America/New_York')).date()
    folder=root/'register_data'/'quarter_sources'
    def cached(url):
        if not url.startswith("https://www.sec.gov/Archives/"):
            return get(url)
        path=folder/sha256(url.encode()).hexdigest()
        if path.is_file():
            return path.read_bytes()
        raw=get(url)
        if b'<html' not in raw[:12000].lower() or b'Your Request Originates from an Undeclared Automated Tool' in raw[:12000]:
            raise ValueError("SEC原文を取得できません")
        atomic(path,raw)
        return raw
    results={}
    with locked(folder), closing(connect(root)) as db:
        for sym in symbols:
            previous=db.execute("SELECT asof FROM syncs WHERE symbol=?",(sym,)).fetchone()
            if previous and previous[0] > day.isoformat():
                raise ValueError("保存済みデータより古い基準日です")
            try:
                rows,issues=scan(sym,cached,day)
                with db: save(db,sym,day.isoformat(),rows,issues)
                results[sym]=dict(quarters=len(rows), unmapped_filings=len(issues),
                    open_metrics=sum(m['status'] in PROBLEMS for r in rows for m in r['metrics']))
            except (OSError,HTTPException,ValueError,KeyError,TypeError) as exc:
                with db:
                    db.execute("""INSERT INTO syncs VALUES(?,'',?,?) ON CONFLICT(symbol)
                        DO UPDATE SET attempted=excluded.attempted,error=excluded.error""",(sym,day.isoformat(),str(exc)))
                results[sym]={'error':str(exc)}
    return results


def review(db,form):
    state=form['state']
    name=form.get('reviewer','').strip()
    note=form.get('note','').strip()
    if state not in DECISIONS or not name or len(name)>100 or len(note)>1500:
        raise ValueError("確認状態、担当者名、メモの長さを確認してください")
    sym,period,version=form['symbol'],form['period'],form['version']
    with db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT version FROM quarters WHERE symbol=? AND period=?",(sym,period)).fetchone()
        if not row or row[0]!=version:
            raise ValueError("対象の照合結果が更新されました。再読み込みしてください")
        last=db.execute("SELECT id,version,state,reviewer,note FROM decisions WHERE symbol=? AND period=? ORDER BY id DESC LIMIT 1",(sym,period)).fetchone()
        if last and tuple(last)[1:]==(version,state,name,note):
            return
        if int(form['previous'])!=(last[0] if last else 0):
            raise ValueError("ほかの確認が保存されました。再読み込みしてください")
        db.execute("INSERT INTO decisions(symbol,period,version,state,reviewer,note,stamp) VALUES(?,?,?,?,?,?,?)",
                   (sym,period,version,state,name,note,datetime.now(timezone.utc).isoformat(timespec='seconds')))


def records(db):
    rows=db.execute("""SELECT q.data,q.version,d.id,d.version AS reviewed,d.state,d.reviewer,d.note,d.stamp
            FROM (SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY period DESC) AS rank FROM quarters) q LEFT JOIN decisions d ON d.id=(SELECT MAX(id) FROM decisions
            WHERE symbol=q.symbol AND period=q.period) WHERE q.rank<=8 ORDER BY q.symbol,q.period DESC""")
    for r in rows:
        data=json.loads(r['data'])
        data.update(version=r['version'],previous=r['id'] or 0,state=(
            "再確認" if r['reviewed'] and r['reviewed']!=r['version'] else r['state'] or "未確認"),
            reviewer=r['reviewer'] or '',note=r['note'] or '',stamp=r['stamp'] or '')
        yield data


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contact',required=True)
    parser.add_argument('--company',choices=ISSUERS,nargs='+',default=list(ISSUERS))
    args=parser.parse_args()
    result=sync(ROOT,fetcher(args.contact),symbols=dict.fromkeys(args.company))
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(int(any('error' in r for r in result.values())))
