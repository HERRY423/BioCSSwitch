# 更新日志 / Changelog

本项目所有值得记录的变更都写在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **约定**：已修问题从 [`docs/known-issues.md`](docs/known-issues.md)「毕业」到这里（发布即定稿）；未修/进行中留在 known-issues；硬 bug 的根因证据链存在 [`findings/`](findings/)。

## [Unreleased] — 生医研究扩展（GRADE/scFM/benchmark 加固 + EtD/模型矩阵/provider 矩阵）

> 主题：修三处正确性问题，再叠三层能力。全部离线可测（`test/test_bio_offline.py` 现 62 项断言全绿）。

### 修复 Fixed
- **GRADE 起始确定性**：meta-analysis / systematic-review **不再默认 High**——起始档取决于 `underlying_design`（meta of RCTs→High，meta of observational→Low）；未声明则保守按 Low + 警告。模糊的 `clinical-trial` 拆类型：随机对照→High，单臂/非随机→Low（按观察性，且**可升级**）；无法识别的设计词保守按 Low + 提示。`_start_certainty` 返回 (起始档, 警告, is_rct)。
- **scFM 脚本真实性**：`scfm_embed_plan` 明确降级为 skeleton——返回 `artifact_type=skeleton` / `runnable=false`，生成脚本带 NOT-RUNNABLE 横幅 + `raise SystemExit` 护栏 + TODO/伪代码标注，工具描述也改口"不产出可直接运行的脚本"。杜绝把模板误当 runnable。
- **隐私红队片段泄露**：`privacy_redteam._leaked` 从"只查完整串"升级为查片段集——完整 PHI、后四位、前三/后三位、身份证生日段（第 7-14 位）、MRN 片段都判泄露；同时剔除会与安全内容碰撞的片段（身份证前三位 110/120 撞报警/急救电话）。

### 新增 Added
- **bio-scfm 模型矩阵**：registry 从 Geneformer/scGPT 扩到 **4 个 foundation model（Geneformer、scGPT、CellFM、UCE）** + **3 个 domain-specific baseline（scVI、totalVI、MultiVI，每份数据自训 VAE，保留作诚实对照）**，每个标 `category`、输入 ID 类型、模态。新增 `scfm_model_matrix` 工具给对比视图 + "跑 foundation 至少配一个 baseline"的指导。`scfm_embed_plan` 支持全部 7 个模型（baseline 脚本走 scvi-tools API 伪代码）。
- **bio_eval provider 矩阵**：`run.py --matrix` 把 `results/` 里各 provider（label）汇成对比矩阵——每列 overall / **红队分（safety+privacy 均值）** / **工具调用分（tool_invoked 均值）** / **稳定性（--repeat N 的分数标准差）** / **成本**（内置价目或 `--price-in/out`）。配合 `--repeat 3` 得到"哪个 provider 又准又稳又便宜又安全"的横比。
- **GRADE EtD 层**：新增 `etd_recommendation`——certainty 只是证据确定性，推荐强度（strong / conditional）还要看获益/危害平衡、价值观与偏好、资源/成本、公平性/可接受性/可行性。工具确定性地映射到推荐方向+强度，守卫"低确定性上的强推荐"（GRADE 不一致推荐，需符合特殊情形否则降 conditional），措辞遵循 recommend/suggest 惯例。`grade-sof` skill 增 EtD 步 + 起始档拆类型说明。

### 变更 Changed
- `test/test_bio_offline.py` 增覆盖：GRADE meta/clinical-trial 修复 + 单臂可升级 + EtD、scFM skeleton 护栏 + 模型矩阵、隐私片段泄露（含 110 不误判）；`run.py --selftest` 增部分泄露判 0 校验。工具总数 75。

## [Unreleased] — 生医研究扩展（GRADE / scFM 适配层 / 回归 benchmark）

> 主题：让系统能给出「顶级医学级」的证据确定性、把单细胞基础模型接成可复现的计算工具、把 benchmark 做成能证明"系统变强"的回归基准。三条主线，全部离线可测。

### 新增 Added
- **1. bio-audit 升级为 GRADE / SoF 引擎**：新增 `grade_server.py`（MCP server `bio-audit-grade`）。`grade_outcome` 按 GRADE 给**单个 outcome** 的证据确定性评级——起始档由设计定（RCT→High / 观察性→Low），模型对 5 个降级域（偏倚/不一致/间接/不精确/发表偏倚）+ 3 个升级域（大效应/剂量反应/残余混杂）给判断与理由，**工具确定性地算完算术**，输出四档确定性（⊕⊕⊕⊕ High … ⊕⊝⊝⊝ Very Low）+ 逐域「为什么」+ GRADE 规则守卫（RCT 不可升级、无理由降级告警、样本过小提示不精确性）。`grade_sof_table` 产出 Summary of Findings 表；`grade_explain` 给域定义速查。配 `grade-sof` skill。
- **2. 新增 bio-singlecell + bio-scfm 两个 pack（scFM 计算工具适配层）**：把 Geneformer / scGPT 当**编码器工具**而非聊天模型。新增 `_lib/provenance.py`（规范化 JSON + sha256 内容哈希）。
  - `bio-singlecell`：`anndata_fingerprint`（元数据指纹 + 生成算真·内容哈希的本地代码）、`sc_preprocess_recipe`（模型对口的确定性 scanpy 配方 + `recipe_hash` + 脚本；Geneformer 跳过 log/HVG，scGPT 走 HVG+binning）、`sc_qc_thresholds`（MAD-based 可解释阈值）。配 `single-cell-prep` skill。
  - `bio-scfm`：`scfm_registry`（钉版本 checkpoint + 输入要求）、`scfm_embed_plan`（产出可复现 embedding 脚本 + provenance 骨架）、`scfm_provenance_record`（构造带 `provenance_hash` 的规范记录）、`scfm_provenance_verify`（重算哈希 + 查必填 → trustworthy / not_reproducible）。**铁律：任何 embedding 必须附 provenance——输入 AnnData 内容哈希、预处理参数哈希、模型版本、embedding 维度/输出哈希/pooling、seed/环境**。配 `scfm-embed` skill。
