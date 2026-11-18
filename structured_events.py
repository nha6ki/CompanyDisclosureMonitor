#!/usr/bin/env python3
"""Validate structured disclosure events against frozen source sections. No LLM calls."""
import argparse
from datetime import date
import hashlib
from html import escape
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator
import read_sections

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'structured'
SCHEMA = json.loads((DATA / 'event_schema.json').read_text(encoding='utf-8'))
VERSION = 'nvidia-events-0.1'
ROLE = {'decision': '決定日', 'notice': '通知日', 'agreement': '契約締結日',
        'effective': '発効予定日', 'expected_completion': '完了見込み', 'transition_end': '移行支援の終了予定'}
STATE = {'pending_effective': '発効待ち', 'retirement_announced': '役職退任の意向を通知',
         'agreement_signed_pending_close': '契約締結済み・完了待ち',
         'completed_as_reported': '本文上は完了', 'unclear': '状態を判断できず'}
MONTHS = ('January February March April May June July August September October November December').split()


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def source_contexts(source_dirs):
    """Re-read original bytes using the frozen manifest; do not trust candidate text."""
    selection = load_json(DATA / 'selection.json')
    manifest = load_json(ROOT / 'section_manifest.json')
    rows = {r['accession_number']: r for r in manifest['filings']}
    contexts = {}
    for acc, section_id in selection['sections'].items():
        row = rows[acc]
        result = read_sections.inspect_source(row, source_dirs)
        section = next((s for s in result['sections'] if s['section_id'] == section_id), None)
        contexts[acc] = {'accession_number': acc, 'filing_date': row['filing_date'],
                         'document_url': row['document_url'], 'source_sha256': row['sha256'],
                         'status': result['status'], 'section': section}
    return contexts


def day_supported(value, texts):
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return False
    expressions = (rf'\b{MONTHS[d.month - 1]}\s+0?{d.day},?\s+{d.year}\b',
                   rf'(?<!\d){re.escape(value)}(?!\d)')
    return any(re.search(p, t, re.I) for t in texts for p in expressions)


def period_supported(value, texts):
    year, half = value.split('-H')
    word = 'first' if half == '1' else 'second'
    return any(re.search(rf'\b{word}\s+half\s+(?:of\s+)?{year}\b', t, re.I) for t in texts)


