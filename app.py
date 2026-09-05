"""
app.py - FIP転＋蓄電池 事業性シミュレーター（Phase 1）
====================================================
太陽光発電所＋蓄電池のFIP転ビジネス向け事業性シミュレーター。
JEPXスポット価格を活用した蓄電池アービトラージのLP最適化。

Phase 1 機能:
  - SQLite DB（radiation.db）から77地点気象データ読込 / NEDO CSVアップロード
  - 最大8面のアレイ設定（pvlibでPOA変換、JIS C 8907準拠）
  - 両面パネル＋積雪アルベド対応
  - JEPX DB（jepx.db）からエリア・年度別スポット価格読込（複数年度平均対応）
  - 蓄電池LP最適化（目的関数: JEPX売電収入最大化）
  - 蓄電池なしベースラインの自動計算（増分価値の可視化）
  - 月別発電量・売電量グラフ
  - 日別48コマ運用グラフ（PV発電・充放電・売電・SOC・JEPX価格）

姉妹プロジェクト pv-sim-biz から流用:
  - JIS C 8907 発電量計算ロジック
  - radiation.db 読込ロジック
  - pvlib POA変換 / 両面パネル
  - PuLP/CBC LP最適化エンジン（目的関数を差替え）
"""

import os
import csv
import sqlite3
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import gradio as gr

try:
    import pvlib
    HAS_PVLIB = True
except ImportError:
    HAS_PVLIB = False

try:
    import pulp
    HAS_PULP = True
except ImportError:
    HAS_PULP = False


# ============================================================
# 定数・設定
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "radiation.db")
JEPX_DB_PATH = os.path.join(BASE_DIR, "jepx.db")

G_STC = 1.0  # 標準日射強度 [kW/m2]
NO_DATA = 8888

# === JIS C 8907 デフォルト補正係数 ===
DEFAULT_KHD = 0.97
DEFAULT_KPD = 0.95
DEFAULT_KPM = 0.94
DEFAULT_KPA = 0.97
DEFAULT_ETA_INO = 0.90
DEFAULT_ALPHA = -0.35  # %/℃
DEFAULT_DELTA_T = 21.5  # ℃（架台設置形）
DEFAULT_PV_DEGRADE_PCT_PER_YEAR = 0.5  # PV年間劣化率 [%/年]（線形、結晶シリコン一般値）
DEFAULT_ARBITRAGE_REALIZATION_RATE_PCT = 85.0  # 蓄電池アービトラージ実現率 [%]
# LP最適化は1年分のJEPX価格を完全予見する前提の理論上限。実運用は前日予測ベースのため、
# 蓄電池による増分収益（アービトラージ＋出力制御回避）はこの理論値の7〜9割程度に低下する
# とされる（Phase 4設計書 §4 決定事項、デフォルト85%）。

MAX_FACES = 8

# === 方位 → pvlib用アジマス角（北=0, 時計回り） ===
ORIENTATION_TO_AZIMUTH = {
    "南": 180, "南西": 225, "西": 270, "北西": 315,
    "北": 0, "北東": 45, "東": 90, "南東": 135,
}

# === 両面パネルデフォルト値 ===
BIFACIAL_DEFAULTS = {
    "bifaciality": 0.75,    # 背面/前面効率比
    "gcr": 0.4,             # 地面被覆率
    "height": 2.0,          # パネル中心地上高 [m]
    "pitch": 5.0,           # 列間隔 [m]
}
ALBEDO_NORMAL = 0.2   # 通常（草・土）
ALBEDO_SNOW = 0.7     # 積雪時（NEDO METPV-20定義値）

# === 蓄電池デフォルト値（FIP用、産業規模を想定） ===
BATTERY_DEFAULTS = {
    "capacity_kwh": 1000.0,    # 蓄電池容量 [kWh]
    "max_charge_kw": 500.0,    # 最大充電電力 [kW]
    "max_discharge_kw": 500.0, # 最大放電電力 [kW]
    "eff_charge_pct": 95,      # 充電効率 [%]
    "eff_discharge_pct": 95,   # 放電効率 [%]
    "soc_min_pct": 10,         # SOC下限 [%]
    "soc_max_pct": 90,         # SOC上限 [%]
    "degrade_pct_per_year": 1.0,  # 年間劣化率 [%/年]（線形）三菱総研試算準拠 (20年で80%残存)
    "life_years": 20,             # 蓄電池寿命 [年]（三菱総研試算と同じ）
}
BATTERY_END_OF_LIFE_OPTIONS = ["交換（再投資）", "終了（蓄電池なし運用）"]
BATTERY_REPLACE_COST_RATIO_DEFAULT = 60  # 蓄電池交換単価 [%]（当初単価比、将来コストダウン想定）

# === JEPX設定 ===
JEPX_AREAS = ["北海道", "東北", "東京", "中部", "北陸", "関西", "中国", "四国", "九州"]
JEPX_FISCAL_YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
# 2022年度はウクライナ危機による異常価格のためデフォルトでは除外
JEPX_FISCAL_YEARS_DEFAULT = [2023, 2024, 2025]

# === FIP設定 ===
DEFAULT_FIP_BASE_PRICE = 9.6   # FIP基準価格 [円/kWh]（METI公表値。50kW以上・地上設置、2026年度想定）
# 参照価格は選択年度のJEPX単純平均から自動算定し、
#   実効プレミアム = 基準価格 − 参照価格
# を売電単価に加算する。ユーザーが「上乗せ額」ではなく「基準価格」を直接入力できるようにするため。
DEFAULT_NONFOSSIL_PRICE = 0.6  # 非化石証書単価 [円/kWh]
DEFAULT_BG_FEE = 2.0           # BG手数料 [円/kWh]

# === 出力制御プロファイル（CLAUDE.md セクション 4-4） ===
# 月別×時間帯別の制御確率重み（正規化前）
# 制御集中月: 3〜5月、10〜11月、制御集中時間帯: 10:00〜15:00
CURTAIL_PROFILE_WEIGHTS = {
    # 月: {(開始時, 終了時): 重み, ...}
    1:  {(6, 8): 0, (8, 10): 0, (10, 12): 0, (12, 14): 0, (14, 16): 0, (16, 18): 0},
    2:  {(6, 8): 0, (8, 10): 0, (10, 12): 1, (12, 14): 2, (14, 16): 1, (16, 18): 0},
    3:  {(6, 8): 0, (8, 10): 1, (10, 12): 3, (12, 14): 5, (14, 16): 3, (16, 18): 1},
    4:  {(6, 8): 0, (8, 10): 2, (10, 12): 5, (12, 14): 8, (14, 16): 5, (16, 18): 2},
    5:  {(6, 8): 0, (8, 10): 2, (10, 12): 5, (12, 14): 8, (14, 16): 5, (16, 18): 2},
    6:  {(6, 8): 0, (8, 10): 0, (10, 12): 1, (12, 14): 2, (14, 16): 1, (16, 18): 0},
    7:  {(6, 8): 0, (8, 10): 0, (10, 12): 0, (12, 14): 0, (14, 16): 0, (16, 18): 0},
    8:  {(6, 8): 0, (8, 10): 0, (10, 12): 0, (12, 14): 0, (14, 16): 0, (16, 18): 0},
    9:  {(6, 8): 0, (8, 10): 0, (10, 12): 1, (12, 14): 2, (14, 16): 1, (16, 18): 0},
    10: {(6, 8): 0, (8, 10): 1, (10, 12): 3, (12, 14): 5, (14, 16): 3, (16, 18): 1},
    11: {(6, 8): 0, (8, 10): 1, (10, 12): 3, (12, 14): 5, (14, 16): 3, (16, 18): 1},
    12: {(6, 8): 0, (8, 10): 0, (10, 12): 0, (12, 14): 0, (14, 16): 0, (16, 18): 0},
}

# === 経済性デフォルト値 ===
# 出典:
#   PV単価: METI 2026年度想定（地上50kW以上）= システム12.9 + 土地造成1.21 + 接続1.45 = 15.56 万円/kW
#     https://www.meti.go.jp/shingikai/energy_environment/storage_system/pdf/20250307_1.pdf
#   蓄電池単価: 三菱総研 補助事業データ推計（令和3〜6年度）平均
#     システム価格5.5 + 工事費1.3 = 6.8 万円/kWh
#   O&M費率: METI 2026想定の 0.42 万円/kW/年 を PV単価比 約2.7% とする場合は手入力で変更可
ECON_DEFAULTS = {
    "pv_cost_per_kw": 155_600,    # PVシステム単価 [円/kW] (METI 2026想定 地上50kW以上)
    "bat_cost_per_kwh": 68_000,   # 蓄電池単価 [円/kWh] (MRI補助事業データ推計 平均)
    "subsidy_pv_pct": 0,          # PV補助率 [%]
    "subsidy_bat_pct": 0,         # 蓄電池補助率 [%]
    "om_ratio_pct": 1.5,          # O&M費率 [%/年]（CAPEX比）※METI 2026想定は約2.7%
    # 蓄電池O&Mの「PCS定格 kW建て」モード用（三菱総研試算: 5,000円/kW/年）
    "om_bat_per_kw_pcs": 5_000,   # 蓄電池O&M [円/kW(PCS)/年]
    "decom_pct": 5.0,             # 廃止措置費用 [CAPEX×%]（三菱総研試算と同じ）
    "equity_ratio_pct": 30,       # 自己資本比率 [%]
    "loan_interest_pct": 2.0,     # 借入金利 [%/年]
    "loan_years": 15,             # 借入期間 [年]
    "irr_period_years": 20,       # P-IRR計算期間 [年]
}

# === 蓄電池モード ===
BATTERY_MODES = ["手動入力", "最適容量探索"]

# === 蓄電池O&M モード ===
# CAPEX比: O&M費 = 蓄電池CAPEX × (om_ratio_pct / 100)（PVと共通モデル）
# PCS_kW建て: O&M費 = bat_max_charge_kw × om_bat_per_kw_pcs（三菱総研試算準拠）
BATTERY_OM_MODES = ["CAPEX比（PVと共通）", "PCS_kW建て（三菱総研試算）"]


# ============================================================
# 気象データ読み込み（radiation.db / NEDO CSV）
# ============================================================

def get_station_options():
    """DBから地点一覧を取得してドロップダウン用リストを返す。"""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT point_no, point_name FROM points ORDER BY point_no"
    ).fetchall()
    conn.close()
    return [f"{no} ({name})" for no, name in rows]


def load_from_db(point_no):
    """DBから指定地点のGHI(要素1)と気温(要素5)を読み込む。"""
    conn = sqlite3.connect(DB_PATH)
    h_cols = ", ".join([f"h{i:02d}" for i in range(1, 25)])

    pt = conn.execute(
        "SELECT lat, lon FROM points WHERE point_no = ?", (point_no,)
    ).fetchone()
    if pt is None:
        conn.close()
        raise ValueError(f"地点 {point_no} がDBに見つかりません")
    lat, lon = pt

    ghi_rows = conn.execute(
        f"SELECT month, day, {h_cols} FROM radiation "
        f"WHERE point_no = ? AND element_no = 1 ORDER BY day_of_year",
        (point_no,)
    ).fetchall()

    temp_rows = conn.execute(
        f"SELECT month, day, {h_cols} FROM radiation "
        f"WHERE point_no = ? AND element_no = 5 ORDER BY day_of_year",
        (point_no,)
    ).fetchall()
    conn.close()

    cols = ["month", "day"] + [f"h{i:02d}" for i in range(1, 25)]
    ghi_df = pd.DataFrame(ghi_rows, columns=cols)
    temp_df = pd.DataFrame(temp_rows, columns=cols)

    h_cols_list = [f"h{i:02d}" for i in range(1, 25)]
    ghi_df[h_cols_list] = ghi_df[h_cols_list].replace(8888, np.nan)
    temp_df[h_cols_list] = temp_df[h_cols_list].replace(8888, np.nan)

    return lat, lon, ghi_df, temp_df


def load_snow_depth(point_no):
    """DBから指定地点の積雪深（要素9）を読み込む。"""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    h_cols = ", ".join([f"h{i:02d}" for i in range(1, 25)])
    rows = conn.execute(
        f"SELECT month, day, {h_cols} FROM radiation "
        f"WHERE point_no = ? AND element_no = 9 ORDER BY day_of_year",
        (point_no,)
    ).fetchall()
    conn.close()
    if not rows:
        return None
    cols = ["month", "day"] + [f"h{i:02d}" for i in range(1, 25)]
    snow_df = pd.DataFrame(rows, columns=cols)
    h_data_cols = [f"h{i:02d}" for i in range(1, 25)]
    snow_df[h_data_cols] = snow_df[h_data_cols].replace(8888, np.nan)
    return snow_df


def build_albedo_series(snow_df):
    """積雪深DataFrameからalbedo時系列（30分×365日）を生成する。"""
    if snow_df is None:
        return np.full(365 * 48, ALBEDO_NORMAL)

    n_days = len(snow_df)
    albedo_30min = np.full((n_days, 48), ALBEDO_NORMAL)
    h_cols = [f"h{i:02d}" for i in range(1, 25)]

    for i in range(n_days):
        for j, col in enumerate(h_cols):
            val = snow_df.iloc[i][col]
            is_snow = (val is not None and not np.isnan(val) and val > 0)
            albedo_30min[i, j * 2] = ALBEDO_SNOW if is_snow else ALBEDO_NORMAL
            albedo_30min[i, j * 2 + 1] = ALBEDO_SNOW if is_snow else ALBEDO_NORMAL

    return albedo_30min.flatten()


def load_from_csv(file_obj):
    """アップロードされたNEDO CSVファイルからGHIと気温を読み込む。"""
    if hasattr(file_obj, 'name'):
        filepath = file_obj.name
    else:
        filepath = file_obj

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    hdr = rows[0]
    point_name = hdr[1].strip()
    lat = float(hdr[2]) + float(hdr[3]) / 60.0
    lon = float(hdr[4]) + float(hdr[5]) / 60.0

    ghi_records = []
    temp_records = []
    for row in rows[1:]:
        if not row or len(row) < 33:
            continue
        elem = int(row[0])
        month = int(row[1])
        day = int(row[2])
        hourly = []
        for i in range(24):
            v = float(row[4 + i])
            hourly.append(None if v == NO_DATA else v)
        if elem == 1:
            ghi_records.append([month, day] + hourly)
        elif elem == 5:
            temp_records.append([month, day] + hourly)

    cols = ["month", "day"] + [f"h{i:02d}" for i in range(1, 25)]
    ghi_df = pd.DataFrame(ghi_records, columns=cols)
    temp_df = pd.DataFrame(temp_records, columns=cols)

    return lat, lon, ghi_df, temp_df


# ============================================================
# JEPX価格データ読み込み（jepx.db）
# ============================================================

def load_jepx_prices(area, fiscal_years, month_day):
    """JEPX DBから指定エリア・複数年度の価格を読み込み、平均してmonth_day順に並べる。

    Args:
        area: エリア名（"東京" など）
        fiscal_years: 年度リスト（[2023, 2024, 2025] など）
        month_day: [(month, day), ...] 365日分（radiation.dbと同じ並び）

    Returns:
        np.array (365, 48) [円/kWh]
    """
    if not os.path.exists(JEPX_DB_PATH):
        raise FileNotFoundError(
            f"JEPX DB が見つかりません: {JEPX_DB_PATH}\n"
            "build_jepx_db.py を実行してDBを生成してください。"
        )
    if not fiscal_years:
        raise ValueError("JEPX年度を1つ以上選択してください")

    conn = sqlite3.connect(JEPX_DB_PATH)

    # 指定年度・エリアの全データを取得
    placeholders = ",".join(["?"] * len(fiscal_years))
    rows = conn.execute(
        f"SELECT date, slot, AVG(price) FROM jepx_prices "
        f"WHERE area = ? AND fiscal_year IN ({placeholders}) "
        f"GROUP BY date, slot",
        [area] + list(fiscal_years),
    ).fetchall()
    conn.close()

    # MM-DD × slot → 価格 のディクショナリ化
    price_lookup = {}
    for date_str, slot, price in rows:
        price_lookup[(date_str, int(slot))] = float(price)

    # month_day順に並べる（365×48）
    n_days = len(month_day)
    prices = np.zeros((n_days, 48))
    for i, (m, d) in enumerate(month_day):
        date_str = f"{m:02d}-{d:02d}"
        for slot in range(1, 49):
            key = (date_str, slot)
            # JEPXコマ番号slot=1〜48 → 配列index 0〜47
            prices[i, slot - 1] = price_lookup.get(key, 0.0)

    return prices


# ============================================================
# 30分補間
# ============================================================

def interpolate_to_30min(hourly_values):
    """24コマの毎時値を48コマの30分値に線形補間する。"""
    h = np.array(hourly_values, dtype=float)
    x_hourly = np.arange(0.5, 24.5, 1.0)
    x_30min = np.arange(0.25, 24.25, 0.5)
    result = np.interp(x_30min, x_hourly, h)
    return result


def prepare_30min_data(ghi_df, temp_df):
    """365日分のGHIと気温を30分48コマに補間する。"""
    n_days = len(ghi_df)
    ghi_30min = np.zeros((n_days, 48))
    temp_30min = np.zeros((n_days, 48))
    month_day = []

    h_cols = [f"h{i:02d}" for i in range(1, 25)]

    for i in range(n_days):
        ghi_hourly = ghi_df.iloc[i][h_cols].values.astype(float)
        ghi_hourly = np.nan_to_num(ghi_hourly, nan=0.0)
        ghi_kwh = ghi_hourly * 0.01 / 3.6

        temp_hourly = temp_df.iloc[i][h_cols].values.astype(float)
        temp_hourly = np.nan_to_num(temp_hourly, nan=0.0)
        temp_c = temp_hourly * 0.1

        ghi_30min[i] = interpolate_to_30min(ghi_kwh)
        temp_30min[i] = interpolate_to_30min(temp_c)
        month_day.append((int(ghi_df.iloc[i]["month"]), int(ghi_df.iloc[i]["day"])))

    return ghi_30min, temp_30min, month_day


