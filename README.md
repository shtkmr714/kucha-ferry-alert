# Kucha Ferry Alert System

座間味島の波予報を毎朝自動チェックし、フェリー欠航リスクがある場合に
ゲスト向けメッセージ案をLINEに送信するシステムです。

## 動作フロー

```
毎朝7:00 JST
  → 波高データ取得（Open-Meteo Marine API）
  → 午前/午後別に欠航リスク判定
  → リスクあり → Claude APIでメッセージ生成
  → LINEに通知（メッセージ案3パターン）

毎朝8:15 JST（座間味村HP更新後）
  → 同じ処理 + 座間味村HPの運航情報を取得
  → より正確な情報でメッセージ更新
```

## セットアップ（30分でできます）

### Step 1: GitHubリポジトリ作成

1. https://github.com/new でリポジトリ作成（名前例：`kucha-ferry-alert`）
2. このフォルダのファイルをすべてアップロード

### Step 2: APIキーを取得

**Anthropic API Key**
- https://console.anthropic.com/ にログイン
- API Keys → Create Key
- 月数十円〜数百円程度

**LINE Notify Token**
- https://notify-bot.line.me/ にLINEアカウントでログイン
- 「マイページ」→「トークンを発行する」
- 通知先：「1:1でLINE Notifyから通知を受け取る」
- 無料

### Step 3: GitHubにシークレット設定

GitHubリポジトリの Settings → Secrets and variables → Actions → New repository secret

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | AnthropicのAPIキー |
| `LINE_NOTIFY_TOKEN` | LINE NotifyのToken |

### Step 4: GitHub Actionsを有効化

リポジトリの「Actions」タブ → ワークフローを有効化

これで毎朝7時と8時15分に自動実行されます。

---

## ローカルでのテスト実行

```bash
# 依存パッケージインストール
pip install requests anthropic beautifulsoup4 lxml python-dotenv

# .envファイルを作成
cp .env.example .env
# .envを編集してAPIキーを設定

# 実行
python ferry_alert.py
```

---

## カスタマイズ

`ferry_alert.py` の設定部分を変更できます：

```python
# 波高の基準値
THRESHOLD_HIGHSPEED = 3.0   # 高速船欠航の可能性（メートル）
THRESHOLD_FERRY = 4.0       # フェリー欠航の可能性（メートル）
```

---

## 通知が来たら

LINEに以下が届きます：
- 波高サマリー（午前/午後別）
- 座間味村HPの運航情報（8時以降）
- ゲスト向けメッセージ案3パターン（英語＋日本語）

→ 内容を確認してAirbnb/Booking.comのメッセージ画面にコピペして送信

---

## コスト

| サービス | 費用 |
|---|---|
| GitHub Actions | 無料（月2000分まで） |
| Open-Meteo Marine API | 無料 |
| Claude API | 約1〜5円/回 × 60回/月 ≒ 月60〜300円 |
| LINE Notify | 無料 |
| **合計** | **月300円以下** |
