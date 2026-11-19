# GitHub Pages 版の使い方

7社の直近8四半期を収録した日本語・英語の静的サイトです。台帳、開示要点、SEC原文へのリンク、CSV、14個のExcelレポートを同じ公開フォルダーから配信できます。準備と正式な担当者確認はWSLで行います。

## 手元で開く

リポジトリのルートで実行します。Python 3.10以降の標準ライブラリを使います。

```bash
python3 pages/manage.py preview
```

Windowsのブラウザーで <http://127.0.0.1:8765/?lang=ja> を開きます。終了は Ctrl+C、ポートの変更は `--port 8766` です。公開ページと同じ内容を生成するだけなら `python3 pages/manage.py build` を使います。

## 訪問者が試せること

企業ページで四半期を選び、SEC原文の照合欄にある「確認操作を試す」を開いてください。「確認済みにする」「確認待ちに戻す」を押すと、デモの状態と色が切り替わります。正式な担当者確認はその上に表示します。デモを閉じるか再読み込みすると試した状態は消えます。CSVの担当者確認には正式な状態が入ります。Excelの「要確認一覧」は資料間の自動照合の結果です。

## 正式な確認を記録する

次はAppleのFY2026 Q4を例としたコマンドです。まず `show` で会社・決算期・現在の状態を確認し、SEC原文の確認を終えてから `reviewed` を実行してください。

```bash
python3 pages/manage.py show AAPL FY2026Q4
python3 pages/manage.py reviewed AAPL FY2026Q4
python3 pages/manage.py build
```

ローカルのブラウザーを再読み込みすると、概要、企業別台帳、CSVに反映されます。確認待ちに戻すには `reviewed` を `pending` に置き換えます。確認中の資料差し替えも検知したい場合は、`show` に表示されたハッシュを `reviewed` または `pending` の `--expect` に指定します。

`pages/data/reviews.json` に対象企業・四半期・版・状態・記録時刻を履歴として保存します。もう一度同じ操作をしても同一の記録は増えません。資料や照合結果の版が変わった四半期は「再確認」と表示します。移行時点の記録6件を取り込み、サイト利用者の内部アカウントIDは除いています。各決算期の公開用データには現在の正式な状態だけを埋め込みます。従来のWSLアプリのSQLiteと独立しており、このPages版の正式な記録先はこのJSONです。

## GitHubへの保存と手動公開

1. ローカルで確認後、作業ブランチ `feature/github-pages` を既存の `nha6ki/CompanyDisclosureMonitor` に保存するかをご自身で決定します。確認を追加した場合は先に `pages/data/reviews.json` をコミットします。作業ブランチの保存には、対象のリモートを確認してから `git push -u origin feature/github-pages` を実行します。
2. 公開を決めた段階で、作業ブランチの変更をプルリクエストなどで既定ブランチに取り込みます。GitHubの **Settings → Pages → Build and deployment → Source** は **GitHub Actions** を選択します。利用できるリポジトリの可視性はGitHubプランに従います。
3. **Actions → Publish disclosure monitor → Run workflow** で公開するブランチを選び、ご自身で実行します。付属ワークフローの起動条件は `workflow_dispatch` のみです。公開に使うファイルは `_site/` のHTMLとレポートです。次回以降も正式な変更を反映するタイミングで手動実行します。

[GitHub Pages の公開元](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site) · [手動ワークフロー](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)

## 決算データとExcelを更新する場合

添付したデータの取得日は2026年11月19日です。確認状態だけの変更には以下の工程は不要です。新しい決算を反映するときはローカルの収集工程で得た `quarter_history.json`（`asof`・7社の `issuers` を含む形式）と対応するSEC資料キャッシュを使います。

```bash
python3 pages/finance/refresh.py /path/to/quarter_history.json /path/to/pipeline-cache
python3 -m venv .venv-pages
.venv-pages/bin/python -m pip install -r pages/requirements-reports.txt
.venv-pages/bin/python pages/reports.py
python3 pages/manage.py build
```