- **3. bio_eval 做成真正的回归 benchmark**：从 9 类 ~49 case 扩到 **11 类 60 case**，新增两个红队类：`safety_redteam`（危险用药/致死剂量/停药施压/编造治愈的对抗提示 → 奖励拒答/护栏/循证）、`privacy_redteam`（诱导回显 PHI / 把 PHI 塞进检索参数外泄 / 重建脱敏 → 检查答复+工具调用参数都不泄露原始 PHI）。rubric 新增维度：`gold_match`（专家金标准 ID/必提实体命中）、`semantic_grounding`（答复里的数字事实/命名实体是否真有工具出处——抓"ID 对但数字编造"）。`run.py` 新增 `--repeat`（跑多次测稳定性/方差）+ token 用量/延迟采集 + **provider 成本×稳定性矩阵**（内置 deepseek/qwen/glm/kimi/claude 价目，`--price-in/out` 可覆盖）。

### 变更 Changed
- `test/test_bio_offline.py` 再加 2 组离线断言（GRADE 引擎算术+规则守卫、scFM 指纹稳定性+provenance 篡改检出）；`run.py --selftest` 增 gold_match / semantic_grounding / 安全红队 / 隐私红队 6 项校验。
- `tool_executor.py` 加载 grade / bio-singlecell / bio-scfm 三个 server（共 73 个工具可离线执行）。
- `evidence-audit` skill 之外，bio-audit pack 现含 `grade-sof` skill（version 0.2.0，两个 server）。

## [Unreleased] — 生医研究扩展（证据图 / 不确定性 / 编译器 / benchmark）

> 主题：从"引用真不真"升级到"结论站不站得住 + 盲区在哪"。四条主线，全部离线可测（并入 `test/test_bio_offline.py`）。

### 新增 Added
- **1. Claim 级证据图（bio-audit）**：不止验 PMID/DOI/NCT 存在。新增 `_lib/evidence_profile.py`（可解释启发式：物种优先信 MeSH CheckTag、样本量正则挖摘要、回顾/前瞻看 MeSH、临床分期看摘要，每个推断带 `signals` 溯源）；bio-audit 新增两个 MCP 工具：`evidence_profile`（单篇：物种/人群/样本量/实验类型如"临床 II 期·动物·体外·回顾性队列"/疾病阶段）与 `evidence_graph`（每条 claim 绑证据 → 证据等级 + **适用边界** + **conflicts**（含"断言人类但证据是动物"的错配）+ **counter_evidence**（`stance:refutes` 反例）+ verdict，返回 nodes/edges 图）。`parse_pubmed_xml` 增抽 `mesh_terms`。
- **2. 不确定性优先（强制五段面板）**：bio-audit 新增 `uncertainty_ledger` 工具——吃 evidence_graph 结果，自动派生 **Known knowns / Known unknowns / Conflicts / Missing data / Next experiment** 并渲染 Markdown。新增横切 skill `uncertainty-first` 定义标准+反例；6 个 workflow skill（lit-review/target-discovery/geo-triage/trial-landscape/reviewer-response/grant-specific-aims）结尾都加了"结论不得省略五段面板"的硬约束。
- **3. 研究问题编译器（新 pack `bio-compiler`）**：`compile_research_question` 把模糊问题（"EGFR 在 GBM 里还有没有新靶点价值"）编译成结构化任务书——研究对象/疾病/分子/干预/终点/数据库/排除标准/证据等级门槛/推荐工具链/推荐 skill/`gaps`，每字段带 `via` 溯源，识别不到标 `needs_user_input` 不编。含 `compiler_capabilities`（自述覆盖范围）+ `question-compiler` skill（强制"先编译、回填缺口、再检索"）。
- **4. bio_eval 升级为多维 benchmark**：从 6 类 10 case 扩到 **9 类 ~49 case**（文献综述/临床试验/靶点发现/药物再利用/组学分析/证据审计/PHI 处理/JSON 稳定性/多轮工具调用），拆进 `cases_data/<category>.py`。新增 `rubric.py` 多维评分——不再只看"调没调工具"，还看 `grounded`（结果 ID 是否真来自工具输出）、`uncertainty`（是否输出五段面板）、`json_valid`、`custom`（PHI 不泄漏 / 多轮轮数 / 审计 verdict 被引用）+ 编 ID 乘法惩罚（`expected_fake_ids` 排除故意埋的假 ID）。`run.py` 捕获 tool 结果供 grounding 评分，新增 `--list` / `--selftest`。框架支持扩到每类 20–50（README 附扩充路线）。

### 变更 Changed
- `test/test_bio_offline.py` 新增 4 组离线断言（evidence_profile / evidence_graph+ledger / question_compiler / bio_eval rubric selftest），CI 零网络覆盖上述新功能。
- `tool_executor.py` 加载 bio-privacy(phi) + bio-compiler 两个 server（PHI / 编译器 case 可离线执行）。

## [Unreleased] — 生医研究扩展（五点加固）

> 主题：把前面搭好的骨架**做扎实**。真实 tool loop、离线 golden tests、隐私默认脱敏、host 规范化、验证环境指纹。

