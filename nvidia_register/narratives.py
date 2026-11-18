"""Read the filing register and project saved narrative evidence into the dashboard."""
import json
from pathlib import Path

import companies
import demo_history
import llm_runner
from nvidia_qwen27b_review import MODEL

MARKER = '<script type="application/json" id="narrative-filings">'
SNAPSHOT = 'qwen_ledger_snapshot.json'


def section(filing, source, state):
    keys = ('accession_number', 'section_id', 'source_sha256', 'section_sha256', 'as_of_date')
    if any(filing[k] != source[k] for k in keys):
        raise ValueError('抽出結果と根拠資料の識別子が一致しません')
    references = set()
    for event in filing['events']:
        fields = [event[k] for k in ('subject', 'event_type', 'organization', 'summary_ja',
                                      'status_as_of_filing', 'notes_ja')]
        for field in [*fields, *event['conditions'], *event['dates']]:
            if field:
                references.update(field['block_numbers'])
    blocks = [b for b in source['blocks'] if b['block_number'] in references]
    if references != {b['block_number'] for b in blocks}:
        raise ValueError('根拠段落が見つかりません')
    return dict(id=source['section_id'], item=source['item'], state=state, model=MODEL,
                sourceHash=source['source_sha256'], sectionHash=source['section_sha256'],
                events=filing['events'], count=len(filing['events']), blocks=blocks,
                missing=filing['missing_information'])


def metadata(record):
    return dict(date=record['filing_date'], form=record.get('form', '8-K'), url=record['document_url'],
                items=record['api_items'], sections={})


def collect(root, reg=None):
    filings = {symbol: {r['accession_number']: metadata(r) for r in batch['rows']}
               for symbol, batch in companies.load(root).items()}
    coverage = Path(__file__).parent / 'examples' / 'narrative_coverage.json'
    for path in (coverage, root / 'register_data' / SNAPSHOT):
        if not path.is_file():
            continue
        snapshot = json.loads(path.read_text(encoding='utf-8'))
        for symbol, batch in snapshot.items():
            for acc, record in batch.items():
                filings[symbol][acc] = record
    for acc, doc in getattr(reg, 'docs', {}).items():
        record = doc['record']
        if record.get('ticker') != 'NVDA' or 'api_items' not in record:
            continue
        entry = filings['NVDA'].setdefault(acc, metadata(record))
        entry.update({k:v for k,v in metadata(record).items() if k != 'sections'})
        # A revised source invalidates any earlier evidence from the saved snapshot.
        entry['sections'] = {sid:s for sid,s in entry['sections'].items()
                             if s['sourceHash'] == record.get('sha256')}
        for sid, saved in doc['sections'].items():
            if saved.get('model') != MODEL:
                continue
            current, state, _ = reg.current(acc, sid)
            source = llm_runner.document_input(acc, saved['ctx'])
            if state == 'changed':
                current = saved['original']
            entry['sections'][sid] = section(current, source, state)
    return filings


def embed(html, filings, *, include_pending=False):
    # Remove unreviewed text from the actual distributable, including embedded JSON.
    projected = {}
    for symbol, batch in filings.items():
        projected[symbol] = {}
        for acc, filing in batch.items():
            sections = {}
            for sid, saved in filing['sections'].items():
                if include_pending or saved['state'] == 'reviewed':
                    sections[sid] = saved
                else:
                    sections[sid] = {k:saved[k] for k in ('id', 'item', 'state', 'count', 'model', 'sourceHash', 'sectionHash')}
            projected[symbol][acc] = dict(filing, sections=sections)
    return demo_history.replace_data(html, MARKER, projected)
