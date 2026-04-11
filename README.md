---
title: FIP転＋蓄電池 事業性シミュレーター
emoji: 🔋
colorFrom: green
colorTo: blue
sdk: gradio
sdk_version: 5.23.0
app_file: app.py
pinned: false
license: mit
---

# FIP転＋蓄電池 事業性シミュレーター

太陽光発電所併設型蓄電池による FIP 転ビジネスの事業性をシミュレーションするツールです。
既存 FIT 発電所からの FIP 移行（ケースB）、および新規 FIP 発電所＋蓄電池の新設（ケースA）の両ケースに対応します。

## 主な機能

- **PV 発電シミュレーション**: JIS C 8907:2005 準拠、NEDO METPV-20 の 77 地点対応、30 分 × 365 日 = 17,520 コマの高精度計算
- **JEPX スポット価格 DB**: 9 エリア × 6 年度（2020〜2025、沖縄は JEPX 非取引のため除外）の 30 分コマ価格を収録（複数年度の平均利用可）
- **蓄電池 LP 最適化**: PuLP/CBC で 17,520 コマの充放電スケジュールを最適化（JEPX アービトラージ＋出力制御回避）
- **FIP 2 フェーズ収益モデル**: プレミアム期間 → プレミアム終了後の 2 段階キャッシュフロー
- **ケースA / ケースB 自動切替**:
  - ケースA: 新規 FIP 発電所＋蓄電池の新設
  - ケースB: 既存 FIT 発電所への蓄電池後付＋ FIP 転（FIT 残存期間 = プレミアム交付期間）
- **最適蓄電池容量探索**: LP 一体化（粗探索） + グリッドサーチ（NPV 最大化）の 2 段階アプローチ
- **出力制御モデル**: 月別×時間帯別の制御確率プロファイルを年間制御率にスケーリング
- **蓄電池劣化・寿命管理**: 線形劣化（年率指定）＋寿命到来時の交換 / 終了選択
- **比較分析**:
  - 蓄電池あり / なしの自動比較
  - ケースB では「FIT 継続 vs FIP 転＋蓄電池」の 20 年累計 CF 比較
  - 増分 CF ベースの Project IRR / NPV / 投資回収年数
- **オーナー視点 CF チャート（ケースB）**: PV 初期投資（プラント運転年 0）＋蓄電池追加投資（FIP 転年）の 2 段階投資をライフサイクル全景で表示

## 使い方

1. **太陽光発電設定**: 地点・面数・容量・方位・傾斜・PCS 出力制限を入力
2. **蓄電池設定**: 容量・PCS 定格・効率・SOC 範囲・寿命・劣化率を入力（または最適容量探索を選択）
3. **JEPX / FIP 設定**: エリア・年度（複数選択可）・基準価格・非化石証書・BG 手数料を入力
4. **出力制御**: 年間制御率を入力（月別×時間帯別プロファイルが自動適用）
5. **事業性パラメータ**: PV / 蓄電池単価・補助率・O&M ・借入条件・廃止措置率を入力
6. **ケース選択**: 新規 FIP / 既存 FIT→FIP 転 を選択（ケースB は FIT 単価・契約期間・転実施年・PV 取得価額を追加入力）
7. **計算実行**: 月別グラフ・日別 48 コマ運用・年次 CF・最適容量カーブ・事業性指標を確認

## 技術スタック

- Python / Gradio / Plotly / pvlib / pandas / numpy
- PuLP + CBC（蓄電池充放電 LP 最適化、17,520 コマ）
- SQLite（`radiation.db` 77 地点気象データ ／ `jepx.db` JEPX スポット価格）

## 関連プロジェクト

- 住宅用シミュレーター: [pv-sim-gh](https://huggingface.co/spaces/hachinai/pv-sim-gh)
- 産業用自家消費 / マイクログリッド: [pv-sim-biz](https://huggingface.co/spaces/hachinai/pv-sim-biz)

## 参考ドキュメント

- JIS C 8907:2005: <https://kikakurui.com/c8/C8907-2005-01.html>
- NEDO METPV-20: 日射量データベース
- JEPX: <https://www.jepx.org/>
- エネがえる「FIP 転マニュアル」: <https://www.enegaeru.com/evaluationforconsidering-fiptransfer-storagebattery>
