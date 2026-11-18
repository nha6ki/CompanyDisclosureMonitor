"""Embed quarterly figures in the bilingual HTML so the demo also works offline."""
import json
from pathlib import Path

MARKER = '<script type="application/json" id="workflow-history">'
TEMPLATE = Path(__file__).parent / 'public_demo' / 'index.html'


def embed(html, history):
    before, middle = html.split(MARKER, 1)
    _, after = middle.split('</script>', 1)
    data = json.dumps(history, ensure_ascii=False, separators=(',', ':')).replace('<', r'\u003c')
    return before + MARKER + data + '</script>' + after


def presentation(rows):
    result = {}
    for r in sorted(rows,key=lambda r:(r['symbol'],r['period'])):
        result.setdefault(r['symbol'],[]).append(dict(
            period=r['period'], end=r['end'], accession=r['accession'], filing=r['filing'], url=r['exhibit'], sha=r['sha'],
            quarterly=r['report'], quarterlyAccession=r['report_acc'], quarterlySha=r['report_sha'], form=r['form'],
            state=r.get('state','未確認'), facts=[dict(metric=m['metric'], summaryRow=m['metric'], unit=m['unit'],
                value=m['release'], detail=m['comparison'], detailUnit=m['unit'], row=m['locator'] or m['metric'],
                column=r['end'], status=m['status'], reason=m['reason']) for m in r['metrics']]))
    return result


if __name__ == '__main__':
    seed=json.loads((TEMPLATE.parents[1]/'examples'/'quarter_history.json').read_text())
    TEMPLATE.write_text(embed(TEMPLATE.read_text(), presentation(r for c in seed['issuers'].values() for r in c['rows'])))
