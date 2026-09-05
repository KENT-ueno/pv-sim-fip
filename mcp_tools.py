"""
mcp_tools.py — pv-sim-fip MCPツール群（Phase 4a）
==================================================
Gradio `mcp_server=True` + `gr.api()` 経由でAIエージェント（Claude等）に公開する
API関数を定義する。計算ロジック本体は app.py の関数をimportして使用し、
本モジュールは「型付きパラメータの受け口＋検証＋構造化JSONの組み立て」に徹する。

設計書: docs/agent_design.md
  - パラメータ名に単位を含める（例: fit_tariff_yen_per_kwh）
  - validate → ユーザー確認 → simulate の2段階プロトコル
  - 全simulate結果に assumptions（入力エコー）と caveats（免責）を同梱
"""

import os
import sqlite3

import numpy as np

# NOTE: 計算ロジック本体（app.py）は _get_app() で遅延解決する。
# - モジュールレベルで `import app` すると「mcp_tools を先に import した場合」に循環importで失敗する
# - `python app.py` で起動した場合（HF Spaces含む）、appは "__main__" 名でロードされるため、
#   関数内で素朴に `import app` すると app.py が二重実行され、リクエストスレッド内で
#   build_ui() が走って Gradio がクラッシュする（'Radio' object has no attribute '_id'）
# 既にロード済みのモジュール（app または __main__）を探して再利用するのが唯一安全な方法。


def _get_app():
    """計算ロジック本体（app.pyモジュール）を返す。二重import・二重build_uiを防ぐ。"""
    import sys
    for name in ("app", "__main__"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "optimize_battery_fip"):
            return mod
    import app
    return app

# ============================================================
# 定数
# ============================================================

# 入力上限（無料CPUでの巨大LP実行を防ぐガード）
MAX_PPEAK_KW = 100_000.0
MAX_BATTERY_KWH = 100_000.0
MAX_PCS_KW = 50_000.0

_EOL_MAP = {
    "replace": "交換（再投資）",
    "end": "終了（蓄電池なし運用）",
}

_COMMON_CAVEATS = [
    "蓄電池運用は1年分のJEPX価格を全て既知とする完全予見LPによる理論上限であり、"
    "実運用（前日予測ベース）の収益はこれを下回るのが通常です",
    "JEPX価格は過去実績の平均であり、将来の市場価格を保証するものではありません",
    "本結果は投資判断の参考情報であり、収益を保証するものではありません",
]


# ============================================================
# 内部ヘルパー
# ============================================================

def _resolve_station(station_no: str):
    """地点番号から (lat, lon, ghi_df, temp_df, station_name) を返す。"""
    app = _get_app()
    conn = sqlite3.connect(app.DB_PATH)
    row = conn.execute(
        "SELECT point_name FROM points WHERE point_no = ?", (str(station_no),)
    ).fetchone()
    conn.close()
    if row is None:
        raise ValueError(
            f"地点番号 {station_no} が見つかりません。list_stations で一覧を確認してください。"
        )
    lat, lon, ghi_df, temp_df = app.load_from_db(str(station_no))
    return lat, lon, ghi_df, temp_df, row[0]