# ============================================================
# pvlibによるPOA変換
# ============================================================

def compute_poa_30min(ghi_30min, lat, lon, surface_tilt, surface_azimuth,
                      bifacial=False, bifaciality=0.75, gcr=0.4,
                      height=2.0, pitch=5.0, albedo_flat=None):
    """30分GHI配列からpvlibを使ってPOA（傾斜面日射量）を計算する。"""
    if not HAS_PVLIB:
        return ghi_30min.copy()

    n_days = ghi_30min.shape[0]
    total_slots = n_days * 48

    times = pd.date_range(
        start="2023-01-01 00:00", periods=total_slots, freq="30min", tz="Asia/Tokyo"
    )

    # ghi_30min は「30分平均 kW/m²」（毎時kWh/m²を大きさ保存で補間したもの）。
    # W/m² 実強度への変換は ×1000 のみ。×2000 にすると晴天指数ktが2倍で評価され、
    # Erbs分離のDNIが過大 → POAが5〜8%過大になる（pv-sim-biz バグ1と同型）。
    ghi_flat = ghi_30min.flatten() * 1000.0
    ghi_series = pd.Series(ghi_flat, index=times)

    site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")
    solpos = site.get_solarposition(times)

    erbs = pvlib.irradiance.erbs(ghi_series, solpos["zenith"], times)
    dni = erbs["dni"].fillna(0).clip(lower=0)
    dhi = erbs["dhi"].fillna(0).clip(lower=0)

    if bifacial:
        from pvlib.bifacial.infinite_sheds import get_irradiance as get_bifacial_irradiance

        albedo_series = pd.Series(
            albedo_flat if albedo_flat is not None else np.full(total_slots, ALBEDO_NORMAL),
            index=times,
        )
        result = get_bifacial_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            solar_zenith=solpos["zenith"],
            solar_azimuth=solpos["azimuth"],
            gcr=gcr,
            height=height,
            pitch=pitch,
            ghi=ghi_series,
            dhi=dhi,
            dni=dni,
            albedo=albedo_series,
            bifaciality=bifaciality,
        )
        poa_global = result["poa_global"].fillna(0).clip(lower=0)
    else:
        poa_components = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            solar_zenith=solpos["zenith"],
            solar_azimuth=solpos["azimuth"],
            dni=dni,
            ghi=ghi_series,
            dhi=dhi,
        )
        poa_global = poa_components["poa_global"].fillna(0).clip(lower=0)

    # 出力も入力と同じ「30分平均 kW/m²」に戻す（W/m² → kW/m² は /1000 のみ）
    poa_kw = poa_global.values / 1000.0
    poa_30min = poa_kw.reshape(n_days, 48)

    return poa_30min


# ============================================================
# 発電量計算（JIS C 8907準拠・30分48コマ）
# ============================================================

def calculate_generation(
    lat, lon, ghi_df, temp_df,
    faces, KHD, KPD, KPM, KPA, eta_ino,
    alpha_pct, delta_t,
    bifacial=False, bifaciality=0.75, gcr=0.4,
    height=2.0, pitch=5.0, albedo_flat=None,
):
    """メイン発電量計算関数。"""
    K_prime = KHD * KPD * KPM * KPA * eta_ino

    ghi_30min, temp_30min, month_day = prepare_30min_data(ghi_df, temp_df)
    n_days = len(month_day)

    face_poa_list = []
    for face in faces:
        azimuth = face.get("azimuth", ORIENTATION_TO_AZIMUTH.get(face["orientation"], 180))
        tilt = face["tilt"]
        poa = compute_poa_30min(
            ghi_30min, lat, lon, tilt, azimuth,
            bifacial=bifacial, bifaciality=bifaciality,
            gcr=gcr, height=height, pitch=pitch,
            albedo_flat=albedo_flat,
        )
        face_poa_list.append(poa)

    total_gen = np.zeros((n_days, 48))
    face_annual = []

    for f_idx, face in enumerate(faces):
        ppeak = face["ppeak"]
        poa = face_poa_list[f_idx]
        pcs_kw = face.get("pcs_limit_kw")

        tcr = temp_30min + delta_t
        kpt = 1.0 + alpha_pct * (tcr - 25.0) / 100.0
        k_total = K_prime * kpt

        ep_face = k_total * ppeak * poa / G_STC * 0.5
        ep_face = np.clip(ep_face, 0, None)

        if pcs_kw and pcs_kw > 0:
            pcs_limit_30min = pcs_kw * 0.5
            ep_face = np.clip(ep_face, 0, pcs_limit_30min)

        total_gen += ep_face
        face_annual.append(float(np.sum(ep_face)))

    monthly = {}
    for i in range(n_days):
        m = month_day[i][0]
        day_total = np.sum(total_gen[i])
        monthly[m] = monthly.get(m, 0) + day_total

    annual = sum(monthly.values())

    return {
        "annual": annual,
        "monthly": monthly,
        "face_annual": face_annual,
        "K_prime": K_prime,
        "total_gen_clipped": total_gen,
        "month_day": month_day,
        "n_faces": len(faces),
    }


# ============================================================
# 出力制御プロファイル
# ============================================================

def build_curtail_prob_30min(month_day, annual_curtail_rate_pct, generation_30min=None):
    """月別×時間帯別の制御確率重みを30分コマに展開し、年間制御率にスケーリング。

    年間制御率の解釈: 「年間制御損失 ÷ 年間発電量 = 制御率」（エネルギー重み付け）
    generation_30min が与えられた場合はエネルギー重み付けスケーリング、
    None の場合はコマ数平均スケーリング（粗近似）にフォールバック。

    Args:
        month_day: [(month, day), ...] 365日分
        annual_curtail_rate_pct: 年間出力制御率 [%]
        generation_30min: np.array (365, 48) 発電量。エネルギー重み付け用
    Returns:
        np.array (365, 48) — 各コマの制御確率（0〜1）
    """
    n_days = len(month_day)
    prob = np.zeros((n_days, 48))

    if annual_curtail_rate_pct <= 0:
        return prob

    # 月別×時間帯別の重みを各30分コマに展開
    for i, (m, _d) in enumerate(month_day):
        weights = CURTAIL_PROFILE_WEIGHTS.get(m, {})
        for s in range(48):
            hour = s * 0.5  # 30分コマの開始時刻
            w = 0.0
            for (h_start, h_end), val in weights.items():
                if h_start <= hour < h_end:
                    w = val
                    break
            prob[i, s] = w

    if generation_30min is not None:
        # エネルギー重み付け: sum(gen * prob) / sum(gen) == 目標制御率
        gen_total = float(generation_30min.sum())
        weighted = float((generation_30min * prob).sum())
        if gen_total <= 0 or weighted <= 0:
            return np.zeros_like(prob)
        target_loss = (annual_curtail_rate_pct / 100.0) * gen_total
        scale = target_loss / weighted
        prob = prob * scale
    else:
        # コマ数平均でのスケーリング（フォールバック）
        total_weight = float(prob.sum())
        if total_weight <= 0:
            return np.zeros_like(prob)
        target_total = (annual_curtail_rate_pct / 100.0) * (n_days * 48)
        prob = prob * (target_total / total_weight)

    prob = np.clip(prob, 0.0, 1.0)
    return prob


def apply_curtailment(generation_30min, curtail_prob):
    """発電量に制御確率を適用する。蓄電池なしの場合の制御後発電量。

    Returns:
        gen_after: np.array (365, 48) — 制御後発電量
        curtail_amount: np.array (365, 48) — 制御による損失量
    """
    curtail_amount = generation_30min * curtail_prob
    gen_after = generation_30min - curtail_amount
    return gen_after, curtail_amount


# ============================================================
# ベースライン: 蓄電池なし（PV全量売電・制御適用）
# ============================================================

def baseline_no_battery(generation_30min, jepx_prices, month_day,
                        premium, nonfossil_price, bg_fee,
                        curtail_prob=None):
    """蓄電池なしの場合の売電・収益を計算する。

    PV発電量をJEPX市場に売電するシンプルな計算。
    収益単価 = JEPX価格 + プレミアム + 非化石証書 − BG手数料

    2種類の抑制を扱う:
      1. 強制出力制御（系統からの指令）: curtail_prob 由来。回避不可
      2. 経済的自主抑制: 売電単価が負のコマは売ると損失になるため出力を絞る。
         LP側（optimize_battery_fip）は curtail 変数で同じ判断を行うため、
         ベースライン側にも同じ選択肢を与えないと「自主抑制できること自体の価値」が
         蓄電池の増分収益に混入する（with/without の非対称性）。

    Returns:
        dict: 年間/月別の発電量・売電量・収益・制御損失
              curtailment は強制＋自主の合計。内訳は annual_curtail_forced /
              annual_curtail_economic で個別に返す。
    """
    n_days = generation_30min.shape[0]

    if curtail_prob is None:
        curtail_prob = np.zeros_like(generation_30min)

    # 1. 強制出力制御後の発電量 = (1 - 制御確率) × 元発電量
    gen_after, curtail_forced = apply_curtailment(generation_30min, curtail_prob)

    # 単価マトリクス (365, 48)
    revenue_unit = jepx_prices + premium + nonfossil_price - bg_fee  # 円/kWh

    # 2. 経済的自主抑制: 単価が負のコマは売電せず出力を絞る（LP側と対称）
    loss_making = revenue_unit < 0
    curtail_economic = np.where(loss_making, gen_after, 0.0)
    export_final = np.where(loss_making, 0.0, gen_after)

    curtail_amount = curtail_forced + curtail_economic

    # 収益 = 売電量 × 単価
    revenue_30min = export_final * revenue_unit  # 円

    annual_gen = float(np.sum(generation_30min))                 # 制御前
    annual_curtail = float(np.sum(curtail_amount))               # 抑制合計
    annual_curtail_forced = float(np.sum(curtail_forced))        # うち強制出力制御
    annual_curtail_economic = float(np.sum(curtail_economic))    # うち経済的自主抑制
    annual_export = float(np.sum(export_final))
    annual_revenue = float(np.sum(revenue_30min))

    monthly_gen = {}
    monthly_export = {}
    monthly_revenue = {}
    monthly_curtail = {}
    for i in range(n_days):
        m = month_day[i][0]
        monthly_gen[m] = monthly_gen.get(m, 0) + np.sum(generation_30min[i])
        monthly_export[m] = monthly_export.get(m, 0) + np.sum(export_final[i])
        monthly_revenue[m] = monthly_revenue.get(m, 0) + np.sum(revenue_30min[i])
        monthly_curtail[m] = monthly_curtail.get(m, 0) + np.sum(curtail_amount[i])

    return {
        "export": export_final,
        "curtailment": curtail_amount,
        "curtailment_forced": curtail_forced,
        "curtailment_economic": curtail_economic,
        "revenue_30min": revenue_30min,
        "annual_gen": annual_gen,
        "annual_curtail": annual_curtail,
        "annual_curtail_forced": annual_curtail_forced,
        "annual_curtail_economic": annual_curtail_economic,
        "annual_export": annual_export,
        "annual_revenue": annual_revenue,
        "monthly_gen": monthly_gen,
        "monthly_export": monthly_export,
        "monthly_revenue": monthly_revenue,
        "monthly_curtail": monthly_curtail,
    }


# ============================================================
# 蓄電池LP最適化（JEPX売電収入の最大化）
# ============================================================

def optimize_battery_fip(generation_30min, jepx_prices, month_day,
                         capacity_kwh, max_charge_kw, max_discharge_kw,
                         eff_charge_pct, eff_discharge_pct,
                         soc_min_pct, soc_max_pct,
                         premium, nonfossil_price, bg_fee,
                         curtail_prob=None):
    """蓄電池の最適充放電をLPで求める。

    目的関数: 年間JEPX売電収入の最大化
        maximize Σ_t [export(t) × (jepx(t) + premium + nonfossil − bg_fee)]

    エネルギーバランス（需要なし）:
        pv_gen(t) + discharge(t) = export(t) + charge(t) + curtailment(t)

    出力制御（POI=系統接続点の総輸出制限）:
        export(t) ≤ pv_gen(t) × (1 − curtail_prob(t))
        ※curtailment時の制限はPOIの総輸出にかかる（PV直売も蓄電池放電も区別なく）。
        ※蓄電池は「PVを充電して逃がし（charge）、非curtailment時に放電」する経路のみで
          curtailmentを回避できる。同スロットの放電で上限を押し上げることはできない。

    PCS同時稼働制約（ソフト・mutual exclusion）:
        charge(t) + discharge(t) ≤ max(max_charge_kw, max_discharge_kw) × 0.5
        ※1台のPCSは同スロットで充放電を同時フル稼働できない物理制約。

    Returns:
        dict: 充放電・売電・SOC・収益等
    """
    if not HAS_PULP:
        raise RuntimeError("PuLPがインストールされていません。pip install PuLP を実行してください。")

    n_days, n_slots = generation_30min.shape
    T = n_days * n_slots  # 17,520
    dt = 0.5  # 30分 = 0.5時間

    eff_ch = eff_charge_pct / 100.0
    eff_dc = eff_discharge_pct / 100.0
    soc_min = capacity_kwh * soc_min_pct / 100.0
    soc_max = capacity_kwh * soc_max_pct / 100.0
    max_charge_per_slot = max_charge_kw * dt
    max_discharge_per_slot = max_discharge_kw * dt
    # 同一コマ内の充放電合計をPCS定格以下に制限（バイパス配管化の防止）
    # charge[t] + discharge[t] ≤ max_power_per_slot
    # これにより1コマで全力充電と全力放電を同時に実行することを物理的に不可能にする
    max_power_per_slot = max(max_charge_per_slot, max_discharge_per_slot)

    # 1次元に展開
    gen_flat = generation_30min.flatten()
    price_flat = jepx_prices.flatten()
    # 売電単価（30分コマ別）
    unit_price = price_flat + premium + nonfossil_price - bg_fee  # 円/kWh

    if curtail_prob is None:
        curtail_prob_flat = np.zeros(T)
    else:
        curtail_prob_flat = curtail_prob.flatten()

    # === LP定式化 ===
    prob = pulp.LpProblem("FIP_Battery_Optimization", pulp.LpMaximize)

    # 決定変数（各時刻 t）
    charge = [pulp.LpVariable(f"ch_{t}", lowBound=0, upBound=max_charge_per_slot) for t in range(T)]
    discharge = [pulp.LpVariable(f"dc_{t}", lowBound=0, upBound=max_discharge_per_slot) for t in range(T)]
    export = [pulp.LpVariable(f"ex_{t}", lowBound=0) for t in range(T)]
    curtail = [pulp.LpVariable(f"ct_{t}", lowBound=0) for t in range(T)]
    soc_var = [pulp.LpVariable(f"soc_{t}", lowBound=soc_min, upBound=soc_max) for t in range(T)]

    # 目的関数: 年間売電収入の最大化
    prob += pulp.lpSum([export[t] * unit_price[t] for t in range(T)])

    # 制約条件
    for t in range(T):
        # エネルギーバランス: PV発電 + 放電 = 売電 + 充電 + 出力抑制
        prob += gen_flat[t] + discharge[t] == export[t] + charge[t] + curtail[t]

        # 出力制御: POI（系統接続点）の総輸出がcurtail率で制限される
        # ※export = pv_to_grid + discharge 全体にかかる（放電も制限対象）
        # ※蓄電池は「PVをcharge経路で一時吸収 → 非curtailスロットでdischarge」
        #   することで curtailmentを回避できる（phantom dischargeは不可）
        if curtail_prob_flat[t] > 0:
            max_export = gen_flat[t] * (1.0 - curtail_prob_flat[t])
            prob += export[t] <= max_export

        # PCS同時稼働制約（ソフト mutual exclusion）
        # 1台のPCSは同じコマで充電と放電を同時にフル稼働できない
        prob += charge[t] + discharge[t] <= max_power_per_slot

        # SOC遷移: SOC(t) = SOC(t-1) + charge*η_ch − discharge/η_dc
        if t == 0:
            prob += soc_var[t] == soc_min + charge[t] * eff_ch - discharge[t] / eff_dc
        else:
            prob += soc_var[t] == soc_var[t - 1] + charge[t] * eff_ch - discharge[t] / eff_dc

    # 終端SOC制約: 年末SOCを初期SOCに戻す
    prob += soc_var[T - 1] == soc_min

    # === ソルバー実行 ===
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=180)
    prob.solve(solver)

    if prob.status != pulp.constants.LpStatusOptimal:
        raise RuntimeError(f"LP最適化に失敗しました（ステータス: {pulp.LpStatus[prob.status]}）")

    # === 結果取得 ===
    charge_vals = np.array([ch.varValue for ch in charge]).reshape(n_days, n_slots)
    discharge_vals = np.array([dc.varValue for dc in discharge]).reshape(n_days, n_slots)
    export_vals = np.array([ex.varValue for ex in export]).reshape(n_days, n_slots)
    curtail_vals = np.array([ct.varValue for ct in curtail]).reshape(n_days, n_slots)
    soc_vals = np.array([s.varValue for s in soc_var]).reshape(n_days, n_slots)

    # 30分コマごとの収益
    revenue_30min = export_vals * unit_price.reshape(n_days, n_slots)

    # 抑制の内訳分解（ベースライン側と同じ基準で分類）
    # 売電単価が負のコマの抑制 = 経済的自主抑制、それ以外 = 強制出力制御由来
    unit_price_2d = unit_price.reshape(n_days, n_slots)
    loss_making = unit_price_2d < 0
    curtail_economic_vals = np.where(loss_making, curtail_vals, 0.0)
    curtail_forced_vals = np.where(loss_making, 0.0, curtail_vals)

    # 年間集計
    annual_gen = float(np.sum(generation_30min))
    annual_export = float(np.sum(export_vals))
    annual_charge = float(np.sum(charge_vals))
    annual_discharge = float(np.sum(discharge_vals))
    annual_curtail = float(np.sum(curtail_vals))
    annual_curtail_forced = float(np.sum(curtail_forced_vals))
    annual_curtail_economic = float(np.sum(curtail_economic_vals))
    annual_revenue = float(pulp.value(prob.objective))

    # 月別集計
    monthly_gen = {}
    monthly_export = {}
    monthly_revenue = {}
    monthly_charge = {}
    monthly_discharge = {}
    for i in range(n_days):
        m = month_day[i][0]
        monthly_gen[m] = monthly_gen.get(m, 0) + np.sum(generation_30min[i])
        monthly_export[m] = monthly_export.get(m, 0) + np.sum(export_vals[i])
        monthly_revenue[m] = monthly_revenue.get(m, 0) + np.sum(revenue_30min[i])
        monthly_charge[m] = monthly_charge.get(m, 0) + np.sum(charge_vals[i])
        monthly_discharge[m] = monthly_discharge.get(m, 0) + np.sum(discharge_vals[i])

    return {
        "battery_charge": charge_vals,
        "battery_discharge": discharge_vals,
        "export": export_vals,
        "curtailment": curtail_vals,
        "curtailment_forced": curtail_forced_vals,
        "curtailment_economic": curtail_economic_vals,
        "soc": soc_vals,
        "revenue_30min": revenue_30min,
        "annual_gen": annual_gen,
        "annual_export": annual_export,
        "annual_charge": annual_charge,
        "annual_discharge": annual_discharge,
        "annual_curtail": annual_curtail,
        "annual_curtail_forced": annual_curtail_forced,
        "annual_curtail_economic": annual_curtail_economic,
        "annual_revenue": annual_revenue,
        "monthly_gen": monthly_gen,
        "monthly_export": monthly_export,
        "monthly_revenue": monthly_revenue,
        "monthly_charge": monthly_charge,
        "monthly_discharge": monthly_discharge,
        "battery_capacity": capacity_kwh,
        "soc_min": soc_min,
        "soc_max": soc_max,
    }


