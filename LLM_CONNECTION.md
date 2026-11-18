# ローカルLLM：WSL2のPythonからWindowsのOllamaへ接続

2026-11-17の現在の構成は、Windows 11のOllamaとWSL2 UbuntuのPython。
対象PCはRyzen 7 5700X、DDR4 32GB、RTX 5060 Ti 16GB。
Dockerへの移行やモデルの再インストールは今回の作業に含まない。
WSLから `http://127.0.0.1:11434/api/tags` の取得に成功済み。
プロジェクトは `~/projects/nvidia_evidence_extractor`、仮想環境は `.venv`。

## 実機で確認されたこと

ユーザー提示のOllamaログと `nvidia_llm_trial.zip` に基づく。

| 設定ファイル | モデル | 確認状況 |
|---|---|---|
| llm/ollama-qwen35-9b.json | nvidia-qwen35-9b:16k | 元モデルqwen3.5:9b-q8_0、短文試験時100% GPU。抽出2資料に応答 |
| llm/ollama-qwen38-27b.json | nvidia-qwen38-27b:16k | 元モデルqwen3.8:27b-q4_K_M、短文試験時24%/76% CPU/GPU。抽出はdry-runのみ |

CPU/GPU比率は配置の表示であり、速度比ではない。
9Bの旧版試行は1資料目19.282秒、2資料目23.825秒。
1資料目は任命を取りこぼし、2資料目はID形式で停止した。
出力内容にも問題があり、成功した抽出結果として採用していない。
更新内容と再試行手順は [llm/TRIAL_FIX.md](llm/TRIAL_FIX.md) に記載。

## 共通の比較条件

- コンテキスト16,384、1資料ずつ逐次処理、最大3資料。
- 同じ原文、対象節、指示文、出力JSON Schema、Python検査。
- temperature=0、seed=42、reasoning_effort=none。
- 最大出力8,000トークン、1リクエストの通信タイムアウト900秒。
- 接続先 `http://127.0.0.1:11434/v1`、外部APIキーは不要。
- 失敗時の自動再試行・モデル切替は行わない。

再現性を高める設定であり、版や機器をまたぐ完全一致の保証ではない。
モデル世代と量子化が異なるため、将来の比較も実用構成の比較であり、CPU併用だけの効果は分離できない。

## 今回の実行

取得済み原文と仮想環境を使う。新たなSEC取得は不要。
更新版を適用した後、WSLで実行する。

```bash
cd ~/projects/nvidia_evidence_extractor
source .venv/bin/activate
python -m unittest -q && python llm_runner.py --config llm/ollama-qwen35-9b.json --execute
```

`--execute`を省略すると通信せずに入力と設定を組み立てる。
dry-runはOllamaの起動・モデルの存在・空きメモリを確認するものではない。
別のWSLターミナルの `ollama.exe ps` で実行中のGPU/CPU配置とCONTEXTを確認できる。
今回は9Bを1回だけ試す。27Bの実行は、この結果の確認後に判断する。

## 出力と確認

各実行は新しい `llm_runs/<日時>/` に保存される。
送信内容・応答・候補JSON・検査結果を保存し、認証ヘッダーは保存しない。
失敗時は後続資料への送信を止める。過去の失敗ログを書き換えない。

- `completed_pending_review`：全資料の機械検査通過。意味の確認は未承認。
- `mechanical_validation_failed`：形式・根拠などの検査で停止。
- `request_or_output_error`：通信や応答形式で停止。
- `dry_run`：通信していない。

終了コード0はdry-runまたは機械検査通過、2は原文・設定・通信・検査の停止、
1は設定形式・入出力のエラー。
タイムアウト後もサーバー側で処理中の可能性があるため、状態を確認してから再実行する。

`run.json` の `requests[].seconds` は読込み・入力処理・生成・通信を含む応答時間。
`completion_tokens / seconds` は純粋な生成速度ではない。
指示文を変更したので、旧版との時間差をそのままモデル速度の改善と解釈しない。

原文のハッシュ不一致を無視したり、期待ハッシュを自動更新したりしない。
`events=[]` も機械検査を通り得るため、出来事の取りこぼしは別に確認する。
人物と役職の対応、和訳、日付の役割、条件の漏れを原文と照合する。
この3資料は方式設計に用いた既知資料であり、未知資料での汎化精度は未評価。
参照JSONは未承認の例であり、モデルの正解として無条件に使わない。

## 公式資料

- https://docs.ollama.com/windows
- https://docs.ollama.com/api/openai-compatibility
- https://docs.ollama.com/modelfile
- https://docs.ollama.com/capabilities/structured-outputs （2026-11-17再確認。Schemaを指示文にも渡す方法の参考）
