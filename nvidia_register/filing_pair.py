"""Shared reconciliation of a pinned SEC earnings release and quarterly report."""
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urljoin

import earnings
from periods import fetcher
from reconcile import one


METRICS = ("Revenue", "Diluted earnings per share")
UNITS = ("USD millions", "USD/share")


@dataclass(frozen=True)
class FilingPair:
    ticker: str
    period: str
    quarter_end: str
    accession: str
    quarterly_accession: str
    primary: str
    quarterly: str

    def local_result(self, root):
        return root / "register_data" / "m7_financials" / f"{self.period}-{self.ticker}.json"


def exhibit_link(raw, primary):
    doc = earnings.parse(raw)
    filename = one({url for url, t, r in doc.row_links
                    if doc.tables[t][r][:1] == ["99.1"] and re.fullmatch(r"[A-Za-z0-9_-]+\.html?", url, re.I)},
                   "Cannot identify one Exhibit 99.1 in the 8-K.")
    return urljoin(primary, filename)


def read_values(raw, statement, metric_value):
    try:
        rows, column = statement(raw)
    except ValueError as exc:
        return [(None, str(exc)) for _ in METRICS]
    values = []
    for name in METRICS:
        try:
            values.append((metric_value(rows, column, name), ""))
        except ValueError as exc:
            values.append((None, str(exc)))
    return values


def compare(pair, get, extract, labels):
    url = exhibit_link(get(pair.primary), pair.primary)
    release = get(url)
    quarterly = get(pair.quarterly)
    facts = []
    for i, (a, b) in enumerate(zip(extract(release), extract(quarterly), strict=True)):
        value, e1 = a
        detail, e2 = b
        errors = [f"{source}: {msg}" for source, msg in (("EX-99.1", e1), ("10-Q", e2)) if msg]
        status = "要確認" if errors else ("一致" if Decimal(value) == Decimal(detail) else "不一致")
        facts.append(dict(metric=METRICS[i], value=value, detail=detail, unit=UNITS[i], detailUnit=UNITS[i],
                          summaryRow=labels[i], row=labels[i], column="Three months ended " + pair.quarter_end,
                          status=status, reason=" ".join(errors)))
    return dict(period=pair.period, accession=pair.accession, filing=pair.primary, url=url,
                sha=sha256(release).hexdigest(), quarterly=pair.quarterly, quarterlyAccession=pair.quarterly_accession,
                quarterlySha=sha256(quarterly).hexdigest(), facts=facts)


def run_cli(pair, compare_documents, description):
    import argparse
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--contact", required=True)
    args = parser.parse_args()
    result = compare_documents(fetcher(args.contact))
    earnings.atomic(pair.local_result(Path(__file__).resolve().parents[1]), json.dumps(result).encode())
    print(json.dumps({f["metric"]: f["status"] for f in result["facts"]}, ensure_ascii=False))
    return int(any(f["status"] != "一致" for f in result["facts"]))
