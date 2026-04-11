# Architecture & Implementation Notes / 実装ドキュメント

This document describes the internal architecture, design decisions, and implementation details of **pv-sim-fip** (Solar PV + Battery FIP Transition Simulator).

本ドキュメントは pv-sim-fip の内部アーキテクチャ・設計判断・実装詳細を解説します。

---

## 1. Overview / 概要

pv-sim-fip is a standalone Gradio web application that evaluates the business feasibility of grid-scale solar PV + battery storage systems under Japan's **FIP (Feed-in Premium)** scheme. It supports two distinct business cases within a single UI:

- **Case A — New build**: New PV + battery installed under FIP from day one. Both PV and battery are CAPEX components.
- **Case B — Existing FIT → FIP transition**: Battery added to an existing FIT plant. Only the battery is CAPEX (PV is sunk cost), and the FIP premium grant period equals the FIT remainder.

The application is forked from the industrial self-consumption sister project (`pv-sim-biz`) but **strips out all demand-side and tariff logic**, replacing them with JEPX spot-market revenue, FIP premium economics, and POI-based curtailment.

pv-sim-fip は日本の FIP（Feed-in Premium）制度における太陽光＋蓄電池事業の事業性を評価する Gradio ウェブアプリです。1 つの UI 内でケースA（新規 FIP 新設）とケースB（既存 FIT → FIP 転）の 2 つを切替可能です。姉妹プロジェクト `pv-sim-biz` からフォークしましたが、需要側・電気料金体系を全て削除し、JEPX 連動売電・FIP プレミアム・POI ベース出力制御モデルに置き換えています。

> **Why FIP transition matters now**: From FY2026 onward, Japan's curtailment priority is expected to be reversed — FIT plants will be curtailed **before** FIP plants. For existing FIT owners, FIP transition is becoming a strategic asset-defense decision, not just an income-optimization exercise. / 2026 年度から出力制御の優先順位が FIT ＞ FIP に変更される見通しで、既存 FIT オーナーにとって FIP 転は資産防衛の戦略的判断となりつつあります。

---

## 2. Tech Stack / 技術スタック

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| UI Framework | Gradio 5.23 |
| Plotting | Plotly |
| Solar Modeling | pvlib (Erbs decomposition, isotropic transposition, `bifacial.infinite_sheds`) |
| Optimization | PuLP + CBC (linear programming) |
| Data Storage | SQLite (`radiation.db` NEDO METPV-20, `jepx.db` JEPX spot prices) |
| Numerical | pandas, numpy |
| Deployment | Hugging Face Spaces |

---

## 3. File Structure / ファイル構成

```
pv-sim-fip/
├── app.py              # Standalone main application (~2,800 LOC)
├── radiation.db        # NEDO METPV-20 weather DB (77 sites, 10 elements, Git LFS)
├── jepx.db             # JEPX spot price DB (9 areas × 6 fiscal years, Git LFS)
├── requirements.txt
├── README.md           # HF Spaces metadata + project description
├── LICENSE             # MIT
└── docs/
    └── architecture.md # This file
```

The application is designed as a **standalone single-file** (`app.py`), with weather and JEPX price databases as the only required external dependencies. This simplifies deployment and version management.

---

## 4. Core Generation Model (JIS C 8907) / 発電量モデル

The application implements the **JIS C 8907:2005** standard for PV power generation estimation in Japan. The generation pipeline is reused from the sister project `pv-sim-biz`.

### 4.1 Time Resolution

- **Granularity**: 30 minutes × 365 days = **17,520 timeslots per year**
- **Per-array support**: Up to 8 PV arrays with independent tilt, azimuth, and PCS output limits
- **Per-array PCS clipping**: Each array is clipped to its PCS rated output before aggregation

### 4.2 Irradiance Conversion

GHI (global horizontal irradiance) from the NEDO METPV-20 database is converted to POA (plane-of-array) irradiance via pvlib:

1. **DNI/DHI decomposition**: Erbs model (`pvlib.irradiance.erbs`)
2. **Tilted-plane transposition**: Isotropic sky model (`pvlib.irradiance.get_total_irradiance`)
3. **Bifacial gain (optional)**: `pvlib.bifacial.infinite_sheds.get_irradiance` for two-sided panels