def _normalize_and_validate(
    case: str,
    station_no: str,
    ppeak_kw: float,
    tilt_deg: float,
    azimuth_deg: float,
    pcs_limit_kw: float,
    battery_capacity_kwh: float,
    battery_max_charge_kw: float,
    battery_max_discharge_kw: float,
    battery_charge_efficiency_pct: float,
    battery_discharge_efficiency_pct: float,
    battery_soc_min_pct: float,
    battery_soc_max_pct: float,
    battery_life_years: int,
    battery_degrade_pct_per_year: float,
    battery_eol_action: str,
    battery_replace_cost_ratio_pct: float,
    jepx_area: str,
    jepx_fiscal_years: list[int],
    fip_base_price_yen_per_kwh: float,
    nonfossil_price_yen_per_kwh: float,
    bg_fee_yen_per_kwh: float,
    fip_premium_years: int,
    annual_curtailment_rate_pct: float,
    pv_cost_yen_per_kw: float,
    battery_cost_yen_per_kwh: float,
    subsidy_pv_pct: float,
    subsidy_battery_pct: float,
    om_ratio_pct_per_year: float,
    battery_om_yen_per_kw_pcs_per_year: float,
    decommission_pct: float,
    equity_ratio_pct: float,
    loan_interest_pct: float,
    loan_years: int,
    irr_period_years: int,
    fit_tariff_yen_per_kwh: float,
    fit_term_years: int,
    fip_transition_year: int,
    pv_acquisition_cost_yen: float,
    fit_curtailment_rate_pct: float = 0.0,
    pv_degrade_pct_per_year: float = 0.5,
):
    """パラメータを正規化し (params, warnings, errors) を返す。LPは実行しない。"""
    app = _get_app()
    errors = []
    warnings = []

    # --- 地点 ---
    station_name = None
    try:
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute(
            "SELECT point_name FROM points WHERE point_no = ?", (str(station_no),)
        ).fetchone()
        conn.close()
        if row is None:
            errors.append(f"地点番号 {station_no} がDBに存在しません（list_stations 参照）")
        else:
            station_name = row[0]
    except Exception as e:
        errors.append(f"地点DB照会エラー: {e}")

    # --- 範囲チェック ---
    if not (0 < ppeak_kw <= MAX_PPEAK_KW):
        errors.append(f"ppeak_kw は 0 < x <= {MAX_PPEAK_KW:.0f} で指定してください")
    if not (0 <= battery_capacity_kwh <= MAX_BATTERY_KWH):
        errors.append(f"battery_capacity_kwh は 0 <= x <= {MAX_BATTERY_KWH:.0f} で指定してください")
    # 蓄電池なし（capacity=0）のときはPCSレートは意味を持たないためチェック対象外にする。
    # capacity=0なら発電・売電計算はbaseline_no_batteryのみを使い、PCS値は一切参照されない。
    if battery_capacity_kwh > 0:
        if not (0 < battery_max_charge_kw <= MAX_PCS_KW):
            errors.append(f"battery_max_charge_kw は 0 < x <= {MAX_PCS_KW:.0f} で指定してください")
        if not (0 < battery_max_discharge_kw <= MAX_PCS_KW):
            errors.append(f"battery_max_discharge_kw は 0 < x <= {MAX_PCS_KW:.0f} で指定してください")
    elif battery_max_charge_kw < 0 or battery_max_discharge_kw < 0:
        errors.append("battery_max_charge_kw / battery_max_discharge_kw は0以上で指定してください")
    if not (0 <= tilt_deg <= 90):
        errors.append("tilt_deg は 0〜90 で指定してください")
    azimuth_deg = float(azimuth_deg) % 360
    if jepx_area not in app.JEPX_AREAS:
        errors.append(f"jepx_area は {app.JEPX_AREAS} から選択してください")
    years = [int(y) for y in (jepx_fiscal_years or [])]
    bad_years = [y for y in years if y not in app.JEPX_FISCAL_YEARS]
    if not years:
        errors.append("jepx_fiscal_years を1つ以上指定してください")
    if bad_years:
        errors.append(f"jepx_fiscal_years に未収録年度があります: {bad_years}（収録: {app.JEPX_FISCAL_YEARS}）")
    if battery_eol_action not in _EOL_MAP:
        errors.append('battery_eol_action は "replace"（交換）または "end"（終了）を指定してください')
    if not (1 <= int(irr_period_years) <= 40):
        errors.append("irr_period_years は 1〜40 で指定してください")
    if not (0 <= battery_soc_min_pct < battery_soc_max_pct <= 100):
        errors.append("SOC範囲が不正です（0 <= soc_min < soc_max <= 100）")
    for name, v in [("battery_charge_efficiency_pct", battery_charge_efficiency_pct),
                    ("battery_discharge_efficiency_pct", battery_discharge_efficiency_pct)]:
        if not (50 <= v <= 100):
            errors.append(f"{name} は 50〜100 で指定してください")
    if not (0 <= annual_curtailment_rate_pct <= 50):
        errors.append("annual_curtailment_rate_pct は 0〜50 で指定してください")
    if not (0 <= pv_degrade_pct_per_year <= 5):
        errors.append("pv_degrade_pct_per_year は 0〜5 で指定してください")

    if case == "B":
        if not (1 <= int(fip_transition_year) <= int(fit_term_years)):
            errors.append("fip_transition_year は 1〜fit_term_years の範囲で指定してください")
        if fit_tariff_yen_per_kwh <= 0:
            errors.append("fit_tariff_yen_per_kwh を正の値で指定してください")

    # --- 警告（エラーではないが確認を促す） ---
    if 2022 in years:
        warnings.append("2022年度はウクライナ危機による価格高騰年です（結果が楽観側に振れます）")
    if battery_capacity_kwh > 0 and battery_max_charge_kw > battery_capacity_kwh:
        warnings.append(
            f"充電レート {battery_max_charge_kw:.0f}kW が容量 {battery_capacity_kwh:.0f}kWh を超えています"
            "（1C超の高速蓄電池想定になっています）"
        )
    if annual_curtailment_rate_pct > 30:
        warnings.append("制御率30%超では内蔵プロファイルの確率クリップにより実現制御率が目標を下回る場合があります")
    if fip_base_price_yen_per_kwh == 0:
        warnings.append("FIP基準価格が0円です（FIP認定を受けない市場直売想定になります）")
    if case == "B" and pv_acquisition_cost_yen <= 0:
        warnings.append("pv_acquisition_cost_yen が未指定のため、ライフサイクルCF表示は省略されます（増分IRR計算には影響なし）")

    effective_premium_years = (
        max(0, int(fit_term_years) - int(fip_transition_year) + 1)
        if case == "B" else int(fip_premium_years)
    )

    # 参照価格の事前見積もり（LP実行なし、DBから直接算定）。
    # 実際の値は simulate 実行時に発電量重み付けと無関係な単純平均で再計算されるが、
    # LPを回す前に「基準価格から実際どれだけ差し引かれるか」を確認できるようにする。
    reference_price_preview = None
    premium_preview = None
    if not errors and jepx_area in app.JEPX_AREAS and years:
        try:
            conn = sqlite3.connect(app.JEPX_DB_PATH)
            ph = ",".join(["?"] * len(years))
            row = conn.execute(
                f"SELECT AVG(p) FROM ("
                f"SELECT AVG(price) AS p FROM jepx_prices "
                f"WHERE area = ? AND fiscal_year IN ({ph}) GROUP BY date, slot)",
                [jepx_area] + years,
            ).fetchone()
            conn.close()
            if row and row[0] is not None:
                reference_price_preview = round(float(row[0]), 2)
                raw = float(fip_base_price_yen_per_kwh) - reference_price_preview
                # ゼロ下限クリップ（simulate側と同じ扱い）
                premium_preview = round(max(0.0, raw), 2)
                if raw < 0:
                    warnings.append(
                        f"参照価格{reference_price_preview:.2f}円/kWh（選択エリア・年度のJEPX平均）が"
                        f"基準価格{fip_base_price_yen_per_kwh:.2f}円/kWhを上回るため、"
                        "プレミアムは0円/kWhに制限されます（FIP制度上、負のプレミアムは発生しません）。"
                        "この条件ではFIPによる収益上乗せがないため、市場直売と同等の採算になります"
                    )
        except Exception:
            pass  # プレビューに失敗してもエラーにはしない（simulate側で正式算定される）

    params = {
        "case": "新規FIP" if case == "A" else "既存FIT→FIP転",
        "station_no": str(station_no),
        "station_name": station_name,
        "ppeak_kw": float(ppeak_kw),
        "tilt_deg": float(tilt_deg),
        "azimuth_deg": float(azimuth_deg),
        "pcs_limit_kw": float(pcs_limit_kw) if pcs_limit_kw and pcs_limit_kw > 0 else None,
        "battery_capacity_kwh": float(battery_capacity_kwh),
        "battery_max_charge_kw": float(battery_max_charge_kw),
        "battery_max_discharge_kw": float(battery_max_discharge_kw),
        "battery_charge_efficiency_pct": float(battery_charge_efficiency_pct),
        "battery_discharge_efficiency_pct": float(battery_discharge_efficiency_pct),
        "battery_soc_min_pct": float(battery_soc_min_pct),
        "battery_soc_max_pct": float(battery_soc_max_pct),
        "battery_life_years": int(battery_life_years),
        "battery_degrade_pct_per_year": float(battery_degrade_pct_per_year),
        "battery_eol_action": battery_eol_action,
        "battery_replace_cost_ratio_pct": float(battery_replace_cost_ratio_pct),
        "jepx_area": jepx_area,
        "jepx_fiscal_years": years,
        "fip_base_price_yen_per_kwh": float(fip_base_price_yen_per_kwh),
        "reference_price_preview_yen_per_kwh": reference_price_preview,
        "premium_preview_yen_per_kwh": premium_preview,
        "nonfossil_price_yen_per_kwh": float(nonfossil_price_yen_per_kwh),
        "bg_fee_yen_per_kwh": float(bg_fee_yen_per_kwh),
        "effective_premium_years": effective_premium_years,
        "annual_curtailment_rate_pct": float(annual_curtailment_rate_pct),
        "pv_cost_yen_per_kw": float(pv_cost_yen_per_kw),
        "battery_cost_yen_per_kwh": float(battery_cost_yen_per_kwh),
        "subsidy_pv_pct": float(subsidy_pv_pct),
        "subsidy_battery_pct": float(subsidy_battery_pct),
        "om_ratio_pct_per_year": float(om_ratio_pct_per_year),
        "battery_om_mode": "PCS_kW建て（三菱総研試算）",
        "battery_om_yen_per_kw_pcs_per_year": float(battery_om_yen_per_kw_pcs_per_year),
        "decommission_pct": float(decommission_pct),
        "equity_ratio_pct": float(equity_ratio_pct),
        "loan_interest_pct": float(loan_interest_pct),
        "loan_years": int(loan_years),
        "irr_period_years": int(irr_period_years),
        "pv_degrade_pct_per_year": float(pv_degrade_pct_per_year),
        # PVの既経過年数。ケースB: FIP転時点で何年稼働済みか（fip_transition_year-1）。
        # ケースA: 新設のため0
        "pv_start_age_years": (int(fip_transition_year) - 1) if case == "B" else 0,
    }
    if case == "B":
        params.update({
            "fit_tariff_yen_per_kwh": float(fit_tariff_yen_per_kwh),
            "fit_term_years": int(fit_term_years),
            "fip_transition_year": int(fip_transition_year),
            "pv_acquisition_cost_yen": float(pv_acquisition_cost_yen),
            "fit_curtailment_rate_pct": float(fit_curtailment_rate_pct),
        })
    return params, warnings, errors


