# pv-sim Agent 統合設計書（Phase 4）

**目標: pv-simシリーズをMCP（Model Context Protocol）ツール群として公開し、
Claude等のAIエージェントから自然言語で日本の太陽光・蓄電池事業性を試算できる
「統合エージェント基盤」を構築する。**

- 作成: 2026-08-31（Fable 5設計セッション）
- 開発拠点: 本リポジトリ（pv-sim-fip）で先行実装し、姉妹プロジェクトへ横展開
- ステータス: 設計段階（実装未着手）

---

## 0. ビジョンと統合の考え方

### 何を解くか
現行のGradioフォームUIは「傾斜角」「BG手数料」「KHD」等のパラメータを理解する
専門家しか使えない。MCP化により、FIT発電所オーナー・蓄電池投資検討者・一般の
需要家が「福岡の5MW、FIT36円が残り8年。蓄電池を足すべき？」と自然言語で聞き、
前提が全部見える中立試算を得られるようにする。

### 統合アーキテクチャの基本方針: 「ハブを作らない」

```
                    ┌─ (MCP) ─ pv-sim-fip   … FIP転 + 系統用蓄電池 + PV発電 + JEPXデータ
Claude (エージェント) ┼─ (MCP) ─ pv-sim-gh    … 家庭用需給
                    └─ (MCP) ─ pv-sim-biz   … 産業用自家消費/MG
```

- **統合はクライアント側で行う**: MCPクライアント（Claude Code / Claude Desktop等）は
  複数MCPサーバーに同時接続できる。横断比較（「自家消費とFIP転どちらが得か」）は
  エージェントが複数Spaceのツールを呼び分けることで実現する。
- サーバー側にハブSpaceやプロキシを作らない。新規Spaceが不要なので
  HF無料枠のハードウェアクォータを消費しない（pv-sim-genのPaused問題を回避）。
- 賢さ（パラメータ解釈・感度分析・レポート作成）はエージェント側、
  Space側は「正確で高速な計算機」に徹する。

### エージェント化で自動的に手に入るもの
| 従来の改善候補 | エージェント構成での実現方法 |
|---|---|
| 感度分析（トルネード図） | エージェントがパラメータを振って複数回ツールを呼ぶ（実装不要） |
| レポート/提案書出力 | エージェントが結果JSONから文書を生成（実装不要） |
| 完全予見バイアスの明示 | ツール側に実現率パラメータを実装（**要実装**、§4） |

---

## 1. 技術基盤（確認済み事実）

- Gradio MCP対応: `pip install "gradio[mcp]"` + `demo.launch(mcp_server=True)`
  （または環境変数 `GRADIO_MCP_SERVER=True`）
- ツール定義は **関数のdocstring（Args:セクション必須）＋型ヒント＋api_name** から自動生成
- HF Spaces上のエンドポイント: `https://hachinai-pv-sim-fip.hf.space/gradio_api/mcp/`
- 進捗通知: `gr.Progress()` 対応（キュー経由、約500msオーバーヘッド）
- **現行fipは Gradio 5.23（MCP対応前）→ sdk_version 更新が必須**（§6）
- Claude Code からの接続例:
  `claude mcp add --transport http pv-sim https://hachinai-pv-sim-fip.hf.space/gradio_api/mcp/`

---

## 2. MCPツールセット定義（pv-sim-fip Space）

### 設計原則
1. **全パラメータにデフォルト値**。必須入力は最小限（地点・容量など）
2. **パラメータ名に単位を含める**: `fit_tariff_yen_per_kwh`, `battery_capacity_kwh`,
   `tilt_deg` 等。エージェントの単位誤解釈を構造的に防ぐ
3. **戻り値は構造化JSON**（テキスト整形はエージェントの仕事）
4. **軽量/重量ツールを分離**し、重量ツールは実行時間をdocstringに明記

### ツール一覧

#### 軽量ツール（即答、LP不要）
| ツール | 内容 | 実行時間 |
|---|---|---|
| `list_stations()` | NEDO 77地点の一覧（地点番号・地点名・緯度経度） | 即時 |
| `get_jepx_stats(area, fiscal_years)` | エリア別価格統計: 年平均・時間帯別平均・日内スプレッド分布・負価格コマ数 | 即時 |
| `estimate_pv_generation(station_no, ppeak_kw, tilt_deg, azimuth_deg, ...)` | 発電量のみ（JIS C 8907、LPなし） | 数秒 |

#### 検証ツール（実行前確認用）
| ツール | 内容 |
|---|---|
| `validate_fip_params(...)` | simulate系と同一シグネチャ。正規化後パラメータの全エコー＋警告リスト（例: 「PCS容量が蓄電池容量の2倍以上です」）＋推定計算時間を即答。LPは実行しない |

#### 重量ツール（LP実行）
| ツール | 内容 | 実行時間 |
|---|---|---|
| `simulate_fip_case_a(...)` | 新規FIP＋蓄電池（現ケースA） | 30〜90秒 |
| `simulate_fip_case_b(...)` | 既存FIT→FIP転（現ケースB、増分CF評価） | 30〜90秒 |
| `simulate_grid_battery(...)` | **新規**: 系統用蓄電池単独（§5） | 30〜90秒 |
| `search_optimal_capacity(...)` | 最適容量探索（2段階） | 2〜3分（docstringに明示警告） |

