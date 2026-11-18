"""Cross-check two GAAP figures within NVIDIA's EX-99.1 using the physical table grid."""
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser
from html import escape
from functools import lru_cache
import re

LABELS = ('一致', '丸めの範囲で一致', '不一致', '比較資料なし')
STATEMENT = 'CONDENSED CONSOLIDATED STATEMENTS OF INCOME'
ORDINALS = ['first', 'second', 'third', 'fourth']


class Grid(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.depth = 0
        self.outside = []
        self.cell = None
        self.row = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            if not self.depth:
                self.tables.append((' '.join(' '.join(self.outside).split()), []))
                self.outside = []
            self.depth += 1
        if self.depth != 1:
            return
        if tag == 'tr':
            self.row = []
            self.tables[-1][1].append(self.row)
            self.pos = 0
        if tag in ('td', 'th') and self.row is not None:
            attrs = dict(attrs)
            if attrs.get('rowspan','1') != '1':
                self.row.append((-1,-1,'ROWSPAN'))
            self.span = int(attrs.get('colspan','1'))
            self.cell = []

    def handle_endtag(self, tag):
        if self.depth == 1 and tag in ('td','th') and self.cell is not None:
            value = ' '.join(''.join(self.cell).split())
            if value:
                self.row.append((self.pos,self.pos+self.span,value))
            self.pos += self.span
            self.cell = None
        if tag == 'tr' and self.depth == 1:
            self.row = None
        if tag == 'table':
            self.depth = max(0,self.depth-1)

    def handle_data(self, value):
        if self.depth == 0:
            self.outside.append(value)
        elif self.cell is not None:
            self.cell.append(value)


def one(items, message):
    values=list(items)
    if len(values)!=1:
        raise ValueError(message)
    return values[0]


def detail_table(raw, period):
    doc=Grid()
    doc.feed(raw.decode('utf-8-sig',errors='replace'))
    q, fy = period.split()
    ordinal=ORDINALS[int(q[1])-1]
    year=fy[2:]
    heading=doc.tables[0][0] if doc.tables else ''
    if not re.search(rf'{ordinal} Quarter (?:and )?Fiscal (?:20)?{year}\b',heading,re.I):
        raise ValueError('決算表の四半期と資料表題が一致しません')
    end=one(set(re.findall(rf'{ordinal} quarter ended ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})',heading,re.I)),
            '当四半期の期末日を特定できません')
    context,rows=one(((c,r) for c,r in doc.tables if STATEMENT in c.upper() and
                      any(cell[2]=='Three Months Ended' for row in r for cell in row)),
                     '連結損益計算書の四半期表を一意に特定できません')
    caption=context.upper().rsplit(STATEMENT,1)[1]
    if not re.match(r'\s*\(IN MILLIONS, EXCEPT PER SHARE DATA\)',caption):
        raise ValueError('連結損益計算書の単位を確認できません')
    if any(cell[2]=='ROWSPAN' for row in rows for cell in row):
        raise ValueError('結合された行の範囲を要確認')
    i,left,right=one(((i,a,b) for i,row in enumerate(rows) for a,b,v in row if v=='Three Months Ended'),
                     '3か月列を一意に特定できません')
    headers=[r for r in rows[i+1:] if r][:2]
    if len(headers)!=2: raise ValueError('期末日の列見出しが不足しています')
    lo,hi=one(((a,b) for a,b,v in headers[0] if left<=a<b<=right
               and any(c==a and d==b and v+' '+y==end for c,d,y in headers[1])),
              '3か月列に当四半期の期末日がありません')
    return rows,lo,hi,end


def compare(raw,facts):
    try:
        rows,lo,hi,end=detail_table(raw,facts[0]['period'])
    except ValueError as e:
        return [dict(metric=f['metric'],status=LABELS[3],reason=str(e),detail='',unit='',column='',row='') for f in facts]
    results=[]
    for f in facts:
        eps=f['metric']=='Diluted earnings per share'
        label='Net income per share / Diluted' if eps else 'Revenue'
        result=dict(metric=f['metric'],row=label,column='Three Months Ended '+end,
                    unit='USD/share' if eps else 'USD millions',detail='')
        try:
            candidates=[]
            section=''
            for row in rows:
                if not row: continue
                name=row[0][2]
                if len(row)==1: section=name.rstrip(':')
                if (eps and section=='Net income per share' and name=='Diluted') or (not eps and name=='Revenue'):
                    candidates.append(row)
            row=one(candidates,'詳細表の対象行を一意に特定できません')
            value=one((v for a,b,v in row if lo<=a<b<=hi and re.fullmatch(r'\d+(?:,\d{3})*(?:\.\d+)?',v)),
                      '対象列の値を特定できません')
            result['detail']=value
            amount=Decimal(value.replace(',',''))
            if f['unit']=='USD billions': amount/=1000
            summary=Decimal(f['value'])
            rounded=amount.quantize(Decimal(1).scaleb(summary.as_tuple().exponent),rounding=ROUND_HALF_UP)
            status=LABELS[0] if amount==summary else LABELS[1] if rounded==summary else LABELS[2]
            result.update(status=status,reason=f"詳細値 {amount} {f['unit']}、概要表示の精度では {rounded}。概要値 {summary}。")
        except ValueError as e:
            result.update(status=LABELS[3],reason=str(e))
        results.append(result)
    return results


@lru_cache(maxsize=24)
def cached(raw, packed):
    import json
    return compare(raw,json.loads(packed))


def html(checks):
    rows=[]
    for c in checks:
        rows.append('<tr>'+''.join('<td>'+escape(c[k])+'</td>' for k in
                    ('metric','status','detail','unit','row','column','reason'))+'</tr>')
    return ('<h3>連結損益計算書との照合</h3><p>同じEX-99.1内のGAAP概要表と連結損益計算書を比較。</p>'
            '<table><tr><th>指標</th><th>判定</th><th>詳細値</th><th>単位</th><th>詳細表の行</th><th>対象列</th><th>理由</th></tr>'
            +''.join(rows)+'</table>')