def _zero_battery_opt_result(baseline, gen_shape):
    """容量0のときの opt_result 互換dict（run_simulationと同じ流儀）。"""
    zero = np.zeros(gen_shape)
    return {
        "annual_export": baseline["annual_export"],
        "annual_charge": 0.0,
        "annual_discharge": 0.0,
        "annual_curtail": baseline["annual_curtail"],
        "annual_curtail_forced": baseline.get("annual_curtail_forced", baseline["annual_curtail"]),
        "annual_curtail_economic": baseline.get("annual_curtail_economic", 0.0),
        "annual_revenue": baseline["annual_revenue"],
        "curtailment": baseline["curtailment"],
        "battery_charge": zero,
        "battery_discharge": zero,
    }


def _run_fip_simulation(case: str, p: dict):
    """検証済みパラメータ p で ケースA/B のシミュレーションを実行し、構造化dictを返す。"""
    app = _get_app()
    lat, lon, ghi_df, temp_df, _ = _resolve_station(p["station_no"])

    faces = [{
        "ppeak": p["ppeak_kw"],
        "orientation": "南",  # azimuth直接指定が優先されるためプレースホルダ
        "azimuth": p["azimuth_deg"],
        "tilt": p["tilt_deg"],
        "pcs_limit_kw": p["pcs_limit_kw"],
    }]

    result = app.calculate_generation(
        lat, lon, ghi_df, temp_df, faces,
        app.DEFAULT_KHD, app.DEFAULT_KPD, app.DEFAULT_KPM,
        app.DEFAULT_KPA, app.DEFAULT_ETA_INO,
        app.DEFAULT_ALPHA, app.DEFAULT_DELTA_T,
    )
    gen = result["total_gen_clipped"]

    jepx_prices = app.load_jepx_prices(
        p["jepx_area"], p["jepx_fiscal_years"], result["month_day"]
    )
    curtail_prob = app.build_curtail_prob_30min(
        result["month_day"], p["annual_curtailment_rate_pct"], generation_30min=gen
    )

    # 参照価格 = 選択エリア・年度のJEPX単純平均。実効プレミアム = 基準価格 − 参照価格。
    # ユーザー（エージェント）は公表済みの「基準価格」をそのまま渡せばよく、
    # 市場への上乗せ額の計算はここで行う（基準価格をそのままプレミアムにする誤りを防ぐ）。
    # ゼロ下限クリップ: 参照価格が基準価格を上回ってもプレミアムはゼロ止まり
    # （FIP制度に事業者から差額を徴収する仕組みは存在しないため）。
    reference_price = float(np.mean(jepx_prices))
    premium_raw = p["fip_base_price_yen_per_kwh"] - reference_price
    premium_effective = max(0.0, premium_raw)
    premium_clipped = premium_raw < 0
    p["reference_price_yen_per_kwh"] = round(reference_price, 2)
    p["premium_effective_yen_per_kwh"] = round(premium_effective, 2)
    p["premium_clipped_at_zero"] = premium_clipped

    baseline = app.baseline_no_battery(
        gen, jepx_prices, result["month_day"],
        premium=premium_effective,
        nonfossil_price=p["nonfossil_price_yen_per_kwh"],
        bg_fee=p["bg_fee_yen_per_kwh"],
        curtail_prob=curtail_prob,
    )
    baseline_no_prem_rev = (
        baseline["annual_revenue"]
        - baseline["annual_export"] * premium_effective
    )

    if p["battery_capacity_kwh"] > 0:
        opt = app.optimize_battery_fip(
            gen, jepx_prices, result["month_day"],
            capacity_kwh=p["battery_capacity_kwh"],
            max_charge_kw=p["battery_max_charge_kw"],
            max_discharge_kw=p["battery_max_discharge_kw"],
            eff_charge_pct=p["battery_charge_efficiency_pct"],
            eff_discharge_pct=p["battery_discharge_efficiency_pct"],
            soc_min_pct=p["battery_soc_min_pct"],
            soc_max_pct=p["battery_soc_max_pct"],
            premium=premium_effective,
            nonfossil_price=p["nonfossil_price_yen_per_kwh"],
            bg_fee=p["bg_fee_yen_per_kwh"],
            curtail_prob=curtail_prob,
        )
    else:
        opt = _zero_battery_opt_result(baseline, gen.shape)

    opt_no_prem_rev = (
        opt["annual_revenue"] - opt["annual_export"] * premium_effective
    )

    pv_capex_gross = p["ppeak_kw"] * p["pv_cost_yen_per_kw"]
    bat_capex = p["battery_capacity_kwh"] * p["battery_cost_yen_per_kwh"]
    is_case_b = (case == "B")
    pv_capex_for_irr = 0.0 if is_case_b else pv_capex_gross
    subsidy_pv = 0.0 if is_case_b else pv_capex_gross * p["subsidy_pv_pct"] / 100.0
    subsidy_bat = bat_capex * p["subsidy_battery_pct"] / 100.0

    cf_result = app.build_cashflow(
        pv_capex=pv_capex_for_irr, bat_capex=bat_capex,
        subsidy_pv=subsidy_pv, subsidy_bat=subsidy_bat,
        annual_revenue_with_bat=opt["annual_revenue"],
        annual_revenue_without_bat=baseline["annual_revenue"],
        om_ratio_pct=p["om_ratio_pct_per_year"],
        equity_ratio_pct=p["equity_ratio_pct"],
        loan_interest_pct=p["loan_interest_pct"],
        loan_years=p["loan_years"],
        irr_period_years=p["irr_period_years"],
        bat_life_years=p["battery_life_years"],
        bat_degrade_pct_per_year=p["battery_degrade_pct_per_year"],
        bat_eol_action=_EOL_MAP[p["battery_eol_action"]],
        bat_replace_cost_ratio_pct=p["battery_replace_cost_ratio_pct"],
        bat_om_mode=p["battery_om_mode"],
        om_bat_per_kw_pcs=p["battery_om_yen_per_kw_pcs_per_year"],
        bat_max_charge_kw=p["battery_max_charge_kw"],
        decom_pct=p["decommission_pct"],
        fip_premium_years=p["effective_premium_years"],
        annual_revenue_with_bat_no_premium=opt_no_prem_rev,
        annual_revenue_without_bat_no_premium=baseline_no_prem_rev,
        pv_degrade_pct_per_year=p.get("pv_degrade_pct_per_year", 0.0),
        pv_start_age_years=p.get("pv_start_age_years", 0),
    )

    caseb_comparison = None
    if is_case_b:
        fit_remaining = p["effective_premium_years"]
        fit_cf = app.build_fit_continuation_cashflow(
            annual_gen_kwh=float(result["annual"]),
            fit_tariff=p["fit_tariff_yen_per_kwh"],
            annual_revenue_jepx_direct_no_premium=baseline_no_prem_rev,
            fit_remaining_years=fit_remaining,
            irr_period_years=p["irr_period_years"],
            pv_capex_sunk=p["pv_acquisition_cost_yen"],
            om_ratio_pct=p["om_ratio_pct_per_year"],
            fit_curtailment_rate_pct=p.get("fit_curtailment_rate_pct", 0.0),
            pv_degrade_pct_per_year=p.get("pv_degrade_pct_per_year", 0.0),
            pv_start_age_years=p.get("pv_start_age_years", 0),
        )
        app.apply_caseb_incremental_metrics(cf_result, fit_cf, p["loan_interest_pct"])

        fip_op_cum = sum(r["project_cf"] for r in cf_result["rows"][1:])
        fit_op_cum = sum(r["revenue"] for r in fit_cf["rows"][1:])
        caseb_comparison = {
            "fit_continuation_operating_cf_yen": round(fit_op_cum),
            "fip_with_battery_operating_cf_yen": round(fip_op_cum),
            "difference_yen": round(fip_op_cum - fit_op_cum),
            "battery_net_capex_yen": round(bat_capex - subsidy_bat),
            "net_gain_over_period_yen": round(fip_op_cum - fit_op_cum - bat_capex + subsidy_bat),
            "note": "運営CFはPV O&Mを両シナリオ共通として除外した比較。IRR/NPV/回収年数は増分CFベース",
        }

    # --- 構造化出力 ---
    # 抑制は「強制出力制御」と「経済的自主抑制」で意味がまったく異なるため分けて報告する。
    # 蓄電池の効果（回避量）は強制出力制御分のみで評価する（自主抑制は蓄電池の価値ではない）。
    base_forced = baseline.get("annual_curtail_forced", baseline["annual_curtail"])
    base_econ = baseline.get("annual_curtail_economic", 0.0)
    opt_forced = opt.get("annual_curtail_forced", float(np.sum(opt["curtailment"])))
    opt_econ = opt.get("annual_curtail_economic", 0.0)
    avoided = base_forced - opt_forced
    cashflow_rows = []
    for r in cf_result["rows"]:
        row_out = {
            "year": r["year"],
            "revenue_yen": round(r["revenue"]),
            "om_yen": round(r["om"]),
            "debt_service_yen": round(r["debt_service"]),
            "battery_replace_yen": round(r.get("battery_replace", 0.0)),
            "decommission_yen": round(r.get("decom_cost", 0.0)),
            "project_cf_yen": round(r["project_cf"]),
            "net_cf_yen": round(r["net_cf"]),
            "cum_project_cf_yen": round(r["cum_project"]),
        }
        if "incremental_project_cf" in r:
            row_out["incremental_project_cf_yen"] = round(r["incremental_project_cf"])
        cashflow_rows.append(row_out)

    # 捕捉価格（capture price）: PVが実際に売電できているJEPX加重平均単価
    # （プレミアム・非化石証書等を含まない、JEPX市場価格のみの捕捉状況）。
    # PVの発電時間帯が市場価格の安い時間帯に偏っていると avg_jepx_price より低くなる。
    avg_jepx_price = float(np.mean(jepx_prices))
    export_for_capture = baseline["export"]
    export_sum = float(np.sum(export_for_capture))
    if export_sum > 0:
        capture_price = float(np.sum(export_for_capture * jepx_prices)) / export_sum
        capture_rate_pct = capture_price / avg_jepx_price * 100.0 if avg_jepx_price != 0 else None
    else:
        capture_price = None
        capture_rate_pct = None

    irr = cf_result["project_irr"]
    caveats = list(_COMMON_CAVEATS)
    if p.get("premium_clipped_at_zero"):
        caveats.append(
            f"参照価格{p['reference_price_yen_per_kwh']:.2f}円/kWhが"
            f"基準価格{p['fip_base_price_yen_per_kwh']:.2f}円/kWhを上回るため、"
            "プレミアムを0円/kWhに制限しました（FIP制度上、参照価格超過分を事業者から"
            "徴収する仕組みは存在しません）。この条件ではFIPプレミアムによる収益上乗せは発生しません"
        )
    else:
        caveats.append(
            f"実効プレミアム{p['premium_effective_yen_per_kwh']:+.2f}円/kWhは、"
            f"基準価格{p['fip_base_price_yen_per_kwh']:.2f}円 − 参照価格{p['reference_price_yen_per_kwh']:.2f}円"
            "（選択年度JEPX単純平均から自動算定）で計算しています"
        )
    if is_case_b:
        caveats.append("ケースBのIRR/NPV/回収年数は「FIP転+蓄電池 − FIT継続」の増分CFで評価しています")
    pv_deg = p.get("pv_degrade_pct_per_year", 0.0)
    if pv_deg > 0:
        age_note = (f"（FIP転時点のPV既経過年数{p['pv_start_age_years']}年を起点に含む）"
                    if p.get("pv_start_age_years", 0) > 0 else "")
        caveats.append(
            f"PV発電量は年{pv_deg:.2f}%の線形劣化を織り込んでキャッシュフローを算出しています{age_note}"
        )

    return {
        "assumptions": p,
        "kpis": {
            "project_irr_pct": round(irr * 100, 2) if irr is not None else None,
            "project_npv_yen": round(cf_result["project_npv"]),
            "discount_rate_pct": p["loan_interest_pct"],
            "payback_years": cf_result["payback_year"],
            "net_capex_yen": round(cf_result["net_capex"]),
            "annual_revenue_with_battery_yen": round(opt["annual_revenue"]),
            "annual_revenue_without_battery_yen": round(baseline["annual_revenue"]),
            "battery_annual_increment_yen": round(opt["annual_revenue"] - baseline["annual_revenue"]),
            "discount_rate_note": "project_npv_yen は discount_rate_pct（借入金利をWACC近似として"
                                  "流用）で割り引いています。loan_interest_pct を変更すると、"
                                  "借入返済負担とNPV割引率の両方が連動して変わります",
        },
        "annual": {
            "generation_kwh": round(result["annual"]),
            "capacity_factor_pct": round(result["annual"] / (p["ppeak_kw"] * 8760) * 100, 2),
            "export_with_battery_kwh": round(opt["annual_export"]),
            "export_without_battery_kwh": round(baseline["annual_export"]),
            "battery_charge_kwh": round(opt["annual_charge"]),
            "battery_discharge_kwh": round(opt["annual_discharge"]),
            "curtailed_without_battery_kwh": round(baseline["annual_curtail"]),
            "curtailed_with_battery_kwh": round(float(np.sum(opt["curtailment"]))),
            # 強制出力制御（系統指令）— 蓄電池が回避できるのはこちらのみ
            "forced_curtailed_without_battery_kwh": round(base_forced),
            "forced_curtailed_with_battery_kwh": round(opt_forced),
            "curtailment_avoided_kwh": round(avoided),
            # 経済的自主抑制（売電単価が負のコマで出力を絞った量）— 蓄電池の価値ではない
            "economic_curtailed_without_battery_kwh": round(base_econ),
            "economic_curtailed_with_battery_kwh": round(opt_econ),
            "avg_jepx_price_yen_per_kwh": round(avg_jepx_price, 2),
            "capture_price_yen_per_kwh": round(capture_price, 2) if capture_price is not None else None,
            "capture_rate_pct": round(capture_rate_pct, 1) if capture_rate_pct is not None else None,
            "curtailment_note": "curtailment_avoided_kwh は強制出力制御分のみの回避量。"
                                "経済的自主抑制は売電すると損失になるコマで出力を絞った量であり、"
                                "蓄電池の有無にかかわらず発生する（意味が異なるため合算しない）",
            "capture_price_note": "capture_price_yen_per_kwh はPV（蓄電池なし）が実際に捕捉している"
                                  "JEPX市場価格の加重平均（プレミアム等を含まない）。"
                                  "avg_jepx_price比のcapture_rate_pctが低いほど、PVの発電時間帯と"
                                  "市場価格の高い時間帯がズレていることを示す",
        },
        "cashflow": cashflow_rows,
        "caseb_comparison": caseb_comparison,
        "caveats": caveats,
    }


