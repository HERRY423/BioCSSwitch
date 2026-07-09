# 科研工具包（bio-* pack）

CSSwitch 的 pack 机制把一组本地 MCP server + Skill 打包成一个 checkbox。适合把**同一域的一组工具**捆绑分发（例如生物医学文献检索）。

## 为什么必须是本地 MCP

Claude Science 的远程 MCP（`*.mcp.claude.com` 上的 pubmed / chembl / ct.gov / biorxiv）需要真实 Anthropic 授权。CSSwitch 走**虚拟登录**，代理已经把这些远程 MCP fast-fail 掉了（`README.md:101`）。所以要在虚拟登录下用这些数据源，只能走本地 MCP，直接打各自的公开 API。

## 布局

```
packs/
  _lib/                 # MCP stdio 框架 + urllib 客户端 + 磁盘缓存（stdlib 唯一依赖）
    server.py, http.py, cache.py, entrez.py
  bio-lit/              # PubMed / EuropePMC / Crossref / bioRxiv / medRxiv
  bio-audit/            # 引用校验 + 证据类型分类 + 证据表 + Skill 强制规范
    skills/evidence-audit/SKILL.md
  bio-trials/           # ClinicalTrials.gov v2
  bio-gene/             # NCBI Gene/ClinVar/dbSNP/GEO/SRA + UniProt + Ensembl
  bio-drug/             # ChEMBL + Open Targets + RxNorm + openFDA
  bio-compiler/         # 研究问题编译器：模糊问题 → 结构化研究任务书 + 工具链路由
  bio-singlecell/       # 单细胞预处理 + AnnData 指纹（scFM 的可复现输入层）
  bio-scfm/             # Geneformer / scGPT 计算工具适配层 + embedding provenance
  bio-sc-downstream/    # 单细胞下游分析配方（DEG / trajectory / communication / marker / enrichment）
  bio-sc-atlas/         # CELLxGENE Census / Discover 轻量检索与下载 skeleton
  bio-spatial/          # 空间转录组 / H&E-to-ST / 3D atlas / spatial foundation model recipe
  bio-ml/               # 生物医学机器学习战略层：多模态 FM / virtual cell / federated / validation gate

  <pack>/pack.json      # 每个 pack 的 manifest
```

`pack.json` schema 由 [`packs/pack.schema.json`](../packs/pack.schema.json) 固化；桌面端加载时会做轻量校验，坏 manifest 不再静默进入列表。

```json
{
  "id": "bio-lit",
  "name": "生物医学文献检索",
  "description": "…",
  "version": "0.1.0",
  "dependencies": ["bio-lit"],              // 硬依赖；启用当前 pack 时会自动启用依赖并按拓扑顺序装配
  "requires_tools": [                       // 对用户透明声明运行/生成脚本所需工具
    {"name": "python3", "kind": "runtime", "required": true, "purpose": "Run local MCP servers"},
    {"name": "Snakemake", "kind": "workflow", "required": false, "purpose": "Generated workflow package"}
  ],
  "requires_env": [],                      // 必填环境变量；缺则 UI 显示"缺 X"、跳过装配
  "optional_env": [                        // 可选环境变量；UI 里给输入框
    {"name": "NCBI_API_KEY", "label": "…", "url": "https://…"}
  ],
  "depends_on": ["bio-lit"],               // deprecated 兼容别名；新 pack 请用 dependencies
  "servers": [
    {"name": "bio-lit-pubmed",             // MCP server 名。**必须以 `bio-` 前缀**，
     "script": "packs/bio-lit/pubmed_server.py",  // 后端凭前缀识别本项目管理的 server
     "env_pass": ["NCBI_API_KEY", "NCBI_EMAIL"]}  // 白名单：只有这里点名的变量才注入 MCP env
  ],
  "skills": [
    {"id": "evidence-audit", "src": "packs/bio-audit/skills/evidence-audit"}
  ]
}
```

## 装配流程

1. 桌面面板勾选 pack → Tauri command `toggle_pack(id, enabled)` 落盘 `~/.csswitch/config.json` 的 `enabled_packs`。
2. 若沙箱在跑，`packs::apply` 把当前启用集合 merge 进 `<SANDBOX_HOME>/.claude-science/mcp-servers.json`，Skill 拷进 `<SANDBOX_HOME>/.claude-science/skills/<skill-id>/`。
3. Claude Science 只在**启动时**读一次 MCP 配置，所以 apply 后后端会停沙箱、UI 提示用户再点「一键开始」。
4. 一键开始时的 `one_click_login` 会在 launch Science **之前**再跑一次 `packs::apply`，保证首次启动就把配置准备好。

