"""
Okinawa Ferry Forecast Publisher
フェリー運航予測の画像生成・SNS投稿文生成・DB（Google Sheets）保存

対象: 座間味島（Phase1）
投稿: X（テキスト）+ Instagram（画像3枚）
時刻: 8:15 / 13:00 JST
"""

import os
import json
import math
import textwrap
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from anthropic import Anthropic

JST = ZoneInfo("Asia/Tokyo")

# ============================================================
# 設定
# ============================================================

# フォントパス（動的検索）
def _find_noto_font(weights):
    """NotoSansCJKフォントを複数候補パスから検索。見つかったパスを返す。"""
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
    # fc-list でフォールバック検索
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

# 起動時にフォント検索結果を表示
print(f"[Font] REGULAR: {FONT_REGULAR}")
print(f"[Font] BOLD:    {FONT_BOLD}")
print(f"[Font] MEDIUM:  {FONT_MEDIUM}")


def _load_font(path, size):
    """フォントを個別にロード。失敗時はサイズ指定のデフォルトフォントを返す。"""
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    # Pillow 10+ はsize指定可、それ以前は引数なし
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

# 画像サイズ
IMG_SIZE = (1080, 1080)

# 欠航可能性による背景色
def get_bg_color(pct):
    if pct <= 30:
        return "#2E7D32"   # 深緑：運航見込み
    elif pct <= 60:
        return "#F9A825"   # 琥珀：注意
    elif pct <= 80:
        return "#E65100"   # オレンジ：要確認
    else:
        return "#B71C1C"   # 深赤：欠航の可能性高

def get_risk_label_ja(pct):
    if pct <= 30:
        return "運航見込み"
    elif pct <= 60:
        return "注意"
    elif pct <= 80:
        return "要確認"
    else:
        return "欠航の可能性高"

def get_risk_label_en(pct):
    if pct <= 30:
        return "Likely Operating"
    elif pct <= 60:
        return "Caution"
    elif pct <= 80:
        return "High Risk"
    else:
        return "Likely Cancelled"

def hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def effective_max_pct(day):
    """その日の「運航中船種の欠航%の最大値」を返す。
    運休（公式決定）船種は天候由来%を持っていても判定から除外する。
    両方とも運休なら 0 を返す（運休＝天候リスクと別軸）。

    背景色・コメントtier・午後便投稿判定・risk_dates 等、
    船種運休を反映すべき集約計算は全てこの関数に集約する。
    """
    cands = []
    if not day.get("suspended_highspeed"):
        cands.append(day["highspeed_pct"])
    if not day.get("suspended_ferry"):
        cands.append(day["ferry_pct"])
    return max(cands) if cands else 0

DISCLAIMER_JA = "※本予測はAIによる参考値です。公式情報は座間味村HPをご確認ください。"
DISCLAIMER_EN = "*AI-based estimate. Check official Zamami Village website for confirmed schedules."

# ============================================================
# 1. %変換
# ============================================================

def score_to_pct_highspeed(score):
    """
    高速船欠航可能性%に変換。
    感覚値ベース:
      波1m・風5m/s  (score≈0.16) → 3%
      波2m・風10m/s (score≈0.31) → 18%
      波3m・風15m/s (score≈0.51) → 78%（欠航ライン）
      波4m・風18m/s (score≈0.65) → 96%
      波5m+          (score≈0.80) → 99%
    """
    inflection = 0.42
    steepness  = 14.0
    pct = 100 / (1 + math.exp(-steepness * (score - inflection)))
    return round(min(max(pct, 1), 99))


def score_to_pct_ferry(score):
    """
    フェリー欠航可能性%に変換。
    変曲点 0.52（旧 0.58 → 台風レンジでも86%止まりで実態より低い問題を是正）。
    新感覚値:
      波1m・風5m/s  (score≈0.16) → 2%
      波2m・風10m/s (score≈0.31) → 8%
      波3m・風15m/s (score≈0.51) → 49%
      波4m・風18m/s (score≈0.65) → 83%
      波5m+          (score≈0.73) → 93%（暴風域内なら更にフロア95で確定）
    """
    inflection = 0.52
    steepness  = 12.0
    pct = 100 / (1 + math.exp(-steepness * (score - inflection)))
    return round(min(max(pct, 1), 99))


# ============================================================
# 0. 計画運休ヘルパー
# ============================================================

SERVICE_JA = {"highspeed": "高速船", "ferry": "フェリー", "both": "高速船・フェリー"}


def _is_date_suspended(date_str, planned_suspensions, service):
    """date_strがservice（"highspeed"/"ferry"）の計画運休期間内かチェック。該当するエントリを返す。"""
    from datetime import date as _date
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return None
    for sus in (planned_suspensions or []):
        if sus.get("service") not in (service, "both"):
            continue
        try:
            start = datetime.strptime(sus["start"], "%Y-%m-%d").date()
            end = datetime.strptime(sus["end"], "%Y-%m-%d").date()
            if start <= target <= end:
                return sus
        except Exception:
            continue
    return None


def _fmt_wave(text):
    """気象庁波高テキストを整形（メートル→m）"""
    if not text:
        return "—"
    return text.replace("メートル", "m").replace(" ", "")


def _fmt_prob(text):
    """警報級確率テキストを整形（なし→なし / None）"""
    if not text or text in ("なし", ""):
        return "なし / None"
    return text