# ============================================================
# 公開MCPツール
# ============================================================

def list_stations() -> dict:
    """気象観測地点（NEDO METPV-20、DB収録の全地点）の一覧を返す。

    シミュレーションの station_no 引数にはここで返る地点番号を使用する。

    Returns:
        dict: {"stations": [{"station_no", "name", "latitude", "longitude"}, ...]}
    """
    app = _get_app()
    conn = sqlite3.connect(app.DB_PATH)
    rows = conn.execute(
        "SELECT point_no, point_name, lat, lon FROM points ORDER BY point_no"
    ).fetchall()
    conn.close()
    return {
        "stations": [
            {"station_no": str(no), "name": name,
             "latitude": float(lat), "longitude": float(lon)}
            for no, name, lat, lon in rows
        ],
        "count": len(rows),
    }


def get_jepx_stats(area: str = "東京", fiscal_years: list[int] = [2023, 2024, 2025]) -> dict:
    """JEPXスポット市場価格の統計を返す（LP計算なし・即答）。

    エリア・年度を指定すると、年平均価格・時間帯別平均・日次スプレッド
    （蓄電池アービトラージの原資）などを集計する。

    Args:
        area: JEPXエリア名。北海道/東北/東京/中部/北陸/関西/中国/四国/九州 のいずれか
        fiscal_years: 対象年度のリスト（2020〜2025）。複数指定時は同一コマの平均価格

    Returns:
        dict: 価格統計（円/kWh）。slot_avg_yen_per_kwh は48コマ（30分刻み）の平均価格
    """
    app = _get_app()
    if area not in app.JEPX_AREAS:
        return {"error": f"area は {app.JEPX_AREAS} から選択してください"}
    years = [int(y) for y in (fiscal_years or [])]
    bad = [y for y in years if y not in app.JEPX_FISCAL_YEARS]
    if not years or bad:
        return {"error": f"fiscal_years は {app.JEPX_FISCAL_YEARS} の中から1つ以上指定してください"}

    conn = sqlite3.connect(app.JEPX_DB_PATH)
    ph = ",".join(["?"] * len(years))
    rows = conn.execute(
        f"SELECT date, slot, AVG(price) FROM jepx_prices "
        f"WHERE area = ? AND fiscal_year IN ({ph}) GROUP BY date, slot",
        [area] + years,
    ).fetchall()
    conn.close()

    by_date = {}
    slot_sum = np.zeros(48)
    slot_cnt = np.zeros(48)
    all_prices = []
    for date_str, slot, price in rows:
        by_date.setdefault(date_str, []).append(float(price))
        slot_sum[int(slot) - 1] += float(price)
        slot_cnt[int(slot) - 1] += 1
        all_prices.append(float(price))

    arr = np.array(all_prices)
    spreads = np.array([max(v) - min(v) for v in by_date.values()])
    slot_avg = np.divide(slot_sum, np.maximum(slot_cnt, 1))

    return {
        "area": area,
        "fiscal_years": years,
        "annual_avg_yen_per_kwh": round(float(arr.mean()), 2),
        "p10_yen_per_kwh": round(float(np.percentile(arr, 10)), 2),
        "p90_yen_per_kwh": round(float(np.percentile(arr, 90)), 2),
        "min_yen_per_kwh": round(float(arr.min()), 2),
        "max_yen_per_kwh": round(float(arr.max()), 2),
        "avg_daily_spread_yen_per_kwh": round(float(spreads.mean()), 2),
        "max_daily_spread_yen_per_kwh": round(float(spreads.max()), 2),
        "near_zero_slots_ratio_pct": round(float((arr <= 0.5).mean() * 100), 2),
        "slot_avg_yen_per_kwh": [round(float(v), 2) for v in slot_avg],
        "note": "slot_avg は slot1=0:00-0:30 〜 slot48=23:30-24:00。"
                "avg_daily_spread が蓄電池アービトラージ1kWhあたり原資の目安",
    }


