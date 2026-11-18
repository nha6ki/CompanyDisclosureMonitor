"""Replay the five additional filings with the unchanged extraction rules."""
from pathlib import Path
import argparse
import hashlib
from html import escape
import json
import os
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import extract_evidence as engine


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(results, notes, assessments, hashes_ok):
    count = sum(r['candidate_count'] > 0 for r in results)
    quotes = sum(r['candidate_count'] for r in results)
    valid_sources = sum(r['status'] in ('candidates_found', 'no_candidate') for r in results)
    source_note = (f'今回の実行で得た{quotes}候補は原文の記録位置と一致しています。2026年11月17日の原文確認では、4候補のラベルと段落内容の対応も確認しました。'
                   if valid_sources == len(results) else
                   '今回は一部の原文を確認できていないため、全5件についての内容評価は完了していません。未取得・原文変更を「出来事なし」として扱いません。')
    cards, rows = [], []
    for i, r in enumerate(results, 1):
        acc = r['accession_number']; n = notes[acc]; a = assessments[acc]
        has_valid_source = r['status'] in ('candidates_found', 'no_candidate')
        live_status = f'候補あり：{r["candidate_count"]}箇所' if r['candidate_count'] else ('候補なし' if has_valid_source else '原文を確認できず')
        outcome = a['verdict'] if has_valid_source else r.get('error', '資料を確認できません')
        rows.append(f'<tr><th scope="row">{r["filing_date"]}</th><td>{escape(n["title"])}</td><td>{escape(outcome)}</td></tr>')
        candidate_html = ''
        for c in r['candidates']:
            q = ('… ' if c['leading_omission'] else '') + c['quote'] + (' …' if c['trailing_omission'] else '')
            candidate_html += f'<p><b>{escape(c["label"])}</b> ／ Item {escape(c["item"])}</p><blockquote lang="en">{escape(q)}</blockquote>'
        if not candidate_html:
            candidate_html = '<p>' + escape(live_status) + '。これを「出来事なし」とは扱いません。</p>'
        manual = ''
        if a['manual_quote'] and has_valid_source:
            manual = f'<div class="manual"><p><b>原文確認で見つけた根拠（自動抽出の結果ではありません）</b></p><p class="note">{escape(a["manual_quote_location"])}</p><blockquote lang="en">{escape(a["manual_quote"])}</blockquote></div>'
        comment = a['reason'] if has_valid_source else '前回の原文と同一と確認できないため、この実行についての内容評価を保留します。'
        cards.append(f'''<article class="card" id="filing-{i}"><p class="eyebrow">{i:02d} ／ {r['filing_date']} ／ SEC登録項目 {escape(', '.join(r['api_items']))}</p><h2>{escape(n['title'])}</h2><p><b>原文の内容：</b>{escape(n['summary'])}</p><p class="note">原文確認箇所：{escape(n['source_location'])}。この日本語説明はChatGPTによる確認メモです。</p><div class="auto"><h3>変更していないPython処理の結果</h3>{candidate_html}</div>{manual}<div class="finding"><h3>照合結果：{escape(outcome)}</h3><p>{escape(comment)}</p><p><b>候補の対象：</b>{escape(a['caught']) if has_valid_source else '未判定'}</p><p><b>残る確認事項：</b>{escape(a['missed']) if has_valid_source else '未判定'}</p></div><p><a href="{escape(r['document_url'], quote=True)}" target="_blank" rel="noopener noreferrer">SECの8-K原文を開く ↗</a></p></article>''')
    integrity = '変更なしを確認' if hashes_ok else '変更を検出'
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NVIDIA：別の5件で抽出ルールを検証</title><style>
body{{max-width:1000px;margin:34px auto;padding:0 20px 40px;background:#f6f8fa;color:#1d3342;font:15px/1.8 system-ui,sans-serif}}h1{{font-size:28px;line-height:1.4}}h2{{font-size:21px;line-height:1.5;margin:0 0 12px}}h3{{font-size:15px;margin:0 0 10px}}a{{color:#096f83}}.eyebrow{{font-size:12px;color:#526b79;font-weight:700;letter-spacing:.04em}}.note{{font-size:13px;color:#526b79}}.card{{background:white;border:1px solid #d7e1e6;border-radius:10px;padding:22px;margin:22px 0}}.intro{{border-left:4px solid #16837d}}.auto,.manual,.finding{{padding:16px 18px;margin:16px 0;border-radius:8px}}.auto{{background:#edf5f8;border-left:3px solid #137d84}}.manual{{background:#f4f5f6;border-left:3px solid #7a8990}}.finding{{background:#fff8e9;border-left:3px solid #bb8a35}}.auto p,.manual p,.finding p{{margin:7px 0}}blockquote{{font:14px/1.7 ui-monospace,monospace;margin:8px 0;overflow-wrap:anywhere}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:12px;border-bottom:1px solid #dce5e9;text-align:left;vertical-align:top}}thead{{background:#edf3f5}}th{{white-space:nowrap}}td{{min-width:140px}}.count{{font-size:25px;font-weight:700}}@media(max-width:600px){{body{{margin-top:22px;padding:0 14px 25px}}h1{{font-size:24px}}h2{{font-size:19px}}.card{{padding:16px}}.auto,.manual,.finding{{padding:13px}}}}@media print{{body{{background:white}}.card{{break-inside:avoid}}}}
</style></head><body><header><p class="eyebrow">COMPANY DISCLOSURE MONITOR ／ NVIDIA</p><h1>別の5件で、同じ抽出ルールを試す</h1><p>前回使わなかった資料に適用し、どこまで拾えるかを確認しました。</p></header>
<section class="card intro"><p class="count">候補を得た書類：{count} / {len(results)}件</p><p>抽出候補は合計{quotes}箇所。固定した原文との照合：{valid_sources} / {len(results)}件。資料を取得できたことと、内容を抽出できたことを分けて評価しています。</p><p><strong>この比率は要約の正解率ではありません。</strong>候補が出た書類でも、内容全体を取り出せているわけではありません。</p><p class="note">選定：前回の5件を除き、保存済み一覧から提出日の新しい順に次の5件を選択。結果を見た後の選び直しはありません。抽出コード：{integrity}。日本語の内容説明と取りこぼしの判定はChatGPTによる原文確認です。</p></section>
<section class="card"><h2>5件の結果</h2><div class="scroll"><table><thead><tr><th scope="col">提出日</th><th scope="col">開示内容</th><th scope="col">結果</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
{''.join(cards)}
<section class="card"><h2>2026年11月17日の原文確認で分かった限界</h2><p>現行ルールは特定の英語表現を探すため、対応していない出来事や言い換えを取りこぼします。取締役の任命のような既知の出来事でも、表現が変わると拾えませんでした。</p><p>{source_note} ただし各12語の断片は、氏名・日付・条件などを十分に含まない場合があります。</p><p><strong>今回の結果を受けて、抽出ルールを修正したり、資料を「確認済み」に変更したりしていません。</strong>候補なしを無視する運用や、短い抜粋だけを使った自動要約には進めないと判断します。</p><p class="note">同じ企業の5件に限る小規模な確認です。他社・全期間での精度や、重要な情報をどれだけ網羅できるかは評価していません。添付資料の本文は確認対象外です。</p></section>
<footer><p class="note">確認日：2026-11-17 JST。本文・抽出コードのSHA-256、使用ルール、根拠の位置は検証パッケージに記録しています。候補が出ない原因を発行者の開示ミスとして扱いません。</p></footer></body></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=HERE / 'cache')
    parser.add_argument('--output-dir', type=Path, default=HERE / 'output')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--contact', default=os.environ.get('SEC_CONTACT_EMAIL', ''))
    args = parser.parse_args(argv)
    selection = json.loads((HERE / 'selection.json').read_text(encoding='utf-8'))
    sources = json.loads((HERE / 'source_manifest.json').read_text(encoding='utf-8'))['filings']
    engine_path = Path(engine.__file__)
    if digest(engine_path) != selection['extractor_sha256']:
        raise SystemExit('抽出コードが検証時から変わっています。比較結果を混ぜないため停止しました。')
    if [r['accession_number'] for r in sources] != [r['accession_number'] for r in selection['filings']]:
        raise SystemExit('選定した資料と原文情報が一致しません。')
    engine.validate_manifest({'filings': sources})
    fetch_error = None
    if args.fetch:
        try:
            engine.fetch_sources(sources, args.source_dir, args.contact)
        except ValueError as error:
            fetch_error = str(error)
    results = [engine.inspect_file(row, args.source_dir) for row in sources]
    notes = json.loads((HERE / 'manual_review_before_run.json').read_text(encoding='utf-8'))['filings']
    assessments = json.loads((HERE / 'assessment.json').read_text(encoding='utf-8'))['filings']
    literal_count = 0
    for r in results:
        if r['status'] not in ('candidates_found', 'no_candidate'):
            continue
        blocks = engine.visible_blocks((args.source_dir / r['accession_number'] / 'primary.html').read_bytes())
        for c in r['candidates']:
            text = blocks[c['block_number'] - 1]
            assert c['quote'] == text[c['char_start']:c['char_end']]
            assert hashlib.sha256(text.encode()).hexdigest() == c['paragraph_sha256']
            literal_count += 1
    hashes_ok = digest(engine_path) == selection['extractor_sha256']
    assert hashes_ok
    report = render(results, notes, assessments, hashes_ok)
    audit_results = []
    for r in results:
        audit = dict(r)
        audit['candidates'] = [{**{k: v for k, v in c.items() if k != 'quote'}, 'quote_word_count': len(c['quote'].split())} for c in r['candidates']]
        audit_results.append(audit)
    output = {'extractor_sha256': selection['extractor_sha256'], 'rules_modified': False,
              'filing_count': len(results), 'source_hashes_matched': sum(r['status'] in ('candidates_found', 'no_candidate') for r in results),
              'files_with_candidates': sum(r['candidate_count'] > 0 for r in results),
              'candidate_count': sum(r['candidate_count'] for r in results),
              'literal_quotes_verified': literal_count, 'fetch_error': fetch_error,
              'filings': audit_results}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'nvidia_additional_five_validation.html').write_text(report, encoding='utf-8')
    (args.output_dir / 'results.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in output.items() if k != 'filings'}, ensure_ascii=False))
    return 2 if fetch_error or any(r['status'] != 'candidates_found' for r in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