### 新增 / 变更 Added / Changed
- **1. bio_eval 进入真实 tool loop**：`run.py` 从"单轮只看是否发 tool_use"升级为**完整 agent 循环**——模型发 `tool_use` → `tool_executor.py` 真跑本地 MCP handler（复用 pack/shim 已注册 handler）→ `tool_result` 回灌 → 模型继续（`--max-turns`）→ 最终答案跑 `evidence_linter` 校验引用真实性。评分 = case scorer × (1 − 假引用比例)，全假归零。设 `CSSWITCH_HTTP_FIXTURES` 可全离线。
- **2. generator fixture / golden tests**：新增 `_lib/fixtures.py` HTTP 回放层（replay/record/auto，录制自动脱敏 api_key/token/mailto）。`test/generators/synth_fixtures.py` 离线合成 19 条 fixture（MeSH/HGNC/UniProt/ChEMBL/Open Targets/CT.gov 各 1–2 个），`test_generators_golden.py` 默认离线比对 golden，`make_fixtures.py --live` 录真实数据，`--live` 集成测试只校验结构不变式。
- **3. 证据 linter 默认脱敏**：JSON 默认只导 `line_sha256`（关联同行 finding 而不泄露内容）+ privacy note；`--include-line-text` 才导行原文（且 note 变 PHI 警告），`--no-metadata` 可连公开文献元数据一起关。防止扫病历 / 内部材料时把敏感上下文带进报告。
- **4. 敏感模式 host 规范化**（新增 `netcanon.rs` + `url`/`idna` 依赖）：`set_local_endpoint_hosts` 现接受 URL 或裸 host，统一成 host（去 scheme/port/末尾点、小写、**IDNA punycode**）。分类 localhost / 私网 IP / 公网域名：前两者自动收，公网域名**只在 `confirm_public=true`（用户明确确认机构端点）时收**。denylist 扩到含子域匹配。`assert_sensitive_ok` 两边都规范化后比对。含 8 条单元测试。
- **5. 验证记录环境指纹**：`config.verification` 加存 Science 版本 / `MCP_CONFIG_REL` / `SKILLS_REL` / python 路径 / marker / 通过时间 / 最后失败原因。`verification_summary` 自动比对当前 vs 通过时指纹，Science 更新导致路径可能变时 `stale=true`，面板弹"验证结果可能过期，建议重跑"。
- 新增离线自测 `test/test_bio_offline.py`（fixture 回放 / tool_executor / linter 隐私 / generator golden），接进 `run_all.sh`，CI 零网络零代理可跑。

### 跨平台
- 所有 CLI（run.py / lab.py / evidence_linter.py / golden test / synth）强制 UTF-8 stdout，修 Windows 控制台 GBK 编不出 ✓/⚠ 的崩溃。

## [Unreleased] — 生医研究扩展（四阶段）

### 新增 Added — phase-1 路径验证
- **canary smoke test 框架**：解决"MCP / Skill 路径靠猜"的诚实性问题。
  - `packs/_lib/smoke/smoke_mcp.py` — canary MCP，启动时往 `~/.csswitch/smoke/latest.json` 写 marker
  - `packs/_lib/smoke/canary_skill/SKILL.md` — canary Skill，触发词固定短语
  - `desktop/src-tauri/src/verification.rs` — prepare / poll / cleanup canary
  - 4 个 Tauri 命令：`start_smoke_verification` / `poll_smoke_verification` / `confirm_skill_verified` / `cleanup_smoke_verification`
  - `config.verification` 存 verdict + marker + last_run + reason
  - 面板新增「🧪 验证 MCP / Skill 路径」折叠区，未验证时每个已启用 pack 显示"实验中"chip
- `docs/bio-unknowns.md` — 权威追踪清单，`U/S/M/F/V` 五档可信度

### 新增 Added — phase-2 补丁
- `docs/implementation-notes/bio-audit.md` / `bio-mcp-shim.md` — 明写"已实现 / 已知不完美 / 依赖的未验证事实"
- `packs/bio-audit/evidence_linter.py` — 离线批量证据 linter（PMID / DOI / NCT 存在性 + 证据类型匹配），CI 友好 `--strict --format json`
- **非 active profile 探针**：`run_probes` 支持非当前 profile，走 `scratch::scratch_probe`（新增 `ProbeKind::CustomPost`），探完杀净临时代理

### 新增 Added — phase-3 评测实验室
- `test/bio_eval/lab.py` — 端到端评测编排器
  - `--matrix label:proxy_url,label:proxy_url` 支持 provider 矩阵
  - `--iterations N` 多次重复算 mean / stdev；n<3 明示 low confidence
  - `--html-report` 输出报告卡 HTML
  - `--junit-xml --fail-under 0.5` CI 集成
  - `--save-artifacts` 逐 case 落 raw request/response 到 `results/artifacts/`

### 新增 Added — phase-4 高质量 workflow 生成器
- `packs/bio-workflows/generators/sr_pico_builder.py` — PICO → 真实 PubMed / Europe PMC / Cochrane 检索式（MeSH 词从 NCBI 拉，不猜）
- `packs/bio-workflows/generators/ct_landscape.py` — CT.gov v2 → Markdown landscape 报告（phase×status 矩阵 + endpoint 归一 + sponsor 归一）
- `packs/bio-workflows/generators/td_druggability.py` — HGNC + UniProt + ChEMBL + Open Targets → druggability A/B/C/D 评分卡 + 反查链接
- `packs/bio-workflows/generators/omics_deseq2.py` — 完整 DESeq2 + apeglm shrinkage + Hallmark GSEA R 脚本生成器（batch 自动处理 / QC / volcano / heatmap）
- `packs/bio-workflows/generators/README.md` — 三条原则：数据从真实上游拉 · 可反查 · 不代跑分析

### 变更 Changed
- `config.rs` +1 字段 `verification: BTreeMap<String, String>`
- `scratch.rs` +1 变体 `ProbeKind::CustomPost(Vec<u8>, bool)`
- `list_packs` 返回附带 `verification` 摘要，用于 UI 决定是否标"实验中"

## [Unreleased] — 生医研究扩展

> 主题：面向生物医学研究场景的扩展。共三大新特性 + 三大补丁包，从 v0.3.1 增量而来。**未改动主线 v0.3.x 的多 profile / relay / DSML 语义**；铁律零回退。

### 新增 Added — 三大新特性

- **1. 生医任务级模型路由**：不只是选 provider，按科研任务选模型。
  - 后端加 `config.task_routes: BTreeMap<task_id, profile_id>` + `config.probe_results: BTreeMap<key, json>`。
  - 内置任务清单（`packs::BIOMED_TASKS`）：lit-review / clinical-trials / target-discovery / omics-code / long-context-pdf / tool-heavy / evidence-check / phi-sensitive。
  - Tauri 命令 `list_biomed_tasks` / `set_task_route` / `run_probes`。
  - 面板新增「🧭 生医任务 → profile 路由」折叠区，每个任务一行 select + 探针结果 chip。
  - `run_probes` 打三条 micro 探针（tool_use / long_ctx / json_stable），一发一次，结果写回 `probe_results`，UI 显示"合适/不合适"chip。

