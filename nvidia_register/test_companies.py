import csv
from datetime import date, timedelta
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from app import application
import companies


END = date(2026, 11, 18)
ACCESSION = "0001193125-26-000001"  # A filing agent's prefix may differ from the issuer CIK.
ARCHIVE = "CIK0000320193-submissions-001.json"


def page(entries):
    names = ("form", "filingDate", "accessionNumber", "primaryDocument", "items")
    return {name: [entry[i] for entry in entries] for i, name in enumerate(names)}


def entry(day=END, form="8-K", accession=ACCESSION):
    return form, day.isoformat(), accession, "earnings.htm", "2.02,9.01"


def feed(cik, entries, archives=()):
    return {"cik": cik, "filings": {"recent": page(entries), "files": list(archives)}}


def route(reg, path, query="", form=None):
    captured = {}
    def start(status, headers):
        captured.update(status=status, headers=dict(headers))
    body = urlencode(form or {}).encode()
    env = {"PATH_INFO": path, "QUERY_STRING": query, "HTTP_HOST": "localhost:8774",
           "REQUEST_METHOD": "POST" if form is not None else "GET", "CONTENT_LENGTH": str(len(body)),
           "wsgi.input": io.BytesIO(body)}
    result = b"".join(application(reg, 8774)(env, start))
    return captured["status"], captured["headers"], result


class CompaniesTest(unittest.TestCase):
    def test_window_amendments_and_archive_url(self):
        start = END - timedelta(days=89)
        entries = [entry(day=start, form="8-K/A"), entry(day=start-timedelta(days=1)),
                   entry(day=END+timedelta(days=1)), entry(form="10-Q")]
        state = companies.collect("AAPL", END, lambda _: json.dumps(feed(320193, entries)))
        row, = state["rows"]
        self.assertEqual(row["form"], "8-K/A")
        self.assertEqual(row["api_items"], ["2.02", "9.01"])
        self.assertEqual(row["document_url"],
            "https://www.sec.gov/Archives/edgar/data/320193/000119312526000001/earnings.htm")

    def test_overlapping_archive_and_recent_are_deduplicated(self):
        archives = [{"name": ARCHIVE, "filingFrom": "2026-01-01", "filingTo": END.isoformat()}]
        data = feed(320193, [entry()], archives)
        def get(url):
            if url.endswith(ARCHIVE):
                return json.dumps(page([entry(), entry(accession="0001193125-26-000002")]))
            return json.dumps(data)
        self.assertEqual(len(companies.collect("AAPL", END, get)["rows"]), 2)

    def test_invalid_cik_and_partial_columns_fail(self):
        with self.assertRaisesRegex(ValueError, "CIK"):
            companies.collect("AAPL", END, lambda _: json.dumps(feed(123, [])))
        data = feed(320193, [entry()])
        data["filings"]["recent"]["items"].clear()
        with self.assertRaises(ValueError):
            companies.collect("AAPL", END, lambda _: json.dumps(data))

    def test_refresh_failure_preserves_one_issuer_and_can_recover(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = companies.load(root)
            def get(url):
                cik = int(url.split("CIK")[1].split(".")[0])
                if cik == 320193:
                    raise OSError("unavailable")
                return json.dumps(feed(cik, [entry()]))
            for _ in range(2):
                companies.refresh(root, get, END)
            actual = companies.load(root)
            self.assertEqual(actual["AAPL"]["rows"], initial["AAPL"]["rows"])
            self.assertEqual(actual["AAPL"]["error"], "unavailable")
            self.assertEqual(actual["TSLA"]["rows"][0]["ticker"], "TSLA")
            self.assertEqual(len(companies.matches(actual, "TSLA", "")), 1)
            self.assertEqual(len(companies.matches(actual, "", "2.02")), 6 + len(companies.matches(initial, "AAPL", "2.02")))
            companies.refresh(root, lambda url: json.dumps(feed(int(url.split("CIK")[1].split(".")[0]), [])), END)
            self.assertEqual(companies.load(root)["AAPL"], {"as_of": END.isoformat(), "rows": []})

    def test_web_filters_csv_and_update(self):
        with TemporaryDirectory() as tmp:
            reg = SimpleNamespace(root=Path(tmp), token="token")
            filters = "ticker=GOOGL&item=2.02"
            status, _, body = route(reg, "/companies", filters)
            self.assertEqual(status, "200 OK")
            snapshot = companies.load(reg.root)
            expected = companies.matches(snapshot, "GOOGL", "2.02")
            self.assertGreater(len(expected), 0)
            for row in expected:
                self.assertIn(row["document_url"].encode(), body)
            _, _, raw = route(reg, "/companies.csv", filters)
            csv_rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
            self.assertEqual(csv_rows, list(companies.csv_rows(snapshot, expected)))
            self.assertEqual(route(reg, "/companies", "ticker=unknown")[0], "400 Bad Request")
            with patch("companies.refresh") as refresh:
                self.assertEqual(route(reg, "/companies-fetch", form={"token":"invalid"})[0], "400 Bad Request")
                refresh.assert_not_called()
                status, headers, _ = route(reg, "/companies-fetch", form={"token":"token", "contact":"tester@example.org"})
                self.assertEqual((status, headers["Location"]), ("303 See Other", "/companies"))
                refresh.assert_called_once()
