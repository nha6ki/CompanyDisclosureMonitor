"""Generate the NVIDIA filing register using the existing review workflow."""
import argparse
from datetime import date
from html import escape
import json
from pathlib import Path
import tempfile


STATES = {'pending': '未確認', 'needs_revision': '要修正', 'reviewed': '担当者確認済み'}
EVENT_STATES = {'pending_effective': '発効待ち', 'retirement_announced': '退任意向を通知',
                'agreement_signed_pending_close': '契約締結・完了待ち',
                'completed_as_reported': '提出時点で完了', 'unclear': '不明'}
ROLES = {'decision': '決定', 'notice': '通知', 'agreement': '契約締結', 'effective': '発効',
         'transition_end': '移行支援終了', 'expected_completion': '完了見込み'}
CERTAINTY = {'scheduled': '予定', 'expected': '見込み', 'reported': '本文記載', 'unknown': '不明'}


def register(view):
    rows, details = [], []
    filings = sorted(view['payload']['filings'], key=lambda f: f['as_of_date'], reverse=True)
    for filing in filings:
        acc = filing['accession_number']
        doc = view['source_documents'][acc]
        sources = {b['block_number']: b['text'] for b in doc['blocks']}
        status = STATES[view['review'][acc]['status']]
        for event in filing['events']:
            def fact(field):
                numbers = field['block_numbers']
                links = ' '.join(f'<a href="#{escape(acc)}-{int(n)}">¶{int(n)}</a>' for n in numbers)
                if any(n not in sources for n in numbers):
                    raise ValueError('根拠ブロックが本文にありません')
                return escape(str(field['value'])) + ' ' + links

            dates = '<br>'.join(escape(ROLES[d['role']]) + ' ' + fact(dict(d,value=d['value'] or '不明'))
                                + '（' + CERTAINTY[d['certainty']] + '）' for d in event['dates'])
            conditions = '<br>'.join(fact(c) for c in event['conditions']) or '記録なし'
            note = '<p>' + fact(event['notes_ja']) + '</p>' if event['notes_ja'] else ''
            rows.append(f"<tr><td>{escape(filing['as_of_date'])}</td><td>{fact(event['subject'])}</td>"
                        f"<td>{fact(event['summary_ja'])}{note}</td><td>{dates}</td><td>{conditions}</td>"
                        f"<td>{EVENT_STATES[event['status_as_of_filing']['value']]}<br>{status}</td></tr>")
        if not filing['events']:
            rows.append(f"<tr><td>{escape(filing['as_of_date'])}</td><td colspan='4'>抽出候補なし・本文を確認</td><td>{status}</td></tr>")
        blocks = ''.join(f'<p id="{escape(acc)}-{int(n)}"><b>¶{int(n)}</b> {escape(t)}</p>' for n,t in sources.items())
        details.append(f'<section><h3>{escape(acc)} · Item {escape(doc["item"])}</h3>{blocks}</section>')
    return ('''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NVIDIA 開示一覧</title><style>body{font:15px/1.65 system-ui;margin:3vw;color:#15372f}table{border-collapse:collapse;width:100%}td,th{vertical-align:top;text-align:left;padding:10px;border-bottom:1px solid #ccc}th{background:#e9f3ed}td:first-child{white-space:nowrap}.scroll{overflow:auto}p:target{background:#fff2b8}a{color:#07594e}section{max-width:920px}td p{font-size:90%}</style>
<h1>NVIDIA 開示一覧</h1>''' + f"<p>提出書類時点の記録 · {len(filings)}資料 · 確認記録の版 {int(view['revision'])}</p>"
            + '<div class="scroll"><table><thead><tr><th>提出日</th><th>対象</th><th>開示内容・補足</th><th>日付</th><th>条件</th><th>提出時の状態・確認状況</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div><h2>根拠の本文</h2>' + ''.join(details) + '</html>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--review-json', type=Path, help='確認画面から書き出したJSONを使用')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().with_name('nvidia_disclosures.html'))
    args = parser.parse_args()
    if args.review_json:
        view = json.loads(args.review_json.read_text(encoding='utf-8'))
    else:
        from nvidia_register.nvidia_qwen27b_review import ReviewStore
        view = ReviewStore(args.project).view()
    if not view['validation']['mechanical_checks_passed']:
        parser.error('機械検査を通過した入力を指定してください')
    html = register(view)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=args.output.parent, delete=False) as out:
        out.write(html)
    Path(out.name).replace(args.output)
    print(args.output.resolve())


if __name__ == '__main__':
    main()