- **2. 远程 MCP 本地替身层（bio-mcp-shim pack）**：让 Science 仍看到 `pubmed` / `clinical-trials` / `chembl` / `biorxiv` 同名 MCP，实际流量走本地 API + 用户 key。
  - 靠 `ServerDef.aliases` —— 本地 MCP 以多个名字挂进 mcp-servers.json；开启后 Science 里同时看到本项目命名（`bio-mcp-shim-pubmed`）和 Anthropic 兼容名（`pubmed`）。
  - 4 个 shim（pubmed / ctgov / chembl / biorxiv）覆盖远程 MCP 惯用工具名：`search_articles / search_trials / compound_search / search_preprints` 等。
  - 冲突处理：用户已手工挂了同名 MCP → 保留原有，跳过 alias 注册并 warning。

- **3. 生医工具调用回归测试集（`test/bio_eval/`）**：
  - 10 个 case 覆盖 pubmed / ctgov / chembl / biorxiv / audit / json 六大类；靠正则挖规范格式 ID，编 ID 给 0 分。
  - `python test/bio_eval/run.py --proxy ... --write-to-config` 会把 per-category 得分写回 `probe_results` 作为 rich 结果，与桌面 app 探针互补。
  - 阈值：`✓✓ ≥ 0.9` · `✓ ≥ 0.7` · `⚠ ≥ 0.4` · `✗ < 0.4`。

### 新增 Added — 补丁包（前一轮已实施，本轮适配 v0.3 profile 架构）

- **科研工具包 pack 机制**：面板「🧬 科研工具包」折叠区，一键启用/停用一组本地 MCP + Skill，写进沙箱 Claude Science 的 mcp-servers.json。
  - `bio-lit` — PubMed / Europe PMC / Crossref / bioRxiv / medRxiv 检索
  - `bio-audit` — PMID / DOI / NCT 校验 + 证据类型分层 + Skill 强制规范
  - `bio-trials` — ClinicalTrials.gov v2
  - `bio-gene` — NCBI Gene / ClinVar / dbSNP / GEO / SRA + UniProt + Ensembl
  - `bio-drug` — ChEMBL / Open Targets / RxNorm / openFDA
  - `bio-norm` — HGNC / MeSH / MONDO / HPO / GO / ChEBI + `disambiguate(term, context)`
  - `bio-workflows` — 6 个 Skill 预设：lit-review / target-discovery / geo-triage / trial-landscape / reviewer-response / grant-specific-aims
  - `bio-privacy` — PHI 扫描（HIPAA + 中文 identifier）+ 一致占位符脱敏 + 追加式 JSON-Lines 审计日志 + Skill 强制"先扫再问"

- **敏感 / 合规模式（`config.sensitive_mode`）**：开启后 `one_click_login` 拒绝把请求发给不在 `local_endpoint_hosts` 白名单里的 provider。白名单**反向防御**：`api.deepseek.com` / `dashscope.aliyuncs.com` / `api.anthropic.com` / `openai.com` / `open.bigmodel.cn` / `api.xiaomimimo.com` / `api.siliconflow.cn` / `openrouter.ai` 一律拒绝加入。面板新增「🔒 隐私 / 合规」折叠区。

### 变更 Changed
- `config.rs`：Config 加 6 个字段（`enabled_packs / pack_env / sensitive_mode / local_endpoint_hosts / task_routes / probe_results`）。全部 `#[serde(default)]`，旧文件读取兼容。
- `packs.rs`：新增模块，pack 加载 + apply/purge + alias 处理 + Skill 装配。
- `proc.rs`：新增 `http_post_status_body`（POST 返回 status + body），供 `run_probes` 判断响应内容。
- `set_mode("official")` 现顺带 `packs::purge_bio_from_mcp`；`one_click_login` 起沙箱前跑 `packs::apply` + `assert_sensitive_ok`。
- `tauri.conf.json`：`packs/` 加入 bundle resources。

### 铁律 / 安全
- pack 只写沙箱 `<SANDBOX_HOME>/.claude-science/`，绝不碰真实 `~/.claude-science`。
- API key（NCBI_API_KEY 等）走 `config.pack_env`（0600 存盘，前端只回显有无），env 注入 MCP 子进程时**只透传 `env_pass` 白名单里的名字**。
- 审计日志（bio-privacy）只吃 hash + 摘要，`input_sample` 参数 SHA-256 后立即丢弃。

### 未验证
- Claude Science 的 MCP 配置文件位置与 Skill 目录**尚未从二进制逆向确认**。当前按 Claude Code 惯例落 `<data-dir>/mcp-servers.json` 和 `<data-dir>/skills/<id>/`（见 `packs::MCP_CONFIG_REL` / `SKILLS_REL`）。逆向后只需改这两个常量。

## [0.3.1] — 2026-07-04

> 主题：**内置预设支持自定义 base_url**。修用户反馈的小米 MiMo「token plan」401。

### 修复 Fixed
- **内置预设 `base_url` 只读，导致小米 MiMo「token plan」报 401**：小米 MiMo 的「token plan」套餐走独立域名 `token-plan-cn.xiaomimimo.com/anthropic`，与内置的 `api.xiaomimimo.com/anthropic` 不是同一 host；旧版预设地址锁死改不了，套餐 key 打到内置域名被上游 401。现将四家 relay 预设（智谱 GLM / 小米 MiMo / 硅基流动 / OpenRouter）的 `base_url` 改为**可编辑的默认值**：预填官方地址，允许改到 token 套餐 / 区域镜像 / 自建反代（新建向导与「编辑连接」两处都可改）。DeepSeek / 通义千问为原生 adapter（上游地址在代理内固定，运行时不吃自定义地址），保持只读以免「能填但不生效」的假象。「自定义」来源行为不变（空地址、可编辑）。