def estimate_pv_generation(
    station_no: str = "44132",
    ppeak_kw: float = 1000.0,
    tilt_deg: float = 20.0,
    azimuth_deg: float = 180.0,
    pcs_limit_kw: float = 0.0,
) -> dict:
    """太陽光発電量のみを試算する（JIS C 8907準拠、LP最適化なし、数秒〜十数秒）。

    蓄電池・経済性を含まない発電量の当たり付けに使用する。

    Args:
        station_no: 地点番号（list_stations で取得。例 "44132"=東京, "82182"=福岡）
        ppeak_kw: 太陽光パネル容量 [kW]
        tilt_deg: 傾斜角 [度]（0=水平〜90=垂直）
        azimuth_deg: 方位角 [度]（北=0, 東=90, 南=180, 西=270）
        pcs_limit_kw: PCS出力制限 [kW]（0=制限なし）

    Returns:
        dict: 年間発電量・月別発電量・設備利用率
    """
    app = _get_app()
    try:
        if not (0 < ppeak_kw <= MAX_PPEAK_KW):
            return {"error": f"ppeak_kw は 0 < x <= {MAX_PPEAK_KW:.0f}"}
        lat, lon, ghi_df, temp_df, name = _resolve_station(station_no)
        faces = [{
            "ppeak": float(ppeak_kw), "orientation": "南",
            "azimuth": float(azimuth_deg) % 360, "tilt": float(tilt_deg),
            "pcs_limit_kw": float(pcs_limit_kw) if pcs_limit_kw > 0 else None,
        }]
        g = app.calculate_generation(
            lat, lon, ghi_df, temp_df, faces,
            app.DEFAULT_KHD, app.DEFAULT_KPD, app.DEFAULT_KPM,
            app.DEFAULT_KPA, app.DEFAULT_ETA_INO,
            app.DEFAULT_ALPHA, app.DEFAULT_DELTA_T,
        )
        return {
            "station_no": str(station_no),
            "station_name": name,
            "ppeak_kw": float(ppeak_kw),
            "annual_generation_kwh": round(g["annual"]),
            "capacity_factor_pct": round(g["annual"] / (ppeak_kw * 8760) * 100, 2),
            "monthly_generation_kwh": {str(m): round(g["monthly"].get(m, 0)) for m in range(1, 13)},
            "note": "JIS C 8907準拠（標準補正係数使用）。蓄電池・出力制御・経済性は含まない",
        }
    except Exception as e:
        return {"error": str(e)}