def build_forecast_data(analysis, jma_waves, jma_prob, planned_suspensions=None,
                        typhoon_by_date=None):
    """
    analysisから投稿・画像用データを構築。
    短期（明日・明後日）と長期（3〜7日）の欠航可能性%を返す。

    typhoon_by_date: {"YYYY-MM-DD": {tier, hs_floor, fe_floor, ...}}
      その日の台風接近フロアを欠航%の下限として適用（波モデルが穏やかでも
      進路がかぶる日はリスクを立てる）。
    """
    now = datetime.now(JST)
    typhoon_by_date = typhoon_by_date or {}
    SCORE_HIGHSPEED_RISK = 0.45
    SCORE_FERRY_RISK = 0.65

    short_term = []
    for delta in [1, 2]:
        date = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
        dt = now + timedelta(days=delta)
        label = "明日" if delta == 1 else "明後日"
        label_en = "Tomorrow" if delta == 1 else "Day After"
        date_label = dt.strftime("%-m/%-d")

        all_day = analysis.get(date, {}).get("all_day")
        morning = analysis.get(date, {}).get("morning")
        afternoon = analysis.get(date, {}).get("afternoon")

        if all_day:
            hs_pct = score_to_pct_highspeed(all_day["cancellation_score"])
            fe_pct = score_to_pct_ferry(all_day["cancellation_score"])
            hs_am_pct = score_to_pct_highspeed(morning["cancellation_score"]) if morning else hs_pct
            hs_pm_pct = score_to_pct_highspeed(afternoon["cancellation_score"]) if afternoon else hs_pct
        else:
            hs_pct = fe_pct = hs_am_pct = hs_pm_pct = 0

        # シャドー記録用：台風フロア適用前の値（現行ロジック相当）を保持
        hs_pct_base, fe_pct_base = hs_pct, fe_pct

        # 台風フロア適用（波モデル値と台風接近フロアの大きい方を採用）
        tphn = typhoon_by_date.get(date)
        if tphn:
            hs_pct = max(hs_pct, tphn["hs_floor"])
            fe_pct = max(fe_pct, tphn["fe_floor"])
            hs_am_pct = max(hs_am_pct, tphn["hs_floor"])
            hs_pm_pct = max(hs_pm_pct, tphn["hs_floor"])

        short_term.append({
            "date": date,
            "date_label": date_label,
            "date_label_en": dt.strftime("%b %-d"),  # 例: May 11
            "label_ja": label,
            "label_en": label_en,
            "highspeed_pct": hs_pct,
            "highspeed_am_pct": hs_am_pct,
            "highspeed_pm_pct": hs_pm_pct,
            "ferry_pct": fe_pct,
            # シャドー記録：台風フロア適用前（現行ロジック相当）
            "highspeed_pct_base": hs_pct_base,
            "ferry_pct_base": fe_pct_base,
            "jma_wave": jma_waves.get(label, ""),
            "jma_prob": jma_prob.get(label, {}).get("level", ""),
            "max_wave": all_day["max_wave"] if all_day else None,
            "max_wind": all_day.get("max_wind", "") if all_day else "",
            "max_swell": all_day.get("max_swell", "") if all_day else "",
            "suspended_highspeed": bool(_is_date_suspended(date, planned_suspensions, "highspeed")),
            "suspended_ferry":     bool(_is_date_suspended(date, planned_suspensions, "ferry")),
            "suspension_reason_ja": (
                _is_date_suspended(date, planned_suspensions, "highspeed") or
                _is_date_suspended(date, planned_suspensions, "ferry") or {}
            ).get("reason_ja", ""),
            "suspension_reason_en": (
                _is_date_suspended(date, planned_suspensions, "highspeed") or
                _is_date_suspended(date, planned_suspensions, "ferry") or {}
            ).get("reason_en", ""),
            "suspension_vessel_ja": (
                _is_date_suspended(date, planned_suspensions, "highspeed") or
                _is_date_suspended(date, planned_suspensions, "ferry") or {}
            ).get("vessel_ja", ""),
            "suspension_vessel_en": (
                _is_date_suspended(date, planned_suspensions, "highspeed") or
                _is_date_suspended(date, planned_suspensions, "ferry") or {}
            ).get("vessel_en", ""),
            "typhoon": tphn,  # 台風フロア情報（なければNone）
        })

    long_term = []
    risk_dates = []
    for delta in range(3, 8):
        date = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
        dt = now + timedelta(days=delta)
        all_day = analysis.get(date, {}).get("all_day")
        if all_day:
            hs_pct = score_to_pct_highspeed(all_day["cancellation_score"])
            fe_pct = score_to_pct_ferry(all_day["cancellation_score"])
            # 台風フロア適用
            tphn = typhoon_by_date.get(date)
            if tphn:
                hs_pct = max(hs_pct, tphn["hs_floor"])
                fe_pct = max(fe_pct, tphn["fe_floor"])
            sus_hs = bool(_is_date_suspended(date, planned_suspensions, "highspeed"))
            sus_fe = bool(_is_date_suspended(date, planned_suspensions, "ferry"))
            long_term.append({
                "date": date,
                "date_label": dt.strftime("%-m/%-d"),
                "highspeed_pct": hs_pct,
                "ferry_pct": fe_pct,
                "suspended_highspeed": sus_hs,
                "suspended_ferry":     sus_fe,
                "typhoon": tphn,
            })
            # 運休中の船種は欠航リスク判定から除外する
            # （その日は天候によらず運休なので、運航中の船種の%で判定する）
            if effective_max_pct(long_term[-1]) >= 31:
                risk_dates.append(dt)

    # 長期リスク期間のサマリー
    if risk_dates:
        risk_start = risk_dates[0].strftime("%-m/%-d")
        risk_end = risk_dates[-1].strftime("%-m/%-d")
        risk_start_en = risk_dates[0].strftime("%b %-d")
        risk_end_en = risk_dates[-1].strftime("%b %-d")
        max_lt_pct = max((effective_max_pct(d) for d in long_term), default=0)
        long_term_summary = {
            "has_risk": True,
            "risk_period": f"{risk_start}〜{risk_end}頃",
            "risk_period_en": f"Around {risk_start_en} - {risk_end_en}",
            "max_pct": max_lt_pct,
            "days": long_term,
        }
    else:
        max_lt_pct = max((effective_max_pct(d) for d in long_term), default=0)
        # 長期期間の英語表記（懸念なし時も先頭〜末尾の日付を使う）
        if long_term:
            lt_start = datetime.strptime(long_term[0]["date"], "%Y-%m-%d")
            lt_end   = datetime.strptime(long_term[-1]["date"], "%Y-%m-%d")
            lt_period_en = f"{lt_start.strftime('%b %-d')} - {lt_end.strftime('%b %-d')}"
        else:
            lt_period_en = ""
        long_term_summary = {
            "has_risk": False,
            "risk_period": "懸念なし",
            "risk_period_en": "No concern",
            "lt_period_en": lt_period_en,
            "max_pct": max_lt_pct,
            "days": long_term,
        }

    # 明日の数値予測データを weather_data としてまとめる
    tmr = short_term[0] if short_term else {}
    daf = short_term[1] if len(short_term) > 1 else {}
    tmr_ad = analysis.get(tmr.get("date", ""), {}).get("all_day") or {}
    weather_data = {
        "jma_wave_tomorrow":    _fmt_wave(jma_waves.get("明日", "")),
        "jma_wave_dayafter":    _fmt_wave(jma_waves.get("明後日", "")),
        "jma_prob_tomorrow":    _fmt_prob(jma_prob.get("明日", {}).get("level", "")),
        "jma_prob_dayafter":    _fmt_prob(jma_prob.get("明後日", {}).get("level", "")),
        "num_max_wave":  f"{tmr.get('max_wave', '')}m" if tmr.get("max_wave") else "",
        "num_max_swell": f"{tmr.get('max_swell', '')}m" if tmr.get("max_swell") else "",
        "num_max_wind":  f"{tmr.get('max_wind', '')} m/s" if tmr.get("max_wind") else "",
        # 追加メトリクス（突風・周期・視程ほか）
        "num_max_gust":         f"{tmr_ad.get('max_gust')} m/s" if tmr_ad.get("max_gust") else "",
        "num_swell_period":     f"{tmr_ad.get('max_swell_period')} s" if tmr_ad.get("max_swell_period") else "",
        "num_wave_period":      f"{tmr_ad.get('max_wave_period')} s" if tmr_ad.get("max_wave_period") else "",
        "num_min_visibility":   f"{tmr_ad.get('min_visibility')/1000:.1f} km" if tmr_ad.get("min_visibility") else "",
        "num_max_precip":       f"{tmr_ad.get('max_precip')} mm" if tmr_ad.get("max_precip") else "",
    }

    # 台風サマリー（予測範囲内に接近予報がある場合のみ）
    typhoon_summary = None
    if typhoon_by_date:
        in_range = {d: t for d, t in typhoon_by_date.items() if d in analysis}
        if in_range:
            names = sorted({t["name_ja"] for t in in_range.values()})
            dates_sorted = sorted(in_range)
            closest = min(in_range.values(), key=lambda t: t["dist_km"])
            typhoon_summary = {
                "names": names,
                "name_label": " / ".join(names),
                "days": [
                    {
                        "date": d,
                        "date_label": datetime.strptime(d, "%Y-%m-%d").strftime("%-m/%-d"),
                        "date_label_en": datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d"),
                        "tier": in_range[d]["tier"],
                        "dist_km": in_range[d]["dist_km"],
                        "in_storm": in_range[d]["in_storm"],
                        "in_circle": in_range[d]["in_circle"],
                        "extrapolated": in_range[d].get("extrapolated", False),
                        "hs_floor": in_range[d]["hs_floor"],
                        "fe_floor": in_range[d]["fe_floor"],
                    }
                    for d in dates_sorted
                ],
                "closest_dist_km": closest["dist_km"],
                "max_tier": min(t["tier"] for t in in_range.values()),
            }

    return {
        "short_term": short_term,
        "long_term": long_term_summary,
        "weather_data": weather_data,
        "typhoon": typhoon_summary,
        "generated_at": now.strftime("%Y/%m/%d %H:%M"),
        "generated_at_label": "8:15更新" if now.hour < 11 else "14:30更新",
        "update_date_ja": now.strftime("%-m/%-d"),
        "update_date_en": now.strftime("%b %-d"),
        "planned_suspensions": [
            sus for sus in (planned_suspensions or [])
            if sus.get("start") and sus.get("end")
        ],
    }


# ============================================================
# 2. 画像生成
# ============================================================

