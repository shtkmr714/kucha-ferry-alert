"""
Kucha Ferry Alert System v3
座間味島フェリー運航状況モニター＆ゲストメッセージ自動生成

改良点（v3）：
- 気象庁forecast JSON追加（波高テキスト：今日〜明後日）
- 気象庁早期注意情報追加（警報級確率：翌日・翌々日）
- Open-Meteo数値 + 気象庁テキスト + 早期注意情報の3ソース統合
"""

import os
import json
import math
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from anthropic import Anthropic

JST = ZoneInfo("Asia/Tokyo")

# ============================================================
# 設定
# ============================================================

# 取得地点：航路を代表する3地点。各時刻で「最も荒れる値」を採用する。
# 欠航は航路上で最も荒れる区間（外洋に面した座間味側）で決まるため、
# 単一の中間点では最悪区間を過小評価する（実測で確認）。
#   座間味沖：外洋に面し最も荒れる / 海峡中央：航路中央 / 泊沖：那覇側
ROUTE_POINTS = [
    (26.23, 127.30, "座間味沖"),
    (26.22, 127.48, "海峡中央"),
    (26.21, 127.62, "泊沖"),
]
# 後方互換・台風距離計算の参照点（航路中央）
LAT = 26.225
LON = 127.485

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

def _as_location_list(data):
    """Open-Meteoレスポンスを地点リストに正規化（単一地点はdict、複数地点はlistで返る）。"""
    if isinstance(data, list):
        return data
    return [data]


