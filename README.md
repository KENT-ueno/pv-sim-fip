---
title: FIP転＋蓄電池 事業性シミュレーター
emoji: 🔋
colorFrom: green
colorTo: blue
sdk: gradio
sdk_version: 6.26.0
app_file: app.py
pinned: false
license: mit
---

# pv-sim-fip — Solar PV + Battery FIP Transition Simulator / 太陽光＋蓄電池 FIP 転事業性シミュレーター

[![Hugging Face Space](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Spaces-yellow)](https://huggingface.co/spaces/hachinai/pv-sim-fip)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Gradio](https://img.shields.io/badge/Gradio-6.26-orange)](https://gradio.app/)

A web-based simulator for **utility-scale Solar PV + battery storage business cases under Japan's FIP (Feed-in Premium) scheme**. Built on JIS C 8907 generation modeling, NEDO METPV-20 weather data for 77 Japanese sites, JEPX spot-market price database (9 areas × 6 fiscal years), and PuLP/CBC linear programming for optimal 30-minute battery dispatch over 17,520 timeslots per year.

日本国内の **FIP（Feed-in Premium）制度** に対応した、太陽光発電所併設型蓄電池の事業性シミュレーターです。**新規 FIP 発電所＋蓄電池の新設（ケースA）** と **既存 FIT 発電所への蓄電池後付＋ FIP 移行（ケースB）** の両ケースに対応し、JEPX 市場連動売電・FIP プレミアム・出力制御回避・蓄電池劣化を統合した 20 年事業性評価を一括で行います。

**🔗 Live Demo / ライブデモ:** https://huggingface.co/spaces/hachinai/pv-sim-fip

**📝 Article (Qiita) / 解説記事:** https://qiita.com/jizou/items/bb557711a62e119a85ac

---

## ✨ Features / 機能

### English

- **JIS C 8907 compliant generation model** — Tilted-plane irradiance via pvlib (Erbs decomposition + isotropic transposition), per-array PCS clipping, JIS C 8907 temperature correction with hourly ambient data, and bifacial gain via `pvlib.bifacial.infinite_sheds` with snow-aware albedo switching.
- **77-site Japanese weather database** — NEDO METPV-20 dataset (10 elements: GHI, temperature, snow depth, etc.) bundled as SQLite (`radiation.db`).
- **JEPX spot-price database** — 9 areas × 6 fiscal years (FY2020–FY2025, Okinawa excluded as it has no JEPX trading) × 17,520 half-hour slots = 945,720 records as SQLite (`jepx.db`). Multi-year averaging supported (e.g., exclude FY2022 Ukraine-crisis spike).
- **FIP revenue model** — JEPX market sales + FIP premium + non-fossil certificate − BG (balancing group) fee, with automatic two-phase switching (premium period → post-premium period).
- **Two business cases**:
  - **Case A (New build)** — New PV + battery installed under FIP from day one. CAPEX includes both PV and battery; cashflow runs 1–20 years (with premium) and 21+ years (post-premium, until battery end-of-life).
  - **Case B (Existing FIT → FIP transition)** — Battery added to an existing FIT plant. Critically, **the FIP premium grant period equals the FIT remainder** (e.g., FIT 12-year-old plant → 8 years of premium). CAPEX is **battery only** (PV is sunk cost). Three-phase cashflow: FIT period → FIP+premium period → post-premium period.
- **Battery dispatch optimization (PuLP/CBC LP)** — Annual revenue maximization (JEPX × export + premium × export + non-fossil − BG fee) over all 17,520 timeslots, with mutual exclusion constraint `charge[t] + discharge[t] ≤ max_power_per_slot`, SOC continuity, terminal SOC equality, and POI-based curtailment cap `export[t] ≤ gen[t] × (1 − curtail_prob[t])`.
- **Optimal battery sizing** — Two-stage approach: (1) one-shot LP with battery capacity as decision variable for fast NPV optimum, (2) grid search around the optimum to visualize NPV/IRR/payback curves.
- **Curtailment model** — Monthly × hourly curtailment-probability profile (concentrated in spring/autumn 10:00–15:00) scaled to user-specified annual curtailment rate. Battery acts as curtailment-avoidance buffer.
- **Battery degradation & lifetime** — Linear annual degradation (default 1.0%/year), end-of-life replacement / decommission selectable, replacement cost ratio configurable (default 60% of original, future cost-down assumption).
- **With/without-battery comparison** — Battery's incremental value automatically computed (JEPX direct sale baseline runs in background).
- **Case B incremental analysis** — "FIP transition + battery" vs "FIT continuation" 20-year cumulative CF comparison; Project IRR / NPV / payback computed on incremental CF basis (battery investment only).
- **Owner-perspective CF chart (Case B)** — Two-stage investment displayed across the project lifecycle: PV initial investment at year 0, battery investment at FIP-transition year. Visualizes the full ownership timeline.

### 日本語

- **JIS C 8907 準拠の発電量モデル** — pvlib による傾斜面日射量変換（Erbs モデル＋ isotropic モデル）、面別 PCS クリップ、毎時外気温反映の温度補正、`pvlib.bifacial.infinite_sheds` による両面パネル対応、積雪深データに基づく動的アルベド切替（積雪時 0.7 ／通常 0.2）。
- **77 地点の日本気象データベース** — NEDO METPV-20 の 10 要素データ（日射量・気温・積雪深ほか）を SQLite (`radiation.db`) として同梱。
- **JEPX スポット価格データベース** — 9 エリア × 6 年度（2020〜2025 年度、沖縄は JEPX 取引なしのため除外）× 30 分 × 365 日 = 945,720 レコードを SQLite (`jepx.db`) として同梱。複数年度の平均利用に対応（例: 2022 年度＝ウクライナ危機高騰をデフォルト除外）。
- **FIP 収益モデル** — JEPX 市場売電収入＋ FIP プレミアム＋非化石証書−BG（バランシンググループ）手数料を統合。プレミアム期間→プレミアム終了後の 2 フェーズ自動切替。
- **2 つのビジネスケース**:
  - **ケースA（新設）** — 太陽光＋蓄電池を新設し最初から FIP 認定。CAPEX に PV と蓄電池を含み、1〜20 年（プレミアム期間）＋ 21 年〜蓄電池寿命（プレミアム終了後）の年次 CF を算出。
  - **ケースB（既存 FIT → FIP 転）** — 既設 FIT 発電所に蓄電池を追加して FIP 移行。**重要: FIP プレミアム交付期間 ＝ FIT 残存期間**（FIT 12 年経過なら 8 年分のプレミアム）。CAPEX は **蓄電池のみ**（PV はサンクコスト）。FIT 期間→ FIP ＋プレミアム期間→プレミアム終了後の 3 フェーズ CF。
- **蓄電池最適充放電（PuLP/CBC LP）** — 17,520 コマ全体で年間収益（JEPX ×売電量＋プレミアム×売電量＋非化石証書−BG 手数料）を最大化。`charge[t] + discharge[t] ≤ max_power_per_slot` の mutual exclusion 制約、SOC 連続性、終端 SOC 一致制約、POI ベース輸出上限 `export[t] ≤ gen[t] × (1 − curtail_prob[t])` を含む完全な定式化。
- **最適蓄電池容量探索** — 2 段階アプローチ：(1) 蓄電池容量を決定変数に含めた LP 一体化で NPV 最適容量を高速取得、(2) 容量範囲のグリッドサーチで NPV / P-IRR / 投資回収年数のカーブを可視化。
- **出力制御モデル** — 月別×時間帯別の制御確率プロファイル（春秋 10:00〜15:00 に集中）を年間制御率にスケーリング。蓄電池は制御回避バッファとして機能。
- **蓄電池劣化・寿命管理** — 年率指定の線形劣化（デフォルト 1.0%/年）、寿命到来時の交換／終了選択、交換単価比率（デフォルト 60%、将来コストダウン想定）。
- **蓄電池あり / なしの自動比較** — バックグラウンドで JEPX 直売（蓄電池なし）を自動計算し、蓄電池の増分価値を可視化。
- **ケースB 増分分析** — 「FIP 転＋蓄電池」 vs 「FIT 継続」の 20 年累計 CF 比較を出力。Project IRR / NPV / 投資回収年数は **蓄電池追加投資に対する増分 CF ベース** で算出。
- **オーナー視点 CF チャート（ケースB）** — PV 初期投資（運転開始年 0）＋蓄電池追加投資（FIP 転年）の 2 段階投資をライフサイクル全景で表示。

---

## 🎯 Who is this for? / 想定ユーザー

| Persona / ペルソナ | Case | 主な用途 |
|---|---|---|
| FIT 発電所オーナー / IPP | B: 既存 FIT → FIP 転 | 既設 FIT 発電所を FIP 転すべきかの判断。蓄電池追加投資の回収可否、FIT 継続との 20 年累計 CF 比較 |
| EPC ／開発事業者 | A: 新規 FIP 新設 | 新規 FIP ＋蓄電池プロジェクトの事業計画、最適蓄電池容量探索、Project IRR / NPV / 回収年数の試算 |

> **Why FIP transition matters now / FIP 転がいま重要な理由**: From FY2026, output curtailment priority is expected to be reversed — FIT plants will be curtailed **before** FIP plants. For existing FIT owners, FIP transition is becoming a strategic asset-defense move, not just a revenue play. / 2026 年度から出力制御の優先順位が FIT ＞ FIP に変更される見通しのため、既存 FIT オーナーにとって FIP 転は単なる収益向上策ではなく **資産防衛の戦略的必然** となりつつあります。

---

## 🛠 Tech Stack / 技術スタック

- **Language**: Python 3.10+
- **UI**: Gradio 5.23
- **Plotting**: Plotly
- **Solar modeling**: pvlib (Erbs, isotropic transposition, infinite_sheds for bifacial)
- **Optimization**: PuLP + CBC (linear programming, 17,520 timeslots)
- **Data**: SQLite — `radiation.db` (NEDO METPV-20, 77 sites) + `jepx.db` (JEPX spot prices, 9 areas × 6 fiscal years)
- **Numerical**: pandas, numpy
- **Deployment**: Hugging Face Spaces

---

## 🚀 Quick Start / クイックスタート

### Online (Recommended)

Use the live demo on Hugging Face Spaces — no installation required:

→ **https://huggingface.co/spaces/hachinai/pv-sim-fip**

### Local

```bash
git clone https://github.com/KENT-ueno/pv-sim-fip.git
cd pv-sim-fip
pip install -r requirements.txt
python app.py
```

The Gradio UI will start at `http://127.0.0.1:7860`.

> **Note**: `radiation.db` (~19 MB) and `jepx.db` (~58 MB) are managed via Git LFS. You may need `git lfs pull` after cloning.

---

## 📂 Repository Structure / リポジトリ構成

```
pv-sim-fip/
├── app.py                      # Main application (standalone Gradio app)
├── radiation.db                # NEDO METPV-20 weather DB, 77 sites (Git LFS)
├── jepx.db                     # JEPX spot price DB, 9 areas × 6 FYs (Git LFS)
├── requirements.txt
├── README.md                   # This file
├── LICENSE                     # MIT
└── docs/
    └── architecture.md         # Detailed architecture & implementation notes
```

---

## 📚 References / 参考文献

- **JIS C 8907:2005** — Estimation method of generating electric energy by PV power systems: https://kikakurui.com/c8/C8907-2005-01.html
- **pvlib-python** — PV modeling library: https://pvlib-python.readthedocs.io/
- **NEDO METPV-20** — Japanese solar irradiance database (77 sites)
- **JEPX (Japan Electric Power Exchange)** — Spot market price source: https://www.jepx.org/
- **PuLP** — Python LP modeler: https://coin-or.github.io/pulp/
- **エネがえる「FIP 転マニュアル」** — https://www.enegaeru.com/evaluationforconsidering-fiptransfer-storagebattery

---

## 🌐 Sister Projects / 姉妹プロジェクト

- **Residential version / 家庭用**: [pv-sim-gh](https://huggingface.co/spaces/hachinai/pv-sim-gh) — 住宅用太陽光需給シミュレーター
- **Industrial self-consumption / Microgrid / 産業用自家消費・マイクログリッド**: [pv-sim-biz](https://huggingface.co/spaces/hachinai/pv-sim-biz) — 産業用太陽光＋蓄電池シミュレーター

---

## 📄 License / ライセンス

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

本プロジェクトは **MIT ライセンス** のもとで公開されています。詳細は [LICENSE](LICENSE) を参照してください。

---

## 🙏 Acknowledgments / 謝辞

- NEDO (New Energy and Industrial Technology Development Organization) for the METPV-20 dataset
- JEPX (Japan Electric Power Exchange) for the spot market price data
- The pvlib-python and PuLP open-source communities
- 三菱総合研究所「次世代型太陽光発電の導入加速化に向けた制度的支援に関する調査」の試算結果を本ツールの妥当性検証に活用
