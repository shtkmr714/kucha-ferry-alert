"""
analyze_zamami_full.py
座間味航路 欠航予測 精度分析（2024年12月以降 全データ）

分析内容:
  1. データ品質チェック（欠損・スクレイプ失敗・理由誤分類）
  2. 気象条件と欠航の相関（波高・うねり・風速の分布比較）
  3. 現行モデルのキャリブレーション（予測% vs 実欠航率）
  4. 特徴量重要度（どの気象変数が最も予測力があるか）
  5. ロジスティック回帰による新しい閾値導出
  6. 朝の波高 vs 日次最大値の比較
  7. JMA情報の有効性検証
  8. 総合提案（新しいパラメータ案）
"""

import os
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

# ============================================================
# 接続
# ============================================================

def connect_sheets():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds)


def to_float(v, default=None):
    try:    return float(v)
    except: return default


def to_int(v, default=None):
    try:    return int(float(v))
    except: return default


# ============================================================
# データ取得
# ============================================================

def load_operation_log(gc, sheets_id, since_date):
    """daily_operation_log を全件取得してフィルタリング"""
    sh = gc.open_by_key(sheets_id)
    ws = sh.worksheet("daily_operation_log")
    rows = ws.get_all_records()
    print(f"  daily_operation_log: 総行数 {len(rows)}")
    records = []
    for row in rows:
        d = str(row.get("date","")).strip()
        if not d: continue
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except: continue
        if dt < since_date: continue
        records.append({
            "date":               d,
            "dt":                 dt,
            "month":              to_int(row.get("month"), dt.month),
            "announcement_text":  str(row.get("announcement_text","")),
            "weather_desc":       str(row.get("weather_desc","")),
            # 高速船
            "hs_bin1":            to_int(row.get("hs_bin1_operated")),
            "hs_bin2":            to_int(row.get("hs_bin2_operated")),
            "hs_bin3":            to_int(row.get("hs_bin3_operated")),
            "hs_reason":          str(row.get("hs_cancel_reason","none")).lower(),
            "hs_am_wx":           to_int(row.get("hs_am_weather_cancel"),0),
            "hs_pm_wx":           to_int(row.get("hs_pm_weather_cancel"),0),
            # フェリー
            "fe_op":              to_int(row.get("ferry_operated")),
            "fe_reason":          str(row.get("ferry_cancel_reason","none")).lower(),
            "fe_wx":              to_int(row.get("ferry_weather_cancel"),0),
            # 気象（全日）
            "wave_am":            to_float(row.get("wave_am")),
            "wave_pm":            to_float(row.get("wave_pm")),
            "wave_max":           to_float(row.get("wave_max")),
            "swell":              to_float(row.get("swell_height")),
            "wind_am":            to_float(row.get("wind_speed_am")),
            "wind_pm":            to_float(row.get("wind_speed_pm")),
            "wind_max":           to_float(row.get("wind_speed_max")),
            # JMA
            "jma_today":          str(row.get("jma_wave_today","")),
            "jma_warn_today":     str(row.get("jma_warning_today","なし")),
            "jma_warn_tmr":       str(row.get("jma_warning_tomorrow","なし")),
            # 台風
            "typhoon_active":     to_int(row.get("typhoon_active"),0),
            # モデル予測値
            "model_hs_pct":       to_float(row.get("model_highspeed_pct")),
            "model_fe_pct":       to_float(row.get("model_ferry_pct")),
        })
    return records


# ============================================================
# 分析関数
# ============================================================

def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def data_quality_check(recs):
    print_section("1. データ品質チェック")
    n = len(recs)
    n_no_announce = sum(1 for r in recs if not r["announcement_text"])
    n_no_wave     = sum(1 for r in recs if r["wave_max"] is None)
    n_no_model    = sum(1 for r in recs if r["model_hs_pct"] is None)
    n_hs_none     = sum(1 for r in recs if r["hs_bin1"] is None)

    print(f"\n  総レコード数:              {n}")
    print(f"  announcement_text 欠損:   {n_no_announce} ({n_no_announce/n:.0%})")
    print(f"  wave_max 欠損:            {n_no_wave} ({n_no_wave/n:.0%})")
    print(f"  model_hs_pct 欠損:        {n_no_model} ({n_no_model/n:.0%})")
    print(f"  hs_bin1 欠損:             {n_hs_none} ({n_hs_none/n:.0%})")

    # 理由別欠航日
    by_reason = {}
    for r in recs:
        reason = r["hs_reason"] if r["hs_bin1"] == 0 or r["hs_bin2"] == 0 else "operated"
        by_reason[reason] = by_reason.get(reason, 0) + 1
    print(f"\n  高速船 理由別集計:")
    for k, v in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"    {k:15s}: {v}日")

    # announcement_text が空なのに欠航している日
    suspect = [r for r in recs if not r["announcement_text"] and
               (r["hs_am_wx"] == 1 or r["hs_pm_wx"] == 1 or r["fe_wx"] == 1)]
    print(f"\n  ⚠️ announcement_text 欠損 かつ 気象欠航フラグあり: {len(suspect)}日")
    for r in suspect:
        print(f"    {r['date']}: hs_am_wx={r['hs_am_wx']} hs_reason={r['hs_reason']} "
              f"fe_wx={r['fe_wx']} wave={r['wave_max']}")

    return suspect