# ============================================================
# 蓄電池容量探索（LP一体化）
# ============================================================

def optimize_capacity_fip(generation_30min, jepx_prices, month_day,
                          max_charge_kw, max_discharge_kw,
                          eff_charge_pct, eff_discharge_pct,
                          soc_min_pct, soc_max_pct,
                          premium, nonfossil_price, bg_fee,
                          bat_cost_per_kwh_net, irr_period_years,
                          capacity_upper_kwh,
                          curtail_prob=None):
    """段階1: LP一体化で蓄電池容量を決定変数に含めて最適化。

    目的関数: 売電収入 − 蓄電池投資の年額換算（払戻期間 = irr_period_years）
        maximize Σ_t [export(t) × unit_price(t)] − capacity × (bat_cost / irr_period_years)

    LP一体化は P-IRR の近似として機能する。厳密な P-IRR 最大化ではないが、
    収益貢献の限界容量を示す。後段のグリッドサーチで P-IRR カーブを描く。
    """
    if not HAS_PULP:
        raise RuntimeError("PuLPがインストールされていません")

    n_days, n_slots = generation_30min.shape
    T = n_days * n_slots
    dt = 0.5

    eff_ch = eff_charge_pct / 100.0
    eff_dc = eff_discharge_pct / 100.0
    soc_max_ratio = soc_max_pct / 100.0
    soc_min_ratio = soc_min_pct / 100.0
    max_charge_per_slot = max_charge_kw * dt
    max_discharge_per_slot = max_discharge_kw * dt
    # 同一コマ内の充放電合計をPCS定格以下に制限（バイパス配管化の防止）
    max_power_per_slot = max(max_charge_per_slot, max_discharge_per_slot)

    gen_flat = generation_30min.flatten()
    price_flat = jepx_prices.flatten()
    unit_price = price_flat + premium + nonfossil_price - bg_fee

    if curtail_prob is None:
        curtail_prob_flat = np.zeros(T)
    else:
        curtail_prob_flat = curtail_prob.flatten()

    prob = pulp.LpProblem("FIP_Optimal_Capacity", pulp.LpMaximize)

    # 容量を決定変数に含める
    capacity_var = pulp.LpVariable("cap", lowBound=0, upBound=capacity_upper_kwh)
    charge = [pulp.LpVariable(f"ch_{t}", lowBound=0, upBound=max_charge_per_slot) for t in range(T)]
    discharge = [pulp.LpVariable(f"dc_{t}", lowBound=0, upBound=max_discharge_per_slot) for t in range(T)]
    export = [pulp.LpVariable(f"ex_{t}", lowBound=0) for t in range(T)]
    curtail = [pulp.LpVariable(f"ct_{t}", lowBound=0) for t in range(T)]
    soc_var = [pulp.LpVariable(f"soc_{t}", lowBound=0) for t in range(T)]

    # 目的関数: 売電収入 − 蓄電池投資年額換算
    annual_battery_cost_per_kwh = bat_cost_per_kwh_net / max(irr_period_years, 1)
    revenue_term = pulp.lpSum([export[t] * unit_price[t] for t in range(T)])
    cost_term = capacity_var * annual_battery_cost_per_kwh
    prob += revenue_term - cost_term

    # 制約条件
    for t in range(T):
        prob += gen_flat[t] + discharge[t] == export[t] + charge[t] + curtail[t]
        # 出力制御: POIの総輸出が curtail率 で制限される（optimize_battery_fipと同じ）
        if curtail_prob_flat[t] > 0:
            max_export = gen_flat[t] * (1.0 - curtail_prob_flat[t])
            prob += export[t] <= max_export
        # PCS同時稼働制約（ソフト mutual exclusion）
        prob += charge[t] + discharge[t] <= max_power_per_slot
        # SOC上下限（容量に連動）
        prob += soc_var[t] <= capacity_var * soc_max_ratio
        prob += soc_var[t] >= capacity_var * soc_min_ratio
        if t == 0:
            prob += soc_var[t] == capacity_var * soc_min_ratio + charge[t] * eff_ch - discharge[t] / eff_dc
        else:
            prob += soc_var[t] == soc_var[t - 1] + charge[t] * eff_ch - discharge[t] / eff_dc

    # 終端SOC = 初期SOC
    prob += soc_var[T - 1] == capacity_var * soc_min_ratio

    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=300)
    prob.solve(solver)

    if prob.status != pulp.constants.LpStatusOptimal:
        raise RuntimeError(f"最適容量探索LPに失敗（{pulp.LpStatus[prob.status]}）")

    return {
        "optimal_capacity_kwh": float(capacity_var.varValue),
        "objective_value": float(pulp.value(prob.objective)),
    }


# ============================================================
# 経済性計算（CAPEX / OPEX / 借入金 / CF / IRR）
# ============================================================

def calc_irr(cashflows, tol=1e-8, max_iter=200):
    """IRRを求める。複数根がある場合は経済的に意味のある「ゼロに最も近い」根を返す。

    実装方針:
      1. CF が同符号のみ → None（未定義）
      2. r ∈ [-0.99, 5.0] を粗くスキャンして全ての符号変化区間を列挙
      3. 各区間で二分法を実行（高速・確実）
      4. 区間が無ければ広範囲の二分法でフォールバック
      5. 複数根がある場合は |r| 最小（ゼロに最も近い）を返す

    Project CF の終端に廃止措置等の負キャッシュがあると 2 つ以上の IRR が
    現れることがあるため、単純な Newton 法ではなく区間スキャンを使う。
    """
    # CF に符号変化が無ければ IRR は未定義
    nonzero = [cf for cf in cashflows if cf != 0]
    if not nonzero:
        return None
    if all(cf > 0 for cf in nonzero) or all(cf < 0 for cf in nonzero):
        return None

    def npv(rate):
        try:
            return sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))
        except OverflowError:
            return float("inf") if rate > 0 else float("-inf")

    def bisect(lo, hi, f_lo, f_hi):
        """[lo, hi] 区間で f が符号変化していると仮定して根を求める。"""
        for _ in range(max_iter):
            mid = (lo + hi) / 2
            f_mid = npv(mid)
            if not np.isfinite(f_mid):
                hi = mid
                continue
            if abs(f_mid) < tol or (hi - lo) / 2 < tol:
                return mid
            if f_lo * f_mid < 0:
                hi = mid
                f_hi = f_mid
            else:
                lo = mid
                f_lo = f_mid
        return (lo + hi) / 2

    # === 粗スキャンで符号変化区間を全て列挙 ===
    # 細かいステップでスキャン（解像度 0.01 = 1% 単位）
    sample_rates = []
    # 負のレート [-0.99, 0.0]
    r = -0.99
    while r <= 0.0 + 1e-9:
        sample_rates.append(r)
        r += 0.01
    # 正のレート [0.01, 5.0]
    r = 0.01
    while r <= 5.0 + 1e-9:
        sample_rates.append(r)
        r += 0.01
    sample_npvs = [npv(r) for r in sample_rates]

    roots = []
    for i in range(1, len(sample_rates)):
        f0, f1 = sample_npvs[i - 1], sample_npvs[i]
        if not (np.isfinite(f0) and np.isfinite(f1)):
            continue
        if f0 == 0:
            roots.append(sample_rates[i - 1])
            continue
        if f0 * f1 < 0:
            root = bisect(sample_rates[i - 1], sample_rates[i], f0, f1)
            if root is not None and np.isfinite(root):
                roots.append(root)

    if roots:
        # ゼロに最も近い根（経済的に意味のある根）を返す
        return min(roots, key=abs)

    # スキャンで見つからない場合: 広範囲の二分法フォールバック
    lo, hi = -0.99, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if not (np.isfinite(f_lo) and np.isfinite(f_hi)):
        return None
    if f_lo * f_hi > 0:
        return None
    return bisect(lo, hi, f_lo, f_hi)


def calc_loan_payment(principal, annual_rate_pct, n_years):
    """元利均等返済の年額を計算する（PMT方式）。"""
    if principal <= 0 or n_years <= 0:
        return 0.0
    r = annual_rate_pct / 100.0
    if r == 0:
        return principal / n_years
    return principal * r * (1 + r) ** n_years / ((1 + r) ** n_years - 1)


def calc_npv(cashflows, discount_rate_pct):
    """NPV を計算する。cashflows[0] が初期投資（通常マイナス）。"""
    r = discount_rate_pct / 100.0
    return sum(cf / (1 + r) ** t for t, cf in enumerate(cashflows))


def build_cashflow(
    pv_capex, bat_capex, subsidy_pv, subsidy_bat,
    annual_revenue_with_bat, annual_revenue_without_bat,
    om_ratio_pct, equity_ratio_pct,
    loan_interest_pct, loan_years, irr_period_years,
    bat_life_years, bat_degrade_pct_per_year, bat_eol_action,
    bat_replace_cost_ratio_pct,
    bat_om_mode="CAPEX比（PVと共通）",
    om_bat_per_kw_pcs=5_000.0,
    bat_max_charge_kw=0.0,
    decom_pct=0.0,
    fip_premium_years=None,
    annual_revenue_with_bat_no_premium=None,
    annual_revenue_without_bat_no_premium=None,
    pv_degrade_pct_per_year=0.0,
    pv_start_age_years=0,
    arbitrage_realization_rate_pct=100.0,
):
    """20年間の年次キャッシュフローを構築する。

    2フェーズ収益モデル（FIP+プレミアム → プレミアム終了後）:
      Phase 1 (year 1..fip_premium_years): FIPプレミアム有り期間
        revenue = with_bat / without_bat (プレミアム込みLP結果)
      Phase 2 (year fip_premium_years+1..irr_period_years): プレミアム終了後
        revenue = with_bat_no_premium / without_bat_no_premium

    Args:
        pv_capex: PV設備投資額（補助金前）[円] ※ケースB(FIT→FIP)は 0 を渡す（サンクコスト）
        bat_capex: 蓄電池投資額（補助金前）[円]
        subsidy_pv, subsidy_bat: 補助金額 [円]
        annual_revenue_with_bat: プレミアム期間 蓄電池あり年収益 [円/年]
        annual_revenue_without_bat: プレミアム期間 蓄電池なし年収益 [円/年]
        annual_revenue_with_bat_no_premium: プレミアム終了後 蓄電池あり年収益 [円/年]
            None の場合は annual_revenue_with_bat と同じ（後方互換）
        annual_revenue_without_bat_no_premium: プレミアム終了後 蓄電池なし年収益 [円/年]
            None の場合は annual_revenue_without_bat と同じ（後方互換）
        fip_premium_years: プレミアム期間の年数 [年]
            None or >= irr_period_years の場合はプレミアムが全期間有効（従来動作）
        om_ratio_pct: O&M費率 [%/年]（CAPEX比、補助金前ベース）
        equity_ratio_pct: 自己資本比率 [%]
        loan_interest_pct: 借入金利 [%/年]
        loan_years: 借入期間 [年]
        irr_period_years: P-IRR計算期間 [年]
        bat_life_years: 蓄電池寿命 [年]
        bat_degrade_pct_per_year: 蓄電池年間劣化率 [%/年]
        bat_eol_action: "交換（再投資）" or "終了（蓄電池なし運用）"
        bat_replace_cost_ratio_pct: 交換時単価（当初比、%）
        bat_om_mode: 蓄電池O&Mモード "CAPEX比（PVと共通）" or "PCS_kW建て（三菱総研試算）"
        om_bat_per_kw_pcs: 蓄電池O&M [円/kW(PCS)/年]（PCS_kW建てモード時）
        bat_max_charge_kw: 蓄電池PCS定格 [kW]（PCS_kW建てモード時）
        decom_pct: 廃止措置費用 [CAPEX×%]（最終年に計上）
        pv_degrade_pct_per_year: PV年間劣化率 [%/年]（線形）。年収益全体
            （蓄電池アービトラージ込み）に乗算する経年劣化係数
        pv_start_age_years: このキャッシュフローの年1が始まる時点でのPVの既経過年数。
            ケースA（新設）は0。ケースB（既存FIT→FIP転）はFIP転時点でのPV経過年数
            （fip_transition_year − 1）を渡し、転居前からの物理劣化を正しく反映する
        arbitrage_realization_rate_pct: 蓄電池アービトラージ実現率 [%]（デフォルト100=無補正）。
            LP最適化は1年分のJEPX価格を完全予見する理論上限であり、実運用（前日予測ベース）の
            蓄電池増分収益（アービトラージ＋出力制御回避）はこれを下回るのが通常。
            蓄電池による増分収益にのみ乗算し、ベースラインのJEPX直売収入には適用しない。

    Returns:
        dict: 年次CFテーブル、IRR、回収年数等
    """
    # プレミアム期間の正規化
    if fip_premium_years is None:
        fip_premium_years = irr_period_years  # 全期間プレミアム有り（後方互換）
    # プレミアム終了後の収益（省略時は同一 = 後方互換）
    if annual_revenue_with_bat_no_premium is None:
        annual_revenue_with_bat_no_premium = annual_revenue_with_bat
    if annual_revenue_without_bat_no_premium is None:
        annual_revenue_without_bat_no_premium = annual_revenue_without_bat
    # === CAPEX ===
    total_capex_gross = pv_capex + bat_capex  # 補助金前
    total_subsidy = subsidy_pv + subsidy_bat
    net_capex = total_capex_gross - total_subsidy  # 補助金後

    # === ファイナンス構造 ===
    equity_ratio = equity_ratio_pct / 100.0
    equity = net_capex * equity_ratio
    debt = net_capex * (1 - equity_ratio)
    annual_loan_payment = calc_loan_payment(debt, loan_interest_pct, loan_years)

    # === O&M ===
    # PV分は常にCAPEX比（om_ratio_pct）
    pv_annual_om = pv_capex * (om_ratio_pct / 100.0)
    # 蓄電池分はモードに依存（蓄電池なし=bat_capex=0 の場合はゼロ）
    if bat_capex <= 0:
        bat_annual_om = 0.0
    elif bat_om_mode == "PCS_kW建て（三菱総研試算）":
        bat_annual_om = bat_max_charge_kw * om_bat_per_kw_pcs
    else:
        # CAPEX比モード（従来動作）
        bat_annual_om = bat_capex * (om_ratio_pct / 100.0)
    annual_om = pv_annual_om + bat_annual_om

    # === 年次キャッシュフロー構築 ===
    # 各フェーズの「蓄電池あり収益 − 蓄電池なし収益」= アービトラージ価値（劣化に応じて減少）
    # LP最適化は1年分のJEPX価格を完全予見する理論上限のため、実運用の実現率で割り引く
    # （ベースラインのJEPX直売収入には適用しない。蓄電池の増分収益にのみ効かせる）。
    realization_factor = arbitrage_realization_rate_pct / 100.0
    arbitrage_initial_premium = (
        annual_revenue_with_bat - annual_revenue_without_bat
    ) * realization_factor
    arbitrage_initial_nopremium = (
        annual_revenue_with_bat_no_premium - annual_revenue_without_bat_no_premium
    ) * realization_factor
    # PV年間劣化（線形）: 年収益全体（ベース売電＋蓄電池アービトラージ）に乗算する。
    # 蓄電池アービトラージ自体もPV発電量に規模が連動するため、劣化を通しで乗じる近似とする
    # （PV劣化ごとにLPを再実行するのは計算コスト上非現実的なための簡略化）。

    rows = []  # 各年の {year, revenue, om, debt_service, ebitda, net_cf, project_cf, cum_project, cum_net}

    # Year 0: 投資（補助金後）
    project_cf_0 = -net_capex  # Project CF (無借入仮定)
    equity_cf_0 = -equity      # Equity CF（自己資本投下のみ）
    rows.append({
        "year": 0,
        "revenue": 0.0,
        "om": 0.0,
        "debt_service": 0.0,
        "battery_replace": 0.0,
        "decom_cost": 0.0,
        "ebitda": 0.0,
        "initial_capex": project_cf_0,  # 投資CF（負値）。チャート描画用
        "net_cf": equity_cf_0,
        "project_cf": project_cf_0,
        "cum_project": project_cf_0,
        "cum_net": equity_cf_0,
    })

    bat_replace_unit_ratio = bat_replace_cost_ratio_pct / 100.0
    cum_proj = project_cf_0
    cum_net = equity_cf_0
    bat_active = (bat_capex > 0)

    for y in range(1, irr_period_years + 1):
        # 蓄電池の有効容量（線形劣化）
        if bat_active:
            life_used = (y - 1) % bat_life_years  # 0..life-1
            degrade_factor = max(0.0, 1.0 - bat_degrade_pct_per_year / 100.0 * life_used)
        else:
            degrade_factor = 0.0

        # PVの経年劣化（線形、既経過年数を含めた実年齢で評価）
        pv_age = pv_start_age_years + (y - 1)
        pv_degrade_factor = max(0.0, 1.0 - pv_degrade_pct_per_year / 100.0 * pv_age)

        # その年の収益: フェーズに応じてプレミアム期/プレミアム終了後 を選択
        in_premium_phase = (y <= fip_premium_years)
        if in_premium_phase:
            base_rev = annual_revenue_without_bat
            arb_init = arbitrage_initial_premium
        else:
            base_rev = annual_revenue_without_bat_no_premium
            arb_init = arbitrage_initial_nopremium

        if bat_active:
            year_revenue = (base_rev + arb_init * degrade_factor) * pv_degrade_factor
        else:
            year_revenue = base_rev * pv_degrade_factor

        # 蓄電池寿命到来の処理
        bat_replace_cost = 0.0
        if bat_active and (y % bat_life_years == 0) and y < irr_period_years:
            # 寿命の最終年。翌年から「交換」or「終了」
            if bat_eol_action == "終了（蓄電池なし運用）":
                # 翌年以降は蓄電池なし
                bat_active = False
            else:
                # 交換: 翌年を新品スタートとして再投資
                bat_replace_cost = bat_capex * bat_replace_unit_ratio

        # O&M（蓄電池が無効化された後はPV分のみ）
        if bat_active:
            year_om = pv_annual_om + bat_annual_om
        else:
            year_om = pv_annual_om

        # 借入返済（loan_years まで）
        year_debt_service = annual_loan_payment if y <= loan_years else 0.0

        # 廃止措置費用（最終年に計上）
        decom_cost = 0.0
        if y == irr_period_years and decom_pct > 0:
            decom_cost = total_capex_gross * (decom_pct / 100.0)

        # EBITDA = 収益 − O&M
        ebitda = year_revenue - year_om

        # Project CF (無借入) = EBITDA − 蓄電池交換投資 − 廃止措置費用
        project_cf = ebitda - bat_replace_cost - decom_cost
        # Net CF (借入返済後) = Project CF − 借入返済
        net_cf = project_cf - year_debt_service

        cum_proj += project_cf
        cum_net += net_cf

        rows.append({
            "year": y,
            "revenue": year_revenue,
            "om": year_om,
            "debt_service": year_debt_service,
            "battery_replace": bat_replace_cost,
            "decom_cost": decom_cost,
            "ebitda": ebitda,
            "net_cf": net_cf,
            "project_cf": project_cf,
            "cum_project": cum_proj,
            "cum_net": cum_net,
        })

    # === Project IRR ===
    project_cfs = [r["project_cf"] for r in rows]
    project_irr = calc_irr(project_cfs)

    # === Project NPV（割引率=借入金利をWACC近似として使用） ===
    # NPVは「最適容量探索」の判定指標として機能する
    project_npv = calc_npv(project_cfs, loan_interest_pct)

    # === 投資回収年数（Project CF累計がゼロを超える年） ===
    payback_year = None
    for r in rows[1:]:
        if r["cum_project"] >= 0:
            payback_year = r["year"]
            break

    # === 平均年間EBITDA（参考） ===
    ebitda_list = [r["ebitda"] for r in rows[1:]]
    avg_ebitda = sum(ebitda_list) / len(ebitda_list) if ebitda_list else 0

    return {
        "rows": rows,
        "total_capex_gross": total_capex_gross,
        "total_subsidy": total_subsidy,
        "net_capex": net_capex,
        "equity": equity,
        "debt": debt,
        "annual_loan_payment": annual_loan_payment,
        "annual_om": annual_om,
        "project_irr": project_irr,
        "project_npv": project_npv,
        "payback_year": payback_year,
        "avg_ebitda": avg_ebitda,
        "arbitrage_realization_rate_pct": arbitrage_realization_rate_pct,
        # 実現率適用前（LP理論値）/ 適用後（キャッシュフローに実際使われた値）を透明化
        "arbitrage_value_theoretical_yen": annual_revenue_with_bat - annual_revenue_without_bat,
        "arbitrage_value_realized_yen": arbitrage_initial_premium,
    }


