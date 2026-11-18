#!/usr/bin/env python3
"""NVIDIA/Qwen 27B: local source review, editing and revision records.

WSL: source ~/projects/nvidia_evidence_extractor/.venv/bin/activate
python nvidia_register/nvidia_qwen27b_review.py --project .
Open the printed localhost URL in Windows. Ctrl+C stops the server.
No inference, downloads, external assets or changes to original LLM logs.
--check performs read-only preparation; --run chooses an existing run folder.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from urllib.parse import urlsplit

MODEL = 'nvidia-qwen38-27b:16k'
VERSION = 'nvidia-review-1'
CHECKS = ('identity', 'dates', 'conditions', 'evidence')
FIXED = ('accession_number', 'source_sha256', 'section_id', 'section_sha256', 'as_of_date')


class ReviewError(Exception):
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.status, self.details = status, details


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def diff(before, after, path=''):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
        return [d for k in before for d in diff(before[k], after[k], path + '/' + k)]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [d for i, (a, b) in enumerate(zip(before, after)) for d in diff(a, b, path + '/' + str(i))]
    return [{'path': path or '/', 'before': before, 'after': after}]


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.review-', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


class ReviewStore:
    def __init__(self, project, run=None, source_dirs=None):
        self.project = Path(project).expanduser().resolve()
        if not (self.project / 'structured_events.py').is_file():
            raise ReviewError('既存プロジェクトが見つかりません。--project で指定してください。')
        sys.path.insert(0, str(self.project))
        self.events = importlib.import_module('structured_events')
        self.runner = importlib.import_module('llm_runner')
        if self.events.ROOT.resolve() != self.project or self.runner.ROOT.resolve() != self.project:
            raise ReviewError('読み込んだPythonモジュールと指定プロジェクトが一致しません。')
        self.source_dirs = source_dirs or [self.project / 'cache', self.project / 'validation/cache']
        if run is None:
            found = []
            for p in (self.project / 'llm_runs').glob('*/run.json'):
                log = read(p)
                if log.get('config', {}).get('model') == MODEL and log.get('status') == 'completed_pending_review':
                    found.append(p.parent)
            if not found:
                raise ReviewError('完了したQwen 27Bの実行ログがありません。--run で保存済み実行フォルダを指定してください。')
            self.run = sorted(found)[-1].resolve()
        else:
            self.run = Path(run).expanduser().resolve()
        self.log = read(self.run / 'run.json')
        if self.log.get('config', {}).get('model') != MODEL or self.log.get('status') != 'completed_pending_review':
            raise ReviewError('Qwen 27Bの抽出が完了した実行だけを読み込めます。')
        self.original = read(self.run / 'candidate.json')
        self.original_hash = sha(self.original)
        self.contexts = self.events.source_contexts(self.source_dirs)
        self.verify_run()
        self.path = self.project / 'review_runs' / self.run.name / 'review.json'
        self.fingerprint = sha({'run': self.run.name, 'log': self.log, 'original': self.original_hash})
        self.state()  # Detect a mismatched existing review before showing a page.

    def validate(self, payload):
        result = self.events.validate(payload, self.contexts)
        if not result['mechanical_checks_passed']:
            raise ReviewError('原文・名称・日付・根拠位置などの検査に通りませんでした。内容を修正してください。',
                              422, result['errors'])
        return result

    def verify_run(self):
        self.validate(self.original)
        combined = {'schema_version': self.original['schema_version'], 'filings': []}
        requests = self.log['requests']
        if len(requests) != len(self.contexts) or len({r['accession_number'] for r in requests}) != len(requests):
            raise ReviewError('実行ログの対象資料数・提出番号が一致しません。')
        for r in requests:
            acc = r['accession_number']
            if acc not in self.contexts:
                raise ReviewError('実行ログに選択対象外の資料があります。')
            folder = self.run / acc
            body, response, candidate = (read(folder / n) for n in ('request.json', 'response.json', 'candidate.json'))
            doc = self.runner.document_input(acc, self.contexts[acc])
            if (sha(body) != r['request_sha256'] or sha(doc) != r['document_sha256']
                    or body.get('model') != MODEL or response.get('model') != MODEL
                    or json.loads(body['messages'][-1]['content']) != {'documents': [doc]}
                    or self.runner.parse_response(response) != candidate):
                raise ReviewError('元のリクエスト・応答・原文と抽出結果の対応が一致しません。')
            combined['filings'].extend(candidate['filings'])
        if combined != self.original:
            raise ReviewError('集約された結果と各資料の抽出結果が一致しません。')

    def fresh_sources(self):
        if sha(read(self.run / 'candidate.json')) != self.original_hash or read(self.run / 'run.json') != self.log:
            raise ReviewError('確認中に元の実行ログが変わりました。サーバーを再起動してください。', 409)
        self.contexts = self.events.source_contexts(self.source_dirs)
        self.validate(self.original)

    def state(self):
        document = {'format': VERSION, 'fingerprint': self.fingerprint, 'run_id': self.run.name,
                    'model': MODEL, 'original_sha256': self.original_hash, 'revision': 0, 'history': []}
        if self.path.exists():
            document = read(self.path)
            if document.get('format') != VERSION or document.get('fingerprint') != self.fingerprint:
                raise ReviewError('保存済みの確認記録が、この抽出結果と対応していません。', 409)
        payload = copy.deepcopy(self.original)
        rows = {r['accession_number']: r for r in payload['filings']}
        review = {acc: {'status': 'pending', 'reviewer': '', 'reason': '',
                        'checks': {k: False for k in CHECKS}} for acc in rows}
        if document['revision'] != len(document['history']):
            raise ReviewError('確認記録の版番号と履歴が一致しません。', 409)
        for index, entry in enumerate(document['history'], 1):
            acc = entry['accession_number']
            if entry['revision'] != index or acc not in rows or entry['before_sha256'] != sha(rows[acc]):
                raise ReviewError('確認記録の履歴の対応が一致しません。', 409)
            self.check_identity(rows[acc], entry['filing'])
            if entry['changes'] != diff(rows[acc], entry['filing']):
                raise ReviewError('確認記録の差分が保存内容と一致しません。', 409)
            self.check_review(entry)
            rows[acc] = copy.deepcopy(entry['filing'])
            review[acc] = {k: copy.deepcopy(entry[k]) for k in ('status', 'reviewer', 'reason', 'checks')}
            review[acc]['at_utc'] = entry['at_utc']
        payload['filings'] = [rows[r['accession_number']] for r in self.original['filings']]
        self.validate(payload)
        return document, payload, review

    @staticmethod
    def check_identity(before, after):
        if not isinstance(after, dict) or any(before[k] != after.get(k) for k in FIXED):
            raise ReviewError('提出番号・原文識別情報・提出日は変更できません。', 422)

    @staticmethod
    def check_review(body):
        if body.get('status') not in ('pending', 'needs_revision', 'reviewed'):
            raise ReviewError('確認状態が不正です。')
        for key in ('reviewer', 'reason'):
            if not isinstance(body.get(key), str) or not body[key].strip() or len(body[key]) > 2000:
                raise ReviewError('確認者名と保存理由を入力してください（各2,000文字以内）。')
        checks = body.get('checks')
        if not isinstance(checks, dict) or set(checks) != set(CHECKS) or any(type(v) is not bool for v in checks.values()):
            raise ReviewError('確認項目の形式が不正です。')
        if body['status'] == 'reviewed' and not all(checks.values()):
            raise ReviewError('確認済みにするには、4つの確認項目を原文と照合してください。', 422)

    def save(self, body):
        # WSL/Linux advisory lock also prevents lost writes from another server process.
        import fcntl
        if not isinstance(body, dict):
            raise ReviewError('保存データはJSONオブジェクトで指定してください。')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / '.review.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.fresh_sources()
            document, payload, _ = self.state()
            if type(body.get('base_revision')) is not int or body['base_revision'] != document['revision']:
                raise ReviewError('別の画面で保存されています。現在の編集を控えてから、ページを再読込してください。', 409)
            self.check_review(body)
            acc = body.get('accession_number')
            current = next((r for r in payload['filings'] if r['accession_number'] == acc), None)
            if current is None:
                raise ReviewError('対象資料が見つかりません。')
            after = body.get('filing')
            self.check_identity(current, after)
            revised = copy.deepcopy(payload)
            revised['filings'] = [copy.deepcopy(after) if r['accession_number'] == acc else r for r in revised['filings']]
            self.validate(revised)
            revision = document['revision'] + 1
            entry = {'revision': revision, 'at_utc': datetime.now(timezone.utc).isoformat(),
                     'accession_number': acc, 'before_sha256': sha(current), 'filing': copy.deepcopy(after),
                     'changes': diff(current, after)}
            entry.update({k: copy.deepcopy(body[k]) for k in ('reviewer', 'reason', 'status', 'checks')})
            document['history'].append(entry)
            document['revision'] = revision
            atomic_json(self.path, document)
        return self.view()

    def view(self):
        self.fresh_sources()
        record, payload, review = self.state()
        return {'format': VERSION, 'model': MODEL, 'run_id': self.run.name, 'revision': record['revision'],
                'original': self.original, 'payload': payload, 'review': review, 'history': record['history'],
                'source_documents': {acc: dict(self.runner.document_input(acc, ctx), document_url=ctx['document_url'])
                                     for acc, ctx in self.contexts.items()},
                'validation': self.validate(payload), 'record_path': str(self.path),
                'publication_performed': False, 'llm_invocation_performed': False,
                'human_review_complete': all(r['status'] == 'reviewed' for r in review.values()),
                'note': '担当者の確認記録です。第三者の正確性保証、未使用資料での精度評価、公開承認ではありません。'}

    def validate_draft(self, body):
        self.fresh_sources()
        _, payload, _ = self.state()
        if not isinstance(body, dict) or not isinstance(body.get('filing'), dict):
            raise ReviewError('資料のJSONオブジェクトが必要です。')
        after = body['filing']
        before = next((r for r in payload['filings'] if r['accession_number'] == after.get('accession_number')), None)
        if before is None:
            raise ReviewError('対象資料が見つかりません。')
        self.check_identity(before, after)
        payload['filings'] = [after if r['accession_number'] == before['accession_number'] else r for r in payload['filings']]
        return self.validate(payload)


def handler_for(store, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value, content_type='application/json; charset=utf-8', download=False):
            data = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-" + token + "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="nvidia_qwen27b_review_record.json"')
            self.end_headers()
            self.wfile.write(data)

        def check_host(self):
            port = self.server.server_port
            if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                raise ReviewError('localhostから開いてください。', 403)

        def dispatch(self, post=False):
            try:
                self.check_host()
                path = urlsplit(self.path).path
                if not post and path == '/':
                    return self.send(200, HTML.replace('__TOKEN__', token), 'text/html; charset=utf-8')
                if self.headers.get('X-Review-Token') != token:
                    raise ReviewError('ページを開き直してください。', 403)
                if not post and path in ('/api/state', '/api/export'):
                    return self.send(200, store.view(), download=path == '/api/export')
                if post and path in ('/api/save', '/api/validate'):
                    origin = self.headers.get('Origin')
                    if origin != 'http://' + self.headers['Host']:
                        raise ReviewError('同じ画面から保存してください。', 403)
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise ReviewError('JSON形式で送信してください。', 415)
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 1_000_000:
                        raise ReviewError('保存データのサイズが不正です。', 413)
                    body = json.loads(self.rfile.read(size))
                    return self.send(200, store.save(body) if path == '/api/save' else store.validate_draft(body))
                raise ReviewError('ページがありません。', 404)
            except ReviewError as exc:
                self.send(exc.status, {'error': str(exc), 'details': exc.details})
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send(400, {'error': 'ファイルまたは入力を処理できませんでした。再読込して保存状態を確認してください。',
                                'details': type(exc).__name__})

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch(True)
    return Handler


HTML = r'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NVIDIA — 原文照合と確認記録</title><style>
:root{--ink:#172b35;--muted:#536671;--line:#d8e2e6;--green:#136554;--pale:#eaf5ef;--amber:#865b16}*{box-sizing:border-box}body{margin:0;color:var(--ink);background:#f3f6f5;font:15px/1.65 system-ui,sans-serif}header{padding:24px 4vw 18px;background:white;border-bottom:1px solid var(--line)}.eyebrow{font-size:11px;letter-spacing:.12em;color:var(--green);font-weight:750}h1{font-size:28px;line-height:1.3;margin:6px 0 12px}h2{font-size:20px;margin:0 0 12px}h3{font-size:18px;margin:0}p{margin:8px 0}button,input,textarea,select{font:inherit}button{cursor:pointer;border:1px solid #aabcb6;border-radius:6px;padding:8px 13px;background:white;color:var(--ink)}button:hover{background:var(--pale)}button:disabled{cursor:wait;opacity:.5}button.primary{background:var(--green);color:white;border-color:var(--green)}button:focus-visible,a:focus-visible{outline:3px solid #c5a347;outline-offset:3px}input,textarea,select{border:1px solid #abbcc3;border-radius:5px;padding:8px;background:#fff;min-width:0;max-width:100%;color:var(--ink)}textarea{width:100%;resize:vertical;line-height:1.6}label{display:block;font-weight:650;font-size:13px;margin:10px 0 4px}small,.muted{color:var(--muted);font-size:13px}.bar{display:flex;flex-wrap:wrap;align-items:center;gap:9px}.badge{display:inline-block;padding:3px 9px;border-radius:4px;background:#edf0f2;font-size:12px;font-weight:650}.badge.ok{background:var(--pale);color:var(--green)}.badge.warn{background:#fff1d8;color:var(--amber)}.layout{display:grid;grid-template-columns:245px minmax(0,1fr);gap:24px;max-width:1600px;margin:auto;padding:24px 4vw 80px}nav button{display:block;width:100%;text-align:left;margin:0 0 10px;padding:13px;line-height:1.65}nav button.active{border:2px solid var(--green);background:white;padding:12px}nav .title{display:block;font-weight:700;margin:3px 0}.panel{border:1px solid var(--line);border-radius:10px;padding:20px;background:white;margin-bottom:18px}.columns{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:18px;align-items:start}.source{position:sticky;top:16px;max-height:86vh;overflow:auto}.block{padding:14px 0;border-top:1px solid var(--line);scroll-margin:15px}.block p{font-size:14px;line-height:1.8;overflow-wrap:anywhere}.block.highlight{background:#fff5d5;outline:3px solid #e4c15d}.chip{padding:2px 7px;font-size:12px;margin:2px;white-space:nowrap;color:var(--green);border-color:#c3dacf}.refs{display:flex;flex-wrap:wrap;align-items:center;gap:5px;margin:3px 0 14px}.refs input{width:105px;font-size:12px;padding:4px}.event{padding-top:18px;margin-top:18px;border-top:2px solid var(--line)}.event:first-child{border-top:0;margin-top:0;padding-top:0}.fact{display:grid;grid-template-columns:85px 1fr;gap:10px;margin:9px 0}.fact b{font-size:12px;color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:7px 4px;text-align:left;border-bottom:1px solid var(--line)}.warning{background:#fff7e8;border-left:3px solid #c38c30;padding:9px 12px;font-size:13px;margin:10px 0}.conditions{margin:8px 0}.condition{padding:10px 12px;background:#f5f8f7;border-radius:7px;margin:8px 0}.condition button.remove{font-size:12px;padding:3px 8px}.review-check{display:flex;gap:8px;font-size:14px;font-weight:400;align-items:start;margin:10px 0}.review-check input{margin-top:5px}.review-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}details{margin:16px 0}summary{cursor:pointer;font-weight:650;color:var(--green)}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,monospace}#message{display:none;margin:18px 4vw 0;padding:13px;border-radius:7px;background:#e4f4e9}#message.error{background:#ffe9e5;color:#8f2f20}#dirty{color:var(--amber);font-weight:650;font-size:13px}a{color:var(--green)}.history-entry{border-top:1px solid var(--line);padding:10px 0}.changes{font-size:13px}.changes li{margin:10px 0;overflow-wrap:anywhere}.before{color:#8a4538}.after{color:var(--green)}.footer{font-size:12px;color:var(--muted);overflow-wrap:anywhere;margin-top:20px}.empty{padding:30px}.noscript{padding:30px}.review-actions{display:flex;gap:12px;align-items:center;margin-top:14px}#advanced{font-family:ui-monospace,monospace;font-size:12px}#download{margin-left:auto}@media(max-width:1100px){.layout{grid-template-columns:200px minmax(0,1fr);gap:16px;padding:20px}.columns{grid-template-columns:1fr}.source{position:static;max-height:65vh}}@media(max-width:680px){header{padding:20px}h1{font-size:24px}.layout{display:block;padding:16px}nav{display:flex;overflow-x:auto;gap:8px;margin-bottom:12px}nav button{min-width:220px}.panel{padding:16px}.review-grid{grid-template-columns:1fr}.bar{align-items:start}#download{margin-left:0}.fact{grid-template-columns:70px 1fr}}
</style></head><body><header><div class="eyebrow">COMPANY DISCLOSURE MONITOR / NVIDIA</div><h1>抽出結果を、原文と確かめる</h1><div class="bar"><span class="badge ok">Qwen 27B 採用</span><span id="counts" class="muted">読込中…</span><span id="dirty"></span><button id="download">確認記録をダウンロード</button></div><p class="muted">保存済みの抽出結果を確認します。状態と日付は各提出書類の時点です。</p></header>
<noscript><p class="noscript">この確認画面はJavaScriptを使用します。</p></noscript><div id="message" role="status" aria-live="polite"></div><main class="layout"><nav id="navigation" aria-label="対象資料"></nav><section id="workspace"></section></main>
<script nonce="__TOKEN__">
'use strict';
const token='__TOKEN__';let state,selected,draft,dirty=false,busy=false;
const roles={decision:'決定日',notice:'通知日',agreement:'契約日',effective:'発効日',expected_completion:'完了見込み',transition_end:'移行支援の終了'};
const statuses={pending_effective:'発効待ち',retirement_announced:'役職退任の意向を通知',agreement_signed_pending_close:'契約締結済み・完了待ち',completed_as_reported:'本文上は完了',unclear:'状態不明'};
const certainties={reported:'本文記載の実施日',scheduled:'予定',expected:'見込み',unknown:'不明'};
const reviewLabels={pending:'未確認',needs_revision:'要修正',reviewed:'担当者が確認済み'};
const types={appointment:'任命',retirement_notice:'退任意向',acquisition_agreement:'買収契約',other:'その他'};
const clone=x=>JSON.parse(JSON.stringify(x));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty=x=>typeof x==='string'?x:JSON.stringify(x,null,2);
function message(text,error=false){const el=document.querySelector('#message');el.textContent=text;el.className=error?'error':'';el.style.display='block';if(error)el.scrollIntoView({behavior:'smooth',block:'center'});}
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Review-Token':token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});const v=await r.json();if(!r.ok)throw Error(v.error+(v.details?'\n'+pretty(v.details):''));return v;}
function markDirty(){dirty=true;document.querySelector('#dirty').textContent='未保存の変更あり';const s=document.querySelector('#review-status');if(s&&s.value==='reviewed')s.value='pending';document.querySelectorAll('[data-check]').forEach(x=>x.checked=false);}
function parseRefs(value){if(!value.trim())return [];const a=value.split(/[,、\s]+/).map(Number);if(a.some(x=>!Number.isInteger(x)||x<1)||new Set(a).size!==a.length)throw Error('根拠番号は重複のない正の整数を、カンマ区切りで入力してください。');return a;}
function chips(numbers){return numbers.map(n=>`<button type="button" class="chip" data-block="${esc(n)}">原文 ${esc(n)}</button>`).join('');}
function refs(fact,index,field){return `<div class="refs"><span class="muted">根拠</span><input aria-label="${esc(field)}の根拠番号" data-refs="${esc(field)}" data-event="${index}" value="${esc(fact.block_numbers.join(', '))}">${chips(fact.block_numbers)}</div>`;}
function warnings(e){const items=[];if(!e.conditions.length)items.push('条件が空欄です。原文に就任・完了の条件がないか確認してください。');if(e.dates.some(x=>x.certainty==='scheduled'||x.certainty==='expected'))items.push('予定・見込みを含みます。要約も「完了した」と読めない表現にします。');if(e.notes_ja&&/[$＄]|ドル|億|million|billion/i.test(e.notes_ja.value))items.push('補足に金額が含まれています。今回の抽出対象外なので、不要なら削除してください。残す場合は条件や留保も原文で確認します。');return items.map(t=>`<div class="warning">${esc(t)}</div>`).join('');}
function eventHTML(e,i){const original=state.original.filings.find(x=>x.accession_number===selected).events.find(x=>x.event_id===e.event_id);return `<article class="event"><div class="bar"><h3>${esc(e.subject.value)}</h3><span class="badge">${esc(types[e.event_type.value]||e.event_type.value)}</span></div><div class="fact"><b>所属・主体</b><div>${esc(e.organization.value)} ${chips(e.organization.block_numbers)}</div></div><div class="fact"><b>提出時の状態</b><div>${esc(statuses[e.status_as_of_filing.value])} ${chips(e.status_as_of_filing.block_numbers)}</div></div>${warnings(e)}<label for="summary-${i}">日本語要約</label><textarea rows="3" id="summary-${i}" data-event="${i}" data-field="summary_ja">${esc(e.summary_ja.value)}</textarea>${refs(e.summary_ja,i,'summary_ja')}<table><thead><tr><th>日付の意味</th><th>値・確度</th><th>根拠</th></tr></thead><tbody>${e.dates.map(d=>`<tr><td>${esc(roles[d.role])}</td><td>${esc(d.value||'不明')}<br><span class="muted">${esc(certainties[d.certainty])}</span></td><td>${chips(d.block_numbers)}</td></tr>`).join('')}</tbody></table><label>条件・留保</label><div class="conditions">${e.conditions.map((c,j)=>`<div class="condition"><textarea rows="2" aria-label="条件 ${j+1}" data-event="${i}" data-condition="${j}">${esc(c.value)}</textarea>${refs(c,i,'condition:'+j)}<button type="button" class="remove" data-remove-condition="${j}" data-event="${i}">この条件を削除</button></div>`).join('')||'<p class="muted">この出力には条件が記録されていません。</p>'}<button type="button" data-add-condition="${i}">＋ 条件を追加</button></div><label for="notes-${i}">補足（不要な場合は空欄）</label><textarea rows="3" id="notes-${i}" data-event="${i}" data-field="notes_ja">${esc(e.notes_ja?.value||'')}</textarea>${refs(e.notes_ja||{block_numbers:[]},i,'notes_ja')}<details><summary>Qwenの元の出力と比較</summary><p class="muted">この欄は編集前のまま保持されます。</p><pre>${esc(pretty(original||'追加された出来事'))}</pre></details></article>`;}
function renderNav(){document.querySelector('#navigation').innerHTML=state.payload.filings.map(r=>`<button class="${r.accession_number===selected?'active':''}" data-select="${r.accession_number}"><small>提出日 ${esc(r.as_of_date)}</small><span class="title">${r.events.map(e=>esc(e.subject.value)).join('<br>')||'出来事なし'}</span><span class="badge ${state.review[r.accession_number].status==='reviewed'?'ok':'warn'}">${reviewLabels[state.review[r.accession_number].status]}</span></button>`).join('');const done=Object.values(state.review).filter(x=>x.status==='reviewed').length;document.querySelector('#counts').textContent=`${state.payload.filings.length}資料 / ${state.payload.filings.reduce((n,x)=>n+x.events.length,0)}件の出来事 · 確認済み ${done}/${state.payload.filings.length}`;}
function controls(){const s=document.querySelector('#review-status');return s?{status:s.value,reviewer:document.querySelector('#reviewer').value,reason:document.querySelector('#reason').value,checks:Object.fromEntries([...document.querySelectorAll('[data-check]')].map(x=>[x.dataset.check,x.checked]))}:null;}
function render(preserved=null){renderNav();const doc=state.source_documents[selected],rev=preserved||state.review[selected];const history=state.history.filter(x=>x.accession_number===selected).slice().reverse();document.querySelector('#workspace').innerHTML=`<div class="bar" style="margin-bottom:14px"><h2 style="margin:0">提出日 ${esc(draft.as_of_date)}</h2><span class="badge ok">保存済みデータは機械検査通過</span></div><p class="muted" style="margin-bottom:18px">${esc(selected)} · Item ${esc(doc.item)} · 内容の正しさ・漏れは原文で確認します。</p><div class="columns"><section class="panel" aria-label="抽出内容">${draft.events.map(eventHTML).join('')||'<p>出来事が0件です。抽出漏れがないか原文を確認してください。</p>'}<details id="advanced-details"><summary>詳細な項目を編集する</summary><p class="muted">氏名・状態・日付・出来事の追加や削除などを変更する場合に使用します。原文の識別情報は変更できません。</p><textarea id="advanced" rows="20" aria-label="詳細JSON">${esc(JSON.stringify(draft,null,2))}</textarea><button id="apply-advanced" type="button">詳細の変更を画面に反映</button><p id="advanced-message" class="muted"></p></details></section><aside class="panel source" aria-label="根拠の原文"><div class="bar"><h2 style="margin:0">根拠の原文</h2><a href="${esc(doc.document_url)}" target="_blank" rel="noopener noreferrer">SEC原文 ↗</a></div><p class="muted">対象のItem全文。番号は本文を整形したブロック番号です。</p>${doc.blocks.map(b=>`<section class="block" id="block-${b.block_number}"><b class="muted">BLOCK ${b.block_number}</b><p>${esc(b.text)}</p></section>`).join('')}</aside></div><section class="panel"><h2>この資料の確認記録</h2><p class="muted">要約だけでなく、同じ資料の全ての出来事を確認してから保存します。</p><div class="review-grid"><div><label for="reviewer">確認者名</label><input id="reviewer" value="${esc(rev.reviewer)}" placeholder="氏名または識別できる名前"><label for="review-status">確認状態</label><select id="review-status">${Object.entries(reviewLabels).map(([k,v])=>`<option value="${k}" ${rev.status===k?'selected':''}>${v}</option>`).join('')}</select></div><div>${[['identity','人物・組織・役割の対応を確認した'],['dates','日付・予定と完了の区別を確認した'],['conditions','条件・留保と抽出漏れを確認した'],['evidence','各項目の根拠が内容を支えることを確認した']].map(([k,v])=>`<label class="review-check"><input type="checkbox" data-check="${k}" ${rev.checks[k]?'checked':''}>${v}</label>`).join('')}</div></div><label for="reason">修正・判断の理由</label><textarea id="reason" rows="2" placeholder="例：原文45の就任条件を追加。予定であることを要約に明記。">${esc(rev.reason)}</textarea><div class="review-actions"><button id="save" class="primary">検査して保存</button><span class="muted">機械検査に通らない変更は保存されません。</span></div><p class="footer">保存先：${esc(state.record_path)}<br>原文とQwenの元の出力は保持されます。「担当者が確認済み」は公開や第三者への送信を行いません。</p></section><section class="panel"><h2>変更・確認の履歴</h2>${history.map(h=>`<div class="history-entry"><b>版 ${h.revision} · ${esc(reviewLabels[h.status])}</b><p class="muted">${esc(new Date(h.at_utc).toLocaleString('ja-JP'))} · ${esc(h.reviewer)}</p><p>${esc(h.reason)}</p><ul class="changes">${h.changes.map(c=>`<li><b>${esc(c.path)}</b><div class="before">変更前：${esc(pretty(c.before))}</div><div class="after">変更後：${esc(pretty(c.after))}</div></li>`).join('')||'<li>内容変更なし。確認状態・判断を記録しました。</li>'}</ul></div>`).join('')||'<p class="muted">保存すると、変更前後・理由・確認者・日時がここに表示されます。</p>'}</section><p class="footer">既知の開発用3資料です。未知資料に対する抽出精度や予定の実現を評価したものではありません。実行ID：${esc(state.run_id)}</p>`;}
function collect(){const result=clone(draft);document.querySelectorAll('[data-field]').forEach(el=>{const e=result.events[+el.dataset.event],k=el.dataset.field;if(k==='notes_ja')e[k]=el.value.trim()?{value:el.value,block_numbers:e[k]?.block_numbers||[]}:null;else e[k].value=el.value;});document.querySelectorAll('[data-condition]').forEach(el=>result.events[+el.dataset.event].conditions[+el.dataset.condition].value=el.value);document.querySelectorAll('[data-refs]').forEach(el=>{const e=result.events[+el.dataset.event],k=el.dataset.refs,f=k.startsWith('condition:')?e.conditions[+k.split(':')[1]]:e[k];if(f)f.block_numbers=parseRefs(el.value);});return result;}
function updateAdvanced(){const el=document.querySelector('#advanced');if(el&&el.dataset.unapplied!=='true'){try{el.value=JSON.stringify(collect(),null,2);}catch(_){}}}
document.addEventListener('input',event=>{if(event.target.closest('#workspace')){if(event.target.id==='advanced'){markDirty();document.querySelector('#advanced-message').textContent='この変更はまだ反映されていません。上のボタンで反映してください。';event.target.dataset.unapplied='true';}else if(event.target.matches('[data-field],[data-condition],[data-refs]')){markDirty();updateAdvanced();}else{dirty=true;document.querySelector('#dirty').textContent='未保存の変更あり';}}});
document.addEventListener('click',async event=>{const t=event.target.closest('button');if(!t||busy)return;try{
if(t.dataset.block){document.querySelectorAll('.block.highlight').forEach(x=>x.classList.remove('highlight'));const b=document.querySelector('#block-'+t.dataset.block);if(b){b.classList.add('highlight');b.scrollIntoView({behavior:'smooth',block:'nearest'});}return;}
if(t.dataset.select){if(dirty&&!confirm('未保存の変更を破棄して別の資料を開きますか？'))return;selected=t.dataset.select;draft=clone(state.payload.filings.find(x=>x.accession_number===selected));dirty=false;document.querySelector('#dirty').textContent='';render();return;}
if(t.dataset.addCondition!==undefined||t.dataset.removeCondition!==undefined){if(document.querySelector('#advanced')?.dataset.unapplied==='true')throw Error('先に詳細の変更を反映してください。');draft=collect();markDirty();const keep=controls();if(t.dataset.addCondition!==undefined){const e=draft.events[+t.dataset.addCondition];e.conditions.push({value:'',block_numbers:[...e.summary_ja.block_numbers]});}else draft.events[+t.dataset.event].conditions.splice(+t.dataset.removeCondition,1);render(keep);return;}
if(t.id==='apply-advanced'){const next=JSON.parse(document.querySelector('#advanced').value);if(!Array.isArray(next.events)||!Array.isArray(next.missing_information))throw Error('eventsとmissing_informationを配列で指定してください。');busy=true;t.disabled=true;await api('/api/validate',{filing:next});for(const k of ['accession_number','source_sha256','section_id','section_sha256','as_of_date'])if(next[k]!==draft[k])throw Error('原文の識別情報と提出日は変更できません。');for(const e of next.events){for(const k of ['subject','organization','event_type','summary_ja','status_as_of_filing'])if(typeof e[k]?.value!=='string'||!Array.isArray(e[k]?.block_numbers))throw Error('項目のvalueとblock_numbersを確認してください。');if(!Array.isArray(e.conditions)||!Array.isArray(e.dates)||(e.notes_ja!==null&&(typeof e.notes_ja?.value!=='string'||!Array.isArray(e.notes_ja?.block_numbers))))throw Error('条件・日付・補足の形式を確認してください。');}markDirty();const keep=controls();draft=next;render(keep);message('詳細の変更を画面に反映しました。「検査して保存」で確定してください。');return;}
if(t.id==='save'){if(document.querySelector('#advanced')?.dataset.unapplied==='true')throw Error('詳細JSONの変更が未反映です。「詳細の変更を画面に反映」を押してください。');const body={...controls(),base_revision:state.revision,accession_number:selected,filing:collect()};busy=true;t.disabled=true;state=await api('/api/save',body);draft=clone(state.payload.filings.find(x=>x.accession_number===selected));dirty=false;document.querySelector('#dirty').textContent='';render();message('検査を通過し、版 '+state.revision+' として保存しました。');return;}
if(t.id==='download'){if(dirty)throw Error('未保存の編集があります。先に保存するか、ページを再読込して破棄してください。');const v=await api('/api/export');const url=URL.createObjectURL(new Blob([JSON.stringify(v,null,2)+'\n'],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='nvidia_qwen27b_review_record.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('確認記録を書き出しました。未確認の資料は未確認のまま含まれます。');}
}catch(e){message(e.message,true);}finally{busy=false;if(t.id==='save'||t.id==='apply-advanced')t.disabled=false;}});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
api('/api/state').then(v=>{state=v;selected=v.payload.filings[0].accession_number;draft=clone(v.payload.filings[0]);render();}).catch(e=>message(e.message,true));
</script></body></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', type=Path, default=Path.home() / 'projects/nvidia_evidence_extractor')
    parser.add_argument('--run', type=Path, help='完了したQwen 27Bの実行フォルダ。省略時は最新の完了実行')
    parser.add_argument('--source-dir', type=Path, action='append')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--check', action='store_true', help='原文とログを照合し、書き込まず終了')
    args = parser.parse_args(argv)
    try:
        store = ReviewStore(args.project, args.run, args.source_dir)
        view = store.view()
        print(json.dumps({'status': 'ready', 'model': MODEL, 'filings': len(view['payload']['filings']),
                          'events': sum(len(r['events']) for r in view['payload']['filings']),
                          'run_id': store.run.name, 'review_revision': view['revision'],
                          'llm_requests': 0}, ensure_ascii=False), flush=True)
        if args.check:
            return 0
        if not 1024 <= args.port <= 65535:
            raise ReviewError('ポート番号は1024～65535を指定してください。')
        server = HTTPServer(('127.0.0.1', args.port), handler_for(store, secrets.token_hex(24)))
        server.timeout = 1
        print(f'確認画面: http://127.0.0.1:{args.port}/\n終了: Ctrl+C', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\n終了しました。保存した確認記録は次回も読み込めます。')
        finally:
            server.server_close()
        return 0
    except (ReviewError, OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