def _worst(values, mode="max"):
    """Noneを除いて最悪値（max/min）を返す。全てNoneならNone。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return max(vals) if mode == "max" else min(vals)


def get_marine_and_weather_data():
    """
    Open-Meteo Marine API + Weather APIから総合データを取得。
    航路代表3地点（ROUTE_POINTS）を1リクエストで取得し、各時刻で
    「最も荒れる値」を採用した統合系列を返す（最悪区間の過小評価を防ぐ）。
      - 波高・うねり・周期・風速・突風・降水: 最大
      - 視程: 最小
      - 波向・風向: 最大波高地点の値
    """
    lats = ",".join(str(p[0]) for p in ROUTE_POINTS)
    lons = ",".join(str(p[1]) for p in ROUTE_POINTS)

    marine_url = "https://marine-api.open-meteo.com/v1/marine"
    marine_params = {
        "latitude": lats,
        "longitude": lons,
        "hourly": [
            "wave_height", "wave_period", "wave_direction",
            "wind_wave_height", "swell_wave_height", "swell_wave_period",
        ],
        "timezone": "Asia/Tokyo",
        "forecast_days": 8,
    }

    weather_url = "https://api.open-meteo.com/v1/forecast"
    weather_params = {
        "latitude": lats,
        "longitude": lons,
        "hourly": [
            "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m",
            "visibility", "precipitation",
        ],
        "timezone": "Asia/Tokyo",
        "forecast_days": 8,
    }

    marine_resp = requests.get(marine_url, params=marine_params, timeout=20)
    marine_resp.raise_for_status()
    marine_locs = _as_location_list(marine_resp.json())

    weather_resp = requests.get(weather_url, params=weather_params, timeout=20)
    weather_resp.raise_for_status()
    weather_locs = _as_location_list(weather_resp.json())

    # 時間軸は全地点共通（先頭地点を基準）
    times = marine_locs[0]["hourly"]["time"]
    combined = []

    def mh(loc, key, i):
        return loc["hourly"].get(key, [None] * len(times))[i]

    for i, t in enumerate(times):
        # 各地点の波高（最大波高地点を波向・風向の代表に使う）
        waves = [mh(loc, "wave_height", i) for loc in marine_locs]
        valid_waves = [(w, idx) for idx, w in enumerate(waves) if w is not None]
        peak_idx = max(valid_waves)[1] if valid_waves else 0

        # 風速・突風はkm/h→m/s（÷3.6）。各地点の最悪値を採る
        wind_ms = [
            (mh(loc, "wind_speed_10m", i) / 3.6) if mh(loc, "wind_speed_10m", i) is not None else None
            for loc in weather_locs
        ]
        gust_ms = [
            (mh(loc, "wind_gusts_10m", i) / 3.6) if mh(loc, "wind_gusts_10m", i) is not None else None
            for loc in weather_locs
        ]
        worst_wind = _worst(wind_ms, "max")
        worst_gust = _worst(gust_ms, "max")

        entry = {
            "time": t,
            "date": t.split("T")[0],
            "hour": int(t.split("T")[1].split(":")[0]),
            "wave_height":       _worst([mh(loc, "wave_height", i) for loc in marine_locs], "max"),
            "wave_period":       _worst([mh(loc, "wave_period", i) for loc in marine_locs], "max"),
            "wave_direction":    mh(marine_locs[peak_idx], "wave_direction", i),
            "wind_wave_height":  _worst([mh(loc, "wind_wave_height", i) for loc in marine_locs], "max"),
            "swell_wave_height": _worst([mh(loc, "swell_wave_height", i) for loc in marine_locs], "max"),
            "swell_wave_period": _worst([mh(loc, "swell_wave_period", i) for loc in marine_locs], "max"),
            "wind_speed":  round(worst_wind, 1) if worst_wind is not None else None,
            "wind_gust":   round(worst_gust, 1) if worst_gust is not None else None,
            "wind_direction":  mh(weather_locs[peak_idx], "wind_direction_10m", i) if peak_idx < len(weather_locs) else mh(weather_locs[0], "wind_direction_10m", i),
            "visibility":  _worst([mh(loc, "visibility", i) for loc in weather_locs], "min"),
            "precipitation": _worst([mh(loc, "precipitation", i) for loc in weather_locs], "max"),
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


def get_jma_forecast_waves():
    """
    気象庁forecast JSONから沖縄地方の波高テキスト予報を取得。
    今日・明日・明後日の3日分を返す。
    例: {"今日": "1メートル後2メートル", "明日": "3メートル", "明後日": "4メートルのち5メートル"}
    """
    try:
        url = "https://www.jma.go.jp/bosai/forecast/data/forecast/471000.json"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        result = {}
        for series in data[0].get("timeSeries", []):
            times = series.get("timeDefines", [])
            for area in series.get("areas", []):
                waves = area.get("waves", [])
                if not waves:
                    continue
                area_name = area.get("area", {}).get("name", "")
                if "中南部" not in area_name and "南部" not in area_name:
                    continue
                for i, wave in enumerate(waves):
                    if i < len(times):
                        dt = datetime.fromisoformat(times[i])
                        delta = (dt.date() - datetime.now(JST).date()).days
                        label = {0: "今日", 1: "明日", 2: "明後日"}.get(delta)
                        if label and wave:
                            result[label] = wave
                if result:
                    break

        return result

    except Exception as e:
        print(f"[警告] 気象庁forecast JSON取得エラー: {e}")
        return {}


def get_jma_probability():
    """
    気象庁早期注意情報（471000: 沖縄県）から波浪警報級確率を取得。
    timeSeries → areas → properties 構造をパース。
    翌日・翌々日の波浪警報級確率（高/中/なし）を返す。
    """
    try:
        url = "https://www.jma.go.jp/bosai/probability/data/probability/471000.json"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        result = {}
        for entry in data:
            for series in entry.get("timeSeries", []):
                time_defines = series.get("timeDefines", [])
                for area in series.get("areas", []):
                    # 沖縄本島南部エリア（471010）に絞る
                    if area.get("code") != "471010":
                        continue
                    for i, time_str in enumerate(time_defines):
                        dt = datetime.fromisoformat(time_str)
                        delta = (dt.date() - datetime.now(JST).date()).days
                        label = {1: "明日", 2: "明後日"}.get(delta)
                        if not label:
                            continue
                        for prop in area.get("properties", []):
                            prop_type = prop.get("type", "")
                            if "波浪" not in prop_type and "高波" not in prop_type:
                                continue
                            parts = prop.get("parts", [])
                            if i < len(parts):
                                level = parts[i].get("level", "")
                                if level:
                                    result[label] = {"type": prop_type, "level": level}
        return result

    except Exception as e:
        print(f"[警告] 気象庁早期注意情報取得エラー: {e}")
        return {}


# ============================================================
# 1-2. 台風進路（気象庁 typhoon API）
# ============================================================

# 慶良間海域の代表点（航路中央）
KERAMA_LAT, KERAMA_LON = 26.225, 127.485


def _haversine_km(lat1, lon1, lat2, lon2):
    """2点間の大圏距離（km）。"""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# 外挿（独自予測）設定
TYPHOON_EXTRAP_TIER = 5          # 公式tier1〜4とは別格の「外挿（参考）」tier
TYPHOON_EXTRAP_HS_FLOOR = 50     # 外挿フロアは控えめ（参考値）
TYPHOON_EXTRAP_FE_FLOOR = 25
TYPHOON_EXTRAP_MAX_DIST_KM = 600 # これより遠い外挿は採用しない
DEFAULT_CIRCLE_GROWTH_M_PER_H = 2500  # 予報円半径の拡大率フォールバック


def get_typhoon_forecast(window_days=8):
    """
    気象庁 typhoon API から活動中の台風進路を取得し、
    日付別の慶良間接近リスク（フロア）を返す。

    公式進路（最大約5日先）:
      tier1 暴風警報域内        → 高速船95% / フェリー85%
      tier2 予報円内＋暴風域あり  → 高速船80% / フェリー60%
      tier3 予報円内のみ         → 高速船60% / フェリー35%
      tier4 中心〜500km          → 高速船40%（控えめ）

    独自外挿（6〜8日先・公式予報の範囲外）:
      tier5 移動ベクトル外挿で接近見込み → 高速船50% / フェリー25%（参考・不確実性大）
        - 最終2点から速度ベクトルを算出して中心位置を外挿
        - 予報円の拡大率を延長して不確実性円を広げ、その内側＋600km以内なら採用
        - "extrapolated": True ラベルを付け、公式扱いしない

    戻り値: {"YYYY-MM-DD": {tier, hs_floor, fe_floor, name_ja, dist_km,
                            in_storm, in_circle, extrapolated, tcid}}
    """
    result = {}
    try:
        idx = requests.get(
            "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json", timeout=10
        ).json()
    except Exception as e:
        print(f"[警告] 台風一覧取得エラー: {e}")
        return result

    today = datetime.now(JST).date()
    window_end = today + timedelta(days=window_days - 1)

    def _set(date, tier, hs, fe, name_ja, dist, in_storm, in_circle, extrapolated, tcid):
        prev = result.get(date)
        if prev is None or tier < prev["tier"]:
            result[date] = {
                "tier": tier, "hs_floor": hs, "fe_floor": fe,
                "name_ja": name_ja, "dist_km": round(dist),
                "in_storm": in_storm, "in_circle": in_circle,
                "extrapolated": extrapolated, "tcid": tcid,
            }

    for tc in idx:
        tcid = tc.get("tropicalCyclone")
        if not tcid:
            continue
        name_ja = ""
        try:
            fc = requests.get(
                f"https://www.jma.go.jp/bosai/typhoon/data/{tcid}/forecast.json",
                timeout=10,
            ).json()
        except Exception as e:
            print(f"[警告] 台風進路取得エラー {tcid}: {e}")
            continue

        points = []   # [(datetime, lat, lon)] 速度外挿用
        circles = []  # [(hours_from_now, radius_m)] 拡大率算出用

        for part in fc:
            if part.get("part") == "title":
                name_ja = part.get("name", {}).get("jp", "") or tcid
                continue

            center = part.get("center")
            vt = part.get("validtime", {}).get("JST")
            if not center or not vt:
                continue

            try:
                vt_dt = datetime.fromisoformat(vt)
            except ValueError:
                vt_dt = None
            if vt_dt is not None:
                points.append((vt_dt, center[0], center[1]))

            date = vt.split("T")[0]
            dist = _haversine_km(KERAMA_LAT, KERAMA_LON, center[0], center[1])

            circle_r = part.get("probabilityCircle", {}).get("radius")
            if circle_r is not None and vt_dt is not None:
                circles.append(((vt_dt - datetime.now(JST)).total_seconds() / 3600, circle_r))
            in_circle = circle_r is not None and dist * 1000 <= circle_r

            # 暴風警報域判定：
            # stormWarningArea.arc は「当該予報時刻までの累積エンベロープ」を描く
            # arc列で、過去位置の arc も含む。素朴に全 arc を見ると、台風が遥か
            # 遠方にある日でも過去 arc に引っかかり「暴風域内」と誤判定する。
            # 正しい判定は「現時刻の台風中心(part.center)を中心とする arc」のみ。
            storm = part.get("stormWarningArea")
            in_storm = False
            if storm and storm.get("arc"):
                storm_r = 0
                for arc in storm["arc"]:
                    try:
                        c, r = arc[0], arc[1]
                        # 当該予報点の中心と一致する arc を抽出（=今この瞬間の暴風域）
                        if abs(c[0] - center[0]) < 0.05 and abs(c[1] - center[1]) < 0.05:
                            if r > storm_r:
                                storm_r = r
                    except (IndexError, TypeError):
                        continue
                if storm_r and dist * 1000 <= storm_r:
                    in_storm = True

            if in_storm:
                # tier1（暴風警報域内）: 実態として欠航ほぼ確実なため
                # フェリーフロアも高速船と同水準(95)に設定
                tier, hs, fe = 1, 95, 95
            elif in_circle and storm:
                tier, hs, fe = 2, 80, 60
            elif in_circle:
                tier, hs, fe = 3, 60, 35
            elif dist <= 500:
                tier, hs, fe = 4, 40, 0
            else:
                continue

            _set(date, tier, hs, fe, name_ja, dist, in_storm, in_circle, False, tcid)

        # ---- 独自外挿（公式予報の最終点より先の日を補完）----
        if len(points) >= 2:
            points.sort(key=lambda p: p[0])
            (t0, lat0, lon0), (t1, lat1, lon1) = points[-2], points[-1]
            dt_h = (t1 - t0).total_seconds() / 3600
            if dt_h > 0:
                dlat_dt = (lat1 - lat0) / dt_h    # 度/時
                dlon_dt = (lon1 - lon0) / dt_h
                # 予報円の拡大率（m/時）
                if len(circles) >= 2:
                    circles.sort()
                    (h_a, r_a), (h_b, r_b) = circles[0], circles[-1]
                    growth = (r_b - r_a) / (h_b - h_a) if h_b > h_a else DEFAULT_CIRCLE_GROWTH_M_PER_H
                else:
                    growth = DEFAULT_CIRCLE_GROWTH_M_PER_H
                last_r = circles[-1][1] if circles else 300000
                last_date = t1.date()

                for delta in range(window_days):
                    d = today + timedelta(days=delta)
                    if d <= last_date or d > window_end:
                        continue  # 公式範囲内 or 窓外はスキップ
                    # 当日12:00 JST を代表時刻に外挿
                    target = datetime(d.year, d.month, d.day, 12, 0, tzinfo=JST)
                    h_ahead = (target - t1).total_seconds() / 3600
                    if h_ahead <= 0:
                        continue
                    ex_lat = lat1 + dlat_dt * h_ahead
                    ex_lon = lon1 + dlon_dt * h_ahead
                    ex_dist = _haversine_km(KERAMA_LAT, KERAMA_LON, ex_lat, ex_lon)
                    # 不確実性円（拡大率を延長）
                    unc_r = last_r + max(0.0, growth) * h_ahead
                    if ex_dist * 1000 <= unc_r and ex_dist <= TYPHOON_EXTRAP_MAX_DIST_KM:
                        _set(d.strftime("%Y-%m-%d"), TYPHOON_EXTRAP_TIER,
                             TYPHOON_EXTRAP_HS_FLOOR, TYPHOON_EXTRAP_FE_FLOOR,
                             name_ja, ex_dist, False, False, True, tcid)

    return result


def get_ferry_status_from_web():
    """座間味村HPから運航情報を取得。"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(ZAMAMI_URL, headers=headers, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        text_content = soup.get_text(separator="\n", strip=True)

        today = datetime.now(JST)
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

