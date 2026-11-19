# Company Disclosure Monitor

**GitHub Pages edition** · [Local preview, official review commands and manual publication (日本語)](pages/README.md)

A local SEC filing register with a self-contained English/Japanese demo. The NVIDIA baseline traces quarterly GAAP revenue and diluted EPS from Form 8-K exhibits to their source tables. The full workflow also supports filing collection, locally generated event candidates, human review and CSV delivery.

## Magnificent Seven filing index

Open <http://localhost:8774/companies> after starting the local application. The index opens with a bundled snapshot of 24 filings from August 21 through November 18, 2026 (inclusive). Filter Apple, Microsoft, Alphabet, Amazon, NVIDIA, Meta, or Tesla by issuer, narrow to SEC Item 2.02, follow an accession to the SEC primary document, or export the filtered rows as CSV.

Use the update form with your SEC contact email to fetch each issuer's submissions over the latest 90 calendar days (US Eastern time). The index covers Form 8-K and 8-K/A, keyed by issuer CIK and accession. An update replaces the window for each successful issuer. A failed issuer keeps its last successful data, with an error and the date shown. The stored data consists of filing metadata and source links. The company detail links open the financial comparisons for all seven issuers. Narrative review is available for NVIDIA.

## Quarterly reconciliation for all seven companies

Open `/workflow` in the local application for a common review queue, or open a company from `/companies` for its eight-quarter chart and source links. The bundled November 19, 2026 snapshot contains 56 fiscal quarters and 112 figures. It uses consolidated GAAP revenue (USD millions) and diluted EPS (USD/share). The eight quarters are issuer-specific. Filter the common CSV by actual quarter-end dates to compare a calendar window across issuers.

The daily or manual update detects Item 2.02 earnings releases, locates the release’s actual quarter and waits for its 10-Q or 10-K. SEC accession, issuer, fiscal quarter, period end, monetary units, and source hashes identify the evidence. For periodic filings Python selects directly reported three-month consolidated XBRL facts. Fourth-quarter results remain visible from the release even if the 10-K has no comparable quarterly fact. Amended or conflicting filings remain in the review queue with their source links. Unmapped releases and failed downloads are shown separately. The filing index’s 90-day metadata update is independent of the quarterly update.

Run from the repository root with the application environment, or use the **7社を更新** button in the review queue. The first online refresh downloads archival evidence. Later refreshes reuse cached SEC archive documents.

```sh
python nvidia_register/pipeline.py --contact YOUR_EMAIL
```

To install a daily update on the Windows host, run the following in WSL with that same Python environment. Windows runs the task as the current user, using Windows local time.

```sh
python nvidia_register/install_daily.py --workflow --contact YOUR_EMAIL --at 09:00
```

Choose **照合** to filter exceptions or **担当者確認** to filter human review state. A reviewer records one decision per company and quarter; changes in the underlying evidence flag earlier decisions for re-review. CSV export includes only matching metrics and retains provenance, decision, reviewer, note and timestamp. Review history and raw downloads are kept locally under `register_data/`. The latest eight quarters remain on screen while older saved rows and decisions stay in SQLite.

## Offline demo and original comparison cases

Open [the bilingual filing overview](nvidia_register/public_demo/index.html) directly in a browser, or open `/demo?lang=ja` in the local application. The overview lists the latest earnings release by company. Switch to the quarter history or select an automated check to filter all eight quarters. The displayed rows can be exported as a public CSV. Select a quarter for its revenue and diluted EPS, links to SEC evidence, and relevant amended filings. The overview and chart use issuer-specific accent colors and text ticker labels. The public view excludes reviewer identities and notes. The local application renders the same company pages from the current database. The standalone file contains the bundled data and has no review editor.

The earlier filing-pair exercises remain available in the local application by adding `&baseline=1` to a company URL with a language parameter, for example `/company/NVDA?lang=ja&baseline=1`. The NVIDIA baseline compares two tables inside one exhibit. The original six company-specific cases compare a pinned exhibit against its 10-Q. Their scripts and sample CSVs remain available.

## Filing text and Qwen evidence

Open a company and choose its **開示要点** tab. Each filing is keyed by issuer and SEC accession. The filing selector groups saved results separately. Each event field links to its numbered source paragraph. The quarterly evidence panel links directly to the selected earnings 8-K. A completed section with zero candidates is distinct from a filing with no saved output.

The bundled November 17 local trial covers three NVIDIA filings and four candidates awaiting review. The Git demo embeds their coverage counts. The private review copy includes their text and cited paragraphs. The WSL application reads the optional local snapshot from `register_data/qwen_ledger_snapshot.json` and overlays current Qwen sections and review decisions from the register. Opening a ledger does not run inference.

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
