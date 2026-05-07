"""
Kucha Ferry Alert System v2
座間味島フェリー運航状況モニター＆ゲストメッセージ自動生成

改良点：
- メトリクス拡張（うねり・風速・風向・波周期・注意報）
- 欠航スコアリングモデル導入
- 長期予報（7日間）追加
- 台風情報取得
- Slack通知を短期・長期で分離
"""

import os
import json
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from anthropic import Anthropic

# ============================================================
# 設定
# ============================================================

LAT = 26.23
LON = 127.30

# 波高閾値
THRESHOLD_HIGHSPEED = 3.0
THRESHOLD_FERRY = 4.0

# 長期予報の警戒閾値（3日以降）
LONGTERM_WARNING_WAVE = 2.5      # 注意喚起開始
LONGTERM_WARNING_HIGHSPEED = 3.0 # 高速船警戒
LONGTERM_WARNING_FERRY = 4.0     # フェリー警戒

# 欠航スコアリングの重み
SCORE_WEIGHTS = {
    "wave_height": 0.35,
    "swell_wave_height": 0.30,
    "wind_speed": 0.20,
    "warning": 0.15,
}

# 欠航スコア閾値
SCORE_HIGHSPEED_RISK = 0.45
SCORE_FERRY_RISK = 0.65

ZAMAMI_URL = "https://www.vill.zamami.okinawa.jp/info/ship.html"

# ============================================================
# 1. データ取得
# ============================================================

def get_marine_and_weather_data():
    """
    Open-Meteo Marine API + Weather APIから総合データを取得。
    16日分取得し、短期（2日）・長期（3〜7日）に分けて返す。
    """

    # 海洋データ（波）
    marine_url = "https://marine-api.open-meteo.com/v1/marine"
    marine_params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": [
            "wave_height",
            "wave_period",
            "wave_direction",
            "wind_wave_height",
            "swell_wave_height",
            "swell_wave_period",
        ],
        "timezone": "Asia/Tokyo",
        "forecast_days": 8,
    }

    # 気象データ（風・視程）
    weather_url = "https://api.open-meteo.com/v1/forecast"
    weather_params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": [
            "wind_speed_10m",
            "wind_direction_10m",
            "visibility",
            "precipitation",
        ],
        "timezone": "Asia/Tokyo",
        "forecast_days": 8,
    }

    marine_resp = requests.get(marine_url, params=marine_params, timeout=15)
    marine_resp.raise_for_status()
    marine_data = marine_resp.json()

    weather_resp = requests.get(weather_url, params=weather_params, timeout=15)
    weather_resp.raise_for_status()
    weather_data = weather_resp.json()

    # データ統合
    times = marine_data["hourly"]["time"]
    combined = []

    for i, t in enumerate(times):
        entry = {
            "time": t,
            "date": t.split("T")[0],
            "hour": int(t.split("T")[1].split(":")[0]),
            "wave_height": marine_data["hourly"]["wave_height"][i],
            "wave_period": marine_data["hourly"]["wave_period"][i],
            "wave_direction": marine_data["hourly"]["wave_direction"][i],
            "wind_wave_height": marine_data["hourly"]["wind_wave_height"][i],
            "swell_wave_height": marine_data["hourly"]["swell_wave_height"][i],
            "swell_wave_period": marine_data["hourly"]["swell_wave_period"][i],
            "wind_speed": weather_data["hourly"]["wind_speed_10m"][i],
            "wind_direction": weather_data["hourly"]["wind_direction_10m"][i],
            "visibility": weather_data["hourly"]["visibility"][i],
            "precipitation": weather_data["hourly"]["precipitation"][i],
        }
        combined.append(entry)

    return combined


def get_jma_warnings():
    """
    気象庁APIから沖縄本島地方の警報・注意報を取得。
    波浪注意報・強風注意報・暴風警報などを返す。
    """
    try:
        # 沖縄県の警報情報（先島諸島含む）
        url = "https://www.jma.go.jp/bosai/warning/data/warning/471000.json"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        warnings = []
        # 警報・注意報のテキストを抽出
        if "areaTypes" in data:
            for area_type in data["areaTypes"]:
                for area in area_type.get("areas", []):
                    area_name = area.get("name", "")
                    if "座間味" in area_name or "慶良間" in area_name or "沖縄本島" in area_name:
                        for warning in area.get("warnings", []):
                            if warning.get("status") in ["発表", "継続"]:
                                warnings.append({
                                    "area": area_name,
                                    "type": warning.get("code", ""),
                                    "status": warning.get("status", ""),
                                })
        return warnings

    except Exception as e:
        print(f"[警告] 気象庁API取得エラー: {e}")
        return []


