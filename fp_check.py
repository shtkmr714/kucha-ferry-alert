"""
fp_check.py
新パラメータでの擬陽性（高リスク予測したが実際は運航）と
見逃し（低リスク予測したが実際は欠航）の具体日を一覧化する。
"""
import os, json, math
from datetime import datetime
from zoneinfo import ZoneInfo
JST = ZoneInfo("Asia/Tokyo")

NEW_HS = (0.352, 28.18)
NEW_FE = (0.43, 30.0)

def connect():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)

def to_f(v):
    try: return float(v)
    except: return None

def score(wave,swell,wind):
    return min((wave or 0)/5.0,1.0)*0.35 + min((swell or 0)/4.0,1.0)*0.30 + min((wind or 0)/20.0,1.0)*0.20

def sig(s,p): return round(100/(1+math.exp(-p[1]*(s-p[0]))))

if __name__=="__main__":
    gc=connect()
    rows=gc.open_by_key(os.environ["GOOGLE_SHEETS_ID"]).worksheet("daily_operation_log").get_all_records()
    since=datetime(2024,12,1).date()
    recs=[]
    for r in rows:
        d=str(r.get("date","")).strip()
        try:
            if datetime.strptime(d,"%Y-%m-%d").date()<since: continue
        except: continue
        wave=to_f(r.get("wave_max")); swell=to_f(r.get("swell_height")); wind=to_f(r.get("wind_speed_max"))
        if wave is None or wind is None: continue
        sc=score(wave,swell,wind)
        recs.append({
            "date":d,"wave":wave,"swell":swell,"wind":wind,"score":sc,
            "hs_pct":sig(sc,NEW_HS),"fe_pct":sig(sc,NEW_FE),
            "hs_reason":str(r.get("hs_cancel_reason","none")).lower(),
            "fe_reason":str(r.get("ferry_cancel_reason","none")).lower(),
            "hs_wx":1 if (str(r.get("hs_am_weather_cancel"))=="1" or str(r.get("hs_pm_weather_cancel"))=="1") else 0,
            "fe_wx":1 if str(r.get("ferry_weather_cancel"))=="1" else 0,
            "ferry_op":r.get("ferry_operated"),
            "ann":str(r.get("announcement_text",""))[:50],
        })

    for thr in [50,61]:
        print(f"\n{'='*64}\n  判定閾値 {thr}%\n{'='*64}")

        print(f"\n  ◆高速船 擬陽性（予測{thr}%以上だが気象欠航せず・dock/equip除く）")
        hs_fp=[r for r in recs if r["hs_pct"]>=thr and r["hs_wx"]==0 and r["hs_reason"] not in ("dock","equipment")]
        if not hs_fp: print("    なし")
        for r in hs_fp:
            print(f"    {r['date']}: 予測{r['hs_pct']}% 波{r['wave']}m うねり{r['swell']}m 風{r['wind']}m/s "
                  f"score{r['score']:.2f} 理由={r['hs_reason']}")

        print(f"\n  ◆フェリー 擬陽性（予測{thr}%以上だが気象欠航せず・dock/equip除く）")
        fe_fp=[r for r in recs if r["fe_pct"]>=thr and r["fe_wx"]==0 and r["fe_reason"] not in ("dock","equipment")
               and r["ferry_op"] not in (None,"")]
        if not fe_fp: print("    なし")
        for r in fe_fp:
            print(f"    {r['date']}: 予測{r['fe_pct']}% 波{r['wave']}m うねり{r['swell']}m 風{r['wind']}m/s "
                  f"score{r['score']:.2f} 運航={r['ferry_op']} 理由={r['fe_reason']}")

    # 参考: 高速船の見逃し（予測50%未満だが気象欠航）
    print(f"\n{'='*64}\n  参考: 見逃し（予測50%未満だが気象欠航）\n{'='*64}")
    print(f"\n  ◆高速船")
    for r in [r for r in recs if r["hs_pct"]<50 and r["hs_wx"]==1]:
        print(f"    {r['date']}: 予測{r['hs_pct']}% 波{r['wave']}m 風{r['wind']}m/s score{r['score']:.2f}")
    print(f"\n  ◆フェリー")
    for r in [r for r in recs if r["fe_pct"]<50 and r["fe_wx"]==1]:
        print(f"    {r['date']}: 予測{r['fe_pct']}% 波{r['wave']}m 風{r['wind']}m/s score{r['score']:.2f}")