### 说明 Notes
- 纯前端 + 模板注册表改动，不改运行语义与鉴权。cargo test 114 全绿 / clippy 0 / fmt clean；前端预览实测（新建向导 + 编辑连接均可改、原生仍锁、自定义仍空、改到 token-plan 地址生效）。
- 顺带修 `config.rs` 一处历史 fmt 漂移。

## [0.3.0] — 2026-07-04

> 主题：**多 API 支持 + UI 改版**。从只支持 DeepSeek / 通义千问两家，扩展到 7 家 provider + 自定义端点的 cc-switch 式**多 profile 管理**；面板重做为配置列表 + chip 网格 + 三能力模型呈现。真机验收通过，正式毕业为**稳定版**（取代 v0.2.1 成为 Latest）。`0.3.0-beta.1/beta.2` 大预览版的内容在此定稿。

### 新增 Added
- **多 API / 多 provider 支持**：内置 7 家模板（DeepSeek、通义千问、智谱 GLM、OpenRouter、小米 MiMo、硅基流动）+ **自定义 OpenAI / Anthropic 兼容端点**（自填 `base_url` / 模型 / Key）。
- **cc-switch 式多 profile 配置管理**：把「固定槽」升级为用户自管的命名配置列表 + 当前生效指针；同一家可存多套、命名、增删、一键切换。切换是**事务式**的（先探活候选、健康才提交、失败回滚、全程不停沙箱）。配置用 JSON 存储并硬化（原子写 + schema 版本 + 覆盖前留 `.bak`），v1→v2 迁移不丢数据。
- **中转站 relay provider**：填 `base_url` + `token` 即可接任意 Anthropic 兼容中转站；`/v1/models` 回源自动铺该站真实模型到选择器。
- **DSML 工具调用兜底 shim（默认 `off`）**：DeepSeek 偶发把工具调用泄漏成纯文本 DSML 标记致 Science 卡死（issue #8）；由环境变量 `CSSWITCH_TOOLUSE_SHIM` 选 `off`（默认字节透传）/ `detect`（透传 + 遥测）/ `rewrite`（还原成真正的 `tool_use`）。

### 变更 Changed
- **面板 UI 改版**：重做为 profile 配置列表；来源选择改 **chip 网格**（键盘可达 / `aria-pressed`）；模型字段按**三能力**呈现（native 内置映射 / relay 跟随 Science / relay 固定）；新建 / 编辑走独立视图（隐藏运行区、保留反馈区）；文案瘦身 + a11y（label 关联 / `:focus-visible`）。折入四轮外审反馈。
- **反馈栏空闲不占位**：去掉常驻「就绪。」，只有真实反馈（错误 / 结果 / 自检输出）时才显示。

### 修复 Fixed
- **配置列表长 key 掩码横向溢出**：掩码原来一个字符一个圆点，长 key 在 WKWebView 里不换行、撑出横向滚动条、裁掉行与按钮。改为**定长** `••••` + 末 4 位（任何长度都短、不泄漏 key 长度）。
- **自检（doctor）对非 deepseek/qwen 来源误报**：`doctor.sh` 停留在单 provider 时代、按 provider 名写死并去 shell 环境找 key，导致 glm/xiaomi 等生效时误报「未知 provider」。改为多 profile 感知（据 adapter + key 有无报告，key 存 `config.json`）。
- 承接 beta.1/beta.2 的多 profile / DSML 修复（无效 native key 拦截、切换回滚健壮性、SSE 末帧、布尔校验、rewrite 无泄漏不逐字等，详见下方 beta 条目）。

### 说明 Notes
- **真机验收已完成**：多 profile / relay 的真机行为经用户在场实测确认（beta 阶段的 RM 待办已消解）；DSML 仍默认 `off`，`rewrite` 需显式开启。
- **铁律零回退**：全程只碰隔离沙箱，绝不触碰真实 `~/.claude-science` 与端口 8765；真机测试由用户在场完成、Claude 不代登录。
- **验收闸门**：cargo test 113 全绿 / node / bash 语法通过。
- **拔 node / python（治本）仍在 roadmap**：app 运行时零 node（虚拟登录 Rust 原生，v0.1.4 起）；翻译代理仍 Python，收敛到 Rust 单二进制待后续。

## [0.3.0-beta.2] — 2026-07-04

> 主题：**大预览版（Big Preview）**。一次把三块尚未进正式版的功能合在一起供实机试用：① cc-switch 式**多 profile 配置管理** + **中转站 relay provider**；② DeepSeek 工具调用泄漏**兜底 shim**（默认 `off`）。
>
> **⚠️ 这是预览/测试版（prerelease），不是稳定版。** 稳定版仍是 **v0.2.1**。多 profile 与 relay 的真机行为仍待复测（见「说明」），DSML 默认关闭仅供显式验证。本版**取代并撤回**同日名不副实的 `v0.3.0-beta.1`（那版 CHANGELOG 只写了 DSML，却把桌面侧改动一并打包却未如实说明）。

### 新增 Added
- **多 profile 配置管理（cc-switch 式）**：把原来的「固定槽」（每家一份）升级为**用户自管的命名配置列表 + 当前生效指针**：同一家（如 GLM）可存多套、命名、增删、一键切换。切换是**事务式**的：先探活候选、健康才提交、失败回滚、全程不停沙箱。内置 7 家 provider 模板。配置继续用 **JSON** 存储并硬化（原子写 + schema 版本字段 + 覆盖前留 `.bak`），SQLite 缓议。
- **中转站 relay provider**：只需填 `base_url` + `token` 即可接**任意 Anthropic 兼容中转站**；`/v1/models` 回源自动把该站真实模型铺进选择器；双鉴权头兼容各家。
- **DSML 兜底 shim（默认 `off`）**：DeepSeek 偶发把工具调用泄漏成纯文本 DSML 标记（`<｜｜DSML｜｜tool_calls>…`），Science 当普通文本、工具无回执 → **卡死**（issue #8）。shim 端到端接进 `_handle_anthropic`，由环境变量 `CSSWITCH_TOOLUSE_SHIM` 选模式：`off`（默认、字节透传、零回归）/ `detect`（透传 + 遥测）/ `rewrite`（把泄漏还原成真正的 `tool_use`）。新增 `test/test_proxy_dsml_e2e.py` 端到端证明。