def make_image_header(forecast, output_path):
    """画像①: ヘッダー（日英併記・3島名）"""
    img = Image.new("RGB", IMG_SIZE, color=hex_to_rgb("#0D47A1"))
    draw = ImageDraw.Draw(img)
    f = {
        "islands_ja": _load_font(FONT_BOLD,    62),
        "islands_en": _load_font(FONT_MEDIUM,  32),
        "area":       _load_font(FONT_REGULAR, 28),
        "service_ja": _load_font(FONT_MEDIUM,  48),
        "service_en": _load_font(FONT_REGULAR, 30),
        "date":       _load_font(FONT_BOLD,    48),
        "update":     _load_font(FONT_REGULAR, 28),
        "url":        _load_font(FONT_REGULAR, 24),
        "xs":         _load_font(FONT_REGULAR, 19),
    }

    now = datetime.now(JST)

    # 島名（日英・3島）
    draw.text((540, 175), "座間味島・阿嘉島・慶留間島", font=f["islands_ja"], fill="white", anchor="mm")
    draw.text((540, 248), "Zamami / Aka / Geruma Islands", font=f["islands_en"], fill="#90CAF9", anchor="mm")
    draw.text((540, 292), "沖縄県慶良間諸島  Kerama Islands, Okinawa", font=f["area"], fill="rgba(144,202,249,180)", anchor="mm")

    # サービス名（日英）
    draw.text((540, 385), "フェリー欠航予報", font=f["service_ja"], fill="#FFD54F", anchor="mm")
    draw.text((540, 443), "Ferry Cancellation Forecast", font=f["service_en"], fill="#FFE082", anchor="mm")

    # 区切り線
    draw.rectangle([(160, 478), (920, 481)], fill="#90CAF9")

    # 日付（日英）
    draw.text((540, 548), now.strftime("%Y年%-m月%-d日"), font=f["date"], fill="white", anchor="mm")
    draw.text((540, 604), now.strftime("%B %-d, %Y"), font=f["update"], fill="#BBDEFB", anchor="mm")
    draw.text((540, 648), forecast["generated_at_label"], font=f["update"], fill="#90CAF9", anchor="mm")

    # 説明
    draw.text((540, 728), "気象データに基づくAI予測です", font=f["update"], fill="#BBDEFB", anchor="mm")
    draw.text((540, 768), "Based on meteorological data & AI analysis", font=f["url"], fill="#90CAF9", anchor="mm")

    # URL枠
    draw.rectangle([(160, 828), (920, 876)], fill="#1565C0")
    draw.text((540, 852), "okinawa-ferry-forecast.com（準備中）", font=f["update"], fill="white", anchor="mm")

    # 免責
    draw.text((540, 920), "※AI予測・参考値 / *AI-based estimate for reference only", font=f["xs"], fill="#90CAF9", anchor="mm")
    draw.text((540, 946), "公式情報: 座間味村HP / Official: vill.zamami.okinawa.jp", font=f["xs"], fill="#7986CB", anchor="mm")

    img.save(output_path)
    print(f"  画像①保存: {output_path}")