def build_fit_continuation_cashflow(
    annual_gen_kwh,
    fit_tariff,
    annual_revenue_jepx_direct_no_premium,
    fit_remaining_years,
    irr_period_years,
    pv_capex_sunk,
    om_ratio_pct,
    fit_curtailment_rate_pct=0.0,
    pv_degrade_pct_per_year=0.0,
    pv_start_age_years=0,
):
    """ケースB比較用: 「FIP転しない（FIT継続）」シナリオの20年間CF。

    Phase 1 (year 1..fit_remaining_years): FIT単価で買取り（蓄電池なし）
    Phase 2 (year (fit_remaining_years+1)..irr_period_years):
             FIT満了後はJEPX直売（蓄電池なし、プレミアム無し）

    Args:
        annual_gen_kwh: 年間発電量 [kWh/年]（出力制御なし）
        fit_tariff: FIT単価 [円/kWh]
        annual_revenue_jepx_direct_no_premium: FIT満了後のJEPX直売収益 [円/年]
        fit_remaining_years: FIT残存年数 [年]
        irr_period_years: P-IRR計算期間 [年]
        pv_capex_sunk: PV既投資額（O&M計算のみに使用）[円]
        om_ratio_pct: O&M費率 [%/年]（PV CAPEX比）
        fit_curtailment_rate_pct: FIT期間中の出力制御率 [%]（CLAUDE.md §4-6）。
            デフォルト0=FIT優先で制御されない前提。年間発電量に対する単純な
            エネルギー損失率として適用（時間帯別プロファイルは持たない集計値のため）。
        pv_degrade_pct_per_year: PV年間劣化率 [%/年]（線形）
        pv_start_age_years: このキャッシュフローの年1が始まる時点でのPVの既経過年数
            （FIP転せずFIT継続する場合も、その時点でのPVの実年齢を渡す）

    Returns:
        dict: FIT継続CF（行リストと20年累計CF）
    """
    pv_om = pv_capex_sunk * (om_ratio_pct / 100.0)
    fit_export_factor = 1.0 - float(fit_curtailment_rate_pct) / 100.0
    rows = []
    rows.append({"year": 0, "revenue": 0.0, "om": 0.0, "ebitda": 0.0, "cum": 0.0})
    cum = 0.0
    for y in range(1, irr_period_years + 1):
        pv_age = pv_start_age_years + (y - 1)
        pv_degrade_factor = max(0.0, 1.0 - pv_degrade_pct_per_year / 100.0 * pv_age)
        if y <= fit_remaining_years:
            rev = annual_gen_kwh * fit_export_factor * fit_tariff * pv_degrade_factor  # FIT期間
        else:
            rev = annual_revenue_jepx_direct_no_premium * pv_degrade_factor  # FIT満了後はJEPX直売
        ebitda = rev - pv_om
        cum += ebitda
        rows.append({
            "year": y,
            "revenue": rev,
            "om": pv_om,
            "ebitda": ebitda,
            "cum": cum,
        })
    return {
        "rows": rows,
        "cumulative_20y": cum,
        "pv_annual_om": pv_om,
    }


def build_caseb_display_rows(
    cf_rows,
    fip_transition_year,
    fit_tariff,
    annual_gen_kwh,
    pv_acquisition_cost,
    om_ratio_pct,
    bat_net_capex,
    fit_curtailment_rate_pct=0.0,
    pv_degrade_pct_per_year=0.0,
):
    """ケースB（既存FIT→FIP転）のCFチャート表示用の年次行を構築する。

    x軸 = プラント運転年（0 .. fip_transition_year + irr_period_years - 1）。
    オーナー視点で発電所の全ライフサイクル CF を可視化する。

    構成:
      Plant year 0:
        PV 初期投資のみ（-pv_acquisition_cost）
      Plant year 1..(fip_transition_year - 1):
        FIT 売電フェーズ。年間 CF = annual_gen × fit_tariff − pv_om
      Plant year fip_transition_year:
        蓄電池投資（initial_capex = -bat_net_capex）+ FIP+蓄電池の最初の運営年
        運営数値は cf_rows[1] を流用（PV O&M を上乗せ）
      Plant year (fip_transition_year+1)..(fip_transition_year + N - 1):
        FIP+蓄電池運営。cf_rows[2..N] を 1 年ずつシフトして流用

    Note:
      IRR/NPV/payback はこの関数では再計算しない（呼び出し側で増分CFベースで算出済み）。
      これは純粋に「チャート表示用」のロウ列。
    """
    pv_om = pv_acquisition_cost * (om_ratio_pct / 100.0)
    fit_export_factor = 1.0 - float(fit_curtailment_rate_pct) / 100.0
    rows = []

    # --- Plant year 0: PV 初期投資 ---
    cum = -pv_acquisition_cost
    cum_net = -pv_acquisition_cost  # 借入返済後の累計（project累計とは別に積算）
    rows.append({
        "year": 0,
        "revenue": 0.0,
        "om": 0.0,
        "debt_service": 0.0,
        "battery_replace": 0.0,
        "decom_cost": 0.0,
        "ebitda": 0.0,
        "initial_capex": -pv_acquisition_cost,
        "net_cf": -pv_acquisition_cost,
        "project_cf": -pv_acquisition_cost,
        "cum_project": cum,
        "cum_net": cum_net,
    })

    # --- Plant year 1..(fip_transition_year - 1): FIT phase ---
    for py in range(1, int(fip_transition_year)):
        pv_degrade_factor = max(0.0, 1.0 - pv_degrade_pct_per_year / 100.0 * (py - 1))
        rev = annual_gen_kwh * fit_export_factor * float(fit_tariff) * pv_degrade_factor
        ebitda = rev - pv_om
        cum += ebitda
        cum_net += ebitda
        rows.append({
            "year": py,
            "revenue": rev,
            "om": pv_om,
            "debt_service": 0.0,
            "battery_replace": 0.0,
            "decom_cost": 0.0,
            "ebitda": ebitda,
            "initial_capex": 0.0,
            "net_cf": ebitda,
            "project_cf": ebitda,
            "cum_project": cum,
            "cum_net": cum_net,
        })

    # --- Plant year fip_transition_year: 蓄電池投資 + 最初のFIP+蓄電池運営年 ---
    n_op_years = len(cf_rows) - 1  # cf_rows[0]=invest, [1..N]=ops
    if n_op_years >= 1:
        op1 = cf_rows[1]
        rev1 = op1["revenue"]
        om1 = op1["om"] + pv_om  # cf_rows は pv_capex=0 で算出 → PV O&M を加算
        ebitda1 = rev1 - om1
        debt1 = op1["debt_service"]
        bat_repl1 = op1.get("battery_replace", 0.0)
        decom1 = op1.get("decom_cost", 0.0)
        project_cf_op1 = ebitda1 - bat_repl1 - decom1
        project_cf_total = -bat_net_capex + project_cf_op1
        net_cf_total = project_cf_total - debt1
        cum += project_cf_total
        cum_net += net_cf_total
        rows.append({
            "year": int(fip_transition_year),
            "revenue": rev1,
            "om": om1,
            "debt_service": debt1,
            "battery_replace": bat_repl1,
            "decom_cost": decom1,
            "ebitda": ebitda1,
            "initial_capex": -bat_net_capex,
            "net_cf": net_cf_total,
            "project_cf": project_cf_total,
            "cum_project": cum,
            "cum_net": cum_net,
        })

    # --- Plant year (fip_transition_year+1)..: FIP+蓄電池の残り運営年 ---
    for op_idx in range(2, n_op_years + 1):
        op_row = cf_rows[op_idx]
        py = int(fip_transition_year) + op_idx - 1
        rev = op_row["revenue"]
        om = op_row["om"] + pv_om
        ebitda = rev - om
        debt = op_row["debt_service"]
        bat_repl = op_row.get("battery_replace", 0.0)
        decom = op_row.get("decom_cost", 0.0)
        project_cf = ebitda - bat_repl - decom
        net_cf = project_cf - debt
        cum += project_cf
        cum_net += net_cf
        rows.append({
            "year": py,
            "revenue": rev,
            "om": om,
            "debt_service": debt,
            "battery_replace": bat_repl,
            "decom_cost": decom,
            "ebitda": ebitda,
            "initial_capex": 0.0,
            "net_cf": net_cf,
            "project_cf": project_cf,
            "cum_project": cum,
            "cum_net": cum_net,
        })

    return rows


def apply_caseb_incremental_metrics(cf_result, fit_continuation_cf, loan_interest_pct):
    """ケースB: 増分CF「FIP転+蓄電池 − FIT継続」でIRR/NPV/回収年数を再計算しcf_resultを更新する。

    前提:
      - cf_result は pv_capex=0 で構築済み（project_cf = FIP収益 − 蓄電池O&M − 交換 − 廃止措置）
      - PV O&M は両シナリオで同額発生し相殺されるため、FIT継続側は「収益のみ」控除する
    副作用:
      - cf_result["rows"] 各行に incremental_project_cf を追加
      - cf_result の project_irr / project_npv / payback_year を増分CFベースで上書き
    Returns:
      incremental_project_cfs: list[float]
    """
    incremental_project_cfs = []
    for y_idx, row in enumerate(cf_result["rows"]):
        if y_idx == 0:
            # Year 0: 投資額（蓄電池のみ、補助後）
            incremental_project_cfs.append(row["project_cf"])
        else:
            fit_row = fit_continuation_cf["rows"][y_idx]
            # FIT継続の「収益のみ」を控除（PV O&M は相殺するため控除しない）
            inc_pcf = row["project_cf"] - fit_row["revenue"]
            incremental_project_cfs.append(inc_pcf)
        # 行に増分CFを保存（グラフ/結果表示用）
        row["incremental_project_cf"] = incremental_project_cfs[y_idx]

    # IRR/NPV/payback を増分CFで再計算
    cf_result["project_irr"] = calc_irr(incremental_project_cfs)
    cf_result["project_npv"] = calc_npv(incremental_project_cfs, float(loan_interest_pct))
    # payback: 増分project_cfの累計 >= 0 となる最初の年
    cum_inc = 0.0
    payback_inc = None
    for y_idx, pcf in enumerate(incremental_project_cfs):
        if y_idx == 0:
            cum_inc = pcf
            continue
        cum_inc += pcf
        if cum_inc >= 0:
            payback_inc = y_idx
            break
    cf_result["payback_year"] = payback_inc
    return incremental_project_cfs