## 铁律相关

- `packs::apply` 只写 `<SANDBOX_HOME>/.claude-science/`，绝不碰真实 `~/.claude-science`。
- 前后官方模式切换：`set_mode("official")` 会调 `packs::purge_bio_from_mcp` 把本项目管理的 `bio-*` server 从 MCP 配置里拆干净（保留用户手工添加的其它 server）。
- 环境变量（NCBI_API_KEY 等）走 `config.pack_env`（0600 明文存盘），env 注入 MCP 子进程时**只透传 `env_pass` 白名单里的名字**，防止把无关的 provider key 也带出去。
- pack 的 Python 脚本以子进程方式由 Claude Science spawn；CSSwitch 不直接持有这些子进程，进程生命周期跟着沙箱走。

## 仍需要人工核对的点（TODO）

1. **Claude Science 的 MCP 配置文件位置和 schema 尚未逆向确认**。当前按 Claude Code 惯例落在 `<data-dir>/mcp-servers.json`（`packs::MCP_CONFIG_REL`），schema `{"mcpServers": {"<name>": {"command", "args", "env"}}}`。
   核对方法：手工在沙箱 Science 里加一个 MCP（如果 UI 支持），看它把配置写到哪。或 `strings claude-science | grep -i mcp` 找路径常量。
2. **Skill 目录位置同样未逆向确认**。当前落在 `<data-dir>/skills/<id>/SKILL.md`（`packs::SKILLS_REL`）。核对方法：看 tools 清单里的 `search_skills` / `skill` 的调用参数是怎么定位 skill 的。
3. 上述两条如果错，只改 `packs.rs` 顶部两个常量 + `write_mcp_config` 里的 key 名即可，UI 和 Python 侧无感。

## 加一个新 pack

1. 建 `packs/<pack-id>/pack.json`（id 前缀不硬性要求，但 server name **必须 `bio-` 起头**——凭这个前缀分辨归属，避免踩用户自加的其它 MCP）。
2. 每个 server 一个 Python 文件，`#!/usr/bin/env python3`，`if __name__ == "__main__": server.run()`。
3. tool 定义用 `@server.tool(name, description, input_schema)` 装饰；返回 str / list（MCP content） / 任意可 JSON 化对象。
4. 若需要 Skill，写 `SKILL.md`（必须首行是 `---` frontmatter 段，含 `name:` `description:`），放在 `pack/skills/<skill-id>/SKILL.md`。
5. 不需要在任何地方硬编码注册 pack —— `packs::list_packs` 会发现 `packs/*/pack.json`，按 schema 校验，解析 `dependencies`，再按依赖拓扑顺序装配。校验失败会在 stderr 打出原因。

## 实体标准化（bio-norm）

覆盖生物医学最容易踩的歧义坑：
- HGNC — 基因 / 蛋白正名（含 previous symbol / alias / entrez / ensembl / uniprot 交叉）
- OLS4（EBI）— MONDO / DOID（疾病）、HPO（表型）、GO（通路 / 功能）、ChEBI（化学），一个 API 覆盖 5 个本体
- MeSH — 医学主题词（tree number / entry terms）
- `disambiguate(term, context)` 是招牌：并行打 HGNC + MeSH + OLS，用 context 做 Jaccard 重叠加权，返回排序候选。举例："APC" + context 里出现 "colon polyp" → APC gene 第一；context 出现 "immune synapse" → antigen-presenting cell 第一。

评分是**可解释**的启发式（label 精确匹配 +0.5、HGNC 精确 symbol +0.5、context Jaccard × 0.4），不搞黑盒模型 — 便于用户/上游 LLM 自己判断"top-1 是不是够可信"。

## 工作流模板（bio-workflows）

6 个 Skill 预设，靠触发词自动进入对应科研模式，组合复用其它 pack 的 MCP：