def get_ferry_status_from_web():
    """座間味村HPから運航情報を取得。"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(ZAMAMI_URL, headers=headers, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        text_content = soup.get_text(separator="\n", strip=True)

        today = datetime.now()
        date_patterns = [
            today.strftime("%Y年%m月%d日"),
            today.strftime("%m月%d日"),
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

        keywords = ["欠航", "運休", "通常運航", "条件付き", "フェリー", "高速船"]
        keyword_lines = [l for l in lines if any(k in l for k in keywords)]
        if keyword_lines:
            return "\n".join(keyword_lines[:10])

        return None

    except Exception as e:
        print(f"[警告] 座間味村HP取得エラー: {e}")
        return None


# ============================================================
# 2. 欠航スコアリング
# ============================================================

def calc_cancellation_score(wave_h, swell_h, wind_spd, has_warning):
    """
    各メトリクスを0〜1に正規化して欠航スコアを算出。
    スコアが高いほど欠航リスクが高い。
    """
    # 波高スコア（0m=0, 5m以上=1）
    wave_score = min(wave_h / 5.0, 1.0) if wave_h else 0

    # うねりスコア（0m=0, 4m以上=1）
    swell_score = min(swell_h / 4.0, 1.0) if swell_h else 0

    # 風速スコア（0m/s=0, 20m/s以上=1）
    wind_score = min(wind_spd / 20.0, 1.0) if wind_spd else 0

    # 注意報スコア
    warning_score = 1.0 if has_warning else 0.0

    total = (
        wave_score * SCORE_WEIGHTS["wave_height"] +
        swell_score * SCORE_WEIGHTS["swell_wave_height"] +
        wind_score * SCORE_WEIGHTS["wind_speed"] +
        warning_score * SCORE_WEIGHTS["warning"]
    )

    return round(total, 3)


def analyze_period(hourly_data, warnings):
    """
    指定時間帯のデータを分析してサマリーを返す。
    """
    if not hourly_data:
        return None

    valid = [d for d in hourly_data if d["wave_height"] is not None]
    if not valid:
        return None

    has_warning = len(warnings) > 0

    max_wave = max(d["wave_height"] for d in valid)
    max_swell = max(d["swell_wave_height"] for d in valid if d["swell_wave_height"])
    max_wind = max(d["wind_speed"] for d in valid if d["wind_speed"])
    avg_wave = sum(d["wave_height"] for d in valid) / len(valid)

    score = calc_cancellation_score(max_wave, max_swell or 0, max_wind or 0, has_warning)

    return {
        "max_wave": round(max_wave, 1),
        "avg_wave": round(avg_wave, 1),
        "max_swell": round(max_swell, 1) if max_swell else None,
        "max_wind": round(max_wind, 1) if max_wind else None,
        "has_warning": has_warning,
        "cancellation_score": score,
        "risk_highspeed": max_wave >= THRESHOLD_HIGHSPEED or score >= SCORE_HIGHSPEED_RISK,
        "risk_ferry": max_wave >= THRESHOLD_FERRY or score >= SCORE_FERRY_RISK,
    }


def analyze_all_data(combined_data, warnings):
    """
    全データを日付・時間帯別に分析。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    results = {}

    # 今日〜7日後まで分析
    for delta in range(8):
        date = (datetime.now() + timedelta(days=delta)).strftime("%Y-%m-%d")
        day_data = [d for d in combined_data if d["date"] == date and 6 <= d["hour"] < 20]
        morning = [d for d in day_data if 6 <= d["hour"] < 12]
        afternoon = [d for d in day_data if 12 <= d["hour"] < 18]

        results[date] = {
            "delta": delta,
            "all_day": analyze_period(day_data, warnings),
            "morning": analyze_period(morning, warnings),
            "afternoon": analyze_period(afternoon, warnings),
        }

    return results