def cancellation_rate_analysis(recs):
    print_section("2. 欠航率サマリー")

    # 分析可能レコード（hs_bin1 が記録されているもの）
    valid = [r for r in recs if r["hs_bin1"] is not None]
    n = len(valid)

    # 気象欠航（dock/equipment は除外）
    hs_wx_days = [r for r in valid if r["hs_am_wx"] == 1 or r["hs_pm_wx"] == 1]
    hs_wx_am   = [r for r in valid if r["hs_am_wx"] == 1]
    hs_dock    = [r for r in valid if r["hs_reason"] == "dock" and (r["hs_bin1"]==0 or r["hs_bin2"]==0)]
    fe_wx_days = [r for r in valid if r["fe_wx"] == 1]

    print(f"\n  分析可能日数: {n}日")
    print(f"  高速船 気象欠航: {len(hs_wx_days)}日 ({len(hs_wx_days)/n:.1%})")
    print(f"    うち午前便のみ: {len(hs_wx_am)}日")
    print(f"  高速船 ドック運休: {len(hs_dock)}日 (分析から除外)")
    print(f"  フェリー 気象欠航: {len(fe_wx_days)}日 ({len(fe_wx_days)/n:.1%})")

    # 月別欠航率
    from collections import defaultdict
    monthly = defaultdict(lambda: {"n":0, "hs_wx":0, "fe_wx":0})
    for r in valid:
        m = r["dt"].strftime("%Y-%m")
        monthly[m]["n"]    += 1
        monthly[m]["hs_wx"] += (1 if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1 else 0)
        monthly[m]["fe_wx"] += r["fe_wx"]
    print(f"\n  月別欠航率:")
    for m in sorted(monthly):
        d = monthly[m]
        if d["n"] == 0: continue
        print(f"    {m}: n={d['n']:2d}  高速船気象欠航={d['hs_wx']:2d}({d['hs_wx']/d['n']:.0%})  "
              f"フェリー気象欠航={d['fe_wx']:2d}({d['fe_wx']/d['n']:.0%})")


