"""
yaeyama_publisher.py
八重山全航路（route1/3/5/6/7）の欠航リスク予報をInstagramに投稿する。
yaeyama_logger.py の log_daily_records() から呼び出す。

投稿: 3枚カルーセル
  1枚目: 短期予報（明日・明後日 × 5航路）
  2枚目: 長期予報（3〜7日先）
  3枚目: 予報根拠データ（気象数値）

API節約戦略:
  - Day1 (明日): yaeyama_loggerが取得済みの weather データを route_data_list 経由で受け取る
  - Day2〜7: Open-Meteo batched request（5航路を1リクエストに集約）
"""

import os
import json
import math
import time
import base64
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont

JST = ZoneInfo("Asia/Tokyo")

# ============================================================
# 設定
# ============================================================

MODEL_ROUTES = ["route1", "route3", "route5", "route6", "route7"]

ROUTE_INFO = {
    "route1": {"name": "大原（西表島東）", "short": "大原",  "en": "Ohara",    "lat": 24.28,      "lon": 124.13},
    "route3": {"name": "竹富島",           "short": "竹富島", "en": "Taketomi", "lat": 24.36,      "lon": 124.10},
    "route5": {"name": "上原（西表島北）", "short": "上原",  "en": "Uehara",   "lat": 24.40,      "lon": 123.86},
    "route6": {"name": "波照間島",         "short": "波照間", "en": "Hateruma", "lat": 24.165974,  "lon": 123.836266},
    "route7": {"name": "鳩間島",           "short": "鳩間島", "en": "Hatoma",   "lat": 24.47,      "lon": 123.80},
}

IMG_SIZE = (1080, 1080)

# ============================================================
# フォント
# ============================================================

