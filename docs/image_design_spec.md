# Ferry Cancellation Risk Instagram Design Spec for Claude

## Purpose
Create square Instagram images for ferry cancellation-risk forecasts using fixed templates and structured forecast data.

The image generator should not redesign the layout each time. It should only update variable text, numbers, colors, bars, and suspended-state elements based on the input JSON.

Canvas size: `1080 x 1080 px`  
Format: Instagram square post  
Tone: clean, modern, ocean-resort, easy to understand at a glance  
Language: Japanese primary + English secondary

---

# 1. Common Design Rules

## Brand / Visual Tone
- Background: ocean photo or realistic ocean texture, preferably blue sea + sky.
- Overlay: slight dark-blue gradient to improve text readability.
- Cards: white or off-white rounded rectangles with subtle shadow.
- Main text: bold, clean sans-serif.
- Japanese and English should be shown together where useful.
- Avoid decorative icons unless necessary.
- Do not use HOMESTAY KUCHA logo.

## Route Map
Use a fixed island silhouette map for each route.
For Zamami/Aka, use the provided reference shape based on the uploaded island map.
- No island name text on the map itself.
- Pale green land, transparent/blue sea.
- The map is a visual identifier for the route, not a detailed navigation map.

## Fonts（確定）
| 用途 | フォント | 実装 |
|---|---|---|
| 数字・% | **Manrope Bold** | `assets/fonts/Manrope-var.ttf`（可変, "Bold"）|
| 英語UI | **Inter Medium** | `assets/fonts/Inter-var.ttf`（可変, "Medium"）|
| 日本語UI | **Noto Sans JP Medium** | 本番ランナーの Noto Sans CJK Medium を使用 |

- いずれも OFL ライセンス（無料）。ライセンス文は `assets/fonts/OFL-*.txt` に同梱。
- 日英混在ラベル（例「高速船 High-speed boat」「フェリー Ferry」）は1描画のため Noto で統一。
- 数字（大見出し%・フェリー%）のみ Manrope、純英語（TOMORROW / RISK / AM・PM / Suspended）は Inter。

## Risk Color Scale
Use the following fixed color rules.

| Risk % | Label JA | Label EN | Color |
|---:|---|---|---|
| 0–10% | 低い | LOW | Green |
| 10–30% | やや低い | LOW-MID | Light green |
| 30–50% | やや高い | MID | Yellow |
| 50–80% | 高い | HIGH | Orange |
| 80–100% | 非常に高い | VERY HIGH | Red |

Important:
- Color should follow the displayed risk percentage.
- Do not color by date.
- If a vessel is officially suspended, use grey diagonal hatch + official notice style instead of a risk color.

## Official Suspension Visual Rule
When a vessel is suspended by official notice:
- Show only the affected vessel area as suspended.
- Do not cover the entire day card unless all vessels are suspended.
- Use grey/white diagonal hatching.
- Add dashed border.
- Add badge: `公式発表 Official Notice`
- Large text: `運休`
- English: `Suspended`
- Vessel label remains bilingual, e.g. `フェリー Ferry` or `高速船 High-speed boat`.

---

# 2. Short-term Forecast Template

## Use Case
Forecast for tomorrow and the day after tomorrow.

## Fixed Layout
- Left side:
  - `FERRY CANCELLATION RISK`
  - `フェリー欠航予測`
  - `AIによる欠航リスク予測`
  - Route name, e.g. `座間味・阿嘉`
  - English route name, e.g. `ZAMAMI / AKA`
  - Route map
  - Route label:
    - `予測対象航路`
    - `座間味・阿嘉 ⇔ 那覇`
    - `Route: Zamami / Aka ⇔ Naha`

- Center/right:
  - Two large vertical cards:
    - Tomorrow
    - Day After

- Bottom:
  - Risk level guide
  - Disclaimer:
    - `※AI予測・参考値。運休は公式発表に基づきます。`
    - `*AI estimates for weather risk. Suspension notice based on official announcement.`

## Variable Fields
For each day card:
- Date label JA: e.g. `明日 6/12`, `明後日 6/13`
- Date label EN: `TOMORROW`, `DAY AFTER`
- Large display risk percentage
- Risk label JA/EN
- High-speed boat section:
  - AM risk
  - PM risk
  - suspended state
- Ferry section:
  - ferry risk
  - suspended state

## Large Display Risk Rule
The large percentage at the top of each daily card should be:

> The maximum cancellation risk among vessels that are operating that day.

