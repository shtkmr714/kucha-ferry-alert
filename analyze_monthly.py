"""
analyze_monthly.py
2026年1〜5月の月別 気象欠航率を算出する。
MODE=zamami: daily_operation_log（高速船・フェリー）
MODE=yaeyama: yaeyama_operation_log（航路別・全体）
※ 5/9以前は手入力期（運航実績・欠航理由は正確）。気象欠航のみ対象（dock/equipment除外）。
"""
import os, json
from datetime import datetime
from collections import defaultdict

def connect(sheet_id):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds).open_by_key(sheet_id)

def ym(d):
    try: return datetime.strptime(str(d).strip(), "%Y-%m-%d").strftime("%Y-%m")
    except: return None

MONTHS = ["2026-01","2026-02","2026-03","2026-04","2026-05"]

def analyze_zamami():
    sh = connect(os.environ["GOOGLE_SHEETS_ID"])
    rows = sh.worksheet("daily_operation_log").get_all_records()
    # 月別: 便ベース欠航率（高速船=最大3便/日, フェリー=1便/日）と日ベース
    agg = {m: {"days":0, "hs_bins_tot":0, "hs_bins_wx":0, "hs_day_wx":0,
               "fe_bins_tot":0, "fe_bins_wx":0} for m in MONTHS}
    for r in rows:
        m = ym(r.get("date"))
        if m not in agg: continue
        a = agg[m]; a["days"] += 1
        hs_reason = str(r.get("hs_cancel_reason","none")).lower()
        fe_reason = str(r.get("ferry_cancel_reason","none")).lower()
        # 高速船 便別（bin1-3）
        hs_wx_day = 0
        for b in ["hs_bin1_operated","hs_bin2_operated","hs_bin3_operated"]:
            v = r.get(b)
            if v in (None,"",): continue
            try: v = int(float(v))
            except: continue
            a["hs_bins_tot"] += 1
            if v == 0 and hs_reason == "weather":
                a["hs_bins_wx"] += 1; hs_wx_day = 1
        a["hs_day_wx"] += hs_wx_day
        # フェリー（1便）
        fv = r.get("ferry_operated")
        if fv not in (None,""):
            try: fv = int(float(fv))
            except: fv = None
            if fv is not None:
                a["fe_bins_tot"] += 1
                if fv == 0 and fe_reason == "weather":
                    a["fe_bins_wx"] += 1

    print("=== 座間味 月別 気象欠航率 ===")
    print(f"{'月':<9}{'日数':>5}{'高速便数':>8}{'高速欠航':>8}{'高速便率':>8}{'高速日率':>8}{'ﾌｪﾘｰ便':>8}{'ﾌｪﾘｰ欠':>7}{'ﾌｪﾘｰ率':>8}")
    for m in MONTHS:
        a = agg[m]
        if a["days"]==0:
            print(f"{m:<9}{'データなし':>5}"); continue
        hs_bin_rate = a["hs_bins_wx"]/a["hs_bins_tot"] if a["hs_bins_tot"] else 0
        hs_day_rate = a["hs_day_wx"]/a["days"]
        fe_rate = a["fe_bins_wx"]/a["fe_bins_tot"] if a["fe_bins_tot"] else 0
        print(f"{m:<9}{a['days']:>5}{a['hs_bins_tot']:>8}{a['hs_bins_wx']:>8}{hs_bin_rate:>7.0%}{hs_day_rate:>8.0%}"
              f"{a['fe_bins_tot']:>8}{a['fe_bins_wx']:>7}{fe_rate:>8.0%}")
    # JSON出力（グラフ用）
    out = {m: {"hs_bin_rate": round(agg[m]["hs_bins_wx"]/agg[m]["hs_bins_tot"],4) if agg[m]["hs_bins_tot"] else None,
               "fe_bin_rate": round(agg[m]["fe_bins_wx"]/agg[m]["fe_bins_tot"],4) if agg[m]["fe_bins_tot"] else None,
               "days": agg[m]["days"]} for m in MONTHS}
    print("JSON_ZAMAMI=" + json.dumps(out))

def analyze_yaeyama():
    sh = connect(os.environ["GOOGLE_SHEETS_ID_YAEYAMA"])
    rows = sh.worksheet("yaeyama_operation_log").get_all_records()
    ROUTES = {"route1":"大原","route3":"竹富","route5":"上原","route6":"波照間","route7":"鳩間"}
    # 月×航路: route-day欠航率（hs_weather_cancel）
    agg = {m: defaultdict(lambda: {"days":0,"wx":0}) for m in MONTHS}
    for r in rows:
        m = ym(r.get("date"))
        if m not in agg: continue
        rid = str(r.get("route_id",""))
        if rid not in ROUTES: continue
        reason = str(r.get("hs_cancel_reason","none")).lower()
        if reason in ("dock","equipment"): continue
        agg[m][rid]["days"] += 1
        if str(r.get("hs_weather_cancel"))=="1":
            agg[m][rid]["wx"] += 1
    print("=== 八重山 月別 気象欠航率（航路別 route-day率）===")
    hdr = f"{'月':<9}" + "".join(f"{ROUTES[r]:>7}" for r in ROUTES) + f"{'全体':>8}"
    print(hdr)
    out = {}
    for m in MONTHS:
        cells = []; tot_d=0; tot_w=0
        for rid in ROUTES:
            d = agg[m][rid]["days"]; w = agg[m][rid]["wx"]; tot_d+=d; tot_w+=w
            cells.append(f"{(w/d if d else 0):>6.0%}" if d else f"{'--':>6}")
        overall = tot_w/tot_d if tot_d else 0
        print(f"{m:<9}" + "".join(f"{c:>7}" for c in cells) + f"{overall:>8.0%}")
        out[m] = {"overall_rate": round(overall,4) if tot_d else None,
                  "by_route": {rid: round(agg[m][rid]['wx']/agg[m][rid]['days'],4) if agg[m][rid]['days'] else None for rid in ROUTES}}
    print("JSON_YAEYAMA=" + json.dumps(out, ensure_ascii=False))

if __name__ == "__main__":
    mode = os.environ.get("MODE","zamami")
    if mode == "zamami": analyze_zamami()
    else: analyze_yaeyama()