def calc_cancellation_score(wave_h, swell_h, wind_spd, has_warning,
                            gust=None, swell_period=None):
    """
    各メトリクスを0〜1に正規化して欠航スコアを算出。
    スコアが高いほど欠航リスクが高い。

    段階導入（v4）：既存4項目の重み構造は維持しつつ、
      - 突風(gust): 風項を「平均風速 or 突風(25m/s=1.0)」の強い方で評価
      - うねり周期(swell_period): 長周期(>8s)は同じ波高でも動揺が大きいため
        うねり項を最大1.3倍まで割り増し
    を補正項として加える（過去スコアとの不連続を最小化）。
    """
    # 波高スコア（0m=0, 5m以上=1）
    wave_score = min(wave_h / 5.0, 1.0) if wave_h else 0

    # うねりスコア（0m=0, 4m以上=1）
    swell_score = min(swell_h / 4.0, 1.0) if swell_h else 0
    # 長周期うねり補正：8秒を基準に、超過1秒あたり+3.75%（上限1.3倍）
    if swell_period and swell_h:
        factor = 1.0 + max(0.0, swell_period - 8.0) * 0.0375
        swell_score = min(swell_score * min(factor, 1.3), 1.0)

    # 風速スコア（0m/s=0, 20m/s以上=1）
    wind_score = min(wind_spd / 20.0, 1.0) if wind_spd else 0
    # 突風補正：突風は25m/sで1.0正規化し、平均風速と強い方を採用
    if gust:
        wind_score = max(wind_score, min(gust / 25.0, 1.0))

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

    def _max(key):
        vals = [d.get(key) for d in valid if d.get(key) is not None]
        return max(vals) if vals else None

    def _min(key):
        vals = [d.get(key) for d in valid if d.get(key) is not None]
        return min(vals) if vals else None

    max_wave = max(d["wave_height"] for d in valid)
    max_swell = _max("swell_wave_height")
    max_wind = _max("wind_speed")
    max_gust = _max("wind_gust")
    max_swell_period = _max("swell_wave_period")
    avg_wave = sum(d["wave_height"] for d in valid) / len(valid)

    # 追加メトリクス（スコア反映は段階導入のため当面DB蓄積用）
    max_wave_period = _max("wave_period")
    max_wind_wave = _max("wind_wave_height")
    min_visibility = _min("visibility")
    max_precip = _max("precipitation")
    # 代表波向・風向は最大波高時刻の値
    peak = max(valid, key=lambda d: d["wave_height"])
    wave_dir = peak.get("wave_direction")
    wind_dir = peak.get("wind_direction")

    score = calc_cancellation_score(
        max_wave, max_swell or 0, max_wind or 0, has_warning,
        gust=max_gust, swell_period=max_swell_period,
    )

    return {
        "max_wave": round(max_wave, 1),
        "avg_wave": round(avg_wave, 1),
        "max_swell": round(max_swell, 1) if max_swell else None,
        "max_wind": round(max_wind, 1) if max_wind else None,
        "max_gust": round(max_gust, 1) if max_gust else None,
        "max_swell_period": round(max_swell_period, 1) if max_swell_period else None,
        "max_wave_period": round(max_wave_period, 1) if max_wave_period else None,
        "max_wind_wave": round(max_wind_wave, 1) if max_wind_wave else None,
        "min_visibility": round(min_visibility) if min_visibility is not None else None,
        "max_precip": round(max_precip, 1) if max_precip is not None else None,
        "wave_direction": round(wave_dir) if wave_dir is not None else None,
        "wind_direction": round(wind_dir) if wind_dir is not None else None,
        "has_warning": has_warning,
        "cancellation_score": score,
        "risk_highspeed": max_wave >= THRESHOLD_HIGHSPEED or score >= SCORE_HIGHSPEED_RISK,
        "risk_ferry": max_wave >= THRESHOLD_FERRY or score >= SCORE_FERRY_RISK,
    }


