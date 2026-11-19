"""Embed quarterly figures in the bilingual HTML so the demo also works offline."""
import json
from pathlib import Path

MARKER = '<script type="application/json" id="workflow-history">'
TEMPLATE = Path(__file__).parent / 'public_demo' / 'index.html'


def replace_data(html, marker, payload):
    before, middle = html.split(marker, 1)
    _, after = middle.split('</script>', 1)
    data = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('<', r'\u003c')
    return before + marker + data + '</script>' + after


def embed(html, history, dates):
    html = replace_data(html, '<script type="application/json" id="issuer-asof">', dates)
    return replace_data(html, MARKER, history)


def presentation(rows):
    result = {}
    for r in sorted(rows,key=lambda r:(r['symbol'],r['period'])):
        result.setdefault(r['symbol'],[]).append(dict(
            period=r['period'], end=r['end'], releaseDate=r['release_date'], reportDate=r['report_date'],
            candidates=[{k:c[k] for k in ('form','acc','url')} for c in r.get('candidates',[])], accession=r['accession'], filing=r['filing'], url=r['exhibit'], sha=r['sha'],
            quarterly=r['report'], quarterlyAccession=r['report_acc'], quarterlySha=r['report_sha'], form=r['form'],
            state=r.get('state','未確認'), facts=[dict(metric=m['metric'], summaryRow=m['metric'], unit=m['unit'],
                value=m['release'], detail=m['comparison'], detailUnit=m['unit'], row=m['locator'] or m['metric'],
                column=r['end'], status=m['status'], reason=m['reason']) for m in r['metrics']]))
    return result


if __name__ == '__main__':
    seed=json.loads((TEMPLATE.parents[1]/'examples'/'quarter_history.json').read_text())
    TEMPLATE.write_text(embed(TEMPLATE.read_text(), presentation(r for c in seed['issuers'].values() for r in c['rows']),
                              dict.fromkeys(seed['issuers'],seed['asof'])))
