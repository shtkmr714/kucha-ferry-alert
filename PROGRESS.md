# Ferry Forecast — 作業進捗メモ

最終更新：2026年6月9日

---

## ⚠️ 重要：データ系統の不連続（精度検証の前提）

`daily_operation_log`（座間味）は、取得ロジックの変更により **3つの期間でデータ仕様が異なる**。
**期間をまたいだ Apple-to-Apple の精度比較・モデル学習はできない**ため、分析時は必ず期間を区切ること。

| 期間 | 取得方法 | 主な特徴 |
|---|---|---|
| **〜2026/5/9** | **手入力中心** | `announcement_text` 等を手動記入。運航実績は正確だが気象列の粒度・基準が自動期と異なる。約81%で `announcement_text` 空欄（=手入力期は別ソース）。 |
| **2026/5/10〜2026/6/9** | 自動取得（不完全） | スクレイプ＋API自動記録。ただし下記フィールドが**バグで常時空欄/誤値**：<br>・`swell_period` / `swell_direction` / `wind_direction_am` / `precip_am` / `precip_total`（ロガーがハードコード `""`）<br>・`jma_warning_today`（早期注意情報に「今日」がなく常時「なし」）<br>・`typhoon_active` / `typhoon_distance_km` / `typhoon_category` / `typhoon_max_wind`（誤ったAPI `list.json` を参照し常時0/空） |
| **2026/6/9〜（本修正後）** | 自動取得（修正済み） | 上記フィールドを実値で記録：<br>・気象列は `analyze_period()` の算出値を反映<br>・`jma_warning_today` は実警報API（`warning/471000.json`）から生成<br>・台風列は `targetTc.json`→`{tcid}/forecast.json` から取得 |

**含意：**
- モデル学習・キャリブレーションは原則 **2026/6/9 以降の自動期（修正後）** のデータで行う。
- それ以前を使う場合は `wave_max` / `wind_max` / 運航実績（`hs_bin*` / `ferry_operated`）など**全期間で一貫している列のみ**を使う。
- `jma_warning_*` / `typhoon_*` / `swell_period` 等は **6/9以降のみ有効**。

> 修正コミット: operation_logger.py（空欄フィールド実値化・台風API修正）, ferry_alert.py（get_jma_probability の delta マッピング拡張・warnings 受け渡し）

---

## 完了済み

### Instagram Graph API セットアップ
- Meta Developerアプリ作成済み（ビジネスポートフォリオ接続済み）
- Instagram Business Account（@okinawa_ferry_forecast）を接続
- 長期アクセストークン取得済み（有効期限60日・2026年7月上旬に要更新）
- GitHub Secrets に登録済み：
  - `INSTAGRAM_ACCESS_TOKEN`
  - `INSTAGRAM_USER_ID`

### GitHub リポジトリ
- `https://github.com/shtkmr714/kucha-ferry-alert`
- Publicリポジトリ（Pagesのため変更）
- GitHub Pages 有効（`main` / root）
  - 公開URL: `https://shtkmr714.github.io/kucha-ferry-alert/`
  - 画像URL例: `https://shtkmr714.github.io/kucha-ferry-alert/images/img1_header_YYYYMMDD_HHMM.png`

### 主要ファイル
- `ferry_alert.py`：フェリー運航チェック本体（652行・GitHub版）
  - 末尾付近（処理完了の直前）に `run_publisher()` 呼び出し追加済み
- `forecast_publisher.py`：画像生成・SNS投稿（ローカル版が正）
- `.github/workflows/ferry_alert.yml`：GitHub Actions ワークフロー

### 動作確認済み
- GitHub Actions の手動実行（workflow_dispatch）
- 画像4枚の生成（/tmp/ferry_images/）
- GitHub Pages へのアップロード（images/ディレクトリ）
- Instagram カルーセル投稿（4枚）✅
- キャプション（フォールバック版）の投稿
- Google Sheets DB への自動記録（daily_operation_log・daily_forecast）✅

---

## ✅ 解決済み：フォント問題

- GitHub Actions環境での実際のNotoフォントパスを確認し、`forecast_publisher.py` に反映済み
- Actions手動実行で日本語テキストが正常表示されることを確認済み

