"""
operation_logger.py
毎日8:15に実行。座間味村HPから運航情報を取得し、
気象データ・コンテキスト変数とともにGoogle Sheetsに記録する。

ferry_alert.py の run_ferry_check() から呼び出す。
"""

import os
import json
import math
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ZAMAMI_LAT = 26.23
ZAMAMI_LON = 127.30


# ============================================================
# 1. 座間味村HP スクレイピング
# ============================================================

def get_zamami_operation_status():
    """
    座間味村HP から本日の運航情報を取得する。
    戻り値:
    {
        "ferry_operated": 1 or 0,
        "ferry_turnaround": 1 or 0,
        "ferry_cancel_reason": "weather" / "equipment" / "dock" / "none",
        "hs_bin1_operated": 1 or 0,   # 1便
        "hs_bin2_operated": 1 or 0,   # 2便
        "hs_bin3_operated": 1 or 0,   # 3便
        "hs_cancel_reason": "weather" / "equipment" / "dock" / "none",
        "announcement_text": "原文テキスト",
        "weather_desc": "くもり後雨",
    }
    """
    result = {
        "ferry_operated": None,
        "ferry_turnaround": 0,
        "ferry_cancel_reason": "none",
        "hs_bin1_operated": None,
        "hs_bin2_operated": None,
        "hs_bin3_operated": None,
        "hs_cancel_reason": "none",
        "announcement_text": "",
        "weather_desc": "",
    }

    try:
        from bs4 import BeautifulSoup
        resp = requests.get("https://www.vill.zamami.okinawa.jp/", timeout=15)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")

        # 運航情報セクションを探す
        # 「本日の運航情報」のh3を起点に情報を取得
        full_text = ""
        operation_section = None

        # 運航情報テキストブロックを探す
        for h3 in soup.find_all("h3"):
            if "本日の運航情報" in h3.get_text():
                operation_section = h3.find_parent()
                break

        if operation_section:
            full_text = operation_section.get_text(separator=" ", strip=True)
        else:
            # フォールバック：ページ全体から運航情報を探す
            full_text = soup.get_text(separator=" ", strip=True)

        result["announcement_text"] = full_text[:500]  # 最大500文字

        # 天気記述を抽出（「くもり後雨」等）
        import re
        weather_match = re.search(r"(晴|くもり|雨|霧|台風)[^\s。]{0,20}", full_text)
        if weather_match:
            result["weather_desc"] = weather_match.group(0)

        # --- フェリーざまみ の状態判定 ---
        ferry_text = ""
        for h3 in soup.find_all("h3"):
            if "フェリーざまみ" in h3.get_text():
                # 直後のテキストを取得
                sibling = h3.find_next_sibling()
                if sibling:
                    ferry_text = sibling.get_text(strip=True)
                # さらに近隣のテキストも確認
                parent_text = h3.find_parent().get_text(separator=" ", strip=True) if h3.find_parent() else ""
                ferry_text = ferry_text or parent_text
                break

        if ferry_text:
            if "欠航" in ferry_text:
                result["ferry_operated"] = 0
                # 欠航理由の判定
                if any(w in ferry_text for w in ["機器", "エンジン", "トラブル", "故障", "点検", "整備", "ドック"]):
                    result["ferry_cancel_reason"] = "equipment"
                elif any(w in ferry_text for w in ["台風"]):
                    result["ferry_cancel_reason"] = "weather"  # 台風も天候扱い
                else:
                    result["ferry_cancel_reason"] = "weather"
            elif "折り返し" in ferry_text:
                result["ferry_operated"] = 1
                result["ferry_turnaround"] = 1
                result["ferry_cancel_reason"] = "none"
            else:
                result["ferry_operated"] = 1
                result["ferry_cancel_reason"] = "none"

        # --- クィーンざまみ（高速船）の状態判定 ---
        hs_text = ""
        for h3 in soup.find_all("h3"):
            if "クィーンざまみ" in h3.get_text() or "クイーンざまみ" in h3.get_text():
                parent = h3.find_parent()
                if parent:
                    hs_text = parent.get_text(separator=" ", strip=True)
                break

        if hs_text:
            if "欠航" in hs_text:
                # 全便欠航か便別欠航かを判定
                if "全便" in hs_text or ("1便" in hs_text and "2便" in hs_text):
                    result["hs_bin1_operated"] = 0
                    result["hs_bin2_operated"] = 0
                    result["hs_bin3_operated"] = 0
                elif "1便" in hs_text and "欠航" in hs_text:
                    result["hs_bin1_operated"] = 0
                    result["hs_bin2_operated"] = 1
                    result["hs_bin3_operated"] = 1
                elif any(w in hs_text for w in ["2便", "3便", "午後"]):
                    result["hs_bin1_operated"] = 1
                    result["hs_bin2_operated"] = 0
                    result["hs_bin3_operated"] = 0
                else:
                    # 欠航の記述はあるが便の特定ができない→全便欠航と見なす
                    result["hs_bin1_operated"] = 0
                    result["hs_bin2_operated"] = 0
                    result["hs_bin3_operated"] = 0

                # 欠航理由
                if any(w in hs_text for w in ["機器", "エンジン", "トラブル", "故障", "点検", "整備", "ドック"]):
                    result["hs_cancel_reason"] = "equipment"
                else:
                    result["hs_cancel_reason"] = "weather"

            elif "第1便" in hs_text or "1便" in hs_text:
                # 運航便の記述あり
                result["hs_bin1_operated"] = 1 if "第1便" in hs_text else 0
                result["hs_bin2_operated"] = 1 if "第2便" in hs_text else 0
                result["hs_bin3_operated"] = 1 if "第3便" in hs_text else 0
                result["hs_cancel_reason"] = "none"
            else:
                result["hs_bin1_operated"] = 1
                result["hs_bin2_operated"] = 1
                result["hs_bin3_operated"] = 1
                result["hs_cancel_reason"] = "none"

        print(f"  [HP] フェリー: {'運航' if result['ferry_operated'] else '欠航'}{'(折り返し)' if result['ferry_turnaround'] else ''}")
        print(f"  [HP] 高速船: 1便={result['hs_bin1_operated']} 2便={result['hs_bin2_operated']} 3便={result['hs_bin3_operated']}")

    except Exception as e:
        print(f"  [警告] 座間味HP取得エラー: {e}")

    return result


