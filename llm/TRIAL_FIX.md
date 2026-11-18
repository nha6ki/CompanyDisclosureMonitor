# 9B初回試行を受けた修正：nvidia-trial-fix-1

2026-11-17。今回はNVIDIAの既知3資料の再試行準備まで。

## 変更点

1. 生成用SchemaでもID等の文字列パターンを保持し、完全な出力Schemaを指示文にも追加。
2. eventsがある場合の処理状態、任命決定と将来発効、日付の役割とcertaintyを明確化。
3. 会社名を記載した根拠と出来事の根拠の併記、役職名の保持、報酬明細を省く指示を明確化。
4. 提出日より未来の発効・移行支援終了・完了日をreportedとした出力をPythonで拒否。

モデルの出力を自動で書き換えて合格させる処理は追加していない。
人物名や具体的な日付を答えとして指示文へ埋め込んでいない。
原文・3資料の選択・モデル設定・参照JSONは同じ。
給与等を対象外にする指示は、出来事の重要条件まで捨てる意味ではない。
未抽出や誤訳をすべて検出できるわけではなく、実機精度の改善は未確認。

## 既存WSL環境への適用

今回のZIPを `C:\Users\<WindowsUser>\Downloads\nvidia_evidence_extractor.zip` に保存する。
ブラウザが `(1)` 等を付けた場合は、今回のZIPを上記の名前へ変更する。
次をWSLに貼り付ける。`&&` により、途中で失敗した場合は後続を実行しない。

```bash
cd ~/projects/nvidia_evidence_extractor &&
source .venv/bin/activate &&
python -m zipfile -e /mnt/c/Users/<WindowsUser>/Downloads/nvidia_evidence_extractor.zip /tmp/nvidia-trial-fix-1 &&
python /tmp/nvidia-trial-fix-1/nvidia_evidence_extractor/apply_trial_fix.py "$PWD" &&
python -m unittest -q &&
python llm_runner.py --config llm/ollama-qwen35-9b.json --execute
```

更新処理は変更対象の旧版・新版ハッシュを先に検査する。
手元のコードが別版なら、何も上書きせず停止する。
変更前ファイルは `update_backups/<日時>/` へ退避する。
`.venv`、`cache`、`validation/cache`、`llm_runs` は差し替え対象にしない。
モデルのダウンロードや原文の取得をやり直す必要はない。

全テストは64件。推論は9Bだけ、最大3リクエストで失敗時は停止する。
`completed_pending_review` は内容の正しさの確定ではない。
実行結果を確認してから、取りこぼし・日付・条件を再度照合する。

## 再試行結果をまとめる場合

推論の終了後、成功・失敗のどちらでも次を実行できる。
過去の試行との比較用に `llm_runs` のJSONをまとめる。ZIPはAPIに送信しない。

```bash
python - <<'PY'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
out = Path('/mnt/c/Users/<WindowsUser>/Downloads/nvidia_llm_retry1.zip')
with ZipFile(out, 'w', ZIP_DEFLATED) as z:
    for p in sorted(Path('llm_runs').rglob('*.json')):
        z.write(p, p.as_posix())
print(out)
PY
```

このZIPを渡せば、送信された指示・実際の応答・失敗箇所・所要時間を照合できる。
