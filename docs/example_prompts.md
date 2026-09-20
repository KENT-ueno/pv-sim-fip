# 活用例プロンプト集 / Example Prompts

そのままAIエージェント（Claude Code、Claude Desktop、OpenAI Codex CLI等）のチャットに
貼り付けて使える例です。接続方法は [mcp_guide.md](./mcp_guide.md) を参照してください。
各例には検証済みの参照値を添えているので、エージェントの回答と照合できます。

*Ready-to-paste prompts for any MCP-compatible agent (Claude Code, Claude Desktop,
OpenAI Codex CLI, etc.). See [mcp_guide.md](./mcp_guide.md) for connection setup.
Each example includes a verified reference value so you can sanity-check the
agent's answer.*

---

## 1. pv-sim-fip — 新規FIP発電所＋蓄電池（ケースA）

```
pv-sim-fipのMCPツールを使って、新規FIP発電所の事業性を試算してください。

条件:
- 地点: 福岡（station_no=82182）
- PV容量: 5,000 kW、傾斜30°、PCS上限5,000 kW
- 蓄電池: 1,000 kWh、最大充放電2,000 kW
- JEPXエリア: 九州、年度: 2023〜2025年度平均
- FIP基準価格: 15円/kWh、年間出力制御率: 5%
- 他のパラメータはデフォルトのまま

まずvalidate_fip_paramsで検証し、正規化されたパラメータを見せてから
simulate_fip_case_aを実行してください。Project IRRとNPVを教えてください。
```

**参照値 / Reference** (2026-09-06確認): `project_irr_pct` ≈ **-5.42%**、
`project_npv_yen` ≈ **-460,000,037円**。九州エリアはPV発電時間帯（昼）に
JEPX市場価格が沈みやすく、参照価格が低くなるため、蓄電池を足しても
基準価格15円クラスではIRRが回りにくい構造になっています（詳細はCLAUDE.md参照）。

---

## 2. pv-sim-fip — 既存FIT→FIP転＋蓄電池後付け（ケースB）

```
pv-sim-fipで、既存のFIT発電所をFIP転すべきか判断したい。

条件:
- 地点: 福岡（station_no=82182）、PV容量5,000kW（既設）
- 現在のFIT単価: 36円/kWh、FIT契約期間20年、12年目にFIP転を検討
- 追加する蓄電池: 1,000 kWh、最大充放電2,000 kW
- JEPXエリア: 九州、年度: 2023〜2025年度平均、FIP基準価格15円/kWh

validate_fip_paramsで検証後、simulate_fip_case_bを実行し、
「FIP転する」vs「FIT継続する」のどちらが有利か、増分CFベースで教えてください。
```

**参照値 / Reference** (2026-09-06確認): 増分CF（FIP転−FIT継続）≈ **-12億4,294万円**、
Project IRR算出不能（全期間マイナス）。この条件では**FIT継続が明確に有利**——
FIT単価36円は市場連動のFIP収益より高く、蓄電池投資を正当化できません。
FIT単価が低い・FIT残存期間が短い発電所ほどFIP転が有利になりやすい構造です。

---

## 3. pv-sim-fip — 系統用蓄電池単独（PVなし）

```
太陽光を持たない系統用蓄電池単独のビジネスを検討しています。pv-sim-fipの
simulate_grid_batteryで試算してください。

条件:
- JEPXエリア: 東京、年度: 2020〜2023年度平均
- 蓄電池: 1,000 kWh、最大充放電333 kW（3時間率）、単価6万円/kWh
- 容量市場単価: 8,348円/kW/年、託送料金: 0.88円/kWh
- 他はデフォルト

validate_grid_battery_paramsで検証後、simulate_grid_batteryを実行してください。
```

**参照値 / Reference** (2026-09-06確認): `project_irr_pct` ≈ **-1.08%**。
三菱総合研究所の公表資料（試算ベースシナリオ、IRR -1.50%参考値）とほぼ整合する
水準まで再現できています（詳細はCLAUDE.md §10.5参照）。容量市場・託送料金を
どちらも0円にすると大きく悪化するので、これらの単価設定が採算を大きく左右する
点も体感できます。

---

## 4. pv-sim-gh — 住宅用太陽光＋蓄電池

```
東京の戸建て住宅に太陽光と蓄電池を導入した場合の経済性を、pv-sim-ghで
試算してください。

条件:
- 地点: 東京（station_no=44132）
- 屋根: 5.0 kW、傾斜30°、南向き、PCS上限5.5 kW
- 需要: 電気＋ガス併用世帯（標準5,000kWh/年）
- 蓄電池: 5.0 kWh

validate_residential_paramsで検証後、simulate_residential_pvを実行し、
年間発電量・自家消費率・投資回収年数を教えてください。
```

**参照値 / Reference** (2026-09-06確認、Claude CodeとOpenAI Codex CLIの両方で
完全一致を確認済み): `generation_kwh`=**5,416**、`self_consumption_rate_pct`=**55.4%**、
`battery_charge_kwh`=**1,144**、`battery_discharge_kwh`=**1,032**、
電気＋ガス併用シナリオの`payback_years`=**17.1年**。

---

## 5. pv-sim-biz — 産業用（高圧）自家消費＋リース事業