def validate(payload, contexts):
    errors, references, review_items = [], [], []

    def fail(code, path, message):
        errors.append({'code': code, 'path': path, 'message': message})

    for e in Draft202012Validator(SCHEMA).iter_errors(payload):
        fail('schema_error', '/'.join(map(str, e.absolute_path)), e.message)
    if errors:
        return result(errors, references, review_items)
    ids = [r['accession_number'] for r in payload['filings']]
    if len(set(ids)) != len(ids):
        fail('duplicate_filing', 'filings', '同じ提出番号を重複して処理しています。')
    if set(ids) != set(contexts):
        fail('filing_set_mismatch', 'filings', '今回選んだ3資料と入力の提出番号が一致しません。')
    for row in payload['filings']:
        acc = row['accession_number']
        ctx = contexts.get(acc)
        if ctx is None:
            continue
        section = ctx['section']
        if ctx['status'] != 'sections_ready' or section is None:
            fail('source_unavailable', acc, '原文が欠落・変更・不一致、または節の確認が必要です。')
            continue
        for field, actual in [('source_sha256', ctx['source_sha256']),
                              ('section_id', section['section_id']),
                              ('section_sha256', section['text_sha256']),
                              ('as_of_date', ctx['filing_date'])]:
            if row[field] != actual:
                fail('source_binding_mismatch', f'{acc}/{field}', '固定した原文の識別情報と一致しません。')
        blocks = {section['block_start'] + i: b for i, b in enumerate(section['blocks'])}

        def support(fact, path):
            texts = []
            for number in fact['block_numbers']:
                if number not in blocks:
                    fail('invalid_block', path, f'根拠ブロック {number} はこの資料の対象節にありません。')
                else:
                    text = blocks[number]
                    texts.append(text)
                    references.append({'path': path, 'accession_number': acc,
                                       'section_id': section['section_id'], 'block_number': number,
                                       'block_sha256': hashlib.sha256(text.encode()).hexdigest()})
            return texts

        found = row['extraction_status'] == 'events_found'
        if found != bool(row['events']):
            fail('status_event_mismatch', acc, '出来事の有無と処理状態が一致しません。')
        if not found:
            review_items.append({'path': acc, 'reason': '候補なし・要確認は、本文に出来事がないことの証明ではありません。'})
        event_ids = [e['event_id'] for e in row['events']]
        if len(set(event_ids)) != len(event_ids):
            fail('duplicate_event', acc, '同じ資料で出来事IDが重複しています。')
        for event in row['events']:
            base = f"{acc}/{event['event_id']}"
            for field in ('subject', 'organization', 'event_type', 'summary_ja', 'status_as_of_filing'):
                fact = event[field]
                texts = support(fact, f'{base}/{field}')
                if field in ('subject', 'organization') and not any(fact['value'] in t for t in texts):
                    fail('name_not_in_evidence', f'{base}/{field}', '指定された根拠内に氏名・名称の完全一致がありません。')
            if event['notes_ja'] is not None:
                support(event['notes_ja'], f'{base}/notes_ja')
            for i, condition in enumerate(event['conditions']):
                support(condition, f'{base}/conditions/{i}')
            roles = [d['role'] for d in event['dates']]
            if len(set(roles)) != len(roles):
                fail('duplicate_date_role', base, '同じ日付の役割が複数あります。曖昧な場合はnullにして確認事項へ記録してください。')
            for i, d in enumerate(event['dates']):
                path = f'{base}/dates/{i}'
                if d['value'] is None:
                    if d['precision'] is not None or d['certainty'] != 'unknown' or d['block_numbers']:
                        fail('unknown_date_inconsistent', path, '不明日付はvalue・precision=null、certainty=unknown、根拠=[]にします。')
                    continue
                texts = support(d, path)
                if not d['block_numbers']:
                    fail('missing_date_evidence', path, '日付の値には根拠が必要です。')
                if d['certainty'] == 'unknown':
                    fail('known_date_marked_unknown', path, '値を記録した日付のcertaintyはunknownにできません。')
                if d['precision'] == 'day':
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', d['value']) or not day_supported(d['value'], texts):
                        fail('date_not_in_evidence', path, '有効な日付の表記が根拠内に見つかりません。期間から日付を作らないでください。')
                elif d['precision'] == 'half_year':
                    if (d['role'] != 'expected_completion' or not re.fullmatch(r'\d{4}-H[12]', d['value'])
                            or not period_supported(d['value'], texts)):
                        fail('period_not_in_evidence', path, '対象期間を根拠の表現と照合できません。')
                else:
                    fail('invalid_precision', path, '値がある日付にはdayまたはhalf_yearを指定します。')
                if (d['precision'] == 'day' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d['value'])
                        and d['value'] > row['as_of_date'] and d['certainty'] == 'reported'
                        and d['role'] in ('effective', 'transition_end', 'expected_completion')):
                    fail('future_date_marked_reported', path,
                         '提出日より後の発効・移行終了・完了日は、既に起きた日付として扱えません。本文に従いscheduledまたはexpectedにします。')
                if d['role'] in ('decision', 'notice', 'agreement'):
                    if d['precision'] != 'day' or d['certainty'] != 'reported':
                        fail('event_date_role_invalid', path, '今回の意思決定・通知・契約日は、本文に記載された日付として記録します。')
                    elif d['value'] > row['as_of_date']:
                        fail('event_date_after_filing', path, '提出日より後の日を既に起きた決定・通知・契約日として記録しています。')
            if event['status_as_of_filing']['value'] == 'completed_as_reported':
                for d in event['dates']:
                    if d['value'] is not None and d['role'] in ('effective', 'expected_completion'):
                        future = d['precision'] == 'day' and d['value'] > row['as_of_date']
                        if future or d['certainty'] in ('expected', 'scheduled'):
                            fail('completion_conflicts_with_schedule', base, '完了扱いと将来の予定・見込みが矛盾しています。')
            review_items.append({'path': base, 'reason': '人物・役割・日付の対応、要約の意味、条件の十分性は原文と照合して確認が必要です。'})
    return result(errors, references, review_items)


def result(errors, references, review_items):
    return {'validator_version': VERSION, 'mechanical_checks_passed': not errors,
            'semantic_correctness_verified': False, 'human_review_status': 'pending',
            'llm_invocation_performed': False, 'errors': errors,
            'resolved_evidence': references, 'review_items': review_items}