Rules:
1. If both High-speed boat and Ferry are operating:
   - display the max of all available risks.
   - High-speed boat max = max(AM, PM).
   - Ferry risk = ferry risk.
2. If High-speed boat is suspended and Ferry is operating:
   - display Ferry risk.
3. If Ferry is suspended and High-speed boat is operating:
   - display max(High-speed boat AM, PM).
4. If both are suspended:
   - replace the large percentage area with `運休 / Suspended`.

The calculation should ideally be done before image generation and passed as `display_risk`.

## Short-term Input JSON Schema

```json
{
  "template": "short_term",
  "route_id": "zamami_aka",
  "route_label_ja": "座間味・阿嘉",
  "route_label_en": "ZAMAMI / AKA",
  "route_line_ja": "座間味・阿嘉 ⇔ 那覇",
  "route_line_en": "Zamami / Aka ⇔ Naha",
  "days": [
    {
      "date": "2026-06-12",
      "date_label_ja": "明日 6/12",
      "date_label_en": "TOMORROW",
      "display_risk": 1,
      "risk_label_ja": "低い",
      "risk_label_en": "LOW RISK",
      "high_speed_boat": {
        "suspended": false,
        "am_risk": 1,
        "pm_risk": 1
      },
      "ferry": {
        "suspended": false,
        "risk": 1
      }
    },
    {
      "date": "2026-06-13",
      "date_label_ja": "明後日 6/13",
      "date_label_en": "DAY AFTER",
      "display_risk": 5,
      "risk_label_ja": "低い",
      "risk_label_en": "LOW RISK",
      "high_speed_boat": {
        "suspended": false,
        "am_risk": 3,
        "pm_risk": 5
      },
      "ferry": {
        "suspended": true,
        "risk": null,
        "official_notice_text": "Official Notice"
      }
    }
  ]
}
```

---

# 3. Long-term Forecast Template

## Use Case
Forecast for 3–7 days ahead.

## Fixed Layout
- Header:
  - `フェリー欠航可能性 長期予報（3〜7日先）`
  - `Ferry Cancellation Risk / Long-term Forecast (3–7 days ahead)`
  - Route line: `座間味・阿嘉 ⇔ 那覇    Zamami / Aka ⇔ Naha`

- Left top:
  - Route map using fixed island silhouette.
  - No island-name text on map.

- Main summary card:
  - `欠航リスク期間 / Risk Period`
  - Date range, e.g. `6/7 〜 6/13`
  - English date range, e.g. `Jun 7 – Jun 13`
  - High-speed boat max risk
  - Ferry max risk

Important:
- Do not add `頃` after the risk period.

- Middle chart cards:
  - Left: High-speed boat daily bars
  - Right: Ferry daily bars

- Bottom:
  - Risk level guide
  - Disclaimer:
    - `※AI予測・参考値。公式情報は各航路の運航会社HPをご確認ください。`
    - `*AI-based estimate. Check official website for the latest information.`

## Variable Fields
- Risk period start/end
- High-speed boat max risk
- Ferry max risk
- Daily risk bars for each vessel
- Suspended state for each vessel/day

## Bar Display Rule
For each day and vessel:
- If operating:
  - show horizontal bar colored according to risk level.
  - show percentage at right.
- If officially suspended:
  - replace that day’s bar with grey diagonal hatch.
  - show `運休 / Suspended` at right.
  - do not show percentage.

## Max Risk Rule
For each vessel summary:
- Max risk should be calculated from only operating days.
- Suspended days should not be treated as 100% risk.
- If all days are suspended for that vessel:
  - replace the vessel summary area with a suspended panel:
    - `公式発表 Official Notice`
    - `運休`
    - `Suspended`

## Long-term Input JSON Schema