# ============================================================
# 2. 台風データ取得（JMA）
# ============================================================

def get_typhoon_data():
    """
    JMA台風情報APIから現在の台風情報を取得する。
    戻り値:
    {
        "typhoon_active": 0 or 1,
        "typhoon_distance_km": float or None,
        "typhoon_category": str or None,
        "typhoon_max_wind": float or None,
    }
    """
    result = {
        "typhoon_active": 0,
        "typhoon_distance_km": None,
        "typhoon_category": None,
        "typhoon_max_wind": None,
    }

    try:
        # JMA台風情報リスト
        list_url = "https://www.jma.go.jp/bosai/typhoon/data/list.json"
        resp = requests.get(list_url, timeout=10)
        if resp.status_code != 200:
            return result

        typhoon_list = resp.json()
        if not typhoon_list:
            return result  # 台風なし

        # 最も近い（最新の）台風の詳細を取得
        result["typhoon_active"] = 1
        latest = typhoon_list[0]  # 最新の台風

        # 台風詳細データ取得
        detail_url = f"https://www.jma.go.jp/bosai/typhoon/data/{latest.get('id', '')}/forecast.json"
        detail_resp = requests.get(detail_url, timeout=10)

        if detail_resp.status_code == 200:
            detail = detail_resp.json()
            # 最新位置を取得
            positions = detail.get("positions", [])
            if positions:
                latest_pos = positions[-1]
                lat = latest_pos.get("lat")
                lon = latest_pos.get("lon")
                if lat and lon:
                    dist = _haversine(ZAMAMI_LAT, ZAMAMI_LON, lat, lon)
                    result["typhoon_distance_km"] = round(dist, 0)

                # 台風強度カテゴリ
                intensity = latest_pos.get("intensity", "")
                result["typhoon_category"] = intensity
                result["typhoon_max_wind"] = latest_pos.get("wind_speed")

        print(f"  [台風] アクティブ: {result['typhoon_active']} / 距離: {result['typhoon_distance_km']}km")

    except Exception as e:
        print(f"  [警告] 台風データ取得エラー: {e}")

    return result


