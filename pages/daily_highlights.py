"""Prepare local 8-K review candidates. Only an explicit decision can add text to Pages inputs."""
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from http.client import HTTPException
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

import llm_runner
import narratives
import read_sections
import manage
from manage import digest, load, write_atomic
from companies import ISSUERS
from narratives import metadata

MODEL = 'nvidia-qwen38-27b:16k'
PROJECT = Path(__file__).resolve().parents[1]
STATES = dict(pending='処理待ち',retry='次回再試行',done='抽出済み・担当者確認待ち',manual='原文確認が必要',attachment='添付一覧のみ')


def local_config():
    config=load(PROJECT/'llm/ollama-qwen38-27b.json')
    llm_runner.check_config(config)
    if config['base_url']!='http://127.0.0.1:11434/v1' or config['model']!=MODEL or config['api_key_env'] is not None:
        raise ValueError('Expected the configured Windows loopback Ollama model.')
    return config


def model_ready():
    opener=build_opener(ProxyHandler({}),llm_runner.NoRedirect())
    with opener.open('http://127.0.0.1:11434/api/tags',timeout=5) as resp:
        models=json.load(resp)['models']
    if not any(m.get('name')==MODEL for m in models):
        raise ValueError('Ollama model missing: '+MODEL)


def pieces(sec):
    # Preserve original numbered paragraphs; leave an oversized individual paragraph for manual review.
    groups=[]
    current=[]
    size=0
    for block in sec['blocks']:
        length=len(block.encode('utf-8'))+32
        if length>12000: raise ValueError('長い単一段落を原文で確認してください。')
        if current and size+length>12000:
            groups.append(current);current=[];size=0
        current.append(block);size+=length
    if current: groups.append(current)
    first=sec['block_start']
    for i,blocks in enumerate(groups,1):
        text='\n'.join(blocks)
        # Reserve a separate numbered range for each original Item when it needs multiple prompts.
        sid=sec['section_id'] if len(groups)==1 else 'section-'+str(100000000+int(sec['section_id'][8:])*10000+i)
        yield dict(sec,section_id=sid,blocks=blocks,text=text,text_sha256=sha256(text.encode()).hexdigest(),
                   block_start=first,block_end_inclusive=first+len(blocks)-1,character_count=len(text),block_count=len(blocks))
        first+=len(blocks)


def extract(folder, job, fetch, config, retry=False, runner=llm_runner.run):
    record=job['record']
    acc,symbol=record['accession_number'],record['ticker']
    raw=fetch(record['document_url'])
    version=sha256(raw).hexdigest()
    parsed=read_sections.split_sections(raw)
    notes=list(parsed['warnings'])
    items=set(s['item'] for s in parsed['sections'])
    absent=set(record['api_items'])-items
    if absent: notes.append('本文で見出しを照合できなかったItem: '+', '.join(sorted(absent)))
    candidate=dict(metadata(record),sections={})
    job.update(notes=notes,version=version)
    transient=None
    for sec in parsed['sections']:
        if not sec['heading_recognized'] or sec['item']=='9.01': continue
        try:
            chunks=list(pieces(sec))
        except ValueError as error:
            notes.append(str(error));continue
        for chunk in chunks:
            sid=chunk['section_id']
            ctx=dict(issuer=ISSUERS[symbol][0],status='sections_ready',section=chunk,source_sha256=version,
                     filing_date=record['filing_date'],document_url=record['document_url'])
            doc=llm_runner.document_input(acc,ctx)
            fingerprint=digest(llm_runner.make_request(config,doc))
            base=folder/'runs'/symbol/acc/version/sid/fingerprint
            logs=sorted(base.glob('*/run.json'),reverse=True)
            good=next((p for p in logs if load(p)['status']=='completed_pending_review' and (p.parent/'candidate.json').is_file()),None)
            dest=good.parent if good else None
            if dest is None:
                previous=load(logs[0]) if logs else {}
                validation_error=previous.get('status')=='mechanical_validation_failed'
                if validation_error and not retry:
                    notes.append(sid+' の機械検証に失敗しました。');continue
                dest=base/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                _, log=runner(config,{acc:ctx},dest,execute=True)
                if log['status']!='completed_pending_review':
                    why=sid+' '+log['status']+' '+log.get('reason','')
                    notes.append(why)
                    if log['status']!='mechanical_validation_failed':
                        transient=why
                        break
                    continue
            payload=load(dest/'candidate.json')
            checked=llm_runner.events.validate(payload,{acc:ctx})
            if not checked['mechanical_checks_passed']:
                notes.append(sid+' 保存された候補と原文が一致しません。');continue
            filing = payload['filings'][0]
            if filing['extraction_status'] == 'needs_review':
                notes.append(sid+' モデルが本文の判断を保留しました。')
            candidate['sections'][sid]=narratives.section(filing,doc,'pending')
        if transient: break
    if not parsed['sections']: notes.append('本文をItem単位に分割できませんでした。')
    job['candidate']=candidate
    if transient:
        job['state']='retry'
    elif notes:
        job['state']='manual'
    elif not candidate['sections']:
        job['state']='attachment' if items=={'9.01'} else 'manual'
    else:
        job['state']='done'
    return transient


def process_queue(folder, queue, fetch, retry, persist, summary):
    jobs=[v for v in queue.values() if v['state'] in ('pending','retry') or (retry and v['state']=='manual')]
    if not jobs: return
    try:
        config=local_config()
        model_ready()
    except (OSError, ValueError, HTTPException, llm_runner.TrialError) as exc:
        summary['errors']['Qwen']=str(exc)
        return
    for job in sorted(jobs,key=lambda j:j['record']['filing_date'],reverse=True):
        try:
            failure=extract(folder,job,fetch,config,retry)
        except (OSError,HTTPException,KeyError,TypeError,ValueError) as exc:
            job.update(state='retry',notes=[str(exc)])
            failure=str(exc)
        persist()
        if failure:
            summary['errors'][job['record']['accession_number']]=failure
            if 'network_error' in failure: break


