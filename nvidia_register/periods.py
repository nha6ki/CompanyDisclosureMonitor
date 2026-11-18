"""NVIDIA fiscal-quarter coverage. The requested periods define the rows."""
import argparse
from datetime import date
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from nvidia_sync import FEED, locked, select
import earnings
import reconcile
import reporting

METRICS=('Revenue','Diluted earnings per share')
HEADER=['対象期','指標','取得結果','自動照合','担当者確認','値','単位','提出番号','8-K原文','添付原文','理由','原文SHA256']


def number(label):
    m=re.fullmatch(r'FY(20\d{2})Q([1-4])',label.upper().strip())
    if not m: raise ValueError('FY2027Q3 の形式で指定してください')
    return int(m[1])*4+int(m[2])-1


def label(n):
    return f'FY{n//4}Q{n%4+1}'


def requested(first,last):
    lo,hi=number(first),number(last)
    if not 1<=hi-lo+1<=40: raise ValueError('対象期間は1～40四半期です')
    return [label(n) for n in range(lo,hi+1)]


def cache(root):
    return root/'register_data'/'quarter_coverage'


def fetcher(contact):
    if not re.fullmatch(r'[^\s@]+@[^\s@]+',contact): raise ValueError('SEC連絡先メールを指定してください')
    def fetch(url):
        time.sleep(.3)
        with urlopen(Request(url,headers={'User-Agent':f'CompanyDisclosureMonitor/1.2 {contact}'}),timeout=40) as res:
            raw=res.read(16_000_001)
        if len(raw)>16_000_000: raise ValueError('資料がサイズ上限を超えています')
        return raw
    return fetch


def exhibit(root,record,get):
    acc=record['accession_number']
    path=cache(root)/'sources'/acc/'primary.html'
    candidates=[root/d/acc/'primary.html' for d in ('nvidia_inbox','cache','validation/cache')]
    local=next((p for p in [path,*candidates] if p.is_file()),None)
    raw=local.read_bytes() if local else get(record['document_url'])
    doc=earnings.parse(raw)
    urls={url for url,t,r in doc.row_links if doc.tables[t][r][:1]==['99.1']
          and re.fullmatch(r'q[1-4]fy\d{2}pr\.htm',url,re.I)}
    if len(urls)!=1: raise ValueError('当期の決算発表資料99.1を特定できません')
    name=urls.pop()
    if local!=path: earnings.atomic(path,raw)
    return f'FY20{name[4:6]}Q{name[1]}',urljoin(record['document_url'],name)


def run(root,get,first=None,last=None,count=8):
    if not 1<=count<=40: raise ValueError('対象期数は1～40です')
    if first and not last: raise ValueError('開始期と終了期を両方指定してください')
    bounded=requested(first or label(number(last)-count+1),last) if last else None
    with locked(cache(root)):
        feed=json.loads(get(FEED))
        recent=feed['filings']['recent']
        pages=[recent]
        if not last:
            latest=sorted((r for r in select(feed,date.min) if '2.02' in r['api_items']),
                          key=lambda r:r['filing_date'],reverse=True)
            if not latest: raise ValueError('最新の決算発表を特定できません。終了期を指定してください')
            last,_=exhibit(root,latest[0],get)
            bounded=requested(label(number(last)-count+1),last)
        # Broad discovery window only. An exhibit and its GAAP column establish the actual period.
        fy,q=divmod(number(bounded[0]),4)
        since=date(fy-1,q*3+1,1)
        fy,q=divmod(number(bounded[-1]),4)
        end_year,end_month=divmod((fy-1)*12+q*3+8,12)
        until=date(end_year,end_month+1,1)
        for meta in feed['filings'].get('files',[]):
            if meta['filingTo']>=since.isoformat() and meta['filingFrom']<=until.isoformat():
                name=meta['name']
                if not re.fullmatch(r'CIK0001045810-submissions-\d+\.json',name): raise ValueError('SEC履歴名が不正です')
                pages.append(json.loads(get(urljoin(FEED,name))))
        records={}
        for page in pages:
            for r in select(dict(cik=feed['cik'],filings=dict(recent=page)),since):
                if '2.02' in r['api_items'] and r['filing_date']<=until.isoformat():
                    records[r['accession_number']]=r
        result=dict(first=bounded[0],last=bounded[-1],entries=[],unmapped=[])
        for acc,r in sorted(records.items()):
            try: period,url=exhibit(root,r,get)
            except (ValueError,OSError) as exc:
                result['unmapped'].append(dict(acc=acc,url=r['document_url'],error=str(exc)))
                continue
            if period not in bounded: continue
            entry=dict(period=period,acc=acc,url=url,primary=r['document_url'])
            result['entries'].append(entry)
            try:
                p=cache(root)/'sources'/acc/'99.1.html'
                raw=p.read_bytes() if p.exists() else get(url)
                # Verify the document before caching an HTTP response.
                if not earnings.parse(raw).tables: raise ValueError('添付の表を読み取れません')
                if not p.exists(): earnings.atomic(p,raw)
                entry['sha']=sha256(raw).hexdigest()
                entry['metrics']={}
                for metric in METRICS:
                    try:
                        facts=earnings.extract(raw,[metric])
                        if facts[0]['period']!=f'Q{period[-1]} FY{period[4:6]}':
                            raise ValueError('添付ファイル名とGAAP表の四半期が不一致')
                        entry['metrics'][metric]=dict(fact=facts[0],check=reconcile.compare(raw,facts)[0])
                    except ValueError as exc:
                        entry['metrics'][metric]=dict(error=str(exc))
            except (ValueError,OSError) as exc: entry['error']=str(exc)
        key=bounded[0]+'-'+bounded[-1]
        earnings.atomic(cache(root)/(key+'.json'),json.dumps(result,ensure_ascii=False).encode())
        earnings.atomic(cache(root)/'latest.txt',key.encode())
        return result