def analyze_all_data(combined_data, warnings):
    """
    全データを日付・時間帯別に分析。
    """
    today = datetime.now(JST).strftime("%Y-%m-%d")
    results = {}

    # 今日〜7日後まで分析
    for delta in range(8):
        date = (datetime.now(JST) + timedelta(days=delta)).strftime("%Y-%m-%d")
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

    today = datetime.now(JST).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%d")

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

    # 気象庁テキスト予報・早期注意情報を追加取得
    jma_waves = get_jma_forecast_waves()
    jma_prob = get_jma_probability()

    situation = f"""
【明日の予報】{fmt(tomorrow_a)}
【明後日の予報】{fmt(analysis.get((datetime.now(JST) + timedelta(days=2)).strftime("%Y-%m-%d"), {}))}
【気象庁 波高テキスト予報（公式）】
  明日: {jma_waves.get('明日', '未取得')}
  明後日: {jma_waves.get('明後日', '未取得')}
【気象庁 早期注意情報（波浪警報級確率）】
  明日: {jma_prob.get('明日', {}).get('level', 'なし') or 'なし'}
  明後日: {jma_prob.get('明後日', {}).get('level', 'なし') or 'なし'}
【気象庁注意報】{json.dumps(warnings, ensure_ascii=False) if warnings else 'なし'}
【座間味村HP運航情報（本日分）】{ferry_status or '未確認'}
"""

    prompt = f"""
あなたはHomestay Kucha（沖縄・座間味島）のスタッフです。
以下の明日・明後日の気象・波データをもとにゲスト向けメッセージを生成してください。

{situation}

【このメッセージの目的】
ゲストが以下の判断を自分でできるよう、正直な情報を提供すること：
- すでに島にいるゲスト：早めにチェックアウトして那覇に戻るか、延泊するかの判断
- これから来るゲスト：旅程変更・キャンセルを検討するかの判断

以下2パターンを生成：

【パターンA】明日または明後日に欠航リスクあり（警戒レベル）
- 具体的にいつ・どの便にリスクがあるかを明示
- すでに島にいるゲストへの案内（早めの帰島を検討）
- これから来るゲストへの案内（旅程変更の選択肢）
- 最新情報は座間味村公式から確認するよう案内
- キャンセル・変更を強制しない。あくまで判断材料として

【パターンB】明日・明後日ともにリスク低め（念のため共有）
- 現時点では問題なさそうだが予報は変わりうることを伝える
- 短く・明るいトーンで

各パターン：英語（Airbnb/Booking送信用）と日本語（参考）
トーン：honest / warm / practical（Kuchaらしく）
英語は4〜6文程度
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
# 4-2. 計画運休情報
# ============================================================

def scrape_planned_suspensions():
    """座間味村HPトップページから計画運休情報をスクレイプし、Claudeで構造化する"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get("https://www.vill.zamami.okinawa.jp/", headers=headers, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(separator="\n", strip=True)

        lines = text.split("\n")
        # クィーンざまみ（ィ有無両対応）も追加
        keywords = ["運休", "ドック", "クイーンざまみ", "クィーンざまみ", "フェリーざまみ", "高速船"]
        matched_indices = [i for i, l in enumerate(lines) if any(k in l for k in keywords)]
        if not matched_indices:
            return []

        # 行単位フィルタだと日付と船名が別行に分かれた場合に日付が欠落する。
        # マッチ行の前後3行を含めてコンテキストを確保する。
        context_indices = set()
        for idx in matched_indices:
            for j in range(max(0, idx - 3), min(len(lines), idx + 4)):
                context_indices.add(j)
        snippet = "\n".join(lines[i] for i in sorted(context_indices))
        snippet = snippet[:2000]  # Claude への入力上限
        print(f"  [計画運休スクレイプ] 抽出スニペット:\n{snippet[:300]}...")
        client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        today_str = datetime.now(JST).strftime("%Y-%m-%d")
        year = datetime.now(JST).year

        resp_ai = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": f"""以下は座間味村HPから抽出したテキストです。