def validate_fip_params(
    case: str = "A",
    station_no: str = "44132",
    ppeak_kw: float = 1000.0,
    tilt_deg: float = 20.0,
    azimuth_deg: float = 180.0,
    pcs_limit_kw: float = 0.0,
    battery_capacity_kwh: float = 1000.0,
    battery_max_charge_kw: float = 500.0,
    battery_max_discharge_kw: float = 500.0,
    battery_charge_efficiency_pct: float = 95.0,
    battery_discharge_efficiency_pct: float = 95.0,
    battery_soc_min_pct: float = 10.0,
    battery_soc_max_pct: float = 90.0,
    battery_life_years: int = 20,
    battery_degrade_pct_per_year: float = 1.0,
    battery_eol_action: str = "replace",
    battery_replace_cost_ratio_pct: float = 60.0,
    jepx_area: str = "東京",
    jepx_fiscal_years: list[int] = [2023, 2024, 2025],
    fip_base_price_yen_per_kwh: float = 9.6,
    nonfossil_price_yen_per_kwh: float = 0.6,
    bg_fee_yen_per_kwh: float = 2.0,
    fip_premium_years: int = 20,
    annual_curtailment_rate_pct: float = 0.0,
    pv_cost_yen_per_kw: float = 155600.0,
    battery_cost_yen_per_kwh: float = 68000.0,
    subsidy_pv_pct: float = 0.0,
    subsidy_battery_pct: float = 0.0,
    om_ratio_pct_per_year: float = 1.5,
    battery_om_yen_per_kw_pcs_per_year: float = 5000.0,
    decommission_pct: float = 5.0,
    equity_ratio_pct: float = 30.0,
    loan_interest_pct: float = 2.0,
    loan_years: int = 15,
    irr_period_years: int = 20,
    fit_tariff_yen_per_kwh: float = 36.0,
    fit_term_years: int = 20,
    fip_transition_year: int = 12,
    pv_acquisition_cost_yen: float = 0.0,
    fit_curtailment_rate_pct: float = 0.0,
    pv_degrade_pct_per_year: float = 0.5,
) -> dict:
    """FIP事業性シミュレーションのパラメータを検証する（即答・LP実行なし）。

    **simulate_fip_case_a / simulate_fip_case_b を呼ぶ前に必ずこのツールで検証し、
    返ってきた normalized_params をユーザーに提示して確認を得てから実行すること。**

    Args:
        case: "A"=新規FIP（PV+蓄電池を新設） / "B"=既存FIT発電所をFIP転して蓄電池後付け
        station_no: 地点番号（list_stations で取得）
        ppeak_kw: 太陽光パネル容量 [kW]
        tilt_deg: 傾斜角 [度]
        azimuth_deg: 方位角 [度]（北=0, 東=90, 南=180, 西=270）
        pcs_limit_kw: PV側PCS出力制限 [kW]（0=制限なし）
        battery_capacity_kwh: 蓄電池容量 [kWh]（0=蓄電池なし）
        battery_max_charge_kw: 蓄電池最大充電電力 [kW]
        battery_max_discharge_kw: 蓄電池最大放電電力 [kW]
        battery_charge_efficiency_pct: 充電効率 [%]
        battery_discharge_efficiency_pct: 放電効率 [%]
        battery_soc_min_pct: SOC下限 [%]
        battery_soc_max_pct: SOC上限 [%]
        battery_life_years: 蓄電池寿命 [年]
        battery_degrade_pct_per_year: 年間劣化率 [%/年]
        battery_eol_action: 寿命到来時 "replace"=交換再投資 / "end"=蓄電池なしで継続
        battery_replace_cost_ratio_pct: 交換時単価の当初比 [%]
        jepx_area: JEPXエリア（北海道/東北/東京/中部/北陸/関西/中国/四国/九州）
        jepx_fiscal_years: JEPX価格年度リスト（2020〜2025、複数年平均）
        fip_base_price_yen_per_kwh: FIP基準価格 [円/kWh]（公表値をそのまま指定。参照価格は自動算定）
        nonfossil_price_yen_per_kwh: 非化石証書単価 [円/kWh]
        bg_fee_yen_per_kwh: バランシンググループ手数料 [円/kWh]
        fip_premium_years: プレミアム交付期間 [年]（ケースAのみ。ケースBはFIT残存期間で自動計算）
        annual_curtailment_rate_pct: 年間出力制御率 [%]
        pv_cost_yen_per_kw: PVシステム単価 [円/kW]
        battery_cost_yen_per_kwh: 蓄電池単価 [円/kWh]
        subsidy_pv_pct: PV補助率 [%]
        subsidy_battery_pct: 蓄電池補助率 [%]
        om_ratio_pct_per_year: O&M費率 [%/年]（CAPEX比）
        battery_om_yen_per_kw_pcs_per_year: 蓄電池O&M [円/kW(PCS)/年]
        decommission_pct: 廃止措置費用 [補助前CAPEX×%、最終年計上]
        equity_ratio_pct: 自己資本比率 [%]
        loan_interest_pct: 借入金利 [%/年]
        loan_years: 借入期間 [年]
        irr_period_years: IRR計算期間 [年]
        fit_tariff_yen_per_kwh: FIT単価 [円/kWh]（ケースBのみ）
        fit_term_years: FIT契約期間 [年]（ケースBのみ、通常20）
        fip_transition_year: FIP転実施年（FIT開始から何年目か。ケースBのみ）
        pv_acquisition_cost_yen: PV取得価額 [円]（ケースB、ライフサイクルCF表示用・任意）
        fit_curtailment_rate_pct: FIT期間中の出力制御率 [%]（ケースBのみ。「FIP転しない場合」の
            比較シナリオに適用。デフォルト0=FIT優先で制御されない前提）
        pv_degrade_pct_per_year: PV年間劣化率 [%/年]（線形、結晶シリコン一般値の目安0.5）。
            ケースBはFIP転時点のPV既経過年数（fip_transition_year-1）を劣化計算の起点に含める

    Returns:
        dict: {"valid": bool, "normalized_params": {...}, "warnings": [...], "errors": [...]}
              normalized_params には reference_price_preview_yen_per_kwh（選択年度JEPX
              単純平均の概算）と premium_preview_yen_per_kwh（基準価格−参照価格の概算）
              を含む。simulate実行前にユーザーへ提示し、実効プレミアムの大きさを確認すること
    """
    try:
        case = str(case).upper()
        if case not in ("A", "B"):
            return {"valid": False, "errors": ['case は "A" または "B" を指定してください'], "warnings": []}
        params, warnings, errors = _normalize_and_validate(
            case, station_no, ppeak_kw, tilt_deg, azimuth_deg, pcs_limit_kw,
            battery_capacity_kwh, battery_max_charge_kw, battery_max_discharge_kw,
            battery_charge_efficiency_pct, battery_discharge_efficiency_pct,
            battery_soc_min_pct, battery_soc_max_pct,
            battery_life_years, battery_degrade_pct_per_year,
            battery_eol_action, battery_replace_cost_ratio_pct,
            jepx_area, jepx_fiscal_years,
            fip_base_price_yen_per_kwh, nonfossil_price_yen_per_kwh, bg_fee_yen_per_kwh,
            fip_premium_years, annual_curtailment_rate_pct,
            pv_cost_yen_per_kw, battery_cost_yen_per_kwh,
            subsidy_pv_pct, subsidy_battery_pct,
            om_ratio_pct_per_year, battery_om_yen_per_kw_pcs_per_year,
            decommission_pct, equity_ratio_pct,
            loan_interest_pct, loan_years, irr_period_years,
            fit_tariff_yen_per_kwh, fit_term_years, fip_transition_year,
            pv_acquisition_cost_yen, fit_curtailment_rate_pct,
            pv_degrade_pct_per_year,
        )
        return {
            "valid": len(errors) == 0,
            "normalized_params": params,
            "warnings": warnings,
            "errors": errors,
            "estimated_runtime_seconds": "30-90（LP最適化を含むため）",
            "next_step": "normalized_params をユーザーに提示して確認後、"
                         "simulate_fip_case_a または simulate_fip_case_b を同じ引数で呼び出す",
        }
    except Exception as e:
        return {"valid": False, "errors": [str(e)], "warnings": []}