```json
{
  "template": "long_term",
  "route_id": "zamami_aka",
  "route_label_ja": "座間味・阿嘉",
  "route_label_en": "ZAMAMI / AKA",
  "route_line_ja": "座間味・阿嘉 ⇔ 那覇",
  "route_line_en": "Zamami / Aka ⇔ Naha",
  "risk_period": {
    "start": "2026-06-07",
    "end": "2026-06-13",
    "label_ja": "6/7 〜 6/13",
    "label_en": "Jun 7 – Jun 13"
  },
  "summary": {
    "high_speed_boat": {
      "max_risk": 70,
      "all_suspended": false
    },
    "ferry": {
      "max_risk": 45,
      "all_suspended": false
    }
  },
  "daily": {
    "high_speed_boat": [
      { "date": "2026-06-07", "label": "6/7(土)", "weekday_en": "Sat", "risk": 70, "suspended": false },
      { "date": "2026-06-08", "label": "6/8(日)", "weekday_en": "Sun", "risk": 35, "suspended": false },
      { "date": "2026-06-09", "label": "6/9(月)", "weekday_en": "Mon", "risk": 20, "suspended": false },
      { "date": "2026-06-10", "label": "6/10(火)", "weekday_en": "Tue", "risk": null, "suspended": true },
      { "date": "2026-06-11", "label": "6/11(水)", "weekday_en": "Wed", "risk": 15, "suspended": false },
      { "date": "2026-06-12", "label": "6/12(木)", "weekday_en": "Thu", "risk": 5, "suspended": false },
      { "date": "2026-06-13", "label": "6/13(金)", "weekday_en": "Fri", "risk": 5, "suspended": false }
    ],
    "ferry": [
      { "date": "2026-06-07", "label": "6/7(土)", "weekday_en": "Sat", "risk": 45, "suspended": false },
      { "date": "2026-06-08", "label": "6/8(日)", "weekday_en": "Sun", "risk": 25, "suspended": false },
      { "date": "2026-06-09", "label": "6/9(月)", "weekday_en": "Mon", "risk": 20, "suspended": false },
      { "date": "2026-06-10", "label": "6/10(火)", "weekday_en": "Tue", "risk": null, "suspended": true },
      { "date": "2026-06-11", "label": "6/11(水)", "weekday_en": "Wed", "risk": 10, "suspended": false },
      { "date": "2026-06-12", "label": "6/12(木)", "weekday_en": "Thu", "risk": 5, "suspended": false },
      { "date": "2026-06-13", "label": "6/13(金)", "weekday_en": "Fri", "risk": 5, "suspended": false }
    ]
  }
}
```

---

# 4. Route Variants

Use the same template structure for each route. Only route labels, route line, and map silhouette change.

## Zamami / Aka
```json
{
  "route_id": "zamami_aka",
  "route_label_ja": "座間味・阿嘉",
  "route_label_en": "ZAMAMI / AKA",
  "route_line_ja": "座間味・阿嘉 ⇔ 那覇",
  "route_line_en": "Zamami / Aka ⇔ Naha"
}
```

## Tokashiki
```json
{
  "route_id": "tokashiki",
  "route_label_ja": "渡嘉敷",
  "route_label_en": "TOKASHIKI",
  "route_line_ja": "渡嘉敷 ⇔ 那覇",
  "route_line_en": "Tokashiki ⇔ Naha"
}
```

## Yaeyama
```json
{
  "route_id": "yaeyama",
  "route_label_ja": "八重山",
  "route_label_en": "YAEYAMA",
  "route_line_ja": "石垣発着 八重山航路",
  "route_line_en": "Yaeyama routes from Ishigaki"
}
```

For Yaeyama, vessel/route labels may need route-by-route rows instead of only High-speed boat and Ferry. Keep the same visual tone, but structure can be adjusted for multiple routes such as Ohara, Taketomi, Uehara, Hateruma, Hatoma.

---

# 5. Image Generation Prompt Template

Use this instruction when asking Claude or another AI to generate/update the visual.

```md
You are updating an Instagram square ferry cancellation-risk forecast image.
Use the attached template/design spec exactly.
Do not redesign the layout.
Only update variable fields based on the provided JSON.

Requirements:
- Canvas: 1080x1080 px.
- Japanese primary, English secondary.
- Keep the same clean ocean-photo background, white rounded cards, and risk color scale.
- Use the route map silhouette for the specified route.
- Do not include HOMESTAY KUCHA logo.
- If a vessel is suspended, apply the suspended style only to that vessel area/day row.
- Risk period must not include 「頃」.
- Ensure all numbers are readable on Instagram.

Input JSON:
[PASTE JSON HERE]
```

---

# 6. Recommended Workflow

1. Weather/ferry prediction AI calculates probabilities.
2. Prediction AI outputs JSON using the schema above.
3. Validation step checks:
   - Percentages are 0–100.
   - Suspended rows have `risk: null`.
   - `display_risk` follows the rule.
   - Risk labels match color scale.
4. Image generation/rendering system updates the template.
5. Final image is posted to Instagram.

Best production method:
- Prefer template rendering with Canva API, HTML/CSS, or Python/PIL.
- Avoid generating the full design from scratch every day with an image-generation model, because text and layout may drift.

---

# 7. 既存システムとの接続（実装マッピング）