計画的な運休・ドック入りの情報を抽出してJSON配列で返してください。
今日は{today_str}（{year}年）です。

テキスト:
{snippet}

各運休エントリのフォーマット:
{{
  "start": "YYYY-MM-DD",
  "end": "YYYY-MM-DD",
  "service": "highspeed" or "ferry" or "both",
  "vessel_ja": "船名",
  "vessel_en": "vessel name in English",
  "reason_ja": "理由",
  "reason_en": "reason in English"
}}

運休情報がない場合は空配列 [] を返してください。
JSON配列のみ返してください（説明文不要）。"""}]
        )

        import re
        text_resp = resp_ai.content[0].text.strip()
        match = re.search(r'\[.*\]', text_resp, re.DOTALL)
        if not match:
            return []
        return json.loads(match.group())

    except Exception as e:
        print(f"  [警告] 計画運休スクレイプエラー: {e}")
        return []


def load_manual_suspensions():
    """planned_suspensions.jsonから手動入力の運休情報を読み込む"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, "planned_suspensions.json")
    if not os.path.exists(json_path):
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [警告] planned_suspensions.json読み込みエラー: {e}")
        return []


SUSPENSION_HP_CHECK_DAYS = 10  # 開始日まで10日以内のみHP照合チェックを行う


def detect_suspension_conflicts(scraped, manual):
    """スクレイプと手動入力の運休情報を比較し、齟齬を検出する"""
    from datetime import date as _date
    today = _date.today()
    conflicts = []
    for m in manual:
        m_start = m.get("start", "")
        m_end = m.get("end", "")
        m_service = m.get("service", "")
        # 開始日まで10日以上先の場合はHPに未掲載でも正常 → HP照合スキップ
        try:
            days_until = (_date.fromisoformat(m_start) - today).days
            if days_until > SUSPENSION_HP_CHECK_DAYS:
                continue
        except (ValueError, TypeError):
            pass
        matching = [s for s in scraped
                    if s.get("service") in (m_service, "both") or m_service == "both"]
        if not matching:
            # HPに該当サービスの運休情報が見当たらない（手動のみ存在）
            conflicts.append({
                "service": m_service,
                "manual": f"{m_start}〜{m_end}（{m.get('vessel_ja','')}：{m.get('reason_ja','')}）",
                "scraped": "（HPに該当情報なし）",
            })
        for s in matching:
            s_start = s.get("start", "")
            s_end = s.get("end", "")
            if s_start and s_end and (s_start != m_start or s_end != m_end):
                conflicts.append({
                    "service": m_service,
                    "manual": f"{m_start}〜{m_end}（{m.get('vessel_ja','')}：{m.get('reason_ja','')}）",
                    "scraped": f"{s_start}〜{s_end}（{s.get('vessel_ja','')}：{s.get('reason_ja','')}）",
                })
    return conflicts