def simulate_fip_case_a(
    station_no: str = "44132",
    ppeak_kw: float = 1000.0,
    tilt_deg: float = 20.0,
    azimuth_deg: float = 180.0,
    pcs_limit_kw: float = 0.0,
    battery_capacity_kwh: float = 1000.0,
    battery_max_charge_kw: float = 500.0,
    battery_max_discharge_kw: float = 500.0,
    battery_charge_efficiency_pct: float = 95.0,
    battery_discharge_efficiency_pct: float = 95.0,
    battery_soc_min_pct: float = 10.0,
    battery_soc_max_pct: float = 90.0,
    battery_life_years: int = 20,
    battery_degrade_pct_per_year: float = 1.0,
    battery_eol_action: str = "replace",
    battery_replace_cost_ratio_pct: float = 60.0,
    jepx_area: str = "東京",
    jepx_fiscal_years: list[int] = [2023, 2024, 2025],
    fip_base_price_yen_per_kwh: float = 9.6,
    nonfossil_price_yen_per_kwh: float = 0.6,
    bg_fee_yen_per_kwh: float = 2.0,
    fip_premium_years: int = 20,
    annual_curtailment_rate_pct: float = 0.0,
    pv_cost_yen_per_kw: float = 155600.0,
    battery_cost_yen_per_kwh: float = 68000.0,
    subsidy_pv_pct: float = 0.0,
    subsidy_battery_pct: float = 0.0,
    om_ratio_pct_per_year: float = 1.5,
    battery_om_yen_per_kw_pcs_per_year: float = 5000.0,
    decommission_pct: float = 5.0,
    equity_ratio_pct: float = 30.0,
    loan_interest_pct: float = 2.0,
    loan_years: int = 15,
    irr_period_years: int = 20,
    pv_degrade_pct_per_year: float = 0.5,
) -> dict:
    """ケースA: 新規FIP発電所＋蓄電池の事業性を試算する（実行30〜90秒、LP最適化を含む）。

    太陽光＋蓄電池を新設して最初からFIP認定を受けるケース。
    JEPXスポット市場への最適売電（蓄電池アービトラージ）をLPで解き、
    Project IRR / NPV / 投資回収年数を返す。

    **事前に validate_fip_params（case="A"）で検証し、パラメータをユーザーに
    確認してから呼び出すこと。** 引数の意味は validate_fip_params と同一。

    Args:
        station_no: 地点番号（list_stations で取得）
        ppeak_kw: 太陽光パネル容量 [kW]
        tilt_deg: 傾斜角 [度]
        azimuth_deg: 方位角 [度]（北=0, 東=90, 南=180, 西=270）
        pcs_limit_kw: PV側PCS出力制限 [kW]（0=制限なし）
        battery_capacity_kwh: 蓄電池容量 [kWh]（0=蓄電池なし）
        battery_max_charge_kw: 蓄電池最大充電電力 [kW]
        battery_max_discharge_kw: 蓄電池最大放電電力 [kW]
        battery_charge_efficiency_pct: 充電効率 [%]
        battery_discharge_efficiency_pct: 放電効率 [%]
        battery_soc_min_pct: SOC下限 [%]
        battery_soc_max_pct: SOC上限 [%]
        battery_life_years: 蓄電池寿命 [年]
        battery_degrade_pct_per_year: 年間劣化率 [%/年]
        battery_eol_action: 寿命到来時 "replace"=交換再投資 / "end"=蓄電池なしで継続
        battery_replace_cost_ratio_pct: 交換時単価の当初比 [%]
        jepx_area: JEPXエリア名
        jepx_fiscal_years: JEPX価格年度リスト（複数年平均）
        fip_base_price_yen_per_kwh: FIP基準価格 [円/kWh]（公表値をそのまま指定。参照価格は自動算定）
        nonfossil_price_yen_per_kwh: 非化石証書単価 [円/kWh]
        bg_fee_yen_per_kwh: バランシンググループ手数料 [円/kWh]
        fip_premium_years: プレミアム交付期間 [年]
        annual_curtailment_rate_pct: 年間出力制御率 [%]
        pv_cost_yen_per_kw: PVシステム単価 [円/kW]
        battery_cost_yen_per_kwh: 蓄電池単価 [円/kWh]
        subsidy_pv_pct: PV補助率 [%]
        subsidy_battery_pct: 蓄電池補助率 [%]
        om_ratio_pct_per_year: O&M費率 [%/年]（CAPEX比）
        battery_om_yen_per_kw_pcs_per_year: 蓄電池O&M [円/kW(PCS)/年]
        decommission_pct: 廃止措置費用 [補助前CAPEX×%]
        equity_ratio_pct: 自己資本比率 [%]
        loan_interest_pct: 借入金利 [%/年]
        loan_years: 借入期間 [年]
        irr_period_years: IRR計算期間 [年]
        pv_degrade_pct_per_year: PV年間劣化率 [%/年]（線形、結晶シリコン一般値の目安0.5）

    Returns:
        dict: assumptions（入力エコー）/ kpis（IRR・NPV・回収年数）/
              annual（発電・売電・充放電量）/ cashflow（年次CF）/ caveats（免責事項）
    """
    v = validate_fip_params(
        case="A", station_no=station_no, ppeak_kw=ppeak_kw,
        tilt_deg=tilt_deg, azimuth_deg=azimuth_deg, pcs_limit_kw=pcs_limit_kw,
        battery_capacity_kwh=battery_capacity_kwh,
        battery_max_charge_kw=battery_max_charge_kw,
        battery_max_discharge_kw=battery_max_discharge_kw,
        battery_charge_efficiency_pct=battery_charge_efficiency_pct,
        battery_discharge_efficiency_pct=battery_discharge_efficiency_pct,
        battery_soc_min_pct=battery_soc_min_pct,
        battery_soc_max_pct=battery_soc_max_pct,
        battery_life_years=battery_life_years,
        battery_degrade_pct_per_year=battery_degrade_pct_per_year,
        battery_eol_action=battery_eol_action,
        battery_replace_cost_ratio_pct=battery_replace_cost_ratio_pct,
        jepx_area=jepx_area, jepx_fiscal_years=jepx_fiscal_years,
        fip_base_price_yen_per_kwh=fip_base_price_yen_per_kwh,
        nonfossil_price_yen_per_kwh=nonfossil_price_yen_per_kwh,
        bg_fee_yen_per_kwh=bg_fee_yen_per_kwh,
        fip_premium_years=fip_premium_years,
        annual_curtailment_rate_pct=annual_curtailment_rate_pct,
        pv_cost_yen_per_kw=pv_cost_yen_per_kw,
        battery_cost_yen_per_kwh=battery_cost_yen_per_kwh,
        subsidy_pv_pct=subsidy_pv_pct, subsidy_battery_pct=subsidy_battery_pct,
        om_ratio_pct_per_year=om_ratio_pct_per_year,
        battery_om_yen_per_kw_pcs_per_year=battery_om_yen_per_kw_pcs_per_year,
        decommission_pct=decommission_pct, equity_ratio_pct=equity_ratio_pct,
        loan_interest_pct=loan_interest_pct, loan_years=loan_years,
        irr_period_years=irr_period_years,
        pv_degrade_pct_per_year=pv_degrade_pct_per_year,
    )
    if not v.get("valid"):
        return {"error": "パラメータ検証エラー", "errors": v.get("errors", []),
                "warnings": v.get("warnings", [])}
    try:
        out = _run_fip_simulation("A", v["normalized_params"])
        out["validation_warnings"] = v.get("warnings", [])
        return out
    except Exception as e:
        return {"error": str(e)}