def weather_distribution(recs):
    print_section("3. 気象条件 × 欠航の分布")

    valid = [r for r in recs if r["wave_max"] is not None and r["hs_bin1"] is not None
             and r["hs_reason"] != "dock" and r["hs_reason"] != "equipment"]

    cancel = [r for r in valid if r["hs_am_wx"] == 1 or r["hs_pm_wx"] == 1]
    normal = [r for r in valid if r["hs_am_wx"] == 0 and r["hs_pm_wx"] == 0]

    def stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals: return "—"
        return f"min={min(vals):.1f}  median={sorted(vals)[len(vals)//2]:.1f}  "  \
               f"max={max(vals):.1f}  mean={sum(vals)/len(vals):.2f}"

    print(f"\n  気象欠航日 n={len(cancel)} / 運航日 n={len(normal)}")
    print(f"\n  【wave_max (m)】")
    print(f"    欠航日: {stats([r['wave_max'] for r in cancel])}")
    print(f"    運航日: {stats([r['wave_max'] for r in normal])}")
    print(f"\n  【wave_am (m)】")
    print(f"    欠航日: {stats([r['wave_am'] for r in cancel])}")
    print(f"    運航日: {stats([r['wave_am'] for r in normal])}")
    print(f"\n  【swell (m)】")
    print(f"    欠航日: {stats([r['swell'] for r in cancel])}")
    print(f"    運航日: {stats([r['swell'] for r in normal])}")
    print(f"\n  【wind_max (m/s)】")
    print(f"    欠航日: {stats([r['wind_max'] for r in cancel])}")
    print(f"    運航日: {stats([r['wind_max'] for r in normal])}")
    print(f"\n  【wind_am (m/s)】")
    print(f"    欠航日: {stats([r['wind_am'] for r in cancel])}")
    print(f"    運航日: {stats([r['wind_am'] for r in normal])}")

    # 波高区間別欠航率
    print(f"\n  【高速船 波高区間別 気象欠航率（wave_max）】")
    buckets = {}
    for r in valid:
        b = int(r["wave_max"] * 2) / 2  # 0.5m刻み
        if b not in buckets:
            buckets[b] = {"n":0, "cancel":0}
        buckets[b]["n"] += 1
        buckets[b]["cancel"] += (1 if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1 else 0)
    for b in sorted(buckets):
        v = buckets[b]
        rate = v["cancel"] / v["n"]
        bar  = "█" * int(rate * 20)
        print(f"    波{b:.1f}m: n={v['n']:2d}  欠航率={rate:.0%}  {bar}")

    # wave_am 区間別欠航率
    print(f"\n  【高速船 波高区間別 気象欠航率（wave_am: 6-12時）】")
    buckets_am = {}
    for r in valid:
        if r["wave_am"] is None: continue
        b = int(r["wave_am"] * 2) / 2
        if b not in buckets_am:
            buckets_am[b] = {"n":0, "cancel":0}
        buckets_am[b]["n"] += 1
        buckets_am[b]["cancel"] += (1 if r["hs_am_wx"]==1 else 0)
    for b in sorted(buckets_am):
        v = buckets_am[b]
        rate = v["cancel"] / v["n"]
        bar  = "█" * int(rate * 20)
        print(f"    AM波{b:.1f}m: n={v['n']:2d}  欠航率={rate:.0%}  {bar}")


