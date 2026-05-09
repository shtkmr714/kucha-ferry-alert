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

# フォントパス
FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD    = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
FONT_MEDIUM  = "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"

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
    感覚値ベース:
      波1m・風5m/s  (score≈0.16) → 2%
      波2m・風10m/s (score≈0.31) → 5%
      波3m・風15m/s (score≈0.51) → 38%
      波4m・風18m/s (score≈0.65) → 78%（欠航ライン・文脈依存）
      波5m+          (score≈0.80) → 97%
    フェリーは変曲点を高めに・緩やかに上昇。
    """
    inflection = 0.58
    steepness  = 12.0
    pct = 100 / (1 + math.exp(-steepness * (score - inflection)))
    return round(min(max(pct, 1), 99))


def build_forecast_data(analysis, jma_waves, jma_prob):
    """
    analysisから投稿・画像用データを構築。
    短期（明日・明後日）と長期（3〜7日）の欠航可能性%を返す。
    """
    now = datetime.now(JST)
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

        short_term.append({
            "date": date,
            "date_label": date_label,
            "label_ja": label,
            "label_en": label_en,
            "highspeed_pct": hs_pct,
            "highspeed_am_pct": hs_am_pct,
            "highspeed_pm_pct": hs_pm_pct,
            "ferry_pct": fe_pct,
            "jma_wave": jma_waves.get(label, ""),
            "jma_prob": jma_prob.get(label, {}).get("level", ""),
            "max_wave": all_day["max_wave"] if all_day else None,
        })

    long_term = []
    risk_dates = []
    for delta in range(3, 8):
        date = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
        dt = now + timedelta(days=delta)
        all_day = analysis.get(date, {}).get("all_day")
        if all_day:
            hs_pct = score_to_pct_highspeed(all_day["cancellation_score"])
            long_term.append({
                "date": date,
                "date_label": dt.strftime("%-m/%-d"),
                "highspeed_pct": hs_pct,
                "ferry_pct": score_to_pct_ferry(all_day["cancellation_score"]),
            })
            if hs_pct >= 31:
                risk_dates.append(dt)

    # 長期リスク期間のサマリー
    if risk_dates:
        risk_start = risk_dates[0].strftime("%-m/%-d")
        risk_end = risk_dates[-1].strftime("%-m/%-d")
        risk_start_en = risk_dates[0].strftime("%b %-d")
        risk_end_en = risk_dates[-1].strftime("%b %-d")
        max_lt_pct = max(d["highspeed_pct"] for d in long_term)
        long_term_summary = {
            "has_risk": True,
            "risk_period": f"{risk_start}〜{risk_end}頃",
            "risk_period_en": f"Around {risk_start_en} - {risk_end_en}",
            "max_pct": max_lt_pct,
            "days": long_term,
        }
    else:
        max_lt_pct = max((d["highspeed_pct"] for d in long_term), default=0)
        long_term_summary = {
            "has_risk": False,
            "risk_period": "懸念なし",
            "risk_period_en": "No concern",
            "max_pct": max_lt_pct,
            "days": long_term,
        }

    return {
        "short_term": short_term,
        "long_term": long_term_summary,
        "generated_at": now.strftime("%Y/%m/%d %H:%M"),
        "generated_at_label": "8:15更新" if now.hour < 11 else "13:00更新",
    }


# ============================================================
# 2. 画像生成
# ============================================================

def make_image_header(forecast, output_path):
    """画像①: ヘッダー（日英併記・3島名）"""
    img = Image.new("RGB", IMG_SIZE, color=hex_to_rgb("#0D47A1"))
    draw = ImageDraw.Draw(img)
    try:
        f = {
            "islands_ja": ImageFont.truetype(FONT_BOLD, 62),
            "islands_en": ImageFont.truetype(FONT_MEDIUM, 32),
            "area":       ImageFont.truetype(FONT_REGULAR, 28),
            "service_ja": ImageFont.truetype(FONT_MEDIUM, 48),
            "service_en": ImageFont.truetype(FONT_REGULAR, 30),
            "date":       ImageFont.truetype(FONT_BOLD, 48),
            "update":     ImageFont.truetype(FONT_REGULAR, 28),
            "url":        ImageFont.truetype(FONT_REGULAR, 24),
            "xs":         ImageFont.truetype(FONT_REGULAR, 19),
        }
    except Exception:
        f = {k: ImageFont.load_default() for k in ["islands_ja","islands_en","area","service_ja","service_en","date","update","url","xs"]}

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
    """画像②: 短期予報（日英併記・確定版）"""
    short = forecast["short_term"]
    max_pct = max(d["highspeed_pct"] for d in short)
    img = Image.new("RGB", IMG_SIZE, color=hex_to_rgb(get_bg_color(max_pct)))
    draw = ImageDraw.Draw(img)
    try:
        f = {
            "title_ja": ImageFont.truetype(FONT_BOLD, 46),
            "title_en": ImageFont.truetype(FONT_MEDIUM, 28),
            "island":   ImageFont.truetype(FONT_REGULAR, 24),
            "date":     ImageFont.truetype(FONT_MEDIUM, 34),
            "date_en":  ImageFont.truetype(FONT_REGULAR, 26),
            "pct":      ImageFont.truetype(FONT_BOLD, 90),
            "type_ja":  ImageFont.truetype(FONT_MEDIUM, 30),
            "ampm":     ImageFont.truetype(FONT_REGULAR, 24),
            "jma":      ImageFont.truetype(FONT_REGULAR, 19),
            "xs":       ImageFont.truetype(FONT_REGULAR, 18),
        }
    except Exception:
        f = {k: ImageFont.load_default() for k in ["title_ja","title_en","island","date","date_en","pct","type_ja","ampm","jma","xs"]}

    draw.text((540, 48), "フェリー欠航可能性 短期予報", font=f["title_ja"], fill="white", anchor="mm")
    draw.text((540, 90), "Ferry Cancellation Risk  /  Short-term Forecast", font=f["title_en"], fill="rgba(255,255,255,200)", anchor="mm")
    draw.text((540, 118), "座間味島・阿嘉島・慶留間島  Zamami / Aka / Geruma", font=f["island"], fill="rgba(255,255,255,160)", anchor="mm")
    draw.line([(80,135),(1000,135)], fill="rgba(255,255,255,100)", width=1)

    positions = [270, 810]
    for i, day in enumerate(short[:2]):
        x = positions[i]

        # 日付（明日 5/9 + Tomorrow）
        draw.text((x, 175), f"{day['label_ja']}  {day['date_label']}", font=f["date"], fill="white", anchor="mm")
        draw.text((x, 210), day["label_en"], font=f["date_en"], fill="rgba(255,255,255,180)", anchor="mm")

        # 高速船（High Speed Boat）
        draw.text((x, 320), f"{day['highspeed_pct']}%", font=f["pct"], fill="white", anchor="mm")
        draw.text((x, 382), "高速船  High Speed Boat", font=f["type_ja"], fill="rgba(255,255,255,220)", anchor="mm")
        draw.text((x, 416), f"AM {day['highspeed_am_pct']}%  /  PM {day['highspeed_pm_pct']}%", font=f["ampm"], fill="rgba(255,255,255,180)", anchor="mm")
        draw.line([(x-110,445),(x+110,445)], fill="rgba(255,255,255,90)", width=1)

        # フェリー（Carなし・運航見込みなし）
        draw.text((x, 548), f"{day['ferry_pct']}%", font=f["pct"], fill="white", anchor="mm")
        draw.text((x, 610), "フェリー  Ferry", font=f["type_ja"], fill="rgba(255,255,255,220)", anchor="mm")

        # 気象庁・早期注意
        if day["jma_wave"]:
            draw.text((x, 690), f"気象庁: {day['jma_wave']}", font=f["jma"], fill="rgba(255,255,255,160)", anchor="mm")
        if day["jma_prob"]:
            draw.text((x, 714), f"早期注意(波浪): {day['jma_prob']}", font=f["jma"], fill="rgba(255,255,255,160)", anchor="mm")

    draw.line([(540,145),(540,750)], fill="rgba(255,255,255,70)", width=1)
    draw.line([(80,765),(1000,765)], fill="rgba(255,255,255,60)", width=1)
    draw.text((540, 800), "※AI予測・参考値。公式情報は座間味村HPをご確認ください。", font=f["xs"], fill="rgba(255,255,255,160)", anchor="mm")
    draw.text((540, 825), "*AI-based estimate. Check official Zamami Village website for confirmed info.", font=f["xs"], fill="rgba(255,255,255,140)", anchor="mm")

    img.save(output_path)
    print(f"  画像②保存: {output_path}")


def make_image_longterm(forecast, output_path):
    """画像③: 長期予報（日英併記・左右2列棒グラフ・確定版）"""
    lt = forecast["long_term"]
    max_pct = lt["max_pct"]
    img = Image.new("RGB", IMG_SIZE, color=hex_to_rgb(get_bg_color(max_pct)))
    draw = ImageDraw.Draw(img)
    try:
        f = {
            "title_ja": ImageFont.truetype(FONT_BOLD, 44),
            "title_en": ImageFont.truetype(FONT_MEDIUM, 26),
            "island":   ImageFont.truetype(FONT_REGULAR, 22),
            "head":     ImageFont.truetype(FONT_MEDIUM, 32),
            "head_en":  ImageFont.truetype(FONT_REGULAR, 24),
            "period":   ImageFont.truetype(FONT_BOLD, 64),
            "pct":      ImageFont.truetype(FONT_BOLD, 76),
            "label":    ImageFont.truetype(FONT_MEDIUM, 28),
            "label_en": ImageFont.truetype(FONT_REGULAR, 22),
            "col_hd":   ImageFont.truetype(FONT_MEDIUM, 22),
            "bar":      ImageFont.truetype(FONT_REGULAR, 21),
            "xs":       ImageFont.truetype(FONT_REGULAR, 17),
        }
    except Exception:
        f = {k: ImageFont.load_default() for k in ["title_ja","title_en","island","head","head_en","period","pct","label","label_en","col_hd","bar","xs"]}

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

        # 高速船・フェリー最大%を横並び
        hs_max = max(d["highspeed_pct"] for d in lt["days"])
        fe_max = max(d["ferry_pct"] for d in lt["days"])
        for x, pct, lja, len_ in [
            (270, hs_max, "高速船", "High Speed Boat"),
            (810, fe_max, "フェリー", "Ferry"),
        ]:
            draw.text((x, 362), lja, font=f["label"], fill="rgba(255,255,255,200)", anchor="mm")
            draw.text((x, 388), len_, font=f["label_en"], fill="rgba(255,255,255,170)", anchor="mm")
            draw.text((x, 453), f"{pct}%", font=f["pct"], fill="white", anchor="mm")
            draw.text((x, 500), "最大欠航可能性 / Max Risk", font=f["label_en"], fill="rgba(255,255,255,160)", anchor="mm")
        draw.line([(540,333),(540,520)], fill="rgba(255,255,255,60)", width=1)
    else:
        draw.text((540, 300), "懸念なし  /  No Significant Risk", font=f["period"], fill="white", anchor="mm")

    # 左右2列の横棒グラフ
    draw.line([(80,530),(1000,530)], fill="rgba(255,255,255,70)", width=1)
    draw.text((290, 552), "高速船  High Speed Boat", font=f["col_hd"], fill="white", anchor="mm")
    draw.text((790, 552), "フェリー  Ferry", font=f["col_hd"], fill="white", anchor="mm")
    draw.line([(540,535),(540,1030)], fill="rgba(255,255,255,50)", width=1)

    bar_top, bar_h, row_sp = 580, 28, 72
    cols = [
        {"date_x": 155, "bar_x": 175, "bar_max": 270, "pct_x": 455, "key": "highspeed_pct"},
        {"date_x": 595, "bar_x": 615, "bar_max": 270, "pct_x": 895, "key": "ferry_pct"},
    ]
    for i, d in enumerate(lt["days"][:5]):
        y = bar_top + i * row_sp
        dt = datetime.strptime(d["date"], "%Y-%m-%d")
        label = dt.strftime("%-m/%-d")  # 棒グラフは5/11形式
        for col in cols:
            pct = d[col["key"]]
            bar_w = int(col["bar_max"] * pct / 100)
            draw.text((col["date_x"], y + bar_h//2), label, font=f["bar"], fill="white", anchor="rm")
            draw.rectangle([(col["bar_x"], y),(col["bar_x"]+col["bar_max"], y+bar_h)], fill=(0,0,0,50))
            if bar_w > 0:
                draw.rectangle([(col["bar_x"], y),(col["bar_x"]+bar_w, y+bar_h)], fill=(255,255,255,210))
            draw.text((col["pct_x"], y + bar_h//2), f"{pct}%", font=f["bar"], fill="white", anchor="lm")

    draw.line([(80,960),(1000,960)], fill="rgba(255,255,255,40)", width=1)
    draw.text((540, 985), "※AI予測・参考値。公式情報は座間味村HPをご確認ください。", font=f["xs"], fill="rgba(255,255,255,140)", anchor="mm")
    draw.text((540, 1006), "*AI-based estimate. Check official Zamami Village website.", font=f["xs"], fill="rgba(255,255,255,120)", anchor="mm")

    img.save(output_path)
    print(f"  画像③保存: {output_path}")


def generate_images(forecast, output_dir="/tmp/ferry_images"):
    """3枚の画像を生成してパスのリストを返す"""
    os.makedirs(output_dir, exist_ok=True)
    now = datetime.now(JST)
    ts = now.strftime("%Y%m%d_%H%M")

    paths = {
        "header":    f"{output_dir}/img1_header_{ts}.png",
        "short":     f"{output_dir}/img2_short_{ts}.png",
        "longterm":  f"{output_dir}/img3_longterm_{ts}.png",
    }

    make_image_header(forecast, paths["header"])
    make_image_short(forecast, paths["short"])
    make_image_longterm(forecast, paths["longterm"])

    return paths


# ============================================================
# 3. 投稿文生成（Claude API）
# ============================================================

def generate_post_text(forecast):
    """X投稿用テキストを日英で生成"""
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    short = forecast["short_term"]
    lt = forecast["long_term"]

    situation = f"""
