"""
feature_analysis.py
欠航予測の特徴量選択分析（座間味）。
波高・うねり・風速のうち、どれが本当に説明力を持つかを統計的に検証する。

分析:
  1. サンプル妥当性（events per variable）
  2. 特徴量間の相関（交絡・多重共線性の確認）
  3. VIF（分散拡大係数）
  4. 単変量AUC（各変数単独の判別力）
  5. 多変量ロジスティック回帰の係数とp値（交絡を制御した有意性）
  6. ネストモデル比較（波のみ vs 波+風 vs 波+うねり+風）をAIC・交差検証AUCで
  7. 推奨
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


def load(gc):
    rows = gc.open_by_key(os.environ["GOOGLE_SHEETS_ID"]).worksheet("daily_operation_log").get_all_records()
    since = datetime(2024,12,1).date()
    out=[]
    for r in rows:
        d=str(r.get("date","")).strip()
        try:
            if datetime.strptime(d,"%Y-%m-%d").date()<since: continue
        except: continue
        wave=to_f(r.get("wave_max")); swell=to_f(r.get("swell_height")); wind=to_f(r.get("wind_speed_max"))
        if None in (wave,wind): continue
        out.append({
            "date":d,"wave":wave,"swell":swell if swell is not None else 0.0,"wind":wind,
            "hs_reason":str(r.get("hs_cancel_reason","none")).lower(),
            "fe_reason":str(r.get("ferry_cancel_reason","none")).lower(),
            "hs_wx":1 if (str(r.get("hs_am_weather_cancel"))=="1" or str(r.get("hs_pm_weather_cancel"))=="1") else 0,
            "fe_wx":1 if str(r.get("ferry_weather_cancel"))=="1" else 0,
            "ferry_op":r.get("ferry_operated"),
        })
    return out


def corr(a,b):
    a=np.array(a,float); b=np.array(b,float)
    if a.std()==0 or b.std()==0: return float("nan")
    return float(np.corrcoef(a,b)[0,1])


def auc_score(x, y):
    """単変量AUC（Mann-Whitney U基準）"""
    x=np.array(x,float); y=np.array(y,int)
    pos=x[y==1]; neg=x[y==0]
    if len(pos)==0 or len(neg)==0: return float("nan")
    wins=0.0
    for p in pos:
        wins += np.sum(p>neg) + 0.5*np.sum(p==neg)
    return wins/(len(pos)*len(neg))


def vif(X, names):
    """各特徴量のVIF（他特徴量で回帰したR^2から）"""
    import numpy as np
    res={}
    for i,nm in enumerate(names):
        y=X[:,i]; Xo=np.delete(X,i,axis=1)
        Xo=np.column_stack([np.ones(len(Xo)),Xo])
        beta,_,_,_=np.linalg.lstsq(Xo,y,rcond=None)
        pred=Xo@beta
        ss_res=np.sum((y-pred)**2); ss_tot=np.sum((y-y.mean())**2)
        r2=1-ss_res/ss_tot if ss_tot>0 else 0
        res[nm]=1/(1-r2) if r2<1 else float("inf")
    return res


def analyze(name, recs, target_key, exclude_reasons):
    print("\n"+"="*64)
    print(f"  【{name}】")
    print("="*64)
    valid=[r for r in recs if r[target_key.replace('_wx','_reason')] not in exclude_reasons]
    if name=="フェリー":
        valid=[r for r in valid if r["ferry_op"] not in (None,"")]
    y=np.array([r[target_key] for r in valid],int)
    wave=[r["wave"] for r in valid]; swell=[r["swell"] for r in valid]; wind=[r["wind"] for r in valid]
    n=len(valid); events=int(y.sum())
    print(f"\n  サンプル数 n={n}  欠航イベント={events}日  欠航率={events/n:.0%}")
    print(f"  events-per-variable (EPV): 3変数なら {events/3:.1f}（目安≥10）/ 1変数なら {events:.0f}")
    if events/3 < 10:
        print(f"  ⚠️ 3変数を安定推定するにはイベント不足。変数は1〜2個に絞るべき。")

    print(f"\n  --- 単変量AUC（0.5=無意味, 1.0=完全判別）---")
    for nm,x in [("波高",wave),("うねり",swell),("風速",wind)]:
        print(f"    {nm}: AUC={auc_score(x,y):.3f}")

    print(f"\n  --- 特徴量間の相関（交絡の確認）---")
    print(f"    波高×うねり: {corr(wave,swell):+.2f}")
    print(f"    波高×風速:   {corr(wave,wind):+.2f}")
    print(f"    うねり×風速: {corr(swell,wind):+.2f}")

    X=np.column_stack([wave,swell,wind])
    print(f"\n  --- VIF（>5で多重共線性の疑い, >10で深刻）---")
    for nm,v in vif(X,["波高","うねり","風速"]).items():
        flag = "  ⚠️" if v>5 else ""
        print(f"    {nm}: {v:.1f}{flag}")

    # 多変量ロジスティック回帰（statsmodels）
    try:
        import statsmodels.api as sm
        Xs=(X-X.mean(0))/X.std(0)  # 標準化（係数比較のため）
        Xc=sm.add_constant(Xs)
        model=sm.Logit(y,Xc).fit(disp=0, maxiter=200)
        print(f"\n  --- 多変量ロジスティック回帰（標準化係数・交絡を相互制御）---")
        names=["切片","波高","うねり","風速"]
        for nm,c,p in zip(names,model.params,model.pvalues):
            sig = "***" if p<0.01 else ("**" if p<0.05 else ("*" if p<0.1 else "  有意でない"))
            print(f"    {nm:6s}: 係数={c:+.2f}  p値={p:.3f}  {sig}")
        print(f"    擬似R²(McFadden)={model.prsquared:.3f}  AIC={model.aic:.1f}")
    except Exception as e:
        print(f"  [statsmodels エラー] {e}")

    # ネストモデル比較（交差検証AUC）
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import StratifiedKFold
        from sklearn.metrics import roc_auc_score
        from sklearn.preprocessing import StandardScaler
        feats={
            "波のみ":[0],
            "波+風":[0,2],
            "波+うねり":[0,1],
            "波+うねり+風(現行)":[0,1,2],
        }
        print(f"\n  --- ネストモデル比較（5分割交差検証AUC・高いほど良い）---")
        Xall=np.column_stack([wave,swell,wind])
        k=min(5,events) if events>=2 else 2
        for label,idx in feats.items():
            Xf=Xall[:,idx]
            aucs=[]
            skf=StratifiedKFold(n_splits=k,shuffle=True,random_state=42)
            for tr,te in skf.split(Xf,y):
                if len(set(y[te]))<2: continue
                sc=StandardScaler().fit(Xf[tr])
                m=LogisticRegression(max_iter=1000).fit(sc.transform(Xf[tr]),y[tr])
                pr=m.predict_proba(sc.transform(Xf[te]))[:,1]
                aucs.append(roc_auc_score(y[te],pr))
            print(f"    {label:18s}: CV-AUC={np.mean(aucs):.3f} (±{np.std(aucs):.3f})")
    except Exception as e:
        print(f"  [sklearn エラー] {e}")


if __name__=="__main__":
    print(f"特徴量選択分析  {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    gc=connect()
    recs=load(gc)
    print(f"  対象データ: {len(recs)}日（2024-12〜）")
    analyze("高速船", recs, "hs_wx", ("dock","equipment"))
    analyze("フェリー", recs, "fe_wx", ("dock","equipment"))

    print("\n\n"+"="*64)
    print("  読み方")
    print("="*64)
    print("""
  ・単変量AUCが0.5付近の変数は単独では無意味。
  ・多変量で p値>0.1 の変数は「他を制御すると説明力なし」＝交絡の可能性。
  ・VIF>5 は変数同士が重複情報（波高と高相関なら片方で十分）。
  ・ネスト比較で『波のみ』と『現行』のCV-AUCが同等なら、追加変数は不要。
  ・EPV<10 なら過学習リスク高 → 変数を絞るべき。
""")