def get_planned_suspensions():
    """計画運休情報を取得（スクレイプ + 手動入力のマージ）。齟齬があればSlackアラートを送信。"""
    print("\n[計画運休] 情報取得中...")

    scraped = scrape_planned_suspensions()
    print(f"  スクレイプ結果: {len(scraped)}件")

    manual = load_manual_suspensions()
    print(f"  手動入力: {len(manual)}件")

    conflicts = detect_suspension_conflicts(scraped, manual)
    if conflicts:
        alert_lines = ["⚠️ 計画運休情報に齟齬が検出されました。確認してください。\n"]
        for c in conflicts:
            alert_lines.append(f"サービス: {c['service']}")
            alert_lines.append(f"  手動入力: {c['manual']}")
            alert_lines.append(f"  HP取得:   {c['scraped']}")
            alert_lines.append("  → planned_suspensions.json を確認・更新してください。")
        send_slack("\n".join(alert_lines), emoji="")
        print(f"  ⚠️ 齟齬{len(conflicts)}件 → Slackアラート送信")

    # マージ: 手動入力優先 + スクレイプのみにある情報を補完
    merged = list(manual)
    manual_keys = {(m.get("start"), m.get("end"), m.get("service")) for m in manual}
    for s in scraped:
        key = (s.get("start", ""), s.get("end", ""), s.get("service", ""))
        if key not in manual_keys and s.get("start") and s.get("end"):
            merged.append(s)

    print(f"  マージ後: {len(merged)}件")
    for sus in merged:
        print(f"    {sus.get('vessel_ja','')} {sus.get('service','')} "
              f"{sus.get('start','')}〜{sus.get('end','')} {sus.get('reason_ja','')}")
    return merged


# ============================================================
# 5. メイン処理
# ============================================================

