"""Shared quarterly evidence, exception review and export."""
from datetime import date
from html import escape
import json
from urllib.parse import urlencode

from companies import ISSUERS
import pipeline
from reporting import csvdata


def filters(query):
    f = {k:query.get(k, [''])[0] for k in ('symbol','status','state','start','end')}
    for key, choices in [('symbol',ISSUERS),('status',('例外すべて','一致',*pipeline.PROBLEMS)),
                         ('state',(*pipeline.DECISIONS,'再確認'))]:
        if f[key] and f[key] not in choices:
            raise ValueError('絞り込み条件が不正です')
    for k in ('start','end'):
        if f[k] and date.fromisoformat(f[k]).isoformat()!=f[k]:
            raise ValueError('日付はYYYY-MM-DDで指定してください')
    if f['start'] and f['end'] and f['start']>f['end']:
        raise ValueError('開始日が終了日を超えています')
    return f


def selected(db,f):
    rows = []
    for r in pipeline.records(db):
        if f['symbol'] and r['symbol']!=f['symbol'] or f['state'] and r['state']!=f['state']:
            continue
        if f['start'] and r['end']<f['start'] or f['end'] and r['end']>f['end']:
            continue
        metrics=[m for m in r['metrics'] if not f['status'] or m['status']==f['status']
                 or f['status']=='例外すべて' and m['status'] in pipeline.PROBLEMS]
        if metrics:
            rows.append(dict(r,metrics=metrics))
    return sorted(rows,key=lambda r:(r['end'],r['symbol']),reverse=True)


def export(rows):
    data=[['企業','四半期','期末日','期末日推定','指標','単位','決算発表値','定期報告値','照合結果','理由','根拠（タグ・期間）',
           '8-K提出日','8-K提出番号','8-K本文','決算添付','添付SHA256','定期報告種別','定期報告提出日','定期報告提出番号',
           '定期報告URL','定期報告SHA256','候補書類','担当者確認','確認者','メモ','最終確認時刻UTC','照合版']]
    for r in rows:
        for m in r['metrics']:
            data.append([r['symbol'],r['period'],r['end'],'推定' if r['estimated'] else '',m['metric'],m['unit'],
                m['release'] or '',m['comparison'] or '',m['status'],m['reason'],m['locator'],r['release_date'],r['accession'],
                r['filing'],r['exhibit'],r['sha'],r['form'],r['report_date'],r['report_acc'],r['report'],r['report_sha'],
                ' | '.join(c['url'] for c in r['candidates']),r['state'],r['reviewer'],r['note'],r['stamp'],r['version']])
    return csvdata(data)


def options(choices, current, all_label=None):
    items = ([('',all_label)] if all_label else []) + [(key,key) for key in choices]
    return ''.join(f'<option value="{escape(key)}"'+(' selected' if key==current else '')+
                   '>'+escape(label)+'</option>' for key,label in items)


def link(url,title):
    return f'<a href="{escape(url)}">{escape(title)}</a>' if url else ''