# ============================================================
# 3. メッセージ生成
# ============================================================

def generate_shortterm_message(analysis, ferry_status, warnings):
    """短期予報メッセージ生成（今日・明日）"""
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    today_a = analysis.get(today, {})
    tomorrow_a = analysis.get(tomorrow, {})

    def fmt(a):
        if not a or not a.get("all_day"):
            return "データなし"
        d = a["all_day"]
        m = a.get("morning") or {}
        af = a.get("afternoon") or {}
        return f"""
  波高最大: {d['max_wave']}m / 平均: {d['avg_wave']}m
  うねり最大: {d.get('max_swell', 'N/A')}m
  風速最大: {d.get('max_wind', 'N/A')}m/s
  欠航スコア: {d['cancellation_score']} (高速船閾値:{SCORE_HIGHSPEED_RISK} / フェリー閾値:{SCORE_FERRY_RISK})
  高速船リスク 午前:{m.get('risk_highspeed','?')} / 午後:{af.get('risk_highspeed','?')}
  フェリーリスク: {d.get('risk_ferry','?')}
  注意報: {'あり' if d.get('has_warning') else 'なし'}"""

    situation = f"""
【本日】{fmt(today_a)}
【明日】{fmt(tomorrow_a)}
【気象庁注意報】{json.dumps(warnings, ensure_ascii=False) if warnings else 'なし'}
【座間味村HP運航情報】{ferry_status or '未確認'}
"""

    prompt = f"""
あなたはHomestay Kucha（沖縄・座間味島）のスタッフです。
以下の気象・波データをもとにゲスト向けメッセージを生成してください。

{situation}

以下3パターンを生成：

【パターンA】運航情報未確定（波に懸念あり・8時前）
【パターンB】欠航または条件付き運航確認済み
【パターンC】通常運航確認済み

各パターン：英語（Airbnb/Booking送信用）と日本語（参考）
トーン：honest / warm / practical
英語は3〜5文程度
"""

    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


def generate_longterm_message(analysis, warnings):
    """長期予報メッセージ生成（3〜7日先）"""
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # 3〜7日先のリスク日を特定
    risk_days = []
    for date, data in analysis.items():
        delta = data.get("delta", 0)
        if delta < 3:
            continue
        all_day = data.get("all_day")
        if not all_day:
            continue
        if all_day["max_wave"] >= LONGTERM_WARNING_WAVE or all_day["cancellation_score"] >= SCORE_HIGHSPEED_RISK:
            dt = datetime.strptime(date, "%Y-%m-%d")
            risk_days.append({
                "date": date,
                "date_label": dt.strftime("%m/%d(%a)"),
                "delta": delta,
                "max_wave": all_day["max_wave"],
                "max_swell": all_day.get("max_swell"),
                "max_wind": all_day.get("max_wind"),
                "score": all_day["cancellation_score"],
                "risk_highspeed": all_day["risk_highspeed"],
                "risk_ferry": all_day["risk_ferry"],
            })

    if not risk_days:
        return None

    situation = f"""
【懸念日一覧】
{json.dumps(risk_days, ensure_ascii=False, indent=2)}

【気象庁注意報】{json.dumps(warnings, ensure_ascii=False) if warnings else 'なし'}
"""

    prompt = f"""
あなたはHomestay Kucha（沖縄・座間味島）のスタッフです。
3〜7日先の天候が荒れる可能性があります。
その期間に滞在する複数ゲストに一斉送信できる汎用的な警戒メッセージを生成してください。

{situation}

要件：
- 特定の日付ではなく「〇月〇日〜〇日ごろ」という期間表現を使う
- 「その期間にご滞在の方へ」という書き出しで複数ゲストに自然に届く文面に
- 不安を煽りすぎない。あくまで「可能性がある」という表現で
- キャンセルも選択肢の一つとして伝える（強制はしない）
- 最新情報を都度お知らせすることも伝える
- 英語メッセージ（Airbnb/Booking送信用）
- 日本語メッセージ（参考）
- 英語は4〜6文程度
"""

    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text, risk_days


# ============================================================
# 4. Slack通知
# ============================================================