def grid_search_capacity_pirr(
    base_capacity_kwh, n_steps,
    generation_30min, jepx_prices, month_day,
    max_charge_kw, max_discharge_kw,
    eff_charge_pct, eff_discharge_pct,
    soc_min_pct, soc_max_pct,
    premium, nonfossil_price, bg_fee,
    curtail_prob,
    pv_capex, bat_cost_per_kwh, subsidy_pv, subsidy_bat_pct,
    annual_revenue_without_bat,
    om_ratio_pct, equity_ratio_pct,
    loan_interest_pct, loan_years, irr_period_years,
    bat_life_years, bat_degrade_pct_per_year, bat_eol_action,
    bat_replace_cost_ratio_pct,
    bat_om_mode="CAPEX比（PVと共通）",
    om_bat_per_kw_pcs=5_000.0,
    decom_pct=0.0,
    fip_premium_years=None,
    annual_revenue_without_bat_no_premium=None,
    annual_export_without_bat=None,
    pv_degrade_pct_per_year=0.0,
    pv_start_age_years=0,
    arbitrage_realization_rate_pct=100.0,
):
    """段階2: グリッドサーチで容量ごとの P-IRR / NPV を計算。

    base_capacity_kwh の前後に広めのステップを切り、各容量で LP→経済性 を実行する。
    最適容量の判定は **NPV最大点** で行う（IRRは容量増加に対して単調減少する傾向があり、
    「限界IRR ≥ WACC なら追加投資は価値あり」という経済学原則を反映するにはNPVが適切）。
    """
    # 段階1の推定を中心に広めにサンプリング（0〜段階1の3倍を標準）
    cap_max = max(base_capacity_kwh * 3.0, 500.0)
    coarse_grid = list(np.linspace(0, cap_max, n_steps + 1))
    # 段階1の推定を必ず含める（精度向上）
    if base_capacity_kwh > 0 and base_capacity_kwh not in coarse_grid:
        coarse_grid.append(base_capacity_kwh)
    coarse_grid = sorted(set([round(c, 2) for c in coarse_grid]))
    capacities = list(coarse_grid)

    results = []
    # 評価済み容量を記録（リファインメント時の重複回避用）
    evaluated_caps = set()

    # プレミアム期間終了後の「蓄電池なし」収益をデフォルト化
    if annual_revenue_without_bat_no_premium is None:
        annual_revenue_without_bat_no_premium = annual_revenue_without_bat

    def _compute_no_premium_rev(opt_revenue_premium, annual_export):
        """LP schedule は premium に依存しないので、export × premium を引くだけで良い。"""
        return opt_revenue_premium - annual_export * premium

    for cap in capacities:
        try:
            if cap <= 0:
                # 蓄電池なしのケース: 収益はベースラインそのまま
                opt_revenue = annual_revenue_without_bat
                opt_revenue_nopremium = annual_revenue_without_bat_no_premium
                bat_capex_gross = 0.0
            else:
                opt = optimize_battery_fip(
                    generation_30min, jepx_prices, month_day,
                    capacity_kwh=float(cap),
                    max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
                    eff_charge_pct=eff_charge_pct, eff_discharge_pct=eff_discharge_pct,
                    soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
                    premium=premium, nonfossil_price=nonfossil_price, bg_fee=bg_fee,
                    curtail_prob=curtail_prob,
                )
                opt_revenue = opt["annual_revenue"]
                opt_revenue_nopremium = _compute_no_premium_rev(
                    opt_revenue, opt["annual_export"]
                )
                bat_capex_gross = float(cap) * bat_cost_per_kwh

            subsidy_bat = bat_capex_gross * subsidy_bat_pct / 100.0
            cf = build_cashflow(
                pv_capex=pv_capex, bat_capex=bat_capex_gross,
                subsidy_pv=subsidy_pv, subsidy_bat=subsidy_bat,
                annual_revenue_with_bat=opt_revenue,
                annual_revenue_without_bat=annual_revenue_without_bat,
                om_ratio_pct=om_ratio_pct, equity_ratio_pct=equity_ratio_pct,
                loan_interest_pct=loan_interest_pct, loan_years=loan_years,
                irr_period_years=irr_period_years,
                bat_life_years=bat_life_years,
                bat_degrade_pct_per_year=bat_degrade_pct_per_year,
                bat_eol_action=bat_eol_action,
                bat_replace_cost_ratio_pct=bat_replace_cost_ratio_pct,
                bat_om_mode=bat_om_mode,
                om_bat_per_kw_pcs=om_bat_per_kw_pcs,
                bat_max_charge_kw=max_charge_kw,
                decom_pct=decom_pct,
                fip_premium_years=fip_premium_years,
                annual_revenue_with_bat_no_premium=opt_revenue_nopremium,
                annual_revenue_without_bat_no_premium=annual_revenue_without_bat_no_premium,
                pv_degrade_pct_per_year=pv_degrade_pct_per_year,
                pv_start_age_years=pv_start_age_years,
                arbitrage_realization_rate_pct=arbitrage_realization_rate_pct,
            )
            results.append({
                "capacity": float(cap),
                "annual_revenue": opt_revenue,
                "project_irr": cf["project_irr"],
                "project_npv": cf["project_npv"],
                "payback_year": cf["payback_year"],
                "net_capex": cf["net_capex"],
            })
            evaluated_caps.add(round(float(cap), 2))
        except Exception:
            continue

    # === リファインメント: 粗グリッドの NPV最大点の前後を2回細密化 ===
    # 各イテレーションでNPV最大点の左右の中間点を追加評価し、最適容量の精度を上げる
    def _eval_capacity(cap):
        """単一容量でLP+CF計算して結果dictを返す（例外時はNone）。"""
        try:
            if cap <= 0:
                opt_revenue = annual_revenue_without_bat
                opt_revenue_nopremium = annual_revenue_without_bat_no_premium
                bat_capex_gross = 0.0
            else:
                opt = optimize_battery_fip(
                    generation_30min, jepx_prices, month_day,
                    capacity_kwh=float(cap),
                    max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
                    eff_charge_pct=eff_charge_pct, eff_discharge_pct=eff_discharge_pct,
                    soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
                    premium=premium, nonfossil_price=nonfossil_price, bg_fee=bg_fee,
                    curtail_prob=curtail_prob,
                )
                opt_revenue = opt["annual_revenue"]
                opt_revenue_nopremium = _compute_no_premium_rev(
                    opt_revenue, opt["annual_export"]
                )
                bat_capex_gross = float(cap) * bat_cost_per_kwh

            subsidy_bat = bat_capex_gross * subsidy_bat_pct / 100.0
            cf = build_cashflow(
                pv_capex=pv_capex, bat_capex=bat_capex_gross,
                subsidy_pv=subsidy_pv, subsidy_bat=subsidy_bat,
                annual_revenue_with_bat=opt_revenue,
                annual_revenue_without_bat=annual_revenue_without_bat,
                om_ratio_pct=om_ratio_pct, equity_ratio_pct=equity_ratio_pct,
                loan_interest_pct=loan_interest_pct, loan_years=loan_years,
                irr_period_years=irr_period_years,
                bat_life_years=bat_life_years,
                bat_degrade_pct_per_year=bat_degrade_pct_per_year,
                bat_eol_action=bat_eol_action,
                bat_replace_cost_ratio_pct=bat_replace_cost_ratio_pct,
                bat_om_mode=bat_om_mode,
                om_bat_per_kw_pcs=om_bat_per_kw_pcs,
                bat_max_charge_kw=max_charge_kw,
                decom_pct=decom_pct,
                fip_premium_years=fip_premium_years,
                annual_revenue_with_bat_no_premium=opt_revenue_nopremium,
                annual_revenue_without_bat_no_premium=annual_revenue_without_bat_no_premium,
                pv_degrade_pct_per_year=pv_degrade_pct_per_year,
                pv_start_age_years=pv_start_age_years,
                arbitrage_realization_rate_pct=arbitrage_realization_rate_pct,
            )
            return {
                "capacity": float(cap),
                "annual_revenue": opt_revenue,
                "project_irr": cf["project_irr"],
                "project_npv": cf["project_npv"],
                "payback_year": cf["payback_year"],
                "net_capex": cf["net_capex"],
            }
        except Exception:
            return None

    # 2回のリファインメントイテレーション
    for _iter in range(2):
        if len(results) < 3:
            break
        sorted_results = sorted(results, key=lambda r: r["capacity"])
        best_idx = max(
            range(len(sorted_results)),
            key=lambda i: (sorted_results[i].get("project_npv") or -float("inf")),
        )
        refine_caps = []
        if best_idx > 0:
            left = sorted_results[best_idx - 1]["capacity"]
            mid_left = (left + sorted_results[best_idx]["capacity"]) / 2
            refine_caps.append(mid_left)
        if best_idx < len(sorted_results) - 1:
            right = sorted_results[best_idx + 1]["capacity"]
            mid_right = (sorted_results[best_idx]["capacity"] + right) / 2
            refine_caps.append(mid_right)

        added = 0
        for cap in refine_caps:
            key = round(cap, 2)
            if key in evaluated_caps or cap < 0:
                continue
            res = _eval_capacity(cap)
            if res is not None:
                results.append(res)
                evaluated_caps.add(key)
                added += 1
        if added == 0:
            break  # 新規追加がなければ打ち切り

    # 容量順にソートして返す（グラフ描画用）
    results.sort(key=lambda r: r["capacity"])
    return results


# ============================================================
# グラフ生成
# ============================================================

def make_monthly_chart(result, opt_result, baseline_result):
    """月別 発電量 vs 売電量（蓄電池あり/なし）の棒グラフ。"""
    months = list(range(1, 13))
    month_labels = [f"{m}月" for m in months]
    gen_values = [result["monthly"].get(m, 0) for m in months]
    base_export = [baseline_result["monthly_export"].get(m, 0) for m in months]
    opt_export = [opt_result["monthly_export"].get(m, 0) for m in months]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=month_labels, y=gen_values,
        name="PV発電量", marker_color="orange",
        text=[f"{v:.0f}" for v in gen_values], textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=month_labels, y=base_export,
        name="売電量（蓄電池なし）", marker_color="lightsteelblue",
        text=[f"{v:.0f}" for v in base_export], textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=month_labels, y=opt_export,
        name="売電量（蓄電池あり）", marker_color="seagreen",
        text=[f"{v:.0f}" for v in opt_export], textposition="outside",
    ))

    fig.update_layout(
        title="月別 発電量 vs 売電量（蓄電池あり/なし比較）",
        xaxis_title="月", yaxis_title="電力量 [kWh]",
        barmode="group",
        template="plotly_white", height=600,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def make_daily_chart(result, opt_result, jepx_prices, month, day):
    """指定日の48コマ運用グラフ（PV発電・充放電・売電・SOC・JEPX価格）。"""
    month_day = result["month_day"]
    gen = result["total_gen_clipped"]

    target_idx = None
    for i, (m, d) in enumerate(month_day):
        if m == month and d == day:
            target_idx = i
            break

    if target_idx is None:
        fig = go.Figure()
        fig.update_layout(title=f"{month}月{day}日のデータがありません")
        return fig

    times = [f"{h}:{m:02d}" for h in range(24) for m in (0, 30)]

    gen_day = gen[target_idx]
    export_day = opt_result["export"][target_idx]
    charge_day = opt_result["battery_charge"][target_idx]
    discharge_day = opt_result["battery_discharge"][target_idx]
    soc_day = opt_result["soc"][target_idx]
    capacity = opt_result.get("battery_capacity", 1)
    soc_pct = soc_day / capacity * 100 if capacity > 0 else np.zeros_like(soc_day)
    price_day = jepx_prices[target_idx]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=(
            "PV発電・充放電・売電 [kWh/30分]",
            "SOC [%] と JEPX価格 [円/kWh]",
        ),
        specs=[[{"secondary_y": False}], [{"secondary_y": True}]],
    )

    # 上段: 電力量
    fig.add_trace(go.Scatter(
        x=times, y=gen_day, mode="lines",
        line=dict(color="orange", width=2), name="PV発電",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=times, y=export_day, mode="lines",
        line=dict(color="seagreen", width=2), name="売電",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=times, y=charge_day, name="充電",
        marker_color="rgba(70, 130, 180, 0.6)",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=times, y=-discharge_day, name="放電",
        marker_color="rgba(220, 20, 60, 0.6)",
    ), row=1, col=1)

    # 下段: SOC（左軸）と JEPX価格（右軸）
    fig.add_trace(go.Scatter(
        x=times, y=soc_pct, mode="lines",
        line=dict(color="purple", width=2), name="SOC [%]",
    ), row=2, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(
        x=times, y=price_day, mode="lines",
        line=dict(color="darkblue", width=2, dash="dot"), name="JEPX価格",
    ), row=2, col=1, secondary_y=True)

    fig.update_xaxes(tickangle=45, dtick=4, row=2, col=1)
    fig.update_yaxes(title_text="電力量 [kWh/30分]", row=1, col=1)
    fig.update_yaxes(title_text="SOC [%]", range=[0, 100], row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="JEPX [円/kWh]", row=2, col=1, secondary_y=True)

    fig.update_layout(
        title=f"{month}月{day}日 蓄電池運用（30分単位）",
        template="plotly_white", height=700,
        legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="right", x=1),
        barmode="relative",
    )
    return fig


def make_cashflow_chart(cf_result):
    """年次キャッシュフロー（棒）＋累計CF（線）の2軸グラフ。"""
    rows = cf_result["rows"]
    years = [r["year"] for r in rows]
    revenue = [r["revenue"] / 10000 for r in rows]               # 万円
    om_neg = [-r["om"] / 10000 for r in rows]
    debt_neg = [-r["debt_service"] / 10000 for r in rows]
    bat_replace_neg = [-r["battery_replace"] / 10000 for r in rows]
    project_cf = [r["project_cf"] / 10000 for r in rows]
    net_cf = [r["net_cf"] / 10000 for r in rows]
    cum_project = [r["cum_project"] / 10000 for r in rows]
    cum_net = [r["cum_net"] / 10000 for r in rows]

    # 初期投資（マイナスCF）— ケースA: Year 0のみ。ケースB: PV(Year 0)+蓄電池(FIP転年)の2回
    invest_neg = [r.get("initial_capex", 0.0) / 10000 for r in rows]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # 投資（Year 0のみ）
    fig.add_trace(go.Bar(
        x=years, y=invest_neg, name="初期投資",
        marker_color="rgba(70, 70, 70, 0.85)",
    ), secondary_y=False)
    # 年次収益
    fig.add_trace(go.Bar(
        x=years, y=revenue, name="売電収入",
        marker_color="rgba(46, 139, 87, 0.85)",
    ), secondary_y=False)
    # O&M
    fig.add_trace(go.Bar(
        x=years, y=om_neg, name="O&M費",
        marker_color="rgba(255, 165, 0, 0.85)",
    ), secondary_y=False)
    # 借入返済
    fig.add_trace(go.Bar(
        x=years, y=debt_neg, name="借入返済",
        marker_color="rgba(178, 34, 34, 0.85)",
    ), secondary_y=False)
    # 蓄電池交換投資
    fig.add_trace(go.Bar(
        x=years, y=bat_replace_neg, name="蓄電池交換",
        marker_color="rgba(128, 0, 128, 0.85)",
    ), secondary_y=False)

    # 累計Project CF
    fig.add_trace(go.Scatter(
        x=years, y=cum_project, name="累計Project CF",
        mode="lines+markers",
        line=dict(color="darkblue", width=3),
        marker=dict(size=8),
    ), secondary_y=True)
    # 累計Net CF
    fig.add_trace(go.Scatter(
        x=years, y=cum_net, name="累計Net CF（自己資本）",
        mode="lines+markers",
        line=dict(color="crimson", width=2, dash="dot"),
        marker=dict(size=6),
    ), secondary_y=True)

    # ゼロライン
    fig.add_hline(y=0, line_dash="solid", line_color="gray", line_width=1)

    fig.update_layout(
        title="年次キャッシュフロー＋累計CF推移",
        barmode="relative",
        template="plotly_white", height=600,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis=dict(title="年", dtick=1),
    )
    fig.update_yaxes(title_text="年次CF [万円]", secondary_y=False)
    fig.update_yaxes(title_text="累計CF [万円]", secondary_y=True)
    return fig


def make_capacity_search_chart(search_results, optimal_capacity):
    """蓄電池容量 vs NPV／IRR の最適容量探索カーブ。

    主指標: NPV（万円）— 経済学的に最適容量を決定する
    副指標: Project IRR（%）— 参考表示（容量増加に対して単調減少しがち）
    """
    if not search_results:
        fig = go.Figure()
        fig.update_layout(title="グリッドサーチ結果がありません")
        return fig

    caps = [r["capacity"] for r in search_results]
    npvs = [(r["project_npv"] / 10000) if r.get("project_npv") is not None else None for r in search_results]
    irrs = [(r["project_irr"] * 100) if r["project_irr"] is not None else None for r in search_results]
    paybacks = [r["payback_year"] for r in search_results]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # 主指標: NPV（左軸）
    fig.add_trace(go.Scatter(
        x=caps, y=npvs, name="Project NPV [万円]",
        mode="lines+markers",
        line=dict(color="darkgreen", width=3),
        marker=dict(size=9),
    ), secondary_y=False)

    # 副指標: IRR（右軸）
    fig.add_trace(go.Scatter(
        x=caps, y=irrs, name="Project IRR [%]（参考）",
        mode="lines+markers",
        line=dict(color="darkblue", width=2, dash="dot"),
        marker=dict(size=6),
    ), secondary_y=True)

    # 副指標: 投資回収年数（右軸）
    fig.add_trace(go.Scatter(
        x=caps, y=paybacks, name="投資回収年数 [年]",
        mode="lines+markers",
        line=dict(color="orange", width=2, dash="dash"),
        marker=dict(size=6),
        visible="legendonly",
    ), secondary_y=True)

    # 最適容量を縦線で表示（NPV最大点）
    if optimal_capacity is not None:
        label = f"最適 {optimal_capacity:.0f} kWh（NPV最大）"
        if optimal_capacity <= 0:
            label = "最適=蓄電池なし（NPV最大がcap=0）"
        fig.add_vline(
            x=max(optimal_capacity, 1),
            line_dash="dash", line_color="red",
            annotation_text=label,
            annotation_position="top right",
        )

    # NPVゼロライン
    fig.add_hline(y=0, line_dash="solid", line_color="gray", line_width=1)

    fig.update_layout(
        title="蓄電池容量 vs Project NPV（主指標）＋ IRR（参考）",
        template="plotly_white", height=600,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis=dict(title="蓄電池容量 [kWh]"),
    )
    fig.update_yaxes(title_text="Project NPV [万円]", secondary_y=False)
    fig.update_yaxes(title_text="IRR [%] / 回収年数 [年]", secondary_y=True)
    return fig


# ============================================================
# Gradio UI / 計算コールバック
# ============================================================

