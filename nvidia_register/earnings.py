"""NVIDIA quarterly GAAP summary table and exhibit provenance."""
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
from hashlib import sha256
import json
import os
import tempfile


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.links = []
        self.row_links = []
        self.row = self.cell = self.link = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.link = [attrs.get('href') or '', []]
        if tag == 'table':
            self.depth += 1
            if self.depth == 1:
                self.tables.append([])
        if self.depth == 1:
            if tag == 'tr':
                self.row = []
                self.tables[-1].append(self.row)
            if tag in ('td','th') and self.row is not None:
                self.cell = []

    def handle_endtag(self, tag):
        if tag == 'a' and self.link:
            if self.depth == 1 and self.row is not None:
                self.row_links.append((self.link[0],len(self.tables)-1,len(self.tables[-1])-1))
            self.links.append((self.link[0], ' '.join(''.join(self.link[1]).split())))
            self.link = None
        if self.depth == 1:
            if tag in ('td','th') and self.cell is not None:
                value = ' '.join(''.join(self.cell).split())
                if value: self.row.append(value)
                self.cell = None
            if tag == 'tr':
                self.row = self.cell = None
        if tag == 'table':
            self.depth = max(0, self.depth-1)

    def handle_data(self, data):
        if self.link: self.link[1].append(data)
        if self.cell is not None: self.cell.append(data)


def parse(raw):
    doc = Tables()
    doc.feed(raw.decode('utf-8-sig', errors='replace'))
    doc.close()
    return doc


def extract(raw, labels=('Revenue','Diluted earnings per share')):
    tables = parse(raw).tables
    candidates = [t for t in tables if ['GAAP'] in t and ['Non-GAAP'] not in t
                  and any(len(r)>1 and re.fullmatch(r'Q[1-4] FY\d{2,4}',r[1]) for r in t)]
    if len(candidates) != 1:
        raise ValueError("GAAP専用の表を一意に特定できません")
    rows = candidates[0]
    headers = [r for r in rows if len(r)>3 and re.fullmatch(r'Q[1-4] FY\d{2,4}', r[1])]
    if len(headers) != 1:
        raise ValueError("四半期の列が不明です")
    header = headers[0]
    unit = re.fullmatch(r'\(\$ in (millions|billions), except earnings per share\)',header[0],re.I)
    if not unit:
        raise ValueError("通貨単位を確認してください")
    facts = []
    for label in labels:
        found = [r for r in rows if r and r[0].rstrip('*') == label]
        if len(found)!=1 or len(found[0]) < 4 or not all(re.fullmatch(r'\$[\d,]+(?:\.\d+)?',v) for v in found[0][1:4]):
            raise ValueError(label + " の比較列を確認してください")
        row = found[0]
        facts.append(dict(metric=label,period=header[1],value=row[1][1:].replace(',',''),
                          unit='USD '+unit[1].lower() if label=='Revenue' else 'USD/share',
                          columns=header[:4],row=row[:4],table='GAAP'))
    return facts


def atomic(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as out:
        out.write(raw)
    os.replace(out.name,path)


def collect(root,record,get):
    acc = record['accession_number']
    folder = root/'register_data'/'exhibits'/acc
    primary = next((p for d in ('nvidia_inbox','cache','validation/cache')
                    if (p:=root/d/acc/'primary.html').is_file()),None)
    if not primary: raise ValueError("8-K本文が未取得")
    raw = primary.read_bytes()
    if sha256(raw).hexdigest()!=record['sha256']: raise ValueError("本文のハッシュが不一致")
    doc = parse(raw)
    matches = {url for url,table_no,row_no in doc.row_links
               if re.fullmatch(r'q[1-4]fy\d{2,4}pr\.html?',url,re.I)
               and doc.tables[table_no][row_no][:1]==['99.1']}
    if len(matches)!=1: raise ValueError("決算発表資料99.1のリンクを確認してください")
    url = urljoin(record['document_url'],matches.pop())
    if urlsplit(url).hostname!='www.sec.gov': raise ValueError("SEC以外のURL")
    cache = folder/'99.1.html'
    raw = cache.read_bytes() if cache.exists() else get(url)
    facts = extract(raw)
    if not cache.exists(): atomic(cache,raw)
    data = dict(url=url,sha256=sha256(raw).hexdigest(),primary_sha256=record['sha256'],facts=facts)
    atomic(folder/'facts.json',json.dumps(data,ensure_ascii=False).encode())
    return data
