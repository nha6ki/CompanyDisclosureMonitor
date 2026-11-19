"""Rebuild Alphabet's Q3 2026 consolidated GAAP comparison from SEC filings."""
import re

import filing_pair
from reconcile import Grid, one


PRIMARY = "https://www.sec.gov/Archives/edgar/data/1652044/000165204426000080/goog-20261028.htm"
QUARTERLY = "https://www.sec.gov/Archives/edgar/data/1652044/000165204426000084/goog-20260930.htm"
PAIR = filing_pair.FilingPair("GOOGL", "FY2026Q3", "September 30, 2026",
                             "0001652044-26-000080", "0001652044-26-000084", PRIMARY, QUARTERLY)
LABELS = ("Revenues", "Diluted net income per common share")


def statement(raw, end="September 30", year="2026"):
    grid = Grid()
    grid.feed(raw.decode("utf-8-sig"))
    grid.close()
    heading = re.compile(r"\bAlphabet Inc\. Consolidated Statements of Income "
                         r"\(in millions, except per share amounts(?:[,;] unaudited)?\)$", re.I)
    rows = one(([row for row in rows if row] for title, rows in grid.tables if heading.search(title)),
               "Cannot identify Alphabet's consolidated income statement with per-share units.")
    if not rows:
        raise ValueError("Empty consolidated income statement.")
    quarter = one((cell for cell in rows[0] if cell[2] in (f"Quarter Ended {end},", "Three Months Ended", f"Three Months Ended {end},")),
                  "Cannot identify the three-month column group.")
    day_label = end + ","
    start, right_edge, title = quarter
    year_index = 1
    if title == "Three Months Ended":
        if len(rows) < 2 or (start, right_edge, day_label) not in rows[1]:
            raise ValueError("The three-month period has a different ending date.")
        year_index += 1
    if len(rows) <= year_index:
        raise ValueError("Missing year headers.")
    current = one(((left, right) for left, right, value in rows[year_index]
                   if start <= left and right <= right_edge and value == year),
                  "Cannot identify the requested year within the three-month column group.")
    return rows[year_index + 1:], current


def metric_value(rows, column, name):
    label = LABELS[filing_pair.METRICS.index(name)]
    labels = {label} if name == filing_pair.METRICS[0] else {label, "Diluted net income per share"}
    row = one((row for row in rows if re.sub(r" \(Note \d+\)$", "", row[0][2]) in labels),
              "Missing or ambiguous consolidated row: " + label)
    cells = [text for left, right, text in row if column[0] <= left and right <= column[1]]
    amount = "".join(cells)
    if not re.fullmatch(r"\$(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", amount):
        raise ValueError("Cannot verify the dollar amount for " + label + ".")
    return amount[1:].replace(",", "")


def read_values(raw):
    return filing_pair.read_values(raw, statement, metric_value)


def compare(get):
    return filing_pair.compare(PAIR, get, read_values, LABELS)


if __name__ == "__main__":
    raise SystemExit(filing_pair.run_cli(PAIR, compare, __doc__))
