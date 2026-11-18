"""Local NVIDIA register. Run with the extractor project's Python environment."""
import argparse
from contextlib import closing
import csv
from copy import deepcopy
import io
import json
import secrets
import sqlite3
import sys
from hashlib import sha256
from html import escape as h
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import reporting
import companies
import microsoft
import alphabet
import issuer_financials
import periods
import pipeline
import workbench
import demo_history
import narratives
import llm_runner
import read_sections
import structured_events as events
from nvidia_qwen27b_review import ReviewStore, ReviewError


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate(filing, ctx):
    result = events.validate({"schema_version": events.VERSION, "filings": [filing]},
                             {ctx["accession_number"]: ctx})
    if result["errors"]:
        raise ValueError(result["errors"][0]["message"])


class Register:
    def __init__(self, root):
        self.root = root
        folder = root / "register_data"
        folder.mkdir(exist_ok=True)
        self.db = sqlite3.connect(folder / "reviews.sqlite3")
        self.db.execute("""CREATE TABLE IF NOT EXISTS reviews (
            accession TEXT, section TEXT, fingerprint TEXT, revision INTEGER,
            state TEXT, payload TEXT, PRIMARY KEY(accession,section))""")
        reporting.initialize(self)
        self.token = secrets.token_urlsafe(32)
        self.refresh()

    def refresh(self):
        manifest = read(self.root / "section_manifest.json")["filings"]
        docs = {}
        for record in manifest:
            docs[record["accession_number"]] = self.document(record)
        try:
            view = ReviewStore(self.root).view()
        except (OSError, ValueError, ReviewError) as exc:
            view = None
            legacy_error = str(exc)
        if view:
            for f in view["payload"]["filings"]:
                acc = f["accession_number"]
                doc = docs[acc]
                source = view["source_documents"][acc]
                blocks = source["blocks"]
                ctx = dict(accession_number=acc, filing_date=source["as_of_date"], source_sha256=source["source_sha256"],
                           document_url=doc["record"]["document_url"], status="sections_ready",
                           section=dict(section_id=source["section_id"], item=source["item"], text_sha256=source["section_sha256"],
                                        block_start=blocks[0]["block_number"], blocks=[b["text"] for b in blocks]))
                self.add(doc, f, ctx, view["review"][acc]["status"], view["model"])
        for doc in docs.values():
            known_items = {s["ctx"]["section"]["item"] for s in doc["sections"].values()}
            if set(doc["record"]["api_items"]) - known_items - {"9.01"}:
                doc["issues"].append("未抽出の項目あり")
            if not view:
                doc["issues"].append("既存レビュー取込要確認：" + legacy_error)
        for path in sorted((self.root / "nvidia_inbox").glob("*/record.json")):
            acc = path.parent.name
            try:
                record = read(path)
                if record["ticker"] != "NVDA" or acc != record["accession_number"]:
                    raise ValueError("保存記録と提出番号が不一致")
                doc = docs.setdefault(acc, self.document(record))
                if doc["record"]["sha256"] != record["sha256"]:
                    raise ValueError("同じ提出番号の原文が不一致")
                self.inbox(doc)
            except (KeyError, ValueError, OSError) as exc:
                doc = docs.setdefault(acc, {"record":{"accession_number":acc,"filing_date":"","document_url":""},
                                           "sections":{}, "issues":[]})
                doc["issues"].append("取込要確認：" + str(exc))
        self.docs = docs
        for acc,doc in docs.items():
            try:
                reporting.metrics(self,acc)
            except (ValueError,OSError,KeyError) as exc:
                doc['metric_error']=str(exc)
                doc['issues'].append('決算資料要確認：'+str(exc))
        with self.db: reporting.observe(self)

    @staticmethod
    def document(record):
        issues = ["添付本文は未読（Item 9.01）"] if "9.01" in record["api_items"] else []
        return {"record":record, "issues":issues, "sections":{}}

    def add(self, doc, f, ctx, state, model=None):
        validate(f, ctx)
        sid = ctx["section"]["section_id"]
        doc["sections"][sid] = dict(ctx=ctx, original=f, state=state, model=model,
                                    fingerprint=digest([f, state]))

    def inbox(self, doc):
        r = doc["record"]
        acc = r["accession_number"]
        src = read_sections.inspect_source(r, [self.root / "nvidia_inbox"])
        doc["issues"].extend(src["warnings"])
        if not src["sections"]:
            doc["issues"].append("原文要確認：" + src["status"])
        for s in src["sections"]:
            sid = s["section_id"]
            if s["item"] == "9.01":
                if not any("添付本文" in x for x in doc["issues"]):
                    doc["issues"].append("添付本文は未読（Item 9.01）")
                continue
            prefix = "Item " + s["item"] + " "
            ctx = dict(section=s, accession_number=acc, source_sha256=r["sha256"],
                       filing_date=r["filing_date"], document_url=r["document_url"], status="sections_ready")
            folder = self.root / "inbox_reviews" / acc / r["sha256"] / sid
            logs = sorted(folder.glob("*/run.json"), reverse=True)
            completed = next((p for p in logs if read(p)["status"] == "completed_pending_review"), None)
            if not completed:
                if sid not in doc["sections"]:
                    state = "未抽出"
                    if logs:
                        status = read(logs[0])["status"]
                        state = ("抽出中・中断の可能性" if status == "preparing" else "抽出失敗・要確認：" + status)
                    doc["issues"].append(prefix + state)
                continue
            data = read(completed.parent / "candidate.json")
            if len(data["filings"]) != 1:
                raise ValueError("対象の提出書類が一意ではありません")
            if sid not in doc["sections"]:
                self.add(doc, data["filings"][0], ctx, "pending", read(completed).get("config", {}).get("model"))

    def current(self, acc, sid):
        s = self.docs[acc]["sections"][sid]
        row = self.db.execute("SELECT fingerprint, revision, state, payload FROM reviews WHERE accession=? AND section=?",
                              (acc,sid)).fetchone()
        if row is None:
            return deepcopy(s["original"]), s["state"], 0
        fp, rev, state, payload = row
        return json.loads(payload), state if fp == s["fingerprint"] else "changed", rev

    def save(self, acc, sid, form):
        s = self.docs[acc]["sections"][sid]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            filing, _, rev = self.current(acc,sid)
            if int(form["revision"]) != rev or form["fp"] != s["fingerprint"]:
                raise ValueError("表示後に更新されています。画面を開き直してください。")
            if "details" in form:
                filing = json.loads(form["details"])
                if not isinstance(filing, dict):
                    raise ValueError("節のJSONオブジェクトを入力してください")
            else:
                for i, event in enumerate(filing["events"]):
                    event["summary_ja"]["value"] = form[f"summary{i}"].strip()
            validate(filing, s["ctx"])
            state = form["state"]
            if state not in {"pending","reviewed","needs_revision"}:
                raise ValueError("確認状態が不正です")
            self.db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?,?)",
                            (acc,sid,s["fingerprint"],rev+1,state,json.dumps(filing,ensure_ascii=False)))
            reporting.observe(self)

    def export(self):
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(["提出日","提出番号","節ID","Item","出来事ID","対象","組織","開示内容","日付（役割・確度）","条件","補足","要約の根拠段落","SEC原文URL"])
        for acc, doc in self.ordered():
            for sid, section in doc["sections"].items():
                filing, state, _ = self.current(acc,sid)
                if state != "reviewed":
                    continue
                for e in filing["events"]:
                    row = [filing["as_of_date"], acc, sid, section["ctx"]["section"]["item"], e["event_id"],
                           e["subject"]["value"], e["organization"]["value"], e["summary_ja"]["value"],
                           " / ".join(f"{v['role']} {v['value'] or '不明'} ({v['certainty']})" for v in e["dates"]),
                           " / ".join(v["value"] for v in e["conditions"]),
                           e["notes_ja"]["value"] if e["notes_ja"] else "",
                           ", ".join(map(str,e["summary_ja"]["block_numbers"])), doc["record"]["document_url"]]
                    writer.writerow(["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in row])
        return out.getvalue().encode("utf-8-sig")

    def ordered(self):
        return sorted(self.docs.items(), key=lambda pair:(pair[1]["record"]["filing_date"],pair[0]),reverse=True)


LABEL = {"pending":"未確認","reviewed":"確認済み","needs_revision":"要修正","changed":"元データ更新・再確認"}
STYLE = """*{box-sizing:border-box}html{background:#f5f7f1;color:#182f28;font:15px/1.65 'Inter','Segoe UI',system-ui,sans-serif;font-variant-numeric:tabular-nums}body{margin:0}header{background:#102e29;color:#f5f8ed;border-bottom:3px solid #c6ee72}header nav,main{max-width:1280px;margin:auto;padding-inline:clamp(16px,4vw,56px)}header nav{min-height:62px;display:flex;align-items:center;justify-content:space-between;gap:14px}header a{color:inherit}header strong{font-size:17px;letter-spacing:-.04em}main{padding-block:26px 48px}h1{font-size:clamp(26px,3vw,39px);letter-spacing:-.055em;margin:0 0 20px}h2{font-size:22px;letter-spacing:-.035em;margin:24px 0 12px}a{color:#1a5b47;text-underline-offset:.18em}button,select,input,textarea{font:inherit;max-width:100%}button,select,input{padding:6px 11px;border:1px solid #b5c3b5;border-radius:4px;background:white;color:#183b2d}button{cursor:pointer;background:#214939;color:white}button:hover{background:#17382c}form{margin:12px 0}details,article{padding:13px 16px;border:1px solid #d0dace;background:white;border-radius:5px;margin:12px 0}summary{cursor:pointer;font-weight:650}textarea{width:100%;padding:8px;border:1px solid #b5c3b5}table{display:block;overflow-x:auto;border-collapse:collapse;background:white;border:1px solid #d1dbd0;border-radius:5px;width:100%}tbody{display:table;width:100%}th,td{text-align:left;padding:10px 14px;vertical-align:top;border-bottom:1px solid #dee5da}th{background:#eaf0e5;white-space:nowrap;font-size:13px}td{overflow-wrap:anywhere}tr:last-child td{border-bottom:0}p:target{background:#eff9cc}:focus-visible{outline:3px solid #08765b;outline-offset:2px}.actions{display:flex;gap:9px;flex-wrap:wrap}.actions a{padding:5px 10px;background:white;border:1px solid #c6d1c3;border-radius:5px;text-decoration:none}.actions a:hover{text-decoration:underline}.reports{margin:15px 0}article{background:#f9fbf6}@media(max-width:600px){header strong{font-size:14px}th,td{padding:8px 9px}input{margin:4px 0}main{padding-inline:14px}h1{overflow-wrap:anywhere}}
"""


def hidden(name,value):
    return f'<input type="hidden" name="{name}" value="{h(str(value))}">'


def page(title, content):
    company_links = ''.join(
        f'<a href="/company/{symbol}?lang=ja">{h(name)}</a>'
        for symbol, (name, _) in sorted(companies.ISSUERS.items(), key=lambda item: item[1][0]))
    return f"""<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{h(title)} | Company Disclosure Monitor</title><style>{STYLE}
header {{background:white;color:#293e58;border-bottom:1px solid #d7dfe6}}
header .mast,header nav {{max-width:1280px;margin:auto;padding-inline:clamp(16px,4vw,56px)}}
header .mast {{padding-block:.8rem;border-bottom:1px solid #e5e9ed}}
header nav {{min-height:0;justify-content:flex-start;gap:.1rem .3rem;flex-wrap:wrap;padding-block:.25rem;flex-direction:row;align-items:center}}
header nav a {{padding:.4rem .65rem;text-decoration:none;border-radius:4px;white-space:nowrap}}
header .mast a {{text-decoration:none}}header a:hover {{text-decoration:underline}}
header nav a:hover {{background:#edf2f6}}.local-tools {{font-size:.9rem;border-top:1px solid #e5e9ed}}
</style><body><header><div class="mast"><a href="/demo?lang=ja"><strong>Company Disclosure Monitor</strong></a></div>
<nav aria-label="決算開示モニターと企業別台帳"><a href="/demo?lang=ja">決算開示モニター</a>{company_links}</nav>
<nav class="local-tools" aria-label="ローカルの確認画面"><a href="/companies">7社の提出書類一覧</a>
<a href="/workflow">四半期決算の照合</a><a href="/">NVIDIAの開示レビュー</a></nav></header>
<main><h1>{h(title)}</h1>{content}</main></body></html>""".encode()


def index(reg, needs):
    rows = []
    projection = narratives.collect(reg.root, reg)["NVDA"]
    for acc, doc in reg.ordered():
        issues = list(doc["issues"])
        m = reporting.metrics(reg,acc)
        if m:
            issues = [x for x in issues if "添付本文は未読" not in x]
            for check in m[0]['checks']:
                if check['status'] in ('不一致','比較資料なし'):
                    issues.append(check['metric']+'：'+check['status'])
            if m[1] != 'reviewed': issues.append('決算指標が未確認')
        count = 0
        for sid in doc["sections"]:
            f, state, _ = reg.current(acc,sid)
            count += len(f["events"])
            if not f["events"]:
                msg = "候補0件・" + LABEL[state]
            else:
                msg = LABEL[state]
            if state != "reviewed":
                issues.append(sid + " " + msg)
        for sid, saved in projection.get(acc, {}).get("sections", {}).items():
            if sid not in doc["sections"]:
                count += saved["count"]
                if saved["state"] != "reviewed":
                    issues.append("Qwen · Item " + saved["item"] + " " + LABEL[saved["state"]])
        if needs and not issues:
            continue
        r = doc["record"]
        rows.append(f'<tr><td>{h(r["filing_date"])}</td><td><a href="/filing?acc={h(acc)}">{h(acc)}</a></td>'
                    f'<td>{count}</td><td>{h(" ／ ".join(issues) or "確認済み")}</td></tr>')
    return page("NVIDIAの開示レビュー", '<nav class="actions" aria-label="台帳メニュー"><a href="/">全件</a>　<a href="/?needs=1">要対応のみ</a>　<a href="/reviewed.csv">確認済み出来事CSV</a>　<a href="/financial.csv">確認済み決算指標CSV</a>　<a href="/checks.csv">照合検査CSV</a>　<a href="/coverage">期間カバレッジ</a></nav>'
                + '<form method="post" action="/refresh">'+hidden("token",reg.token)+'<button>保存済みの開示を再取り込み</button></form>'+reporting.notices(reg)+reporting.controls(reg,hidden)
                + f'<p>表示 {len(rows)} 書類</p><table><tr><th>提出日</th><th>提出番号</th><th>出来事数</th><th>確認状況</th></tr>{"".join(rows)}</table>')


def detail(reg, acc):
    doc = reg.docs[acc]
    parts = ['<a href="/">NVIDIAの開示レビューに戻る</a>',f'<p><a href="{h(doc["record"]["document_url"])}">SEC原文</a></p>']
    parts.append(f'<p><a href="{h(companies.narrative_url("NVDA",acc))}">この8-Kの開示要点</a></p>')
    financial = reporting.metric_page(reg,acc,hidden)
    if financial: parts.append(financial)
    parts.extend("<p>"+h("99.1の決算表を取得済み。他の添付は未確認" if financial and "添付本文は未読" in i else i)+"</p>" for i in doc["issues"])
    for sid,s in doc["sections"].items():
        f,state,rev = reg.current(acc,sid)
        section = s["ctx"]["section"]
        parts.append(f'<h2>Item {h(section["item"])}　{LABEL[state]}</h2>')
        start = '<form method="post" action="/save">' + ''.join(hidden(k,v) for k,v in
                dict(token=reg.token,acc=acc,sid=sid,revision=rev,fp=s["fingerprint"]).items())
        choices = ''.join(f'<option value="{k}" {"selected" if k==state else ""}>{LABEL[k]}</option>'
                          for k in ("pending","needs_revision","reviewed"))
        end = '<label>この節の確認状態 <select name="state">'+choices+'</select></label> <button>保存</button></form>'
        parts.append(start)
        if not f["events"]:
            parts.append("<p>抽出候補0件。本文を確認してください。</p>")
        for i,e in enumerate(f["events"]):
            refs = ' '.join(f'<a href="#{sid}-{n}">¶{n}</a>' for n in e["summary_ja"]["block_numbers"])
            parts.append(f'<article><b>{h(e["subject"]["value"])}</b>　{refs}<br><label>開示内容<textarea name="summary{i}">{h(e["summary_ja"]["value"])}</textarea></label>')
            fields = [("組織",e["organization"]),("分類",e["event_type"]),("提出時点の状態",e["status_as_of_filing"])]
            fields += [(d["role"]+" ／ "+d["certainty"],d) for d in e["dates"]]
            fields += [("条件",d) for d in e["conditions"]]
            if e["notes_ja"]: fields.append(("補足",e["notes_ja"]))
            for label,fact in fields:
                links = ' '.join(f'<a href="#{sid}-{n}">¶{n}</a>' for n in fact["block_numbers"])
                parts.append(f'<div>{h(label)}　{h(str(fact["value"] or "不明"))}　{links}</div>')
            parts.append("</article>")
        parts.extend("<p>"+h(note)+"</p>" for note in f["missing_information"])
        parts.append(end+'<details><summary>対象・日付・条件・根拠段落の詳細編集（JSON）</summary>'+start+
                     '<textarea name="details" rows="18">'+h(json.dumps(f,ensure_ascii=False,indent=2))+'</textarea>'+end+'</details>')
        for i,b in enumerate(section["blocks"],section["block_start"]):
            parts.append(f'<p id="{sid}-{i}"><b>¶{i}</b> {h(b)}</p>')
    return page(acc,"".join(parts))


def application(reg,port):
    def route(env,reply):
        path = env["PATH_INFO"]
        method = env["REQUEST_METHOD"]
        headers = [("Cache-Control","no-store"),("X-Frame-Options","DENY")]
        status = "200 OK"
        mime = "text/html; charset=utf-8"
        try:
            if env.get("HTTP_HOST") not in {f"127.0.0.1:{port}",f"localhost:{port}"}:
                raise ValueError("ローカルURLから開いてください")
            if method == "POST":
                size = int(env.get("CONTENT_LENGTH") or 0)
                if not 0 < size < 2000000:
                    raise ValueError("入力サイズを確認してください")
                form = {k:v[0] for k,v in parse_qs(env["wsgi.input"].read(size).decode(),keep_blank_values=True).items()}
                if not secrets.compare_digest(form["token"],reg.token):
                    raise ValueError("画面を開き直してください")
                if path == "/refresh":
                    reg.refresh()
                    target = "/"
                elif path == "/workflow-review":
                    with closing(pipeline.connect(reg.root)) as db:
                        pipeline.review(db,form)
                    query = workbench.filters(parse_qs(form.get('back','')))
                    target = '/workflow?' + urlencode({k:v for k,v in query.items() if v})
                elif path == "/workflow-sync":
                    pipeline.sync(reg.root,periods.fetcher(form["contact"]))
                    target = "/workflow"
                elif path == "/companies-fetch":
                    companies.refresh(reg.root, periods.fetcher(form["contact"]))
                    target = "/companies"
                elif path == "/coverage-fetch":
                    result=periods.run(reg.root,periods.fetcher(form['contact']),form.get('first'),form.get('last'))
                    target='/coverage?range='+result['first']+'-'+result['last']
                elif path == "/coverage-review":
                    periods.review(reg,form)
                    target='/coverage?range='+form['key']
                elif path == "/issue":
                    target = "/report?id=" + str(reporting.issue(reg,form))
                elif path == "/metrics":
                    reporting.save_metrics(reg,form)
                    target = "/filing?acc="+form["acc"]
                elif path == "/save":
                    reg.save(form["acc"],form["sid"],form)
                    target = "/filing?acc="+form["acc"]
                else:
                    raise KeyError(path)
                status,body = "303 See Other",b""
                headers.append(("Location",target))
            elif method == "GET" and path in ("/workflow", "/workflow.csv"):
                conditions = workbench.filters(parse_qs(env["QUERY_STRING"]))
                with closing(pipeline.connect(reg.root)) as db:
                    rows = workbench.selected(db,conditions)
                    if path.endswith(".csv"):
                        body = workbench.export(rows)
                        mime = "text/csv; charset=utf-8"
                        headers.append(("Content-Disposition",'attachment; filename="m7-quarterly.csv"'))
                    else:
                        body = page("四半期決算の照合",workbench.render(db,conditions,rows,hidden,reg.token))
            elif method == "GET" and path in ("/companies", "/companies.csv"):
                ticker, item = companies.filters(parse_qs(env["QUERY_STRING"]))
                data = companies.load(reg.root)
                rows = companies.matches(data, ticker, item)
                if path.endswith(".csv"):
                    body = reporting.csvdata(companies.csv_rows(data, rows))
                    mime = "text/csv; charset=utf-8"
                    headers.append(("Content-Disposition", 'attachment; filename="m7_filings.csv"'))
                else:
                    body = page("7社の提出書類一覧", companies.render(
                        data, rows, ticker, item, hidden("token", reg.token)))
            elif method == "GET" and (path == "/demo" or path.removeprefix("/company/") in companies.ISSUERS and path.startswith("/company/")):
                body = (Path(__file__).resolve().parent / "public_demo" / "index.html").read_bytes()
                symbol = (parse_qs(env["QUERY_STRING"]).get("company", [""])[0]
                          if path == "/demo" else path.rsplit("/", 1)[1])
                if symbol not in companies.ISSUERS:
                    symbol = ""
                pairs = {"MSFT": microsoft.PAIR, "GOOGL": alphabet.PAIR, **issuer_financials.PAIRS}
                if symbol in pairs and (local := pairs[symbol].local_result(reg.root)).is_file():
                    marker = f'<script type="application/json" id="{symbol.lower()}-record">'
                    before, middle = body.decode("utf-8").split(marker, 1)
                    _, after = middle.split("</script>", 1)
                    data = json.dumps(read(local), ensure_ascii=True).replace("<", chr(92) + "u003c")
                    body = (before + marker + data + "</script>" + after).encode("utf-8")
                with closing(pipeline.connect(reg.root)) as db:
                    data = demo_history.presentation(r for r in pipeline.records(db) if not symbol or r['symbol']==symbol)
                    dates = dict(db.execute('SELECT symbol,asof FROM syncs'))
                html = demo_history.embed(body.decode("utf-8"), data, dates)
                body = narratives.embed(html, narratives.collect(reg.root, reg), include_pending=True).encode("utf-8")
            elif method == "GET" and path == "/report":
                number = int(parse_qs(env["QUERY_STRING"])["id"][0])
                result = reg.db.execute("SELECT data FROM reports WHERE id=?",(number,)).fetchone()
                if not result: raise ValueError("レポートが見つかりません")
                body,mime = result[0],"text/csv; charset=utf-8"
                headers.append(("Content-Disposition",f'attachment; filename="nvidia_report_{number}.csv"'))
            elif method == "GET" and path in ("/coverage","/coverage.csv"):
                key=parse_qs(env["QUERY_STRING"]).get("range",[""])[0]
                result=periods.load(reg.root,key)
                if path.endswith('.csv'):
                    if result is None: raise ValueError("対象期間を取得してください")
                    body=reporting.csvdata([periods.HEADER,*periods.rows(result,reg)])
                    mime="text/csv; charset=utf-8"
                    headers.append(("Content-Disposition",f'attachment; filename="nvidia_{result["first"]}_{result["last"]}.csv"'))
                else:
                    body=page("期間カバレッジ",'<a href="/">NVIDIAの開示レビューへ</a>'+periods.render(reg.root,result,reg,hidden))
            elif method == "GET" and path == "/checks.csv":
                body,mime = reporting.reconciliation_csv(reg),"text/csv; charset=utf-8"
                headers.append(("Content-Disposition",'attachment; filename="nvidia_checks.csv"'))
            elif method == "GET" and path == "/financial.csv":
                with reg.db: reporting.observe(reg)
                body = reporting.financial_csv(reg)
                mime = "text/csv; charset=utf-8"
                headers.append(("Content-Disposition",'attachment; filename="nvidia_financial.csv"'))
            elif method == "GET" and path == "/reviewed.csv":
                body,mime = reg.export(),"text/csv; charset=utf-8"
                headers.append(("Content-Disposition",'attachment; filename="nvidia_reviewed.csv"'))
            elif method == "GET" and path == "/filing":
                body = detail(reg,parse_qs(env["QUERY_STRING"])["acc"][0])
            elif method == "GET" and path == "/":
                body = index(reg,parse_qs(env["QUERY_STRING"]).get("needs")==["1"])
            else:
                raise KeyError(path)
        except (KeyError,ValueError,TypeError,OSError,sqlite3.Error) as exc:
            status,body = "400 Bad Request",page("確認が必要です",h(str(exc))+'<p><a href="/">台帳へ戻る</a></p>')
        reply(status,[("Content-Type",mime),*headers])
        return [body]
    return route


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=8774)
    args = parser.parse_args()
    reg = Register(ROOT)
    with make_server("127.0.0.1",args.port,application(reg,args.port)) as server:
        print(f"確認画面  http://localhost:{args.port}/",flush=True)
        server.serve_forever()