def run_simulation(
    station_choice, csv_file,
    KHD, KPD, KPM, KPA, eta_ino,
    alpha_pct, delta_t,
    bifacial_enabled, bifaciality_val, gcr_val, height_val, pitch_val,
    snow_albedo_enabled,
    bat_mode,
    bat_capacity, bat_max_charge, bat_max_discharge,
    bat_eff_charge, bat_eff_discharge,
    bat_soc_min, bat_soc_max,
    bat_degrade_pct, bat_life_years, bat_eol_action, bat_replace_cost_ratio,
    jepx_area, jepx_years,
    fip_base_price, nonfossil_price, bg_fee,
    annual_curtail_rate_pct,
    pv_cost_per_kw, bat_cost_per_kwh,
    subsidy_pv_pct, subsidy_bat_pct,
    om_ratio_pct, equity_ratio_pct,
    loan_interest_pct, loan_years, irr_period_years,
    capacity_search_steps,
    face_args,
    num_faces=1,
    display_month=7, display_day=15,
    bat_om_mode="CAPEX比（PVと共通）",
    om_bat_per_kw_pcs=5_000.0,
    decom_pct=0.0,
    # --- ケース切替（Phase 3） ---
    case_type="新規FIP",                  # "新規FIP" or "既存FIT→FIP転"
    fip_premium_years=20,                 # FIPプレミアム交付期間 [年]（新規FIP）
    fit_tariff=36.0,                      # FIT単価 [円/kWh]（ケースB）
    fit_term_years=20,                    # FIT契約期間 [年]（通常20）
    fip_transition_year=12,               # FIP転を実施する年（FIT開始から数えて）
    pv_acquisition_cost=0.0,              # PV取得価額 [円]（ケースB、サンクコスト）
    fit_curtailment_rate_pct=0.0,         # FIT期間中の出力制御率 [%]（ケースB、CLAUDE.md §4-6）
    pv_degrade_pct_per_year=DEFAULT_PV_DEGRADE_PCT_PER_YEAR,  # PV年間劣化率 [%/年]（線形）
    arbitrage_realization_rate_pct=DEFAULT_ARBITRAGE_REALIZATION_RATE_PCT,  # 蓄電池アービトラージ実現率 [%]
):
    """メイン計算コールバック"""
    try:
        # --- 気象データ読み込み ---
        if csv_file is not None:
            lat, lon, ghi_df, temp_df = load_from_csv(csv_file)
            source_text = "CSVアップロード"
            point_no = None
        elif station_choice:
            point_no = station_choice.split(" ")[0]
            lat, lon, ghi_df, temp_df = load_from_db(point_no)
            source_text = f"DB: {station_choice}"
        else:
            return None, None, None, None, "エラー: 地点を選択するかCSVをアップロードしてください", "", None

        # --- 面設定パース ---
        faces = []
        for i in range(int(num_faces)):
            idx = i * 5
            ppeak = float(face_args[idx]) if face_args[idx] else 0
            orientation = face_args[idx + 1] if face_args[idx + 1] else "南"
            azi_direct = face_args[idx + 2]
            tilt = float(face_args[idx + 3]) if face_args[idx + 3] else 30
            pcs_kw = float(face_args[idx + 4]) if face_args[idx + 4] else 0

            if azi_direct is not None and azi_direct != "":
                azimuth = float(azi_direct) % 360
            else:
                azimuth = float(ORIENTATION_TO_AZIMUTH.get(orientation, 180))

            if ppeak > 0:
                faces.append({
                    "ppeak": ppeak,
                    "orientation": orientation,
                    "azimuth": azimuth,
                    "tilt": tilt,
                    "pcs_limit_kw": pcs_kw if pcs_kw > 0 else None,
                })

        if not faces:
            return None, None, None, None, "エラー: 有効な面設定がありません（Ppeak > 0の面が必要です）", "", None

        # --- 両面パネル: albedo時系列 ---
        albedo_flat = None
        if bifacial_enabled:
            if snow_albedo_enabled and csv_file is None and point_no:
                snow_df = load_snow_depth(point_no)
                albedo_flat = build_albedo_series(snow_df)
            else:
                albedo_flat = np.full(365 * 48, ALBEDO_NORMAL)

        # --- 発電量計算 ---
        result = calculate_generation(
            lat, lon, ghi_df, temp_df,
            faces, KHD, KPD, KPM, KPA, eta_ino,
            alpha_pct, delta_t,
            bifacial=bool(bifacial_enabled),
            bifaciality=float(bifaciality_val) if bifaciality_val is not None else BIFACIAL_DEFAULTS["bifaciality"],
            gcr=float(gcr_val) if gcr_val is not None else BIFACIAL_DEFAULTS["gcr"],
            height=float(height_val) if height_val is not None else BIFACIAL_DEFAULTS["height"],
            pitch=float(pitch_val) if pitch_val is not None else BIFACIAL_DEFAULTS["pitch"],
            albedo_flat=albedo_flat,
        )

        # --- JEPX価格読込 ---
        if not jepx_years:
            return None, None, None, None, "エラー: JEPX年度を1つ以上選択してください", "", None
        years_int = [int(y) for y in jepx_years]
        jepx_prices = load_jepx_prices(jepx_area, years_int, result["month_day"])

        # --- FIP実効プレミアムの算定 ---
        # 参照価格 = 選択エリア・年度のJEPX単純平均（公表される参照価格の簡易近似）。
        # 実効プレミアム = 基準価格 − 参照価格。ユーザーは公表済みの「基準価格」を
        # そのまま入力し、市場への上乗せ額はここで自動計算する
        # （基準価格をそのままプレミアムとして加算する誤りを構造的に防ぐ）。
        #
        # ゼロ下限クリップ: FIP制度では参照価格が基準価格を上回っても
        # プレミアムはゼロ止まりであり、事業者から差額を徴収する仕組みは存在しない。
        # 負のプレミアムは制度上あり得ないため max(0, ...) でクリップする。
        reference_price = float(np.mean(jepx_prices))
        fip_premium_raw = float(fip_base_price) - reference_price
        fip_premium_effective = max(0.0, fip_premium_raw)
        premium_clipped = fip_premium_raw < 0

        # --- 出力制御プロファイル（エネルギー重み付け） ---
        curtail_prob = build_curtail_prob_30min(
            result["month_day"], float(annual_curtail_rate_pct),
            generation_30min=result["total_gen_clipped"],
        )

        # --- ベースライン: 蓄電池なし（出力制御適用） ---
        baseline = baseline_no_battery(
            result["total_gen_clipped"], jepx_prices, result["month_day"],
            premium=fip_premium_effective,
            nonfossil_price=float(nonfossil_price),
            bg_fee=float(bg_fee),
            curtail_prob=curtail_prob,
        )

        # --- ケース別パラメータ（手動/最適容量探索 共通） ---
        is_fit_to_fip = (case_type == "既存FIT→FIP転")
        # PVの既経過年数（ケースB: FIP転時点で何年稼働済みか。ケースA: 新設のため0）
        pv_start_age_years = int(fip_transition_year) - 1 if is_fit_to_fip else 0
        if is_fit_to_fip:
            # ケースB: FIPプレミアム期間 = FIT残存期間
            effective_premium_years = max(
                0, int(fit_term_years) - int(fip_transition_year) + 1
            )
        else:
            effective_premium_years = int(fip_premium_years)

        # プレミアム終了後の「蓄電池なし」収益（LP schedule は premium に依存しないので減算のみ）
        baseline_no_prem_rev = (
            baseline["annual_revenue"] - baseline["annual_export"] * fip_premium_effective
        )

        # --- 蓄電池容量決定: 手動 or 最適容量探索 ---
        capacity_search_result = None
        used_capacity = float(bat_capacity)
        if bat_mode == "最適容量探索":
            # 段階1: LP一体化で粗い最適容量を求める
            cap_upper = float(bat_capacity) * 3.0  # 探索上限
            opt_cap = optimize_capacity_fip(
                result["total_gen_clipped"], jepx_prices, result["month_day"],
                max_charge_kw=float(bat_max_charge),
                max_discharge_kw=float(bat_max_discharge),
                eff_charge_pct=float(bat_eff_charge),
                eff_discharge_pct=float(bat_eff_discharge),
                soc_min_pct=float(bat_soc_min),
                soc_max_pct=float(bat_soc_max),
                premium=fip_premium_effective,
                nonfossil_price=float(nonfossil_price),
                bg_fee=float(bg_fee),
                bat_cost_per_kwh_net=float(bat_cost_per_kwh) * (1 - float(subsidy_bat_pct) / 100.0),
                irr_period_years=int(irr_period_years),
                capacity_upper_kwh=cap_upper,
                curtail_prob=curtail_prob,
            )
            base_cap = opt_cap["optimal_capacity_kwh"]
            if base_cap < 1.0:
                base_cap = float(bat_capacity)  # フォールバック

            # 段階2: グリッドサーチでP-IRRカーブを描く
            total_ppeak_for_search = sum(f["ppeak"] for f in faces)
            pv_capex_for_search = total_ppeak_for_search * float(pv_cost_per_kw)
            subsidy_pv_for_search = pv_capex_for_search * float(subsidy_pv_pct) / 100.0

            # ケースB: PVはサンクコスト → pv_capex=0 でP-IRR計算
            if is_fit_to_fip:
                pv_capex_for_cf = 0.0
                subsidy_pv_for_cf = 0.0
            else:
                pv_capex_for_cf = pv_capex_for_search
                subsidy_pv_for_cf = subsidy_pv_for_search

            search_results = grid_search_capacity_pirr(
                base_capacity_kwh=base_cap, n_steps=int(capacity_search_steps),
                generation_30min=result["total_gen_clipped"],
                jepx_prices=jepx_prices, month_day=result["month_day"],
                max_charge_kw=float(bat_max_charge),
                max_discharge_kw=float(bat_max_discharge),
                eff_charge_pct=float(bat_eff_charge),
                eff_discharge_pct=float(bat_eff_discharge),
                soc_min_pct=float(bat_soc_min),
                soc_max_pct=float(bat_soc_max),
                premium=fip_premium_effective,
                nonfossil_price=float(nonfossil_price),
                bg_fee=float(bg_fee),
                curtail_prob=curtail_prob,
                pv_capex=pv_capex_for_cf,
                bat_cost_per_kwh=float(bat_cost_per_kwh),
                subsidy_pv=subsidy_pv_for_cf,
                subsidy_bat_pct=float(subsidy_bat_pct),
                annual_revenue_without_bat=baseline["annual_revenue"],
                om_ratio_pct=float(om_ratio_pct),
                equity_ratio_pct=float(equity_ratio_pct),
                loan_interest_pct=float(loan_interest_pct),
                loan_years=int(loan_years),
                irr_period_years=int(irr_period_years),
                bat_life_years=int(bat_life_years),
                bat_degrade_pct_per_year=float(bat_degrade_pct),
                bat_eol_action=bat_eol_action,
                bat_replace_cost_ratio_pct=float(bat_replace_cost_ratio),
                bat_om_mode=bat_om_mode,
                om_bat_per_kw_pcs=float(om_bat_per_kw_pcs),
                decom_pct=float(decom_pct),
                fip_premium_years=effective_premium_years,
                annual_revenue_without_bat_no_premium=baseline_no_prem_rev,
                pv_degrade_pct_per_year=float(pv_degrade_pct_per_year),
                pv_start_age_years=pv_start_age_years,
                arbitrage_realization_rate_pct=float(arbitrage_realization_rate_pct),
            )
            # NPV最大点を採用（IRRは容量に対して単調減少しがちなので経済学的にNPVが正しい）
            best = None
            for r in search_results:
                if r.get("project_npv") is None:
                    continue
                if best is None or r["project_npv"] > best["project_npv"]:
                    best = r
            used_capacity = best["capacity"] if best else base_cap
            # capacity=0 が最適となった場合は手動モードに準ずるため、最小サンプル点を採用しない
            if used_capacity <= 0:
                # NPVがcapacity=0で最大 → 蓄電池は経済的に不要
                used_capacity = 0.0
            capacity_search_result = {
                "search_results": search_results,
                "stage1_capacity": base_cap,
                "best_capacity": used_capacity,
            }

        # --- 蓄電池LP最適化（採用容量で本番計算） ---
        if used_capacity <= 0:
            # 最適容量=0 → 蓄電池なしが最適。baselineを流用して「opt_result」構造を作る
            zero = np.zeros_like(result["total_gen_clipped"])
            opt_result = {
                "battery_charge": zero.copy(),
                "battery_discharge": zero.copy(),
                "export": baseline["export"],
                "curtailment": baseline["curtailment"],
                "soc": zero.copy(),
                "revenue_30min": baseline["revenue_30min"],
                "annual_gen": baseline["annual_gen"],
                "annual_export": baseline["annual_export"],
                "annual_charge": 0.0,
                "annual_discharge": 0.0,
                "annual_curtail": baseline["annual_curtail"],
                "annual_curtail_forced": baseline["annual_curtail_forced"],
                "annual_curtail_economic": baseline["annual_curtail_economic"],
                "annual_revenue": baseline["annual_revenue"],
                "monthly_gen": baseline["monthly_gen"],
                "monthly_export": baseline["monthly_export"],
                "monthly_revenue": baseline["monthly_revenue"],
                "monthly_charge": {m: 0.0 for m in range(1, 13)},
                "monthly_discharge": {m: 0.0 for m in range(1, 13)},
                "battery_capacity": 0.0,
                "soc_min": 0.0,
                "soc_max": 0.0,
            }
        else:
            opt_result = optimize_battery_fip(
                result["total_gen_clipped"], jepx_prices, result["month_day"],
                capacity_kwh=used_capacity,
                max_charge_kw=float(bat_max_charge),
                max_discharge_kw=float(bat_max_discharge),
                eff_charge_pct=float(bat_eff_charge),
                eff_discharge_pct=float(bat_eff_discharge),
                soc_min_pct=float(bat_soc_min),
                soc_max_pct=float(bat_soc_max),
                premium=fip_premium_effective,
                nonfossil_price=float(nonfossil_price),
                bg_fee=float(bg_fee),
                curtail_prob=curtail_prob,
            )

        # --- 経済性計算 ---
        total_ppeak = sum(f["ppeak"] for f in faces)
        pv_capex_gross = total_ppeak * float(pv_cost_per_kw)
        bat_capex = used_capacity * float(bat_cost_per_kwh)

        # ケースB(FIT→FIP転): PVはサンクコスト → P-IRRはバッテリー単独
        if is_fit_to_fip:
            pv_capex_for_irr = 0.0
            subsidy_pv = 0.0
        else:
            pv_capex_for_irr = pv_capex_gross
            subsidy_pv = pv_capex_gross * float(subsidy_pv_pct) / 100.0
        subsidy_bat = bat_capex * float(subsidy_bat_pct) / 100.0

        # プレミアム終了後の「蓄電池あり」収益（LP schedule は premium に依存しない）
        opt_no_prem_rev = (
            opt_result["annual_revenue"] - opt_result["annual_export"] * fip_premium_effective
        )

        cf_result = build_cashflow(
            pv_capex=pv_capex_for_irr, bat_capex=bat_capex,
            subsidy_pv=subsidy_pv, subsidy_bat=subsidy_bat,
            annual_revenue_with_bat=opt_result["annual_revenue"],
            annual_revenue_without_bat=baseline["annual_revenue"],
            om_ratio_pct=float(om_ratio_pct),
            equity_ratio_pct=float(equity_ratio_pct),
            loan_interest_pct=float(loan_interest_pct),
            loan_years=int(loan_years),
            irr_period_years=int(irr_period_years),
            bat_life_years=int(bat_life_years),
            bat_degrade_pct_per_year=float(bat_degrade_pct),
            bat_eol_action=bat_eol_action,
            bat_replace_cost_ratio_pct=float(bat_replace_cost_ratio),
            bat_om_mode=bat_om_mode,
            om_bat_per_kw_pcs=float(om_bat_per_kw_pcs),
            bat_max_charge_kw=float(bat_max_charge),
            decom_pct=float(decom_pct),
            fip_premium_years=effective_premium_years,
            annual_revenue_with_bat_no_premium=opt_no_prem_rev,
            annual_revenue_without_bat_no_premium=baseline_no_prem_rev,
            pv_degrade_pct_per_year=float(pv_degrade_pct_per_year),
            pv_start_age_years=pv_start_age_years,
            arbitrage_realization_rate_pct=float(arbitrage_realization_rate_pct),
        )

        # --- ケースB: FIT継続シナリオの対比（常に計算） ---
        # Case B の P-IRR は「FIP転+蓄電池 vs FIT継続」の増分CFで評価すべき。
        # CLAUDE.md 仕様:「収益 = FIP転後の売電収入 − FIP転しなかった場合の売電収入（増分CF）」
        fit_continuation_cf = None
        if is_fit_to_fip:
            annual_gen_no_curtail = float(result["annual"])
            fit_remaining = max(0, int(fit_term_years) - int(fip_transition_year) + 1)
            # FIT満了後はJEPX直売（プレミアム無し、蓄電池なし）
            # pv_capex_sunk はO&M計算のみ（PV O&M は両シナリオで相殺されるので増分CFには寄与しない）
            fit_continuation_cf = build_fit_continuation_cashflow(
                annual_gen_kwh=annual_gen_no_curtail,
                fit_tariff=float(fit_tariff),
                annual_revenue_jepx_direct_no_premium=baseline_no_prem_rev,
                fit_remaining_years=fit_remaining,
                irr_period_years=int(irr_period_years),
                pv_capex_sunk=float(pv_acquisition_cost),
                om_ratio_pct=float(om_ratio_pct),
                fit_curtailment_rate_pct=float(fit_curtailment_rate_pct),
                pv_degrade_pct_per_year=float(pv_degrade_pct_per_year),
                pv_start_age_years=pv_start_age_years,
            )

            # === 増分CFに基づくProject IRR/NPV/回収年数の再計算 ===
            # （ロジック本体は apply_caseb_incremental_metrics に集約。MCPツールと共有）
            apply_caseb_incremental_metrics(
                cf_result, fit_continuation_cf, float(loan_interest_pct)
            )

        # --- グラフ生成 ---
        fig_monthly = make_monthly_chart(result, opt_result, baseline)
        fig_daily = make_daily_chart(
            result, opt_result, jepx_prices,
            int(display_month) if display_month is not None else 7,
            int(display_day) if display_day is not None else 15,
        )
        # ケースB は CF チャートをプラント運転年ベースで表示（オーナー視点）
        # IRR/NPV/payback は増分CFで計算済み（cf_result に格納済み）→ そのまま流用
        if is_fit_to_fip and float(pv_acquisition_cost) > 0:
            display_rows = build_caseb_display_rows(
                cf_rows=cf_result["rows"],
                fip_transition_year=int(fip_transition_year),
                fit_tariff=float(fit_tariff),
                annual_gen_kwh=float(result["annual"]),
                pv_acquisition_cost=float(pv_acquisition_cost),
                om_ratio_pct=float(om_ratio_pct),
                bat_net_capex=cf_result["net_capex"],
                fit_curtailment_rate_pct=float(fit_curtailment_rate_pct),
                pv_degrade_pct_per_year=float(pv_degrade_pct_per_year),
            )
            cf_for_chart = dict(cf_result)
            cf_for_chart["rows"] = display_rows
            fig_cashflow = make_cashflow_chart(cf_for_chart)
        else:
            fig_cashflow = make_cashflow_chart(cf_result)
        if capacity_search_result is not None:
            fig_capacity = make_capacity_search_chart(
                capacity_search_result["search_results"],
                capacity_search_result["best_capacity"],
            )
        else:
            fig_capacity = go.Figure()
            fig_capacity.update_layout(
                title="最適容量探索は「最適容量探索」モードでのみ実行されます",
                template="plotly_white", height=400,
            )

        # --- 結果テキスト ---
        avg_jepx = float(np.mean(jepx_prices))
        unit_avg = avg_jepx + fip_premium_effective + float(nonfossil_price) - float(bg_fee)

        result_text = "═══════════════════════════════════\n"
        result_text += " FIP転＋蓄電池 事業性シミュレーション\n"
        result_text += "═══════════════════════════════════\n\n"

        result_text += "── PV発電量 ──\n"
        result_text += f"設備容量: {total_ppeak:.1f} kW（{len(faces)}面）\n"
        result_text += f"年間発電量: {result['annual']:.0f} kWh/年\n"
        result_text += f"設備利用率: {result['annual'] / (total_ppeak * 8760) * 100:.1f}%\n"
        result_text += f"K' = {result['K_prime']:.4f}\n"
        result_text += f"PV年間劣化率（線形、キャッシュフローに反映）: {float(pv_degrade_pct_per_year):.2f}%/年\n"
        if pv_start_age_years > 0:
            result_text += f"  FIP転時点のPV既経過年数: {pv_start_age_years}年（劣化計算の起点に反映）\n"
        result_text += "\n"

        result_text += "── JEPX価格条件 ──\n"
        result_text += f"エリア: {jepx_area}\n"
        result_text += f"年度: {', '.join(str(y) for y in years_int)}（{len(years_int)}年度平均）\n"
        result_text += f"年平均価格: {avg_jepx:.2f} 円/kWh\n"
        result_text += f"FIP基準価格: {float(fip_base_price):.2f} 円/kWh\n"
        result_text += f"参照価格（算定、選択年度JEPX単純平均）: {reference_price:.2f} 円/kWh\n"
        result_text += f"実効プレミアム（基準価格−参照価格）: {fip_premium_effective:+.2f} 円/kWh\n"
        if premium_clipped:
            result_text += (
                f"  ⚠ 参照価格が基準価格を上回るため、プレミアムを0円/kWhに制限しました\n"
                f"    （制度上、参照価格超過分を事業者から徴収する仕組みはありません。"
                f"素の差分は {fip_premium_raw:+.2f} 円/kWh）\n"
            )
        result_text += f"非化石証書: {float(nonfossil_price):.2f} 円/kWh\n"
        result_text += f"BG手数料: -{float(bg_fee):.2f} 円/kWh\n"
        result_text += f"年平均売電単価: {unit_avg:.2f} 円/kWh\n\n"

        result_text += "── 出力制御・抑制 ──\n"
        result_text += f"年間制御率（設定）: {float(annual_curtail_rate_pct):.1f}%\n"
        base_forced = baseline.get("annual_curtail_forced", baseline["annual_curtail"])
        base_econ = baseline.get("annual_curtail_economic", 0.0)
        opt_forced = opt_result.get("annual_curtail_forced", opt_result.get("annual_curtail", 0.0))
        opt_econ = opt_result.get("annual_curtail_economic", 0.0)
        result_text += f"[強制出力制御] 蓄電池なし: {base_forced:.0f} kWh/年 → あり: {opt_forced:.0f} kWh/年\n"
        result_text += f"  出力制御回避量（蓄電池効果）: {base_forced - opt_forced:.0f} kWh/年\n"
        if base_econ > 0 or opt_econ > 0:
            result_text += f"[経済的自主抑制] 蓄電池なし: {base_econ:.0f} kWh/年 → あり: {opt_econ:.0f} kWh/年\n"
            result_text += "  ※売電単価が負のコマで出力を絞った量（強制制御とは別物）\n"
        result_text += "\n"

        result_text += "── 蓄電池なし（ベースライン） ──\n"
        result_text += f"年間売電量: {baseline['annual_export']:.0f} kWh/年\n"
        result_text += f"年間売電収入: {baseline['annual_revenue'] / 10000:.1f} 万円/年\n\n"

        result_text += "── 蓄電池あり（LP最適化） ──\n"
        if bat_mode == "最適容量探索":
            result_text += f"蓄電池容量（最適探索）: {used_capacity:.0f} kWh\n"
            if capacity_search_result:
                result_text += f"  段階1（LP一体化）: {capacity_search_result['stage1_capacity']:.0f} kWh\n"
                result_text += f"  段階2（NPV最大）: {capacity_search_result['best_capacity']:.0f} kWh\n"
        else:
            result_text += f"蓄電池容量（手動入力）: {used_capacity:.0f} kWh\n"
        result_text += f"年間充電量: {opt_result['annual_charge']:.0f} kWh/年\n"
        result_text += f"年間放電量: {opt_result['annual_discharge']:.0f} kWh/年\n"
        result_text += f"充放電損失: {opt_result['annual_charge'] - opt_result['annual_discharge']:.0f} kWh/年\n"
        result_text += f"年間売電量: {opt_result['annual_export']:.0f} kWh/年\n"
        result_text += f"年間売電収入: {opt_result['annual_revenue'] / 10000:.1f} 万円/年\n\n"

        # --- 蓄電池の増分価値 ---
        revenue_diff = opt_result['annual_revenue'] - baseline['annual_revenue']
        export_diff = opt_result['annual_export'] - baseline['annual_export']
        realization_rate = float(arbitrage_realization_rate_pct)
        revenue_diff_realized = revenue_diff * realization_rate / 100.0
        result_text += "── 蓄電池の増分価値 ──\n"
        result_text += f"売電量増減: {export_diff:+.0f} kWh/年\n"
        result_text += f"アービトラージ＋制御回避（LP理論値、完全予見）: {revenue_diff / 10000:+.1f} 万円/年\n"
        result_text += f"  実現率{realization_rate:.0f}%適用後（事業性計算に使用）: {revenue_diff_realized / 10000:+.1f} 万円/年\n"
        if used_capacity > 0:
            result_text += f"単位容量あたり（実現後）: {revenue_diff_realized / used_capacity:+.0f} 円/kWh/年\n"
        result_text += "\n"

        # --- 経済性 ---
        result_text += "── 事業ケース ──\n"
        result_text += f"ケース: {case_type}\n"
        if is_fit_to_fip:
            result_text += f"FIT契約期間: {int(fit_term_years)}年 / "
            result_text += f"FIP転実施年: {int(fip_transition_year)}年目 / "
            result_text += f"FIPプレミアム期間（FIT残存）: {effective_premium_years}年\n"
            result_text += f"FIT期間中の出力制御率（FIT継続比較シナリオに適用）: {float(fit_curtailment_rate_pct):.1f}%\n"
        else:
            result_text += f"FIPプレミアム期間: {effective_premium_years}年\n"
        result_text += "\n"

        result_text += "── 投資・ファイナンス ──\n"
        if is_fit_to_fip:
            result_text += f"PV投資（サンクコスト、P-IRR対象外）: {pv_capex_gross / 10000:.0f} 万円\n"
            result_text += f"蓄電池投資: {bat_capex / 10000:.0f} 万円（補助 {subsidy_bat / 10000:.0f} 万円）\n"
        else:
            result_text += f"PV投資: {pv_capex_gross / 10000:.0f} 万円（補助 {subsidy_pv / 10000:.0f} 万円）\n"
            result_text += f"蓄電池投資: {bat_capex / 10000:.0f} 万円（補助 {subsidy_bat / 10000:.0f} 万円）\n"
        result_text += f"純投資額（補助後、P-IRR対象）: {cf_result['net_capex'] / 10000:.0f} 万円\n"
        result_text += f"自己資本: {cf_result['equity'] / 10000:.0f} 万円（{float(equity_ratio_pct):.0f}%）\n"
        result_text += f"借入: {cf_result['debt'] / 10000:.0f} 万円 / 金利{float(loan_interest_pct):.1f}% / {int(loan_years)}年\n"
        result_text += f"年返済額: {cf_result['annual_loan_payment'] / 10000:.0f} 万円/年\n"
        result_text += f"年O&M費: {cf_result['annual_om'] / 10000:.0f} 万円/年"
        if bat_om_mode == "PCS_kW建て（三菱総研試算）":
            result_text += f"（蓄電池: PCS_kW建て {float(om_bat_per_kw_pcs):.0f}円/kW/年）\n"
        else:
            result_text += f"（蓄電池: CAPEX比 {float(om_ratio_pct):.1f}%/年）\n"
        if float(decom_pct) > 0:
            result_text += f"廃止措置費用: {float(decom_pct):.1f}%（最終年計上）\n"
        result_text += "\n"

        result_text += "── 事業性指標（{}年計算） ──\n".format(int(irr_period_years))
        if is_fit_to_fip:
            result_text += "（ケースB: 増分CF「FIP転+蓄電池 − FIT継続」で評価）\n"
        if cf_result['project_irr'] is not None:
            result_text += f"Project IRR: {cf_result['project_irr'] * 100:.2f}%\n"
        else:
            result_text += "Project IRR: 計算不能（CFが全期間マイナス等）\n"
        result_text += f"Project NPV（割引率{float(loan_interest_pct):.1f}%）: {cf_result['project_npv'] / 10000:+.0f} 万円\n"
        if cf_result['payback_year'] is not None:
            result_text += f"投資回収年数: {cf_result['payback_year']} 年\n"
        else:
            result_text += "投資回収年数: 計算期間内に回収できず\n"
        result_text += f"平均年EBITDA: {cf_result['avg_ebitda'] / 10000:.0f} 万円/年\n"
        result_text += f"蓄電池: 寿命{int(bat_life_years)}年 / 劣化{float(bat_degrade_pct):.1f}%/年 / 寿命到来時={bat_eol_action}\n"

        # --- ケースB: FIT継続比較 ---
        if fit_continuation_cf is not None:
            # cf_result["rows"] の project_cf は FIP転+蓄電池の運営CF（pv_om=0 前提）
            # fit_continuation_cf["rows"] は FIT継続の運営CF（こちらも pv_om を無視して比較）
            # PV O&M は両シナリオで同額発生するため、増分比較では相殺される
            fip_op_cum = sum(
                row["project_cf"] for row in cf_result["rows"][1:]
            )  # 初期投資(-bat_capex)を除いた運営CF累計
            fit_op_cum = sum(
                row["revenue"] for row in fit_continuation_cf["rows"][1:]
            )  # 運営収益累計（PV O&M は相殺のため無視）
            diff = fip_op_cum - fit_op_cum
            net_after_bat = diff - bat_capex + subsidy_bat

            result_text += "\n── 比較: FIT継続 vs FIP転+蓄電池（20年累計） ──\n"
            result_text += f"  FIT継続（運営CF累計、PV O&M除外）: {fit_op_cum / 10000:+.0f} 万円\n"
            result_text += f"  FIP転+蓄電池（運営CF累計、PV O&M除外）: {fip_op_cum / 10000:+.0f} 万円\n"
            result_text += f"  差分（FIP転−FIT継続）: {diff / 10000:+.0f} 万円\n"
            result_text += f"  蓄電池投資（補助後）: -{(bat_capex - subsidy_bat) / 10000:.0f} 万円\n"
            result_text += f"  純利益（20年累計）: {net_after_bat / 10000:+.0f} 万円\n"
            result_text += "  ※ Project IRR/NPV/回収年数 は上記の増分CFで再計算済み\n"

        # --- デバッグ情報 ---
        debug_text = f"データソース: {source_text}\n"
        debug_text += f"緯度: {lat:.4f}°  経度: {lon:.4f}°\n"
        debug_text += f"pvlib: {'利用' if HAS_PVLIB else '未使用（GHI直接）'}\n"
        debug_text += f"PuLP: {'利用' if HAS_PULP else '未使用'}\n"
        debug_text += f"面数: {len(faces)}\n"
        for i, face in enumerate(faces):
            pcs_str = f"{face['pcs_limit_kw']} kW" if face.get('pcs_limit_kw') else "制限なし"
            debug_text += f"  面{i+1}: Ppeak={face['ppeak']}kW, {face['orientation']}({face['azimuth']:.1f}°), 傾斜{face['tilt']}°, PCS={pcs_str}\n"
        if bifacial_enabled:
            debug_text += "両面パネル: ON\n"
        else:
            debug_text += "両面パネル: OFF\n"
        debug_text += f"\n月別発電量:\n"
        for m in range(1, 13):
            debug_text += f"  {m:2d}月: {result['monthly'].get(m, 0):8.0f} kWh\n"

        # State用に保存
        state = {
            "result": result,
            "opt_result": opt_result,
            "jepx_prices": jepx_prices,
        }

        return fig_monthly, fig_daily, fig_cashflow, fig_capacity, result_text, debug_text, state

    except Exception as e:
        import traceback
        return None, None, None, None, f"エラー: {e}", traceback.format_exc(), None