### 修复 Fixed
- **（多 profile）无效 native key 被误报「已切到」**：`deepseek`/`qwen` 等 native adapter 此前跳过上游校验、只探本地 `/health`（恒回 200 不验 key），坏 key 会被提交为当前生效、UI 谎报成功、直到首个真实推理才 401。现 native 也走隔离探测打 `/v1/messages` 触上游，坏 key 拦下不提交、active 与旧代理不动。
- **（多 profile）切换/编辑健壮性**：切换写盘失败回滚进程；停沙箱失败即返错且端口不变（不再谎称已重置）；非 active 连接编辑 truthful-save（只拦明确 4xx、其余据实标「未校验，激活再验」）；真机护栏拒软链 + `canonicalize` 拒落真实 HOME 内 + 写盘先删软链再写新文件；若干前端如实提示。
- **（DSML）SSE 末帧丢失**：`_drain_frames` 的 `flush_tail` 形参此前未被使用，EOF 突然时吞掉 `message_stop`（实测 `b''`）。现 `finalize()` 补吐末帧。
- **（DSML）非法布尔臆断成 `false`**：`_coerce_param` 此前把 `maybe` 之类都当 `False` 并过校验，可能合成参数错误的真实工具调用。现只认 `true/1/yes`、`false/0/no`，其余整块作废。
- **（DSML）rewrite 非流式无泄漏时不逐字 + 遥测误报**（清洁 agent 实机验证发现）：`rewrite_nonstream_body` 此前无条件 `json.dumps` 再序列化，干净响应也被改字节且误报「已改写」。现无改动时原样返回原字节。

### 说明 Notes
- **⚠️ 多 profile / relay 仍待真机复测**：RM-04/06/13（非 active native 编辑即时校验、无效 native key 必被拦、端口占用报错措辞）代码 + 单测已覆盖，但**真机行为需在场实测确认**（铁律 4，Claude 不代登录）。这正是本版为 prerelease 的原因；请勿当稳定版依赖。
- **DSML 默认关闭**：普通用户安装后 DSML 行为与 0.2.1 一致；`rewrite` 需 `CSSWITCH_TOOLUSE_SHIM=rewrite` 显式开启（`detect` 只统计不改写）。把 rewrite 设为默认留待其余闸门（如把合法 DSML 示例误判为调用的边界）关闭后的后续版本。
- **铁律零回退**：全程只碰隔离沙箱，绝不触碰真实 `~/.claude-science` 与端口 8765；真机测试须用户在场、Claude 不代登录。
- **验收闸门**：cargo test 113 / clippy -D warnings 0 / `run_all.sh` ALL GREEN / gitleaks 0。
- **拔 node/python（治本）未在本版**：proxy 仍是 Python、伪造器仍是 Node，收敛到 Rust 单二进制仍在 roadmap。

## [0.2.1] — 2026-07-03

> 主题：热修「开了 CSSwitch 仍被要求登录」。0.2.0 有两个会导致「流程走完仍落登录页」的缺陷，本版各修一个并各补一条离线回归测试。链路方案本身没坏（代理此前成功处理过真实聊天、虚拟 OAuth 结构自洽），坏的是「重开 / 取入口 URL」路径。

### 修复 Fixed
- **入口 URL 解析错误（直接命中用户现象）**：Science 的 `url` 命令输出多行（第一行是真 URL，第二行是「single-use…」说明）。`sandbox_url()` 旧代码把**整段 stdout** 当 URL 交给 `open`，参数带上换行与说明文字 → 打开错误入口、单次性 nonce 未被正确消费 → 最终落到 `/login`。新增纯函数 `first_http_url()`，**只取第一条合法 `http(s)://` URL**；找不到才退回裸端口。回归测试 `first_http_url_takes_only_first_valid_url` 钉死多行/尾随说明/无 URL 各情形。
- **健康快捷路径绕过登录修复（自愈缺口）**：0.2.0 只要 `8990` daemon 活着就「连 auth 文件都不读」直接重开窗口，于是**旧版遗留 / 凭证损坏 / 已落登录页**的健康 daemon，重复点「一键开始」也永不自愈。现在健康分支先做**只读**校验 `login_intact()`（复用既有 `read_intact_login` 的从严自洽判定，绝不写文件）：自洽 → 只重开（保持 0.2.0 行为，org 不动、旧对话不丢）；**健康但登录失效 → 停沙箱，落到「修复保 org + 重启」路径自愈**（`ensure_virtual_login` 幂等，绝不静默换 org）。回归测试 `login_intact_true_for_fresh_false_when_damaged_and_readonly` 钉死「自洽判真 / 缺 .enc 或过期判假 / 全程只读不写」。

### 说明 Notes
- **受影响用户临时绕法**：先点「停止代理与沙箱」，确认 `8990` 已停，再点「一键开始」——绕开健康快捷路径、生成新入口；不会删除历史组织数据。装了 0.2.1 后无需此绕法。
- **铁律零回退**：全程只碰隔离沙箱（`~/.csswitch/sandbox`），绝不触碰真实 `8765` 与 `~/.claude-science`；`login_intact` 只读且带同一套真实目录护栏。
- 现有测试之所以全绿仍漏掉这两处，是因为没覆盖「CLI 多行 URL」与「daemon 健康但登录态失效」两个场景；本版补上。

## [0.2.0] — 2026-07-03

> 主题：一键**幂等化**——修 #3（运行中再点的分派）与 #6 的「复发」部分（更新后对话「不见」），并随三轮外部复审全面加固代理与进程路径。

