"""Build a source-linked pending review of one newly archived NVIDIA filing."""
import argparse
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import llm_runner
import read_sections
from nvidia_sync import locked

ROOT = Path(__file__).resolve().parent


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def process(root, accession):
    inbox = root / "nvidia_inbox"
    record = load(inbox / accession / "record.json")
    if record["ticker"] != "NVDA" or record["accession_number"] != accession:
        raise ValueError("保存記録と提出番号が一致しません")
    inspected = read_sections.inspect_source(record, [inbox])
    if inspected["status"] in {"source_missing", "source_changed", "source_conflict"}:
        raise ValueError(inspected["status"])
    config = load(root / "llm/ollama-qwen38-27b.json")
    if urlsplit(config["base_url"]).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("ローカルのOllama接続先を指定してください")
    directory = root / "inbox_reviews" / accession / record["sha256"]
    parts = [f"<h1>NVIDIA 新着開示</h1><p>{escape(accession)} ／ 提出日 {escape(record['filing_date'])} ／ 未確認</p>",
             f'<p><a href="{escape(record["document_url"], quote=True)}">SEC提出原文</a></p>']
    parts.extend(f"<p>{escape(w)}</p>" for w in inspected["warnings"])
    for section in inspected["sections"]:
        sid = section["section_id"]
        parts.append(f"<h2>Item {escape(section['item'])}　{escape(section['label'])}</h2>")
        if section["item"] == "9.01":
            parts.append("<p>添付資料の一覧です。添付ファイルの本文は今回の処理範囲外です。</p>")
        elif not section["heading_recognized"]:
            parts.append("<p>見出しの判定に原文確認が必要です。</p>")
        else:
            ctx = dict(accession_number=accession, filing_date=record["filing_date"],
                       document_url=record["document_url"], source_sha256=record["sha256"],
                       status="sections_ready", section=section)
            folder = directory / sid
            folder.mkdir(exist_ok=True, parents=True)
            completed = next((p for p in sorted(folder.glob("*/run.json"), reverse=True)
                              if load(p)["status"] == "completed_pending_review"), None)
            if completed:
                run = completed.parent
                status = "completed_pending_review"
            else:
                run = folder / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                _, log = llm_runner.run(config, {accession: ctx}, run, execute=True)
                status = log["status"]
            if status == "completed_pending_review":
                filing = load(run / "candidate.json")["filings"][0]
                parts.append(f"<p>抽出候補 {len(filing['events'])}件 ／ {escape(filing['extraction_status'])}</p>")
                for event in filing["events"]:
                    parts.append("<h3>" + escape(event["summary_ja"]["value"]) + "</h3>")
                    for field, fact in fact_rows(event):
                        links = " ".join(f'<a href="#{sid}-{n}">段落{n}</a>' for n in fact["block_numbers"])
                        parts.append(f"<p>{escape(field)}　{escape(str(fact['value']))}　{links}</p>")
                parts.extend(f"<p>{escape(note)}</p>" for note in filing["missing_information"])
            else:
                parts.append(f"<p>抽出要確認　{escape(status)}　詳細ログ {escape(str(run))}</p>")
        parts.append("<h3>根拠本文</h3>")
        for n, block in enumerate(section["blocks"], section["block_start"]):
            parts.append(f'<p id="{sid}-{n}"><b>{n}</b>　{escape(block)}</p>')
    for block in inspected.get("unsegmented_blocks", []):
        parts.append(f"<p>{escape(block)}</p>")
    page = directory / "review.html"
    directory.mkdir(exist_ok=True, parents=True)
    tmp = page.with_suffix(".tmp")
    tmp.write_text('<!doctype html><html lang="ja"><meta charset="utf-8"><title>NVIDIA 新着確認</title>'
                   '<style>body{max-width:56rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui}'
                   'h2{border-top:1px solid #aaa;padding-top:1rem}:target{background:#fff3be}</style>'
                   + "\n".join(parts) + "</html>", encoding="utf-8")
    tmp.replace(page)
    return page


def fact_rows(event):
    for key, label in (("subject","対象"), ("organization","組織"), ("event_type","分類"),
                       ("status_as_of_filing","提出時点の状態"), ("summary_ja","要約")):
        yield label, event[key]
    for day in event["dates"]:
        yield day["role"] + " ／ " + day["certainty"], day
    for cond in event["conditions"]:
        yield "条件", cond
    if event["notes_ja"]:
        yield "補足", event["notes_ja"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("accession")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", args.accession):
        parser.error("提出番号を指定してください")
    with locked(ROOT / "inbox_reviews" / args.accession):
        print(process(ROOT, args.accession))