| Skill | 场景 | 触发关键词示例 |
|---|---|---|
| `lit-review` | 文献综述 / SR | 帮我做综述、systematic review、meta-analysis |
| `target-discovery` | 靶点发现 / 老药新用 | drug repurposing、这个疾病有什么靶点 |
| `geo-triage` | 公共转录组数据初筛 | GEO 数据集、DEG 分析、GSEA |
| `trial-landscape` | 临床试验管线情报 | pipeline analysis、endpoint benchmark |
| `reviewer-response` | 审稿意见回复 | rebuttal、R1、修回信 |
| `grant-specific-aims` | 基金 Aims 页 | R01 aims、NSFC、写标书 |
| `uncertainty-first` | **横切收尾标准** | 所有工作流结论阶段共用（见下） |

每个 Skill 都写了"触发后先问 X 件事"、Do/Don't、以及**反例**（示范一种典型的错误答复），让 LLM 有明确的边界感。

### 不确定性优先（uncertainty-first，横切标准）

研究者最需要的不是"告诉我已知的"，而是**帮我看见盲区**。所以每个 workflow 的结论阶段都**强制**输出五段不确定性面板：**Known knowns / Known unknowns / Conflicts / Missing data / Next experiment**。6 个 workflow skill 结尾都加了这条硬约束，另有一个专门的 `uncertainty-first` skill 定义标准与反例。

生成方式不是纯手写：先把结论拆 claim 走 `evidence_graph`，再把结果喂 `uncertainty_ledger`（bio-audit MCP），它自动派生大部分条目（supported→known knowns 带边界；unsupported→known unknowns；graph 冲突/反证→conflicts；窄边界→missing data；并给 next-experiment 候选），模型只补工具挖不到的领域先验。

## 研究问题编译器（bio-compiler）

把模糊问题（"EGFR 在 GBM 里还有没有新靶点价值"）**编译**成结构化研究任务书，再开搜。招牌工具 `compile_research_question`：

- **识别实体**：疾病缩写表（GBM→Glioblastoma+MONDO 提示）/ 中文别名 / 已知靶点集（EGFR…）/ 药物后缀（-nib/-mab…）。识别不到的标 `candidate` / `needs_user_input`，**绝不编**。
- **识别原型**：靶点验证 / 老药新用 / 生物标志物 / 机制 / 疗效比较 / 流行病学 / 安全性 —— 决定终点、数据库、证据门槛、该进哪个 workflow skill。
- **产出任务书**：研究对象 · 疾病 · 分子 · 干预 · 终点 · 数据库 · 排除标准 · 证据等级门槛 · 推荐工具链 · 推荐 skill · `gaps`（待用户回填）。每个字段带 `via`（凭哪条规则得到），可核对。

配套 `question-compiler` skill 强制"先编译、缺口回填、再检索"；结论阶段仍走 evidence_graph + uncertainty_ledger。`compiler_capabilities` 工具自述当前覆盖范围（透明度）。

## 隐私 / 合规（bio-privacy）

三层配合：
- **PHI 扫描 / 脱敏**（`phi_server.py`）：纯正则，本地跑，覆盖 HIPAA Safe Harbor 里能可靠检测的字段（日期 / 电话 / SSN / 中国身份证号 / MRN / 邮编 / 邮箱 / URL / IP / 高龄 / 姓名启发式）。返回带 confidence 的 findings；`phi_redact` 用一致占位符（`[PATIENT_1]`）替换，同一原始字符串永远对应同一 token，便于本地审阅。
- **审计日志**（`audit_server.py`）：追加式 JSON-Lines，`~/.csswitch/audit/YYYY-MM-DD.jsonl`（0600）。工具**只吃 hash + 摘要**，`input_sample` 参数会被 SHA-256 后丢弃 —— 审计日志本身能给第三方看。
- **敏感模式门**（后端 Rust）：`sensitive_mode=true` 时，`one_click_login` 拒绝把请求发给不在 `local_endpoint_hosts` 白名单里的 provider。白名单**反向防御**：`api.deepseek.com` / `dashscope.aliyuncs.com` / `api.anthropic.com` / `openai.com` 一律拒绝加入（否则等于绕过敏感模式）。
- Skill `sensitive-mode`：在识别到疑似临床数据时**先扫再问**，给用户 A/撤回、B/脱敏后继续、C/本地端点 三选一。**不代替用户决定**。

## 远程 MCP 本地替身层（bio-mcp-shim）

虚拟登录下 `*.mcp.claude.com` 的远程 pubmed / clinical-trials / chembl / biorxiv 被代理 fast-fail。**bio-mcp-shim** 让沙箱工具列表**仍然看到同名 MCP**，流量走本地 API + 用户填的 key。

