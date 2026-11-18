"""Magnificent Seven SEC filing index for a rolling 90-calendar-day window."""
from datetime import date, datetime, timedelta
from html import escape
from http.client import HTTPException
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from nvidia_sync import filing_rows, locked
from earnings import atomic


# Symbols are display identifiers. Alphabet's share classes have one SEC issuer.
ISSUERS = {
    "AAPL": ("Apple", 320193),
    "MSFT": ("Microsoft", 789019),
    "GOOGL": ("Alphabet", 1652044),
    "AMZN": ("Amazon", 1018724),
    "NVDA": ("NVIDIA", 1045810),
    "META": ("Meta Platforms", 1326801),
    "TSLA": ("Tesla", 1318605),
}
SEED = Path(__file__).resolve().parent / "examples" / "m7_filings.json"
FORMS = {"8-K", "8-K/A"}
CSV_HEADER = ["企業", "ティッカー", "CIK", "提出日", "書類", "SEC Item", "提出番号", "SEC原文", "取得基準日"]


def window_start(end):
    return end - timedelta(days=89)


def collect(ticker, end, get):
    _, cik = ISSUERS[ticker]
    prefix = f"CIK{cik:010d}"
    base = "https://data.sec.gov/submissions/"
    feed = json.loads(get(base + prefix + ".json"))
    if int(feed["cik"]) != cik:
        raise ValueError(f"{ticker}: SEC issuer CIK mismatch")
    start = window_start(end)
    filings = feed["filings"]
    pages = [filings["recent"]]
    for archive in filings["files"]:
        if date.fromisoformat(archive["filingTo"]) < start or date.fromisoformat(archive["filingFrom"]) > end:
            continue
        filename = archive["name"]
        if not re.fullmatch(prefix + r"-submissions-[0-9]+\.json", filename):
            raise ValueError(f"{ticker}: invalid archive name")
        pages.append(json.loads(get(base + filename)))
    by_accession = {}
    for page in pages:
        for form, row in filing_rows(page, start, cik, ticker, FORMS, end):
            accession = row["accession_number"]
            row["form"] = form
            if accession in by_accession and by_accession[accession] != row:
                raise ValueError(f"{ticker}: conflicting SEC records for {accession}")
            by_accession[accession] = row
    rows = sorted(by_accession.values(), key=lambda r: (r["filing_date"], r["accession_number"]), reverse=True)
    return {"as_of": end.isoformat(), "rows": rows}


def load(root):
    data = json.loads(SEED.read_text(encoding="utf-8"))
    for ticker in ISSUERS:
        path = root / "register_data" / "m7" / f"{ticker}.json"
        if path.is_file():
            data[ticker] = json.loads(path.read_text(encoding="utf-8"))
    return data


def refresh(root, get, end=None):
    end = end or datetime.now(ZoneInfo("America/New_York")).date()
    folder = root / "register_data" / "m7"
    with locked(folder):
        data = load(root)
        for ticker in ISSUERS:
            try:
                result = collect(ticker, end, get)
            except (OSError, HTTPException, ValueError, KeyError, TypeError) as exc:
                result = dict(data[ticker], error=str(exc), attempted=end.isoformat())
            atomic(folder / f"{ticker}.json", json.dumps(result, ensure_ascii=False).encode())


def filters(query):
    ticker = query.get("ticker", [""])[0]
    item = query.get("item", [""])[0]
    if (ticker and ticker not in ISSUERS) or item not in ("", "2.02"):
        raise ValueError("企業または開示項目を選び直してください")
    return ticker, item


def matches(data, ticker, item):
    rows = [r for symbol, state in data.items() if not ticker or symbol == ticker
            for r in state["rows"] if not item or item in r["api_items"]]
    return sorted(rows, key=lambda r: (r["filing_date"], r["ticker"], r["accession_number"]), reverse=True)


def csv_rows(data, rows):
    yield CSV_HEADER
    for row in rows:
        symbol = row["ticker"]
        name, cik = ISSUERS[symbol]
        yield [name, symbol, f"{cik:010d}", row["filing_date"], row["form"],
               ", ".join(row["api_items"]), row["accession_number"], row["document_url"], data[symbol]["as_of"]]


def render(data, rows, ticker, item, token):
    options = ['<option value="">全7社</option>']
    errors = []
    for symbol, (name, _) in ISSUERS.items():
        selected = " selected" if ticker == symbol else ""
        options.append(f'<option value="{symbol}"{selected}>{name} ({symbol})</option>')
        state = data[symbol]
        if "error" in state:
            errors.append(f'<li>{name}（{escape(state["attempted"])} 更新失敗）'
                          f'：{escape(state["error"])}。表示中の取得基準日：{escape(state["as_of"])}。</li>')
    selected = " selected" if item else ""
    dates = {state["as_of"] for symbol, state in data.items() if not ticker or ticker == symbol}
    if len(dates) == 1:
        end = date.fromisoformat(next(iter(dates)))
        period = f'対象期間：{window_start(end)} ～ {end}（90日間、米国東部の日付）'
    else:
        period = "各社の取得基準日までの90日間。基準日は各行に表示しています。"
    tr = []
    for r in rows:
        symbol = r["ticker"]
        tr.append("<tr>" + "".join(f"<td>{escape(v)}</td>" for v in (
            r["filing_date"], ISSUERS[symbol][0], r["form"], ", ".join(r["api_items"]) or "—"))
            + f'<td><a href="{escape(r["document_url"])}">{escape(r["accession_number"])}</a></td>'
            + f'<td>{escape(data[symbol]["as_of"])}</td></tr>')
    if not rows:
        tr.append('<tr><td colspan="6">この条件に該当する提出書類は0件です。</td></tr>')
    notice = '<section role="alert"><h2>取得状況</h2><ul>' + "".join(errors) + "</ul></section>" if errors else ""
    detail_links = " ".join(f'<a href="/company/{symbol}?lang=ja">{name}の四半期決算</a>'
                            for symbol, (name, _) in ISSUERS.items())
    return f"""
<p>{period}</p><p>対象書類は Form 8-K・8-K/A です。提出番号からSEC原文を確認できます。</p>
{notice}
<nav class="actions" aria-label="企業の決算詳細">{detail_links}</nav>
<form method="get" action="/companies"><label>企業 <select name="ticker">{''.join(options)}</select></label>
<label>開示項目 <select name="item"><option value="">すべて</option>
<option value="2.02"{selected}>Item 2.02（業績・財務状況）</option></select></label>
<button>絞り込む</button></form>
<p><strong>{len(rows)} 件</strong>　<a href="/companies.csv?ticker={ticker}&amp;item={item}">表示中の一覧をCSVで保存</a></p>
<table><tr><th>提出日</th><th>企業</th><th>書類</th><th>SEC Item</th><th>提出番号・原文</th><th>取得基準日</th></tr>
{''.join(tr)}</table>
<details><summary>SECから全7社を更新</summary>
<form method="post" action="/companies-fetch">{token}
<label>SECへの連絡先メール <input type="email" name="contact" required></label>
<button>7社の開示一覧を取得</button></form></details>
<p><a href="/">NVIDIAのレビュー台帳</a>　<a href="/coverage">NVIDIAの四半期指標を照合</a></p>"""
