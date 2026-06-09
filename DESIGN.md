# Ferry Forecast System — 設計・引継ぎドキュメント

> 対象: 座間味島（Phase 1）  
> リポジトリ: https://github.com/shtkmr714/kucha-ferry-alert  
> 最終更新: 2026-05-10

---

## 1. システム概要

座間味島（沖縄・慶良間諸島）へのフェリー・高速船の**欠航リスクをAIで予測し、自動発信するシステム**。

### 目的
- ゲストハウス（Homestay Kucha）が宿泊ゲストに欠航リスクを事前通知する
- 長期的には機械学習モデルで欠航予測精度を高める
- 将来的に他の離島（波照間島、与那国島など）にも展開する

### 実行フロー

```
GitHub Actions (毎日 8:15 / 13:00 JST)
    │
    ├── [1] Open-Meteo Marine API  → 波高・うねり・風速（8日分）
    ├── [2] 気象庁 forecast JSON   → 波高テキスト予報（今日〜明後日）
    ├── [2] 気象庁 probability     → 波浪早期注意情報（警報級確率）
    ├── [2] 気象庁 warning         → 注意報・警報
    ├── [3] 欠航スコア計算         → 0〜1のスコア → 欠航%に変換
    ├── [4] Slack通知              → 翌日予報サマリー + ゲスト向けメッセージ案
    ├── [DB] operation_logger.py  → Google Sheets（daily_operation_log）に記録 ← 8:15のみ
    └── [5] forecast_publisher.py → 画像生成 → Instagram投稿 / Sheets(daily_forecast)
```

---

## 2. ファイル構成

```
02_ferry-forecast/
├── ferry_alert.py          # メイン処理（データ取得・分析・Slack通知）
├── forecast_publisher.py   # 画像生成・Instagram投稿・DB保存（予報値）
├── operation_logger.py     # 実績ログ（座間味村HP スクレイピング → Sheets）
├── setup_sheets.py         # Google Sheets 初期セットアップ（1回だけ実行）
├── ferry_alert.yml         # GitHub Actions ワークフロー定義
└── DESIGN.md               # 本ドキュメント
```

GitHub リポジトリ上のパス対応：

| ローカル | GitHub |
|---|---|
| `02_ferry-forecast/ferry_alert.py` | `ferry_alert.py` |
| `02_ferry-forecast/ferry_alert.yml` | `.github/workflows/ferry_alert.yml` |
| `02_ferry-forecast/forecast_publisher.py` | `forecast_publisher.py` |
| `02_ferry-forecast/operation_logger.py` | `operation_logger.py` |

---

## 3. 設計思想

### 3-1. データの二層構造

| レイヤー | 内容 | シート | 頻度 |
|---|---|---|---|
| **予報ログ** | モデルが出力した欠航% | `daily_forecast` | 8:15 + 13:00（1日最大4行） |
| **実績ログ** | 当日の実際の運航結果 | `daily_operation_log` | 8:15のみ・1日1行（重複排除） |

予報ログと実績ログを日付で突合することで、**モデル精度（キャリブレーション）を将来的に検証できる**。

### 3-2. スコアリング → 確率変換の設計方針

欠航リスクを「スコア（0〜1）→ 欠航確率（%）」の2段階で計算する。

- **スコア** = 気象指標の加重平均（解釈しやすい）
- **確率** = ロジスティック関数でスコアを変換（非線形・上下限あり）

この2段階設計により、将来的にスコア計算式を変えずに変換カーブだけ調整できる。

### 3-3. 将来的なML移行パス

```
現在（Phase 1）: ルールベース（スコアリング）
     ↓ データが300件程度蓄積されたら
Phase 2: ロジスティック回帰（scikit-learn）
     ↓ さらに精度向上が必要なら
Phase 3: ランダムフォレスト / XGBoost
```

`daily_operation_log` の蓄積データがそのままトレーニングデータになる。

---

## 4. 欠航リスク スコアリング式

### 4-1. スコア計算（`ferry_alert.py: calc_cancellation_score()`）

```
score = wave_score × 0.35
      + swell_score × 0.30
      + wind_score  × 0.20
      + warning_score × 0.15
```

各サブスコアの正規化：

| 指標 | 0点 | 1点 |
|---|---|---|
| 波高（wave_height） | 0 m | 5 m以上 |
| うねり（swell_wave_height） | 0 m | 4 m以上 |
| 風速（wind_speed） | 0 m/s | 20 m/s以上 |
| 注意報（has_warning） | なし | あり |

### 4-2. スコア → 欠航確率（ロジスティック関数）

```python
pct = 100 / (1 + exp(-steepness × (score - inflection)))
```