def render(payload, checks, contexts, label):
    def refs(fact):
        return '・'.join(str(n) for n in fact['block_numbers']) or '—'
    cards = []
    filing_count = len(payload.get('filings', []))
    event_count = sum(len(r.get('events', [])) for r in payload.get('filings', []))
    for row in payload.get('filings', []):
        ctx = contexts.get(row.get('accession_number'))
        if ctx is None or not ctx['section']:
            continue
        for event in row.get('events', []):
            dates = []
            for d in event['dates']:
                v = d['value'] or '不明'
                if d['precision'] == 'half_year':
                    v = v[:4] + ('年前半' if v.endswith('H1') else '年後半')
                suffix = {'reported': '本文記載', 'scheduled': '予定', 'expected': '見込み', 'unknown': '未確認'}[d['certainty']]
                dates.append(f'<tr><th>{ROLE[d["role"]]}</th><td>{escape(v)} <small>{suffix}</small></td><td>{refs(d)}</td></tr>')
            condition = ' / '.join(c['value'] for c in event['conditions']) or 'この出力例では条件を記録していません。条件の不存在を確認した意味ではありません。'
            notes = event['notes_ja']['value'] if event['notes_ja'] else ''
            items = [event[k] for k in ('subject', 'organization', 'event_type', 'summary_ja', 'status_as_of_filing')]
            items += event['dates'] + event['conditions'] + ([event['notes_ja']] if event['notes_ja'] else [])
            block_ids = sorted({b for fact in items for b in fact['block_numbers']})
            cards.append(f'''<article class="card"><div class="eyebrow">提出日 {escape(row['as_of_date'])} · Item {escape(ctx['section']['item'])}</div>
<h2>{escape(event['subject']['value'])}</h2><p class="summary">{escape(event['summary_ja']['value'])}</p>
<p><span class="badge">{STATE[event['status_as_of_filing']['value']]}</span> <small>この提出書類に基づく状態</small></p>
<div class="scroll"><table><thead><tr><th>日付の役割</th><th>値</th><th>根拠ブロック</th></tr></thead><tbody>{''.join(dates)}</tbody></table></div>
<p><b>条件：</b>{escape(condition)}</p>{'<p class="note">'+escape(notes)+'</p>' if notes else ''}
<details><summary>原文の参照先と確認事項</summary><p><a href="{escape(ctx['document_url'], quote=True)}" target="_blank" rel="noopener noreferrer">SEC原文を開く ↗</a></p>
<p>提出番号：{escape(row['accession_number'])}<br>本文内のブロック：{', '.join(map(str, block_ids))}</p>
<p>ブロック番号は空白を正規化した可視テキストの位置です。SECページの段落番号・HTML行番号とは異なります。JSONには項目ごとの根拠位置を保持しています。</p>
<p>{escape(' / '.join(row['missing_information']))}</p></details></article>''')
    state = '機械的な検査に通過' if checks['mechanical_checks_passed'] else '機械的な検査で要修正'
    errors = ''.join('<li>' + escape(e['code'] + ': ' + e['message']) + '</li>' for e in checks['errors'])
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>NVIDIA：出来事と日付を整理する</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#223644;font:15px/1.8 system-ui,sans-serif}}main{{max-width:1000px;margin:36px auto;padding:0 22px 50px}}h1{{font-size:30px;line-height:1.4;letter-spacing:-.02em}}h2{{font-size:22px;line-height:1.4;margin:10px 0}}.eyebrow,small,.note{{font-size:13px;color:#566a78}}.eyebrow{{letter-spacing:.04em}}.card,.intro{{background:white;border:1px solid #d9e3e8;border-radius:12px;margin:22px 0;padding:24px}}.intro{{border-left:5px solid #15847c}}.summary{{font-size:16px}}.badge{{display:inline-block;background:#e6f2ef;color:#1c635c;border-radius:5px;padding:3px 10px;font-size:13px;font-weight:650}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;vertical-align:top;border-bottom:1px solid #e1e7eb;padding:9px}}thead{{background:#f3f6f8}}th{{white-space:nowrap}}.scroll{{overflow-x:auto}}a{{color:#086c87}}summary{{cursor:pointer;color:#086c87}}details{{border-top:1px solid #e1e7eb;margin-top:18px;padding-top:12px}}code{{font-size:12px;overflow-wrap:anywhere}}@media(max-width:600px){{main{{padding:0 14px 30px;margin-top:22px}}.card,.intro{{padding:18px}}h1{{font-size:25px}}h2{{font-size:20px}}}}
</style></head><body><main><header><div class="eyebrow">COMPANY DISCLOSURE MONITOR / NVIDIA</div><h1>誰が、何を、いつ行うのか</h1><p>{filing_count}資料の{event_count}つの出来事で、情報の整理方法と検査の動きを確認します。</p></header>
<section class="intro"><b>{escape(label)}</b><p>この画面の内容は、ChatGPTが保存済み原文を読んで作成した参照例です。Pythonは、そのJSONを原文と照合して形式・名称・日付表記・根拠位置・一部の矛盾を検査しました。LLM APIによる自動抽出はまだ実行していません。</p><p><strong>{state} / 意味の確認は未承認</strong></p><p class="note">検査通過は内容の正しさを保証しません。ここにある3資料は方式設計に用いた既知資料です。未知資料での抽出精度は未評価です。</p>{'<ul>'+errors+'</ul>' if errors else ''}</section>
{''.join(cards)}
<section class="card"><h2>今回の機能が役立つ業務</h2><p>文書の情報を決まった列に整理し、元の資料へ戻って確認できる形で渡す機能です。公開募集にも、文書からの情報抽出、正しい企業・契約との紐付け、根拠の保持という要求があります。</p><p class="note"><a href="https://www.upwork.com/freelance-jobs/apply/Senior-Python-Engineer-for-Web-Scraping-Document-Extraction_~022121844619172281293/" target="_blank" rel="noopener noreferrer">確認した公開募集例 ↗</a>（2026-11-17確認）。NVIDIA専用機能への発注需要や、受注実績を確認した意味ではありません。</p></section>
<section class="card"><h2>自動で検査できることと、残る確認</h2><p>別資料への取り違え、存在しない根拠位置、根拠にない氏名や日付、提出日より未来の契約・通知日、一部の「予定なのに完了扱い」を検出します。</p><p>一方、本文に複数ある氏名と役職の正しい対応、日本語要約の意味、条件の漏れはこの検査だけでは判断できません。根拠が存在していても、解釈が正しいとは限りません。</p><p>次は同じ入力・出力形式でLLMを接続し、実際の自動抽出結果をこの参照例と比較します。実行先とモデルは未設定です。</p></section>
<footer class="note">作成日：2026-11-17 JST。提出書類時点の整理であり、予定が実際に実現したかは確認していません。</footer></main></body></html>'''


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'validate'])
    p.add_argument('--source-dir', type=Path, action='append')
    p.add_argument('--candidate', type=Path, default=DATA / 'reference_examples.json')
    p.add_argument('--output-dir', type=Path, default=ROOT / 'structured_output')
    args = p.parse_args(argv)
    dirs = args.source_dir or [ROOT / 'cache', ROOT / 'validation' / 'cache']
    contexts = source_contexts(dirs)
    if args.command == 'prepare':
        bad = {k: v['status'] for k, v in contexts.items() if v['status'] != 'sections_ready' or not v['section']}
        if bad:
            print(json.dumps({'input_prepared': False, 'source_errors': bad})); return 2
        docs = []
        for acc, ctx in contexts.items():
            s = ctx['section']
            docs.append({'accession_number': acc, 'source_sha256': ctx['source_sha256'],
                         'as_of_date': ctx['filing_date'], 'section_id': s['section_id'],
                         'section_sha256': s['text_sha256'], 'item': s['item'],
                         'blocks': [{'block_number': s['block_start'] + i, 'text': b} for i, b in enumerate(s['blocks'])]})
        dump(args.output_dir / 'model_input.json', {'instructions': (DATA / 'extraction_prompt.txt').read_text(encoding='utf-8'),
                                                  'output_schema': SCHEMA, 'documents': docs})
        print(json.dumps({'input_prepared': True, 'filings': len(docs), 'llm_invocation_performed': False})); return 0
    payload = load_json(args.candidate)
    checks = validate(payload, contexts)
    is_reference = args.candidate.resolve() == (DATA / 'reference_examples.json').resolve()
    checks['input_kind'] = 'assistant_prepared_reference' if is_reference else 'unverified_candidate'
    checks['candidate_sha256'] = hashlib.sha256(args.candidate.read_bytes()).hexdigest()
    dump(args.output_dir / 'validation.json', checks)
    if is_reference and not any(e['code'] == 'schema_error' for e in checks['errors']):
        (args.output_dir / 'nvidia_structured_review.html').write_text(
            render(payload, checks, contexts, '参照例と検証処理を作成しました'), encoding='utf-8')
    print(json.dumps({'mechanical_checks_passed': checks['mechanical_checks_passed'],
                      'errors': len(checks['errors']), 'human_review_status': 'pending',
                      'llm_invocation_performed': False}, ensure_ascii=False))
    return 0 if checks['mechanical_checks_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
