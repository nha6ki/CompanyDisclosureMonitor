# 出来事・日付・条件の整理と検査

NVIDIAの既存3資料を用いて、出力形式と検証処理を追加しました。4つの出来事を整理した
参照例は、ChatGPTが保存済みの原文を読んで作成しました。**Pythonによる意味抽出や、
LLM APIの実行結果ではありません。** ユーザーによる意味内容の確認・承認も未完了です。

## 今回動くもの

- 原文を再読込し、固定したSHA-256と一致する資料・節だけを利用する。
- JSONの必須項目・型・分類値を検査する。
- 各項目の根拠ブロックが、その資料の指定した節に存在するか確認する。
- 人物名・会社名が、指定した根拠に文字列として存在するか確認する。
- 日付を英語の月名またはISO形式、半期をfirst/second halfという原文表記と照合する。
- 提出日より後の決定日・通知日・契約日や、完了扱いと予定・見込みの矛盾を検出する。
- 原文の全文を含む、モデルへ渡すための入力JSONを生成する。実際の通信は行わない。

意味の確認は常にpendingとして出力します。例えば、根拠段落に2人の氏名があっても、
それぞれの役職との正しい対応はこの検査だけでは保証できません。日付が本文に存在することと、
その日付の役割が正しいことも別です。日本語要約・条件の意味の照合は未実装です。

## 参照例と項目

| 提出日 | 対象 | 整理する点 |
|---|---|---|
| 2026-05-08 | Suzanne Nora Johnson | 5月7日の任命決定と7月13日の就任予定を分離 |
| 2026-04-27 | Donald Robertson / Scott Gawel | 2人の退任・就任を分離し、退任後の別役職での支援を保持 |
| 2026-09-03 | Hugging Face | 契約締結と買収完了を区別し、2027年前半という見込みと承認条件を保持 |

`subject`は出来事の対象者・対象企業、`organization`はその出来事を開示した会社です。
`as_of_date`は判断の基準となる提出日で、出来事の日付ではありません。
`dates`には役割・値・精度・確実性・根拠位置を別々に記録します。
不明な日付はnull、必要な条件が読み取れない場合はmissing_informationに残します。
`conditions=[]`は「条件なしを確認済み」という意味ではありません。
`no_supported_event`や`needs_review`も「本文に出来事がない」ことの証明ではありません。

対象は人事と買収契約の整理例です。報酬明細・財務数値表の構造化は今回の範囲外です。
この3資料は方式設計に使った既知資料であり、未知資料での精度評価には使いません。

## 実行

Python 3.10以上。従来の抽出・節保持は標準ライブラリだけですが、この追加処理では
JSON Schemaによる検査にjsonschemaを使います。

```bash
python -m pip install -r structured/requirements.txt
python structured_events.py validate --source-dir /path/to/original-five-cache --source-dir /path/to/additional-five-cache
```

取得済み原文を`cache/<提出番号>/primary.html`または`validation/cache/<提出番号>/primary.html`
に置いてあれば、`--source-dir`を省略できます。原文の取得は従来どおりread_sections.pyの
`--fetch --contact your-address@example.com`で行えます。本処理は取得済み原文の自動上書きや
期待ハッシュの更新、外部モデルへの送信を行いません。

モデル向け入力の準備：

```bash
python structured_events.py prepare --source-dir /path/to/original-five-cache --source-dir /path/to/additional-five-cache
```

`structured_output/model_input.json`に指示、出力Schema、3資料の対象節全文とブロック番号を
まとめます。参照例・既存要約はモデル入力に含めません。全文の中間入力と元HTMLはZIPに含めず、
取得済み原文から再生成します。後続の接続設定はLLM_CONNECTION.mdを参照してください。
Windows用Ollamaの2モデル比較設定を用意していますが、実機での推論は未実行です。

別途得た抽出結果を検査する場合：

```bash
python structured_events.py validate --candidate /path/to/model_result.json --source-dir /path/to/cache
```

別の候補を検査した場合はvalidation.jsonを出力します。参照用のHTMLは書き換えません。
検証結果の`llm_invocation_performed=false`は、この検証プログラム自体がLLMを呼ばないという意味です。
入力JSONが別の環境でLLM生成されたかどうかを推定した値ではありません。

終了コード0は機械的な検査通過、2は原文不一致・入力不整合など、1は設定・入出力のエラーです。
0でも意味内容の正しさや、候補なしという判断の正しさは確認できません。

## ファイル

- `structured/event_schema.json`：モデルに依存しない出力形式。
- `structured/extraction_prompt.txt`：抽出時の指示案。
- `structured/reference_examples.json`：3資料・4出来事の参照例。
- `structured/reference_provenance.json`：参照例の作成方法と未承認の状態。
- `structured/selection.json`：今回の対象と選定理由。
- `structured_events.py`：原文の読込み、入力生成、検証、参照画面生成。
- `structured_output/validation.json`：入力ハッシュ、検査結果、根拠位置のハッシュ、要確認項目。
- `structured_output/nvidia_structured_review.html`：出力形式を確認する画面。

## 検査の確認

```bash
python -m unittest -v test_structured_events.py
```

合成した別の会社・人物・日付を用いて、架空の氏名、未記載・存在しない日付、決定日と発効日の
取り違え、半期の勝手な日付化、予定の完了扱い、根拠位置の誤り、原文不一致、資料の欠落、
出来事の重複、承認状態の不正な追加、不明値・候補なしの扱いを確認します。
意味が誤っていても機械的な検査に通る例も、限界を記録するためのテストに含めています。

## 実需との関係と次の段階

公開の文書抽出案件では、情報を決まった構造に整理し、正しい企業・契約へ紐付け、根拠を保持する
要求があります。今回の形式と検査はその一部分に対応します。NVIDIA専用サービスの発注需要、
受注成果、市場全体の規模を示すものではありません。

確認例（2026-11-17）：
https://www.upwork.com/freelance-jobs/apply/Senior-Python-Engineer-for-Web-Scraping-Document-Extraction_~022121844619172281293/

次は実行先・モデルを設定して同じ3資料から自動抽出を行い、参照例との差を確認します。
その後、設計に使っていない資料で抜け・誤り・根拠との対応を評価する必要があります。
