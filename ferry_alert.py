"""
Kucha Ferry Alert System
座間味島フェリー運航状況モニター＆ゲストメッセージ自動生成

動作フロー:
1. Open-Meteo Marine APIから波高データ取得（座間味島周辺）
2. 時間帯別（午前/午後）に波高を判定
3. 座間味村HPから運航情報をスクレイピング（8時以降）
4. Claude APIで状況に応じたゲスト向けメッセージ生成
5. LINE Notifyで自分のスマホに送信 → 確認後コピペ送信
"""

import os
import json
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 設定
# ============================================================

# 座間味島の座標
LAT = 26.23
LON = 127.30

# 波高の基準値（メートル）
THRESHOLD_HIGHSPEED = 3.0   # 高速船欠航の可能性
THRESHOLD_FERRY = 4.0       # フェリー欠航の可能性

# 座間味村フェリー運航情報ページ
ZAMAMI_URL = "https://www.vill.zamami.okinawa.jp/info/ship.html"

# ============================================================
# 1. 波データ取得
# ============================================================

def get_wave_data():
    """
    Open-Meteo Marine APIから座間味島の波高データを取得。
    今日・明日の時間別データを返す。
    """
    url = "https://marine-api.open-meteo.com/v1/marine"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "wave_height,wind_wave_height,swell_wave_height",
        "timezone": "Asia/Tokyo",
        "forecast_days": 2
    }
    
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    
    hourly = data["hourly"]
    times = hourly["time"]
    waves = hourly["wave_height"]
    
    # 今日の日付でフィルタ
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    today_data = []
    tomorrow_data = []
    
    for t, w in zip(times, waves):
        if w is None:
            continue
        if t.startswith(today):
            hour = int(t.split("T")[1].split(":")[0])
            today_data.append({"hour": hour, "wave_height": w, "time": t})
        elif t.startswith(tomorrow):
            hour = int(t.split("T")[1].split(":")[0])
            tomorrow_data.append({"hour": hour, "wave_height": w, "time": t})
    
    return today_data, tomorrow_data


def analyze_waves(wave_data):
    """
    時間帯別（午前/午後）の波高を分析して判定結果を返す。
    午前 = 6〜12時, 午後 = 12〜18時
    """
    morning = [d for d in wave_data if 6 <= d["hour"] < 12]
    afternoon = [d for d in wave_data if 12 <= d["hour"] < 18]
    all_day = [d for d in wave_data if 6 <= d["hour"] < 18]
    
    def summarize(period):
        if not period:
            return {"max": None, "avg": None, "risk_highspeed": False, "risk_ferry": False}
        max_wave = max(d["wave_height"] for d in period)
        avg_wave = sum(d["wave_height"] for d in period) / len(period)
        return {
            "max": round(max_wave, 1),
            "avg": round(avg_wave, 1),
            "risk_highspeed": max_wave >= THRESHOLD_HIGHSPEED,
            "risk_ferry": max_wave >= THRESHOLD_FERRY,
        }
    
    return {
        "morning": summarize(morning),
        "afternoon": summarize(afternoon),
        "all_day": summarize(all_day),
        "raw": wave_data
    }


# ============================================================
# 2. 座間味村HP 運航情報スクレイピング
# ============================================================

def get_ferry_status_from_web():
    """
    座間味村HPから当日の運航情報を取得。
    8時以降に更新されることが多い。
    情報が見つからない場合はNoneを返す。
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(ZAMAMI_URL, headers=headers, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        
        # ページ内のテキストから運航情報を探す
        # ※サイト構造が変わった場合はここを調整
        text_content = soup.get_text(separator="\n", strip=True)
        
        # 今日の日付が含まれる部分を探す
        today = datetime.now()
        date_patterns = [
            today.strftime("%Y年%m月%d日"),
            today.strftime("%m月%d日"),
            today.strftime("%-m月%-d日"),
        ]
        
        lines = text_content.split("\n")
        relevant_lines = []
        capture = False
        
        for line in lines:
            if any(p in line for p in date_patterns):
                capture = True
            if capture and line.strip():
                relevant_lines.append(line.strip())
            if capture and len(relevant_lines) > 20:
                break
        
        if relevant_lines:
            return "\n".join(relevant_lines)
        
        # 「欠航」「運休」「通常運航」などのキーワードを探す
        keywords = ["欠航", "運休", "通常運航", "条件付き", "フェリー", "高速船"]
        keyword_lines = [l for l in lines if any(k in l for k in keywords)]
        
        if keyword_lines:
            return "\n".join(keyword_lines[:10])
        
        return None
        
    except Exception as e:
        print(f"[警告] 座間味村HP取得エラー: {e}")
        return None


# ============================================================
# 3. Claude APIでメッセージ生成
# ============================================================

def generate_guest_message(wave_analysis, ferry_status_text, target_date="today"):
    """
    波データ・運航情報をもとにClaude APIでゲスト向けメッセージを生成。
    """
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    
    date_label = "本日" if target_date == "today" else "明日"
    wave = wave_analysis["all_day"]
    morning = wave_analysis["morning"]
    afternoon = wave_analysis["afternoon"]
    
    # 状況サマリーを構築
    situation = f"""
【波高データ（{date_label}）】
- 終日最大: {wave['max']}m / 平均: {wave['avg']}m
- 午前（6〜12時）最大: {morning['max']}m
- 午後（12〜18時）最大: {afternoon['max']}m

【判定基準】
- 高速船欠航リスク: 波高{THRESHOLD_HIGHSPEED}m以上
- フェリー欠航リスク: 波高{THRESHOLD_FERRY}m以上

