"""
backtest_new_params.py
新しいsigmoidパラメータを過去の実績データに当てはめ、
旧パラメータと比較して予測性能を検証する（高速船・フェリー別）。

旧: 高速船 inflection=0.42 steepness=14 / フェリー inflection=0.52 steepness=12
新: 高速船 inflection=0.352 steepness=28.18 / フェリー inflection=0.43 steepness=30
"""
import os, json, math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

OLD_HS = (0.42, 14.0);   NEW_HS = (0.352, 28.18)
OLD_FE = (0.52, 12.0);   NEW_FE = (0.43, 30.0)


def connect():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)


def to_f(v):
    try: return float(v)
    except: return None


def calc_score(wave, swell, wind):
    return (min((wave or 0)/5.0,1.0)*0.35 +
            min((swell or 0)/4.0,1.0)*0.30 +
            min((wind or 0)/20.0,1.0)*0.20)


def sigmoid(score, p):
    inflection, steepness = p
    return 100/(1+math.exp(-steepness*(score-inflection)))


def metrics(preds, actuals, thr):
    """閾値thr%以上を欠航予測とした混同行列＋指標"""
    tp=fp=tn=fn=0
    for p,a in zip(preds,actuals):
        if p>=thr and a==1: tp+=1
        elif p>=thr and a==0: fp+=1
        elif p<thr and a==0: tn+=1
        else: fn+=1
    n=tp+fp+tn+fn
    prec = tp/(tp+fp) if tp+fp else 0
    rec  = tp/(tp+fn) if tp+fn else 0
    f1   = 2*prec*rec/(prec+rec) if prec+rec else 0
    acc  = (tp+tn)/n if n else 0
    return tp,fp,tn,fn,prec,rec,f1,acc


def brier(preds, actuals):
    if not preds: return None
    return sum((p/100-a)**2 for p,a in zip(preds,actuals))/len(preds)


def calib(preds, actuals, label):
    print(f"\n  [{label} キャリブレーション]")
    buckets={i:{"n":0,"c":0} for i in range(0,100,10)}
    for p,a in zip(preds,actuals):
        b=min(int(p//10)*10,90); buckets[b]["n"]+=1; buckets[b]["c"]+=a
    for b in sorted(buckets):
        v=buckets[b]
        if v["n"]==0: continue
        print(f"    予測{b:2d}〜{b+9}%: 実欠航率={v['c']/v['n']:.0%} (n={v['n']})")


def report(name, scores, actuals, old_p, new_p):
    print("\n" + "="*60)
    print(f"  【{name}】 n={len(scores)}  実欠航={sum(actuals)}日")
    print("="*60)
    old_pred=[sigmoid(s,old_p) for s in scores]
    new_pred=[sigmoid(s,new_p) for s in scores]

    print(f"\n  Brier Score: 旧={brier(old_pred,actuals):.4f}  新={brier(new_pred,actuals):.4f}  (小さいほど良い)")

    for thr in [50, 61]:
        print(f"\n  --- 判定閾値 {thr}% ---")
        for tag,pred in [("旧",old_pred),("新",new_pred)]:
            tp,fp,tn,fn,prec,rec,f1,acc = metrics(pred,actuals,thr)
            print(f"    {tag}: TP={tp} FP={fp} TN={tn} FN={fn} | "
                  f"適合率={prec:.0%} 検出率={rec:.0%} F1={f1:.2f} 正解率={acc:.0%}")

    calib(old_pred, actuals, f"{name} 旧パラメータ")
    calib(new_pred, actuals, f"{name} 新パラメータ")


if __name__ == "__main__":
    print(f"新旧パラメータ バックテスト  {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    gc = connect()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEETS_ID"])
    rows = sh.worksheet("daily_operation_log").get_all_records()
    since = datetime(2024,12,1).date()

    hs_scores=[]; hs_act=[]
    fe_scores=[]; fe_act=[]
    for r in rows:
        d=str(r.get("date","")).strip()
        try:
            if datetime.strptime(d,"%Y-%m-%d").date() < since: continue
        except: continue
        wave=to_f(r.get("wave_max")); swell=to_f(r.get("swell_height")); wind=to_f(r.get("wind_speed_max"))
        if wave is None or wind is None: continue
        score=calc_score(wave,swell,wind)

        hs_reason=str(r.get("hs_cancel_reason","none")).lower()
        if hs_reason not in ("dock","equipment"):
            hs_scores.append(score)
            hs_act.append(1 if (str(r.get("hs_am_weather_cancel"))=="1" or
                                str(r.get("hs_pm_weather_cancel"))=="1") else 0)

        fe_reason=str(r.get("ferry_cancel_reason","none")).lower()
        if fe_reason not in ("dock","equipment") and r.get("ferry_operated") not in (None,""):
            fe_scores.append(score)
            fe_act.append(1 if str(r.get("ferry_weather_cancel"))=="1" else 0)

    report("高速船", hs_scores, hs_act, OLD_HS, NEW_HS)
    report("フェリー", fe_scores, fe_act, OLD_FE, NEW_FE)

    print("\n\n=== 総括 ===")
    print("Brier改善・F1改善があれば新パラメータが優位。")
    print("※ 学習に使った同じデータでの検証（in-sample）のため、")
    print("  真の汎化性能は今後の新規データで継続検証すること。")