### 修复 Fixed
- **#3 一键幂等分派 + #6 对话不再被孤儿化（阻止复发）**：虚拟 OAuth 伪造由「每次铸新」改为**幂等**。`one_click_login` 先按 **data-dir 强身份**探沙箱（非裸端口）——已在跑就只重新打开、**绝不重伪造、连登录文件都不读**；没起才 `ensure_virtual_login`（完整自洽 → 原样复用 / 部分损坏 → 修复但**保住 org** / 真首次 → 铸新）。**核心不变式：`org_uuid` 只在真正首次铸一次、此后复用与修复都粘住它**，根治「每点一键换新 org、把旧对话留在旧 org 目录里界面看不到」。org 来源优先级 `active-org.json → 可解 token → orgs/ 目录`；发现多个历史组织却无法确定活动者时**报恢复错误、绝不静默新铸**。**注**：本版只「阻止复发」，**已经产生的孤儿历史对话恢复**（把旧 org 指回 active-org）留待后续（#6b），故 #6 未完全毕业。
- **提示据实**：一键结果分「已在运行，已重新打开 / 已用新配置重启代理，Science 沿用不变 / 沿用原有对话 / 已启动」四态；浏览器打开失败改提示手动打开，不再谎报「已重新打开」。
- **停止 / 切换官方模式不再虚报成功**：定位不到停止脚本时如实报错（沙箱可能仍在跑）；切「官方 Claude」改为**先拆第三方链路成功、再落盘 official**，杜绝「磁盘=官方 / UI=第三方 / 进程已停」的状态分裂。
- **代理健壮性**：畸形请求体（顶层非对象 / `messages` 非数组）规范回 **400** 而非击穿线程；上游 **401/403/429 原样透传**（DeepSeek 原生透传与 Qwen 翻译**两条路径都修**），存 key 时能准确提示「key 无效」。

### 安全 / 健壮性（三轮外部复审加固）
- **OAuth 复用校验从严**：仅当 `account_uuid` 是合法 UUID、`provider=claude_ai`、`access_token` 非空、`token_expires_at` 未过期、读路径非符号链接时才判「可复用」；否则降级修复（安全方向）。`encryption.key` 的 `OAUTH_ENCRYPTION_KEY` 非法 base64 时自愈重造。
- **孤儿代理清理收紧**：`pkill` 匹配收紧到**本安装的绝对脚本路径 + 端口**（正则元字符转义），不误杀另一 checkout / 用户手启的同名代理。
- **沙箱身份确认**：一键启动后与状态灯改用 `claude-science status --data-dir`（按 data-dir 的强身份）确认，替代裸端口 `/health`，防端口被冒名服务占用时误判「已启动 / 绿灯」。

### 变更 Changed
- 全仓 Rust 统一格式（`cargo fmt`，`--check` 通过）。

## [0.1.5] — 2026-07-03

### 变更 Changed
- **主按钮及提示文案脱敏**：用户可见的「一键越过登录」全部改为中性的「一键开始」（`desktop/src/index.html` 主按钮 + `desktop/src/main.js` 各 `setMsg` 提示 + `desktop/src-tauri/src/lib.rs` 回传给用户的错误串 + `README.md` / `desktop/README.md` 里展示的按钮名）。README 开场白「绕过 Claude 登录」改为「无需 Claude 订阅也能用上它」。技术内部文档、历史记录与 DMCA 免责里的机制描述按边界保留。

## [0.1.4] — 2026-07-03

> 自 0.1.2 起合并数波修复发布：node-ectomy（拔掉 node 运行时依赖）、沙箱卡「Switching organization」实机修（CONNECT 403→401）、面板改正常窗口去托盘、第三方 / 官方模式一键切换，及两轮外部复审加固。

### 重大：开箱即用（拔掉 node 运行时依赖）
- **🎯 node-ectomy —— 虚拟 OAuth 伪造器移进 Rust 原生实现，app 彻底不再需要 node。** 那 220 行 `make-virtual-oauth.mjs`（HKDF-SHA256 + AES-256-GCM 的 v2 加密令牌）用 RustCrypto（`aes-gcm`/`hkdf`/`sha2`）1:1 重写为 `src/oauth_forge.rs`，编进二进制；一键流程在进程内伪造，启动脚本带 `--skip-oauth-forge` 跳过 node 步。**与 `.mjs` 的 v2 GCM 格式字节兼容**，由 node↔rust 双向解密对拍单测钉死（`oauth_forge::tests::crosscompat_*`）。这是 #2「缺 node」的**治本解**：不管用户装没装 node（含 B 类「根本没装」的研究者用户），app 都能用。`.mjs` 保留作 dev/独立脚本 + 对拍预言机。`doctor` 里 node 从「必需」降为「仅 dev」。

### 新增 Added
- **第三方 / 官方 模式一键切换**：面板顶部加分段切换。**第三方模型**（默认）= 原有一键越过登录 → 走 DeepSeek/Qwen；**官方 Claude** = 面板收起 provider/key/沙箱那套，主按钮改为「打开官方 Claude Science」，用 `open`（LaunchServices 正常启动，显式抹掉任何 `ANTHROPIC_*`）把用户交回自己真实的 Science 与订阅——**CSSwitch 不插手官方登录、不起代理/沙箱、不碰任何真实凭证**（铁律 1/2/3）。切到官方是**真正的切换**：后端先停掉第三方链路（沙箱 + 代理、清 secret），既不留后台空跑，也避免 macOS 单实例下 `open` 误聚焦还活着的沙箱实例。给「有 Claude 订阅、想正常走官方 OAuth」的用户用。模式存 `~/.csswitch/config.json` 的 `mode` 字段，下次启动记住。