def load(root,key=''):
    if not key:
        path=cache(root)/'latest.txt'
        if not path.exists(): return None
        key=path.read_text().strip()
    if not re.fullmatch(r'FY20\d\dQ[1-4]-FY20\d\dQ[1-4]',key): raise ValueError('対象期間が不正です')
    return json.loads((cache(root)/(key+'.json')).read_text())


def rows(result,reg=None):
    out=[]
    for period in requested(result['first'],result['last']):
        found=[e for e in result['entries'] if e['period']==period]
        for i,metric in enumerate(METRICS):
            row=[period,metric,'不足','未実施','未確認','','','','','','','']
            if len(found)!=1:
                row[10]='該当する決算8-Kを特定できません' if not found else '同じ期に複数の提出候補あり'
                if found: row[7]=' / '.join(e['acc'] for e in found)
                if result['unmapped']: row[10]+='（期を特定できない提出候補あり）'
            else:
                e=found[0];row[7:10]=[e['acc'],e['primary'],e['url']]
                item=e.get('metrics',{}).get(metric,{})
                error=e.get('error') or item.get('error')
                if error: row[2],row[10]='抽出失敗',error
                else:
                    f,c=item['fact'],item['check']
                    row[2:4]=['取得済み',c['status']]
                    row[5:7]=[f['value'],f['unit']]
                    row[10:]=[c['reason'],e['sha']]
                    if reg:
                        saved=reg.db.execute('SELECT fp,state FROM metric_review WHERE acc=?',(e['acc'],)).fetchone()
                        if saved==(e['sha'],'reviewed'): row[4]='確認済み'
                        local=reg.db.execute('SELECT state FROM coverage_review WHERE acc=? AND sha=? AND metric=?',
                                             (e['acc'],e['sha'],metric)).fetchone()
                        if local: row[4]=local[0]
            out.append(row)
    return out


def render(root,result,reg,hidden):
    h=escape
    form=('<form method="post" action="/coverage-fetch">'+hidden('token',reg.token)+
          '<label>開始期 <input name="first" placeholder="FY2025Q4"></label> '
          '<label>終了期 <input name="last" placeholder="FY2027Q3"></label> '
          '<label>SEC連絡先 <input type="email" name="contact" required></label> '
          '<button>対象期を取得</button></form><p>両方空欄で最新発表までの8期。取得中はこの画面の応答をお待ちください。</p>')
    if not result: return form
    key=result['first']+'-'+result['last']
    data=rows(result,reg)
    tr=[]
    for row in data:
        cells=''.join('<td>'+h(row[i])+'</td>' for i in (0,1,2,3,4,5,6,10))
        source='<a href="'+h(row[9])+'">SEC添付</a>' if row[9] else ''
        action=''
        if row[2]=='取得済み':
            state='未確認' if row[4]=='確認済み' else '確認済み'
            action=('<form action="/coverage-review" method="post">'+''.join(hidden(k,v) for k,v in
                    dict(token=reg.token,key=key,acc=row[7],sha=row[11],metric=row[1],state=state,old=row[4]).items())
                    +'<button>'+state+'にする</button></form>')
        tr.append('<tr>'+cells+'<td>'+source+action+'</td></tr>')
    table=''.join(tr)
    n=sum(r[2]=='取得済み' for r in data)
    warnings=''.join('<p>対象期未確定　<a href="'+h(e['url'])+'">'+h(e['acc'])+'</a> '+h(e['error'])+'</p>' for e in result['unmapped'])
    return form+f'<h2>{h(key)}</h2><p>期待 {len(data)}項目 ／ 取得 {n} ／ 不足・抽出失敗 {len(data)-n}</p><a href="/coverage.csv?range={key}">全項目CSV</a>'+warnings+'<table><tr>'+''.join('<th>'+h(HEADER[i])+'</th>' for i in (0,1,2,3,4,5,6,10))+'<th>根拠・確認</th></tr>'+table+'</table>'


def review(reg,form):
    if form['state'] not in ('未確認','確認済み'): raise ValueError('確認状態が不正です')
    with reg.db:
        reg.db.execute('BEGIN IMMEDIATE')
        result=load(reg.root,form['key'])
        matches=[r for r in rows(result,reg) if r[2]=='取得済み' and r[7]==form['acc']
                 and r[1]==form['metric'] and r[11]==form['sha'] and r[4]==form['old']]
        if len(matches)!=1: raise ValueError('表示後に更新されました。再読み込みしてください')
        reg.db.execute('INSERT OR REPLACE INTO coverage_review VALUES(?,?,?,?)',
                       (form['acc'],form['sha'],form['metric'],form['state']))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('first','last'): parser.add_argument('--'+arg)
    parser.add_argument('--count',type=int,default=8)
    parser.add_argument('--contact',required=True)
    args=parser.parse_args()
    result=run(ROOT,fetcher(args.contact),args.first,args.last,args.count)
    print(result['first'],result['last'],len(rows(result)),'項目')
