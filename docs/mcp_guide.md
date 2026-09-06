# MCP接続ガイド / MCP Connection Guide

pv-sim ファミリー（pv-sim-fip / pv-sim-gh / pv-sim-biz）は、それぞれの
Hugging Face Space上で [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
サーバーとして計算機能を公開しています。Claude Code・Claude Desktop・OpenAI Codex CLI
など、MCP対応の任意のAIエージェントから自然言語のまま直接呼び出せます。

*This guide is bilingual (Japanese first, English follows each section). The pv-sim
family (pv-sim-fip / pv-sim-gh / pv-sim-biz) each expose their calculation engine as
an [MCP](https://modelcontextprotocol.io/) server on their Hugging Face Space. Any
MCP-compatible AI agent（Claude Code, Claude Desktop, OpenAI Codex CLI, etc.）can call
them directly using natural language.*

---

## 1. 設計思想: ハブを作らない / Design: No Hub Needed

このプロジェクト群は「統合アプリ」を作りません。各Spaceが独立したMCPサーバーとして動き、
エージェント側が複数サーバーに**同時接続**することで統合体験を実現します
（新規Space不要＝HFの無料クォータを消費しない）。

*This project family does not build a single "hub" app. Each Space runs as an
independent MCP server, and the integrated experience comes from the agent
connecting to **multiple servers simultaneously** — no new Space, no extra HF quota
consumed.*

ツール名はGradioによって各Spaceのスラッグで自動的に名前空間化されるため
（例: `pv_sim_fip_list_stations`、`pv_sim_gh_list_stations`）、3サーバーに同時接続しても
ツール名が衝突しません。

*Tool names are automatically namespaced by Gradio using each Space's slug (e.g.
`pv_sim_fip_list_stations`, `pv_sim_gh_list_stations`), so connecting to all three
servers at once never causes a name collision.*

---

## 2. サーバー一覧 / Server List

| プロジェクト / Project | エンドポイント / Endpoint | 対象 / Scope |
|---|---|---|
| pv-sim-fip | `https://hachinai-pv-sim-fip.hf.space/gradio_api/mcp/` | FIP転＋蓄電池（新規/既存FIT転）、系統用蓄電池単独（PVなし） |
| pv-sim-gh | `https://hachinai-pv-sim-gh.hf.space/gradio_api/mcp/` | 住宅用PV＋蓄電池＋需給＋経済性比較 |
| pv-sim-biz | `https://hachinai-pv-sim-biz.hf.space/gradio_api/mcp/` | 産業用（高圧/特別高圧）自家消費＋マイクログリッド |

### ツール一覧 / Tools

**pv-sim-fip** (`pv_sim_fip_` prefix):
`list_stations` / `get_jepx_stats` / `estimate_pv_generation` /
`validate_fip_params` → `simulate_fip_case_a` / `simulate_fip_case_b` /
`validate_grid_battery_params` → `simulate_grid_battery`

**pv-sim-gh** (`pv_sim_gh_` prefix):
`list_stations` / `estimate_pv_generation` /
`validate_residential_params` → `simulate_residential_pv`

**pv-sim-biz** (`pv_sim_biz_` prefix):
`list_stations` / `estimate_pv_generation` /
`validate_industrial_params` → `simulate_industrial_pv`

（`A → B` は「Aで検証してからBを呼ぶ」設計であることを示す。詳細は§4参照）
*(`A → B` denotes "validate with A, then call B" — see §4 for why.)*

---

## 3. 接続方法 / How to Connect

### Claude Code

```bash
claude mcp add --scope user --transport http pv-sim-fip https://hachinai-pv-sim-fip.hf.space/gradio_api/mcp/
claude mcp add --scope user --transport http pv-sim-gh  https://hachinai-pv-sim-gh.hf.space/gradio_api/mcp/
claude mcp add --scope user --transport http pv-sim-biz https://hachinai-pv-sim-biz.hf.space/gradio_api/mcp/
```

3行とも実行すれば、以降このマシン上の全Claude Codeセッションから3サーバーに同時接続できます。
1つのプロジェクトだけ使うなら該当行だけで十分です。

*Run all three lines to connect to all three servers from every Claude Code session
on this machine going forward. If you only need one project, run just that line.*

### Claude Desktop

設定ファイル（`claude_desktop_config.json`）の`mcpServers`に以下を追加:

*Add the following to the `mcpServers` section of your `claude_desktop_config.json`:*

```json
{
  "mcpServers": {
    "pv-sim-fip": { "url": "https://hachinai-pv-sim-fip.hf.space/gradio_api/mcp/" },
    "pv-sim-gh":  { "url": "https://hachinai-pv-sim-gh.hf.space/gradio_api/mcp/" },
    "pv-sim-biz": { "url": "https://hachinai-pv-sim-biz.hf.space/gradio_api/mcp/" }
  }
}
```

### OpenAI Codex CLI

Codexはチャットに自然言語で指示するだけで、自分でセットアップコマンドを実行できます。
以下をそのまま貼り付けてください:

*Codex can run the setup command itself from a natural-language instruction. Paste
this directly into a Codex chat:*

```
以下のリモートMCPサーバーを3つとも追加してください:
- 名前: pv-sim-fip / URL: https://hachinai-pv-sim-fip.hf.space/gradio_api/mcp/
- 名前: pv-sim-gh  / URL: https://hachinai-pv-sim-gh.hf.space/gradio_api/mcp/
- 名前: pv-sim-biz / URL: https://hachinai-pv-sim-biz.hf.space/gradio_api/mcp/
追加できたら、それぞれのツール一覧を確認して教えてください。
```

認証は不要です（公開エンドポイント）。

*No authentication required — these are public endpoints.*

---

## 4. 誤解釈ガードレール: validate → 確認 → simulate / Misinterpretation Guardrail

重い計算ツール（`simulate_*`）は、必ず対応する`validate_*_params`を**先に**呼ぶ設計に
なっています。理由:

*Heavyweight `simulate_*` tools are designed to always be preceded by their matching
`validate_*_params` call. Why:*

1. パラメータを正規化し、単位・デフォルト値を明示した上でユーザーに確認を求められる
   （エージェントが数値を勝手に解釈して計算を進めるリスクを減らす）。
   *Normalizes parameters and surfaces units/defaults so the agent can confirm with
   the user before running — reducing the risk of silently misinterpreted inputs.*
2. LPを含む重い計算（30〜90秒）を実行する前に、明らかな入力ミスを即答でエラーにできる。
   *Catches obvious input errors instantly, before running an expensive LP (30-90s).*
3. 全simulate結果に`caveats`（モデルの前提・限界）が同梱されるため、エージェントが
   結果をユーザーに伝える際に見落としにくい。
   *Every simulate result carries `caveats` describing model assumptions/limits, so
   the agent is less likely to omit them when reporting back to the user.*

エージェントには「まずvalidateを呼び、返ってきた`normalized_params`をユーザーに提示して
確認を得てからsimulateを呼ぶこと」という指示が各ツールのdocstringに明記されています。

*Each tool's docstring instructs the agent to call validate first, show the user the
returned `normalized_params`, get confirmation, then call simulate.*

---

## 5. 動作実績 / Verified Compatibility

Claude Code と OpenAI Codex CLI の両方から接続し、同一パラメータでの計算結果が
1円単位で完全一致することを確認済みです（3サーバー全て）。詳細は
[agent_design.md §8.5](./agent_design.md) を参照。

*Verified from both Claude Code and OpenAI Codex CLI — results match to the yen
across all three servers. See [agent_design.md §8.5](./agent_design.md) for details.*

---

## もっと試したい場合 / Want more examples?

具体的な貼り付け用プロンプト集は [example_prompts.md](./example_prompts.md) を参照。

*See [example_prompts.md](./example_prompts.md) for a curated collection of
ready-to-paste prompts.*