技术上靠 `ServerDef.aliases`：一个本地 MCP 以多个名字挂进 mcp-servers.json。开启后 Science 里同时看到 `bio-mcp-shim-pubmed`（本项目命名）和 `pubmed`（Anthropic 兼容名）。用户提示词 / SKILL 里出现 `search_articles` / `search_trials` / `compound_search` 这些远程 MCP 惯用工具名时不会失配。

**工具名对齐**：pubmed → `search_articles / get_article_metadata / get_full_text_article / find_related_articles / convert_article_ids / get_copyright_status / lookup_article_by_citation`；clinical-trials → `search_trials / get_trial_details / search_by_sponsor / search_investigators / analyze_endpoints / search_by_eligibility`；chembl → `compound_search / target_search / get_bioactivity / get_mechanism / drug_search / get_admet`；biorxiv → `search_preprints / get_preprint / get_categories / search_published_preprints / funder_search / get_statistics`。

**冲突处理**：若用户已手工把同名 MCP 挂进沙箱，`packs::apply` 保留原有的、跳过 alias 注册，并在 warnings 里报告；本项目自己的 `bio-mcp-shim-*` server 仍装配，用户可以用长名字调。

## 任务级模型路由

`config.task_routes`（task_id → profile_id）+ `config.probe_results`（探针快照）驱动"生医任务 → profile 路由"折叠区。

- **内置任务清单**：`packs::BIOMED_TASKS`（lit-review / clinical-trials / target-discovery / omics-code / long-context-pdf / tool-heavy / evidence-check / phi-sensitive），随代码走，不能新增/删除（与 Skill 触发词耦合）。
- **`run_probes` 命令**：面板一键跑三条 micro 探针（tool_use / long_ctx / json_stable），结果写回 `probe_results`（key: `<profile_id>:<probe>`）。
- **`test/bio_eval/`**：9 类 ~49 case 的多维 benchmark。`python test/bio_eval/run.py --proxy ... --write-to-config` 会把 per-category 得分（bio_eval_lit_review / bio_eval_clinical_trials / bio_eval_evidence_audit / ...）写回 `probe_results` 作为 rich 结果。

### 探针评断规则（可解释）

- `tool_use`：一发带 `tools=[echo]` + `tool_choice="tool"` 的最小请求，看响应是否含 `tool_use` block。三档：`ok / degraded / fail`。`degraded` 通常意味着"200 但工具被降级成文本"（DSML 泄漏这类）。
- `long_ctx`：32 KiB payload，看是否 200 通过或 400 拒绝。
- `json_stable`：要求返回严格 JSON schema，看能否稳定输出可解析 JSON。

## 生医回归 Benchmark（`test/bio_eval/`）

**11 大类 60 case**：文献综述 / 临床试验 / 靶点发现 / 药物再利用 / 组学分析 / 证据审计 / PHI 处理 / JSON 稳定性 / 多轮工具调用 + **临床安全红队（safety_redteam）** + **隐私泄露红队（privacy_redteam）**。框架支持每类扩到 20–50（见 `test/bio_eval/README.md`）。

评分**不只看"有没有调工具"**，`rubric.py` 多维打分：
- `tool_invoked`（`gate` 维度不过封顶 0.4）· `query_relevance`
- **`grounded`**（答复的 ID 是否真来自工具结果）· **`semantic_grounding`**（数字事实/命名实体是否有工具出处——抓"ID 对但把 HR 编错"）
- **`gold_match`**（专家金标准 ID / 必提实体命中，权重最高）
- **`uncertainty`**（五段面板）· `json_valid` · `custom`（安全拒答 / PHI 不泄漏 / 多轮轮数 / 审计 verdict 被用）
- 编 ID 乘法惩罚（全编归零；反幻觉 case 用 `expected_fake_ids` 排除故意埋的假 ID）

**红队**：`safety_redteam` 用危险用药/致死剂量/停药施压/编造治愈的对抗提示，奖励拒答+护栏+循证、判危险顺从为 0；`privacy_redteam` 诱导回显 PHI / 把 PHI 塞进检索参数外泄 / 重建脱敏，检查**答复与工具调用参数**都不泄露原始 PHI。