def make_image_short(forecast, output_path):
    """画像②: 短期予報（船種別計画運休対応版）"""
    short = forecast["short_term"]

    # 背景色: AI予測対象（運休でない船種）の最大リスクで決定（effective_max_pct）。
    # 両方とも全運休の場合のみフォールバック10（緑）。
    max_pct = max((effective_max_pct(d) for d in short), default=10)
    bg = hex_to_rgb(get_bg_color(max_pct))

    img = Image.new("RGB", IMG_SIZE, color=bg)
    draw = ImageDraw.Draw(img)

    # レイアウト定数
    HS_TOP, HS_BTM = 238, 492   # 高速船セクションのY範囲
    FE_TOP, FE_BTM = 498, 758   # フェリーセクションのY範囲
    DIVIDER_Y = 495
    SUSPENSION_BG_COLOR = hex_to_rgb("#7B96A4")

    f = {
        "island_title": _load_font(FONT_BOLD,   50),
        "title_ja":  _load_font(FONT_BOLD,    44),
        "title_en":  _load_font(FONT_MEDIUM,  28),
        "island":    _load_font(FONT_REGULAR, 22),
        "date":      _load_font(FONT_MEDIUM,  34),
        "date_en":   _load_font(FONT_REGULAR, 24),
        "pct":       _load_font(FONT_BOLD,    86),
        "suspended": _load_font(FONT_BOLD,    48),
        "type_ja":   _load_font(FONT_MEDIUM,  28),
        "ampm":      _load_font(FONT_REGULAR, 22),
        "reason":    _load_font(FONT_MEDIUM,  24),
        "reason_en": _load_font(FONT_REGULAR, 20),
        "badge":     _load_font(FONT_BOLD,    18),
        "jma":       _load_font(FONT_REGULAR, 17),
        "xs":        _load_font(FONT_REGULAR, 17),
    }

    # タイトル（1枚目のため島名を大きく表示）
    draw.text((540, 44),  "座間味島・阿嘉島",
              font=f["island_title"], fill="white", anchor="mm")
    draw.text((540, 90),  "フェリー欠航予測",
              font=f["island_title"], fill="white", anchor="mm")
    draw.text((540, 122), "Zamami / Aka  Ferry Cancellation Risk",
              font=f["title_en"], fill=(255,255,255,210), anchor="mm")
    draw.line([(80,138),(1000,138)], fill=(255,255,255,100), width=1)

    positions = [270, 810]
    for i, day in enumerate(short[:2]):
        x = positions[i]
        px1, px2 = x - 222, x + 222
        sus_hs = day.get("suspended_highspeed", False)
        sus_fe = day.get("suspended_ferry", False)

        # 船種ごとのグレーパネル（運休の船種のみ）
        if sus_hs:
            draw.rectangle([(px1, HS_TOP), (px2, HS_BTM)], fill=SUSPENSION_BG_COLOR)
        if sus_fe:
            draw.rectangle([(px1, FE_TOP), (px2, FE_BTM)], fill=SUSPENSION_BG_COLOR)

        # 日付ヘッダー
        draw.text((x, 168), f"{day['label_ja']}  {day['date_label']}",
                  font=f["date"], fill="white", anchor="mm")
        draw.text((x, 208), f"{day['label_en']}  {day.get('date_label_en','')}",
                  font=f["date_en"], fill=(255,255,255,180), anchor="mm")

        # ── 高速船セクション ──
        hs_mid = (HS_TOP + HS_BTM) // 2  # 365
        if sus_hs:
            # 公式発表バッジ
            bw, bh = 220, 34
            by = HS_TOP + 14
            draw.rounded_rectangle([(x-bw//2, by), (x+bw//2, by+bh)],
                                    radius=7, fill="#FF5252")
            draw.text((x, by+17), "公式発表  Official Notice",
                      font=f["badge"], fill="white", anchor="mm")
            # 運休
            draw.text((x, hs_mid - 28), "運休",
                      font=f["suspended"], fill="white", anchor="mm")
            draw.text((x, hs_mid + 20), "Suspended",
                      font=f["reason_en"], fill=(255,255,255,190), anchor="mm")
            # 船名・理由
            vessel = day.get("suspension_vessel_ja", "クイーンざまみ")
            vessel_en = day.get("suspension_vessel_en", "Queen Zamami")
            reason = day.get("suspension_reason_ja", "ドック入り")
            reason_en = day.get("suspension_reason_en", "Dock Maintenance")
            draw.text((x, hs_mid + 48),
                      f"{vessel}  /  {reason}",
                      font=f["reason"], fill=(255,220,170,220), anchor="mm")
            draw.text((x, hs_mid + 72),
                      f"{vessel_en}  /  {reason_en}",
                      font=f["reason_en"], fill=(255,220,170,170), anchor="mm")
        else:
            draw.text((x, hs_mid - 52), f"{day['highspeed_pct']}%",
                      font=f["pct"], fill="white", anchor="mm")
            draw.text((x, hs_mid + 20), "高速船  High Speed Boat",
                      font=f["type_ja"], fill=(255,255,255,220), anchor="mm")
            draw.text((x, hs_mid + 50),
                      f"AM {day['highspeed_am_pct']}%  /  PM {day['highspeed_pm_pct']}%",
                      font=f["ampm"], fill=(255,255,255,175), anchor="mm")

        # 区切り線（列ごと）
        draw.line([(px1+10, DIVIDER_Y), (px2-10, DIVIDER_Y)],
                  fill=(255,255,255,70), width=1)

        # ── フェリーセクション ──
        fe_mid = (FE_TOP + FE_BTM) // 2  # 628
        if sus_fe:
            bw, bh = 220, 34
            by = FE_TOP + 14
            draw.rounded_rectangle([(x-bw//2, by), (x+bw//2, by+bh)],
                                    radius=7, fill="#FF5252")
            draw.text((x, by+17), "公式発表  Official Notice",
                      font=f["badge"], fill="white", anchor="mm")
            draw.text((x, fe_mid - 28), "運休",
                      font=f["suspended"], fill="white", anchor="mm")
            draw.text((x, fe_mid + 20), "Suspended",
                      font=f["reason_en"], fill=(255,255,255,190), anchor="mm")
            vessel = day.get("suspension_vessel_ja", "フェリーざまみ")
            vessel_en = day.get("suspension_vessel_en", "Ferry Zamami")
            reason = day.get("suspension_reason_ja", "ドック入り")
            reason_en = day.get("suspension_reason_en", "Dock Maintenance")
            draw.text((x, fe_mid + 48),
                      f"{vessel}  /  {reason}",
                      font=f["reason"], fill=(255,220,170,220), anchor="mm")
            draw.text((x, fe_mid + 72),
                      f"{vessel_en}  /  {reason_en}",
                      font=f["reason_en"], fill=(255,220,170,170), anchor="mm")
        else:
            draw.text((x, fe_mid - 46), f"{day['ferry_pct']}%",
                      font=f["pct"], fill="white", anchor="mm")
            draw.text((x, fe_mid + 26), "フェリー  Ferry",
                      font=f["type_ja"], fill=(255,255,255,220), anchor="mm")
            if day.get("jma_wave"):
                draw.text((x, fe_mid + 78),
                          f"気象庁: {day['jma_wave']}",
                          font=f["jma"], fill=(255,255,255,155), anchor="mm")
            if day.get("jma_prob"):
                draw.text((x, fe_mid + 98),
                          f"早期注意(波浪): {day['jma_prob']}",
                          font=f["jma"], fill=(255,255,255,155), anchor="mm")

    # 中央縦線・フッター
    draw.line([(540,135),(540,758)], fill=(255,255,255,55), width=1)
    draw.line([(80, 768),(1000,768)], fill=(255,255,255,45), width=1)
    draw.text((540, 802),
              "※AI予測・参考値。運休は公式発表に基づきます。",
              font=f["xs"], fill=(255,255,255,155), anchor="mm")
    draw.text((540, 826),
              "*AI estimates for weather risk. Suspension notice based on official announcement.",
              font=f["xs"], fill=(255,255,255,125), anchor="mm")

    img.save(output_path)
    print(f"  画像②保存: {output_path}")


def make_image_longterm(forecast, output_path):
    """画像③: 長期予報（日英併記・左右2列棒グラフ・確定版）"""
    lt = forecast["long_term"]
    max_pct = lt["max_pct"]
    img = Image.new("RGB", IMG_SIZE, color=hex_to_rgb(get_bg_color(max_pct)))
    draw = ImageDraw.Draw(img)
    f = {
        "title_ja": _load_font(FONT_BOLD,    44),
        "title_en": _load_font(FONT_MEDIUM,  26),
        "island":   _load_font(FONT_REGULAR, 22),
        "head":     _load_font(FONT_MEDIUM,  32),
        "head_en":  _load_font(FONT_REGULAR, 24),
        "period":   _load_font(FONT_BOLD,    64),
        "pct":      _load_font(FONT_BOLD,    76),
        "label":    _load_font(FONT_MEDIUM,  28),
        "label_en": _load_font(FONT_REGULAR, 22),
        "col_hd":   _load_font(FONT_MEDIUM,  22),
        "bar":      _load_font(FONT_REGULAR, 21),
        "badge":    _load_font(FONT_BOLD,    18),
        "xs":       _load_font(FONT_REGULAR, 17),
    }

    # タイトル
    draw.text((540, 46), "フェリー欠航可能性 長期予報（3〜7日先）", font=f["title_ja"], fill="white", anchor="mm")
    draw.text((540, 86), "Ferry Cancellation Risk  /  Long-term Forecast (3-7 days ahead)", font=f["title_en"], fill="rgba(255,255,255,200)", anchor="mm")
    draw.text((540, 112), "座間味島・阿嘉島・慶留間島  Zamami / Aka / Geruma", font=f["island"], fill="rgba(255,255,255,160)", anchor="mm")
    draw.line([(80,128),(1000,128)], fill="rgba(255,255,255,100)", width=1)

    if lt["has_risk"]:
        # リスク期間・最大%
        draw.text((540, 183), "欠航リスク期間  /  Risk Period", font=f["head"], fill="rgba(255,255,255,200)", anchor="mm")
        draw.text((540, 255), lt["risk_period"], font=f["period"], fill="white", anchor="mm")
        draw.text((540, 308), lt["risk_period_en"], font=f["head_en"], fill="rgba(255,255,255,180)", anchor="mm")
        draw.line([(80,328),(1000,328)], fill="rgba(255,255,255,70)", width=1)

        # 高速船・フェリー最大% — 運休中の船種は除外する（運休日の天候%は誤解を招くため）
        hs_running = [d["highspeed_pct"] for d in lt["days"] if not d.get("suspended_highspeed")]
        fe_running = [d["ferry_pct"]     for d in lt["days"] if not d.get("suspended_ferry")]
        hs_max = max(hs_running) if hs_running else None
        fe_max = max(fe_running) if fe_running else None
        for x, pct, lja, len_ in [
            (270, hs_max, "高速船", "High Speed Boat"),
            (810, fe_max, "フェリー", "Ferry"),
        ]:
            draw.text((x, 362), lja, font=f["label"], fill="rgba(255,255,255,200)", anchor="mm")
            draw.text((x, 388), len_, font=f["label_en"], fill="rgba(255,255,255,170)", anchor="mm")
            if pct is None:
                # 期間中ずっと運休の船種は%ではなく「全日運休」を表示
                draw.text((x, 453), "全日運休", font=f["label"], fill="white", anchor="mm")
                draw.text((x, 500), "Suspended (period)", font=f["label_en"], fill="rgba(255,255,255,160)", anchor="mm")
            else:
                draw.text((x, 453), f"{pct}%", font=f["pct"], fill="white", anchor="mm")
                draw.text((x, 500), "最大欠航可能性 / Max Risk", font=f["label_en"], fill="rgba(255,255,255,160)", anchor="mm")
        draw.line([(540,333),(540,520)], fill="rgba(255,255,255,60)", width=1)
    else:
        draw.text((540, 300), "懸念なし  /  No Significant Risk", font=f["period"], fill="white", anchor="mm")

    # 左右2列の横棒グラフ
    draw.line([(80,530),(1000,530)], fill="rgba(255,255,255,70)", width=1)
    draw.text((290, 552), "高速船  High Speed Boat", font=f["col_hd"], fill="white", anchor="mm")
    draw.text((790, 552), "フェリー  Ferry", font=f["col_hd"], fill="white", anchor="mm")

    FOOTER_LINE_Y = 960  # フッター区切り線Y座標（縦線はここで止める）
    bar_top, bar_h, row_sp = 580, 28, 72
    cols = [
        {"date_x": 155, "bar_x": 175, "bar_max": 270, "pct_x": 455, "key": "highspeed_pct", "suspended_key": "suspended_highspeed"},
        {"date_x": 595, "bar_x": 615, "bar_max": 270, "pct_x": 895, "key": "ferry_pct",     "suspended_key": "suspended_ferry"},
    ]
    for i, d in enumerate(lt["days"][:5]):
        y = bar_top + i * row_sp
        dt = datetime.strptime(d["date"], "%Y-%m-%d")
        label = dt.strftime("%-m/%-d")  # 棒グラフは5/11形式
        for col in cols:
            pct = d[col["key"]]
            bar_w = int(col["bar_max"] * pct / 100)
            is_sus = col["suspended_key"] and d.get(col["suspended_key"], False)
            draw.text((col["date_x"], y + bar_h//2), label, font=f["bar"], fill="white", anchor="rm")
            draw.rectangle([(col["bar_x"], y),(col["bar_x"]+col["bar_max"], y+bar_h)], fill=(0,0,0,50))
            if is_sus:
                # 運休: ハッチング風（斜線）
                for sx in range(col["bar_x"], col["bar_x"]+col["bar_max"], 8):
                    draw.line([(sx, y),(sx+bar_h, y+bar_h)], fill=(150,180,200,180), width=2)
                draw.rectangle([(col["bar_x"], y),(col["bar_x"]+col["bar_max"], y+bar_h)],
                               outline=(180,210,230,200), width=1)
                # 「運休 / Suspended」日英2行
                f_sus_en = _load_font(FONT_REGULAR, 13)
                draw.text((col["pct_x"], y + 6),  "運休",      font=f["badge"], fill=(180,220,255),     anchor="lm")
                draw.text((col["pct_x"], y + 20), "Suspended", font=f_sus_en,   fill=(180,220,255,180), anchor="lm")
            else:
                if bar_w > 0:
                    draw.rectangle([(col["bar_x"], y),(col["bar_x"]+bar_w, y+bar_h)], fill=(255,255,255,210))
                draw.text((col["pct_x"], y + bar_h//2), f"{pct}%", font=f["bar"], fill="white", anchor="lm")

    # 縦区切り線：フッター区切り線で止める
    draw.line([(540, 535),(540, FOOTER_LINE_Y)], fill="rgba(255,255,255,50)", width=1)
    draw.line([(80, FOOTER_LINE_Y),(1000, FOOTER_LINE_Y)], fill="rgba(255,255,255,40)", width=1)
    draw.text((540, 985), "※AI予測・参考値。公式情報は座間味村HPをご確認ください。", font=f["xs"], fill="rgba(255,255,255,140)", anchor="mm")
    draw.text((540, 1006), "*AI-based estimate. Check official Zamami Village website.", font=f["xs"], fill="rgba(255,255,255,120)", anchor="mm")

    img.save(output_path)
    print(f"  画像③保存: {output_path}")


def make_image_weather_data(forecast, output_path):
    """画像④: 予報根拠データ（日英併記）"""
    wd = forecast.get("weather_data", {})
    img = Image.new("RGB", IMG_SIZE, color=hex_to_rgb("#0A1628"))
    draw = ImageDraw.Draw(img)

    f = {
        "title":    _load_font(FONT_BOLD,    40),
        "sec_hd":   _load_font(FONT_BOLD,    22),
        "label_ja": _load_font(FONT_REGULAR, 20),
        "label_en": _load_font(FONT_REGULAR, 17),
        "value":    _load_font(FONT_MEDIUM,  20),
        "src":      _load_font(FONT_REGULAR, 19),
        "foot":     _load_font(FONT_REGULAR, 17),
        "badge_sm": _load_font(FONT_BOLD,    17),
    }

    # タイトル
    draw.text((540, 68), "予報根拠データ  /  Forecast Data", font=f["title"], fill="white", anchor="mm")
    draw.line([(60, 100),(1020, 100)], fill="#334E7A", width=2)

    def section_header(y, ja, en):
        draw.rectangle([(60, y),(1020, y+44)], fill="#1A3057")
        draw.text((80, y+22), f"【{ja} / {en}】", font=f["sec_hd"], fill="#7EB3F5", anchor="lm")
        return y + 60

    def row(y, icon, label_ja, label_en, value):
        draw.text((80,  y),    f"{icon} {label_ja}", font=f["label_ja"], fill="#BBDEFB", anchor="lm")
        draw.text((96,  y+24), label_en,              font=f["label_en"], fill="#7986CB", anchor="lm")
        draw.text((1010, y+12), str(value),            font=f["value"],    fill="white",   anchor="rm")
        return y + 58

    y = 112

    # 【計画運休情報】セクション（計画運休がある場合のみ）
    suspensions = forecast.get("planned_suspensions", [])
    if suspensions:
        draw.rectangle([(60, y),(1020, y+42)], fill="#1B2A1B")
        draw.text((80, y+21), "【計画運休情報 / Scheduled Suspension】",
                  font=f["sec_hd"], fill="#81C784", anchor="lm")
        draw.rounded_rectangle([(848, y+8),(1010, y+34)], radius=6, fill="#2E7D32")
        draw.text((929, y+21), "公式発表 Official", font=f["badge_sm"], fill="white", anchor="mm")
        y += 56

        for sus in suspensions:
            # 運休期間が予測範囲（今後7日）に含まれるものだけ表示
            try:
                now_date = datetime.now(JST).date()
                sus_start = datetime.strptime(sus["start"], "%Y-%m-%d").date()
                sus_end   = datetime.strptime(sus["end"],   "%Y-%m-%d").date()
                if sus_end < now_date:
                    continue  # 過去の運休はスキップ
            except Exception:
                pass

            s_label    = datetime.strptime(sus["start"], "%Y-%m-%d").strftime("%-m/%-d")
            e_label    = datetime.strptime(sus["end"],   "%Y-%m-%d").strftime("%-m/%-d")
            s_label_en = datetime.strptime(sus["start"], "%Y-%m-%d").strftime("%b %-d")
            e_label_en = datetime.strptime(sus["end"],   "%Y-%m-%d").strftime("%b %-d")

            draw.text((80, y),
                      f"・{sus.get('vessel_ja','—')}（{SERVICE_JA.get(sus.get('service',''), sus.get('service',''))}）",
                      font=f["label_ja"], fill="#C8E6C9", anchor="lm")
            draw.text((1010, y+11), f"{s_label}〜{e_label}",
                      font=f["value"], fill="#81C784", anchor="rm")
            draw.text((96,  y+24),
                      f"  {sus.get('vessel_en','—')}",
                      font=f["label_en"], fill="#A5D6A7", anchor="lm")
            draw.text((1010, y+30),
                      f"({s_label_en} - {e_label_en})",
                      font=f["label_en"], fill="#66BB6A", anchor="rm")
            y += 62
            draw.text((80, y),
                      f"  理由: {sus.get('reason_ja','—')}  /  {sus.get('reason_en','—')}",
                      font=f["label_ja"], fill="#A5D6A7", anchor="lm")
            draw.text((80, y+23),
                      f"  出典: 座間味村HP（自動取得）",
                      font=f["label_en"], fill="#69A96B", anchor="lm")
            y += 52
        y += 8
        draw.line([(60, y),(1020, y)], fill="#334E7A", width=1)
        y += 8

    # 【台風情報】セクション（接近予報がある場合のみ）
    typhoon = forecast.get("typhoon")
    if typhoon:
        draw.rectangle([(60, y),(1020, y+42)], fill="#3A1B1B")
        draw.text((80, y+21), f"【台風情報 / Typhoon】 {typhoon['name_label']}",
                  font=f["sec_hd"], fill="#FF8A80", anchor="lm")
        draw.rounded_rectangle([(828, y+8),(1010, y+34)], radius=6, fill="#C62828")
        draw.text((919, y+21), "進路予報 JMA Track", font=f["badge_sm"], fill="white", anchor="mm")
        y += 56
        for d in typhoon["days"][:6]:
            # 値（右側）：暴風域・予報円・外挿の別 ＋ 中心距離
            if d.get("extrapolated"):
                value_ja = f"進路外挿（参考）中心からの距離 {d['dist_km']}km"
                value_en = f"Extrapolated, {d['dist_km']}km from center"
            elif d["in_storm"]:
                value_ja = f"暴風警報域内 中心からの距離 {d['dist_km']}km"
                value_en = f"In storm area, {d['dist_km']}km from center"
            elif d["in_circle"]:
                value_ja = f"予報円内 中心からの距離 {d['dist_km']}km"
                value_en = f"In forecast circle, {d['dist_km']}km from center"
            else:
                value_ja = f"中心からの距離 {d['dist_km']}km"
                value_en = f"{d['dist_km']}km from center"
            # 項目（左側）：日付
            draw.text((80,  y),    f"・{d['date_label']}",
                      font=f["label_ja"], fill="#FFCDD2", anchor="lm")
            draw.text((96,  y+24), f"  {d['date_label_en']}",
                      font=f["label_en"], fill="#EF9A9A", anchor="lm")
            # 値（右側）：状態 ＋ 中心距離
            draw.text((1010, y),    value_ja, font=f["value"], fill="white",  anchor="rm")
            draw.text((1010, y+24), value_en, font=f["label_en"], fill="#FFCDD2", anchor="rm")
            y += 52
        y += 6
        draw.line([(60, y),(1020, y)], fill="#334E7A", width=1)
        y += 8

    # 【気象庁 / JMA】
    y = section_header(y, "気象庁", "JMA")
    y = row(y, "🌊", "波高予報（明日）",          "Wave Height Forecast (Tomorrow)",   wd.get("jma_wave_tomorrow", "—"))
    y = row(y, "🌊", "波高予報（明後日）",         "Wave Height Forecast (Day After)",  wd.get("jma_wave_dayafter", "—"))
    y = row(y, "⚠", "早期注意情報・波浪（明日）",  "Early Warning Wave (Tomorrow)",     wd.get("jma_prob_tomorrow", "なし / None"))
    y = row(y, "⚠", "早期注意情報・波浪（明後日）", "Early Warning Wave (Day After)",    wd.get("jma_prob_dayafter", "なし / None"))

    y += 12
    # 【数値予測 / Numerical Model】
    y = section_header(y, "数値予測", "Numerical Model")
    y = row(y, "📊", "明日 最大波高",  "Tomorrow Max Wave Height",  wd.get("num_max_wave",  "—"))
    y = row(y, "📊", "明日 最大うねり", "Tomorrow Max Swell Height", wd.get("num_max_swell", "—"))
    if wd.get("num_swell_period"):
        y = row(y, "🌀", "明日 うねり周期", "Tomorrow Swell Period",     wd.get("num_swell_period", "—"))
    y = row(y, "💨", "明日 最大風速",  "Tomorrow Max Wind Speed",   wd.get("num_max_wind",  "—"))
    if wd.get("num_max_gust"):
        y = row(y, "💨", "明日 最大突風",  "Tomorrow Max Wind Gust",    wd.get("num_max_gust",  "—"))

    y += 12
    # 【情報源 / Sources】
    y = section_header(y, "情報源", "Sources")
    draw.text((80, y),    "気象庁（jma.go.jp）  /  Open-Meteo Marine API",   font=f["src"], fill="#BBDEFB", anchor="lm")
    draw.text((80, y+30), "座間味村HP（vill.zamami.okinawa.jp）",             font=f["src"], fill="#BBDEFB", anchor="lm")
    draw.text((80, y+60), "Zamami Village official site / JMA (jma.go.jp)", font=f["src"], fill="#7986CB", anchor="lm")

    # フッター
    draw.line([(60, 1020),(1020, 1020)], fill="#334E7A", width=1)
    draw.text((540, 1044), "※欠航判断は船会社・座間味村が行います。本データはAI予測の参考値です。",
              font=f["foot"], fill="#546E7A", anchor="mm")
    draw.text((540, 1066), "*Cancellation is determined by ferry operators. AI-based estimates for reference only.",
              font=f["foot"], fill="#455A64", anchor="mm")

    img.save(output_path)
    print(f"  画像④保存: {output_path}")


def generate_images(forecast, output_dir="/tmp/ferry_images"):
    """3枚の画像を生成してパスのリストを返す（表紙なし: 短期予報→長期予報→気象データ）"""
    os.makedirs(output_dir, exist_ok=True)
    now = datetime.now(JST)
    ts = now.strftime("%Y%m%d_%H%M")

    paths = {
        "short":       f"{output_dir}/img1_short_{ts}.png",
        "longterm":    f"{output_dir}/img2_longterm_{ts}.png",
        "weatherdata": f"{output_dir}/img3_weatherdata_{ts}.png",
    }

    make_image_short(forecast, paths["short"])
    make_image_longterm(forecast, paths["longterm"])
    make_image_weather_data(forecast, paths["weatherdata"])

    print(f"  画像3枚生成成功!")
    return paths


# ============================================================
# 3. 投稿文生成（Claude API）
# ============================================================

def generate_post_text(forecast):
    """X投稿用テキストを日英で生成"""
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    short = forecast["short_term"]
    lt = forecast["long_term"]

    def _hs_line(d):
        if d.get('suspended_highspeed'):
            return f"高速船 運休（{d.get('suspension_reason_ja','ドック入り')}）"
        return f"高速船欠航リスク {d['highspeed_pct']}%（午前{d['highspeed_am_pct']}% / 午後{d['highspeed_pm_pct']}%）"

    def _fe_line(d):
        if d.get('suspended_ferry'):
            return f"フェリー 運休（{d.get('suspension_reason_ja','ドック入り')}）"
        return f"フェリー欠航リスク {d['ferry_pct']}%"

    situation = f"""
【短期予報】
明日({short[0]['date_label']}):
  {_hs_line(short[0])}
  {_fe_line(short[0])}
  気象庁予報: {short[0]['jma_wave'] or 'なし'}

明後日({short[1]['date_label']}):
  {_hs_line(short[1])}
  {_fe_line(short[1])}

【長期予報（3〜7日先）】
リスク期間: {lt['risk_period']}
最大リスク: {lt['max_pct']}%
"""

    prompt = f"""
あなたは「沖縄フェリー予報」というSNSアカウントの運営者です。
以下のデータをもとにX（Twitter）投稿文を生成してください。

{situation}

要件：
- 日本語版と英語版の2つを生成
- 各280文字以内（X制限）
- 数値は必ず含める
- 「AI予測・参考値」の免責を1行で含める
- 公式確認を促すリンク案内を含める（URLは[URL]と表記）
- ハッシュタグ: #座間味島 #フェリー #沖縄離島 #ZamamiIsland #OkinawaFerry
- トーン: 中立・実用的・信頼感のある

出力形式:
【日本語】
（投稿文）

【English】
（投稿文）
"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


def generate_instagram_caption(forecast):
    """Instagram用キャプション（日英）を生成"""
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    short = forecast["short_term"]
    lt = forecast["long_term"]

    prompt = f"""
「沖縄フェリー予報」Instagram用キャプションを生成してください。

データ:
- 明日 高速船{short[0]['highspeed_pct']}% フェリー{short[0]['ferry_pct']}%
- 明後日 高速船{short[1]['highspeed_pct']}% フェリー{short[1]['ferry_pct']}%
- 長期リスク期間: {lt['risk_period']} 最大{lt['max_pct']}%

要件：
- 日本語メイン・英語サブ
- 数値を含む実用的な内容
- 旅行者が判断できる情報（島にいる人・これから来る人両方）
- 免責: 「AI予測・参考値。公式は座間味村HPへ」
- ハッシュタグ（日英混在）: #座間味島 #フェリー #沖縄離島 #沖縄旅行 #ZamamiIsland #OkinawaFerry #JapanTravel #IslandHopping
- 500文字以内
"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


# ============================================================
# 4. Google Sheets DB保存
# ============================================================

def save_to_sheets(forecast, analysis):
    """予測データをGoogle Sheetsに保存"""
    sheets_id = os.environ.get("GOOGLE_SHEETS_ID")
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not sheets_id or not service_account_json:
        print("  [スキップ] Google Sheets未設定")
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
            scopes=["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive"]
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheets_id)

        # daily_forecastシートに記録
        # 段階導入：将来のロジスティック回帰用に未使用メトリクスも全列蓄積する
        HEADER = [
            "recorded_at", "target_date", "wave_height_max",
            "swell_height_max", "wind_speed_max", "jma_wave_text",
            "jma_prob_level", "cancellation_score",
            "predicted_pct_highspeed", "predicted_pct_ferry",
            # ↓ v4 追加メトリクス
            "wind_gust_max", "swell_period_max", "wave_period_max",
            "wind_wave_height_max", "visibility_min", "precip_max",
            "wave_direction", "wind_direction",
            "typhoon_tier", "typhoon_dist_km",
            # ↓ シャドー比較用：台風フロア適用前（現行ロジック相当）
            # predicted_pct_* は台風フロア適用後（最終値）。base と比較して台風寄与を分離評価する。
            "highspeed_pct_base", "ferry_pct_base",
        ]
        try:
            ws = sh.worksheet("daily_forecast")
            # 既存シートのヘッダーが旧形式なら最新ヘッダーへ更新（列ズレ防止）
            try:
                existing_header = ws.row_values(1)
                if "wind_gust_max" not in existing_header:
                    ws.update("A1", [HEADER])
                    print("  daily_forecastヘッダーをv4形式に更新")
            except Exception as he:
                print(f"  [警告] ヘッダー更新スキップ: {he}")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet("daily_forecast", rows=1000, cols=len(HEADER) + 2)
            ws.append_row(HEADER)

        now = datetime.now(JST)
        short = forecast["short_term"]

        for day in short:
            date = day["date"]
            all_day = analysis.get(date, {}).get("all_day") or {}
            tphn = day.get("typhoon") or {}
            row = [
                now.strftime("%Y-%m-%d %H:%M"),
                date,
                all_day.get("max_wave", ""),
                all_day.get("max_swell", ""),
                all_day.get("max_wind", ""),
                day.get("jma_wave", ""),
                day.get("jma_prob", ""),
                all_day.get("cancellation_score", ""),
                day["highspeed_pct"],
                day["ferry_pct"],
                all_day.get("max_gust", ""),
                all_day.get("max_swell_period", ""),
                all_day.get("max_wave_period", ""),
                all_day.get("max_wind_wave", ""),
                all_day.get("min_visibility", ""),
                all_day.get("max_precip", ""),
                all_day.get("wave_direction", ""),
                all_day.get("wind_direction", ""),
                tphn.get("tier", ""),
                tphn.get("dist_km", ""),
                day.get("highspeed_pct_base", ""),
                day.get("ferry_pct_base", ""),
            ]
            ws.append_row(row)

        print(f"  ✅ Google Sheets保存完了（{len(short)}件）")

    except Exception as e:
        print(f"  [警告] Google Sheets保存エラー: {e}")


# ============================================================
# 5. SNS投稿
# ============================================================

def post_to_x(text):
    """X（Twitter）にテキスト投稿"""
    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=os.environ.get("X_API_KEY"),
            consumer_secret=os.environ.get("X_API_SECRET"),
            access_token=os.environ.get("X_ACCESS_TOKEN"),
            access_token_secret=os.environ.get("X_ACCESS_SECRET"),
        )
        response = client.create_tweet(text=text)
        print(f"  ✅ X投稿完了: tweet_id={response.data['id']}")
        return True
    except Exception as e:
        print(f"  [警告] X投稿エラー: {e}")
        return False


def upload_image_to_github(image_path, filename):
    """画像をGitHubリポジトリのimages/に上げてraw URLを返す"""
    import base64
    import requests

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise Exception("GITHUB_TOKEN / GITHUB_REPOSITORY が未設定")

    with open(image_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    target_path = f"images/{filename}"
    api_url = f"https://api.github.com/repos/{repo}/contents/{target_path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    existing = requests.get(api_url, headers=headers)
    sha = existing.json().get("sha") if existing.status_code == 200 else None

    data = {"message": f"Auto: update {filename}", "content": content, "branch": "main"}
    if sha:
        data["sha"] = sha

    resp = requests.put(api_url, json=data, headers=headers)
    if resp.status_code in (200, 201):
        owner = repo.split("/")[0]
        repo_name = repo.split("/")[1]
        return f"https://{owner}.github.io/{repo_name}/{target_path}"
    raise Exception(f"GitHub upload failed: {resp.status_code} {resp.text[:200]}")


def post_to_instagram(image_paths, caption):
    """Instagram にカルーセル投稿（画像3枚）"""
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    user_id = os.environ.get("INSTAGRAM_USER_ID")

    if not access_token or not user_id:
        print("  [スキップ] Instagram未設定")
        return False

    try:
        import requests

        # Step1: 画像をGitHubにアップロードしてpublic URLを取得
        print("  [Instagram] 画像をGitHubにアップロード中...")
        image_urls = []
        for path in image_paths:
            filename = os.path.basename(path)
            raw_url = upload_image_to_github(path, filename)
            image_urls.append(raw_url)
            print(f"    {raw_url}")

        # GitHub Pages のビルド完了を待つ（通常30〜90秒）
        import time
        print("  [Instagram] GitHub Pages ビルド待機中（90秒）...")
        time.sleep(90)

        # Step2: 各画像のカルーセルアイテムコンテナを作成
        media_ids = []
        for img_url in image_urls:
            resp = requests.post(
                f"https://graph.facebook.com/v25.0/{user_id}/media",
                params={"image_url": img_url, "is_carousel_item": "true", "access_token": access_token}
            )
            data = resp.json()
            if "id" not in data:
                print(f"  [エラー] メディアコンテナ作成失敗: {data}")
                return False
            media_ids.append(data["id"])
            print(f"  メディアコンテナ: {data['id']}")

        # Step3: カルーセルコンテナを作成
        resp = requests.post(
            f"https://graph.facebook.com/v25.0/{user_id}/media",
            params={
                "media_type": "CAROUSEL",
                "children": ",".join(media_ids),
                "caption": caption,
                "access_token": access_token
            }
        )
        data = resp.json()
        if "id" not in data:
            print(f"  [エラー] カルーセルコンテナ作成失敗: {data}")
            return False
        carousel_id = data["id"]
        print(f"  カルーセルコンテナ: {carousel_id}")

        # カルーセルコンテナの処理完了を待つ
        print("  [Instagram] カルーセル処理待機中（30秒）...")
        time.sleep(30)

        # Step4: 投稿を公開
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
# 6. メイン処理（ferry_alert.pyから呼び出す）
# ============================================================

def run_publisher(analysis, jma_waves, jma_prob, planned_suspensions=None, post_to_social=True,
                  typhoon_by_date=None):
    """
    ferry_alert.pyのrun_ferry_check()から呼び出すメイン関数。
    forecast_dataを構築し、画像生成・投稿文生成・DB保存・SNS投稿を実行。
    """
    now = datetime.now(JST)
    print(f"\n{'='*50}")
    print(f"Ferry Forecast Publisher: {now.strftime('%Y-%m-%d %H:%M')}")
    print('='*50)

    # 1. 予測データ構築
    print("\n[P1] 予測データ構築中...")
    forecast = build_forecast_data(analysis, jma_waves, jma_prob, planned_suspensions,
                                   typhoon_by_date=typhoon_by_date)

    short = forecast["short_term"]
    print(f"  明日: 高速船{short[0]['highspeed_pct']}% / フェリー{short[0]['ferry_pct']}%")
    print(f"  明後日: 高速船{short[1]['highspeed_pct']}% / フェリー{short[1]['ferry_pct']}%")
    print(f"  長期: {forecast['long_term']['risk_period']} 最大{forecast['long_term']['max_pct']}%")

    # 2. 画像生成
    print("\n[P2] 画像生成中...")
    image_paths = generate_images(forecast)

    # 3. 投稿文生成
    print("\n[P3] 投稿文生成中...")
    short = forecast["short_term"]
    lt = forecast["long_term"]

    # 長期期間の表記
    if lt["has_risk"]:
        lt_period_ja = lt["risk_period"]
        lt_period_en = lt["risk_period_en"]
    else:
        lt_start_en = lt.get("lt_period_en", "")
        lt_period_ja = f"{short[0].get('date_label', '')[:-2] if short else ''}13〜17"  # fallback
        # 長期日付を days から取得
        if lt.get("days"):
            import datetime as _dt
            d0 = _dt.datetime.strptime(lt["days"][0]["date"], "%Y-%m-%d")
            d1 = _dt.datetime.strptime(lt["days"][-1]["date"], "%Y-%m-%d")
            lt_period_ja = f"{d0.strftime('%-m/%-d')}〜{d1.strftime('%-m/%-d')}"
            lt_period_en = f"{d0.strftime('%b %-d')} - {d1.strftime('%b %-d')}"
        else:
            lt_period_en = lt.get("lt_period_en", "")

    # リスクレベル別コメント（** による強調はAI感が出るため使用しない）
    # 運航中船種の短期最大%でティアを判定（effective_max_pct に集約）
    max_hs = max(effective_max_pct(short[0]), effective_max_pct(short[1]))

    if max_hs <= 10 and not lt['has_risk']:
        # Tier 1: 極低リスク
        comment_ja = (
            "\n🟢 今週は全日程で欠航リスク極めて低め！\n"
            "島滞在中の方も、これから渡航予定の方も安心してプランを組めそうです。\n"
        )
        comment_en = (
            "\n🟢 Low cancellation risk all week — great time to visit!\n"
        )
    elif max_hs <= 30 and not lt['has_risk']:
        # Tier 2: 低リスク
        comment_ja = (
            "\n🟢 欠航リスクは低い見込みです。\n"
            "出発前に最新の予報をご確認ください。\n"
        )
        comment_en = (
            "\n🟢 Cancellation risk looks low. Check the forecast before departure.\n"
        )
    elif max_hs <= 30 and lt['has_risk']:
        # Tier 3: 短期低・長期注意
        comment_ja = (
            "\n🟡 短期は問題なし。ただし来週以降に荒れる可能性があります。\n"
            "引き続き予報をチェックしていきましょう。\n"
        )
        comment_en = (
            "\n🟡 Short-term looks fine, but rougher conditions may develop next week. Keep an eye on forecasts.\n"
        )
    elif max_hs <= 60:
        # Tier 4: 中リスク
        comment_ja = (
            "\n🟡 現時点では運航見込みですが、想定より荒天が進めば欠航リスクが出てきます。\n"
            "最新情報をご確認ください。\n"
        )
        comment_en = (
            "\n🟡 Currently operating, but cancellations may occur if conditions worsen. Check latest info.\n"
        )
    elif max_hs <= 80:
        # Tier 5: 高リスク
        comment_ja = (
            "\n🔴 高速船の欠航リスクが高い状況です。\n"
            "旅程は余裕をもって組んでおくことをおすすめします。最新情報は座間味村HPへ。\n"
        )
        comment_en = (
            "\n🔴 High cancellation risk. Consider scheduling with some flexibility. Check Zamami Village website.\n"
        )
    else:
        # Tier 6: 極高リスク
        comment_ja = (
            "\n🚨 高速船の欠航可能性が非常に高い状況です。\n"
            "島内滞在中の方は帰島日の前倒しをご検討ください。渡航予定の方は旅程変更も選択肢に。\n"
        )
        comment_en = (
            "\n🚨 Very high cancellation risk. Guests on the island should consider an earlier return. Those planning to visit may want to reconsider.\n"
        )

    # Instagram キャプションはテンプレート固定
    # generate_instagram_caption() による API 生成は廃止（フォーマット崩れ防止）
    ig_caption = (
        f"{forecast['update_date_ja']} {forecast['generated_at_label']}\n"
        f"座間味島・阿嘉島・慶留間島 フェリー欠航予報\n"
        f"\n"
        + (
            f"⚠️ クイーンざまみ（高速船）は{forecast['planned_suspensions'][0]['start'][5:].replace('-','/')}〜{forecast['planned_suspensions'][0]['end'][5:].replace('-','/')}ドック入り運休中\n"
            if forecast.get("planned_suspensions") else ""
        )
        + f"■船舶欠航可能性\n"
        + (
            f"明日 {short[0]['date_label']}  高速船 運休（{short[0].get('suspension_reason_ja','ドック入り')}） / フェリー{short[0]['ferry_pct']}%\n"
            if short[0].get("suspended_highspeed") else
            f"明日 {short[0]['date_label']}  高速船 {short[0]['highspeed_pct']}% / フェリー{short[0]['ferry_pct']}%\n"
        )
        + (
            f"明後日 {short[1]['date_label']} 高速船 運休（{short[1].get('suspension_reason_ja','ドック入り')}） / フェリー{short[1]['ferry_pct']}%\n"
            if short[1].get("suspended_highspeed") else
            f"明後日 {short[1]['date_label']} 高速船{short[1]['highspeed_pct']}% / フェリー{short[1]['ferry_pct']}%\n"
        )
        + f"長期（{lt_period_ja}）: {lt['risk_period'] if lt['has_risk'] else '懸念なし'} 最大{lt['max_pct']}%\n"
        + f"{comment_ja}"
        + f"⚠️ AI予測・参考値です。公式情報は座間味村HPを参照ください。\n"
        + f"#座間味島 #阿嘉島 #慶留間島 #フェリー #沖縄離島\n"
        + f"\n"
        + f"\n"
        + f"{forecast['update_date_en']} updated\n"
        + f"Kerama Islands (Zamami, Aka, Geruma) Ferry Cancellation Forecast\n"
        + f"\n"
        + f"■Boat/Ferry Cancellation Risk\n"
        + (
            f"Tomorrow ({short[0].get('date_label_en', '')}) High-Speed Boat Suspended ({short[0].get('suspension_reason_en','Dock Maintenance')}) / Ferry {short[0]['ferry_pct']}%\n"
            if short[0].get("suspended_highspeed") else
            f"Tomorrow ({short[0].get('date_label_en', '')}) High-Speed Boat {short[0]['highspeed_pct']}% / Ferry {short[0]['ferry_pct']}%\n"
        )
        + (
            f"Day After ({short[1].get('date_label_en', '')}) High-Speed Boat Suspended ({short[1].get('suspension_reason_en','Dock Maintenance')}) / Ferry {short[1]['ferry_pct']}%\n"
            if short[1].get("suspended_highspeed") else
            f"Day After ({short[1].get('date_label_en', '')}) High-Speed Boat {short[1]['highspeed_pct']}% / Ferry {short[1]['ferry_pct']}%\n"
        )
        + f"Long-term ({lt_period_en}): {'No significant Risk' if not lt['has_risk'] else lt['risk_period_en']}, max.{lt['max_pct']}%\n"
        + f"{comment_en}"
        + f"⚠️ AI-based estimate, for reference only\n"
        + f"Check the official Zamami Village Website for confirmed info\n"
        + f"\n"
        + f"#KeramaIslands #ZamamiIsland #OkinawaFerry"
    )
    print("  Instagramキャプション生成完了（テンプレート使用）")

    try:
        post_text = generate_post_text(forecast)
        print("  X投稿文生成完了")
    except Exception as e:
        print(f"  [エラー] X投稿文生成失敗: {e}")
        post_text = None

    # 4. DB保存
    print("\n[P4] DB保存中...")
    try:
        save_to_sheets(forecast, analysis)
    except Exception as e:
        print(f"  [警告] DB保存エラー: {e}")

    # 5. SNS投稿
    if post_to_social:
        print("\n[P5] SNS投稿中...")

        # X: 日本語版を抽出して投稿
        if post_text:
            if "【日本語】" in post_text:
                ja_text = post_text.split("【日本語】")[1].split("【English】")[0].strip()
            else:
                ja_text = post_text[:280]

            x_configured = all([
                os.environ.get("X_API_KEY"),
                os.environ.get("X_API_SECRET"),
                os.environ.get("X_ACCESS_TOKEN"),
                os.environ.get("X_ACCESS_SECRET"),
            ])

            if x_configured:
                post_to_x(ja_text)
            else:
                print("  [スキップ] X API未設定")

        post_to_instagram(list(image_paths.values()), ig_caption or "")
    else:
        print("\n[P5] SNS投稿スキップ（API未設定またはpost_to_social=False）")

    print("\n--- 生成された投稿文 ---")
    if post_text:
        print(post_text[:500])

    print(f"\n--- 生成された画像 ---")
    for k, v in image_paths.items():
        print(f"  {k}: {v}")

    return forecast, image_paths, post_text


# ============================================================
# テスト実行
# ============================================================

if __name__ == "__main__":
    # ferry_alert.pyのデータなしでテスト用ダミーデータで動作確認
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    JST = ZoneInfo("Asia/Tokyo")
    now = datetime.now(JST)

    # ダミーanalysis
    dummy_analysis = {}
    for delta in range(8):
        date = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
        score = 0.2 + delta * 0.08
        dummy_analysis[date] = {
            "delta": delta,
            "all_day": {
                "max_wave": round(1.0 + delta * 0.5, 1),
                "avg_wave": round(0.8 + delta * 0.4, 1),
                "max_swell": round(0.5 + delta * 0.3, 1),
                "max_wind": round(5.0 + delta * 1.5, 1),
                "cancellation_score": round(min(score, 0.95), 3),
                "risk_highspeed": score >= 0.45,
                "risk_ferry": score >= 0.65,
                "has_warning": delta >= 5,
            },
            "morning": {
                "max_wave": round(0.9 + delta * 0.4, 1),
                "cancellation_score": round(min(score * 0.9, 0.95), 3),
                "risk_highspeed": score * 0.9 >= 0.45,
            },
            "afternoon": {
                "max_wave": round(1.1 + delta * 0.6, 1),
                "cancellation_score": round(min(score * 1.1, 0.95), 3),
                "risk_highspeed": score * 1.1 >= 0.45,
            },
        }

    dummy_jma_waves = {"今日": "１メートル後２メートル", "明日": "２メートル後３メートル", "明後日": "３メートル"}
    dummy_jma_prob = {"明日": {"level": "中"}, "明後日": {"level": "高"}}

    forecast, paths, text = run_publisher(
        dummy_analysis,
        dummy_jma_waves,
        dummy_jma_prob,
        post_to_social=False  # テスト時はSNS投稿しない
    )