【現在の判定】
- 高速船リスク（午前）: {'あり ⚠️' if morning['risk_highspeed'] else 'なし ✅'}
- 高速船リスク（午後）: {'あり ⚠️' if afternoon['risk_highspeed'] else 'なし ✅'}
- フェリーリスク（終日）: {'あり ⚠️' if wave['risk_ferry'] else 'なし ✅'}

【座間味村HP運航情報】
{ferry_status_text if ferry_status_text else "（8時以前のため未更新、または情報なし）"}
"""
    
    prompt = f"""
あなたはHomestay Kucha（沖縄・座間味島の民宿）のスタッフです。
以下の情報をもとに、チェックイン前後のゲスト（欧米系・英語話者）向けのメッセージを生成してください。

{situation}

以下の3パターンのメッセージを生成してください：

【パターンA】運航情報未確定（8時前・波に懸念あり）
- 波予報から欠航の可能性を事前に伝える
- 8時に公式発表があることを案内
- 不安を煽らず、でも正直に

【パターンB】公式発表後（欠航または条件付き運航あり）
- 実際の運航状況を伝える
- 代替手段・次の便の案内
- Kuchaとして何かサポートできることを添える

【パターンC】問題なし（通常運航確認済み）
- 短く安心感を伝える
- 明るいトーンで

各パターンについて：
- 英語メッセージ（Airbnb/Booking.com送信用）
- 日本語メッセージ（参考用）
を生成してください。

トーン：honest / warm / practical（Kuchaのブランドイメージ通り）
長さ：英語で3〜5文程度
"""
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    return response.content[0].text


# ============================================================
# 4. LINE Notifyで自分に送信
# ============================================================

def send_slack_notify(message):
    """
    SlackのIncoming Webhookで通知を送る。
    """
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    print(f"[デバッグ] SLACK_WEBHOOK_URL取得: {'あり' if webhook_url else 'なし'}")
    if not webhook_url:
        print("[Slack通知スキップ] SLACK_WEBHOOK_URLが未設定です")
        return False
    
    payload = {"text": message}
    
    resp = requests.post(
        webhook_url,
        json=payload,
        timeout=10
    )
    
    return resp.status_code == 200


# ============================================================
# 5. メイン処理
# ============================================================

def run_ferry_check():
    """
    メイン処理。毎朝7時と8時以降に実行する想定。
    """
    now = datetime.now()
    print(f"\n{'='*50}")
    print(f"Kucha Ferry Alert Check: {now.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)
    
    # 1. 波データ取得・分析
    print("\n[1] 波データ取得中...")
    try:
        today_waves, tomorrow_waves = get_wave_data()
        today_analysis = analyze_waves(today_waves)
        tomorrow_analysis = analyze_waves(tomorrow_waves)
        
        print(f"  本日最大波高: {today_analysis['all_day']['max']}m")
        print(f"  明日最大波高: {tomorrow_analysis['all_day']['max']}m")
    except Exception as e:
        print(f"  [エラー] 波データ取得失敗: {e}")
        return
    
    # リスクがなければ終了（通常運航の場合はパターンCを送るかどうか選択可）
    has_risk = (
        today_analysis["all_day"]["risk_highspeed"] or
        today_analysis["all_day"]["risk_ferry"] or
        tomorrow_analysis["all_day"]["risk_highspeed"]
    )
    
    if not has_risk:
        print("\n✅ 波高に問題なし。通常運航の見込みです。")
        send_slack_notify("✅ 座間味フェリー：本日波高問題なし。通常運航の見込み。")
        return
    
    # 2. 座間味村HP 運航情報確認
    print("\n[2] 座間味村HP 運航情報確認中...")
    ferry_status = get_ferry_status_from_web()
    if ferry_status:
        print(f"  取得成功:\n  {ferry_status[:100]}...")
    else:
        print("  情報なし（8時前または更新なし）")
    
    # 3. Claude APIでメッセージ生成
    print("\n[3] ゲストメッセージ生成中...")
    try:
        message = generate_guest_message(today_analysis, ferry_status)
        print("\n--- 生成されたメッセージ ---")
        print(message)
    except Exception as e:
        print(f"  [エラー] メッセージ生成失敗: {e}")
        return
    
    # 4. LINE通知
    print("\n[4] LINE通知送信中...")
    
    # 波高サマリーをヘッダーに追加
    alert_header = f"""
⚠️ Kucha フェリーアラート {now.strftime('%m/%d')}

【波高】本日最大 {today_analysis['all_day']['max']}m
  午前: {today_analysis['morning']['max']}m {'⚠️高速船' if today_analysis['morning']['risk_highspeed'] else '✅'}
  午後: {today_analysis['afternoon']['max']}m {'⚠️高速船' if today_analysis['afternoon']['risk_highspeed'] else '✅'}
  フェリー: {'⚠️要注意' if today_analysis['all_day']['risk_ferry'] else '✅'}

【運航情報】{ferry_status[:80] if ferry_status else '未確認（8時以降に再確認）'}

--- メッセージ案 ---
"""
    
    full_notification = alert_header + message[:1000]  # LINEの文字数制限対応
    
    success = send_slack_notify(full_notification)
    if success:
        print("  ✅ Slack通知送信成功")
    else:
        print("  [スキップ] Slack未設定のため標準出力のみ")
    print("\n処理完了。メッセージを確認してゲストに送信してください。")


if __name__ == "__main__":
    run_ferry_check()