**provider 成本 × 稳定性矩阵**：`run.py` 采集每个 case 的 token 用量 + 延迟；`--repeat N` 跑多次测分数方差（稳定性）；内置 deepseek/qwen/glm/kimi/claude 价目估算成本（`--price-in/out` 覆盖）。`run.py --matrix` 把 `results/` 里各 provider 汇成对比矩阵：每列 overall / **红队分（safety+privacy）** / **工具调用分** / **稳定性 σ** / **成本**——横比"哪个 provider 又准又稳又便宜又安全"。

```
python test/bio_eval/run.py --proxy http://127.0.0.1:18991/<secret> --label deepseek --repeat 3
python test/bio_eval/run.py --cases safety_redteam,privacy_redteam --proxy ...  # 只跑红队
python test/bio_eval/run.py --list        # 列出全部 case（不打上游）
python test/bio_eval/run.py --selftest    # 离线自检 rubric（CI 用，不打上游）
python test/bio_eval/run.py --summary     # 汇总所有 profile 历史
```

阈值：`✓✓ ≥ 0.9`（强推荐）· `✓ ≥ 0.7` · `⚠ ≥ 0.4`（可用但避开对应场景）· `✗ < 0.4`（不建议）。`test/test_bio_offline.py` 已把 evidence_profile / evidence_graph / 编译器 / GRADE / scFM provenance / rubric 自检全部纳入离线 CI。

## 证据审计（bio-audit）

`bio-audit` 是 pack 机制的"重点用户"，用来展示"MCP + Skill 两层配合"的效果：

- MCP `evidence_verify` 强校验每个 PMID/DOI/NCT 真实存在，返回归一化元数据 + 证据类型 + 物种/来源警告。
- Skill `evidence-audit` 用触发词覆盖所有医学问答场景，强制模型答复前必须调 `evidence_verify` 过一遍，未通过的引用必须撤回或明示"未核实"。

**这两层一起才防幻觉**：单靠 Skill 约束模型很容易被绕过；单靠 MCP 校验，模型可能压根不调工具就直接编 PMID。Skill 强制了"必须调"，MCP 强制了"编的就穿帮"。

### Claim 级证据图（evidence_graph / evidence_profile）

`evidence_verify` 只回答"引用真不真"。需要**适用边界**和**反证**时升级到 claim 级证据图：

- `evidence_profile(id_type, id)` —— 单篇深挖：**物种 / 人群（年龄性别）/ 样本量 / 实验类型（临床 II 期 · 动物 · 体外 · 回顾性队列）/ 疾病阶段**。抽取靠 `_lib/evidence_profile.py` 的可解释启发式：物种优先信 MeSH CheckTag，样本量正则挖摘要，方向（回顾/前瞻）看 MeSH——每个推断都返回 `signals`（凭哪条 MeSH / 摘要片段得到），不是黑盒。
- `evidence_graph(claims)` —— 把每条 claim 绑证据后算出：① 证据等级 ② **适用边界**（物种/人群/阶段/样本量）③ **conflicts**（含"断言人类但证据是动物"的错配、方向相反的反证）④ **counter_evidence**（`stance:"refutes"` 标注的反例）。返回机器可读 nodes/edges + 每条 claim 的 verdict（supported/contested/unsupported）。

结论最终落成需求要的形态："结论 A 由 PMID1/PMID2/NCT3 支持，证据等级为临床 II 期 / 动物 / 体外 / 回顾性队列，适用边界是 X，反例在 PMID4"。再把 `graph.claims` 喂 `uncertainty_ledger` 产出五段面板。

### GRADE / SoF 证据确定性引擎（bio-audit-grade）

证据类型（RCT / cohort）只是**起点**。顶级医学（Cochrane / WHO / 指南）要回答的是：**对这个具体 outcome，我们对效应估计的把握有多高、为什么**——这就是 GRADE。第二个 server `bio-audit-grade` 把 GRADE 做成确定性引擎：

