# Company Disclosure Monitor

A local SEC filing register with a self-contained English/Japanese demo. The NVIDIA baseline traces quarterly GAAP revenue and diluted EPS from Form 8-K exhibits to their source tables. The full workflow also supports filing collection, locally generated event candidates, human review and CSV delivery.

## Magnificent Seven filing index

Open <http://localhost:8774/companies> after starting the local application. The index opens with a bundled snapshot of 24 filings from August 21 through November 18, 2026 (inclusive). Filter Apple, Microsoft, Alphabet, Amazon, NVIDIA, Meta Platforms, or Tesla by issuer, narrow to SEC Item 2.02, follow an accession to the SEC primary document, or export the filtered rows as CSV.

Use the update form with your SEC contact email to fetch each issuer's submissions over the latest 90 calendar days (US Eastern time). The index covers Form 8-K and 8-K/A, keyed by issuer CIK and accession. An update replaces the window for each successful issuer. A failed issuer keeps its last successful data, with an error and the date shown. The stored data consists of filing metadata and source links. The company detail links open the financial comparisons for all seven issuers. Narrative review is available for NVIDIA.

## View the demo

Open [the interactive filing register](nvidia_register/public_demo/index.html) in a browser after cloning. Choose a metric and quarter to compare reported figures, inspect the SEC source and download the fixed dataset. The NVIDIA eight-quarter snapshot covers FY2025 Q4 through FY2027 Q3. Fifteen automated comparisons match exactly and one matches at reported precision. Human review is pending for all 16 figures. The sample CSV files are in [examples](nvidia_register/examples/).

All seven issuers use the same bilingual page; open a company from the filing index or use the company links in the offline HTML. Microsoft FY2027 Q1 covers the three months ended September 30, 2026. Python reads total revenue and diluted earnings per share independently from Exhibit 99.1 and the 10-Q. Each result retains the filing links, comparison column and source units. The CSV also includes both accession numbers and both source hashes.

To repeat the Microsoft check against the pinned source documents, run `python nvidia_register/microsoft.py --contact YOUR_EMAIL` in the application environment. Reload the company page to see the result. A different amount is flagged as a mismatch; unreadable or ambiguous values are marked for review. The embedded snapshot works offline. Updating the seven-company filing index refreshes filing metadata; the Microsoft comparison currently covers this one quarter.

Alphabet FY2026 Q3 covers the three months ended September 30, 2026. The 8-K and 10-Q were filed one day apart. The parser selects the current year inside the three-month header and uses the consolidated statement's diluted EPS row. Run `python nvidia_register/alphabet.py --contact YOUR_EMAIL` to repeat this filing-pair comparison and update the Alphabet page. The offline preview includes the verified quarter.

Apple, Amazon, Meta Platforms and Tesla each include one verified filing pair. Apple FY2026 Q3 ended June 27. Its subsequent fourth quarter has an annual 10-K. Amazon, Meta and Tesla Q3 2026 ended September 30. The parsers use each issuer’s consolidated income statement and identify the quarter independently of the nine-month columns. Tesla’s exhibit uses the text of the financial-statement slide (page 27) and its five quarter headers. The dated snapshots can be reproduced with one command:

```sh
python nvidia_register/issuer_financials.py --contact YOUR_EMAIL
```

Add `--company AAPL`, `AMZN`, `META`, or `TSLA` to limit the check to selected issuers. Reload the local company page to see its result. Source links in the offline demo require an internet connection.

## Local application

The reference setup uses Windows 11, Ubuntu on WSL2, and Python with a virtual environment. From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r structured/requirements.txt
python nvidia_register/app.py
```

Open http://localhost:8774/coverage to fetch historical quarterly data. Provide your own SEC contact email in the local form. The public demo at http://localhost:8774/demo works with the included snapshot. Downloaded filings, local reviewer decisions, and notifications are runtime data stored outside version control.

## Workflows

- New filings: run `nvidia_sync.py --contact` with Python and your email. The submitted accession number identifies a filing.
- Quarterly figures: `nvidia_register/earnings.py`, `reconcile.py`, and `periods.py` parse the original HTML, compare the current-quarter GAAP columns, and retain missing quarters in the report.
- Narrative events: the Windows Ollama service (localhost port 11434) serves the local model configured in `llm/ollama-qwen38-27b.json`. Run `python nvidia_inbox_review.py ACCESSION` after fetching a filing. Keep the model and inference logs on the workstation.
- Monitoring: `nvidia_register/monitor.py` performs one collection pass. `install_daily.py` registers a Windows scheduled task when invoked explicitly.
- Earlier extraction trials and source manifests are preserved for reproducibility. See [historical extractor notes](README-extractor-history.md).

## Verify

```sh
python -m unittest discover -s . -p 'test_*.py'
python -m unittest discover -s nvidia_register -p 'test_*.py'
```

The tag `nvidia-baseline` identifies the NVIDIA-only implementation before multi-company development.
