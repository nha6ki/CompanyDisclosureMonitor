"""NVIDIA five-filing pilot: extract source passages, not generated summaries.

Python 3.10+; standard library only. The extractor never reads reference_review.html.
That previously reviewed document is used only by the report renderer.
"""
from __future__ import annotations

import argparse
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
VERSION = 'nvidia-evidence-0.1'
MAX_BYTES = 5_000_000
WORD_LIMIT = 12
CANDIDATE_LIMIT = 2

# Section title prefixes distinguish headings from cross-references in prose.
HEADINGS = {
    '1.01': r'Entry\s+into', '1.02': r'Termination\s+of',
    '1.05': r'Material\s+Cybersecurity', '2.01': r'Completion\s+of',
    '2.02': r'Results\s+of\s+Operations', '2.03': r'Creation\s+of',
    '2.04': r'Triggering', '2.05': r'Costs?\s+Associated',
    '2.06': r'Material\s+Impairments', '3.02': r'Unregistered',
    '5.02': r'Departure\s+of', '7.01': r'Regulation\s+FD',
    '8.01': r'Other\s+Events', '9.01': r'Financial\s+Statements',
}

# No issuer/person names, accession numbers, dates, amounts, or prior summaries
# are used as triggers. Each label describes a matched phrase, not a legal finding.
RULES = [
    ('resignation', 10, {'5.02'}, '辞任受理の表現', r'\baccepted\s+(?:the\s+)?resignation\b'),
    ('retirement_intention', 10, {'5.02'}, '退任意向の表現', r'\b(?:intention|intent)\s+to\s+retire\b'),
    ('acquisition_agreement', 10, {'1.01', '8.01'}, '買収契約の表現', r'\bentered\s+into\s+(?:a\s+)?(?:definitive\s+)?agreement\s+to\s+acquire\b'),
    ('earnings', 10, {'2.02'}, '決算発表の表現', r'\bannounc(?:ing|ed)\s+(?:its\s+)?results\s+for\s+(?:the\s+)?quarter\b'),
    ('partnership', 10, {'1.01', '7.01'}, '提携発表の表現', r'\bannounced\s+a\s+(?:multi-year\s+)?partnership\b'),
    ('interim_role', 20, {'5.02'}, '暫定役職の表現', r'\b(?:as|to\s+serve\s+as)\s+interim\s+(?:Chief\s+Executive\s+Officer|CEO)\b'),
    ('payment_cap', 20, {'1.01', '2.03'}, '支払義務の上限表現', r'\baggregate\s+payment\s+obligation\s+is\s+cumulatively\s+capped\s+at\b'),
    ('completion_condition', 20, {'1.01', '8.01'}, '完了条件の表現', r'\bsubject\s+to\s+(?:the\s+)?(?:satisfaction|receipt)\b'),
    ('appointment_effective', 20, {'5.02'}, '発効条件の表現', r'\beffective\s+upon\s+(?:the\s+)?(?:employment\s+commencement|commencement)\b'),
    ('cfo_commentary', 20, {'2.02'}, '補足解説の表現', r'\bfinancial\s+information\s+and\s+commentary\b'),
    ('appointment', 30, {'5.02'}, '任命の表現', r'\b(?:Board|Company)\s+(?:has\s+)?appointed\b'),
]


def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