【短期予報】
明日({short[0]['date_label']}):
  高速船欠航リスク {short[0]['highspeed_pct']}%（午前{short[0]['highspeed_am_pct']}% / 午後{short[0]['highspeed_pm_pct']}%）
  フェリー欠航リスク {short[0]['ferry_pct']}%
  気象庁予報: {short[0]['jma_wave'] or 'なし'}

明後日({short[1]['date_label']}):
  高速船欠航リスク {short[1]['highspeed_pct']}%（午前{short[1]['highspeed_am_pct']}% / 午後{short[1]['highspeed_pm_pct']}%）
  フェリー欠航リスク {short[1]['ferry_pct']}%

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
    import gspread
    from google.oauth2.service_account import Credentials
    sheets_id = os.environ.get("GOOGLE_SHEETS_ID")
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not sheets_id or not service_account_json:
        print("  [スキップ] Google Sheets未設定")
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
        try:
            ws = sh.worksheet("daily_forecast")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet("daily_forecast", rows=1000, cols=15)
            ws.append_row([
                "recorded_at", "target_date", "wave_height_max",
                "swell_height_max", "wind_speed_max", "jma_wave_text",
                "jma_prob_level", "cancellation_score",
                "predicted_pct_highspeed", "predicted_pct_ferry"
            ])

        now = datetime.now(JST)
        short = forecast["short_term"]

        for day in short:
            date = day["date"]
            all_day = analysis.get(date, {}).get("all_day") or {}
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
        return f"https://raw.githubusercontent.com/{repo}/main/{target_path}"
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