### 4.3 Temperature Correction

JIS C 8907 temperature correction is applied per timeslot using hourly ambient temperature from the database:

```
P_corrected = P_nominal × (1 + α × (T_cell − 25))
```

Where `α` is the temperature coefficient (default −0.35 %/°C) and `T_cell = T_ambient + ΔT` (default ΔT = 21.5°C for ground-mounted utility PV).

### 4.4 Bifacial Panel Support

When bifacial mode is enabled, the rear-side irradiance is computed via `pvlib.bifacial.infinite_sheds`. Snow depth data from NEDO METPV-20 (element #9) drives **dynamic albedo switching**:

- Snow depth ≥ threshold → albedo = 0.7 (snow)
- Otherwise → albedo = 0.2 (default ground)

Default parameters: `bifaciality=0.75`, `GCR=0.4`, ground height=2.0 m, pitch=5.0 m.

---

## 5. JEPX Spot-Price Database / JEPX スポット価格データベース

### 5.1 Schema

```sql
CREATE TABLE jepx_prices (
    area TEXT,           -- 9 areas: 北海道/東北/東京/中部/北陸/関西/中国/四国/九州
    fiscal_year INTEGER, -- 2020..2025 (Japanese fiscal year, April–March)
    date TEXT,           -- MM-DD (year-independent, 365 days)
    slot INTEGER,        -- 1..48 (30-minute slot number)
    price REAL           -- spot price [¥/kWh]
);
CREATE INDEX idx_jepx ON jepx_prices(area, fiscal_year, date, slot);
```

### 5.2 Coverage

- **9 areas** × **6 fiscal years (FY2020–FY2025)** × **17,520 half-hour slots** = **945,720 records**
- Okinawa is excluded because JEPX has no spot trading there.
- Leap-day handling: FY2023 (which contains 2024-02-29) has the 48 leap-day slots removed → **365 days exactly** for every fiscal year.

### 5.3 Multi-year Averaging

Users select one or more fiscal years in the UI; for each timeslot the prices are averaged across the selected years. The default selection excludes FY2022 (Ukraine-crisis spike) but the data is still stored so users can include it.

### 5.4 Source CSV Specification

Source files are JEPX spot-market trading-result CSVs (`spot_summary_YYYY.csv`, CP932 / Shift_JIS, 19 columns). Columns 6–14 are the per-area prices. Conversion to `jepx.db` is one-shot via `build_jepx_db.py` (excluded from the deployed image).

---

## 6. FIP Revenue Model / FIP 収益モデル

```
FIP revenue = Σ_t [ export(t) × ( JEPX_price(t) + premium + nonfossil − BG_fee ) ]
```

| Component | Default | Unit | Notes |
|---|---|---|---|
| FIP premium / 基準価格 | 9.6 | ¥/kWh | (基準価格 − 参照価格); user-editable |
| Non-fossil certificate / 非化石証書 | 0.6 | ¥/kWh | |
| Balancing-group fee / BG 手数料 | 2.0 | ¥/kWh | Simplified imbalance-risk model |

### 6.1 Two-phase Revenue Switching

The application automatically switches between two phases inside the cashflow builder:

- **Phase 1 — Premium period**: years 1..`fip_premium_years` (default 20 for Case A, FIT remainder for Case B). Revenue = JEPX + premium + non-fossil − BG fee.
- **Phase 2 — Post-premium**: years `fip_premium_years+1`..battery end-of-life. Revenue = JEPX + non-fossil − BG fee (premium dropped).

The LP is solved separately for each phase so that the optimal dispatch reflects the actual revenue structure of that phase.

### 6.2 Why fixed off-take mode is excluded

Earlier design exploration considered an "aggregator fixed-price" mode in addition to JEPX trading. That mode was dropped: under fixed off-take, battery arbitrage value collapses to zero, and curtailment-avoidance alone cannot recover battery CAPEX (~178-year payback in test simulations). The simulator therefore focuses exclusively on the **battery + JEPX optimal dispatch** business case, computing **with/without-battery** comparisons in the background to expose the incremental value of storage.

---

## 7. Curtailment Model / 出力制御モデル

A flat year-round curtailment rate is unrealistic — curtailment in Japan is heavily concentrated in spring/autumn midday hours. The application uses a **monthly × hourly probability profile** scaled to the user-specified annual curtailment rate.

### 7.1 Default Profile (pre-normalization weights)

```
         06-08  08-10  10-12  12-14  14-16  16-18
1月        0      0      0      0      0      0
2月        0      0      1      2      1      0
3月        0      1      3      5      3      1
4月        0      2      5      8      5      2
5月        0      2      5      8      5      2
6月        0      0      1      2      1      0
7月        0      0      0      0      0      0
8月        0      0      0      0      0      0
9月        0      0      1      2      1      0
10月       0      1      3      5      3      1
11月       0      1      3      5      3      1
12月       0      0      0      0      0      0
```

The weights are normalized so that the year-total `gen × curtail_prob` matches the user-input annual curtailment rate.

### 7.2 POI-based Curtailment Cap

The LP applies a **point-of-interconnection (POI) total-export cap** rather than discounting PV generation:

```
export[t] ≤ gen[t] × (1 − curtail_prob[t])
```

This is the **physically correct** representation: the curtailment limit applies to total exported energy at the grid connection point, regardless of whether the energy comes from PV directly or from battery discharge. Therefore the only way the battery can avoid curtailment is the **store-and-shift** path: charge during curtailed slots, discharge during non-curtailed slots. Same-slot phantom discharge cannot inflate the export cap.

---

## 8. Battery Dispatch LP / 蓄電池 LP 最適化

The LP **maximizes** annual JEPX revenue over all 17,520 timeslots:

### 8.1 Decision Variables (per slot t = 0..17519)

- `charge[t] ∈ [0, max_charge_kw × 0.5]`
- `discharge[t] ∈ [0, max_discharge_kw × 0.5]`
- `export[t] ≥ 0`
- `curtailment[t] ≥ 0`
- `soc[t] ∈ [SOC_min, SOC_max]`

### 8.2 Objective

```
maximize Σ_t [ export[t] × ( JEPX[t] + premium + nonfossil − BG_fee ) ]
```

### 8.3 Constraints

- **Energy balance** (no demand): `gen[t] + discharge[t] == export[t] + charge[t] + curtailment[t]`
- **POI export cap**: `export[t] ≤ gen[t] × (1 − curtail_prob[t])` when `curtail_prob[t] > 0`
- **PCS mutual exclusion** (soft): `charge[t] + discharge[t] ≤ max(max_charge_per_slot, max_discharge_per_slot)`
- **SOC transition**: `soc[t] = soc[t−1] + charge[t] × η_ch − discharge[t] / η_dc`
- **Terminal SOC**: `soc[T−1] == soc_min` (year-end SOC returns to initial)

### 8.4 PCS Mutual Exclusion — Why It Matters

Without the mutual-exclusion constraint, the LP can exploit a **virtual pass-through pipeline** in expensive JEPX slots: PV → charge → simultaneous discharge → export, paying only the round-trip efficiency loss (≈ 9.75 % at 95 % × 95 %) but bypassing the SOC capacity constraint entirely. In one bug-reproduction case a 7.34 kWh battery showed 511,726 kWh/year of charge throughput (≈ 191 cycles/day) instead of the physically realistic ≤ 2 cycles/day.

The fix added to both LPs is:

```python
prob += charge[t] + discharge[t] <= max_power_per_slot
```

where `max_power_per_slot = max(max_charge_kw, max_discharge_kw) × 0.5`. This represents the physical reality that one PCS can be used for **either** charging **or** discharging in the same slot, not both at full power. The constraint is verified end-to-end against test fixtures showing `cycles/day ≈ 1` and the round-trip efficiency ratio matching exactly 90.25 % (= 95 % × 95 %).

This constraint is implemented in **both** `optimize_battery_fip` (single-capacity LP) and `optimize_capacity_fip` (capacity-as-decision-variable LP).

### 8.5 Solver

CBC (bundled with PuLP). Typical solve time on a modest CPU is tens of seconds for the full 17,520-timeslot problem.

---

## 9. Optimal Battery Sizing / 最適蓄電池容量探索

The two-stage approach balances accuracy and runtime:

### 9.1 Stage 1 — One-shot LP (~15 seconds)

- Battery capacity becomes a decision variable: `capacity_var ∈ [0, capacity_upper]`
- SOC bounds become linear in capacity: `soc[t] ≤ capacity_var × soc_max_pct`, `soc[t] ≥ capacity_var × soc_min_pct`
- Objective adds annualized battery investment as a linear penalty: `+ capacity_var × bat_cost / irr_period_years`
- Returns the optimal capacity directly, equivalent to maximizing project NPV under the linear approximation

### 9.2 Stage 2 — Grid Search (~2–3 minutes)

- Sweeps battery capacity around the Stage 1 optimum (e.g., 0 → 2× optimum, 15–20 steps)
- For each capacity, runs the standard `optimize_battery_fip` LP
- Records: annual revenue with/without battery, NPV, P-IRR, payback years
- Visualizes the curves and marks the optimum

### 9.3 Subsidy Handling

PV and battery subsidies (as percentages) reduce the **net unit cost** before being passed to the LP and grid search. This ensures the optimal capacity correctly responds to subsidy levels.

---

## 10. Two Business Cases / 2 つの事業ケース

### 10.1 Case A — New Build

**Persona**: EPC / project developers planning new utility-scale FIP plants with co-located storage.

**Investment**: PV CAPEX + battery CAPEX − subsidies.

**Cashflow phases** (across `irr_period_years`, default 20):
1. **Premium period** (years 1..20): JEPX + premium + non-fossil + arbitrage − BG fee
2. **Post-premium** (years 21..battery EOL): JEPX + non-fossil + arbitrage − BG fee

### 10.2 Case B — Existing FIT → FIP Transition

**Persona**: Existing FIT plant owners (IPPs) deciding whether to add a battery and migrate to FIP.

**Critical rule**: **The FIP premium grant period equals the FIT remainder** — a FIT plant that was 12 years old when transitioning to FIP gets only 8 more years of premium (the remaining 8 of its original 20-year FIT contract).

**Investment**: **Battery only** (PV is sunk cost, already sitting on the operator's balance sheet).

**Cashflow phases**:
1. **FIT period** (years 1..FIP transition year − 1): FIT fixed-price sales (with curtailment); no battery operation
2. **FIP + premium period** (FIP transition year..FIT 20-year maturity): JEPX + premium + non-fossil + arbitrage − BG fee
3. **Post-premium** (FIT 20-year + 1..battery EOL): JEPX + non-fossil + arbitrage − BG fee

#### Concrete example (FIT 36 ¥/kWh, transition at year 12, battery life 15 years)
```
years  1..11: FIT sales (36 ¥/kWh × generation)
years 12..20: FIP + premium + battery arbitrage (8 years)
years 21..26: post-premium + battery arbitrage (6 years)
year 27+    : battery EOL → JEPX direct sale only or replacement
```

### 10.3 Case B — Project IRR is Computed on Incremental CF

For Case B, Project IRR / NPV / payback are computed on the **incremental cashflow** basis:

```
incremental_CF[y] = CF_with_FIP_transition[y] − CF_FIT_continued[y]
```

The incremental CF measures the **return on the battery investment alone** — the question the operator is actually asking is "is it worth adding the battery and switching?", not "what is the absolute return of the new combined system?" (which would double-count the already-recovered FIT revenue). The application also outputs the 20-year cumulative CF comparison "FIT continuation vs FIP transition" so the user can see both perspectives.

### 10.4 Case B — Owner-perspective Cashflow Chart (案α(i))

For visual presentation, the cashflow chart for Case B uses an **owner-lifecycle** layout that places **two investments** at year 0 origin:

- **Year 0 (PV operation start)**: PV initial investment (-pv_net_capex) shown in the `initial_capex` column
- **Year N (FIP transition year)**: battery additional investment (-bat_net_capex) shown in the `initial_capex` column
- Years 1..(N−1): FIT fixed-price revenue
- Years N..(FIT end): FIP + premium + battery arbitrage
- Years (FIT end + 1)..(battery EOL): post-premium + arbitrage

This is the chart the **plant owner** wants to see — the full lifecycle including their original PV investment. The Project IRR / NPV figures shown alongside are still computed on the incremental-CF basis (battery investment only), so the chart and the metric serve two distinct purposes.

---

## 11. Battery Degradation & Lifetime / 蓄電池劣化・寿命

- **Linear degradation**: default 1.0 %/year (so a 20-year-old battery retains ≈ 80 % capacity, matching the Mitsubishi Research Institute reference assumption)
- **Battery lifetime**: default 20 years (user-editable)
- **End-of-life handling**: user selects either
  - **Replace (re-invest)**: replacement cost added at battery EOL year, with configurable replacement-cost ratio (default 60 % of original, reflecting future cost-down assumption)
  - **Decommission**: battery operation stops, cashflow falls back to JEPX direct sale only (no arbitrage)
- **Decommissioning fee**: default 5 % of gross CAPEX, charged in the final year

The arbitrage value `arbitrage_initial × degrade_factor[y]` is applied year by year, so older batteries deliver less incremental revenue.

---

## 12. CAPEX & OPEX Defaults / 単価デフォルト

| Item | Default | Unit | Source |
|---|---|---|---|
| PV system unit cost | 155,600 | ¥/kW | METI 2026 assumption (ground-mount ≥ 50 kW): system 12.9 + land 1.21 + interconnection 1.45 = 15.56 万円/kW |
| Battery unit cost | 68,000 | ¥/kWh | Mitsubishi Research Institute subsidy program data (R3–R6 average): system 5.5 + installation 1.3 = 6.8 万円/kWh |
| O&M ratio | 1.5 | %/year | CAPEX-relative; METI 2026 implies ~2.7 % (user-editable) |
| Battery O&M (PCS-kW mode) | 5,000 | ¥/kW(PCS)/year | MRI reference assumption |
| Decommissioning | 5.0 | % | Gross CAPEX × pct, applied in final year |
| Equity ratio | 30 | % | |
| Loan interest | 2.0 | %/year | |
| Loan period | 15 | years | |
| Project IRR period | 20 | years | |

The battery O&M model has two selectable modes: **CAPEX-ratio** (shared with PV, traditional) or **per-PCS-kW** (MRI reference, default). All values are editable in the UI.

---

## 13. Multi-root IRR / IRR の多根対応

A typical Project CF for this simulator has the structure:

```
year 0       : −investment      (large negative)
years 1..N−1 : +annual revenue  (positive)
year N       : +revenue − decommissioning fee  (could be negative)
```

This creates **two sign changes** in the cashflow series, so the IRR polynomial may have **two real roots**. A naive Newton solver depends on initial-value choice and can converge to the wrong root (e.g., a 50 % "IRR" that is actually the runaway divergence root).

The implementation in `calc_irr` therefore:

1. Returns `None` if the cashflow has only one sign (IRR undefined).
2. Performs a **coarse scan** over `r ∈ [−0.99, 5.0]` at 1 % resolution.
3. **Bisects every interval** that shows a sign change in the scanned NPV.
4. Returns the **root closest to zero** (the economically meaningful root).
5. Falls back to wide-range bisection if no scan-detected interval is found.

This approach is verified against the MRI reference simulation (see § 14).

---

## 14. Validation / 検証

### 14.1 Mitsubishi Research Institute Slide-47 Cross-check

Conditions: 5 MW PV + 1 MWh battery (250 kW PCS = 0.25 C slow battery), Kyushu area, FIP base price 15 ¥/kWh, 20-year period, 15 % output curtailment. Computed as the **"battery marginal IRR"** — PV is treated as pre-existing (sunk cost), incremental CF on battery investment only.

| Battery unit cost | This tool IRR | MRI reference | Δ |
|---|---|---|---|
| 150,000 ¥/kWh | −7.96 % | −7.40 % | −0.56 pt |
| 100,000 ¥/kWh | −3.74 % | −2.80 % | −0.94 pt |
|  60,000 ¥/kWh | +1.79 % | +3.10 % | −1.31 pt |

All three cases match the MRI reference within ~1 pt. **Important finding**: switching to a 0.5 C configuration drops IRR by ~5 pt and diverges from MRI, suggesting MRI's reference simulation assumes a **0.25 C slow-battery** topology rather than the more aggressive 0.5 C often used in newer projects.

### 14.2 Battery LP Pass-through Bug

Smoke tests at three battery capacities (7.34, 100, 1000 kWh) verify:
- `soc > soc_max + ε` count = 0
- `soc < soc_min − ε` count = 0
- `(charge > ε) AND (discharge > ε)` count = 0 (no pass-through)
- `charge + discharge > max_power_per_slot + ε` count = 0 (mutual exclusion holds)
- `cycles/day` ≤ ~2

After applying the mutual-exclusion constraint, all three cases show `cycles/day ≈ 1` and the with/without-battery efficiency ratio matches exactly 90.25 % (= 95 % charge × 95 % discharge).

---

## 15. Implementation Notes / 実装上の注意

### 15.1 Standalone Architecture

`app.py` is intentionally kept as a single standalone file (~2,800 LOC). The only external file dependencies are:

- `radiation.db` (Git LFS, ~19 MB)
- `jepx.db` (Git LFS, ~58 MB)

This simplifies deployment to Hugging Face Spaces and avoids module import complexities.

### 15.2 Numerical Input Handling

All numerical inputs use `is not None` checks (rather than truthy checks) to allow valid `0` inputs. For example:
```python
sell_price_val = sell_price if sell_price is not None else DEFAULT
```

### 15.3 Missing Data Handling

The NEDO METPV-20 dataset uses `8888` as a missing-value marker. The database loader (`load_from_db`) converts these to NaN before processing.

### 15.4 Calendar Base Year

The 30-minute time series is built on a non-leap year (2023) base to avoid Feb 29 indexing issues. JEPX FY2023 also has its leap-day slots removed (see § 5.2).

### 15.5 Reused vs Removed from `pv-sim-biz`

Reused unchanged:
- JIS C 8907 generation pipeline (8-array, bifacial, snow-aware albedo)
- `radiation.db` 77-site loader
- pvlib POA conversion
- PuLP/CBC LP framework (objective swapped, constraints adjusted)

Removed entirely:
- Demand presets (ComStock 6 building types) and CSV upload
- High-voltage / extra-high-voltage tariff model (basic charge, energy charge, power factor)
- Demand tracking and contracted-demand calculation
- Self-consumption logic (`min(gen, demand)`)
- Reverse-power-flow (RPR) prohibition mode
- Mode A (self-consumption lease/PPA) and Mode B (microgrid) bifurcation
- CO₂ reduction calculation

---

## 16. Roadmap / 今後の拡張余地

- **Curtailment profile refinement**: Replace internal default profile with OCCTO-published actual curtailment data (per area, per season): https://www.occto.or.jp/institution/shutsuryokuyokusei/
- **FIP base-price defaults by year/capacity tier**: Currently a flat 9.6 ¥/kWh; the actual base price varies by procurement year and capacity bracket
- **HiGHS solver migration**: Optional faster LP solver to replace CBC
- **Sensitivity analysis**: Tornado charts for input parameter sensitivity on Project IRR
- **Battery degradation model refinement**: Calendar + cycle aging instead of pure linear

---

## 17. References / 参考文献

- **JIS C 8907:2005** — Estimation method of generating electric energy by PV power systems: https://kikakurui.com/c8/C8907-2005-01.html
- **pvlib-python** — https://pvlib-python.readthedocs.io/
- **NEDO METPV-20** — Japanese solar irradiance database (77 sites)
- **JEPX (Japan Electric Power Exchange)** — https://www.jepx.org/
- **PuLP** — https://coin-or.github.io/pulp/
- **エネがえる「FIP 転マニュアル」** — https://www.enegaeru.com/evaluationforconsidering-fiptransfer-storagebattery
- **OCCTO 出力制御実績** — https://www.occto.or.jp/institution/shutsuryokuyokusei/
- **METI 太陽光発電のコスト動向（2026 年度想定）** — https://www.meti.go.jp/shingikai/energy_environment/storage_system/pdf/20250307_1.pdf