---

## ✅ 完了：Google Sheets DB 構築（2026-05-10〜11）

### 設計
- `setup_sheets.py` で2シート構成のスプレッドシートを初期化
  - `daily_operation_log`：実績ログ（42カラム・1日1行・8:15のみ）
  - `model_calibration`：精度検証用（将来利用）
  - `daily_forecast`：予報ログ（forecast_publisher.pyが自動生成・1日最大4行）

### 実装ファイル
- `operation_logger.py`（新規）：座間味村HPスクレイピング + Sheets書き込み
- `forecast_publisher.py`：`save_to_sheets()` が `daily_forecast` に記録
- `ferry_alert.py`：`[DB]` ブロックを Publisher の直前に追加
- `ferry_alert.yml`：`gspread google-auth` を pip install に追加、`GOOGLE_SERVICE_ACCOUNT_JSON` / `GOOGLE_SHEETS_ID` を env に追加

### スクレイピング設計（2026-05-11 確定版）
- 取得URL: `https://www.vill.zamami.okinawa.jp/`（トップページ）
  - `/info/ship.html` は404のため使用不可
- HTMLの構造的クラスで判定（テキスト解析は不使用）：
  - `div.bl_routeSchedule_suspension` の有無 → 欠航判定
  - `div.bl_routeSchedule_row > div.bl_routeSchedule_num` → 便別運航判定
  - `div.un_homeFerryInfo_head` → announcement_text・weather_desc取得元

### 動作確認済み
- スプレッドシートへの記録成功（`daily_operation_log`・`daily_forecast`）
- `ferry_operated`・`hs_bin1/2/3_operated`・`ferry_cancel_reason` 正常取得
- 重複チェック（8:15以降の2回目実行でスキップ）動作確認

---

## ✅ 完了：投稿UI・投稿頻度の改善（2026-05-20）

### ① 表紙削除・3枚構成
- ヘッダー画像（img①）を廃止。短期予報を1枚目にすることでフィードで即座に内容が伝わるように
- 短期予報（新1枚目）のタイトルを刷新：
  - 「座間味島・阿嘉島」サイズ50・白
  - 「フェリー欠航予測」同サイズ・同色（白）
  - 英語サブタイトル「Zamami / Aka  Ferry Cancellation Risk」サイズ28（"Forecast"→"Risk"に変更：%が欠航確率であることを明確化）
- 画像枚数: 4枚 → 3枚（short / longterm / weatherdata）

### ② 13:00台の投稿を高リスク時のみに限定
- **8:15**: 常にInstagram投稿
- **13:00**: 以下いずれかを満たす場合のみ投稿、それ以外はスキップ
  - 明日 or 明後日の高速船欠航確率 ≥ 61%（オレンジゾーン以上）
  - 当日便が気象理由で欠航（Google Sheetsの8:15記録から取得・`weather`のみ対象、`dock`/`equipment`は除外）
- 欠航理由の種類: `weather`（気象）/ `equipment`（機材トラブル）/ `dock`（ドック入り）/ `none`（通常運航）

### GitHubコミット
| コミット | 内容 |
|---|---|
| `08b7544` | 表紙削除3枚構成・13時台高リスク時のみ投稿 |
| `90b83ab` | 13時台投稿条件に当日便気象欠航を追加 |
| `eff079b` | タイトル白統一・欠航判定をスプシ+weather限定に |
| `9a91b82` | 英語サブタイトルを "Ferry Cancellation Risk" に変更 |

---

## ✅ 完了：計画運休機能の実装（2026-05-20）

### 概要
ドック入り等の計画運休情報をInstagram投稿（画像・キャプション）に自動反映する機能を追加。

### 仕組み
1. **自動取得**：座間味村HPトップページ（https://www.vill.zamami.okinawa.jp/）を BeautifulSoup でスクレイプ → Claude API で構造化データ抽出
2. **手動補足**：`planned_suspensions.json` に手動でエントリ追加可能（HPに掲載前の情報など）
3. **齟齬検出**：両ソースを照合し、食い違いがあれば Slack アラート送信
   - HP照合は開始日まで10日以内の運休のみ対象（先々の予定はHP未掲載が正常なため）