def ledger_records(html, quarters):
    """Mirror issuerFilings() in the Pages UI, including quarter-only 8-K links."""
    entries = json.loads(html.split(narratives.MARKER, 1)[1].split('</script>', 1)[0])
    records = {}
    for sym in ISSUERS:
        batch = entries[sym]
        for row in quarters[sym]:
            if row['accession'] and row['filing']:
                batch.setdefault(row['accession'], dict(date=row['releaseDate'], form='8-K', url=row['filing'], items=[]))
        for acc, row in batch.items():
            records[sym+'/'+acc] = dict(ticker=sym, accession_number=acc, filing_date=row['date'],
                form=row['form'], document_url=row['url'], api_items=row['items'])
    return records


def index_page(folder, queue, summary):
    view=folder/'review'
    base=(manage.OUTPUT/'index.html').read_text()
    marker='<script type="application/json" id="narrative-filings">'
    original=json.loads(base.split(marker,1)[1].split('</script>',1)[0])
    listing=['<!doctype html><html lang="ja"><meta charset="utf-8"><title>開示要点の確認候補</title>',
             '<style>body{max-width:64rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui;color:#203b50}article{border-top:1px solid #ccd5da;padding:.6rem 0}h3,p{margin:.35rem 0}code{overflow-wrap:anywhere}a{color:#125885}</style>',
             '<h1>開示要点の確認候補</h1><p>ローカルで抽出した8-K本文の候補です。要点と原文の段落を見比べてください。</p>',
             '<p>最終チェック '+escape(summary['checked_at'])+'</p>']
    targets = ledger_records(base, manage.inputs()[0])
    records = {}
    for key, record in targets.items():
        job = queue.get(key, {})
        records[key] = dict(record=record, state=job.get('state', 'pending'), notes=job.get('notes', []),
                            candidate=job.get('candidate'))
    states = {key:row['state'] for key, row in records.items()}
    complete = sum(state == 'done' for state in states.values())
    attachment = sum(state == 'attachment' for state in states.values())
    manual = sum(state == 'manual' for state in states.values())
    remaining = len(states) - complete - attachment - manual
    listing.append(f'<p>台帳掲載 {len(targets)}件 · 抽出済 {complete}件 · 添付一覧のみ {attachment}件 · '
                   f'原文確認が必要 {manual}件 · 未処理 {remaining}件</p>')
    report = dict(model=MODEL, checked_at=summary['checked_at'], total=len(targets), extracted=complete,
                  attachment_only=attachment, manual=manual, outstanding=remaining, errors=summary['errors'],
                  filings=records)
    write_atomic(view/'backfill-results.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
    listing.append('<p><a href="backfill-results.json" download>抽出結果と根拠をダウンロード（JSON）</a></p>')
    for key,msg in summary['errors'].items():listing.append('<p>'+escape(key+' · '+msg)+'</p>')
    for sym,issues in summary.get('incidents',{}).items():
        for item in issues:
            listing.append('<p>'+escape(sym+' · '+item['acc']+' · '+item['reason'])+'</p>')
    for sym in sorted(ISSUERS):
        listing.append('<h2>'+escape(ISSUERS[sym][0])+'</h2>')
        batch=sorted((j for j in queue.values() if j['record']['ticker']==sym),key=lambda j:j['record']['filing_date'],reverse=True)
        if not batch:listing.append('<p>対象の新着書類はありません。</p>')
        for job in batch:
            r=job['record'];acc=r['accession_number']
            entry=job.get('candidate') or metadata(r)
            # Keep existing published decisions visible when their exact evidence matches.
            previous = original.setdefault(sym, {}).get(acc, {})
            private_sections = dict(entry['sections'])
            for sid, approved in previous.get('sections', {}).items():
                candidate = private_sections.get(sid)
                if approved.get('state') == 'reviewed' and (not job.get('version') or
                        approved['sourceHash'] == job['version']) and (
                        candidate is None or dict(approved, state='pending') == dict(candidate, state='pending')):
                    private_sections[sid] = approved
            original[sym][acc] = dict(entry, sections=private_sections)
            url='detail.html?'+urlencode(dict(lang='ja',company=sym,tab='events',filing=acc))
            listing.append('<article><h3><a href="'+escape(url,quote=True)+'">'+escape(r['filing_date']+' · '+r['form']+' · '+acc)+'</a></h3><p>'+escape(STATES[job['state']])+'</p>')
            for note in job.get('notes',[]): listing.append('<p>'+escape(note)+'</p>')
            for sid,section in entry['sections'].items():
                if private_sections[sid].get('state') == 'reviewed':
                    listing.append('<p>'+escape(sid+' · 確認済み')+'</p>')
                    continue
                cmd='.venv-pages/bin/python pages/daily.py --approve '+' '.join([sym,acc,sid,digest(section)])
                listing.append('<details><summary>'+escape('Item '+section['item']+' · '+sid+' を確認した後の承認コマンド')+'</summary><code>'+escape(cmd)+'</code></details>')
            listing.append('</article>')
    listing.append('</html>')
    # Private directory contains only these two display files. It is outside the Pages build.
    import demo_history
    private_page=demo_history.replace_data(base,marker,original).replace("excel.href='./reports/'", "excel.href='../reports/'")
    write_atomic(view/'detail.html',private_page.encode())
    write_atomic(view/'index.html',''.join(listing).encode())