def _find_noto_font(weights):
    search_dirs = [
        "/usr/share/fonts/opentype/noto",
        "/usr/share/fonts/noto-cjk",
        "/usr/share/fonts/truetype/noto",
        "/usr/share/fonts/noto",
        "/usr/local/share/fonts/noto",
        "/usr/share/fonts/opentype",
        "/usr/share/fonts/truetype",
    ]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for w in weights:
            for ext in [".ttc", ".otf", ".ttf"]:
                p = os.path.join(d, f"NotoSansCJK-{w}{ext}")
                if os.path.exists(p):
                    return p
    try:
        import subprocess
        out = subprocess.check_output(
            ["fc-list", ":lang=ja", "--format=%{file}\n"],
            text=True, timeout=5, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            line = line.strip()
            if line and "Noto" in line and "Sans" in line:
                return line
    except Exception:
        pass
    return None


FONT_REGULAR = _find_noto_font(["Regular"])
FONT_BOLD    = _find_noto_font(["Black", "Bold"])
FONT_MEDIUM  = _find_noto_font(["Medium", "Regular"])


def _load_font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ============================================================
# カラー
# ============================================================

def _get_bg_color(pct):
    if pct is None or pct <= 30:
        return "#2E7D32"
    elif pct <= 60:
        return "#F9A825"
    elif pct <= 80:
        return "#E65100"
    else:
        return "#B71C1C"


def _get_risk_text_color(pct):
    if pct is None or pct <= 30:
        return "#66FF80"
    elif pct <= 60:
        return "#FFD54F"
    elif pct <= 80:
        return "#FF8A50"
    else:
        return "#FF6666"


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


# ============================================================
# モデル推論（sklearn不要）
# ============================================================

_MODEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yaeyama_cancel_model.json")


def _load_model():
    try:
        with open(_MODEL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [警告] モデル読み込み失敗: {e}")
        return None


def _predict_prob(model_params, wave, swell, wind, swell_period=None):
    if not model_params or wave is None or wind is None:
        return None
    mtype = model_params.get("model_type", "logistic")
    if mtype == "rule":
        p = model_params
        score = 0.0
        if wave >= p["wave_thr_high"]:
            score = p["prob_wave_high"]
        elif wave >= p["wave_thr_mid"]:
            score = p["prob_wave_mid"]
        elif wave >= p["wave_thr_mid"] * 0.8:
            score = p["prob_wave_mid"] * 0.5
        if wind >= p["wind_thr"]:
            score = min(score + p["prob_wind_add"], 0.95)
        return round(score, 3)
    else:
        m = model_params
        feat_vals = {
            "wave_max": wave, "swell_max": swell,
            "wind_max": wind, "swell_period_max": swell_period,
        }
        try:
            vals = [feat_vals[f] for f in m["features"]]
            if any(v is None for v in vals):
                return None
            x_s = [(v - mu) / sc
                   for v, mu, sc in zip(vals, m["scaler_mean"], m["scaler_scale"])]
            z = m["intercept"] + sum(c * x for c, x in zip(m["coef"], x_s))
            return round(1.0 / (1.0 + math.exp(-z)), 3)
        except Exception:
            return None


# ============================================================
# Open-Meteo: batched 7日間予報取得
# ============================================================

def _fetch_forecast_batched(lats, lons, days=7, timeout=30, max_retries=3):
    """
    複数座標を1リクエストに集約して 7日分の波高・うねり・風速を返す。
    戻り値: {(lat, lon): [{"date", "max_wave", "max_swell", "max_wind"}, ...]}
    """
    lat_str = ",".join(str(la) for la in lats)
    lon_str = ",".join(str(lo) for lo in lons)
    now     = datetime.now(JST)

    marine_data  = None
    weather_data = None

    marine_url = (
        f"https://marine-api.open-meteo.com/v1/marine"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&hourly=wave_height,swell_wave_height"
        f"&timezone=Asia%2FTokyo&forecast_days={days}"
    )
    weather_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat_str}&longitude={lon_str}"
        f"&hourly=wind_speed_10m"
        f"&wind_speed_unit=ms"
        f"&timezone=Asia%2FTokyo&forecast_days={days}"
    )

    def _as_list(d):
        return d if isinstance(d, list) else [d]

    for attempt in range(max_retries):
        try:
            if marine_data is None:
                marine_data = _as_list(requests.get(marine_url, timeout=timeout).json())
            if weather_data is None:
                weather_data = _as_list(requests.get(weather_url, timeout=timeout).json())
            break
        except Exception as e:
            print(f"  [警告] batched予報取得エラー (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))

    result = {}
    for idx, (la, lo) in enumerate(zip(lats, lons)):
        day_list = []
        for delta in range(days):
            target = (now + timedelta(days=delta)).strftime("%Y-%m-%d")

            def _max(data, key_name, loc_idx):
                if not data or loc_idx >= len(data):
                    return None
                loc   = data[loc_idx]
                times = loc.get("hourly", {}).get("time", [])
                vals  = loc.get("hourly", {}).get(key_name, [])
                v = [v for t, v in zip(times, vals) if t.startswith(target) and v is not None]
                return round(max(v), 2) if v else None

            day_list.append({
                "date":      target,
                "max_wave":  _max(marine_data,  "wave_height",       idx),
                "max_swell": _max(marine_data,  "swell_wave_height", idx),
                "max_wind":  _max(weather_data, "wind_speed_10m",    idx),
            })
        result[(la, lo)] = day_list

    return result


# ============================================================
# 予報データ構築
# ============================================================

def _pct(prob):
    if prob is None:
        return None
    return max(1, min(int(round(prob * 100)), 99))  # 1〜99%（0%/100%は確定表現を避ける）


def _max_pct(probs_by_route, day_indices):
    vals = []
    for rid in MODEL_ROUTES:
        for i in day_indices:
            p = probs_by_route.get(rid, [None] * 7)
            if i < len(p) and p[i] is not None:
                vals.append(_pct(p[i]))
    return max(vals) if vals else 0


def _build_forecast_data(route_data_list, cancel_models):
    """
    5航路 × 7日分の欠航確率を計算する。

    - Day 1 (明日): route_data_list の天気データを使用（ロガー取得済み）
    - Day 0, 2〜6: batched API 1回で全5航路分を取得
    戻り値: {route_id: [prob_day0, ..., prob_day6]}
    """
    # Day1: ロガー取得済みデータを抽出
    day1_weather = {}
    if route_data_list:
        for rid, _op, w in route_data_list:
            if rid in MODEL_ROUTES:
                day1_weather[rid] = w

    # Day0, 2〜7: batched fetch（6/7まで含める = 8日分）
    lats = [ROUTE_INFO[rid]["lat"] for rid in MODEL_ROUTES]
    lons = [ROUTE_INFO[rid]["lon"] for rid in MODEL_ROUTES]
    print("  [API] 8日間予報 batched取得中（5航路×1リクエスト）...")
    batched = _fetch_forecast_batched(lats, lons, days=8)

    result = {}
    for i_rid, rid in enumerate(MODEL_ROUTES):
        info  = ROUTE_INFO[rid]
        m_hs  = (cancel_models or {}).get(rid, {}).get("hs")
        days8 = batched.get((info["lat"], info["lon"]), [{}] * 8)
        probs = []
        for delta in range(8):
            if delta == 1 and rid in day1_weather:
                # Day1: ロガー取得済みデータ優先
                w     = day1_weather[rid]
                wave  = w.get("tmr_max_wave")
                swell = w.get("tmr_max_swell")
                wind  = w.get("tmr_max_wind")
            else:
                d     = days8[delta] if delta < len(days8) else {}
                wave  = d.get("max_wave")
                swell = d.get("max_swell")
                wind  = d.get("max_wind")
            p = _predict_prob(m_hs, wave, swell, wind)
            probs.append(p)
        result[rid] = probs
        print(f"  [{rid}] 明日:{_pct(probs[1])}%  明後日:{_pct(probs[2])}%")

    return result, batched


# ============================================================
# 画像生成
# ============================================================

def _fonts():
    return {
        "title":    _load_font(FONT_BOLD,    42),
        "title_en": _load_font(FONT_MEDIUM,  24),
        "head":     _load_font(FONT_BOLD,    30),
        "head_en":  _load_font(FONT_REGULAR, 20),
        "route":    _load_font(FONT_MEDIUM,  26),
        "pct_med":  _load_font(FONT_BOLD,    56),
        "pct_sm":   _load_font(FONT_BOLD,    40),
        "label":    _load_font(FONT_REGULAR, 20),
        "bar":      _load_font(FONT_REGULAR, 22),
        "xs":       _load_font(FONT_REGULAR, 17),
        "sec":      _load_font(FONT_BOLD,    22),
        "val_bold": _load_font(FONT_BOLD,    28),
    }


def make_image_short(probs_by_route, output_path):
    """画像①: 短期予報（5航路 × 明日/明後日）"""
    now      = datetime.now(JST)
    max_risk = _max_pct(probs_by_route, [1, 2])
    img  = Image.new("RGB", IMG_SIZE, color=_hex_to_rgb(_get_bg_color(max_risk)))
    draw = ImageDraw.Draw(img)
    f    = _fonts()

    # タイトル
    draw.text((540, 44),  "八重山航路（石垣島発着便）欠航リスク予報",
              font=f["title"], fill="white", anchor="mm")
    draw.text((540, 88),  "Yaeyama Routes (Ishigaki-based)  /  Cancellation Risk Forecast",
              font=f["title_en"], fill=(255,255,255,200), anchor="mm")
    draw.line([(60, 110), (1020, 110)], fill=(255,255,255,80), width=1)

    # 日付ヘッダー
    DAY_JA  = ["（月）","（火）","（水）","（木）","（金）","（土）","（日）"]
    DAY_EN  = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    MON_EN  = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    tmr      = now + timedelta(days=1)
    dayafter = now + timedelta(days=2)
    tmr_date_ja      = f"{tmr.month}/{tmr.day}{DAY_JA[tmr.weekday()]}"
    dayafter_date_ja = f"{dayafter.month}/{dayafter.day}{DAY_JA[dayafter.weekday()]}"
    tmr_date_en      = f"{MON_EN[tmr.month-1]} {tmr.day} ({DAY_EN[tmr.weekday()]})"
    dayafter_date_en = f"{MON_EN[dayafter.month-1]} {dayafter.day} ({DAY_EN[dayafter.weekday()]})"

    COL_NAME = 190
    COL_TMR  = 540
    COL_DAY2 = 875
    HDR_Y    = 132

    draw.text((COL_TMR,  HDR_Y),      "明日  /  Tomorrow",  font=f["head"],    fill="white", anchor="mm")
    draw.text((COL_TMR,  HDR_Y + 30), tmr_date_ja,          font=f["head_en"], fill=(255,255,255,200), anchor="mm")
    draw.text((COL_TMR,  HDR_Y + 50), tmr_date_en,          font=f["head_en"], fill=(255,255,255,160), anchor="mm")
    draw.text((COL_DAY2, HDR_Y),      "明後日  /  Day After",font=f["head"],    fill="white", anchor="mm")
    draw.text((COL_DAY2, HDR_Y + 30), dayafter_date_ja,     font=f["head_en"], fill=(255,255,255,200), anchor="mm")
    draw.text((COL_DAY2, HDR_Y + 50), dayafter_date_en,     font=f["head_en"], fill=(255,255,255,160), anchor="mm")

    draw.line([(360, 118), (360, 960)], fill=(255,255,255,50), width=1)
    draw.line([(710, 118), (710, 960)], fill=(255,255,255,50), width=1)

    # 航路行（5行）
    ROW_TOP = 210
    ROW_H   = 148
    for idx, rid in enumerate(MODEL_ROUTES):
        info  = ROUTE_INFO[rid]
        probs = probs_by_route.get(rid, [None] * 8)
        pct1  = _pct(probs[1])
        pct2  = _pct(probs[2])
        row_y = ROW_TOP + idx * ROW_H
        cy    = row_y + ROW_H // 2

        draw.line([(60, row_y), (1020, row_y)], fill=(255,255,255,35), width=1)

        draw.text((COL_NAME, cy - 12), info["short"],
                  font=f["route"], fill="white", anchor="mm")
        draw.text((COL_NAME, cy + 18), info["en"],
                  font=_load_font(FONT_REGULAR, 17), fill=(255,255,255,150), anchor="mm")

        for col_x, pct in [(COL_TMR, pct1), (COL_DAY2, pct2)]:
            if pct is not None:
                draw.text((col_x, cy - 12), f"{pct}%",
                          font=f["pct_med"], fill=_get_risk_text_color(pct), anchor="mm")
            else:
                draw.text((col_x, cy - 12), "—",
                          font=f["pct_sm"], fill=(200,200,200), anchor="mm")

    draw.line([(60, ROW_TOP + 5 * ROW_H), (1020, ROW_TOP + 5 * ROW_H)],
              fill=(255,255,255,35), width=1)

    FOOTER_Y = ROW_TOP + 5 * ROW_H + 20
    draw.text((540, FOOTER_Y + 18), "※AI予測・参考値。欠航判断は安栄観光公式HPをご確認ください。",
              font=f["xs"], fill=(255,255,255,140), anchor="mm")
    draw.text((540, FOOTER_Y + 38), "*AI estimates. Check Anei Kanko official for cancellations.",
              font=f["xs"], fill=(255,255,255,110), anchor="mm")

    img.save(output_path)
    print(f"  画像①保存: {output_path}")


def make_image_longterm(probs_by_route, output_path):
    """
    画像②: 長期予報（3〜7日先）
    レイアウト: 5航路（行）× 5日（列）のテーブル形式
    """
    now    = datetime.now(JST)
    DAY_JA = ["月","火","水","木","金","土","日"]
    DAY_EN = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    MON_EN = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    # 3〜7日先（delta 3,4,5,6,7）の日付・リスク
    lt_deltas = list(range(3, 8))
    lt_dates  = [now + timedelta(days=d) for d in lt_deltas]

    # 全セルのリスク値（5routes × 5days）
    all_pcts = []
    for rid in MODEL_ROUTES:
        probs = probs_by_route.get(rid, [None] * 8)
        for delta in lt_deltas:
            p = _pct(probs[delta]) if delta < len(probs) else None
            if p is not None:
                all_pcts.append(p)

    max_risk = max(all_pcts) if all_pcts else 0
    img  = Image.new("RGB", IMG_SIZE, color=_hex_to_rgb(_get_bg_color(max_risk)))
    draw = ImageDraw.Draw(img)
    f    = _fonts()

    # ── タイトル ──
    draw.text((540, 44),  "長期予報（3〜7日先）",
              font=f["title"], fill="white", anchor="mm")
    draw.text((540, 86),  "Yaeyama Routes (Ishigaki-based)  /  Long-term Cancellation Risk  (3-7 days ahead)",
              font=f["title_en"], fill=(255,255,255,200), anchor="mm")
    draw.line([(40, 108), (1040, 108)], fill=(255,255,255,80), width=1)

    # ── テーブルレイアウト ──
    # 列: 1ラベル列(220px) + 5日列(156px×5)
    # 行: 1ヘッダー行(72px) + 5航路行(150px×5)
    TBL_X    = 40          # テーブル左端
    TBL_W    = 1000        # テーブル幅
    LABEL_W  = 220         # 航路名列の幅
    COL_W    = (TBL_W - LABEL_W) // 5   # 156px
    HDR_Y    = 118         # ヘッダー行トップ
    HDR_H    = 72
    ROW_Y0   = HDR_Y + HDR_H  # データ行開始 y = 190
    ROW_H    = 150

    # ヘッダー行背景
    draw.rectangle([(TBL_X, HDR_Y), (TBL_X + TBL_W, HDR_Y + HDR_H)],
                   fill=(0, 0, 0, 70))

    # ── 列ヘッダー（日付）──
    for ci, dt in enumerate(lt_dates):
        cx = TBL_X + LABEL_W + ci * COL_W + COL_W // 2
        cy = HDR_Y + HDR_H // 2
        date_ja = f"{dt.month}/{dt.day}({DAY_JA[dt.weekday()]})"
        date_en = f"{MON_EN[dt.month-1]} {dt.day} ({DAY_EN[dt.weekday()]})"
        draw.text((cx, cy - 10), date_ja, font=_load_font(FONT_BOLD, 22),
                  fill="white", anchor="mm")
        draw.text((cx, cy + 16), date_en, font=_load_font(FONT_REGULAR, 16),
                  fill=(255,255,255,170), anchor="mm")

    # ── 縦罫線 ──
    for ci in range(6):
        lx = TBL_X + LABEL_W + ci * COL_W
        draw.line([(lx, HDR_Y), (lx, ROW_Y0 + 5 * ROW_H)],
                  fill=(255,255,255,45), width=1)
    # ラベル列右端罫線
    draw.line([(TBL_X + LABEL_W, HDR_Y), (TBL_X + LABEL_W, ROW_Y0 + 5 * ROW_H)],
              fill=(255,255,255,70), width=1)

    # ── 航路行 ──
    for ri, rid in enumerate(MODEL_ROUTES):
        info  = ROUTE_INFO[rid]
        probs = probs_by_route.get(rid, [None] * 8)
        row_y = ROW_Y0 + ri * ROW_H
        cy    = row_y + ROW_H // 2

        # 行区切り
        draw.line([(TBL_X, row_y), (TBL_X + TBL_W, row_y)],
                  fill=(255,255,255,40), width=1)

        # 航路名
        draw.text((TBL_X + LABEL_W // 2, cy - 12), info["short"],
                  font=f["route"], fill="white", anchor="mm")
        draw.text((TBL_X + LABEL_W // 2, cy + 18), info["en"],
                  font=_load_font(FONT_REGULAR, 17), fill=(255,255,255,150), anchor="mm")

        # セル（日付ごとの%）
        for ci, delta in enumerate(lt_deltas):
            pct = _pct(probs[delta]) if delta < len(probs) else None
            cx  = TBL_X + LABEL_W + ci * COL_W + COL_W // 2
            if pct is not None:
                draw.text((cx, cy), f"{pct}%",
                          font=_load_font(FONT_BOLD, 38), fill=_get_risk_text_color(pct), anchor="mm")
            else:
                draw.text((cx, cy), "—",
                          font=_load_font(FONT_BOLD, 30), fill=(180,180,180), anchor="mm")

    # 最終行下罫線
    draw.line([(TBL_X, ROW_Y0 + 5 * ROW_H), (TBL_X + TBL_W, ROW_Y0 + 5 * ROW_H)],
              fill=(255,255,255,40), width=1)

    FOOTER_Y = ROW_Y0 + 5 * ROW_H + 14
    draw.text((540, FOOTER_Y + 18), "※AI予測・参考値。欠航判断は安栄観光公式HPをご確認ください。",
              font=f["xs"], fill=(255,255,255,140), anchor="mm")
    draw.text((540, FOOTER_Y + 38), "*AI-based estimates. Check Anei Kanko official for cancellations.",
              font=f["xs"], fill=(255,255,255,110), anchor="mm")

    img.save(output_path)
    print(f"  画像②保存: {output_path}")


def make_image_weatherdata(probs_by_route, batched_forecast, output_path):
    """
    画像③: 予報根拠データ
    - セクションA: 明日 + 明後日 の5航路 × 波高/うねり/風速（欠航リスク%は表示しない）
    - セクションB: 3〜7日先の5航路 × 波高
    """
    now      = datetime.now(JST)
    DAY_JA   = ["月","火","水","木","金","土","日"]
    DAY_EN   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    MON_EN   = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    tmr      = now + timedelta(days=1)
    dayafter = now + timedelta(days=2)

    img  = Image.new("RGB", IMG_SIZE, color=_hex_to_rgb("#0A1628"))
    draw = ImageDraw.Draw(img)
    f    = _fonts()

    def _fmt(v): return f"{v:.1f}" if v is not None else "—"

    # ── タイトル ──
    draw.text((540, 50), "予報根拠データ  /  Forecast Data",
              font=f["title"], fill="white", anchor="mm")
    draw.text((540, 86), "気象データに基づくリスク根拠  /  Weather basis for cancellation risk forecast",
              font=_load_font(FONT_REGULAR, 17), fill=(255, 255, 255, 155), anchor="mm")
    draw.line([(40, 106), (1040, 106)], fill="#334E7A", width=2)

    # ── セクションA: 明日 & 明後日（横並び） ──
    RX = 40; RW = 130; CW = 141; SEP = 22
    DAY0_X = RX + RW           # 170
    DAY1_X = DAY0_X + 3 * CW + SEP  # 615

    # 日付セクションヘッダー (y=110〜152)
    HDR_DT_Y = 110; HDR_DT_H = 42
    for xi, dt, ja, en in [
        (DAY0_X, tmr,      "明日",   "Tomorrow"),
        (DAY1_X, dayafter, "明後日", "Day After"),
    ]:
        cx = xi + int(1.5 * CW)
        draw.rectangle([(xi, HDR_DT_Y), (xi + 3 * CW, HDR_DT_Y + HDR_DT_H)], fill="#1A3057")
        draw.text((cx, HDR_DT_Y + 14),
                  f"{ja}  {dt.month}/{dt.day}（{DAY_JA[dt.weekday()]}）",
                  font=_load_font(FONT_BOLD, 21), fill="#7EB3F5", anchor="mm")
        draw.text((cx, HDR_DT_Y + 32),
                  f"{en}  {MON_EN[dt.month-1]} {dt.day} ({DAY_EN[dt.weekday()]})",
                  font=_load_font(FONT_REGULAR, 15), fill="#5585B5", anchor="mm")

    # 列ヘッダー (y=152〜194)
    HDR_COL_Y = HDR_DT_Y + HDR_DT_H; HDR_COL_H = 42
    draw.rectangle([(RX, HDR_COL_Y), (1040, HDR_COL_Y + HDR_COL_H)], fill="#102035")
    draw.text((RX + RW // 2, HDR_COL_Y + HDR_COL_H // 2), "航路\nRoute",
              font=_load_font(FONT_REGULAR, 14), fill="#7EB3F5", anchor="mm")
    for base_x in [DAY0_X, DAY1_X]:
        for ci, (ja, en) in enumerate([
            ("波高 (m)", "Wave Ht."),
            ("うねり (m)", "Swell Ht."),
            ("風速 (m/s)", "Wind Spd."),
        ]):
            cx = base_x + ci * CW + CW // 2
            draw.text((cx, HDR_COL_Y + 14), ja,
                      font=_load_font(FONT_REGULAR, 15), fill="#7EB3F5", anchor="mm")
            draw.text((cx, HDR_COL_Y + 30), en,
                      font=_load_font(FONT_REGULAR, 13), fill="#5585B5", anchor="mm")

    # データ行 (y=194〜 )
    ROW_A_Y = HDR_COL_Y + HDR_COL_H; ROW_A_H = 84

    for idx, rid in enumerate(MODEL_ROUTES):
        info  = ROUTE_INFO[rid]
        days8 = batched_forecast.get((info["lat"], info["lon"]), [{}] * 8)
        row_y = ROW_A_Y + idx * ROW_A_H
        cy    = row_y + ROW_A_H // 2

        if idx % 2 == 1:
            draw.rectangle([(RX, row_y), (1040, row_y + ROW_A_H)],
                           fill=(255, 255, 255, 10))
        draw.line([(RX, row_y), (1040, row_y)],
                  fill=(255, 255, 255, 22), width=1)

        draw.text((RX + RW // 2, cy - 8), info["short"],
                  font=_load_font(FONT_BOLD, 22), fill="#BBDEFB", anchor="mm")
        draw.text((RX + RW // 2, cy + 14), info["en"],
                  font=_load_font(FONT_REGULAR, 15), fill="#5585B5", anchor="mm")

        for delta, base_x in [(1, DAY0_X), (2, DAY1_X)]:
            d    = days8[delta] if delta < len(days8) else {}
            vals = [d.get("max_wave"), d.get("max_swell"), d.get("max_wind")]
            thrs = [2.5, 2.0, 12.0]
            for ci, (val, thr) in enumerate(zip(vals, thrs)):
                cx  = base_x + ci * CW + CW // 2
                col = "#FF8A80" if (val is not None and val >= thr) else "#E0E0E0"
                draw.text((cx, cy), _fmt(val),
                          font=_load_font(FONT_BOLD, 24), fill=col, anchor="mm")

    SEC_A_BTM = ROW_A_Y + 5 * ROW_A_H   # 194 + 420 = 614
    draw.line([(RX, SEC_A_BTM), (1040, SEC_A_BTM)],
              fill=(255, 255, 255, 28), width=1)

    # ── セクションB: 3〜7日先 波高 ──
    SEC_B_Y = SEC_A_BTM + 8; SEC_B_H = 36
    draw.rectangle([(RX, SEC_B_Y), (1040, SEC_B_Y + SEC_B_H)], fill="#142240")
    draw.text((540, SEC_B_Y + SEC_B_H // 2),
              "3〜7日先の波高  /  Wave Height Outlook (3-7 days ahead, m)",
              font=_load_font(FONT_BOLD, 18), fill="#7EB3F5", anchor="mm")

    HDR_B_Y = SEC_B_Y + SEC_B_H; HDR_B_H = 38
    draw.rectangle([(RX, HDR_B_Y), (1040, HDR_B_Y + HDR_B_H)], fill="#0F1C33")
    RW2 = 130; CW_B = (1000 - RW2) // 5   # 174px
    DAY_COLS = [now + timedelta(days=d) for d in range(3, 8)]
    for ci, dt in enumerate(DAY_COLS):
        cx = RX + RW2 + ci * CW_B + CW_B // 2
        draw.text((cx, HDR_B_Y + 13),
                  f"{dt.month}/{dt.day}（{DAY_JA[dt.weekday()]}）",
                  font=_load_font(FONT_BOLD, 17), fill="#7EB3F5", anchor="mm")
        draw.text((cx, HDR_B_Y + 29),
                  f"{MON_EN[dt.month-1]} {dt.day}",
                  font=_load_font(FONT_REGULAR, 13), fill="#5585B5", anchor="mm")

    ROW_B_Y = HDR_B_Y + HDR_B_H; ROW_B_H = 50

    for idx, rid in enumerate(MODEL_ROUTES):
        info  = ROUTE_INFO[rid]
        days8 = batched_forecast.get((info["lat"], info["lon"]), [{}] * 8)
        row_y = ROW_B_Y + idx * ROW_B_H
        cy    = row_y + ROW_B_H // 2

        if idx % 2 == 1:
            draw.rectangle([(RX, row_y), (1040, row_y + ROW_B_H)],
                           fill=(255, 255, 255, 8))
        draw.line([(RX, row_y), (1040, row_y)],
                  fill=(255, 255, 255, 18), width=1)

        draw.text((RX + RW2 // 2, cy), info["short"],
                  font=_load_font(FONT_MEDIUM, 19), fill="#BBDEFB", anchor="mm")

        for ci, delta in enumerate(range(3, 8)):
            d    = days8[delta] if delta < len(days8) else {}
            wave = d.get("max_wave")
            cx   = RX + RW2 + ci * CW_B + CW_B // 2
            col  = "#FF8A80" if (wave is not None and wave >= 2.5) else "#E0E0E0"
            draw.text((cx, cy), _fmt(wave),
                      font=_load_font(FONT_BOLD, 20), fill=col, anchor="mm")

    SEC_B_BTM = ROW_B_Y + 5 * ROW_B_H
    draw.line([(RX, SEC_B_BTM), (1040, SEC_B_BTM)],
              fill=(255, 255, 255, 22), width=1)

    # 情報源バー
    SRC_Y = SEC_B_BTM + 8
    draw.rectangle([(RX, SRC_Y), (1040, SRC_Y + 38)], fill="#1A3057")
    draw.text((60, SRC_Y + 19),
              "【情報源】  Open-Meteo Marine API  /  安栄観光 aneikankou.co.jp",
              font=f["xs"], fill="#7EB3F5", anchor="lm")

    FOOT_Y = SRC_Y + 48
    draw.text((540, FOOT_Y),
              "※欠航判断は安栄観光が行います。本データはAI予測の参考値です。",
              font=_load_font(FONT_REGULAR, 14), fill="#546E7A", anchor="mm")
    draw.text((540, FOOT_Y + 18),
              "*Cancellation determined by Anei Kanko. Weather data for reference only.",
              font=_load_font(FONT_REGULAR, 13), fill="#455A64", anchor="mm")
    draw.text((540, FOOT_Y + 36),
              f"生成: {now.strftime('%Y-%m-%d %H:%M')} JST",
              font=_load_font(FONT_REGULAR, 13), fill="#37474F", anchor="mm")

    img.save(output_path)
    print(f"  画像③保存: {output_path}")


# ============================================================
# GitHub Pages へ画像アップロード（常に実行）
# ============================================================

def _upload_images_to_github(image_paths):
    """画像を GitHub の images/ にアップし、公開 URL リストを返す。失敗時は空リスト。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo  = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("  [スキップ] GITHUB_TOKEN / GITHUB_REPOSITORY 未設定")
        return []

    owner     = repo.split("/")[0]
    repo_name = repo.split("/")[1]
    headers   = {"Authorization": f"token {token}",
                 "Accept": "application/vnd.github.v3+json"}
    urls = []

    for path in image_paths:
        filename = os.path.basename(path)
        try:
            with open(path, "rb") as fh:
                content = base64.b64encode(fh.read()).decode()

            target_path = f"images/{filename}"
            api_url     = f"https://api.github.com/repos/{repo}/contents/{target_path}"

            existing = requests.get(api_url, headers=headers)
            sha = existing.json().get("sha") if existing.status_code == 200 else None

            data = {"message": f"Auto: {filename}", "content": content, "branch": "main"}
            if sha:
                data["sha"] = sha

            resp = requests.put(api_url, json=data, headers=headers)
            if resp.status_code in (200, 201):
                page_url = f"https://{owner}.github.io/{repo_name}/{target_path}"
                urls.append(page_url)
                print(f"    ✅ {page_url}")
            else:
                print(f"    [警告] アップロード失敗 ({filename}): {resp.status_code}")
        except Exception as e:
            print(f"    [警告] アップロードエラー ({filename}): {e}")

    return urls


# ============================================================
# Instagram 投稿
# ============================================================

def _post_to_instagram(image_urls, caption):
    """GitHub Pages URL を使ってカルーセル投稿する。"""
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    user_id      = os.environ.get("INSTAGRAM_USER_ID")

    if not access_token or not user_id:
        print("  [スキップ] INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID 未設定")
        return False
    if not image_urls:
        print("  [スキップ] 画像URLなし（GitHub Pagesアップロード失敗）")
        return False

    try:
        # GitHub Pages が実際にファイルを配信するまでポーリング（最大5分）
        check_url = image_urls[0]
        print(f"  [Instagram] GitHub Pages 配信確認中（最大5分）: {check_url}")
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                r = requests.head(check_url, timeout=10, allow_redirects=True)
                if r.status_code == 200:
                    print(f"  [Instagram] 配信確認OK（{r.status_code}）→ Instagram投稿開始")
                    break
                print(f"  [Instagram] まだ未配信（{r.status_code}）... 15秒後再確認")
            except Exception:
                print("  [Instagram] 疎通確認エラー... 15秒後再確認")
            time.sleep(15)
        else:
            print("  [警告] GitHub Pages 5分待機タイムアウト。そのまま試行します")

        media_ids = []
        for img_url in image_urls:
            resp = requests.post(
                f"https://graph.facebook.com/v25.0/{user_id}/media",
                params={"image_url": img_url, "is_carousel_item": "true",
                        "access_token": access_token}
            )
            data = resp.json()
            if "id" not in data:
                print(f"  [エラー] メディアコンテナ作成失敗: {data}")
                return False
            media_ids.append(data["id"])
            print(f"  メディアコンテナ: {data['id']}")

        resp = requests.post(
            f"https://graph.facebook.com/v25.0/{user_id}/media",
            params={"media_type": "CAROUSEL", "children": ",".join(media_ids),
                    "caption": caption, "access_token": access_token}
        )
        data = resp.json()
        if "id" not in data:
            print(f"  [エラー] カルーセルコンテナ作成失敗: {data}")
            return False
        carousel_id = data["id"]

        print("  [Instagram] 処理待機（30秒）...")
        time.sleep(30)

        resp = requests.post(
            f"https://graph.facebook.com/v25.0/{user_id}/media_publish",
            params={"creation_id": carousel_id, "access_token": access_token}
        )
        data = resp.json()
        if "id" not in data:
            print(f"  [エラー] 投稿失敗: {data}")
            return False

        print(f"  ✅ Instagram投稿完了: post_id={data['id']}")
        return True

    except Exception as e:
        print(f"  [警告] Instagram投稿エラー: {e}")
        return False


# ============================================================
# キャプション
# ============================================================

_CAUTION_KEYWORDS = [
    "欠航", "運休", "時化", "台風", "低気圧", "未定", "見込み", "引き返す", "条件付",
]


def _is_notable_caution(text):
    """通常運航以外の重要なお知らせかどうか判定"""
    if not text:
        return False
    return any(kw in text for kw in _CAUTION_KEYWORDS)


def _build_caption(probs_by_route, now, caution_text=None):
    tmr    = now + timedelta(days=1)
    DAY_JA = ["月","火","水","木","金","土","日"]
    DAY_EN = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    MON_EN = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    lines = [
        f"🚢 八重山航路（石垣島発着便）欠航リスク予報  {tmr.month}/{tmr.day}({DAY_JA[tmr.weekday()]})",
        f"🚢 Yaeyama Routes (Ishigaki-based)  Cancellation Risk  {MON_EN[tmr.month-1]} {tmr.day} ({DAY_EN[tmr.weekday()]})",
        "",
    ]
    for rid in MODEL_ROUTES:
        info  = ROUTE_INFO[rid]
        probs = probs_by_route.get(rid, [None] * 7)
        pct1  = _pct(probs[1])
        if pct1 is None:
            continue
        icon = "🔴" if pct1 >= 70 else ("🟡" if pct1 >= 40 else "🟢")
        lines.append(f"{icon} {info['short']} / {info['en']}: {pct1}%")

    # 安栄観光HPの重要お知らせがある場合は追記
    if _is_notable_caution(caution_text):
        lines += ["", "📢【安栄観光より / From Anei Kanko】"]
        notice = caution_text if len(caution_text) <= 300 else caution_text[:297] + "..."
        lines.append(notice)

    lines += [
        "",
        "📊 詳細は画像スワイプでご確認ください。/ Swipe for details.",
        "⚠️ AI予測・参考値。欠航判断は安栄観光公式HPをご確認ください。",
        "⚠️ AI estimates only. Check Anei Kanko official site for cancellations.",
        "",
        "#八重山 #石垣島 #西表島 #波照間島 #竹富島 #欠航予報",
        "#YaeyamaIslands #OkinawaFerry #JapanTravel #IslandHopping",
    ]
    return "\n".join(lines)


# ============================================================
# メイン
# ============================================================

def run_yaeyama_publisher(route_data_list=None, cancel_models=None, caution_text=None):
    """yaeyama_logger.py から呼び出すエントリーポイント。"""
    now = datetime.now(JST)
    print(f"\n{'='*50}")
    print(f"Yaeyama Publisher: {now.strftime('%Y-%m-%d %H:%M')}")
    print("="*50)

    if cancel_models is None:
        cancel_models = _load_model()

    # [P1] 予報データ構築（Day1はロガー取得済みデータ使用）
    print("\n[P1] 欠航確率計算中...")
    probs_by_route, batched = _build_forecast_data(route_data_list, cancel_models)

    # [P2] 画像生成
    print("\n[P2] 画像生成中...")
    output_dir = "/tmp/yaeyama_images"
    os.makedirs(output_dir, exist_ok=True)
    ts    = now.strftime("%Y%m%d_%H%M")
    paths = [
        f"{output_dir}/ya_img1_short_{ts}.png",
        f"{output_dir}/ya_img2_longterm_{ts}.png",
        f"{output_dir}/ya_img3_weatherdata_{ts}.png",
    ]
    make_image_short(probs_by_route,                paths[0])
    make_image_longterm(probs_by_route,             paths[1])
    make_image_weatherdata(probs_by_route, batched, paths[2])

    # [P3] GitHub Pages へアップロード（常に実行・プレビュー用）
    print("\n[P3] GitHub Pages へ画像アップロード中...")
    image_urls = _upload_images_to_github(paths)

    # [P4] キャプション & Instagram 投稿
    if caution_text and _is_notable_caution(caution_text):
        print(f"  [お知らせ] 重要caution_text検出: {caution_text[:60]}...")
    caption = _build_caption(probs_by_route, now, caution_text=caution_text)

    # 午後便（12時以降）は欠航リスクが高い場合のみInstagram投稿（座間味と同じロジック）
    # 条件: 短期（明日・明後日）+ 長期（3〜6日先）全期間のいずれかで欠航確率 61% 以上
    is_afternoon_run = now.hour >= 12
    if is_afternoon_run:
        max_pct = max(
            (_pct(probs_by_route.get(rid, [None] * 7)[i]) or 0)
            for rid in MODEL_ROUTES
            for i in range(1, 7)   # 明日〜6日後（全予報期間）
        )
        if max_pct < 61:
            print(f"  [午後便] 全期間・全航路最大欠航リスク {max_pct}% < 61% → Instagram投稿スキップ")
            print("\n✅ Yaeyama Publisher 完了")
            return
        print(f"  [午後便] 最大欠航リスク {max_pct}% ≥ 61% → Instagram投稿実行")

    print(f"\n[P4] Instagram 投稿中...")
    _post_to_instagram(image_urls, caption)

    print("\n✅ Yaeyama Publisher 完了")


if __name__ == "__main__":
    run_yaeyama_publisher()