def _haversine(lat1, lon1, lat2, lon2):
    """2点間の距離(km)をHaversine公式で計算"""
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


# ============================================================
# 3. コンテキスト変数（連続欠航日数・先行き予報）
# ============================================================

def get_context_vars(analysis, gc, sheets_id):
    """
    Sheetsの履歴から連続欠航日数を計算し、
    Open-Meteoデータから翌3日間の波高予報を集計する。
    """
    result = {
        "consec_ferry_cancel_days": 0,
        "consec_hs_cancel_days": 0,
        "next3d_wave_avg": None,
        "next3d_bad_days": 0,
    }

    # --- 翌3日間の波高予報（analysisデータから） ---
    try:
        now = datetime.now(JST)
        next3d_waves = []
        for delta in range(1, 4):
            date_str = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
            all_day = analysis.get(date_str, {}).get("all_day")
            if all_day and all_day.get("max_wave"):
                next3d_waves.append(all_day["max_wave"])

        if next3d_waves:
            result["next3d_wave_avg"] = round(sum(next3d_waves) / len(next3d_waves), 2)
            result["next3d_bad_days"] = sum(1 for w in next3d_waves if w >= 2.5)
    except Exception as e:
        print(f"  [警告] 先行き予報計算エラー: {e}")

    # --- 連続欠航日数（Sheetsの直近レコードから） ---
    try:
        if not gc or not sheets_id:
            return result

        ws = gc.open_by_key(sheets_id).worksheet("daily_operation_log")
        records = ws.get_all_records()

        if not records:
            return result

        # 日付降順でソート
        sorted_records = sorted(records, key=lambda r: r.get("date", ""), reverse=True)

        # フェリー連続欠航日数
        ferry_consec = 0
        for rec in sorted_records:
            cancel = rec.get("ferry_weather_cancel")
            if str(cancel) == "1":
                ferry_consec += 1
            else:
                break
        result["consec_ferry_cancel_days"] = ferry_consec

        # 高速船連続欠航日数
        hs_consec = 0
        for rec in sorted_records:
            cancel = rec.get("hs_am_weather_cancel") or rec.get("hs_pm_weather_cancel")
            if str(cancel) == "1":
                hs_consec += 1
            else:
                break
        result["consec_hs_cancel_days"] = hs_consec

    except Exception as e:
        print(f"  [警告] 連続欠航日数計算エラー: {e}")

    return result


# ============================================================
# 4. Google Sheets への書き込み
# ============================================================

