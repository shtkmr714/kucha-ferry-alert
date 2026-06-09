"""
fit_wave_only.py
波高単独で欠航%を直接ロジスティック回帰でフィットし、
高速船・フェリーそれぞれの wave→pct パラメータ（変曲点・急峻さ）を導出する。
dock/equipment は除外。6/3・6/4 のような記録ミスも reason!=weather で自動除外される。
"""
import os, json, math
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
JST = ZoneInfo("Asia/Tokyo")

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

def fit_logistic(wave, y):
    """logistic: p = 1/(1+exp(-k*(wave - x0)))  を最尤推定。戻り値 (x0, k)"""
    from scipy.optimize import minimize
    wave=np.array(wave,float); y=np.array(y,int)
    def nll(params):
        x0,k=params
        z=k*(wave-x0)
        z=np.clip(z,-30,30)
        p=1/(1+np.exp(-z)); p=np.clip(p,1e-7,1-1e-7)
        return -np.sum(y*np.log(p)+(1-y)*np.log(1-p))
    res=minimize(nll,[2.0,3.0],method="Nelder-Mead",options={"xatol":1e-4,"fatol":1e-4})
    return res.x

def pct_at(wave,x0,k):
    return round(100/(1+math.exp(-k*(wave-x0))))

if __name__=="__main__":
    print(f"波高単独フィット  {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    gc=connect()
    rows=gc.open_by_key(os.environ["GOOGLE_SHEETS_ID"]).worksheet("daily_operation_log").get_all_records()
    since=datetime(2024,12,1).date()
    recs=[]
    for r in rows:
        d=str(r.get("date","")).strip()
        try:
            if datetime.strptime(d,"%Y-%m-%d").date()<since: continue
        except: continue
        wave=to_f(r.get("wave_max"))
        if wave is None: continue
        recs.append({
            "wave":wave,
            "hs_reason":str(r.get("hs_cancel_reason","none")).lower(),
            "fe_reason":str(r.get("ferry_cancel_reason","none")).lower(),
            "hs_wx":1 if (str(r.get("hs_am_weather_cancel"))=="1" or str(r.get("hs_pm_weather_cancel"))=="1") else 0,
            "fe_wx":1 if str(r.get("ferry_weather_cancel"))=="1" else 0,
            "ferry_op":r.get("ferry_operated"),
        })

    for name,key,exc in [("高速船","hs_wx",("dock","equipment")),("フェリー","fe_wx",("dock","equipment"))]:
        rk=key.replace("_wx","_reason")
        v=[r for r in recs if r[rk] not in exc]
        if name=="フェリー":
            v=[r for r in v if r["ferry_op"] not in (None,"")]
        wave=[r["wave"] for r in v]; y=[r[key] for r in v]
        x0,k=fit_logistic(wave,y)
        print(f"\n{'='*56}\n  【{name}】 n={len(v)} 欠航={sum(y)}日")
        print(f"{'='*56}")
        print(f"  波高ロジスティック: 変曲点(50%)={x0:.2f}m, 急峻さ={k:.2f}")
        print(f"\n  波高 → 欠航% （フィット結果）")
        for w in [1.0,1.5,2.0,2.5,3.0,3.5,4.0]:
            print(f"    波{w:.1f}m → {pct_at(w,x0,k):3d}%")
        # 実測バケット
        print(f"\n  実測（波高0.5m刻み・参考）")
        buckets={}
        for r in v:
            b=int(r["wave"]*2)/2
            buckets.setdefault(b,{"n":0,"c":0}); buckets[b]["n"]+=1; buckets[b]["c"]+=r[key]
        for b in sorted(buckets):
            bb=buckets[b]
            print(f"    波{b:.1f}m: 実欠航率={bb['c']/bb['n']:.0%} (n={bb['n']})")