| 対象 | 変曲点 (inflection) | 急峻さ (steepness) | 根拠 |
|---|---|---|---|
| 高速船 | 0.352 | 28.18 | 実績165日のロジスティック回帰最適値 |
| フェリー | 0.43 | 30.0 | 実績最適 inflection=0.431。steepness は最適値72が過適合のため30にcap |

> **2026-06 再キャリブレーション**：旧値（高速船 0.42/14、フェリー 0.52/12、設計当初は
> フェリー0.58）は感覚値ベースだったが、過去165日（2024-12〜2026-06）の実績照合で
> 中域の一貫した過小評価が判明（高速船: 予測60-69%→実欠航率100%、フェリー: 予測30-39%→実91%）。
> ロジスティック回帰で再導出した。フェリーは score≈0.40 を境にほぼ0%↔100%の急峻な
> 階段状で、最適 steepness=72 は欠航16サンプルへの過適合のため 30 にcapして頑健性を確保。
> ※ 渡嘉敷（tokashiki）は運航判断基準が異なる（座間味より欠航しにくい）ため別パラメータ。
> ※ この再キャリブレーションは欠航理由が weather のもののみ対象（dock/equipment は除外）。

感覚値との対応（高速船・新パラメータ）：

| score | 欠航% | 実績欠航率 |
|---|---|---|
| 0.25 | 13% | ≈9% |
| 0.30 | 19% | 32% |
| 0.35 | 51% | 境界 |
| 0.40 | 79% | 95% |
| 0.45 | 94% | 100% |

### 4-3. 欠航リスク判定閾値

| 閾値 | 意味 |
|---|---|
| `THRESHOLD_HIGHSPEED = 3.0 m` | 波高が3m以上で高速船リスク判定 |
| `THRESHOLD_FERRY = 4.0 m` | 波高が4m以上でフェリーリスク判定 |
| `SCORE_HIGHSPEED_RISK = 0.45` | スコアが0.45以上で高速船リスクフラグ |
| `SCORE_FERRY_RISK = 0.65` | スコアが0.65以上でフェリーリスクフラグ |

---

## 5. データスキーマ（Google Sheets）

### 5-1. daily_operation_log（実績ログ・ML用メインDB）

1日1行・8:15のみ記録（重複チェックあり）。

| カラム名 | 型 | 説明 |
|---|---|---|
| `date` | YYYY-MM-DD | 日付（主キー） |
| `recorded_at` | YYYY-MM-DD HH:MM | 記録日時 |
| `weather_desc` | str | 天気記述（「くもり後雨」等） |
| `announcement_text` | str | 運航情報の原文（最大500文字） |
| `ferry_operated` | 0/1 | フェリーざまみ運航フラグ |
| `ferry_turnaround` | 0/1 | 折り返し運転フラグ |
| `ferry_cancel_reason` | str | weather / equipment / dock / none |
| `hs_bin1_operated` | 0/1 | クィーンざまみ 1便運航フラグ |
| `hs_bin2_operated` | 0/1 | クィーンざまみ 2便運航フラグ |
| `hs_bin3_operated` | 0/1 | クィーンざまみ 3便運航フラグ |
| `hs_cancel_reason` | str | weather / equipment / dock / none |
| `ferry_weather_cancel` | 0/1 | 天候理由での欠航（ML目的変数） |
| `hs_am_weather_cancel` | 0/1 | 高速船AM便の天候欠航 |
| `hs_pm_weather_cancel` | 0/1 | 高速船PM便の天候欠航 |
| `wave_am` | float | 午前最大波高（m） |
| `wave_pm` | float | 午後最大波高（m） |
| `wave_max` | float | 当日最大波高（m） |
| `swell_height` | float | うねり高さ（m） |
| `swell_period` | float | うねり周期（秒）※未取得 |
| `swell_direction` | float | うねり方向（度）※未取得 |
| `wind_speed_am` | float | 午前最大風速（m/s） |
| `wind_speed_pm` | float | 午後最大風速（m/s） |
| `wind_speed_max` | float | 当日最大風速（m/s） |
| `wind_direction_am` | float | 午前卓越風向（度）※未取得 |
| `precip_am` | float | 午前降水量（mm）※未取得 |
| `precip_total` | float | 合計降水量（mm）※未取得 |
| `jma_wave_today` | str | 気象庁波高テキスト（当日） |
| `jma_wave_tomorrow` | str | 気象庁波高テキスト（翌日） |
| `jma_warning_today` | str | 早期注意情報レベル（当日） |
| `jma_warning_tomorrow` | str | 早期注意情報レベル（翌日） |
| `typhoon_active` | 0/1 | 台風発生中フラグ |
| `typhoon_distance_km` | float | 座間味〜台風中心距離（km） |
| `typhoon_category` | str | 台風強さ区分 |
| `typhoon_max_wind` | float | 台風最大風速（m/s） |
| `consec_ferry_cancel_days` | int | 直前の連続天候欠航日数 |
| `consec_hs_cancel_days` | int | 直前の連続天候欠航日数（高速船） |
| `next3d_wave_avg` | float | 翌3日間の平均波高予報（m） |
| `next3d_bad_days` | int | 翌3日間で波高2.5m超の日数 |
| `month` | int | 月（1〜12） |
| `is_typhoon_season` | 0/1 | 台風シーズン（6〜10月） |
| `model_highspeed_pct` | int | モデルの高速船欠航予測%（翌日） |
| `model_ferry_pct` | int | モデルのフェリー欠航予測%（翌日） |