def simulate_fip_case_b(
    station_no: str = "44132",
    ppeak_kw: float = 1000.0,
    tilt_deg: float = 20.0,
    azimuth_deg: float = 180.0,
    pcs_limit_kw: float = 0.0,
    battery_capacity_kwh: float = 1000.0,
    battery_max_charge_kw: float = 500.0,
    battery_max_discharge_kw: float = 500.0,
    battery_charge_efficiency_pct: float = 95.0,
    battery_discharge_efficiency_pct: float = 95.0,
    battery_soc_min_pct: float = 10.0,
    battery_soc_max_pct: float = 90.0,
    battery_life_years: int = 20,
    battery_degrade_pct_per_year: float = 1.0,
    battery_eol_action: str = "replace",
    battery_replace_cost_ratio_pct: float = 60.0,
    jepx_area: str = "東京",
    jepx_fiscal_years: list[int] = [2023, 2024, 2025],
    fip_base_price_yen_per_kwh: float = 9.6,
    nonfossil_price_yen_per_kwh: float = 0.6,
    bg_fee_yen_per_kwh: float = 2.0,
    annual_curtailment_rate_pct: float = 0.0,
    pv_cost_yen_per_kw: float = 155600.0,
    battery_cost_yen_per_kwh: float = 68000.0,
    subsidy_pv_pct: float = 0.0,
    subsidy_battery_pct: float = 0.0,
    om_ratio_pct_per_year: float = 1.5,
    battery_om_yen_per_kw_pcs_per_year: float = 5000.0,
    decommission_pct: float = 5.0,
    equity_ratio_pct: float = 30.0,
    loan_interest_pct: float = 2.0,
    loan_years: int = 15,
    irr_period_years: int = 20,
    fit_tariff_yen_per_kwh: float = 36.0,
    fit_term_years: int = 20,
    fip_transition_year: int = 12,
    pv_acquisition_cost_yen: float = 0.0,
    fit_curtailment_rate_pct: float = 0.0,
    pv_degrade_pct_per_year: float = 0.5,
) -> dict:
    """ケースB: 既存FIT発電所のFIP転＋蓄電池後付けの事業性を試算する（実行30〜90秒）。

    既設FIT発電所に蓄電池を追加してFIP制度に移行するケース。
    FIPプレミアム交付期間はFIT残存期間（fit_term_years − fip_transition_year + 1）。
    **IRR/NPV/回収年数は「FIP転＋蓄電池」と「FIT継続」の増分キャッシュフローで評価**し、
    PVはサンクコストとして投資額に含めない（蓄電池追加投資に対するリターンを見る）。

    **事前に validate_fip_params（case="B"）で検証し、パラメータをユーザーに
    確認してから呼び出すこと。** 引数の意味は validate_fip_params と同一。

    Args:
        station_no: 地点番号（list_stations で取得）
        ppeak_kw: 太陽光パネル容量 [kW]
        tilt_deg: 傾斜角 [度]
        azimuth_deg: 方位角 [度]（北=0, 東=90, 南=180, 西=270）
        pcs_limit_kw: PV側PCS出力制限 [kW]（0=制限なし）
        battery_capacity_kwh: 蓄電池容量 [kWh]
        battery_max_charge_kw: 蓄電池最大充電電力 [kW]
        battery_max_discharge_kw: 蓄電池最大放電電力 [kW]
        battery_charge_efficiency_pct: 充電効率 [%]
        battery_discharge_efficiency_pct: 放電効率 [%]
        battery_soc_min_pct: SOC下限 [%]
        battery_soc_max_pct: SOC上限 [%]
        battery_life_years: 蓄電池寿命 [年]
        battery_degrade_pct_per_year: 年間劣化率 [%/年]
        battery_eol_action: 寿命到来時 "replace"=交換再投資 / "end"=蓄電池なしで継続
        battery_replace_cost_ratio_pct: 交換時単価の当初比 [%]
        jepx_area: JEPXエリア名
        jepx_fiscal_years: JEPX価格年度リスト（複数年平均）
        fip_base_price_yen_per_kwh: FIP基準価格 [円/kWh]（公表値をそのまま指定。参照価格は自動算定）
        nonfossil_price_yen_per_kwh: 非化石証書単価 [円/kWh]
        bg_fee_yen_per_kwh: バランシンググループ手数料 [円/kWh]
        annual_curtailment_rate_pct: FIP転後の年間出力制御率 [%]
        pv_cost_yen_per_kw: PVシステム単価 [円/kW]（参考表示用）
        battery_cost_yen_per_kwh: 蓄電池単価 [円/kWh]
        subsidy_pv_pct: PV補助率 [%]（ケースBでは未使用）
        subsidy_battery_pct: 蓄電池補助率 [%]
        om_ratio_pct_per_year: O&M費率 [%/年]（CAPEX比）
        battery_om_yen_per_kw_pcs_per_year: 蓄電池O&M [円/kW(PCS)/年]
        decommission_pct: 廃止措置費用 [補助前CAPEX×%]
        equity_ratio_pct: 自己資本比率 [%]
        loan_interest_pct: 借入金利 [%/年]
        loan_years: 借入期間 [年]
        irr_period_years: IRR計算期間 [年]
        fit_tariff_yen_per_kwh: 現行のFIT買取単価 [円/kWh]
        fit_term_years: FIT契約期間 [年]（通常20）
        fip_transition_year: FIP転を実施する年（FIT開始から何年目か）
        pv_acquisition_cost_yen: PV取得価額 [円]（任意、FIT継続比較の参考用）
        fit_curtailment_rate_pct: FIT期間中の出力制御率 [%]（「FIP転しない場合」の
            比較シナリオに適用。デフォルト0=FIT優先で制御されない前提）
        pv_degrade_pct_per_year: PV年間劣化率 [%/年]（線形、結晶シリコン一般値の目安0.5）。
            FIP転時点のPV既経過年数（fip_transition_year-1）を劣化計算の起点に含める

    Returns:
        dict: assumptions / kpis（増分CFベースのIRR・NPV・回収年数）/ annual /
              cashflow / caseb_comparison（FIT継続との20年累計比較）/ caveats
    """
    v = validate_fip_params(
        case="B", station_no=station_no, ppeak_kw=ppeak_kw,
        tilt_deg=tilt_deg, azimuth_deg=azimuth_deg, pcs_limit_kw=pcs_limit_kw,
        battery_capacity_kwh=battery_capacity_kwh,
        battery_max_charge_kw=battery_max_charge_kw,
        battery_max_discharge_kw=battery_max_discharge_kw,
        battery_charge_efficiency_pct=battery_charge_efficiency_pct,
        battery_discharge_efficiency_pct=battery_discharge_efficiency_pct,
        battery_soc_min_pct=battery_soc_min_pct,
        battery_soc_max_pct=battery_soc_max_pct,
        battery_life_years=battery_life_years,
        battery_degrade_pct_per_year=battery_degrade_pct_per_year,
        battery_eol_action=battery_eol_action,
        battery_replace_cost_ratio_pct=battery_replace_cost_ratio_pct,
        jepx_area=jepx_area, jepx_fiscal_years=jepx_fiscal_years,
        fip_base_price_yen_per_kwh=fip_base_price_yen_per_kwh,
        nonfossil_price_yen_per_kwh=nonfossil_price_yen_per_kwh,
        bg_fee_yen_per_kwh=bg_fee_yen_per_kwh,
        annual_curtailment_rate_pct=annual_curtailment_rate_pct,
        pv_cost_yen_per_kw=pv_cost_yen_per_kw,
        battery_cost_yen_per_kwh=battery_cost_yen_per_kwh,
        subsidy_pv_pct=subsidy_pv_pct, subsidy_battery_pct=subsidy_battery_pct,
        om_ratio_pct_per_year=om_ratio_pct_per_year,
        battery_om_yen_per_kw_pcs_per_year=battery_om_yen_per_kw_pcs_per_year,
        decommission_pct=decommission_pct, equity_ratio_pct=equity_ratio_pct,
        loan_interest_pct=loan_interest_pct, loan_years=loan_years,
        irr_period_years=irr_period_years,
        fit_tariff_yen_per_kwh=fit_tariff_yen_per_kwh,
        fit_term_years=fit_term_years,
        fip_transition_year=fip_transition_year,
        pv_acquisition_cost_yen=pv_acquisition_cost_yen,
        fit_curtailment_rate_pct=fit_curtailment_rate_pct,
        pv_degrade_pct_per_year=pv_degrade_pct_per_year,
    )
    if not v.get("valid"):
        return {"error": "パラメータ検証エラー", "errors": v.get("errors", []),
                "warnings": v.get("warnings", [])}
    try:
        out = _run_fip_simulation("B", v["normalized_params"])
        out["validation_warnings"] = v.get("warnings", [])
        return out
    except Exception as e:
        return {"error": str(e)}
