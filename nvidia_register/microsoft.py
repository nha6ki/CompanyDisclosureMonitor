"""Rebuild Microsoft's FY2027 Q1 comparison from its SEC 8-K exhibit and 10-Q."""
import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urljoin

import earnings
from periods import fetcher
from reconcile import Grid, one


# A reviewed filing pair. The period is also checked independently in both tables.
PERIOD = "FY2027Q1"
END = "September 30"
YEAR = "2026"
ACCESSION = "0001193125-26-436122"
Q_ACCESSION = "0001193125-26-436133"
SEC = "https://www.sec.gov/Archives/edgar/data/789019/"
PRIMARY = SEC + ACCESSION.replace("-", "") + "/msft-20261028.htm"
QUARTERLY = SEC + Q_ACCESSION.replace("-", "") + "/msft-20260930.htm"
METRICS = ("Revenue", "Diluted earnings per share")
LABELS = ("Total revenue", "Earnings per share / Diluted")
UNITS = ("USD millions", "USD/share")
TABLE_UNIT = "(inmillions,exceptpershareamounts)"


def exhibit_link(raw):
    doc = earnings.parse(raw)
    filename = one({url for url, t, r in doc.row_links
                    if doc.tables[t][r][:1] == ["99.1"] and re.fullmatch(r"[A-Za-z0-9_-]+\.html?", url, re.I)},
                   "Cannot identify one Exhibit 99.1 in the 8-K.")
    return urljoin(PRIMARY, filename)


def compact(text):
    return "".join(text.lower().split())


def statement(raw):
    grid = Grid()
    grid.feed(raw.decode("utf-8-sig"))
    grid.close()
    candidates = []
    for context, rows in grid.tables:
        heading = compact(context)
        # The release places the unit outside the table; the 10-Q uses its first row.
        if not re.search(r"(?<!comprehensive)incomestatements(?:\(inmillions,exceptpershareamounts\)\(unaudited\))?$", heading):
            continue
        rows = [r for r in rows if r]
        if not rows or TABLE_UNIT not in heading[-90:] + compact(rows[0][0][2]):
            continue
        candidates.append(rows)
    rows = one(candidates, "Cannot identify the consolidated income statement and per-share units.")
    day_label = compact("Three Months Ended " + END + ",")
    header_index = one((i for i, row in enumerate(rows) if any(compact(c[2]) == day_label for c in row)),
                       "The three-month ending date is missing or ambiguous.")
    # In the exhibit the years are on the next row, under a shared date header.
    date_row = rows[header_index]
    year_row = date_row
    if not any(c[2] == YEAR for c in date_row):
        if header_index + 1 == len(rows):
            raise ValueError("Missing year columns.")
        year_row = rows[header_index+1]
    years = [cell for cell in year_row if re.fullmatch(r"[0-9]{4}", cell[2])]
    if sorted(c[2] for c in years) != [str(int(YEAR)-1), YEAR]:
        raise ValueError("Cannot identify the current-year quarter independently of the prior year.")
    column = next((left, right) for left, right, value in years if value == YEAR)
    date_cell = next(c for c in date_row if compact(c[2]) == day_label)
    if year_row is not date_row and not (date_cell[0] <= column[0] and column[1] <= date_cell[1]):
        raise ValueError("The year is outside the three-month column group.")
    if not any("$" in value for row in rows for left, right, value in row
               if column[0] <= left and right <= column[1]):
        raise ValueError("Cannot establish dollar units in the selected column.")
    return rows, column


def metric_value(rows, column, name):
    if name == METRICS[0]:
        label = "Total revenue"
    else:
        first = one((i for i, r in enumerate(rows) if r[0][2] == "Earnings per share:"), "Missing EPS section.")
        last = one((i for i, r in enumerate(rows) if r[0][2] == "Weighted average shares outstanding:"),
                   "Missing end of EPS section.")
        rows = rows[first+1:last]
        label = "Diluted"
    row = one((r for r in rows if r[0][2] == label), "Missing or ambiguous row: " + label)
    cells = [v for left, right, v in row if column[0] <= left and right <= column[1]]
    text = "".join(cells).replace("$", "").strip()
    if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
        raise ValueError("Unrecognized amount in " + label + ".")
    return text.replace(",", "")


def read_values(raw):
    try:
        rows, column = statement(raw)
    except ValueError as exc:
        return [(None, str(exc)) for _ in METRICS]
    result = []
    for name in METRICS:
        try:
            result.append((metric_value(rows, column, name), ""))
        except ValueError as exc:
            result.append((None, str(exc)))
    return result


def compare(get):
    primary = get(PRIMARY)
    url = exhibit_link(primary)
    release = get(url)
    quarterly = get(QUARTERLY)
    facts = []
    for i, (a, b) in enumerate(zip(read_values(release), read_values(quarterly), strict=True)):
        value, e1 = a
        detail, e2 = b
        errors = [f"{source}: {msg}" for source, msg in (("EX-99.1", e1), ("10-Q", e2)) if msg]
        status = "要確認" if errors else ("一致" if Decimal(value) == Decimal(detail) else "不一致")
        facts.append(dict(metric=METRICS[i], value=value, detail=detail, unit=UNITS[i], detailUnit=UNITS[i],
                          summaryRow=LABELS[i], row=LABELS[i], column=f"Three months ended {END}, {YEAR}",
                          status=status, reason=" ".join(errors)))
    return dict(period=PERIOD, accession=ACCESSION, filing=PRIMARY, url=url, sha=sha256(release).hexdigest(),
                quarterly=QUARTERLY, quarterlyAccession=Q_ACCESSION, quarterlySha=sha256(quarterly).hexdigest(),
                facts=facts)


def local_result(root):
    return root / "register_data" / "m7_financials" / (PERIOD + "-MSFT.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contact", required=True)
    args = parser.parse_args()
    result = compare(fetcher(args.contact))
    earnings.atomic(local_result(Path(__file__).resolve().parents[1]), json.dumps(result).encode())
    print(json.dumps({f["metric"]: f["status"] for f in result["facts"]}, ensure_ascii=False))
    raise SystemExit(int(any(f["status"] != "一致" for f in result["facts"])))