```
病院向けにPV+蓄電池のリース事業を提案したい。pv-sim-bizで試算してください。

条件:
- 地点: 福岡（station_no=82182）
- 施設: 病院、延床面積22,400 m²
- PV容量: 800 kW、蓄電池300 kWh（LP最適化モード）
- 事業モデル: リース、契約期間15年、目標P-IRR 12%
- FIT経過年数: 3年目
- 逆潮流禁止・売電あり の両モードで比較してください

validate_industrial_paramsで検証後、simulate_industrial_pvを両モードで実行し、
必要リース料と需要家メリットを比較してください。
```

**参照値 / Reference** (2026-09-06確認、OpenAI Codex CLI経由・本番Space):
`generation_kwh`=**804,919**、`self_consumption_rate_pct`=**95.5%**、
`co2_reduction_t_per_year`=**331.268**、必要リース料`required_lease_yen_per_year`=
**23,750,289円/年**。需要家メリット`customer_annual_benefit_yen`は
売電あり**-2,175,000円**／逆潮流禁止**-2,813,655円**——いずれも赤字
（`proposal_viable`=false）。目標P-IRR 12%を確保するリース料が需要家の
電気代削減額を上回っており、この条件では提案が成立しないことが分かります。
（目標P-IRR自体は需要家向け出力には現れません。CLAUDE.mdの設計方針どおり、
リース料・PPA単価・需要家メリット額のみが公開されます）

---

## 5-2. pv-sim-biz — データセンター＋PV＋系統受電上限

```
pv-sim-bizで、札幌のデータセンターにPVと蓄電池を入れる試算をしてください。

条件:
- IT容量1,000kW、IT負荷率80%、PUE1.4
- 負荷形状: CEC実測形状、ノイズなし（用途は手動設定）
- PV: 1,000kW（傾斜30度・真南）
- 蓄電池: 2,000kWh、充放電とも1,000kW、SOC 10〜90%、最適充放電（LP）
- 系統受電の上限: 1,190kW
- 電気料金: 基本料金1,890円/kW、電力量は夏季19.93円・その他18.77円

validate_dc_paramsで検証し、内容を私に確認してからsimulate_dcを実行してください。
上限を守れたか、導入後のピークと年間コスト削減額を教えてください。
```

**参照値 / Reference**（ローカルの直接呼び出しと、本番MCPをプロトコル層で呼んだ結果が一致。2026-09-20）:
受電上限は`enforced`（LPで強制）、導入前ピーク**1,207.4 kW** → 導入後**1,115.3 kW**（上限内）、
`generation_kwh`=**1,007,698**、`import_kwh`=**8,809,655**、年間コスト削減額**25,407,417円**、
設備投資合計**558,000,000円**（単純回収**22.0年**）、`co2_reduction_t_per_year`=**431.666**。
※ Codex・Claude Code での実機検証（2026-09-20）でも全項目一致（手順は pv-sim-biz の `docs/dc_agent_verification.md`）。

**上限を守れない例**: 同じ条件でIT容量を「中規模（5,000kW）」にして系統受電を「高圧6.6kVに収める（2,000kW未満）」に
すると、`simulate_dc`は**エラーではなく診断**（`grid_cap_infeasible=true`）を返します。蓄電池の出力不足とエネルギー収支の
不足が理由で、「蓄電池を大きくしても解決しません」「上限を約5,497kW以上にする／PVを増やす／IT負荷を下げる」といった
次の一手が返ります。

---

## 6. サーバーをまたいだ活用例 / Cross-server example

ハブアプリを作らずに複数サーバーへ同時接続することで実現できる、統合的な問いかけの例です。

*An example of the integrated experience that comes from connecting to multiple
servers at once, without any hub app.*

```
系統用蓄電池としてスポット市場でアービトラージする場合（pv-sim-fipの
simulate_grid_battery）と、工場の自家消費用として導入する場合（pv-sim-bizの
simulate_industrial_pv、PVなしで蓄電池のみ）で、同じ容量（1,000 kWh、
PCS500kW、単価6.8万円/kWh）の蓄電池単独投資を比較してください。
地点は東京、JEPX年度は2023〜2025年度平均で揃えてください。
どちらのビジネスモデルの方がこの蓄電池にとって有利か教えてください。
```

この問いには`pv-sim-fip`と`pv-sim-biz`の2サーバーへの接続が必要です。単一の
アプリでは扱えない横断比較ですが、MCPなら「複数サーバーに繋ぐだけ」で
エージェントが両方を呼び分けて答えを合成してくれます。

*This question requires both `pv-sim-fip` and `pv-sim-biz`. No single app handles
this cross-domain comparison — but with MCP, simply connecting to both servers lets
the agent call each one and synthesize the answer.*

---

## ヒント / Tips

- 各`simulate_*`ツールは30秒〜90秒かかることがあります（LPを含む場合）。
  `validate_*_params`は即答なので、まずそちらで条件を確認してから実行すると良いです。
  *Each `simulate_*` tool may take 30-90 seconds when it involves an LP. The matching
  `validate_*_params` tool answers instantly — check your inputs there first.*
- 全ての結果には`caveats`（モデルの前提・限界）が含まれます。エージェントに
  「caveatsも教えて」と頼むと、見落としがちな注意点を確認できます。
  *Every result includes `caveats` describing model assumptions/limits. Ask the
  agent to surface them if it doesn't mention them on its own.*
- `list_stations`で地点番号を確認できます。地点名で聞いても、エージェントが
  自動で番号に変換してくれます。
  *Use `list_stations` to look up station numbers — or just name a city and the
  agent will resolve it for you.*
