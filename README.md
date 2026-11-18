# Company Disclosure Monitor

A local SEC filing register with a self-contained English/Japanese demo. The NVIDIA baseline traces quarterly GAAP revenue and diluted EPS from Form 8-K exhibits to their source tables. The full workflow also supports filing collection, locally generated event candidates, human review and CSV delivery.

## View the demo

Open [the interactive filing register](nvidia_register/public_demo/index.html) in a browser after cloning. Choose a metric and quarter to compare reported figures, inspect the SEC source and download the fixed dataset. The eight-quarter snapshot covers FY2025 Q4 through FY2027 Q3. Fifteen automated comparisons match exactly and one matches at reported precision. Human review is pending for all 16 figures. The sample CSV files are in [examples](nvidia_register/examples/).

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
