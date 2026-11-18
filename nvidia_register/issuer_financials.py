"""Rebuild the Apple, Amazon, Meta and Tesla quarterly GAAP filing comparisons."""
import argparse
from dataclasses import dataclass
from functools import partial
from html.parser import HTMLParser
from http.client import HTTPException
import json
from pathlib import Path
import re

from earnings import atomic
from filing_pair import FilingPair, METRICS, compare, read_values
from periods import fetcher
from reconcile import Grid, one


BASE = "https://www.sec.gov/Archives/edgar/data/"
PAIRS = {
    "AAPL": FilingPair("AAPL", "FY2026Q3", "June 27, 2026",
        "0000320193-26-000018", "0000320193-26-000020",
        BASE + "320193/000032019326000018/aapl-20260730.htm",
        BASE + "320193/000032019326000020/aapl-20260627.htm"),
    "AMZN": FilingPair("AMZN", "FY2026Q3", "September 30, 2026",
        "0001018724-26-000038", "0001018724-26-000040",
        BASE + "1018724/000101872426000038/amzn-20261029.htm",
        BASE + "1018724/000101872426000040/amzn-20260930.htm"),
    "META": FilingPair("META", "FY2026Q3", "September 30, 2026",
        "0001628280-26-066700", "0001628280-26-066773",
        BASE + "1326801/000162828026066700/meta-20261019.htm",
        BASE + "1326801/000162828026066773/meta-20260930.htm"),
    "TSLA": FilingPair("TSLA", "FY2026Q3", "September 30, 2026",
        "0001628280-26-066738", "0001628280-26-066775",
        BASE + "1318605/000162828026066738/tsla-20261021.htm",
        BASE + "1318605/000162828026066775/tsla-20260930.htm"),
}


@dataclass(frozen=True)
class Statement:
    heading: str
    units: str
    sales: str
    eps: str
    eps_section: str = ""
    shares_section: str = ""


RULES = {
    "AAPL": Statement("Apple Inc. Condensed Consolidated Statements of Operations",
        "In millions, except number of shares, which are reflected in thousands, and per-share amounts",
        "Total net sales", "Diluted", "Earnings per share:", "Shares used in computing earnings per share:"),
    "AMZN": Statement("Amazon.com, Inc. Consolidated Statements of Operations", "in millions, except per share data",
        "Total net sales", "Diluted earnings per share"),
    "META": Statement("Meta Platforms, Inc. Condensed Consolidated Statements of Income", "In millions, except per share amounts",
        "Revenue", "Diluted", "Earnings per share:", "Weighted-average shares used to compute earnings per share:"),
    "TSLA": Statement("Tesla, Inc. Consolidated Statements of Operations", "in millions, except per share data",
        "Total revenues", "Diluted", "Net income per share of common stock attributable to common stockholders",
        "Weighted average shares used in computing net income per share of common stock"),
}
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


def compact(value):
    return "".join(value.lower().split())


def statement_table(raw, symbol):
    rule = RULES[symbol]
    grid = Grid()
    grid.feed(raw.decode("utf-8-sig"))
    grid.close()
    candidates = []
    for outside, table in grid.tables:
        rows = [r for r in table if r]
        # Meta puts its heading inside the exhibit's table and outside the 10-Q table.
        front = compact(" ".join(r[0][2] for r in rows[:4] if len(r) == 1))
        context = compact(outside)[-400:] + front
        tail = context.rpartition(compact(rule.heading))
        if tail[1] and compact("(" + rule.units + ")") in tail[2] and "(unaudited)" in tail[2]:
            candidates.append(rows)
    rows = one(candidates, "Cannot uniquely identify the consolidated statement with its monetary units.")
    end, year = PAIRS[symbol].quarter_end.rsplit(", ", 1)
    group_text = "Three Months Ended" if symbol == "AAPL" else f"Three Months Ended {end},"
    index, group = one(((i, cell) for i, row in enumerate(rows) for cell in row
                        if compact(cell[2]) == compact(group_text)), "Missing or ambiguous three-month header.")
    if index + 1 >= len(rows):
        raise ValueError("Missing column dates.")
    date_label = PAIRS[symbol].quarter_end if symbol == "AAPL" else year
    col = one(((lo, hi) for lo, hi, val in rows[index+1] if group[0] <= lo and hi <= group[1]
               and compact(val) == compact(date_label)), "Cannot identify the quarter within its column group.")
    rows = rows[index+2:]
    if not any('$' in v for row in rows for lo, hi, v in row if col[0] <= lo and hi <= col[1]):
        raise ValueError("Cannot establish dollar units for the quarter.")
    return rows, col