def log_daily_record(analysis, jma_waves, jma_prob, forecast):
    """
    メイン関数。全データを収集してGoogle Sheetsに1行追加する。
    ferry_alert.py の run_ferry_check() から呼び出す。
    """
    sheets_id = os.environ.get("GOOGLE_SHEETS_ID")
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not sheets_id or not service_account_json:
        print("  [スキップ] Google Sheets未設定（GOOGLE_SHEETS_ID / GOOGLE_SERVICE_ACCOUNT_JSON）")
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("  [スキップ] gspread未インストール")
        return

    try:
        creds_dict = json.loads(service_account_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ]
        )
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(sheets_id).worksheet("daily_operation_log")
    except Exception as e:
        print(f"  [エラー] Sheets接続失敗: {e}")
        return

    now = datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")

    print(f"\n[DB] 日次記録を取得中（{today_str}）...")

    # --- 各データ取得 ---
    op = get_zamami_operation_status()
    typhoon = get_typhoon_data()
    context = get_context_vars(analysis, gc, sheets_id)

    # --- 気象データの抽出 ---
    today = now.strftime("%Y-%m-%d")
    today_data = analysis.get(today, {})
    morning = today_data.get("morning") or {}
    afternoon = today_data.get("afternoon") or {}
    all_day = today_data.get("all_day") or {}

    # 明日のデータ（翌日分を先行き波高として使用）
    tmr_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    tmr_data = analysis.get(tmr_str, {}).get("all_day") or {}

    # --- 派生目的変数の計算 ---
    ferry_weather_cancel = (
        1 if op["ferry_operated"] == 0 and op["ferry_cancel_reason"] == "weather" else 0
    )
    hs_am_weather_cancel = (
        1 if op["hs_bin1_operated"] == 0 and op["hs_cancel_reason"] == "weather" else 0
    )
    hs_pm_weather_cancel = (
        1 if (op["hs_bin2_operated"] == 0 or op["hs_bin3_operated"] == 0)
        and op["hs_cancel_reason"] == "weather" else 0
    )

    # --- JMA情報 ---
    jma_wave_today   = jma_waves.get("今日", "")
    jma_wave_tomorrow = jma_waves.get("明日", "")
    jma_warn_today   = jma_prob.get("今日", {}).get("level", "なし")
    jma_warn_tomorrow = jma_prob.get("明日", {}).get("level", "なし")

    # --- モデル予測値（forecast から取得）---
    short = forecast.get("short_term", [])
    model_hs_pct = short[0].get("highspeed_pct", "") if short else ""
    model_fe_pct = short[0].get("ferry_pct", "") if short else ""

    # --- レコード構築（ヘッダー順に合わせる）---
    row = [
        today_str,                                          # date
        now.strftime("%Y-%m-%d %H:%M"),                     # recorded_at
        op.get("weather_desc", ""),                         # weather_desc
        op.get("announcement_text", "")[:300],              # announcement_text

        op.get("ferry_operated", ""),                       # ferry_operated
        op.get("ferry_turnaround", 0),                      # ferry_turnaround
        op.get("ferry_cancel_reason", ""),                  # ferry_cancel_reason

        op.get("hs_bin1_operated", ""),                     # hs_bin1_operated
        op.get("hs_bin2_operated", ""),                     # hs_bin2_operated
        op.get("hs_bin3_operated", ""),                     # hs_bin3_operated
        op.get("hs_cancel_reason", ""),                     # hs_cancel_reason

        ferry_weather_cancel,                               # ferry_weather_cancel
        hs_am_weather_cancel,                               # hs_am_weather_cancel
        hs_pm_weather_cancel,                               # hs_pm_weather_cancel

        morning.get("max_wave", ""),                        # wave_am
        afternoon.get("max_wave", ""),                      # wave_pm
        all_day.get("max_wave", ""),                        # wave_max
        all_day.get("max_swell", ""),                       # swell_height
        "",                                                 # swell_period（未取得）
        "",                                                 # swell_direction（未取得）

        morning.get("max_wind", ""),                        # wind_speed_am
        afternoon.get("max_wind", ""),                      # wind_speed_pm
        all_day.get("max_wind", ""),                        # wind_speed_max
        "",                                                 # wind_direction_am（未取得）

        "",                                                 # precip_am（未取得）
        "",                                                 # precip_total（未取得）

        jma_wave_today,                                     # jma_wave_today
        jma_wave_tomorrow,                                  # jma_wave_tomorrow
        jma_warn_today,                                     # jma_warning_today
        jma_warn_tomorrow,                                  # jma_warning_tomorrow

        typhoon.get("typhoon_active", 0),                   # typhoon_active
        typhoon.get("typhoon_distance_km", ""),             # typhoon_distance_km
        typhoon.get("typhoon_category", ""),                # typhoon_category
        typhoon.get("typhoon_max_wind", ""),                # typhoon_max_wind

        context.get("consec_ferry_cancel_days", 0),         # consec_ferry_cancel_days
        context.get("consec_hs_cancel_days", 0),            # consec_hs_cancel_days
        context.get("next3d_wave_avg", ""),                 # next3d_wave_avg
        context.get("next3d_bad_days", 0),                  # next3d_bad_days

        now.month,                                          # month
        1 if 6 <= now.month <= 10 else 0,                  # is_typhoon_season

        model_hs_pct,                                       # model_highspeed_pct
        model_fe_pct,                                       # model_ferry_pct
    ]

    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
        print(f"  ✅ Sheets記録完了: {today_str}")
        print(f"     フェリー={op.get('ferry_operated')} 高速船1便={op.get('hs_bin1_operated')} "
              f"2便={op.get('hs_bin2_operated')} 3便={op.get('hs_bin3_operated')}")
    except Exception as e:
        print(f"  [エラー] Sheets書き込み失敗: {e}")
