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
    座間味村HP（ship.html）から本日の運航情報を取得する。
    ferry_alert.py の get_ferry_status_from_web() と同様に
    今日の日付を起点にテキストを抽出して解析する。

    戻り値:
    {
        "ferry_operated": 1 or 0,
        "ferry_turnaround": 1 or 0,
        "ferry_cancel_reason": "weather" / "equipment" / "dock" / "none",
        "hs_bin1_operated": 1 or 0,
        "hs_bin2_operated": 1 or 0,
        "hs_bin3_operated": 1 or 0,
        "hs_cancel_reason": "weather" / "equipment" / "dock" / "none",
        "announcement_text": "運航情報セクションの原文",
        "weather_desc": "くもり後雨",
    }
    """
    import re
    from bs4 import BeautifulSoup

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
        resp = requests.get("https://www.vill.zamami.okinawa.jp/",
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        soup = BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "lxml")

        # ============================================================
        # announcement_text / weather_desc
        # div.un_homeFerryInfo_head に当日の概況テキストがある
        # ============================================================
        head_div = soup.find(class_="un_homeFerryInfo_head")
        head_text = head_div.get_text(" ", strip=True) if head_div else ""
        result["announcement_text"] = head_text[:500]

        weather_match = re.search(r"(晴れ?|くもり|雨|霧|台風)[^\s。、]{0,10}", head_text)
        if weather_match:
            result["weather_desc"] = weather_match.group(0)

        # ============================================================
        # 欠航理由はannouncement_textから判定（構造データにはない）
        # ============================================================
        def _cancel_reason(text):
            if any(w in text for w in ["機器", "エンジン", "トラブル", "故障", "点検", "整備"]):
                return "equipment"
            elif "ドック" in text:
                return "dock"
            else:
                return "weather"

        def _sentence_for(text, names):
            """本文を文単位に分割し、指定船名を含む文だけを返す。
            船種ごとに理由判定を分離し、他船の『ドック』等が混入するのを防ぐ
            （例: クイーン=ドック運休／フェリー=海上時化欠航 が同一本文に併記される日）。
            見つからなければ空文字（_cancel_reason は weather にフォールバック）。"""
            for p in re.split(r"[・。\n]", text or ""):
                if any(nm in p for nm in names):
                    return p
            return ""

        # ============================================================
        # 船ごとの運航判定（CSSクラス構造を直接使用）
        #
        # div.un_homeFerryInfo_route
        #   h3.un_homeFerryInfo_heading02        ← 船名
        #   div.bl_routeSchedule
        #     div.bl_routeSchedule_suspension    ← 欠航（存在すれば欠航確定）
        #     div.bl_routeSchedule_row           ← 運航便（便ごとに1要素）
        #       div.bl_routeSchedule_num         ← 「第1便」「第2便」など
        # ============================================================
        for route_div in soup.find_all(class_="un_homeFerryInfo_route"):
            heading = route_div.find(class_="un_homeFerryInfo_heading02")
            if not heading:
                continue
            vessel_name = heading.get_text(strip=True)
            is_suspended = bool(route_div.find(class_="bl_routeSchedule_suspension"))

            # ---- フェリーざまみ ----
            if "フェリーざまみ" in vessel_name:
                if is_suspended:
                    result["ferry_operated"] = 0
                    result["ferry_cancel_reason"] = _cancel_reason(
                        _sentence_for(head_text, ["フェリーざまみ"]))
                else:
                    result["ferry_operated"] = 1
                    result["ferry_cancel_reason"] = "none"
                    if "折り返し" in route_div.get_text():
                        result["ferry_turnaround"] = 1

            # ---- クィーンざまみ ----
            elif "クィーンざまみ" in vessel_name or "クイーンざまみ" in vessel_name:
                if is_suspended:
                    result["hs_bin1_operated"] = 0
                    result["hs_bin2_operated"] = 0
                    result["hs_bin3_operated"] = 0
                    result["hs_cancel_reason"] = _cancel_reason(
                        _sentence_for(head_text, ["クイーンざまみ", "クィーンざまみ"]))
                else:
                    # 運航している便 = bl_routeSchedule_row が存在する便
                    active_bins = set()
                    for row in route_div.find_all(class_="bl_routeSchedule_row"):
                        num_el = row.find(class_="bl_routeSchedule_num")
                        if num_el:
                            m = re.search(r'([１２３1-3])', num_el.get_text(strip=True))
                            if m:
                                n = str("１２３".index(m.group(1)) + 1) \
                                    if m.group(1) in "１２３" else m.group(1)
                                active_bins.add(n)

                    if active_bins:
                        result["hs_bin1_operated"] = 1 if "1" in active_bins else 0
                        result["hs_bin2_operated"] = 1 if "2" in active_bins else 0
                        result["hs_bin3_operated"] = 1 if "3" in active_bins else 0
                    else:
                        # rowが取れなかった場合は全便運航と見なす
                        result["hs_bin1_operated"] = 1
                        result["hs_bin2_operated"] = 1
                        result["hs_bin3_operated"] = 1
                    result["hs_cancel_reason"] = "none"

        print(f"  [HP] フェリー: {'運航' if result['ferry_operated']==1 else '欠航('+result['ferry_cancel_reason']+')' if result['ferry_operated']==0 else '不明'}{'(折り返し)' if result['ferry_turnaround'] else ''}")
        print(f"  [HP] 高速船: 1便={result['hs_bin1_operated']} 2便={result['hs_bin2_operated']} 3便={result['hs_bin3_operated']}")
        print(f"  [HP] 天気: {result['weather_desc']}")

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
        "typhoon_distance_km": float or None,   # 座間味からの現在距離
        "typhoon_category": str or None,         # 階級（強い/非常に強い 等）
        "typhoon_max_wind": float or None,       # 最大風速 m/s
    }

    ※ 旧実装は存在しない list.json を叩いて常に台風なし(0)を返していた。
       ferry_alert.py の get_typhoon_forecast() と同じ targetTc.json →
       {tcid}/forecast.json 構造に修正（2026-06 バグ修正）。
    """
    result = {
        "typhoon_active": 0,
        "typhoon_distance_km": None,
        "typhoon_category": None,
        "typhoon_max_wind": None,
    }

    try:
        # 活動中の台風ID一覧（台風なしのときは空配列）
        idx = requests.get(
            "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json", timeout=10
        ).json()
        if not idx:
            return result  # 台風なし

        result["typhoon_active"] = 1

        # 最も座間味に近い台風の現在位置を探す
        best_dist = None
        for tc in idx:
            tcid = tc.get("tropicalCyclone")
            if not tcid:
                continue
            try:
                fc = requests.get(
                    f"https://www.jma.go.jp/bosai/typhoon/data/{tcid}/forecast.json",
                    timeout=10,
                ).json()
            except Exception as e:
                print(f"  [警告] 台風進路取得エラー {tcid}: {e}")
                continue

            # part=="Analysis"（現在位置）を優先、なければ center を持つ最初の part
            analysis_part = None
            for part in fc:
                if part.get("part") == "Analysis" and part.get("center"):
                    analysis_part = part
                    break
            if analysis_part is None:
                for part in fc:
                    if part.get("center"):
                        analysis_part = part
                        break
            if analysis_part is None:
                continue

            center = analysis_part["center"]  # [lat, lon]
            dist = _haversine(ZAMAMI_LAT, ZAMAMI_LON, center[0], center[1])

            if best_dist is None or dist < best_dist:
                best_dist = dist
                result["typhoon_distance_km"] = round(dist, 0)
                # 階級・最大風速（キー名はAPIに合わせ複数候補をフォールバック）
                result["typhoon_category"] = (
                    analysis_part.get("category")
                    or analysis_part.get("intensity")
                    or analysis_part.get("class", "")
                )
                mw = (analysis_part.get("maximumWind", {}) or {})
                result["typhoon_max_wind"] = (
                    mw.get("speed") if isinstance(mw, dict) else None
                ) or analysis_part.get("wind_speed")

        print(f"  [台風] アクティブ: {result['typhoon_active']} / "
              f"距離: {result['typhoon_distance_km']}km / "
              f"階級: {result['typhoon_category']}")

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