def row_index(rows, label):
    return one((i for i, r in enumerate(rows) if r[0][2] == label), "Missing or ambiguous section: " + label)


def value_from_grid(rows, column, name, symbol):
    rule = RULES[symbol]
    label = rule.sales if name == METRICS[0] else rule.eps
    if name == METRICS[1] and rule.eps_section:
        rows = rows[row_index(rows, rule.eps_section)+1:row_index(rows, rule.shares_section)]
    if name == METRICS[0] and symbol == "AAPL":
        # Stop before expenses; Apple's exhibit repeats totals in two footnote breakdowns.
        rows = rows[row_index(rows, "Net sales:")+1:row_index(rows, "Cost of sales:")]
    row = one((r for r in rows if re.sub(r" \(\d+\)$", "", r[0][2]) == label), "Missing or ambiguous row: " + label)
    amount = "".join(v for lo, hi, v in row if column[0] <= lo and hi <= column[1])
    if not re.fullmatch(r"\$?" + NUMBER, amount):
        raise ValueError("Cannot identify a numeric amount in " + label + ".")
    return amount.lstrip("$").replace(",", "")


class SlideText(HTMLParser):
    """The SEC exhibit includes one text transcript per slide alongside its image."""
    def __init__(self):
        super().__init__()
        self.statements = []

    def handle_data(self, data):
        text = " ".join(data.split())
        if text.startswith("In millions of USD or shares as applicable, except per share data ") and re.search(
                r"STATEMENTOFOPERATIONS\(UNAUDITED\)\d+$", compact(text).upper()):
            self.statements.append(text)


def tesla_slide(raw):
    p = SlideText()
    p.feed(raw.decode("utf-8-sig"))
    p.close()
    text = one(p.statements, "Cannot uniquely identify the Tesla statement-of-operations slide.")
    header = text.split("except per share data ", 1)[1].partition(" REVENUES ")[0].split()
    if len(set(header)) != len(header) or not all(re.fullmatch(r"Q[1-4]-20\d\d", v) for v in header):
        raise ValueError("Unexpected Tesla quarterly header.")
    col = one((i for i,v in enumerate(header) if v == "Q3-2026"), "Cannot identify Q3-2026.")
    return text, (col, len(header))


def value_from_slide(text, position, name):
    index, width = position
    if name == METRICS[0]:
        pattern = r"\bTotal revenues (.*?) COST OF REVENUES\b"
    else:
        pattern = (r"Net income per share of common stock attributable to common stockholders Basic [^A-Za-z]+"
                   r"Diluted (.*?) Weighted average shares used in computing net income per share")
    row = one(re.findall(pattern, text), "Missing or ambiguous Tesla " + name + " row.")
    token = (r"\$\s*" if name == METRICS[1] else "") + NUMBER
    if not re.fullmatch(token + (r"(?:\s+" + token + ")") + "{" + str(width-1) + "}", row):
        raise ValueError("Tesla amount count does not match the quarterly columns.")
    return re.findall(token, row)[index].replace("$", "").replace(",", "").strip()


def reconcile_company(symbol, get):
    rule = RULES[symbol]
    grid = partial(read_values, statement=partial(statement_table, symbol=symbol),
                   metric_value=partial(value_from_grid, symbol=symbol))
    exhibit = partial(read_values, statement=tesla_slide, metric_value=value_from_slide) if symbol == "TSLA" else grid
    eps = rule.eps_section.rstrip(":") + " / " + rule.eps if rule.eps_section else rule.eps
    return compare(PAIRS[symbol], get, exhibit, (rule.sales, eps), quarterly_extract=grid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contact", required=True)
    parser.add_argument("--company", choices=PAIRS, nargs="+", default=list(PAIRS))
    args = parser.parse_args()
    get = fetcher(args.contact)
    root = Path(__file__).resolve().parents[1]
    failed = False
    for symbol in dict.fromkeys(args.company):
        try:
            result = reconcile_company(symbol, get)
            atomic(PAIRS[symbol].local_result(root), json.dumps(result).encode())
            statuses = {f["metric"]: f["status"] for f in result["facts"]}
            print(symbol, json.dumps(statuses, ensure_ascii=False))
            failed |= any(status != "一致" for status in statuses.values())
        except (OSError, ValueError, HTTPException) as exc:
            print(f"{symbol}: update failed: {exc}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