> **空欄について**: 未取得カラムは空欄のまま（NaN）が正しい。"n/a"文字列は入れない。pandasで読んだときにNaNとして扱われるため、ML前処理が容易。

### 5-2. daily_forecast（予報ログ・精度検証用）

`forecast_publisher.py` の `save_to_sheets()` が自動作成・記録。1日最大4行（8:15・13:00 × 明日・明後日）。

| カラム名 | 説明 |
|---|---|
| `recorded_at` | 記録日時 |
| `target_date` | 予測対象日 |
| `wave_height_max` | 最大波高（m） |
| `swell_height_max` | 最大うねり（m） |
| `wind_speed_max` | 最大風速（m/s） |
| `jma_wave_text` | 気象庁波高テキスト |
| `jma_prob_level` | 早期注意情報レベル |
| `cancellation_score` | 欠航スコア |
| `predicted_pct_highspeed` | 高速船欠航予測% |
| `predicted_pct_ferry` | フェリー欠航予測% |

### 5-3. model_calibration（精度分析用・手動記録）

波高レンジ別の的中率・誤差を記録する将来用シート。

---

## 6. 座間味村HP スクレイピング設計

### 取得URL
```
https://www.vill.zamami.okinawa.jp/  （トップページ）
```
※ `/info/ship.html` は404。運航情報はトップページに掲載。

### HTML構造（2026年5月確認）

```html
<section class="un_homeFerryInfo">
  <div class="un_homeFerryInfo_head">
    <!-- 当日の概況テキスト（announcement_text・weather_descの取得元） -->
    5月10日（日）くもり後雨　高速船クイーンざまみの...フェリーざまみ３はエンジン機器トラブルの為、欠航となります。
  </div>

  <!-- 船ごとのセクション（フェリーざまみ / クィーンざまみ / みつしま） -->
  <div class="un_homeFerryInfo_route">
    <h3 class="un_homeFerryInfo_heading02">フェリーざまみ3</h3>
    <div class="bl_routeSchedule">
      <!-- 欠航の場合: bl_routeSchedule_suspension が存在 -->
      <div class="bl_routeSchedule_suspension">欠航</div>
    </div>
  </div>

  <div class="un_homeFerryInfo_route un_homeFerryInfo_route__02">
    <h3 class="un_homeFerryInfo_heading02">クィーンざまみ</h3>
    <div class="bl_routeSchedule">
      <!-- 運航の場合: bl_routeSchedule_row が便ごとに存在 -->
      <div class="bl_routeSchedule_row js_accordionItem">
        <div class="bl_routeSchedule_num">第1便</div>
        <div class="bl_routeSchedule_content">09:00発 泊港...</div>
      </div>
      <div class="bl_routeSchedule_row js_accordionItem">
        <div class="bl_routeSchedule_num">第2便</div>
        ...
      </div>
    </div>
  </div>
</section>
```

### 判定ロジック

| 判定対象 | 方法 |
|---|---|
| フェリー欠航 | `div.bl_routeSchedule_suspension` が存在するか |
| 高速船全便欠航 | 同上 |
| 高速船便別運航 | `div.bl_routeSchedule_row` の `div.bl_routeSchedule_num` に何便があるか |
| 欠航理由 | `div.un_homeFerryInfo_head` のテキストに機器系キーワードがあれば `equipment`、ドックなら `dock`、それ以外 `weather` |
| weather_desc | `un_homeFerryInfo_head` テキストから正規表現 `(晴れ?|くもり|雨|霧|台風)[^\s。、]{0,10}` |

---

## 7. 外部API・データソース

| ソース | URL | 用途 |
|---|---|---|
| Open-Meteo Marine API | `marine-api.open-meteo.com/v1/marine` | 波高・うねり・風波（8日分・時間別） |
| Open-Meteo Weather API | `api.open-meteo.com/v1/forecast` | 風速・降水量（8日分・時間別） |
| 気象庁 forecast | `jma.go.jp/bosai/forecast/data/forecast/471000.json` | 波高テキスト予報（今日〜明後日） |
| 気象庁 probability | `jma.go.jp/bosai/probability/data/probability/471000.json` | 波浪早期注意情報（警報級確率） |
| 気象庁 warning | `jma.go.jp/bosai/warning/data/warning/471000.json` | 注意報・警報 |
| 気象庁 typhoon | `jma.go.jp/bosai/typhoon/data/list.json` | 台風一覧 |
| 座間味村 HP | `vill.zamami.okinawa.jp/` | 当日の実際の運航情報 |