def model_calibration(recs):
    print_section("4. 現行モデル キャリブレーション分析")

    valid = [r for r in recs if r["model_hs_pct"] is not None and r["hs_bin1"] is not None
             and r["hs_reason"] not in ("dock", "equipment")]

    print(f"\n  分析可能日数: {len(valid)}日")

    # 10%刻みのキャリブレーション
    print(f"\n  【高速船: 予測% → 実際の気象欠航率】")
    buckets = {i: {"n":0, "cancel":0} for i in range(0, 100, 10)}
    for r in valid:
        b = min(int(r["model_hs_pct"] // 10) * 10, 90)
        buckets[b]["n"] += 1
        buckets[b]["cancel"] += (1 if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1 else 0)
    for b in sorted(buckets):
        v = buckets[b]
        if v["n"] == 0: continue
        rate = v["cancel"] / v["n"]
        diff = rate - b/100
        flag = "⬆过大" if diff < -0.15 else ("⬇過小" if diff > 0.15 else "  ✓")
        print(f"    予測{b:2d}〜{b+9}%: 実={rate:.0%} (n={v['n']:2d})  差={diff:+.0%}  {flag}")

    # Brier score
    pairs = [(r["model_hs_pct"]/100,
              1 if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1 else 0) for r in valid]
    bs = sum((p-a)**2 for p,a in pairs) / len(pairs)
    print(f"\n  Brier Score（高速船）: {bs:.3f}")
    print(f"  参考: 0=完璧, 0.083=10%のランダム欠航率で常に10%と答えた場合, 0.25=常に50%")

    # フェリー
    valid_fe = [r for r in recs if r["model_fe_pct"] is not None and r["fe_op"] is not None
                and r["fe_reason"] not in ("dock","equipment")]
    print(f"\n  【フェリー: 予測% → 実際の気象欠航率】")
    bfe = {i: {"n":0, "cancel":0} for i in range(0, 100, 10)}
    for r in valid_fe:
        b = min(int(r["model_fe_pct"] // 10) * 10, 90)
        bfe[b]["n"] += 1
        bfe[b]["cancel"] += r["fe_wx"]
    for b in sorted(bfe):
        v = bfe[b]
        if v["n"] == 0: continue
        rate = v["cancel"] / v["n"]
        print(f"    予測{b:2d}〜{b+9}%: 実={rate:.0%} (n={v['n']:2d})")


def logistic_regression_analysis(recs):
    print_section("5. ロジスティック回帰による新閾値導出")

    try:
        import numpy as np
        from scipy.optimize import minimize
        from scipy.special import expit  # sigmoid
    except ImportError:
        print("  numpy/scipy が未インストール")
        return

    # ドック・機器除外、wave_max + wind_am + swell が揃っているレコード
    valid = [r for r in recs
             if r["wave_max"] is not None and r["wave_am"] is not None
             and r["wind_max"] is not None and r["hs_bin1"] is not None
             and r["hs_reason"] not in ("dock","equipment")]

    print(f"\n  分析可能日数: {len(valid)}日")

    # 特徴量
    X_max = np.array([[r["wave_max"], r["swell"] or 0, r["wind_max"]] for r in valid])
    X_am  = np.array([[r["wave_am"],  r["swell"] or 0, r["wind_am"] or r["wind_max"] or 0] for r in valid])
    y     = np.array([1 if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1 else 0 for r in valid])

    # スコア式（現行）
    def calc_score(wave, swell, wind):
        return min(wave/5.0,1.0)*0.35 + min(swell/4.0,1.0)*0.30 + min(wind/20.0,1.0)*0.20

    scores_max = np.array([calc_score(r["wave_max"], r["swell"] or 0, r["wind_max"] or 0) for r in valid])
    scores_am  = np.array([calc_score(r["wave_am"],  r["swell"] or 0, r["wind_am"] or r["wind_max"] or 0) for r in valid])

    # 現行パラメータでの対数尤度
    def neg_log_likelihood(params, scores, y):
        inflection, steepness = params
        probs = expit(steepness * (scores - inflection))
        probs = np.clip(probs, 1e-7, 1-1e-7)
        return -np.sum(y * np.log(probs) + (1-y) * np.log(1-probs))

    # 最適化（wave_max版）
    res_max = minimize(neg_log_likelihood, [0.42, 14.0], args=(scores_max, y),
                       method="Nelder-Mead", options={"xatol":0.001,"fatol":0.001})
    # 最適化（wave_am版）
    res_am  = minimize(neg_log_likelihood, [0.42, 14.0], args=(scores_am,  y),
                       method="Nelder-Mead", options={"xatol":0.001,"fatol":0.001})

    print(f"\n  【現行パラメータ（高速船）】")
    print(f"    inflection=0.42, steepness=14.0")
    ll_current = neg_log_likelihood([0.42, 14.0], scores_max, y)
    print(f"    対数尤度（wave_max基準）: {-ll_current:.1f}")

    print(f"\n  【最適化結果: wave_max使用】")
    print(f"    inflection={res_max.x[0]:.3f}, steepness={res_max.x[1]:.2f}")
    print(f"    対数尤度: {-res_max.fun:.1f}  (改善: {ll_current-res_max.fun:.1f})")

    print(f"\n  【最適化結果: wave_am使用（朝の波高）】")
    print(f"    inflection={res_am.x[0]:.3f}, steepness={res_am.x[1]:.2f}")
    print(f"    対数尤度: {-res_am.fun:.1f}  (改善: {ll_current-res_am.fun:.1f})")

    # スコア分布の比較
    print(f"\n  【欠航日 vs 運航日 のスコア分布（wave_max）】")
    sc_cancel = [s for s, label in zip(scores_max, y) if label == 1]
    sc_normal = [s for s, label in zip(scores_max, y) if label == 0]
    if sc_cancel:
        print(f"    欠航日スコア: min={min(sc_cancel):.3f}  median={sorted(sc_cancel)[len(sc_cancel)//2]:.3f}  max={max(sc_cancel):.3f}")
    if sc_normal:
        print(f"    運航日スコア: min={min(sc_normal):.3f}  median={sorted(sc_normal)[len(sc_normal)//2]:.3f}  max={max(sc_normal):.3f}")

    # スコア区間別欠航率
    print(f"\n  【スコア区間別 欠航率（wave_max使用）】")
    score_buckets = {}
    for s, label in zip(scores_max, y):
        b = int(s * 10) / 10
        if b not in score_buckets:
            score_buckets[b] = {"n":0, "cancel":0}
        score_buckets[b]["n"] += 1
        score_buckets[b]["cancel"] += label
    for b in sorted(score_buckets):
        v = score_buckets[b]
        rate = v["cancel"] / v["n"]
        pct_current = 100 / (1 + math.exp(-14.0 * (b + 0.05 - 0.42)))
        pct_new     = 100 / (1 + math.exp(-res_max.x[1] * (b + 0.05 - res_max.x[0])))
        bar = "█" * int(rate * 20)
        print(f"    score{b:.1f}: n={v['n']:2d} 実欠航率={rate:.0%}  "
              f"現モデル={pct_current:.0f}%  最適={pct_new:.0f}%  {bar}")

    return res_max.x, res_am.x


def jma_effectiveness(recs):
    print_section("6. JMA早期注意情報の有効性")

    valid = [r for r in recs if r["hs_bin1"] is not None
             and r["hs_reason"] not in ("dock","equipment")]

    cancel = [r for r in valid if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1]
    normal = [r for r in valid if r["hs_am_wx"]==0 and r["hs_pm_wx"]==0]

    # jma_warn_today の分布
    def warn_dist(recs):
        d = {}
        for r in recs:
            w = r["jma_warn_today"] if r["jma_warn_today"] else "なし"
            d[w] = d.get(w,0) + 1
        return d

    print(f"\n  jma_warning_today の分布:")
    print(f"    欠航日(n={len(cancel)}): {warn_dist(cancel)}")
    print(f"    運航日(n={len(normal)}): {warn_dist(normal)}")

    # 早期注意あり → 欠航率
    warn_days  = [r for r in valid if r["jma_warn_today"] and r["jma_warn_today"] != "なし"]
    if warn_days:
        wx_rate = sum(1 for r in warn_days if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1) / len(warn_days)
        print(f"\n  早期注意あり日: {len(warn_days)}日 → 高速船気象欠航率 {wx_rate:.0%}")
    no_warn = [r for r in valid if not r["jma_warn_today"] or r["jma_warn_today"]=="なし"]
    if no_warn:
        wx_rate = sum(1 for r in no_warn if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1) / len(no_warn)
        print(f"  早期注意なし日: {len(no_warn)}日 → 高速船気象欠航率 {wx_rate:.0%}")

    # 見逃し（早期注意なしで欠航した日）
    missed_with_no_warn = [r for r in cancel
                           if not r["jma_warn_today"] or r["jma_warn_today"] == "なし"]
    print(f"\n  早期注意なしで気象欠航した日: {len(missed_with_no_warn)}日")
    for r in missed_with_no_warn[:8]:
        print(f"    {r['date']}: wave={r['wave_max']} swell={r['swell']} wind={r['wind_max']} "
              f"announce='{r['announcement_text'][:60]}'")


def wave_am_vs_max(recs):
    print_section("7. wave_am（朝）vs wave_max（日次最大値）の予測力比較")

    valid = [r for r in recs
             if r["wave_am"] is not None and r["wave_max"] is not None
             and r["hs_bin1"] is not None
             and r["hs_reason"] not in ("dock","equipment")]

    cancel = [r for r in valid if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1]
    normal = [r for r in valid if r["hs_am_wx"]==0 and r["hs_pm_wx"]==0]

    def mean(lst):
        return sum(lst)/len(lst) if lst else 0

    print(f"\n  欠航日 (n={len(cancel)}):")
    print(f"    wave_am  平均: {mean([r['wave_am'] for r in cancel]):.2f}m")
    print(f"    wave_max 平均: {mean([r['wave_max'] for r in cancel]):.2f}m")
    print(f"    乖離（max-am）平均: {mean([r['wave_max']-r['wave_am'] for r in cancel]):.2f}m")

    print(f"\n  運航日 (n={len(normal)}):")
    print(f"    wave_am  平均: {mean([r['wave_am'] for r in normal]):.2f}m")
    print(f"    wave_max 平均: {mean([r['wave_max'] for r in normal]):.2f}m")
    print(f"    乖離（max-am）平均: {mean([r['wave_max']-r['wave_am'] for r in normal]):.2f}m")

    # 大きく乖離しているケース（wave_maxが高いが実際は朝は低い → 空振りの原因）
    high_diff = [(r["wave_max"] - r["wave_am"], r) for r in normal if r["wave_max"] - r["wave_am"] > 1.5]
    high_diff.sort(reverse=True)
    print(f"\n  空振りの原因候補（運航日で wave_max - wave_am > 1.5m）: {len(high_diff)}日")
    for diff, r in high_diff[:5]:
        print(f"    {r['date']}: am={r['wave_am']:.1f}m → max={r['wave_max']:.1f}m  (+{diff:.1f}m)  "
              f"model={r['model_hs_pct']}%")


def proposal(recs, opt_params_max=None, opt_params_am=None):
    print_section("8. 総合提案")

    valid = [r for r in recs if r["hs_bin1"] is not None
             and r["hs_reason"] not in ("dock","equipment")
             and r["wave_max"] is not None]
    cancel_rate = sum(1 for r in valid if r["hs_am_wx"]==1 or r["hs_pm_wx"]==1) / len(valid)

    print(f"""
  【現状の問題点まとめ】

  1. スコア計算に「日次最大値（wave_max）」を使っているが、
     フェリーの出発判断は「朝の波高（wave_am）」で決まる。
     → 午後に波が高くなるケースで空振りが発生。

  2. 現行シグモイドの inflection=0.42 は実績より低すぎる可能性あり。
     → 最適化の結果次第で引き上げが必要。

  3. JMA早期注意情報が「なし」でも欠航するケースがある（台風急接近等）。
     → 気象庁警報よりも早く動く Open-Meteo モデルの限界。

  4. 台風・急発達低気圧の際は前日の波高予報が低く出る構造的問題がある。

  【推奨修正（優先度順）】

  優先度①: wave_am（6〜12時帯の波高）を予測の主指標に切り替え
    - 現状: analysis["all_day"]["max_wave"] を使用
    - 変更: analysis["morning"]["max_wave"] をメインに、
           all_dayは補助（max(morning, all_day*0.7) などで合成）

  優先度②: シグモイドパラメータの最適化（データ導出値に更新）""")

    if opt_params_max is not None:
        print(f"    最適化結果（wave_max使用）:")
        print(f"      inflection: 0.42 → {opt_params_max[0]:.3f}")
        print(f"      steepness:  14.0 → {opt_params_max[1]:.2f}")
    if opt_params_am is not None:
        print(f"    最適化結果（wave_am使用）:")
        print(f"      inflection: 0.42 → {opt_params_am[0]:.3f}")
        print(f"      steepness:  14.0 → {opt_params_am[1]:.2f}")

    print(f"""
  優先度③: JMA早期注意情報を「スコアの下限（フロア）」として活用
    - 現状: 注意報を scoring weight 0.15 として加算
    - 変更:
      "高" → hs_pct = max(hs_pct, 70)
      "中" → hs_pct = max(hs_pct, 40)
    ※ 台風フロアと同じ方式。注意報が出ている日は最低限 40-70%を保証。

  優先度④: announcement_text スクレイプ失敗時の cancel_reason 誤判定を修正
    - 現状: announcement_text が空の場合、欠航理由が "none" になり
            hs_am_weather_cancel=0 になるべきところが誤判定されることがある
    - 変更: announcement_text が空かつ hs_bin1=0 の場合は reason="unknown" とし、
            分析から除外する（weather キャリブレーションを汚染しない）

  優先度⑤: フェリーの inflection 点を DESIGN.md の 0.58 に戻す（別途実施）
    - 現状: 0.52（コードのバグ）
    - 変更: 0.58（設計値）

  【データ蓄積が増えたら実施すべきこと】
  - sklearn の LogisticRegression で wave_am, swell, wind_am を特徴量とした
    本格的なMLモデルへの移行（目安: 150〜200件の気象欠航サンプル）
  - 現在の気象欠航サンプル数: {sum(1 for r in valid if r['hs_am_wx']==1 or r['hs_pm_wx']==1)}件
    （150件目安まで: {max(0, 150-sum(1 for r in valid if r['hs_am_wx']==1 or r['hs_pm_wx']==1))}件不足）
""")


# ============================================================
# メイン
# ============================================================

if __name__ == "__main__":
    print(f"座間味 欠航予測 精度分析（フル）")
    print(f"実行: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")

    gc = connect_sheets()
    sheets_id = os.environ.get("GOOGLE_SHEETS_ID")
    if not sheets_id:
        print("[ERROR] GOOGLE_SHEETS_ID 未設定")
        exit(1)

    # 2024-12-01 以降
    since = datetime(2024, 12, 1).date()
    recs = load_operation_log(gc, sheets_id, since)
    print(f"\n  {since} 以降 {len(recs)} レコードを取得")

    # 各分析の実行
    data_quality_check(recs)
    cancellation_rate_analysis(recs)
    weather_distribution(recs)
    model_calibration(recs)
    opt = logistic_regression_analysis(recs)
    opt_max = opt[0] if opt else None
    opt_am  = opt[1] if opt else None
    jma_effectiveness(recs)
    wave_am_vs_max(recs)
    proposal(recs, opt_max, opt_am)
