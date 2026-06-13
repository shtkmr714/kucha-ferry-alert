"""座間味 5月下旬の生レコードを確認（5/31台風欠航が記録されているか）。"""
import os, json
def connect(sid):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds).open_by_key(sid)

sh = connect(os.environ["GOOGLE_SHEETS_ID"])
rows = sh.worksheet("daily_operation_log").get_all_records()
print("=== 座間味 5/20〜6/3 生レコード ===")
for r in rows:
    d = str(r.get("date","")).strip()
    if "2026-05-2" in d or "2026-05-3" in d or "2026-06-0" in d:
        print(f"\n{d}:")
        print(f"  HS bin1/2/3 = {r.get('hs_bin1_operated')}/{r.get('hs_bin2_operated')}/{r.get('hs_bin3_operated')}  reason={r.get('hs_cancel_reason')}")
        print(f"  FERRY operated = {r.get('ferry_operated')}  reason={r.get('ferry_cancel_reason')}  turnaround={r.get('ferry_turnaround')}")
        print(f"  hs_am_wx={r.get('hs_am_weather_cancel')} hs_pm_wx={r.get('hs_pm_weather_cancel')} fe_wx={r.get('ferry_weather_cancel')}")
        ann = str(r.get('announcement_text',''))[:120]
        print(f"  announce='{ann}'")
        print(f"  typhoon_active={r.get('typhoon_active')} dist={r.get('typhoon_distance_km')}")