def log_daily_record(analysis, jma_waves, jma_prob, forecast, warnings=None):
    """
    メイン関数。全データを収集してGoogle Sheetsに1行追加する。
    ferry_alert.py の run_ferry_check() から呼び出す。

    warnings: get_jma_warnings() の返り値（当日の実警報・注意報リスト）。
        jma_warning_today は早期注意情報（=明日以降の予報）には含まれないため、
        当日の警報はこの実警報APIから生成する。
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

    # ---- 重複チェック：当日分がすでに記録されていればスキップ ----
    # daily_operation_log は 1日1行（8:15 のみ）が設計方針
    try:
        existing_dates = ws.col_values(1)  # A列（date）を取得
        if today_str in existing_dates:
            print(f"  [スキップ] {today_str} の記録はすでに存在します（1日1行ルール）")
            return
    except Exception as e:
        print(f"  [警告] 重複チェックエラー（続行）: {e}")

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
    jma_wave_today    = jma_waves.get("今日", "")
    jma_wave_tomorrow = jma_waves.get("明日", "")

    # jma_warning_today: 当日の実警報・注意報（warning API）から生成。
    #   早期注意情報（probability API = jma_prob）は「明日以降」の予報のため
    #   "今日" は常に空になる。当日の警報はこちらの実警報リストを使う。
    # JMA警報コード（warning/471000.json の code）→ 表示名（船舶に関係する主要コード）
    _WARN_CODE = {
        "02": "暴風雪警報", "03": "大雨警報", "04": "洪水警報", "05": "暴風警報",
        "06": "大雪警報", "07": "波浪警報", "08": "高潮警報",
        "10": "大雨注意報", "12": "大雪注意報", "13": "風雪注意報", "14": "雷注意報",
        "15": "強風注意報", "16": "波浪注意報", "18": "洪水注意報", "19": "高潮注意報",
        "20": "濃霧注意報", "32": "暴風雪特別警報", "33": "大雨特別警報",
        "35": "暴風特別警報", "36": "大雪特別警報",
    }
    if warnings:
        names = []
        for w in warnings:
            code = str(w.get("type", ""))
            names.append(_WARN_CODE.get(code, f"コード{code}"))
        # 重複除去・順序維持
        seen = set()
        uniq = [n for n in names if not (n in seen or seen.add(n))]
        jma_warn_today = " / ".join(uniq) if uniq else "なし"
    else:
        jma_warn_today = "なし"

    # jma_warning_tomorrow: 早期注意情報（警報級確率: 高/中/なし）
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
        all_day.get("max_swell_period", ""),                # swell_period（analyze_periodで取得済み）
        all_day.get("wave_direction", ""),                  # swell_direction（波向で代用・うねり単独の向きは未算出）

        morning.get("max_wind", ""),                        # wind_speed_am
        afternoon.get("max_wind", ""),                      # wind_speed_pm
        all_day.get("max_wind", ""),                        # wind_speed_max
        morning.get("wind_direction", ""),                  # wind_direction_am（analyze_periodで取得済み）

        morning.get("max_precip", ""),                      # precip_am（analyze_periodで取得済み）
        all_day.get("max_precip", ""),                      # precip_total（analyze_periodで取得済み）

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
