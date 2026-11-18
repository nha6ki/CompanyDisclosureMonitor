"""Preserve complete normalized Item sections before any semantic extraction.

This stage neither generates summaries nor identifies completed business events.
The earlier phrase extractor remains unchanged for comparison.
"""
from pathlib import Path
import argparse
import hashlib
from html import escape
import json
import os
import re

import extract_evidence as baseline

ROOT = Path(__file__).resolve().parent
VERSION = 'nvidia-sections-0.2'
TITLE_PREFIXES = {**baseline.HEADINGS, '5.07': r'Submission\s+of\s+Matters'}
LABELS = {'1.01': '重要契約', '2.02': '業績・財務状況', '2.03': '債務等に関する開示',
          '5.02': '取締役・役員・報酬関連', '5.07': '株主総会等の投票結果',
          '7.01': 'Regulation FDに基づく開示', '8.01': 'その他の事象',
          '9.01': '財務諸表・添付資料'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def heading_at(blocks, start):
    if not re.match(r'^Item(?:\s|$)', blocks[start], re.I):
        return None
    for width in range(1, min(4, len(blocks) - start) + 1):
        text = ' '.join(blocks[start:start + width])
        match = re.match(r'^Item\s+(\d\.\d{2})\b[.\s:–—-]*(.*)$', text, re.I)
        if not match:
            continue
        code, title = match.groups()
        if not title:
            continue
        # References such as "Item 1.01 above ..." stay in their original section.
        if re.match(r'^(?:above|below|of|is|was|and|or|in|hereof|thereof)\b', title, re.I):
            return None
        prefix = TITLE_PREFIXES.get(code)
        if prefix and re.match(prefix, title, re.I):
            return {'item': code, 'heading_width': width, 'recognized': True}
        # Preserve an apparent but unsupported heading as a separate section,
        # flagged for review. It must never silently merge into a known section.
        if title[0].isupper() and len(title) <= 240:
            return {'item': code, 'heading_width': width, 'recognized': False}
        return None
    return None


def split_sections(raw):
    blocks = baseline.visible_blocks(raw)
    boundaries, i = [], 0
    stop = len(blocks)
    while i < len(blocks):
        if boundaries and re.fullmatch(r'SIGNATURES?', blocks[i], re.I):
            stop = i
            break
        found = heading_at(blocks, i)
        if found:
            boundaries.append({**found, 'start': i})
            i += found['heading_width']
        else:
            i += 1
    if not boundaries:
        return {'status': 'unsegmented', 'sections': [], 'visible_block_count': len(blocks),
                'warnings': ['Item見出しを識別できません。可視テキスト全体を未分割で保持しました。'],
                'unsegmented_blocks': blocks, 'partition_check_passed': False}
    sections, warnings = [], []
    for n, boundary in enumerate(boundaries):
        start = boundary['start']
        end = boundaries[n + 1]['start'] if n + 1 < len(boundaries) else stop
        content = blocks[start:end]
        text = '\n'.join(content)
        if not boundary['recognized']:
            warnings.append(f'Item {boundary["item"]}の見出しは未対応です。本文を保持したまま要確認としました。')
        sections.append({'section_id': f'section-{n + 1:02d}', 'item': boundary['item'],
                         'label': LABELS.get(boundary['item'], '対応表にない項目'),
                         'heading_recognized': boundary['recognized'],
                         'block_start': start + 1, 'block_end_inclusive': end,
                         'block_count': len(content), 'character_count': len(text),
                         'text_sha256': sha(text.encode()), 'blocks': content, 'text': text})
    retained = [b for section in sections for b in section['blocks']]
    expected = blocks[boundaries[0]['start']:stop]
    if retained != expected:
        raise ValueError('節の分割時にテキストの欠落・重複・順序変更を検出しました')
    return {'status': 'review_required' if warnings else 'sections_ready', 'sections': sections,
            'visible_block_count': len(blocks), 'retained_block_count': len(retained),
            'first_item_block': boundaries[0]['start'] + 1, 'last_item_block': stop,
            'warnings': warnings, 'partition_check_passed': True}


def inspect_source(row, source_dirs):
    result = dict(row)
    paths = [p / row['accession_number'] / 'primary.html' for p in source_dirs]
    paths = [p for p in paths if p.is_file()]
    if not paths:
        return {**result, 'status': 'source_missing', 'sections': [], 'warnings': ['保存済み原文がありません。']}
    raw = paths[0].read_bytes()
    if len(raw) > baseline.MAX_BYTES or sha(raw) != row['sha256']:
        return {**result, 'status': 'source_changed', 'sections': [], 'warnings': ['原文が固定サンプルと一致しません。']}
    if any(p.read_bytes() != raw for p in paths[1:]):
        return {**result, 'status': 'source_conflict', 'sections': [], 'warnings': ['同じ提出番号の保存資料が互いに異なります。']}
    result['observed_sha256'] = sha(raw)
    result.update(split_sections(raw))
    result['body_items'] = sorted({s['item'] for s in result['sections']})
    result['items_only_in_body'] = sorted(set(result['body_items']) - set(row['api_items']))
    result['items_only_in_api'] = sorted(set(row['api_items']) - set(result['body_items']))
    if result['items_only_in_api']:
        result['warnings'].append('登録項目の一部を節として識別できていません。本文欠落とは断定せず、原文確認が必要です。')
        result['status'] = 'review_required'
    result['baseline_candidate_count'] = baseline.extract(raw)['candidate_count']
    return result


def metadata_only(result):
    out = {k: v for k, v in result.items() if k != 'unsegmented_blocks'}
    out['sections'] = [{k: v for k, v in s.items() if k not in ('text', 'blocks')} for s in result['sections']]
    if 'unsegmented_blocks' in result:
        out['unsegmented_block_count'] = len(result['unsegmented_blocks'])
    return out


def render(results):
    successful = sum(r['status'] == 'sections_ready' for r in results)
    section_count = sum(len(r['sections']) for r in results)
    table = []
    for r in results:
        section_rows = ''.join(f'<li><b>Item {escape(s["item"])}</b>：{escape(s["label"])} ／ {s["character_count"]:,}文字</li>' for s in r['sections'])
        warning = ' '.join(r.get('warnings', []))
        detail = f'<details><summary>保持した節と文字数</summary><ul>{section_rows}</ul><p class="note">空白を正規化した本文の文字数。表のセルを含む順序を保持していますが、財務数値を構造化した表へ変換したものではありません。</p></details>' if r['sections'] else ''
        state = '節の本文を保持' if r['status'] == 'sections_ready' else '要確認'
        table.append(f'<tr><th scope="row">{r["filing_date"]}</th><td>{r.get("baseline_candidate_count", "未実行")}箇所</td><td>{escape(state)}<br>{len(r["sections"])}節{detail}<p class="note">{escape(warning)}</p></td><td><a href="{escape(r["document_url"], quote=True)}" target="_blank" rel="noopener noreferrer">SEC原文 ↗</a></td></tr>')
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>NVIDIA：本文を節単位で保持する</title><style>
body{{max-width:1050px;margin:32px auto;padding:0 20px 40px;background:#f6f8fa;color:#1d3342;font:15px/1.8 system-ui,sans-serif}}h1{{font-size:28px;line-height:1.45}}h2{{font-size:21px}}.box{{background:white;border:1px solid #d7e1e6;border-radius:10px;padding:22px;margin:22px 0}}.intro{{border-left:4px solid #16837d}}.note{{font-size:13px;color:#526b79}}.count{{font-size:25px;font-weight:700}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #dce5e9}}thead{{background:#edf3f5}}th{{white-space:nowrap}}td:nth-child(3){{min-width:320px}}a{{color:#096f83}}summary{{cursor:pointer}}li{{margin:7px 0}}.change td{{min-width:170px}}@media(max-width:600px){{body{{padding:0 14px 24px;margin-top:22px}}.box{{padding:16px}}h1{{font-size:24px}}}}
</style></head><body><header><p class="note">COMPANY DISCLOSURE MONITOR ／ NVIDIA</p><h1>短い抜粋の前に、本文のまとまりを保持する</h1><p>見出しで分けた本文を、確認や後続の情報抽出へ渡す処理を追加しました。</p></header>
<section class="box intro"><p class="count">節の本文を保持できた資料：{successful} / {len(results)}件</p><p>合計{section_count}節。最初のItem見出しから署名欄の直前まで、正規化した可視テキストの欠落・重複・順序変更がないかを確認しています。</p><p><strong>これは出来事を正しく理解・要約できた件数ではありません。</strong>前回の「根拠候補が2/5件」とは異なる指標です。今回は、情報を探す前に文脈を保持できるかを確認しました。</p></section>
<section class="box"><h2>前回取りこぼした3件への対応</h2><div class="scroll"><table class="change"><thead><tr><th>資料</th><th>今回の扱い</th></tr></thead><tbody><tr><td>株主総会の投票結果</td><td>Item 5.07の本文と、投票結果のセルの並びを保持します。</td></tr><tr><td>社債発行</td><td>出来事のキーワードに一致しなくても、Item 8.01の本文を保持します。</td></tr><tr><td>取締役の任命</td><td>言い回しに依存せず、Item 5.02の氏名・日付・条件を含む本文を保持します。</td></tr></tbody></table></div><p class="note">上の資料名は前回の原文確認に基づく説明で、今回のプログラムが自動命名したものではありません。</p></section>
<section class="box"><h2>既存10件での確認</h2><p>「旧処理」は表現を探して切り出した短い候補の数。「今回」は次の処理へ渡せる節の数です。</p><div class="scroll"><table><thead><tr><th>提出日</th><th>旧処理の候補</th><th>今回の節分割</th><th>参照先</th></tr></thead><tbody>{''.join(table)}</tbody></table></div></section>
<section class="box"><h2>何が業務に役立つか</h2><p>依頼された情報を抽出する際に、氏名・日付・条件・否定表現が切り落とされるのを防ぐための前処理です。分割できない資料は未分割のテキストを残して要確認とし、「記載なし」に置き換えません。原文URL・ファイルのハッシュ・節の位置を記録し、出力から原文を確認できるようにします。</p><p class="note">公開募集では、未知の文書での取りこぼしの改善、根拠参照の保持、運用と限界の文書化が求められています。<a href="https://www.upwork.com/freelance-jobs/apply/Senior-Python-Engineer-for-Web-Scraping-Document-Extraction_~022121844619172281293/" target="_blank" rel="noopener noreferrer">確認した募集例 ↗</a></p></section>
<section class="box"><h2>ここまでの到達点</h2><p>コードを実行すると、各節の本文を含む <code>section_inputs.json</code> と、根拠位置などを記録した <code>section_index.json</code> を生成します。日本語要約や人物・金額・日付の意味の判定は、今回の処理には含まれていません。</p><p>既存10件を使った修正後の確認です。この10件は今後の方式設計に使った資料として扱い、未知資料に対する精度の証明には使いません。任意のHTMLでの分割や、表の意味の完全な復元を保証するものではありません。</p></section><footer class="note">確認日：2026-11-17 JST。対象はNVIDIAの既存10件。元の根拠候補抽出コードと追加5件の検証結果は比較用に保持しています。</footer></body></html>'''


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-dir', type=Path, action='append', help='複数指定可。提出番号/primary.html を読みます')
    p.add_argument('--output-dir', type=Path, default=ROOT / 'section_output')
    p.add_argument('--fetch', action='store_true')
    p.add_argument('--contact', default=os.environ.get('SEC_CONTACT_EMAIL', ''))
    args = p.parse_args(argv)
    payload = json.loads((ROOT / 'section_manifest.json').read_text(encoding='utf-8'))
    rows = payload['filings']
    if len(rows) != 10 or len({r['accession_number'] for r in rows}) != 10:
        raise SystemExit('NVIDIAの固定10件のみを対象とします')
    baseline.validate_manifest({'filings': rows[:5]}); baseline.validate_manifest({'filings': rows[5:]})
    dirs = args.source_dir or [ROOT / 'cache', ROOT / 'validation' / 'cache']
    fetch_error = None
    if args.fetch:
        missing = [r for r in rows if not any((d / r['accession_number'] / 'primary.html').exists() for d in dirs)]
        try:
            baseline.fetch_sources(missing, dirs[0], args.contact)
        except ValueError as error:
            fetch_error = str(error)
    results = [inspect_source(row, dirs) for row in rows]
    summary = {'reader_version': VERSION, 'semantic_extraction_performed': False,
               'filing_count': len(results), 'sections_ready': sum(r['status'] == 'sections_ready' for r in results),
               'section_count': sum(len(r['sections']) for r in results), 'fetch_error': fetch_error}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {'section_inputs.json': json.dumps({**summary, 'filings': results}, ensure_ascii=False, indent=2) + '\n',
               'section_index.json': json.dumps({**summary, 'filings': [metadata_only(r) for r in results]}, ensure_ascii=False, indent=2) + '\n',
               'nvidia_section_review.html': render(results)}
    for name, content in outputs.items():
        target = args.output_dir / name; temp = target.with_suffix(target.suffix + '.tmp')
        temp.write_text(content, encoding='utf-8'); temp.replace(target)
    print(json.dumps(summary, ensure_ascii=False))
    return 2 if fetch_error or any(r['status'] != 'sections_ready' for r in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
