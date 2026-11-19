#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
contact="${1:?使い方: bash pages/enable-daily.sh SEC連絡先メール [HH:MM]}"
at="${2:-09:00}"
cd "$root"
if [[ -z "${WSL_DISTRO_NAME:-}" ]]; then echo 'WSLのUbuntu内で実行してください。' >&2; exit 1; fi
python3 -m venv .venv-pages
.venv-pages/bin/python -m pip install -r pages/requirements-reports.txt -r structured/requirements.txt
.venv-pages/bin/python nvidia_register/install_daily.py --pages --contact "$contact" --at "$at" --start-now
echo '初回チェックをWindowsのバックグラウンドタスクに依頼しました。'
echo '確認画面を開くには python3 pages/manage.py preview を実行してください。'