def build_ui():
    """Gradio UIを構築"""
    station_options = get_station_options()

    with gr.Blocks(title="FIP転＋蓄電池 事業性シミュレーター") as demo:
        gr.Markdown("# ⚡ FIP転＋蓄電池 事業性シミュレーター")
        gr.Markdown(
            "太陽光発電所のFIP移行＋蓄電池併設による事業性をシミュレーション。"
            "JEPXスポット価格を活用したアービトラージ収益をLP最適化で算出します。"
        )

        with gr.Row():
            # ===== 左パネル: 入力 =====
            with gr.Column(scale=2):
                gr.Markdown("### 📍 地点選択")
                station_input = gr.Dropdown(
                    label="プリセット地点（NEDO METPV-20 全77地点）",
                    choices=station_options,
                    value=station_options[0] if station_options else None,
                )
                csv_input = gr.File(
                    label="またはCSVアップロード（NEDO形式）",
                    file_types=[".csv"],
                )

                # --- JEPX設定 ---
                gr.Markdown("### 💱 JEPX市場設定")
                jepx_area_input = gr.Dropdown(
                    label="エリア",
                    choices=JEPX_AREAS,
                    value="東京",
                )
                jepx_years_input = gr.CheckboxGroup(
                    label="年度（複数選択可、平均価格を使用。2022年度はウクライナ危機で異常値のため非推奨）",
                    choices=[str(y) for y in JEPX_FISCAL_YEARS],
                    value=[str(y) for y in JEPX_FISCAL_YEARS_DEFAULT],
                )

                # --- 事業ケース（ケースA/B切替） ---
                gr.Markdown("### 🏢 事業ケース")
                case_type_input = gr.Radio(
                    label="ケース",
                    choices=["新規FIP", "既存FIT→FIP転"],
                    value="新規FIP",
                    info="新規FIP=PV+蓄電池を新設 / 既存FIT→FIP転=既設FITに蓄電池後付け",
                )
                with gr.Group(visible=False) as case_b_group:
                    gr.Markdown("**ケースB（FIT→FIP転）入力**")
                    with gr.Row():
                        fit_tariff_input = gr.Number(
                            label="FIT単価 [円/kWh]",
                            value=36.0, precision=2,
                        )
                        fit_term_years_input = gr.Number(
                            label="FIT契約期間 [年]",
                            value=20, precision=0,
                        )
                    with gr.Row():
                        fip_transition_year_input = gr.Number(
                            label="FIP転実施年（FIT開始から何年目）",
                            value=12, precision=0,
                        )
                        pv_acquisition_cost_input = gr.Number(
                            label="PV取得価額 [円]（FIT継続比較用、任意）",
                            value=0, precision=0,
                        )
                    with gr.Row():
                        fit_curtailment_rate_input = gr.Number(
                            label="FIT期間中の出力制御率 [%]（比較対象「FIT継続」シナリオに適用）",
                            value=0.0, precision=2,
                        )
                    gr.Markdown(
                        "<small>FIPプレミアム交付期間 = FIT残存期間（FIT契約期間 − FIP転実施年 + 1）。"
                        "PVはサンクコストとしてP-IRRには含めません。</small>"
                    )

                def toggle_case_b(val):
                    return gr.update(visible=(val == "既存FIT→FIP転"))

                case_type_input.change(
                    fn=toggle_case_b, inputs=[case_type_input], outputs=[case_b_group],
                    api_visibility="hidden",
                )

                # --- FIP設定 ---
                gr.Markdown("### 📋 FIP制度パラメータ")
                with gr.Row():
                    fip_base_price_input = gr.Number(
                        label="FIP基準価格 [円/kWh]（公表値をそのまま入力。参照価格は自動算定）",
                        value=DEFAULT_FIP_BASE_PRICE, precision=2,
                    )
                    nonfossil_price_input = gr.Number(
                        label="非化石証書 [円/kWh]",
                        value=DEFAULT_NONFOSSIL_PRICE, precision=2,
                    )
                    bg_fee_input = gr.Number(
                        label="BG手数料 [円/kWh]",
                        value=DEFAULT_BG_FEE, precision=2,
                    )
                with gr.Row():
                    fip_premium_years_input = gr.Number(
                        label="FIPプレミアム交付期間 [年]（ケースA新規FIPのみ。ケースBではFIT残存期間を自動使用）",
                        value=20, precision=0,
                    )

                # --- 蓄電池設定 ---
                gr.Markdown("### 🔋 蓄電池設定")
                bat_mode_input = gr.Radio(
                    label="蓄電池容量モード",
                    choices=BATTERY_MODES,
                    value=BATTERY_MODES[0],
                )
                with gr.Row():
                    bat_capacity_input = gr.Number(
                        label="蓄電池容量 [kWh]（手動モード時、最適探索モードでは初期推定として使用）",
                        value=BATTERY_DEFAULTS["capacity_kwh"], precision=0,
                    )
                with gr.Row():
                    bat_max_charge_input = gr.Number(
                        label="最大充電電力 [kW]",
                        value=BATTERY_DEFAULTS["max_charge_kw"], precision=0,
                    )
                    bat_max_discharge_input = gr.Number(
                        label="最大放電電力 [kW]",
                        value=BATTERY_DEFAULTS["max_discharge_kw"], precision=0,
                    )
                with gr.Row():
                    bat_eff_charge_input = gr.Number(
                        label="充電効率 [%]",
                        value=BATTERY_DEFAULTS["eff_charge_pct"], precision=0,
                    )
                    bat_eff_discharge_input = gr.Number(
                        label="放電効率 [%]",
                        value=BATTERY_DEFAULTS["eff_discharge_pct"], precision=0,
                    )
                with gr.Row():
                    bat_soc_min_input = gr.Number(
                        label="SOC下限 [%]",
                        value=BATTERY_DEFAULTS["soc_min_pct"], precision=0,
                    )
                    bat_soc_max_input = gr.Number(
                        label="SOC上限 [%]",
                        value=BATTERY_DEFAULTS["soc_max_pct"], precision=0,
                    )
                with gr.Row():
                    bat_degrade_input = gr.Number(
                        label="年間劣化率 [%/年]",
                        value=BATTERY_DEFAULTS["degrade_pct_per_year"], precision=2,
                    )
                    bat_life_input = gr.Number(
                        label="蓄電池寿命 [年]",
                        value=BATTERY_DEFAULTS["life_years"], precision=0,
                    )
                with gr.Row():
                    bat_eol_input = gr.Radio(
                        label="寿命到来時の運用",
                        choices=BATTERY_END_OF_LIFE_OPTIONS,
                        value=BATTERY_END_OF_LIFE_OPTIONS[0],
                    )
                    bat_replace_cost_input = gr.Number(
                        label="交換時の単価比 [%]（当初比、将来コストダウン想定）",
                        value=BATTERY_REPLACE_COST_RATIO_DEFAULT, precision=0,
                    )

                # --- 出力制御 ---
                gr.Markdown("### 🚦 出力制御設定")
                annual_curtail_input = gr.Number(
                    label="年間出力制御率 [%]（月×時間帯プロファイルに従って分散）",
                    value=0.0, precision=2,
                )

                # --- 経済性 ---
                gr.Markdown("### 💰 経済性パラメータ")
                with gr.Row():
                    pv_cost_input = gr.Number(
                        label="PVシステム単価 [円/kW]",
                        value=ECON_DEFAULTS["pv_cost_per_kw"], precision=0,
                    )
                    bat_cost_input = gr.Number(
                        label="蓄電池単価 [円/kWh]",
                        value=ECON_DEFAULTS["bat_cost_per_kwh"], precision=0,
                    )
                with gr.Row():
                    subsidy_pv_input = gr.Number(
                        label="PV補助率 [%]",
                        value=ECON_DEFAULTS["subsidy_pv_pct"], precision=1,
                    )
                    subsidy_bat_input = gr.Number(
                        label="蓄電池補助率 [%]",
                        value=ECON_DEFAULTS["subsidy_bat_pct"], precision=1,
                    )
                with gr.Row():
                    om_ratio_input = gr.Number(
                        label="O&M費率 [%/年]（CAPEX比）",
                        value=ECON_DEFAULTS["om_ratio_pct"], precision=2,
                    )
                    equity_ratio_input = gr.Number(
                        label="自己資本比率 [%]",
                        value=ECON_DEFAULTS["equity_ratio_pct"], precision=0,
                    )
                with gr.Row():
                    pv_degrade_input = gr.Number(
                        label="PV年間劣化率 [%/年]（線形、結晶シリコン一般値）",
                        value=DEFAULT_PV_DEGRADE_PCT_PER_YEAR, precision=2,
                    )
                with gr.Row():
                    arbitrage_realization_input = gr.Number(
                        label="蓄電池アービトラージ実現率 [%]（完全予見LPの理論値に対する実運用の目安）",
                        value=DEFAULT_ARBITRAGE_REALIZATION_RATE_PCT, precision=1,
                    )
                with gr.Row():
                    bat_om_mode_input = gr.Radio(
                        label="蓄電池O&Mモード",
                        choices=BATTERY_OM_MODES,
                        value=BATTERY_OM_MODES[1],  # PCS_kW建て（三菱総研試算）をデフォルト
                    )
                    om_bat_pcs_input = gr.Number(
                        label="蓄電池O&M [円/kW(PCS)/年]",
                        value=ECON_DEFAULTS["om_bat_per_kw_pcs"], precision=0,
                    )
                with gr.Row():
                    decom_pct_input = gr.Number(
                        label="廃止措置費用 [CAPEX×%]（最終年計上）",
                        value=ECON_DEFAULTS["decom_pct"], precision=1,
                    )
                with gr.Row():
                    loan_interest_input = gr.Number(
                        label="借入金利 [%/年]",
                        value=ECON_DEFAULTS["loan_interest_pct"], precision=2,
                    )
                    loan_years_input = gr.Number(
                        label="借入期間 [年]",
                        value=ECON_DEFAULTS["loan_years"], precision=0,
                    )
                with gr.Row():
                    irr_period_input = gr.Number(
                        label="P-IRR計算期間 [年]",
                        value=ECON_DEFAULTS["irr_period_years"], precision=0,
                    )
                    capacity_search_steps_input = gr.Number(
                        label="最適容量探索ステップ数",
                        value=8, precision=0,
                    )

                # --- 太陽光発電補正係数 ---
                with gr.Accordion("⚙️ 太陽光発電補正係数（JIS C 8907）", open=False):
                    with gr.Row():
                        KHD_input = gr.Number(label="KHD（日射量年変動）", value=DEFAULT_KHD, precision=3)
                        KPD_input = gr.Number(label="KPD（経時変化）", value=DEFAULT_KPD, precision=3)
                    with gr.Row():
                        KPM_input = gr.Number(label="KPM（負荷整合）", value=DEFAULT_KPM, precision=3)
                        KPA_input = gr.Number(label="KPA（回路補正）", value=DEFAULT_KPA, precision=3)
                    with gr.Row():
                        eta_input = gr.Number(label="ηINO（インバータ効率）", value=DEFAULT_ETA_INO, precision=3)
                    with gr.Row():
                        alpha_input = gr.Number(label="αPmax [%/℃]", value=DEFAULT_ALPHA, precision=3)
                        delta_t_input = gr.Number(label="ΔT [℃]", value=DEFAULT_DELTA_T, precision=1)
                    gr.Markdown(
                        "<small>ΔT参考値: 架台設置形=21.5℃ / 屋根置き形=28.1℃ / 屋根一体形=46.3℃</small>"
                    )

                # --- 両面パネル ---
                with gr.Accordion("☀️ 両面パネル設定", open=False):
                    bifacial_enabled_input = gr.Checkbox(label="両面パネルを使用", value=False)
                    with gr.Row():
                        bifaciality_input = gr.Number(
                            label="背面効率比", value=BIFACIAL_DEFAULTS["bifaciality"], precision=2,
                        )
                        gcr_input = gr.Number(
                            label="GCR", value=BIFACIAL_DEFAULTS["gcr"], precision=2,
                        )
                    with gr.Row():
                        height_input = gr.Number(
                            label="パネル高 [m]", value=BIFACIAL_DEFAULTS["height"], precision=1,
                        )
                        pitch_input = gr.Number(
                            label="列間隔 [m]", value=BIFACIAL_DEFAULTS["pitch"], precision=1,
                        )
                    snow_albedo_input = gr.Checkbox(
                        label="積雪アルベド自動切替（積雪時0.7 / 通常0.2）", value=True,
                    )

                # --- アレイ設定 ---
                gr.Markdown("### 🔲 太陽電池アレイ設定")
                num_faces_input = gr.Slider(
                    label="面数", minimum=1, maximum=MAX_FACES, step=1, value=1,
                )
                gr.Markdown(
                    "<small>方位角: 北=0° → 東=90° → 南=180° → 西=270°（時計回り）。"
                    "直接入力はドロップダウンより優先</small>"
                )

                face_components = []
                face_groups = []
                for i in range(MAX_FACES):
                    visible = (i == 0)
                    with gr.Group(visible=visible) as grp:
                        gr.Markdown(f"**面{i+1}**")
                        with gr.Row():
                            pp = gr.Number(
                                label="Ppeak [kW]",
                                value=1000.0 if i == 0 else 0,
                                precision=1,
                            )
                            ori = gr.Dropdown(
                                label="方位（選択）",
                                choices=list(ORIENTATION_TO_AZIMUTH.keys()),
                                value="南",
                            )
                            azi = gr.Number(label="方位角 [°]（直接入力優先）", value=None, precision=1)
                            tlt = gr.Number(label="傾斜角 [°]", value=20, precision=1)
                            pcs = gr.Number(
                                label="PCS出力制限 [kW]",
                                value=900.0 if i == 0 else 0,
                                precision=1,
                            )
                    face_components.extend([pp, ori, azi, tlt, pcs])
                    face_groups.append(grp)

                def update_face_visibility(n):
                    return [gr.update(visible=(i < n)) for i in range(MAX_FACES)]

                num_faces_input.change(
                    fn=update_face_visibility,
                    inputs=[num_faces_input],
                    outputs=face_groups,
                    api_visibility="hidden",
                )

                run_btn = gr.Button("▶️ 計算実行（LP最適化のため数十秒かかります）", variant="primary", size="lg")

            # ===== 右パネル: 出力 =====
            with gr.Column(scale=3):
                with gr.Tabs():
                    with gr.Tab("📊 月別発電量・売電量"):
                        monthly_plot = gr.Plot(label="月別")
                    with gr.Tab("📈 日別運用（48コマ）"):
                        with gr.Row():
                            month_input = gr.Number(label="月 (1-12)", value=7, precision=0)
                            day_input = gr.Number(label="日 (1-31)", value=15, precision=0)
                        daily_plot = gr.Plot(label="日別運用")
                    with gr.Tab("💴 年次CF推移"):
                        cashflow_plot = gr.Plot(label="年次キャッシュフロー")
                    with gr.Tab("🎯 最適容量探索"):
                        gr.Markdown(
                            "<small>「最適容量探索」モード時にのみ計算されます。"
                            "段階1（LP一体化）で粗最適を求め、段階2（グリッドサーチ）でP-IRRカーブを描きます。</small>"
                        )
                        capacity_plot = gr.Plot(label="蓄電池容量 vs Project IRR")
                result_box = gr.Textbox(label="計算結果", lines=40, interactive=False)
                debug_box = gr.Textbox(label="デバッグ情報", lines=10, interactive=False)

        # State（再描画用）
        result_state = gr.State(value=None)

        # === 計算ボタン ===
        all_inputs = [
            station_input, csv_input,
            KHD_input, KPD_input, KPM_input, KPA_input, eta_input,
            alpha_input, delta_t_input,
            bifacial_enabled_input, bifaciality_input, gcr_input, height_input, pitch_input,
            snow_albedo_input,
            bat_mode_input,
            bat_capacity_input, bat_max_charge_input, bat_max_discharge_input,
            bat_eff_charge_input, bat_eff_discharge_input,
            bat_soc_min_input, bat_soc_max_input,
            bat_degrade_input, bat_life_input, bat_eol_input, bat_replace_cost_input,
            jepx_area_input, jepx_years_input,
            fip_base_price_input, nonfossil_price_input, bg_fee_input,
            annual_curtail_input,
            pv_cost_input, bat_cost_input,
            subsidy_pv_input, subsidy_bat_input,
            om_ratio_input, equity_ratio_input,
            loan_interest_input, loan_years_input, irr_period_input,
            capacity_search_steps_input,
        ] + face_components

        # 上記 all_inputs のうち face_components 直前までの長さ
        N_BASE = 43
        N_FACE = MAX_FACES * 5  # 40
        # 追加: bat_om_mode, om_bat_pcs, decom_pct, case_type, fip_premium_years,
        #       fit_tariff, fit_term_years, fip_transition_year, pv_acquisition_cost,
        #       fit_curtailment_rate_pct, pv_degrade_pct_per_year, arbitrage_realization_rate_pct
        extra_inputs = [
            bat_om_mode_input, om_bat_pcs_input, decom_pct_input,
            case_type_input, fip_premium_years_input,
            fit_tariff_input, fit_term_years_input,
            fip_transition_year_input, pv_acquisition_cost_input,
            fit_curtailment_rate_input, pv_degrade_input,
            arbitrage_realization_input,
        ]

        def on_click(*args):
            base = args[:N_BASE]
            face_args = args[N_BASE:N_BASE + N_FACE]
            tail = args[N_BASE + N_FACE:]
            num_f, month_val, day_val = tail[0], tail[1], tail[2]
            bat_om_mode_v, om_bat_pcs_v, decom_pct_v = tail[3], tail[4], tail[5]
            case_type_v, fip_premium_years_v = tail[6], tail[7]
            fit_tariff_v, fit_term_years_v = tail[8], tail[9]
            fip_transition_year_v, pv_acq_v = tail[10], tail[11]
            fit_curtail_v = tail[12]
            pv_degrade_v = tail[13]
            arbitrage_realization_v = tail[14]
            return run_simulation(
                *base, face_args,
                num_faces=num_f,
                display_month=month_val, display_day=day_val,
                bat_om_mode=bat_om_mode_v,
                om_bat_per_kw_pcs=om_bat_pcs_v,
                decom_pct=decom_pct_v,
                case_type=case_type_v,
                fip_premium_years=fip_premium_years_v,
                fit_tariff=fit_tariff_v,
                fit_term_years=fit_term_years_v,
                fip_transition_year=fip_transition_year_v,
                pv_acquisition_cost=pv_acq_v,
                fit_curtailment_rate_pct=fit_curtail_v,
                pv_degrade_pct_per_year=pv_degrade_v,
                arbitrage_realization_rate_pct=arbitrage_realization_v,
            )

        all_inputs_with_display = all_inputs + [num_faces_input, month_input, day_input] + extra_inputs

        run_btn.click(
            fn=on_click,
            inputs=all_inputs_with_display,
            outputs=[monthly_plot, daily_plot, cashflow_plot, capacity_plot, result_box, debug_box, result_state],
            api_visibility="hidden",
        )

        # === 月日変更時のグラフ再描画（再計算なし） ===
        def on_date_change(stored_state, month_val, day_val):
            if stored_state is None:
                fig = go.Figure()
                fig.update_layout(title="先に「計算実行」ボタンを押してください")
                return fig
            # Number入力が空欄（None）のときはデフォルト日付にフォールバック
            month_val = month_val if month_val is not None else 7
            day_val = day_val if day_val is not None else 15
            return make_daily_chart(
                stored_state["result"],
                stored_state["opt_result"],
                stored_state["jepx_prices"],
                int(month_val), int(day_val),
            )

        month_input.change(
            fn=on_date_change,
            inputs=[result_state, month_input, day_input],
            outputs=[daily_plot],
            api_visibility="hidden",
        )
        day_input.change(
            fn=on_date_change,
            inputs=[result_state, month_input, day_input],
            outputs=[daily_plot],
            api_visibility="hidden",
        )

        # === MCP APIエンドポイント（Phase 4a、docs/agent_design.md） ===
        # gr.api() でUI無しのAPI関数を登録。mcp_server=True 時にMCPツールとして公開される。
        # ここで遅延importすることで app ⇄ mcp_tools の循環importを回避
        # （この時点で app モジュールの全関数定義が完了しているため安全）。
        import mcp_tools
        gr.api(mcp_tools.list_stations, api_name="list_stations")
        gr.api(mcp_tools.get_jepx_stats, api_name="get_jepx_stats")
        gr.api(mcp_tools.estimate_pv_generation, api_name="estimate_pv_generation")
        gr.api(mcp_tools.validate_fip_params, api_name="validate_fip_params")
        gr.api(mcp_tools.simulate_fip_case_a, api_name="simulate_fip_case_a")
        gr.api(mcp_tools.simulate_fip_case_b, api_name="simulate_fip_case_b")

    return demo


# ============================================================
# エントリポイント
# ============================================================

demo = build_ui()

if __name__ == "__main__":
    # mcp_server=True: /gradio_api/mcp/ でMCPサーバーを公開（Phase 4a）
    demo.launch(mcp_server=True)
