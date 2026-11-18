"""Incrementally archive new NVIDIA 8-K primary documents from WSL."""
import argparse
import fcntl
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
FEED = "https://data.sec.gov/submissions/CIK0001045810.json"


def select(feed, start):
    if int(feed["cik"]) != 1045810:
        raise ValueError("Expected NVIDIA submissions")
    recent = feed["filings"]["recent"]
    fields = ("form", "filingDate", "accessionNumber", "primaryDocument", "items")
    for form, day, accession, filename, items in zip(*(recent[k] for k in fields), strict=True):
        if form != "8-K" or date.fromisoformat(day) < start:
            continue
        if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
            raise ValueError("Invalid accession number")
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.html?", filename, re.I):
            raise ValueError(f"Unsupported primary document: {filename}")
        url = f"https://www.sec.gov/Archives/edgar/data/1045810/{accession.replace('-', '')}/{filename}"
        yield dict(ticker="NVDA", accession_number=accession, filing_date=day,
                   document_url=url, api_items=[s.strip() for s in items.split(",") if s.strip()])


@contextmanager
def locked(directory):
    directory.mkdir(exist_ok=True, parents=True)
    with (directory / "sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def sync(root, get):
    baseline = json.loads((root / "section_manifest.json").read_text())["filings"]
    known = {r["accession_number"] for r in baseline}
    start = max(date.fromisoformat(r["filing_date"]) for r in baseline)
    archive = root / "nvidia_inbox"
    added = []
    with locked(archive):
        rows = select(json.loads(get(FEED)), start)
        for row in rows:
            key = row["accession_number"]
            target = archive / key
            if key in known or (target / "record.json").is_file():
                continue
            body = get(row["document_url"])
            if not re.search(rb"<html\b", body, re.I):
                raise ValueError(f"{key}: expected HTML")
            if re.search(rb"Your Request Originates from an Undeclared Automated Tool|Request Rate Threshold Exceeded", body, re.I):
                raise ValueError(f"{key}: SEC returned an access notice")
            row["sha256"] = hashlib.sha256(body).hexdigest()
            stage = Path(tempfile.mkdtemp(dir=archive, prefix=".pending-"))
            try:
                (stage / "primary.html").write_bytes(body)
                (stage / "record.json").write_text(json.dumps(row, indent=2) + "\n")
                stage.rename(target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            added.append(key)
    return dict(start_date=start.isoformat(), added=added, count=len(added))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contact", required=True, help="Email for the SEC User-Agent")
    args = parser.parse_args()

    def fetch(url):
        time.sleep(0.25)
        request = Request(url, headers={"User-Agent": f"CompanyDisclosureMonitor/1.0 {args.contact}"})
        with urlopen(request, timeout=45) as response:
            return response.read()

    try:
        print(json.dumps(sync(ROOT, fetch)))
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"Update incomplete: {exc}. Rerun to resume.")


if __name__ == "__main__":
    main()
