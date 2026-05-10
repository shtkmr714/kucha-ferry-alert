# Ferry Forecast — 作業進捗メモ

最終更新：2026年5月11日

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