class VisibleBlocks(HTMLParser):
    BLOCKS = {'p', 'div', 'td', 'th', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'br', 'hr'}
    VOID = {'br', 'hr', 'img', 'input', 'meta', 'link', 'wbr', 'area', 'base', 'col', 'embed', 'param', 'source', 'track'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []

    @property
    def hidden(self):
        return bool(self.stack and self.stack[-1][1])

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        hidden = self.hidden or tag in {'script', 'style', 'head', 'ix:hidden', 'ix:header'} or 'hidden' in a
        hidden = hidden or bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', a.get('style', ''), re.I))
        if tag in self.BLOCKS and not hidden:
            self.parts.append('\n')
        if tag not in self.VOID:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.BLOCKS and not self.hidden:
            self.parts.append('\n')
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible_blocks(raw: bytes) -> list[str]:
    enc = re.search(br'charset\s*=\s*["\']?([a-zA-Z0-9_-]+)', raw[:6000], re.I)
    encoding = enc.group(1).decode('ascii') if enc else 'utf-8'
    try:
        decoded = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        decoded = raw.decode('windows-1252')
    parser = VisibleBlocks()
    parser.feed(decoded)
    return [normalize(v) for v in ''.join(parser.parts).splitlines() if normalize(v)]


def section_blocks(blocks: list[str]):
    sections, headings = [], []
    item, blocked, i = None, False, 0
    while i < len(blocks):
        line = blocks[i]
        if re.match(r'^SIGNATURES?\s*$', line, re.I):
            break
        if re.match(r'^Item\s+\d\.\d{2}\b', line, re.I):
            # Fail closed at any new Item boundary, even if the title is unknown.
            item, blocked = None, False
            for width in (1, 2, 3):
                joined = ' '.join(blocks[i:i + width])
                match = re.match(r'^Item\s+(\d\.\d{2})\b[.\s:–—-]*(.*)', joined, re.I)
                if match and match[1] in HEADINGS and re.match(HEADINGS[match[1]], match[2], re.I):
                    item = match[1]
                    headings.append(item)
                    i += width - 1
                    break
        elif re.match(r'^(?:Forward[ -]Looking\s+Statements|Risks?\s+Related\s+to)', line, re.I):
            blocked = True
        elif item and not blocked:
            sections.append({'item': item, 'block_number': i + 1, 'text': line})
        i += 1
    return sorted(set(headings)), sections


def short_quote(text: str, match: re.Match) -> dict:
    words = list(re.finditer(r'\S+', text))
    anchor = next(i for i, word in enumerate(words) if word.end() > match.start())
    start = max(0, anchor - 2)
    end = min(len(words), start + WORD_LIMIT)
    a, b = words[start].start(), words[end - 1].end()
    return {'quote': text[a:b], 'char_start': a, 'char_end': b,
            'leading_omission': a > 0, 'trailing_omission': b < len(text)}


def extract(raw: bytes) -> dict:
    """Select up to two literal fragments, independently of reference annotations."""
    blocks = visible_blocks(raw)
    headings, paragraphs = section_blocks(blocks)
    hits = []
    for p in paragraphs:
        text = p['text']
        # Conservative screens only; this is not a complete negation/claim parser.
        if re.search(r'\b(?:not|never|rumou?r|hypothetical)\b', text, re.I) or re.match(r'^(?:If|Assuming)\b', text, re.I):
            continue
        for rule_id, rank, items, label, pattern in RULES:
            if p['item'] not in items:
                continue
            match = re.search(pattern, text, re.I)
            if match:
                hits.append({'rule_id': rule_id, 'rank': rank, 'label': label,
                             'item': p['item'], 'block_number': p['block_number'],
                             'paragraph_sha256': hashlib.sha256(text.encode()).hexdigest(),
                             **short_quote(text, match)})
    selected, used_blocks, used_rules = [], set(), set()
    for hit in sorted(hits, key=lambda h: (h['rank'], h['block_number'], h['rule_id'])):
        if hit['block_number'] in used_blocks or hit['rule_id'] in used_rules:
            continue
        selected.append({k: v for k, v in hit.items() if k != 'rank'})
        used_blocks.add(hit['block_number']); used_rules.add(hit['rule_id'])
        if len(selected) == CANDIDATE_LIMIT:
            break
    return {'text_items': headings, 'status': 'candidates_found' if selected else 'no_candidate',
            'candidate_count': len(selected), 'candidates': selected}


def validate_manifest(payload: dict) -> list[dict]:
    rows = payload['filings']
    if len(rows) != 5 or len({r['accession_number'] for r in rows}) != 5:
        raise ValueError('この試作は重複しないNVIDIAの5件に限定しています')
    for row in rows:
        if row['ticker'] != 'NVDA' or not re.fullmatch(r'\d{10}-\d{2}-\d{6}', row['accession_number']):
            raise ValueError('対象企業または提出番号が範囲外です')
        url = urlsplit(row['document_url'])
        prefix = '/Archives/edgar/data/1045810/' + row['accession_number'].replace('-', '') + '/'
        if url.scheme != 'https' or url.netloc != 'www.sec.gov' or not url.path.startswith(prefix) or url.query or url.fragment:
            raise ValueError('対象外の原文URLです')
        if not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
            raise ValueError('原文の固定ハッシュが不正です')
    return rows


def inspect_file(row: dict, source_dir: Path) -> dict:
    result = dict(row)
    path = source_dir / row['accession_number'] / 'primary.html'
    result.update(candidates=[], candidate_count=0, text_items=[])
    if not path.is_file():
        result.update(status='source_missing', error='保存済み本文がありません')
        return result
    if path.stat().st_size > MAX_BYTES:
        result.update(status='source_invalid', error='本文のサイズ上限を超えています')
        return result
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    result['observed_sha256'] = actual
    if actual != row['sha256']:
        result.update(status='source_changed', error='前回の原文とハッシュが異なります。参照要約との比較を止めました')
        return result
    result.update(extract(raw))
    result['items_only_in_body'] = sorted(set(result['text_items']) - set(row['api_items']))
    result['items_only_in_api'] = sorted(set(row['api_items']) - set(result['text_items']))
    return result


def fetch_sources(rows: list[dict], source_dir: Path, contact: str):
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', contact):
        raise ValueError('--contact または SEC_CONTACT_EMAIL に連絡用メールアドレスを指定してください')
    last_request = None
    for row in rows:
        path = source_dir / row['accession_number'] / 'primary.html'
        if path.exists():
            continue  # Existing source changes are reviewed, not silently overwritten.
        if last_request is not None:
            time.sleep(max(0, 1.1 - (time.monotonic() - last_request)))
        request = Request(row['document_url'], headers={'User-Agent': f'CompanyDisclosureMonitorEvidence/0.1 {contact}', 'Accept': 'text/html'})
        last_request = time.monotonic()
        try:
            with urlopen(request, timeout=30) as response:
                if response.geturl() != row['document_url']:
                    raise ValueError('予期しないリダイレクトを検出したため取得を停止しました')
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('本文のサイズ上限を超えたため取得を停止しました')
        except HTTPError as exc:
            raise ValueError(f'HTTP {exc.code} のため後続の取得を停止しました。自動再試行は行いません') from None
        except (URLError, TimeoutError):
            raise ValueError('通信に失敗したため後続の取得を停止しました') from None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def render_report(reference: str, results: list[dict]) -> str:
    report = reference
    for r in results:
        acc = r['accession_number']
        pattern = r'(<article\b[^>]*data-accession="' + re.escape(acc) + r'"[^>]*>)(.*?)(</article>)'
        matches = list(re.finditer(pattern, report, re.S))
        if len(matches) != 1:
            raise ValueError('参照画面の提出番号と実行対象が一致しません')
        m = matches[0]
        body = m[2]
        body = body.replace('<p class="lead">', '<p class="note manual-label"><strong>前回の原文確認に基づく日本語要約</strong></p><p class="lead">', 1)
        snippet = '<div class="evidence automated"><h3>今回、Pythonが自動抽出した根拠候補</h3>'
        if r['status'] == 'candidates_found':
            for c in r['candidates']:
                quote = ('… ' if c['leading_omission'] else '') + c['quote'] + (' …' if c['trailing_omission'] else '')
                snippet += f'<p><b>{escape(c["label"])}</b> ／ Item {escape(c["item"])}</p><blockquote lang="en">{escape(quote)}</blockquote>'
            snippet += '<p class="note">ルールに合った原文の短い断片です。前後の文脈を含む意味や、日本語要約との一致を自動判定したものではありません。</p>'
        elif r['status'] == 'no_candidate':
            snippet += '<p>対応する表現が見つかりませんでした。出来事がないという意味ではありません。</p>'
        else:
            snippet += '<p>' + escape(r.get('error', '原文を確認できません')) + '</p>'
            body = '<p class="followup">原文が前回と一致することを確認できないため、前回の要約は参考情報として残しています。</p>' + body
        snippet += '</div>'
        body, count = re.subn(r'<div class="evidence">.*?</div>', lambda _: snippet, body, count=1, flags=re.S)
        if count != 1:
            raise ValueError('参照画面の根拠欄が見つかりません')
        report = report[:m.start()] + m[1] + body + m[3] + report[m.end():]
    matched = sum(r['status'] == 'candidates_found' for r in results)
    count = sum(r['candidate_count'] for r in results)
    report = report.replace('NVIDIA — 5件の開示内容を確認', 'NVIDIA — 原文の根拠候補を自動抽出')
    report = report.replace('何が起きたかを、原文から確認する', '要約と、自動抽出した根拠を照らし合わせる')
    report = report.replace('項目番号による分類に、具体的な出来事・根拠・追加の確認点を加えました。', '前回の日本語要約に、Pythonが原文から抽出した根拠候補を並べました。対象は同じNVIDIAの5件です。')
    notice = f'''<section class="box notice" aria-label="確認範囲"><p><strong>今回の自動実行：5件中{matched}件で候補を検出、合計{count}箇所。</strong>この件数は要約の正解数や抽出精度を示すものではありません。</p><p>日本語の要約・確認点は前回ChatGPTが原文を読んで作成した内容です。今回のプログラムは、その要約を読まずに英語本文から候補を取り出しています。</p><p class="note">各提出につき最大2箇所、各箇所12語以内。原文へのリンクから前後の文脈を確認できます。対応表現がない場合は「候補なし」とします。LLM・外部の要約APIは使用していません。</p><p class="note">保存済み一覧：2026-11-17 10:29 JST。前回と同じ原文かをSHA-256で照合しています。過去の開示時点の記載であり、予定の実現やその後の状況を確認したものではありません。</p></section>'''
    report, n = re.subn(r'<section class="box notice".*?</section>', lambda _: notice, report, count=1, flags=re.S)
    if n != 1:
        raise ValueError('参照画面の確認範囲欄が見つかりません')
    return report.replace('確認用の静的レポートです。', 'Pythonの実行結果を含む静的レポートです。')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source-dir', type=Path, default=ROOT / 'cache', help='提出番号/primary.html があるディレクトリ')
    ap.add_argument('--output-dir', type=Path, default=ROOT / 'output')
    ap.add_argument('--fetch', action='store_true', help='不足する5件の本文をSECから取得する')
    ap.add_argument('--contact', default=os.environ.get('SEC_CONTACT_EMAIL', ''), help='SEC取得時の連絡先。出力には保存しません')
    args = ap.parse_args(argv)
    try:
        manifest = json.loads((ROOT / 'filings.json').read_text(encoding='utf-8'))
        rows = validate_manifest(manifest)
        fetch_error = None
        if args.fetch:
            try:
                fetch_sources(rows, args.source_dir, args.contact)
            except ValueError as exc:
                fetch_error = str(exc)
        results = [inspect_file(row, args.source_dir) for row in rows]
        report = render_report((ROOT / 'reference_review.html').read_text(encoding='utf-8'), results)
        # Keep exact source locations in JSON; the readable excerpts are in HTML.
        # This also avoids redundant copies of source text in the output package.
        audit_results = []
        for result in results:
            audit = dict(result)
            audit['candidates'] = [
                {**{k: v for k, v in c.items() if k != 'quote'}, 'quote_word_count': len(c['quote'].split())}
                for c in result['candidates']
            ]
            audit_results.append(audit)
        summary = {'schema_version': 1, 'extractor_version': VERSION,
                   'mode': 'rule_based_source_fragments', 'generated_japanese_summary': False,
                   'snapshot_at': manifest['snapshot_at'], 'fetch_error': fetch_error,
                   'filing_count': len(results), 'files_with_candidates': sum(r['candidate_count'] > 0 for r in results),
                   'candidate_count': sum(r['candidate_count'] for r in results), 'filings': audit_results}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, content in [('evidence.json', json.dumps(summary, ensure_ascii=False, indent=2) + '\n'),
                              ('nvidia_five_filings_review.html', report)]:
            target = args.output_dir / name
            temporary = target.with_suffix(target.suffix + '.tmp')
            temporary.write_text(content, encoding='utf-8'); temporary.replace(target)
        print(json.dumps({k: summary[k] for k in ['extractor_version', 'filing_count', 'files_with_candidates', 'candidate_count', 'fetch_error']}, ensure_ascii=False))
        return 2 if fetch_error or any(r['status'] != 'candidates_found' for r in results) else 0
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f'実行を完了できませんでした: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
