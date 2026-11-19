"""Extract the 8-K disclosures actually selectable on all seven local Pages ledgers."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import shutil

import daily
import daily_highlights as highlights
import llm_runner
import manage
from narratives import metadata, section


def read_prior(path, targets):
    """Bind genuine historical model responses to the frozen source and section manifest."""
    seed = manage.load(path)
    frozen = manage.load(daily.PROJECT / 'section_output/section_index.json')['filings']
    original = {r['accession_number']: r for r in frozen}
    result = {}
    for item in seed:
        doc, output = item['document'], item['response']
        acc = doc['accession_number']
        record = targets.get('NVDA/' + acc)
        if not record:
            continue
        ref = original[acc]
        if (output.get('model') != highlights.MODEL or doc['source_sha256'] != ref['sha256']
                or ref['observed_sha256'] != ref['sha256'] or ref['status'] != 'sections_ready'
                or record['document_url'] != ref['document_url'] or record['filing_date'] != ref['filing_date']
                or doc['as_of_date'] != ref['filing_date']):
            raise ValueError('保存済みのモデルまたは原文情報が一致しません: ' + acc)
        if record['api_items'] and set(record['api_items']) != set(ref['api_items']):
            continue
        relevant = [s for s in ref['sections'] if s['item'] != '9.01']
        if len(relevant) != 1:
            raise ValueError('既存の結果で全項目を確認できません: ' + acc)
        sec = relevant[0]
        blocks = doc['blocks']
        numbers = list(range(sec['block_start'], sec['block_end_inclusive'] + 1))
        texts = [b['text'] for b in blocks]
        if (not sec['heading_recognized'] or doc['item'] != sec['item'] or doc['section_id'] != sec['section_id']
                or [b['block_number'] for b in blocks] != numbers
                or sha256('\n'.join(texts).encode()).hexdigest() != sec['text_sha256']
                or doc['section_sha256'] != sec['text_sha256']):
            raise ValueError('保存済みの根拠段落が一致しません: ' + acc)
        ctx = dict(status='sections_ready', section=dict(sec, blocks=texts), source_sha256=ref['sha256'],
                   filing_date=record['filing_date'], document_url=ref['document_url'])
        payload = llm_runner.parse_response(output)
        if not llm_runner.events.validate(payload, {acc: ctx})['mechanical_checks_passed']:
            raise ValueError('既存要点の根拠照合に失敗しました: ' + acc)
        filing = payload['filings'][0]
        if filing['extraction_status'] != 'events_found':
            raise ValueError('有効な既存候補がありません: ' + acc)
        saved = section(filing, doc, 'pending')
        result['NVDA/' + acc] = dict(record=record, version=ref['sha256'], state='done', notes=[],
            candidate=dict(metadata(record), sections={sec['section_id']: saved}))
    return result


def run(contact, prior=None, retry=False, prepare=False):
    # Use the daily task's lock, queue and evidence cache so the two entry points can resume each other.
    with daily.locked(daily.PRIVATE):
        with manage.lock('reports'), manage.lock('reviews'):
            applied = daily.PRIVATE / 'applied'
            if applied.exists():
                shutil.rmtree(applied)
            daily.recover(daily.PRIVATE)
        manage.build()
        targets = highlights.ledger_records((manage.OUTPUT / 'index.html').read_text(), manage.inputs()[0])
        queue_path = daily.PRIVATE / 'queue.json'
        queue = manage.load(queue_path) if queue_path.is_file() else {}
        seed = read_prior(prior, targets) if prior else {}
        jobs = {}
        for key, record in targets.items():
            old = queue.get(key)
            # A quarter-only fallback has no Item list. Keep richer verified SEC metadata when available.
            if old and not record['api_items']:
                record['api_items'] = old['record']['api_items']
            matched = old and old['record'] == record
            if not matched or (key in seed and old['state'] == 'pending'):
                job = seed.get(key)
                if job and job['record'] == record:
                    queue[key] = job
                else:
                    queue[key] = dict(record=record, state='pending')
            jobs[key] = queue[key]
        summary = dict(checked_at=datetime.now(timezone.utc).isoformat(), errors={}, incidents={})
        def checkpoint():
            daily.save(queue_path, queue)
            highlights.index_page(daily.PRIVATE, queue, summary)
            counts = Counter(j['state'] for j in jobs.values())
            print(f"対象 {len(jobs)}件 · 処理済 {counts['done']} · 添付一覧のみ {counts['attachment']} · "
                  f"要確認 {counts['manual']} · 未処理 {counts['pending'] + counts['retry']}", flush=True)
        checkpoint()
        if not prepare:
            sources = daily.Sources(daily.pipeline.fetcher(contact), daily.PROJECT/'register_data/quarter_sources')
            highlights.process_queue(daily.PRIVATE, jobs, sources, retry, checkpoint, summary)
            checkpoint()
        highlights.update_public_states(queue)
        for key, error in summary['errors'].items():
            print(f'{key}: {error}', flush=True)
        return int(bool(summary['errors']) or (not prepare and any(
            j['state'] not in ('done', 'attachment') for j in jobs.values())))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--contact')
    mode.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--prior', type=Path)
    parser.add_argument('--retry-review', action='store_true')
    args = parser.parse_args()
    raise SystemExit(run(args.contact, args.prior, args.retry_review, args.prepare_only))
