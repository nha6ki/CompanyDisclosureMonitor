"""Reviewed financial metrics and repeatable delivery reports."""
import csv
from datetime import datetime,date
from hashlib import sha256
from html import escape as h
import io
import json
import sqlite3
import reconcile
from zoneinfo import ZoneInfo

HEAD=['種類','提出日','提出番号','項目ID','対象または指標','値または開示内容','単位','対象期間','根拠','原文URL','追加情報']


def initialize(reg):
    reg.db.executescript("""
    CREATE TABLE IF NOT EXISTS coverage_review(acc TEXT,sha TEXT,metric TEXT,state TEXT,PRIMARY KEY(acc,sha,metric));
    CREATE TABLE IF NOT EXISTS metric_review(acc TEXT PRIMARY KEY,fp TEXT,state TEXT,rev INTEGER);
    CREATE TABLE IF NOT EXISTS observed(id TEXT PRIMARY KEY, payload TEXT, changed TEXT);
    CREATE TABLE IF NOT EXISTS issued(id TEXT PRIMARY KEY, payload TEXT);
    CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,created TEXT,data BLOB);
    """)


def now():
    return datetime.now(ZoneInfo('Asia/Tokyo')).isoformat(timespec='seconds')


def metrics(reg,acc):
    d=reg.root/'register_data'/'exhibits'/acc
    if 'metric_error' in reg.docs[acc] or not (d/'facts.json').is_file(): return None
    data=json.loads((d/'facts.json').read_text())
    raw=(d/'99.1.html').read_bytes()
    if data['primary_sha256']!=reg.docs[acc]['record']['sha256'] or sha256(raw).hexdigest()!=data['sha256']:
        raise ValueError('決算資料が変更されています。再取得してください')
    import earnings
    if earnings.extract(raw)!=data['facts']: raise ValueError('保存した数値と原表が不一致です')
    data['checks']=reconcile.cached(raw,json.dumps(data['facts'],sort_keys=True))
    row=reg.db.execute('SELECT fp,state,rev FROM metric_review WHERE acc=?',(acc,)).fetchone()
    return data, row[1] if row and row[0]==data['sha256'] else 'pending', row[2] if row else 0


def metric_page(reg,acc,hidden):
    m=metrics(reg,acc)
    if not m: return ''
    d,state,rev=m
    rows=[]
    for f in d['facts']:
        rows.append('<tr>'+''.join('<td>'+h(v)+'</td>' for v in (f['metric'],f['value'],f['unit'],f['period']))+'</tr>')
    evidence='<p><a href="'+h(d['url'])+'">SEC添付原文を開く</a></p><table><tr>'+''.join('<th>'+h(v)+'</th>' for v in d['facts'][0]['columns'])+'</tr>'+''.join(
        '<tr>'+''.join('<td>'+h(v)+'</td>' for v in fact['row'])+'</tr>' for fact in d['facts'])+'</table>'
    form='<form method="post" action="/metrics">'+''.join(hidden(k,v) for k,v in dict(token=reg.token,acc=acc,fp=d['sha256'],revision=rev).items())
    buttons='<button name="state" value="reviewed">2項目を確認済みにする</button>　<button name="state" value="pending">未確認に戻す</button>'
    return '<h2>決算指標 · Exhibit 99.1</h2><p>'+('確認済み' if state=='reviewed' else '未確認')+'</p><table><tr><th>GAAP指標</th><th>値</th><th>単位</th><th>四半期</th></tr>'+''.join(rows)+'</table><details open><summary>根拠の表（当期と比較列）</summary>'+evidence+'</details>'+reconcile.html(d['checks'])+form+buttons+'</form>'


def save_metrics(reg,form):
    acc=form['acc']
    with reg.db:
        reg.db.execute('BEGIN IMMEDIATE')
        m=metrics(reg,acc)
        if not m or m[0]['sha256']!=form['fp'] or m[2]!=int(form['revision']):
            raise ValueError('表示後に更新されています')
        if form['state'] not in ('reviewed','pending'): raise ValueError('確認状態が不正です')
        reg.db.execute('INSERT OR REPLACE INTO metric_review VALUES(?,?,?,?)',(acc,form['fp'],form['state'],m[2]+1))
        observe(reg)


def observe(reg):
    stamp=now()
    def put(key,row):
        payload=json.dumps(row,ensure_ascii=False)
        reg.db.execute("""INSERT INTO observed VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET
            payload=excluded.payload,changed=excluded.changed WHERE observed.payload!=excluded.payload""",(key,payload,stamp))
    for acc,doc in reg.ordered():
        rec=doc['record']
        for sid,s in doc['sections'].items():
            filing,state,_=reg.current(acc,sid)
            prefix='event/'+acc+'/'+sid+'/'
            alive=set()
            if state=='reviewed':
                for e in filing['events']:
                    key=prefix+e['event_id']
                    alive.add(key)
                    put(key,['出来事',rec['filing_date'],acc,sid+'/'+e['event_id'],e['subject']['value'],e['summary_ja']['value'],'','',
                             ', '.join(map(str,e['summary_ja']['block_numbers'])),rec['document_url'],json.dumps(e,ensure_ascii=False,sort_keys=True)])
            for (key,) in reg.db.execute('SELECT id FROM observed WHERE id LIKE ?',(prefix+'%',)).fetchall():
                if key not in alive: put(key,None)
        m=metrics(reg,acc)
        if m:
            d,state,_=m
            for f in d['facts']:
                key='metric/'+acc+'/'+f['metric']
                row=['決算指標',rec['filing_date'],acc,'EX-99.1',f['metric'],f['value'],f['unit'],f['period'],
                     'GAAP / '+f['metric']+' / '+f['period'],d['url'],'SHA256 '+d['sha256']]
                put(key,row if state=='reviewed' else None)