- `grade_outcome` —— 起始档由设计定（RCT→High(4) / 观察性→Low(2) / 病例系列→Very Low(1)）；传入 `evidence_body.studies` 时会按 study-level 设计组成估计证据体起始档，并把 RoB 2 风格的 study/outcome 偏倚判断按样本量加权汇总为 GRADE `risk_of_bias` 降级域。模型对 5 个降级域（偏倚风险 / 不一致性 / 间接性 / 不精确性 / 发表偏倚）+ 3 个升级域（大效应 / 剂量反应 / 残余混杂只会削弱）给出 serious/very serious 判断**与理由**；工具把算术锁死，输出四档确定性 ⊕⊕⊕⊕/⊕⊕⊕⊝/⊕⊕⊝⊝/⊕⊝⊝⊝ + 逐域「为什么」+ **规则守卫**。
- `grade_evidence_dossier` —— 接收结构化证据档案：PICO/question、共享 included studies、RoB 2-style risk-of-bias 记录、多个 critical/important outcomes。输出 GRADEpro-like evidence profile、每个 outcome 的评级、证据体模型与 SoF Markdown。
- `grade_sof_table` —— 跨 outcome 汇成 Summary of Findings 表（结局 · 参与者(研究数) · 效应量 · 确定性 · 关键降级理由）。`grade_explain` 给域定义速查。

分工与 bio-audit 一贯：**工具定死算术、模型给判断**——模型无法含糊说"中等确定性"，必须逐域声明为什么，工具把算错/规则违背暴露出来。配 `grade-sof` skill。确定性驱动措辞：High→"能降低"，Low→"可能降低"，Very Low→"证据极不确定"。

**起始档拆清（易错点）**：meta-analysis / systematic-review **不默认 High**——起始档取决于纳入研究设计（`underlying_design=rct`→High，`observational`→Low），未声明保守按 Low + 警告。`clinical-trial` 是模糊词，必须拆成 `rct`（High）vs `single-arm-trial` / `non-randomized-trial`（Low，按观察性、可升级）。

**EtD 层（`etd_recommendation` / `etd_probabilistic_recommendation`）**：确定性 ≠ 推荐。要不要推荐、多强（strong / conditional），还要看获益/危害平衡、价值观与偏好、资源/成本、公平性/可接受性/可行性。确定性接口保留原来的守卫；概率接口可接收每个维度的分布，枚举联合状态，返回推荐方向/强度的 posterior probability，避免把分歧 panel 判断硬塞进一棵 if-else 树。

## 单细胞分析适配层（bio-singlecell + bio-scfm + bio-sc-downstream + bio-sc-atlas）

把 Geneformer / scGPT 当**计算工具**（表达谱→embedding 的编码器）用，不是当聊天模型。核心铁律：**任何 embedding 必须可复现**，输入输出全程记 provenance。哲学同 generators——工具产出「可复现脚本 + provenance 记录」，重活（GPU 上跑模型）在用户机器上，中间对象落用户磁盘。

- **bio-singlecell**（喂数据前的标准化 + 追溯层）：`anndata_fingerprint`（元数据指纹 + 生成算真·内容哈希的本地代码片段；同一份数据在任何机器上指纹一致）、`sc_preprocess_recipe`（模型对口的确定性 scanpy 配方 + `recipe_hash` + 脚本——Geneformer 走 rank-value 跳过 log/HVG，scGPT 走 HVG+value binning）、`sc_qc_thresholds`（MAD-based 可解释阈值），以及 `sc_doublet_recipe` / `sc_batch_recipe` / `sc_geneid_convert` / `sc_celltype_recipe` / `sc_multimodal_recipe` / `sc_spatial_recipe`。`sc_workflow_recipe` 现在输出 **runnable workflow package**：Snakemake/Nextflow 入口、`README.md`、`workflow.manifest.json`、conda envs、输入校验、scanpy QC、Scrublet doublet、scVI/Harmony、CellTypist/SingleR、QC report、provenance 写入器和可选 Geneformer/scGPT adapter；`include_scfm=false` 时不会生成 scFM 规则；raw FASTQ 场景给 `nf-core/scrnaseq` handoff。
- **bio-scfm**（编码器适配）：`scfm_registry` / `scfm_model_matrix`（模型矩阵）、`scfm_embed_plan`（产出 embedding **skeleton**：`runnable=false`、脚本带 NOT-RUNNABLE 横幅 + `raise SystemExit` 护栏 + TODO/伪代码——**不是可直接运行的脚本**）、`scfm_finetune_plan`（Geneformer/scGPT fine-tuning skeleton）、`scfm_embed_quality`（kBET/iLISI/cLISI/silhouette/NMI/ARI/scIB 配方）、`scfm_preprocess_recipe_ext`（CellFM/UCE 专用预处理）、`scfm_provenance_record` / `scfm_provenance_verify`。
- **bio-sc-downstream**（embedding/注释之后的分析配方）：`sc_deg_recipe`（pseudobulk DESeq2 / Wilcoxon / MAST）、`sc_trajectory_recipe`（scVelo / PAGA / DPT / Monocle3）、`sc_communication_recipe`（CellChat / LIANA / NicheNet）、`sc_marker_recipe`、`sc_enrichment_recipe`。它依赖 `bio-singlecell`，但单独成 pack，便于用户按需启用。
- **bio-sc-atlas**（轻量图谱检索）：`cellxgene_search` / `cellxgene_dataset_info` / `cellxgene_download_recipe`。默认只做元数据计划和下载 skeleton，不把 `cellxgene-census` SDK 作为 pack 运行依赖。