### 気象庁コード
- エリアコード `471000` = 沖縄県
- エリアコード `471010` = 沖縄本島南部（早期注意情報用）
- 座間味島の座標: `LAT=26.23, LON=127.30`

---

## 8. 環境変数・シークレット

| 変数名 | 用途 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API（メッセージ生成） |
| `SLACK_WEBHOOK_URL` | Slack通知 |
| `INSTAGRAM_ACCESS_TOKEN` | Instagram Graph API |
| `INSTAGRAM_USER_ID` | Instagram ユーザーID |
| `GITHUB_TOKEN` | 画像をGitHub Pagesに上げるため |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Sheets書き込み用サービスアカウント |
| `GOOGLE_SHEETS_ID` | スプレッドシートID |

---

## 9. GitHub Actions スケジュール

```yaml
on:
  schedule:
    - cron: '15 23 * * *'   # 8:15 JST（UTC+9）
    - cron: '0 4 * * *'     # 13:00 JST
  workflow_dispatch:          # 手動実行
```

**8:15 実行**: Slack通知 + DB実績記録（`daily_operation_log`） + Instagram投稿  
**13:00 実行**: Slack通知 + Instagram投稿のみ（DB記録は重複排除でスキップ）

---

## 10. 画像投稿（Instagram）設計

4枚カルーセル投稿：

| 画像 | 内容 | クラス |
|---|---|---|
| ① Header | 島名・サービス名・日付 | `make_image_header()` |
| ② Short-term | 明日・明後日の欠航% | `make_image_short()` |
| ③ Long-term | 3〜7日先の棒グラフ | `make_image_longterm()` |
| ④ Weather data | 気象庁予報・数値データ | `make_image_weather_data()` |

画像はいったんGitHub Pages（`images/`ディレクトリ）に上げてからInstagram APIに渡す。Pages build完了まで90秒待機後にAPIコール。

---

## 11. 他離島への展開時の変更箇所

同じ仕組みを別の離島に適用する場合に変更が必要な箇所：

### 必須変更

| 箇所 | 変数/関数 | 内容 |
|---|---|---|
| `ferry_alert.py` | `LAT, LON` | 離島の座標 |
| `ferry_alert.py` | `ZAMAMI_URL` | 現地自治体の運航情報URL |
| `operation_logger.py` | `ZAMAMI_LAT, ZAMAMI_LON` | 座標（台風距離計算用） |
| `operation_logger.py` | `get_zamami_operation_status()` | HPのHTML構造に合わせてスクレイピングを書き直す |
| `ferry_alert.py` | `get_ferry_status_from_web()` | 同上 |

### 検討が必要な変更

| 項目 | 考慮事項 |
|---|---|
| スコアリング重み | 離島ごとに欠航しやすい気象条件が異なる場合がある（例：波照間は南向きのうねりに強い、与那国は西風が強い） |
| 閾値 | 船のサイズ・航路の長さによって変わる。フェリーなしで高速船のみの離島も多い |
| 気象庁コード | 地域コードを確認（例：先島諸島は別コード） |
| HP構造 | HTML構造は自治体ごとに異なるため、毎回 `prettify()` で確認してから設計 |
| 便数 | 1日3便固定ではない離島もある（便数をスキーマで柔軟にするか検討） |

### スキーマ拡張の方針

離島ごとにスプレッドシートを分けるか、`island` カラムを追加して1つのDBに統合するかは運用規模次第。  
**当面は離島ごとに別リポジトリ・別スプレッドシートを推奨**（コード変更が頻繁なため）。

---

## 12. 既知の課題・TODO

| 優先度 | 課題 | 備考 |
|---|---|---|
| Medium | `swell_period`, `swell_direction`, `wind_direction_am`, `precip_*` が未取得 | Open-Meteo のレスポンスに含まれているので追加可能 |
| Medium | 気象庁エリアコード `471010` は沖縄本島南部（座間味専用ではない） | より近いエリアコードがあれば変更 |
| Low | モデル出力（`model_highspeed_pct` / `model_ferry_pct`）が翌日分のみ | 明後日分も記録するか検討 |
| Low | Instagramキャプション生成にClaude API呼び出し（1回/日）がかかる | エラー時はフォールバックキャプションを使用 |
| Future | データ300件蓄積後にロジスティック回帰モデルを実装 | `daily_operation_log` の `ferry_weather_cancel` が目的変数 |
