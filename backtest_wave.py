"""
backtest_wave.py
波高単独モデル（新）を実績に当てはめ、合成スコアモデル（旧）と比較。
6/3・6/4等のdock誤記録は reason!=weather で自動除外される。
"""
import os, json, math
from datetime import datetime
from zoneinfo import ZoneInfo
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

def sig(x,x0,k): return round(100/(1+math.exp(-k*(x-x0))))
# 新（波高単独）
def hs_new(w): return sig(w,2.01,4.92) if w is not None else 1
def fe_new(w): return sig(w,2.68,7.34) if w is not None else 1

def metrics(preds,act,thr):
    tp=fp=tn=fn=0
    for p,a in zip(preds,act):
        if p>=thr and a==1: tp+=1
        elif p>=thr and a==0: fp+=1
        elif p<thr and a==0: tn+=1
        else: fn+=1
    prec=tp/(tp+fp) if tp+fp else 0; rec=tp/(tp+fn) if tp+fn else 0
    f1=2*prec*rec/(prec+rec) if prec+rec else 0
    return tp,fp,tn,fn,prec,rec,f1
def brier(preds,act): return sum((p/100-a)**2 for p,a in zip(preds,act))/len(preds) if preds else None

if __name__=="__main__":
    print(f"波高単独モデル バックテスト  {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    gc=connect()
    rows=gc.open_by_key(os.environ["GOOGLE_SHEETS_ID"]).worksheet("daily_operation_log").get_all_records()
    since=datetime(2024,12,1).date()
    recs=[]
    for r in rows:
        d=str(r.get("date","")).strip()
        try:
            if datetime.strptime(d,"%Y-%m-%d").date()<since: continue
        except: continue
        w=to_f(r.get("wave_max"))
        if w is None: continue
        recs.append({"date":d,"wave":w,
            "hs_reason":str(r.get("hs_cancel_reason","none")).lower(),
            "fe_reason":str(r.get("ferry_cancel_reason","none")).lower(),
            "hs_wx":1 if (str(r.get("hs_am_weather_cancel"))=="1" or str(r.get("hs_pm_weather_cancel"))=="1") else 0,
            "fe_wx":1 if str(r.get("ferry_weather_cancel"))=="1" else 0,
            "ferry_op":r.get("ferry_operated")})

    for name,key,pf in [("高速船","hs_wx",hs_new),("フェリー","fe_wx",fe_new)]:
        rk=key.replace("_wx","_reason")
        v=[r for r in recs if r[rk] not in ("dock","equipment")]
        if name=="フェリー": v=[r for r in v if r["ferry_op"] not in (None,"")]
        preds=[pf(r["wave"]) for r in v]; act=[r[key] for r in v]
        print(f"\n{'='*56}\n  【{name}】波高単独モデル n={len(v)} 欠航={sum(act)}日\n{'='*56}")
        print(f"  Brier={brier(preds,act):.4f}")
        for thr in [50,61]:
            tp,fp,tn,fn,prec,rec,f1=metrics(preds,act,thr)
            print(f"  閾値{thr}%: TP={tp} FP={fp} TN={tn} FN={fn} | 適合率={prec:.0%} 検出率={rec:.0%} F1={f1:.2f}")
        # 擬陽性・見逃し
        fps=[r for r in v if pf(r['wave'])>=61 and r[key]==0]
        fns=[r for r in v if pf(r['wave'])<50 and r[key]==1]
        print(f"  擬陽性(予測61%以上・運航): {len(fps)}件 " + ", ".join(f"{r['date']}(波{r['wave']}m)" for r in fps))
        print(f"  見逃し(予測50%未満・欠航): {len(fns)}件 " + ", ".join(f"{r['date']}(波{r['wave']}m)" for r in fns))
