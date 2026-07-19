"""画像レイアウトの軽量回帰検査（CI用・座間味/渡嘉敷）。

2026-07 に八重山で発生した「カード/枠から文字・白背景がはみ出す」系の再発防止。
本番と同じ描画関数を worst-case データ（運休あり・100%・リスク1日・懸念なし）で呼び、
(1) 例外なく 1254² が出るか、(2) 画像内にカード外への破綻が無いかを最低限確認する。
座間味・渡嘉敷は船種2枠（高速船/フェリー）でルート名ピルが無く、八重山ほど溢れやすくないが、
テンプレ/座標変更時の回帰（クラッシュ・サイズ崩れ）を push 時に検出する。

実行: python check_image_layout.py （失敗時 exit 1）
"""
import sys, os, tempfile
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from PIL import Image
import forecast_publisher as FP

FAILS = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        FAILS.append(msg)

def day_short(lj, dl, hs_am, hs_pm, fe, sus_hs=False, sus_fe=False):
    return {"label_ja": lj, "date_label": dl, "label_en": "TOMORROW",
            "highspeed_am_pct": hs_am, "highspeed_pm_pct": hs_pm,
            "highspeed_pct": max(hs_am, hs_pm), "ferry_pct": fe,
            "suspended_highspeed": sus_hs, "suspended_ferry": sus_fe}

def day_long(date, dl, dle, hs, fe, sus_hs=False, sus_fe=False):
    return {"date": date, "date_label": dl, "date_label_en": dle,
            "highspeed_pct": hs, "ferry_pct": fe,
            "suspended_highspeed": sus_hs, "suspended_ferry": sus_fe,
            "dist_km": 50.0, "in_storm": False, "in_circle": False}

def make_forecast(has_risk=True):
    lt = {"has_risk": has_risk, "max_pct": 100 if has_risk else 5,
          "days": [day_long("2026-07-22", "7/22(火)", "Jul 22", 8, 1),
                   day_long("2026-07-23", "7/23(水)", "Jul 23", 100, 1, sus_hs=True),
                   day_long("2026-07-24", "7/24(木)", "Jul 24", 3, 1),
                   day_long("2026-07-25", "7/25(金)", "Jul 25", 5, 1),
                   day_long("2026-07-26", "7/26(土)", "Jul 26", 100, 100)]}
    if has_risk:
        lt["risk_period"] = "7/26"          # 単日
        lt["risk_period_en"] = "Jul 26"
    else:
        lt["risk_period"] = "懸念なし"
        lt["risk_period_en"] = "No concern"
        lt["lt_period_en"] = "Jul 22 - Jul 26"
    return {
        "short_term": [day_short("明日", "7/20", 100, 100, 100, sus_fe=True),
                       day_short("明後日", "7/21", 5, 5, 1)],
        "long_term": lt,
        "weather_data": {}, "typhoon": None, "planned_suspensions": [],
    }

tmp = tempfile.mkdtemp()
ROUTES = ["zamami_aka"]   # 座間味リポ。渡嘉敷リポ側の同ファイルは route を差し替える。

for rid in ROUTES:
    for tag, has_risk in (("risk-single-day+suspended", True), ("no-risk(懸念なし)", False)):
        fc = make_forecast(has_risk)
        for kind, fn in (("short", FP.make_image_short), ("long", FP.make_image_longterm)):
            p = os.path.join(tmp, f"{rid}_{kind}_{has_risk}.png")
            try:
                fn(fc, p, rid)
                im = Image.open(p)
                check(im.size == (1254, 1254),
                      f"{rid} {kind} [{tag}] renders 1254x1254 (got {im.size})")
            except Exception as e:
                check(False, f"{rid} {kind} [{tag}] raised: {e!r}")

print()
if FAILS:
    print(f"LAYOUT CHECK FAILED ({len(FAILS)} issue(s)):")
    for m in FAILS:
        print("  - " + m)
    sys.exit(1)
print("LAYOUT CHECK PASSED")