def render(db,f,rows,hidden,token):
    from companies import narrative_url
    controls = ''.join('<label>'+title+' <select name="'+k+'">'+options(values,f[k],'すべて')+'</select></label> '
        for k,title,values in [('symbol','企業',ISSUERS),('status','照合',('例外すべて','一致',*pipeline.PROBLEMS)),
                               ('state','担当者確認',(*pipeline.DECISIONS,'再確認'))])
    for k,title in [('start','期末日（開始）'),('end','期末日（終了）')]:
        controls+=f'<label>{title} <input type="date" name="{k}" value="{escape(f[k])}"></label> '
    query=urlencode({k:v for k,v in f.items() if v})
    content=['<p>各社の直近8四半期について、決算発表と定期報告書から売上高・希薄化後EPSを照合します。</p>',
        '<form method="get">'+controls+'<button>絞り込む</button></form>',
        f'<p>{len(rows)}四半期 · {sum(len(r["metrics"]) for r in rows)}指標　'+link('/workflow.csv?'+query,'CSVをダウンロード')+'</p>',
        '<div style="overflow-x:auto"><table><thead><tr><th>企業・対象期</th><th>指標と単位</th><th>決算発表</th><th>定期報告</th><th>照合結果・理由</th><th>担当者確認</th></tr></thead><tbody>']
    for r in rows:
        company=link('/company/'+r['symbol']+'?lang=ja',r['symbol'])
        sources=' · '.join(filter(None,[link(r['filing'],'8-K'),link(r['exhibit'],'添付99.1'),link(r['report'],r['form'])]))
        if r['accession']:
            sources += ' · '+link(narrative_url(r['symbol'],r['accession']),'この8-Kの要点')
        if r['candidates']:
            sources+=' <details><summary>提出候補</summary>'+ ' · '.join(link(c['url'],c['form']+' '+c['acc']) for c in r['candidates'])+'</details>'
        fields=dict(token=token,symbol=r['symbol'],period=r['period'],version=r['version'],previous=r['previous'],back=query)
        editor=('<details><summary>'+escape(r['state'])+'</summary><form method="post" action="/workflow-review">'+
                ''.join(hidden(k,v) for k,v in fields.items())+
                '<label>判定 <select name="state">'+options(pipeline.DECISIONS,r['state'] if r['state'] in pipeline.DECISIONS else '未確認')+
                '</select></label><label>確認者 <input required maxlength="100" name="reviewer" value="'+escape(r['reviewer'])+
                '"></label><label>メモ <textarea maxlength="1500" name="note">'+escape(r['note'])+
                '</textarea></label><button>四半期の確認を保存</button></form></details>')
        n=len(r['metrics'])
        for idx,m in enumerate(r['metrics']):
            cells=[f'<td rowspan="{n}">{company}<br>{r["period"]}<br>{r["end"]}'+('（推定）' if r['estimated'] else '')+'<br>'+sources+'</td>'] if idx==0 else []
            cells += ['<td>'+escape(x)+'</td>' for x in (('売上高' if m['metric']=='Revenue' else '希薄化後EPS')+' · '+m['unit'],
                         m['release'] or '—',m['comparison'] or '—',m['status']+' '+m['reason'])]
            if idx==0:
                cells.append(f'<td rowspan="{n}">'+editor+'</td>')
            content.append('<tr>'+''.join(cells)+'</tr>')
    content.append('</tbody></table></div>')
    issues=[]
    for row in db.execute('SELECT data FROM incidents ORDER BY symbol,accession DESC'):
        i=json.loads(row[0])
        if not f['symbol'] or i['symbol']==f['symbol']:
            issues.append('<li>'+escape(i['symbol']+' '+i['day']+' '+i['reason'])+' '+link(i['url'],i['acc'])+'</li>')
    content.append('<details><summary>四半期を特定できない提出（企業条件のみ） · '+str(len(issues))+'件</summary><ul>'+''.join(issues)+'</ul></details>')
    history=[]
    periods={(r['symbol'],r['period']) for r in rows}
    for item in db.execute('SELECT symbol,period,state,reviewer,note,stamp FROM decisions ORDER BY id DESC'):
        if (item[0],item[1]) in periods:
            history.append('<li>'+escape(' · '.join(item))+'</li>')
    content.append('<details><summary>表示期間の確認履歴 · '+str(len(history))+'件</summary><ul>'+''.join(history)+'</ul></details>')
    content.append('<h2>SECから更新</h2><ul>')
    for sym,asof,attempted,error in db.execute('SELECT * FROM syncs ORDER BY symbol'):
        content.append('<li>'+escape(sym+'　最終取得 '+(asof or '未取得')+('　更新失敗 '+attempted+' '+error if error else ''))+'</li>')
    content.append('</ul><form method="post" action="/workflow-sync">'+hidden('token',token)+
            '<label>SEC取得用の連絡先 <input type="email" name="contact" required></label> <button>7社を更新</button></form>')
    return ''.join(content)