### 出力JSONスキーマ（simulate系共通）

```json
{
  "assumptions": { "…全入力パラメータの正規化後の値を必ずエコー…" },
  "kpis": {
    "project_irr_pct": 7.11, "project_npv_yen": 457480000,
    "payback_years": 11, "annual_revenue_yen": 100231000
  },
  "annual": { "generation_kwh": 5030743, "export_kwh": ..., "curtailed_kwh": ...,
              "battery_charge_kwh": ..., "arbitrage_value_yen": ... },
  "cashflow": [ {"year": 0, "project_cf_yen": ...}, ... ],
  "caveats": [
    "蓄電池運用は完全予見LPによる理論上限です（実現率パラメータ適用後の値）",
    "JEPX価格は過去実績（YYYY-YYYY年度平均）であり将来価格を保証しません",
    "本結果は投資判断の参考情報であり、収益を保証するものではありません"
  ]
}
```

- `caveats` は**全simulate系ツールの戻り値に常時同梱**する（削除不可）。
  エージェントが要約を作る際に免責が脱落しない層をデータ側に持たせる。

---

## 3. 誤解釈ガードレール（2段階プロトコル）

金融判断に直結するため、自然言語→パラメータの**サイレントな誤解釈**を最重要リスクとする。

```
ユーザー発話
  → エージェントがパラメータ解釈
  → validate_fip_params() 呼び出し（即答）
  → エージェントが正規化パラメータ＋警告をユーザーに提示、確認を得る
  → simulate_fip_case_x() 実行（30秒〜）
  → 結果（assumptions同梱）を提示
```

- simulate系docstringの冒頭に
  「**必ず事前に validate_* を呼び、解釈したパラメータをユーザーに提示して
  確認を得てから実行すること**」を記載（MCPの仕様上ソフト強制だが、
  docstringはツール説明としてエージェントに毎回提示される）。
- simulate結果にも `assumptions` を同梱し、事後検証を可能にする。

---

## 4. アービトラージ実現率パラメータ（新規・要協議）

- `arbitrage_realization_rate`（0〜1）: LPが算出したアービトラージ増分収益に乗じる係数。
  完全予見LPは1年分の価格を全て知っている前提の理論上限であり、
  実運用（前日予測ベース）では一般に7〜9割程度に低下するため。
- 適用対象: 蓄電池による増分収益のみ（ベースラインのJEPX直売収入には適用しない）
- **デフォルト値 0.85 に決定**（2026-08-31）。UI側（Gradioフォーム）にも同じ
  パラメータを追加し、UIとMCPで計算結果が一致する状態を保つ。

---

## 5. 系統用蓄電池モジュール（simulate_grid_battery）

現スイート唯一の空白セグメント。fipのLPエンジンの変形として本リポジトリに実装する。

### LP定式化（案）
```
決定変数（30分コマ t = 0..17519）:
  buy(t)  ≥ 0 : 系統からの充電量 [kWh/30分]
  sell(t) ≥ 0 : 系統への放電販売量 [kWh/30分]
  soc(t)      : 蓄電池残量 [kWh]

目的関数:
  maximize Σ_t [ sell(t) × (jepx(t) + premium + nonfossil − bg_fee)
               − buy(t) × (jepx(t) + wheeling_fee + purchase_fees) ]

制約:
  SOC遷移: soc(t) = soc(t-1) + buy(t)×η_ch − sell(t)/η_dc
  SOC範囲 / 充放電レート / 終端SOC=初期SOC（fipと同じ）
  mutual exclusion: buy(t) + sell(t) ≤ max_power_per_slot（パススルー防止、fipで実証済み）
```

### 制度パラメータ（第一弾の割り切り）
| 項目 | 扱い |
|---|---|
| 託送料金（充電時） | ユーザー入力 [円/kWh]（**制度調査が必要**: 発電側/需要側課金の扱いが流動的） |
| FIPプレミアム | 入力可（蓄電池単独もFIP認定対象。基準価格はユーザー入力） |
| 容量市場収入 | 固定額入力 [円/kW/年]（オークション価格はユーザーが調べて入れる） |
| 需給調整市場（ΔkW） | **対象外と明記**。モデル化難度が高く、精度を装わない |

### 検証
- 2〜4コマの手計算可能なミニケースでLPユニットテスト
- 公表されている系統用蓄電池の事業性試算（例: 経産省・OCCTO資料）との突合を検討

---

## 6. 実装方針・ファイル構成

