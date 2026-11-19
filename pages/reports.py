"""Regenerate the two localized Excel reports after an evidence snapshot update."""
import argparse
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
import json
from urllib.parse import urlsplit, urlunsplit

from manage import ROOT, DATA, digest, inputs, load, lock, write_atomic

FIRMS = dict(AAPL='Apple', AMZN='Amazon', GOOGL='Alphabet', META='Meta', MSFT='Microsoft', NVDA='NVIDIA', TSLA='Tesla')
NAMES = {
    'revenue': ('Revenue','売上高'), 'priorRevenue':('Prior-year quarter revenue','前年同期の売上高'),
    'operating':('Operating income','営業利益'), 'eps':('Diluted EPS','希薄化後EPS'),
    'cash':('Operating cash flow','営業キャッシュフロー'), 'capex':('Capital expenditures','設備投資額'),
}
STATUS = {'一致':'Exact match','丸めの範囲で一致':'Match after rounding','累計差額と一致':'Matches derived quarter',
          '累計差額と差異':'Differs from derived quarter','不一致':'Mismatch','比較値なし':'No quarterly comparison',
          '訂正・重複':'Amendment or duplicate','照合待ち':'Awaiting filing','要確認':'Needs review',
          '抽出失敗':'Extraction issue','添付未特定':'Exhibit not identified','開示未検出':'No filing found'}
RESOLVED = {'一致','丸めの範囲で一致','累計差額と一致'}
def quarter(value):
    return value[:6]+' '+value[6:]


def point(d, key, side, fact):
    item = d['financials'][side].get(key)
    if item: return item
    if not fact: return None
    release = side=='release'
    amount = fact['value' if release else 'detail']
    if amount in (None,''): return None
    value = float(str(amount).replace(',',''))
    return {'value':value,'method':'direct','sources':[{
        'value':value,'url':d['url' if release else 'quarterly'],'form':'Exhibit 99.1' if release else d['form'],
        'column':fact.get('column') or d['end'],'row':fact['summaryRow' if release else 'row']} ]}