def run_ferry_check():
    now = datetime.now(JST)
    print(f"\n{'='*50}")
    print(f"Kucha Ferry Alert v2: {now.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    # スケジュール実行の時刻ガード
    # GitHub Actions のスケジューラーが古いキャッシュで意図しない時刻に実行されるケースへの対策
    # 手動実行（workflow_dispatch）はスキップチェックなし
    event_name = os.environ.get("GITHUB_EVENT_NAME", "manual")
    if event_name == "schedule":
        # 許可時間帯（JST）: 7〜10時台（8:15 JST想定）、12〜15時台（14:30 JST想定＋Actions遅延吸収）
        allowed_hours = set(range(7, 11)) | set(range(12, 16))
        if now.hour not in allowed_hours:
            print(f"[スキップ] スケジュール外の時刻: {now.strftime('%H:%M')} JST")
            print(f"  許可時間帯: 7〜10時 / 12〜15時（JST）")
            print(f"  GitHubスケジューラーの遅延が原因の可能性があります。処理を中断します。")
            return

    # 0. 計画運休情報取得
    planned_suspensions = get_planned_suspensions()

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
    print("\n[2] 気象庁データ取得中...")
    warnings = get_jma_warnings()
    print(f"  注意報: {len(warnings)}件")

    jma_waves = get_jma_forecast_waves()
    print(f"  気象庁波高テキスト: {jma_waves}")

    jma_prob = get_jma_probability()
    print(f"  早期注意情報（波浪）: 明日={jma_prob.get('明日',{}).get('level','なし')} / 明後日={jma_prob.get('明後日',{}).get('level','なし')}")

    # 早期注意情報で「高」があれば短期リスクとして扱う
    prob_risk = any(v.get("level") == "高" for v in jma_prob.values())

    # 2-2. 台風進路取得（日付別の慶良間接近フロア）
    print("\n[2-2] 台風進路取得中...")
    typhoon_by_date = get_typhoon_forecast()
    if typhoon_by_date:
        for _d in sorted(typhoon_by_date):
            _t = typhoon_by_date[_d]
            print(f"  {_d}: {_t['name_ja']} tier{_t['tier']} "
                  f"距離{_t['dist_km']}km フロア高速船{_t['hs_floor']}%/フェリー{_t['fe_floor']}%")
    else:
        print("  活動中の台風で慶良間に影響するものなし")

    # 3. 分析
    print("\n[3] データ分析中...")
    analysis = analyze_all_data(combined_data, warnings)

    # 3-2. 台風フロアを analysis に反映（波モデルが穏やかでも進路がかぶる日はリスクを立てる）
    for _date, _tinfo in typhoon_by_date.items():
        node = analysis.get(_date)
        if not node or not node.get("all_day"):
            continue
        node["all_day"]["typhoon"] = _tinfo
        # tier3以下（予報円内）で高速船リスク、tier2以下（暴風域 or 予報円内＋暴風域）でフェリーも
        if _tinfo["tier"] <= 3:
            node["all_day"]["risk_highspeed"] = True
        if _tinfo["tier"] <= 2:
            node["all_day"]["risk_ferry"] = True

    today = datetime.now(JST).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%d")
    day_after = (datetime.now(JST) + timedelta(days=2)).strftime("%Y-%m-%d")

    today_data = analysis.get(today, {}).get("all_day")
    tomorrow_data = analysis.get(tomorrow, {}).get("all_day")
    day_after_data = analysis.get(day_after, {}).get("all_day")
    tomorrow_morning = analysis.get(tomorrow, {}).get("morning")
    tomorrow_afternoon = analysis.get(tomorrow, {}).get("afternoon")

    print(f"  本日: 波高{today_data['max_wave']}m / スコア{today_data['cancellation_score']}" if today_data else "  本日: データなし")
    print(f"  明日: 波高{tomorrow_data['max_wave']}m / スコア{tomorrow_data['cancellation_score']}" if tomorrow_data else "  明日: データなし")
    print(f"  明後日: 波高{day_after_data['max_wave']}m / スコア{day_after_data['cancellation_score']}" if day_after_data else "  明後日: データなし")

    # ---- 共通ヘルパー ----
    def risk_label(data, morning=None, afternoon=None):
        if not data:
            return "データなし"
        hs = "[!高速船]" if data["risk_highspeed"] else ""
        fe = "[!フェリー]" if data["risk_ferry"] else ""
        risk = " ".join(filter(None, [hs, fe])) or "[OK]"
        am_icon = "[!]" if morning and morning.get("risk_highspeed") else "[OK]"
        pm_icon = "[!]" if afternoon and afternoon.get("risk_highspeed") else "[OK]"
        am = f"午前:{morning['max_wave']}m{am_icon}" if morning else ""
        pm = f"午後:{afternoon['max_wave']}m{pm_icon}" if afternoon else ""
        time_detail = f" [{am} / {pm}]" if am and pm else ""
        return f"波{data['max_wave']}m うねり{data.get('max_swell','?')}m 風{data.get('max_wind','?')}m/s → {risk}{time_detail}"

    # ---- 短期リスク判定 ----
    # 台風フロアは analysis の risk_highspeed/risk_ferry に既に反映済み
    typhoon_short_risk = any(
        d in typhoon_by_date and typhoon_by_date[d]["tier"] <= 3
        for d in (tomorrow, day_after)
    )
    short_risk = (
        (tomorrow_data and (tomorrow_data["risk_highspeed"] or tomorrow_data["risk_ferry"])) or
        (day_after_data and (day_after_data["risk_highspeed"] or day_after_data["risk_ferry"])) or
        len(warnings) > 0 or
        prob_risk or
        typhoon_short_risk
    )

    # ---- 長期リスク判定（3〜7日先） ----
    long_risk_days = []
    for delta in range(3, 8):
        date = (datetime.now(JST) + timedelta(days=delta)).strftime("%Y-%m-%d")
        d = analysis.get(date, {}).get("all_day")
        if d and (d["risk_highspeed"] or d["cancellation_score"] >= 0.35):
            dt = datetime.now(JST) + timedelta(days=delta)
            long_risk_days.append({
                "date_label": dt.strftime("%-m/%-d"),
                "max_wave": d["max_wave"],
                "score": d["cancellation_score"],
            })
    long_risk = len(long_risk_days) > 0

    # ---- Slackメッセージ構築（短期＋長期を1通に） ----
    print("\n[4] Slackメッセージ構築中...")

    ferry_status = get_ferry_status_from_web()

    # 短期セクション
    short_section = f"""[{now.strftime('%m/%d %H:%M')}] Kucha フェリー予報

[明日] {risk_label(tomorrow_data, tomorrow_morning, tomorrow_afternoon)}
  気象庁: {jma_waves.get('明日', '未取得')}
  早期注意(波浪): {jma_prob.get('明日', {}).get('level', 'なし') or 'なし'}

[明後日] {risk_label(day_after_data)}
  気象庁: {jma_waves.get('明後日', '未取得')}
  早期注意(波浪): {jma_prob.get('明後日', {}).get('level', 'なし') or 'なし'}

[注意報] {'あり(!!)' if warnings else 'なし'}
[運航情報] {ferry_status[:60] if ferry_status else '未確認（8時以降に再確認）'}"""

    # 長期セクション
    if long_risk:
        risk_summary = " / ".join([f"{d['date_label']}波{d['max_wave']}m" for d in long_risk_days])
        long_section = f"""
--- 長期予報（3〜7日先）---
[懸念日] {risk_summary}
[早期注意] 明日:{jma_prob.get('明日',{}).get('level','なし')} / 明後日:{jma_prob.get('明後日',{}).get('level','なし')}"""
    else:
        long_section = "\n--- 長期予報（3〜7日先）---\n[長期] 懸念なし"

    # メッセージ生成（リスクありの場合のみ）
    message_section = ""
    if short_risk:
        print("  短期リスクあり → メッセージ生成中...")
        try:
            short_message = generate_shortterm_message(analysis, ferry_status, warnings)
            message_section += f"\n--- ゲスト向けメッセージ案（短期）---\n{short_message[:800]}"
        except Exception as e:
            print(f"  [警告] 短期メッセージ生成失敗: {e}")

    if long_risk:
        print("  長期リスクあり → メッセージ生成中...")
        try:
            result = generate_longterm_message(analysis, warnings)
            if result:
                long_message, _ = result
                message_section += f"\n--- ゲスト向けメッセージ案（長期）---\n{long_message[:800]}"
        except Exception as e:
            print(f"  [警告] 長期メッセージ生成失敗: {e}")

    # 台風セクション（活動中かつ慶良間に影響がある場合のみ）
    typhoon_section = ""
    if typhoon_by_date:
        tcnames = sorted({t["name_ja"] for t in typhoon_by_date.values()})
        lines = [f"\n--- 台風情報 ---", f"[台風] {' / '.join(tcnames)} 接近予報あり"]
        for _d in sorted(typhoon_by_date):
            _t = typhoon_by_date[_d]
            dt = datetime.strptime(_d, "%Y-%m-%d")
            if _t.get("extrapolated"):
                area = "進路外挿(参考)"
            elif _t["in_storm"]:
                area = "暴風域内"
            elif _t["in_circle"]:
                area = "予報円内"
            else:
                area = f"中心{_t['dist_km']}km"
            lines.append(
                f"  {dt.strftime('%-m/%-d')}: {area}（最接近{_t['dist_km']}km）"
                f" → 欠航フロア 高速船{_t['hs_floor']}%/フェリー{_t['fe_floor']}%"
            )
        typhoon_section = "\n".join(lines)

    # 1通にまとめて送信
    full_message = short_section + typhoon_section + long_section + message_section
    send_slack(full_message, emoji="")
    print("  ✅ Slack送信完了")

    # [DB] 日次運航記録（Google Sheets蓄積）
    print("\n[DB] 日次運航データ記録中...")
    _fc = None
    try:
        from operation_logger import log_daily_record
        from forecast_publisher import build_forecast_data
        _fc = build_forecast_data(analysis, jma_waves, jma_prob, planned_suspensions,
                                  typhoon_by_date=typhoon_by_date)
        log_daily_record(analysis, jma_waves, jma_prob, _fc)
    except Exception as e:
        print(f"  [警告] DB記録エラー: {e}")

    # 午後便（14:30 JST cron／旧13:00）は欠航リスクが高い場合のみInstagram投稿
    # 条件①: 明日 or 明後日の運航中船種の欠航確率が61%以上
    # 条件②: 当日便が気象理由で欠航（Google Sheetsの8:15記録を参照・weatherのみ対象）
    is_afternoon_run = now.hour >= 12
    if is_afternoon_run and _fc is not None:
        _short = _fc["short_term"]
        # 運航中船種の最大%で判定（運休船種は除外）。effective_max_pct に集約。
        from forecast_publisher import effective_max_pct
        max_hs = max(effective_max_pct(_short[0]), effective_max_pct(_short[1]))
        high_risk = max_hs >= 61

        # スプシから当日の欠航理由を確認（equipment/dockは対象外）
        actual_weather_cancel = False
        try:
            sheets_id = os.environ.get("GOOGLE_SHEETS_ID")
            svc_json  = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
            if sheets_id and svc_json:
                import gspread, json as _json
                from google.oauth2.service_account import Credentials as _Creds
                _creds = _Creds.from_service_account_info(
                    _json.loads(svc_json),
                    scopes=["https://spreadsheets.google.com/feeds",
                            "https://www.googleapis.com/auth/drive"]
                )
                _gc = gspread.authorize(_creds)
                _ws = _gc.open_by_key(sheets_id).worksheet("daily_operation_log")
                _today = now.strftime("%Y-%m-%d")
                _records = _ws.get_all_records()
                _today_rec = next((r for r in _records if r.get("date") == _today), None)
                if _today_rec:
                    _fc_reason  = _today_rec.get("ferry_cancel_reason", "none")
                    _hs_reason  = _today_rec.get("hs_cancel_reason", "none")
                    _fe_cancel  = _today_rec.get("ferry_operated") == 0
                    _hs_cancel  = (_today_rec.get("hs_bin1_operated") == 0 or
                                   _today_rec.get("hs_bin2_operated") == 0)
                    actual_weather_cancel = (
                        (_fe_cancel and _fc_reason == "weather") or
                        (_hs_cancel and _hs_reason == "weather")
                    )
                    print(f"  [午後便] スプシ確認: フェリー={_today_rec.get('ferry_operated')}({_fc_reason}) "
                          f"高速船1便={_today_rec.get('hs_bin1_operated')}({_hs_reason})")
        except Exception as e:
            print(f"  [警告] スプシ欠航確認エラー（スキップ）: {e}")

        post_to_social = high_risk or actual_weather_cancel
        reason = []
        if high_risk:              reason.append(f"高速船リスク最大{max_hs}% ≥ 61%")
        if actual_weather_cancel:  reason.append("当日便が気象欠航")
        if post_to_social:
            print(f"  [午後便] Instagram投稿あり（{' / '.join(reason)}）")
        else:
            print(f"  [午後便] 高速船リスク最大{max_hs}% < 61% かつ気象欠航なし → Instagram投稿スキップ")
    else:
        post_to_social = True  # 8:15は常に投稿 / _fc取得失敗時は安全側（投稿する）

    # 画像生成・SNS投稿
    print("\n[5] Publisher実行中...")
    try:
        from forecast_publisher import run_publisher
        run_publisher(analysis, jma_waves, jma_prob,
                      planned_suspensions=planned_suspensions,
                      post_to_social=post_to_social,
                      typhoon_by_date=typhoon_by_date)
    except Exception as e:
        print(f"  [警告] Publisher実行エラー: {e}")

    print("\n処理完了。")


if __name__ == "__main__":
    run_ferry_check()