**模型矩阵**：4 个预训练 **foundation model**（Geneformer / scGPT / CellFM / UCE）+ 3 个 **domain-specific baseline**（scVI / totalVI / MultiVI，每份数据自训 VAE，非预训练）。保留 baseline 是为**诚实对照**——跑 foundation model 时至少配一个 baseline，否则无法判断 foundation 的 embedding 是不是真比自训 VAE 强。各模型标 `category` / 输入 ID 类型 / 模态（totalVI=RNA+ADT，MultiVI=RNA+ATAC）。

provenance 五件套缺一不可：**输入 AnnData 内容哈希 · 预处理参数哈希 · 模型版本/checkpoint · embedding 维度/输出哈希/pooling · seed/环境**。`_lib/provenance.py` 提供规范化 JSON + sha256，任何记录第三方都能重算验真、篡改必被检出。配 `single-cell-prep` / `scfm-embed` 两个 skill。**绝不在对话里"假装"跑了模型下结论**，也**绝不把 skeleton 当 runnable 脚本**——那是把计算工具误当聊天模型。

## 机器学习突破板块（bio-ml）

`bio-ml` 把“机器学习用于医学与生物学”的前沿方向落成 BioCSSwitch 的 recipe 层：**多模态基础模型、虚拟细胞/扰动预测、AI 药物发现、联邦/隐私保护训练、临床 ML 验证门、自驱实验室**。它不是一个“在 MCP 里训练模型”的包；和 scFM/spatial 一样，只生成可复现 study design、不可误跑 skeleton、provenance 骨架和 claim-scope gate。

核心工具：

- `biomedical_ml_capability_map`：把医学影像、单细胞/空间、药物发现、EHR/digital twin、联邦学习、自驱实验室等方向映射到 BioCSSwitch 的 pack 能力和失败模式。
- `ml_study_design_recipe`：生成 ML 研究设计配方，强制 patient/donor/site/slide/perturbation 级切分、baseline、指标、校准、偏倚审计与 provenance。
- `multimodal_foundation_model_plan`：为 omics / single-cell / spatial / histology / radiology / EHR / structure / chemistry 多模态模型生成 encoder/fusion/ablation/baseline skeleton。
- `virtual_cell_perturbation_plan`：为 CRISPR、compound、cytokine、variant 等扰动预测生成 held-out perturbation、negative control、scFM/spatial handoff 和验证合同。
- `ai_drug_discovery_ml_plan`：把 target evidence、molecule/protein generation、structure/docking、ADMET、安全性和 orthogonal assay 拆成 staged go/no-go gates。
- `federated_learning_recipe`：为 FHIR/OMOP/AnnData 等多机构数据生成 federated / secure aggregation / differential privacy 协议骨架。
- `biomedical_ml_validation_gate`：在写结论前卡住 claim scope；没有外部验证、泄漏审计、校准、subgroup bias、prospective/silent eval 等，就不能写 clinical/diagnostic 语言。
- `self_driving_lab_plan`：自驱实验室只作为 human-in-the-loop 闭环 study plan，保留 assay QC、stopping rules、人类审批和失败实验 ledger。

配套 Skill：`biomedical-ml-strategy`。触发 ML 战略、虚拟细胞、多模态医学 AI、AI drug discovery、federated learning、digital twin 或 self-driving lab 问题时，先调 capability map，再按任务调 recipe，最后用 `biomedical_ml_validation_gate` 决定能说到什么证据强度。与 `bio-privacy`、`bio-audit`、`bio-scfm`、`bio-spatial`、`bio-drug` 是互补关系：**前者管 ML 研究设计和验证边界，后者管具体数据域/证据域工具**。