def report(symbol, rows, as_of, lang):
    import xlsxwriter
    from xlsxwriter.utility import xl_col_to_name
    buf = BytesIO()
    ja = lang=='ja'
    def t(en,jp): return jp if ja else en
    def metric(k): return NAMES[k][int(ja)]
    def status(value): return value if ja else STATUS[value]
    booklet = xlsxwriter.Workbook(buf, {'in_memory':True,'strings_to_urls':False,'strings_to_formulas':False})
    trend, sources, issues = [booklet.add_worksheet(t(*names)) for names in [
        ('Quarterly trends','四半期推移'),('Sources and calculations','出典・計算根拠'),('Items to review','要確認一覧')]]
    fmt = booklet.add_format
    dark = fmt({'bg_color':'#203B50','font_color':'white','bold':True,'font_size':15,'valign':'vcenter'})
    sub = fmt({'bg_color':'#E7EFF3','font_color':'#334E60','text_wrap':True,'valign':'vcenter'})
    head = fmt({'bg_color':'#36596B','font_color':'white','bold':True,'valign':'vcenter','text_wrap':True})
    plain = fmt({'valign':'vcenter','text_wrap':True})
    number = fmt({'num_format':'#,##0;(#,##0);0'})
    eps = fmt({'num_format':'0.00;(0.00);0.00'})
    pct = fmt({'num_format':'0.0%;(0.0%);0.0%','bg_color':'#E2EFF1'})
    dt = fmt({'num_format':'yyyy-mm-dd'})
    def layout(sh, widths, title, subtitle, headers):
        sh.hide_gridlines(2)
        for i,width in enumerate(widths): sh.set_column(i,i,width)
        for row,text,style,height in [(0,title,dark,32),(1,subtitle,sub,30)]:
            sh.merge_range(row,0,row,len(widths)-1,text,style);sh.set_row(row,height)
        sh.write_row(3,0,headers,head);sh.set_row(3,30);sh.freeze_panes(4,1)
    periods=[quarter(r['period']) for r in rows]
    layout(trend,[34]+[16]*len(rows),FIRMS[symbol]+'  '+t('Quarterly earnings report','四半期決算レポート'),
           ' – '.join([periods[0],periods[-1]])+'     '+t('Data as of ','データ取得日 ')+as_of+'     '+
           t('USD millions, USD/share for EPS, percentages for ratios.','金額は百万米ドル、EPSは米ドル／株、比率は％で表示。'),[t('Metric','指標')]+periods)
    layout(sources,[17,30,18,24,17,17,34,58,21,29],FIRMS[symbol]+'  '+sources.name,
           t('SEC links open the source. Capital expenditures are positive spending.','SEC原文列から該当資料を開けます。設備投資額は支出を正の数で表示します。'),
           [t(*s) for s in [('Quarter','四半期'),('Metric','指標'),('Unit','単位'),('Basis','算出方法'),('Quarter value','四半期値'),('Source amount','参照額'),('Source period','原文の対象期間'),('Original row','原文の行名'),('SEC source','SEC原文'),('Automated check','自動照合')]])
    layout(issues,[17,30,30,18,18,18,17,68],FIRMS[symbol]+'  '+issues.name,
           t('Automated exceptions. Difference = release less periodic report.','自動照合で差異または未確定の項目です。差額は決算発表値から定期報告値を引いた値です。'),
           [t(*s) for s in [('Quarter','四半期'),('Metric','指標'),('Check','照合結果'),('Unit','単位'),('Release','決算発表値'),('Periodic report','定期報告値'),('Difference','差額'),('Review point','確認事項')]])
    positions={'revenue':7,'priorRevenue':8,'operating':10,'eps':12,'cash':14,'capex':15}
    trend.write('A5',t('Quarter end','期末日'))
    for k,r in positions.items(): trend.write('A'+str(r),metric(k))
    for r,label in [(9,t('Revenue growth (YoY)','売上高前年同期比')),(11,t('Operating margin','営業利益率')),(16,t('Free cash flow','フリーキャッシュフロー'))]:
        trend.write('A'+str(r),label)
    source_row, issue_row = 5, 5
    for col,d in enumerate(rows,1):
        xlcol=xl_col_to_name(col)
        trend.write_datetime(4,col,datetime.combine(date.fromisoformat(d['end']),datetime.min.time()),dt)
        facts=dict(zip(('revenue','eps','operating'),d['facts'],strict=True))
        points={'release':{},'report':{}}
        for key in NAMES:
            fact=facts.get(key)
            check=fact['status'] if fact else None
            unit=t('USD/share','米ドル／株') if key=='eps' else t('USD millions','百万米ドル')
            nfmt=eps if key=='eps' else number
            for side in points:
                item=point(d,key,side,fact)
                if not item: continue
                first=source_row
                difference=item['method']=='difference'
                expected=2 if difference else 1
                if len(item['sources'])!=expected: raise ValueError(f'Unexpected source count: {symbol} {d["period"]} {key}')
                for j,e in enumerate(item['sources']):
                    method=t('Direct disclosure','直接開示') if not difference else (t('Cumulative difference','累計値から算出') if not j else t('Subtract previous total','差引く前期累計'))
                    sources.write_row(source_row-1,0,[quarter(d['period']),metric(key),unit,method],plain)
                    sources.write_number('F'+str(source_row),e['value'],nfmt)
                    sources.write_row(source_row-1,6,[e['column'],e['row']],plain)
                    url=urlsplit(e['url'])
                    if url.scheme!='https' or url.netloc!='www.sec.gov' or not url.path.startswith('/Archives/'):
                        raise ValueError('Invalid SEC source')
                    sources.write_url('I'+str(source_row),urlunsplit(url._replace(fragment=e.get('anchor') or url.fragment)),string=e.get('form') or 'SEC')
                    if check and j==0:
                        label=t('Supplemental calculation','参考算出値（照合対象外）') if side=='report' and fact['detail'] is None else status(check)
                        sources.write('J'+str(source_row),label,plain)
                    sources.set_row(source_row-1,36)
                    source_row+=1
                ref='F'+str(first)
                expression=f'{ref}-F{first+1}' if difference else ref
                sources.write_formula('E'+str(first),'='+expression,nfmt,item['value'])
                points[side][key]=(f"'{sources.name}'!E{first}",item['value'])
            release,periodic=points['release'].get(key),points['report'].get(key)
            if fact and fact['detail'] is None: periodic=None
            cash_diff=key in ('cash','capex') and release and periodic and release[1]!=periodic[1]
            if (check and check not in RESOLVED) or cash_diff:
                label=t('Cash flow figure differs','キャッシュフロー数値差異') if cash_diff else status(check)
                issues.write_row(issue_row-1,0,[quarter(d['period']),metric(key),label,unit],plain)
                issues.set_row(issue_row-1,32)
                for letter,item in [('E',release),('F',periodic)]:
                    if item: issues.write_formula(letter+str(issue_row),'='+item[0],nfmt,item[1])
                if release and periodic:
                    issues.write_formula('G'+str(issue_row),f'=E{issue_row}-F{issue_row}',nfmt,release[1]-periodic[1])
                msg=t('Compare the source amounts and periods.','出典シートで両資料の参照額と対象期間を確認してください。') if periodic else t('Check the periodic filing for a comparable quarter.','定期報告書の有無と四半期単独の開示値を確認してください。')
                issues.write('H'+str(issue_row),msg,plain);issue_row+=1
        selected={}
        for key,r in positions.items():
            item=points['release'].get(key)
            if key not in ('eps','revenue'): item=item or points['report'].get(key)
            if item:
                selected[key]=item
                trend.write_formula(xlcol+str(r),'='+item[0],eps if key=='eps' else number,item[1])
        for r,a,b in [(9,'revenue','priorRevenue'),(11,'operating','revenue'),(16,'cash','capex')]:
            pair=next((s for s in points.values() if a in s and b in s),None)
            if not pair: continue
            x,y=pair[a][1],pair[b][1]
            ax,bx=[xlcol+str(positions[k]) if pair[k]==selected.get(k) else pair[k][0] for k in (a,b)]
            if r==16: expression=f'{ax}-{bx}'; value=x-y
            else:
                expression=f'IF({bx}=0,"",{ax}/{bx}'+('-1' if r==9 else '')+')'
                value=(x/y-(1 if r==9 else 0)) if y else ''
            trend.write_formula(xlcol+str(r),'='+expression,number if r==16 else pct,value)
    notes={'NVDA':t(' Includes intangible asset purchases.',' NVIDIAは無形資産の取得を含みます。'),
           'TSLA':t(' Scope follows the filing for each period.',' Teslaは各期の開示範囲に従います。'),
           'AMZN':t(' Gross purchases before proceeds and incentives.',' Amazonは売却収入・インセンティブ受取前の購入額です。')}
    trend.merge_range(17,0,17,len(rows),t('Capital expenditures are positive spending.','設備投資額は支出を正の数で表示します。')+notes.get(symbol,''),plain)
    trend.merge_range(18,0,18,len(rows),t('Free cash flow = operating cash flow less capital expenditures. Blank cells indicate unavailable inputs.','フリーキャッシュフローは営業キャッシュフローから設備投資額を引いた算出値です。空欄は必要な開示値がない項目です。'),plain)
    for r in (17,18): trend.set_row(r,30)
    if issue_row==5: issues.write('A5',t('No automated comparison exceptions.','自動照合の要確認項目はありません。'))
    booklet.close()
    return buf.getvalue()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--company',choices=sorted(FIRMS))
    args=parser.parse_args()
    with lock('reports'):
        history,dates=inputs()
        manifest=load(DATA/'reports.json')
        for s in ([args.company] if args.company else history):
            entry={'input':digest([dates[s],history[s]])}
            for lang in ('ja','en'):
                blob=report(s,history[s],dates[s],lang)
                entry[lang]=sha256(blob).hexdigest()
                write_atomic(ROOT/'reports'/f'{s}-{lang}.xlsx',blob)
            manifest[s]=entry
        write_atomic(DATA/'reports.json',(json.dumps(manifest,indent=2)+'\n').encode())


if __name__=='__main__':
    main()