> この仕様書は**設計のSingle Source of Truth**。画像生成コードを変更する際は必ず本書に従い、
> レイアウトを再設計せず「可変フィールドのみ」を更新すること。

## 7-1. 実装方式（採用）
本事業は **Python/PIL によるテンプレ合成**で実装する（Canva API / 画像生成AIは使わない）。
- テンプレ画像（背景・海・島マップ・タイトル・リスクガイド・免責）は**固定アセット**として読み込み、
  **2カード（明日・明後日）等の可変部のみコードで上書き描画**する。
- 「毎日フルでデザインを作り直さない」を徹底し、フォーマットのドリフトを防ぐ。

## 7-2. ファイル対応表

| 役割 | ファイル |
|---|---|
| 設計仕様（本書） | `docs/image_design_spec.md` |
| 短期テンプレ | `assets/templates/Format_Zamami.png`（座間味・1254²） |
| 短期テンプレ・運休例 | `assets/templates/Format_Zamami_Suspended.png` |
| 長期 参考デザイン（縦長） | `assets/templates/Format_zamami_longterm.png` |
| フォント | `assets/fonts/`（Manrope, Inter + OFLライセンス） |
| JSONサンプル | `docs/samples/short_term_forecast_sample.json` / `long_term_forecast_sample.json` |
| 短期レンダラ | `forecast_publisher.py` → `make_image_short()` ✅テンプレ合成で実装済 |
| 長期レンダラ | `forecast_publisher.py` → `make_image_longterm()` ✅新デザインをPILで正方形1254²再構築（参考テンプレは縦長のため流用せず同デザインを実装） |
| リスク5段階・色 | `forecast_publisher.py` → `_risk_band()` ✅本書の色スケールと一致 |
| 運休枠（点線＋公式発表） | `forecast_publisher.py` → `_draw_suspended_box()` ✅本書準拠 |
| display_risk 計算 | `forecast_publisher.py` → `effective_max_pct()` ✅本書「運航中船種の最大」と一致 |

## 7-3. JSONスキーマ ↔ 内部 forecast dict のフィールド対応

本番は JSON を経由せず内部の `forecast` dict を直接レンダリングする（中間層を増やさず負荷最小化）。
本書のJSONは**可変フィールドの仕様書**として機能し、内部dictと以下で対応する。

| 仕様JSON（short_term） | 内部 forecast dict | 備考 |
|---|---|---|
| `days[].date_label_ja` | `short[i]['label_ja']` + `['date_label']` | 例「明日 6/12」 |
| `days[].date_label_en` | `short[i]['label_en'].upper()` | TOMORROW/DAY AFTER |
| `days[].display_risk` | `effective_max_pct(short[i])` | 運航中船種の最大% |
| `days[].risk_label_ja/en` | `_risk_band(display_risk)` | 色も同時決定 |
| `high_speed_boat.am_risk/pm_risk` | `['highspeed_am_pct']` / `['highspeed_pm_pct']` | |
| `high_speed_boat.suspended` | `['suspended_highspeed']` | |
| `ferry.risk` | `['ferry_pct']` | |
| `ferry.suspended` | `['suspended_ferry']` | |

## 7-4. 運用上の決定事項

1. **キャンバス＝正方形 1254²で統一（確定）**。カルーセル3枚（①短期/②長期/③気象データ）すべて1254²。
   - 短期＝テンプレ native 1254²。長期＝PILで1254²直接描画。気象データ＝1080描画→保存時に1254²へ拡大。
2. **長期は正方形にPIL再構築済み**（参考テンプレ `Format_zamami_longterm.png` は縦長4:5のため画像合成には使わず、同じデザイン言語を1254²で実装）。
3. **フォント（確定）**：数字%=Manrope Bold / 英語=Inter Medium / 日本語=Noto Sans JP（CJK）Medium。§1 Fonts 参照。
4. **長期の島マップは未実装**（縦長テンプレの写真マップが正方形に流用できないため、海グラデ背景に集約）。
   正方形用の島マップ素材（透過PNG等）が用意できれば追加可能。
5. **渡嘉敷・八重山**は同じテンプレ構造を流用予定（route_label / route_line / 島マップのみ差替）。
   八重山は航路別の行構成に拡張（本書 §4 参照）。※現状は座間味のみ新デザイン適用。

## 7-5. 検証（軽量バリデーション）
レンダリング前に最低限：
- 全%が 0〜100。
- 運休船種は risk を表示せず運休枠にする。
- `display_risk` は §2「Large Display Risk Rule」に従う。
- リスクラベルの色は §1 の色スケールに一致。