def csvdata(rows):
    out=io.StringIO(newline='')
    writer=csv.writer(out)
    for row in rows:
        writer.writerow(["'"+v if v.lstrip().startswith(('=','+','-','@')) else v for v in row])
    return out.getvalue().encode('utf-8-sig')


def issue(reg,form):
    dates=[form.get(k,'') for k in ('start','end')]
    for d in dates:
        if d: date.fromisoformat(d)
    if all(dates) and dates[0]>dates[1]: raise ValueError('期間を確認してください')
    with reg.db:
        reg.db.execute('BEGIN IMMEDIATE')
        observe(reg)
        rows=[['変更','確認データ更新日時（JST）',*HEAD]]
        for key,payload,stamp,last in reg.db.execute("""SELECT o.id,o.payload,o.changed,i.payload FROM observed o
            LEFT JOIN issued i ON i.id=o.id ORDER BY o.changed,o.id""").fetchall():
            kind,acc,rest=key.split('/',2)
            if acc not in reg.docs: continue
            if kind=='metric' and not metrics(reg,acc): continue
            if kind=='event' and rest.split('/',1)[0] not in reg.docs[acc]['sections']: continue
            if (dates[0] and stamp[:10]<dates[0]) or (dates[1] and stamp[:10]>dates[1]) or payload==last: continue
            current=json.loads(payload)
            old=json.loads(last) if last else None
            if current is None and old is None: continue
            action='確認解除・削除' if current is None else ('修正' if old else '追加')
            rows.append([action,stamp,*(current or old)])
            reg.db.execute('INSERT OR REPLACE INTO issued VALUES(?,?)',(key,payload))
        if len(rows)==1: raise ValueError('対象期間に未出力の変更はありません')
        return reg.db.execute('INSERT INTO reports(created,data) VALUES(?,?)',(now(),csvdata(rows))).lastrowid


def controls(reg,hidden):
    links=' '.join(f'<a href="/report?id={i}">第{i}回 {h(stamp[:10])}</a>' for i,stamp in reg.db.execute('SELECT id,created FROM reports ORDER BY id DESC LIMIT 20'))
    return ('<h2>差分レポート</h2><form method="post" action="/issue">'+hidden('token',reg.token)+
            '<label>更新日（JST） <input type="date" name="start"></label> ～ <input type="date" name="end" aria-label="終了日"> '
            '<button>未出力の変更をCSVに確定</button></form><p>期間を省略すると全期間。確定済みCSVの再取得　'+links+'</p>')


def notices(reg):
    path=reg.root/'register_data'/'monitor.sqlite3'
    if not path.is_file(): return ''
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='notices'").fetchone(): return ''
        rows=db.execute('SELECT acc,message,updated FROM notices WHERE active ORDER BY updated DESC LIMIT 30').fetchall()
    return '<details><summary>監視通知（直近30件）</summary>'+''.join('<p>'+h(stamp[:16])+' '+h(msg)+' <a href="/filing?acc='+h(acc)+'">'+h(acc)+'</a></p>' for acc,msg,stamp in rows)+'</details>'

def financial_csv(reg):
    rows=[HEAD]
    for acc,doc in reg.ordered():
        entry=metrics(reg,acc)
        if not entry or entry[1]!='reviewed': continue
        for fact in entry[0]['facts']:
            key='metric/'+acc+'/'+fact['metric']
            stored=reg.db.execute('SELECT payload FROM observed WHERE id=?',(key,)).fetchone()
            if stored and stored[0]!='null': rows.append(json.loads(stored[0]))
    return csvdata(rows)


def reconciliation_csv(reg):
    """One result per expected metric, including filings with unavailable exhibits."""
    rows=[]
    statuses=[]
    for acc,doc in reg.ordered():
        record=doc['record']
        if '2.02' not in record.get('api_items',[]): continue
        m=metrics(reg,acc)
        for i,label in enumerate(('Revenue','Diluted earnings per share')):
            if m:
                data,state,_=m
                check=data['checks'][i]
                f=data['facts'][i]
                if check['metric']!=label or f['metric']!=label: raise ValueError('指標の対応が不一致')
                status=check['status']
                fields=[f['period'],f['table'],label,f['period'],f['value'],f['unit'],data['url'],
                        reconcile.STATEMENT,check['row'],check['column'],check['detail'],check['unit'],
                        data['url'],data['sha256'],check['reason']]
            else:
                state='pending'
                status=reconcile.LABELS[3]
                fields=['','','','','','','','','','','','','','',
                        doc.get('metric_error','EX-99.1の概要表を利用できません')]
            statuses.append(status)
            rows.append([acc,record['filing_date'],label,status,'確認済み' if state=='reviewed' else '未確認',
                         record['document_url'],*fields])
    counts=[len(rows),len(rows)-statuses.count(reconcile.LABELS[3]),
            statuses.count(reconcile.LABELS[2])+statuses.count(reconcile.LABELS[3])]
    header=['提出番号','提出日','指標','自動照合判定','担当者確認','8-K原文URL','対象期間','概要表名','概要行','概要列',
            '概要値','概要単位','概要URL','詳細表名','詳細行','詳細列','詳細値','詳細単位','詳細URL','原文SHA256','理由',
            '対象指標数','比較実施数','要確認数']
    return csvdata([header,*[row+list(map(str,counts)) for row in rows]])