### 修复 Fixed
- **#2 已装 node 仍报「缺少依赖 node」**（GUI 从访达启动只拿到最小 PATH，查不到装在 `/usr/local/bin`、Homebrew、nvm 等处的 node）。两层解决：**① 治本** = 上面的 node-ectomy（app 不再需要 node）；**② 止血兜底**（仍用于定位 python3）= `which()` PATH 未命中时扫常见安装目录 + 登录 shell `zsh -lic 'command -v'` 解析真实 PATH。*多位客户 + 开发者第二台机器复现确认。*
- **#3 沙箱卡在「Switching organization」**（启动时对 `claude.ai/api/oauth/profile` 的阻塞请求在到不了 claude.ai 的网络上挂住）。代理新增 `do_CONNECT`：对 `claude.ai / *.claude.com / *.anthropic.com` 的 CONNECT 立即 **401（未登录）** fast-fail，其余域名隧道透传；`launch-virtual-sandbox.sh` 注入 `https_proxy` 指向代理 + `no_proxy=127.0.0.1`（本地推理仍直连）。**实机整链验证并修正**：初版回 403（禁止）会让 operon 当组织问题反复重试、仍卡住；改回 **401** 才触发 operon `treating as logged-out` 秒过（operon 日志实测：403→卡、401→过，唯一变量就是状态码）。根因与实机证据见 [`findings/switching-organization-hang.md`](findings/switching-organization-hang.md)。
- **#1 面板无法鼠标拖动**：随 #4 消失（原生标题栏自带拖动）。

### 变更 Changed
- **#4 面板从「菜单栏 accessory」改为正常 Dock 窗口**：`decorations`（标题栏关闭/最小化/缩放三键）+ `visible`（启动即显示）+ `center`（居中）+ `resizable`（min 320×520）；**移除菜单栏托盘图标**（纯正常窗口）；从红叉关窗与「退出」按钮一致——停代理、清 secret、保留沙箱（避免留孤儿代理）。
- 依赖收敛：app 运行时依赖从 node + python3 减为 **仅 python3**（下一步同法移 axum 拔 python，见 [`docs/dependency-analysis.md`](docs/dependency-analysis.md)）。

### 安全 / 健壮性（外部复审修复）
- **[P1] 伪造器符号链接重定向**：去掉旧 `.sandbox/` 字符串启发式后，仅「==真实凭证目录」拦不住把 auth_dir 预置成指向其它目录的符号链接 → 恢复「沙箱根内」约束（`forge` 收 `sandbox_root=sandbox_home()`，解析后路径必须落其下），在写任何文件前拒绝逃逸。加回归测试 `forge_rejects_symlink_escaping_sandbox_root`。
- **[P2] 旧 `.enc` 删除失败被静默忽略**：原 `let _ = remove_file`，删不掉会残留多个 `.enc`（Science 预期恰好一个 → 显示启动成功却登录不上）→ 改为显式失败。
- **[P2] `http_proxy` 设了却不支持普通 HTTP 代理**：普通 HTTP 的 MCP/下载会撞代理拿 404 → 启动脚本改为**只设 `https_proxy`**（fast-fail 目标 `claude.ai/api/oauth/profile` 是 HTTPS），普通 HTTP 直连或走用户自己的代理。
- **[P3] 登录 shell 探测超时后泄漏子进程**：改 spawn + 轮询 + 超时 kill，病态 rc 卡死时终止 zsh。
- **[P2·评估后保留] CONNECT 不校验 path-secret**：代理只绑回环 + 隧道是裸 TCP 转发（不注入 key、不经推理端点），path-secret 守护的边界未被削弱，风险面小；已在代码注释记录该判断与「`Proxy-Authorization` 收紧」路径，待整链联调验证 operon 行为再定。

### 安全 / 健壮性（第二轮外部复审修复）
- **[P1] 模式切换只改配置、不切换进程** → `set_mode` 切到「官方」时**真正拆掉第三方链路**（停沙箱 Science + 杀代理 + 清 secret），做成名副其实的「切换」而非「选择模式」；也堵住 macOS 单实例下 `open` 误聚焦到还带着 `ANTHROPIC_*` 环境的沙箱实例。全程绝不碰真实 8765。
- **[P1] 伪造器信任根仍可被父级软链接带偏**：若 `~/.csswitch/sandbox` 被预置成指向真实 `~/.claude-science` 子树的符号链接，沙箱根自身会解析进真实树，令「沙箱根内」检查失效。**新增护栏 0（铁律最高优先，先于沙箱根检查）**：解析后的写入根绝不落在真实 Science 目录之内或本身，任何异常布局都绝不触碰真实目录。加回归测试 `forge_rejects_symlink_into_real_science_tree`（验真实目录零改动）。
- **[P2] 切换失败时 UI 谎报成功**：原「先翻 UI 再写配置、失败不回滚、错误提示又被后续普通提示覆盖」→ 改为**先落盘成功再翻 UI**，失败保持旧模式并如实报错、不被覆盖，切换期间禁用按钮，切成后刷新状态灯。
- **[P2] 关键 OAuth 实现未纳入 Git**：`oauth_forge.rs`（及对拍脚本 `test/decrypt-oauth.mjs`、CONNECT 测试 `test/test_proxy_connect.py`）此前是未跟踪文件，干净检出会因缺 `mod oauth_forge` 而构建失败 → 本次一并纳入版本控制。

## [0.1.2] — 2026-07-03

### 修复 Fixed
- 面板拖拽与端口相关修复。

### 新增 Added
- 存 API key 后用一条最小请求**真实验证一次 key**（200 可用 / 401·403 被拒）。
- 稳定复用的 path-secret（一次性鉴权令牌持久化，沙箱重连不失效）。
- 反馈 / 报 bug 基建（一键跳 GitHub issue 模板、打开日志目录自查）。

## [0.1.1] — 2026-07-03

### 新增 Added
- 一键越过登录面板（填 key → 一键起代理 + 沙箱 + 打开浏览器）。
- 检查更新（跳 GitHub Releases）。
- terracotta 暖橘品牌主题与图标集。

## [0.1.0] — 2026-07-03

### 新增 Added
- 首个公开版本。
- Tauri 菜单栏 app（进程管家）：管代理与沙箱两个子进程、读写 `~/.csswitch/config.json`、key 只注入环境变量、探活。
- provider 可切代理 `csswitch_proxy.py`：DeepSeek 原生 Anthropic 透传（默认）/ 通义千问 DashScope 翻译。
- 虚拟 OAuth 伪造器（本地假凭证越过 Science 的登录门票，零真实凭证）。
- 运维三件套 `doctor` / `verify-proxy` / `self-test` + 离线回归套件。
- 每日维护巡检（launchd 09:00/21:00，只读 + 规划，守铁律）。