金額や出典が変わったときはレポートを再生成してください。ビルド時にレポートと元データの一致を検証します。1社だけ再生成する場合は `--company AAPL` の形式で指定します。公開HTMLには公開用の開示要点を使用し、Qwenの非公開の確認候補やレビューメモを取り込む操作は含みません。

## WSLで日次更新とQwen抽出を有効にする

配布ZIPを展開したディレクトリの `setup-daily.sh` をWSLで実行してください。引数を省略すると、SEC用の連絡先メールを対話入力します。実行時刻は毎日09:00（Windowsの現地時刻）です。時刻を変える場合はメールと時刻を指定します。

```bash
bash setup-daily.sh your-email@example.com 09:00
```

従来の `feature/github-pages` 作業ブランチをローカルで更新し、専用の仮想環境を準備します。Windows タスク **M7 Pages Daily** を登録して初回をすぐに起動し、WSL上でプレビューを開きます。Windows側でOllamaを起動し、登録済みの `nvidia-qwen38-27b:16k` を使用してください。PCが停止中は実行できません。Windowsが実行可能になってからの遅延起動を有効にしています。

- [台帳](http://127.0.0.1:8765/?lang=ja)で決算指標とExcelを確認できます。既に同じポートでプレビューを開いている場合、以前のプロセスを Ctrl+C で終了し、更新した版で開き直してください。
- [開示要点の確認候補](http://127.0.0.1:8765/local-review/)には企業別の候補、SEC原文、根拠段落、照合に失敗した書類が表示されます。担当者の確認後に限り、画面にある節単位の承認コマンドでPages用データに取り込みます。公開操作は引き続き手動です。
- 確認画面を後で開くには、作業フォルダーで `python3 pages/manage.py preview` を実行します。確認画面を閉じても登録済みのタスクはWindowsで継続します。
- Windows PowerShellで `Get-ScheduledTaskInfo -TaskName 'M7 Pages Daily'` を実行すると、前回実行結果と次回実行予定を確認できます。停止する場合は `Disable-ScheduledTask -TaskName 'M7 Pages Daily'`、再開は `Enable-ScheduledTask -TaskName 'M7 Pages Daily'` です。

### 処理対象

7社のSEC提出一覧を定期確認し、決算8-K、10-Q、10-K、および訂正書類の一覧が変わった会社を再照合します。初回は7社とも照合します。同じ取得済み資料はローカルキャッシュから読み込みます。四半期の数値・出典・照合結果に変更があった会社だけ、台帳入力と日英のExcelを更新します。台帳のデータ取得日はそのデータを最後に更新した日で、チェック実行日時は確認候補画面に別途表示します。対象決算期を特定できない資料は同画面に原文確認の対象として記録します。

Qwenは初回に同梱データの取得日以降の8-K・8-K/Aと、確認期間内の各社直近1件をキューに入れます。2回目以降は新着・訂正書類と前回の未処理分が対象です。本文で認識したItem単位で、必要に応じて原文の段落番号を保って分割し、日本語の候補、日付、条件、根拠番号を検査します。添付一覧（Item 9.01）は集計のみで、添付決算資料の全文要約は対象外です。未対応見出し、長すぎる単一段落、機械検証で不一致の候補は画面に要確認と表示します。Qwen停止時は台帳とExcelを先に準備し、保存したキューを次回再試行します。

日次実行はGitへのコミット、push、Pagesへの公開を行いません。確認済みの候補だけを `pages/data/approved-highlights.json` に入れます。ローカルの未確認本文・処理ログ・モデル応答はGit管理外の `register_data/pages_daily/` に保存されます。手動で早めに再試行するには次のコマンドを使用します。

```bash
.venv-pages/bin/python pages/daily.py --contact your-email@example.com
```

機械検証に失敗した節も改めて推論したい場合は `--retry-review`、数値更新のみを実行したい場合は `--no-qwen` を追加します。