| 項目 | 方針 |
|---|---|
| MCP用API関数 | `mcp_tools.py` に分離（app.py 2,900行のさらなる肥大化を回避）。**CLAUDE.mdの「app.pyスタンドアロン維持」ルール改定を承認済み（2026-08-31）** — HF Spacesは複数ファイルデプロイ可能なため、単一ファイル制約はMCP分離の妨げにならない |
| README.md | `sdk_version: 5.23.0` → MCP対応の最新5.xへ更新。**Gradio更新でUI回帰確認必須**（Number/Slider挙動の変化に注意） |
| requirements.txt | `gradio` → `gradio[mcp]` |
| 起動 | `demo.launch(mcp_server=True)` |
| 入力上限ガード | 巨大LPによる計算資源枯渇防止: `battery_capacity_kwh ≤ 100,000`、`ppeak_kw ≤ 100,000`、`n_steps ≤ 12` 等をvalidate/simulate両方で強制 |
| 同時実行 | Gradioキュー（concurrency 1）でLP多重実行を防止 |

---

## 7. 段階的実装計画

| Phase | 内容 | 完了条件 |
|---|---|---|
| **4a** ✅ | fip MCP化最小版: Gradio更新、`list_stations` / `get_jepx_stats` / `estimate_pv_generation` / `validate_fip_params` / `simulate_fip_case_a/b` | **完了（2026-08-31, commit f1d9bf4）**。本番Spaceで全6ツール動作確認（IRR 7.11%=ローカル一致）。UI回帰なし |
| **4b** | ガードレール整備: 出力スキーマ統一、caveats同梱、`arbitrage_realization_rate`（UI側にも追加）、入力上限 | validate→confirm→simulateの運用が実プロンプトで機能 |
| **4c** | 系統用蓄電池: 制度調査（託送・容量市場）→ LP実装 → `simulate_grid_battery` | ミニケースLP検証＋公表試算との突合 |
| **4d** | 横展開: gh / biz を同パターンでMCP化（各リポジトリで実施） | 3サーバー同時接続で横断比較が動く |
| **4e** | OSSドキュメント: MCP接続ガイド（日英）、活用例プロンプト集、READMEバッジ | 第三者がREADMEだけで接続・試算できる |

各Phaseは独立してデプロイ可能。4aで価値検証してから先へ進む。

### テスト計画
- `test_mcp_tools.py`: 各ツールの正常系/異常系/JSON構造/入力上限
- 既存 `test_phase3_smoke.py` / `test_mri_slide47.py` の回帰維持（MCP化がUI経路を壊さないこと）
- E2E: Claude Codeから実際にMCP接続して代表シナリオ3件（ケースA/B/系統用）

### Phase 4a 実装時の重要な学び（4dの横展開で必ず適用すること）

**`import app` を関数内で行ってはいけない。** HF Spaces / `python app.py` 起動では
appモジュールが `"__main__"` 名でロードされるため、リクエスト処理中に `import app` すると
**app.py がゼロから二重実行され、ワーカースレッド内で `build_ui()` が走ってGradioがクラッシュ**する
（`AttributeError: 'Radio' object has no attribute '_id'`、UIは正常なのにAPI/MCPだけ
`event: error` を返す形で顕在化）。`mcp_tools._get_app()` のように
`sys.modules` からロード済みモジュール（`app` または `__main__`）を探して再利用する。

**UIイベントは `api_visibility="hidden"` で隠す。** 指定しないと `on_click` や
`update_face_visibility` までMCPツールとして公開され、エージェントが誤って呼ぶ余地が生まれる。

**検証はHTTP経路で行う。** 関数を直接呼ぶユニットテストだけでは上記の二重import問題を
検出できない。`python app.py` を起動して `/gradio_api/call/<name>` を叩く経路が本番と同一。

---

## 8. リスク・制約

| リスク | 対応 |
|---|---|
| エージェントのパラメータ誤解釈 | 2段階プロトコル＋単位入りパラメータ名＋assumptionsエコー（§3） |
| 楽観バイアスの独り歩き | caveats常時同梱＋実現率パラメータ（§4） |
| MCPクライアントのタイムアウト | 重量ツール分離＋docstringに実行時間明記＋`gr.Progress()` |
| 無料CPUの計算資源 | 入力上限＋キュー制御。利用が伸びたらHF PRO（月額）を検討 |
| Gradio大幅更新によるUI回帰 | sdk_version更新は単独コミットで行い、全タブの表示・計算を目視確認 |
| Gradio MCP仕様の変動（若い機能） | requirements.txtでバージョンpin |

---

## 9. 決定事項・未確定事項

### 決定済み（2026-08-31）
- [x] `arbitrage_realization_rate` のデフォルト値 = **0.85**
- [x] `mcp_tools.py` 分離、およびCLAUDE.md「app.pyスタンドアロン維持」ルールの改定を承認
- [x] gh / biz への横展開時期 = 4aの価値検証後（推奨案どおり）
- [x] **pv-sim-gen（HF上: pv-sim-ge）は退役しない**。ブログに掲載済みのため現状維持。
      Phase 4の実装はpv-sim-fip単独で完結し、fipは既にRunning状態でクォータ確保済みのため、
      genのPaused状態はPhase 4a〜4eのどの段階にも支障がないことを確認。
      genのクォータ問題は本設計と切り離し、対応不要（将来必要になれば別途検討）。

### 未確定（実装を進めながら解消）
- [ ] 系統用蓄電池の託送料金の扱い（Phase 4cで制度調査を実施し、その結果を反映。
      4a/4bの実装をブロックしない）