def run_publisher(analysis, jma_waves, jma_prob, post_to_social=True):
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
    forecast = build_forecast_data(analysis, jma_waves, jma_prob)

    short = forecast["short_term"]
    print(f"  明日: 高速船{short[0]['highspeed_pct']}% / フェリー{short[0]['ferry_pct']}%")
    print(f"  明後日: 高速船{short[1]['highspeed_pct']}% / フェリー{short[1]['ferry_pct']}%")
    print(f"  長期: {forecast['long_term']['risk_period']} 最大{forecast['long_term']['max_pct']}%")

    # 2. 画像生成
    print("\n[P2] 画像生成中...")
    image_paths = generate_images(forecast)

    # 3. 投稿文生成
    print("\n[P3] 投稿文生成中...")
    try:
        post_text = generate_post_text(forecast)
        print("  X投稿文生成完了")
        ig_caption = generate_instagram_caption(forecast)
        print("  Instagramキャプション生成完了")
    except Exception as e:
        print(f"  [エラー] 投稿文生成失敗: {e}")
        post_text = ig_caption = None

    # 4. DB保存
    print("\n[P4] DB保存中...")
    save_to_sheets(forecast, analysis)

    # 5. SNS投稿
    if post_to_social and post_text:
        print("\n[P5] SNS投稿中...")

        # X: 日本語版を抽出して投稿
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
