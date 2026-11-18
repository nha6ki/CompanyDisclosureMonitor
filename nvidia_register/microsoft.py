"""Rebuild Microsoft's FY2027 Q1 comparison from its SEC 8-K exhibit and 10-Q."""
import re

import filing_pair
from filing_pair import METRICS
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
PAIR = filing_pair.FilingPair("MSFT", PERIOD, f"{END}, {YEAR}", ACCESSION, Q_ACCESSION, PRIMARY, QUARTERLY)
LABELS = ("Total revenue", "Earnings per share / Diluted")
TABLE_UNIT = "(inmillions,exceptpershareamounts)"


def exhibit_link(raw):
    return filing_pair.exhibit_link(raw, PRIMARY)


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
    return filing_pair.read_values(raw, statement, metric_value)


def compare(get):
    return filing_pair.compare(PAIR, get, read_values, LABELS)


local_result = PAIR.local_result


if __name__ == "__main__":
    raise SystemExit(filing_pair.run_cli(PAIR, compare, __doc__))