def send_slack(message, emoji="⚠️"):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("[Slack通知スキップ] SLACK_WEBHOOK_URLが未設定です")
        return False

    payload = {"text": f"{emoji} {message}"}
    resp = requests.post(webhook_url, json=payload, timeout=10)
    return resp.status_code == 200


# ============================================================
# 5. メイン処理
# ============================================================

def run_ferry_check():
    now = datetime.now()
    print(f"\n{'='*50}")
    print(f"Kucha Ferry Alert v2: {now.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    # 1. データ取得
    print("\n[1] データ取得中...")
    try:
        combined_data = get_marine_and_weather_data()
        print(f"  海洋・気象データ取得完了（{len(combined_data)}件）")
    except Exception as e:
        print(f"  [エラー] データ取得失敗: {e}")
        send_slack(f"❌ Kucha Ferry Alert: データ取得失敗\n{e}")
        return

    # 2. 注意報取得
    print("\n[2] 気象庁注意報確認中...")
    warnings = get_jma_warnings()
    print(f"  注意報: {len(warnings)}件")

    # 3. 分析
    print("\n[3] データ分析中...")
    analysis = analyze_all_data(combined_data, warnings)

    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    today_data = analysis.get(today, {}).get("all_day")
    tomorrow_data = analysis.get(tomorrow, {}).get("all_day")

    print(f"  本日: 波高{today_data['max_wave']}m / スコア{today_data['cancellation_score']}" if today_data else "  本日: データなし")
    print(f"  明日: 波高{tomorrow_data['max_wave']}m / スコア{tomorrow_data['cancellation_score']}" if tomorrow_data else "  明日: データなし")

    # ---- 短期アラート ----
    short_risk = (
        (today_data and (today_data["risk_highspeed"] or today_data["risk_ferry"])) or
        (tomorrow_data and (tomorrow_data["risk_highspeed"] or tomorrow_data["risk_ferry"])) or
        len(warnings) > 0
    )

    if short_risk:
        print("\n[4] 短期アラート: リスクあり → メッセージ生成中...")

        ferry_status = get_ferry_status_from_web()
        short_message = generate_shortterm_message(analysis, ferry_status, warnings)

        # Slackに送信
        header = f"""🚢 Kucha フェリーアラート {now.strftime('%m/%d')}

【本日】波{today_data['max_wave']}m / うねり{today_data.get('max_swell','?')}m / 風{today_data.get('max_wind','?')}m/s / スコア{today_data['cancellation_score']}
  高速船: {'⚠️リスクあり' if today_data['risk_highspeed'] else '✅'}  フェリー: {'⚠️リスクあり' if today_data['risk_ferry'] else '✅'}
【明日】波{tomorrow_data['max_wave'] if tomorrow_data else '?'}m / スコア{tomorrow_data['cancellation_score'] if tomorrow_data else '?'}
【注意報】{'あり ⚠️' if warnings else 'なし ✅'}
【運航情報】{ferry_status[:60] if ferry_status else '未確認（8時以降に再確認）'}

"""
        send_slack(header + short_message[:1500], emoji="")
        print("  ✅ 短期アラート送信完了")
    else:
        print("\n[4] 短期: 問題なし（通知スキップ）")
        send_slack(f"✅ {now.strftime('%m/%d')} 座間味フェリー：短期予報に問題なし（波高{today_data['max_wave'] if today_data else '?'}m）", emoji="")

    # ---- 長期アラート ----
    print("\n[5] 長期予報チェック中（3〜7日先）...")
    try:
        result = generate_longterm_message(analysis, warnings)
        if result:
            long_message, risk_days = result
            risk_summary = " / ".join([f"{d['date_label']}波{d['max_wave']}m" for d in risk_days])
            header = f"""🌊 Kucha 長期天候警戒 {now.strftime('%m/%d')}

【懸念日】{risk_summary}

"""
            send_slack(header + long_message[:1500], emoji="")
            print(f"  ⚠️ 長期アラート送信完了（懸念日: {len(risk_days)}日）")
        else:
            print("  ✅ 長期: 3〜7日先に懸念なし")
    except Exception as e:
        print(f"  [エラー] 長期予報生成失敗: {e}")

    print("\n処理完了。")


if __name__ == "__main__":
    run_ferry_check()