4. **画像反映**：
   - img②短期予報：運休サービスのみグレーパネル（#7B96A4）＋「公式発表」バッジ＋「運休 / Suspended」
   - img③長期予報：斜線ハッチ＋「運休 / Suspended」表示
   - img④気象データ：上部に計画運休セクション挿入（緑系カラー）
5. **キャプション反映**：日本語・英語両セクションに運休情報を記載

### 新規ファイル
- `planned_suspensions.json`：手動入力用JSON（現在：5/27-6/5 クイーンざまみ ドック入りのエントリあり）

### 変更ファイル
- `ferry_alert.py`：`get_planned_suspensions()` 他4関数追加、`run_ferry_check()` に組み込み
- `forecast_publisher.py`：`_is_date_suspended()` / `_fmt_wave()` / `_fmt_prob()` 追加、各画像生成関数を修正

### GitHubコミット
| コミット | 内容 |
|---|---|
| `9c37d27` | 計画運休機能の実装（メイン） |
| `bb36aa6` | ferry_alert.py 関数重複の削除 |
| `709f71d` | CTOレビュー指摘3件修正（二重呼び出し・service_ja未定義・HP未掲載検出追加） |
| `63299a2` | HP照合チェックを開始10日以内に限定 |

---

## ✅ 完了：DESIGN.md 作成（2026-05-11）

`02_ferry-forecast/DESIGN.md` に以下を記録：
- システム概要・実行フロー
- 欠航スコアリング式・変換関数の設計思想
- データスキーマ（全42カラム定義）
- HPスクレイピング設計（HTML構造付き）
- 外部API一覧
- **他離島展開時の変更箇所チェックリスト**

---

## 参照デザイン

`assets/` フォルダ内の3ファイルが目標デザイン：
- `img1_header_template.png`：ヘッダー（青背景・島名・日付）
- `img2_short_template.png`：短期予報（緑背景・2列・%表示）
- `img3_longterm_template.png`：長期予報（赤背景・バーチャート）

---

## その他の未対応事項

| 項目 | 状況 | 備考 |
|---|---|---|
| Anthropic API クレジット | 残高不足 | フォールバックキャプションで動作中 |
| X（Twitter）投稿 | 未対応 | アカウント・API未設定 |
| Google Sheets DB | ✅ 自動記録開始済み | — |
| Instagram投稿 | ✅ 済 | — |
| cron自動実行 | ✅ GitHub Actions cron設定済み | 8:15 / 13:00 JST |
| Instagramトークン更新 | 2026年7月上旬期限 | 60日後に手動更新が必要 |
| swell_period / precip 等 | 未取得（空欄） | Open-Meteoから取得可能・将来対応 |

---

## GitHub Actions ワークフロー概要

```yaml
# ferry_alert.yml
permissions:
  contents: write

env:
  ANTHROPIC_API_KEY: ...
  SLACK_WEBHOOK_URL: ...
  INSTAGRAM_ACCESS_TOKEN: ...
  INSTAGRAM_USER_ID: ...
  GITHUB_TOKEN: ...
  GOOGLE_SERVICE_ACCOUNT_JSON: ...
  GOOGLE_SHEETS_ID: ...

steps:
  - name: Install Japanese fonts
    run: sudo apt-get install -y fonts-noto-cjk
  - name: Install dependencies
    run: pip install requests anthropic beautifulsoup4 lxml python-dotenv Pillow gspread google-auth
```

---

## 次回セッション開始時のチェックリスト

1. [x] フォントパス確認・`forecast_publisher.py` に反映・プッシュ済み
2. [x] Actions手動実行で画像品質を確認（日本語正常表示を確認済み）
3. [x] Instagram投稿テスト済み
4. [x] Google Sheets DB 自動記録開始済み・動作確認済み
5. [x] cron自動実行（GitHub Actions 8:15 / 13:00 JST）設定済み
6. [ ] X（Twitter）アカウント作成 → 開発者申請 → 投稿テスト
7. [ ] Anthropic APIクレジット補充（残高不足・フォールバックで動作中）
8. [ ] Instagramアクセストークン更新（2026年7月上旬に期限）
9. [ ] daily_operation_log データ蓄積後にスコアリング精度を検証（目安300件）
