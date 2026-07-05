---
name: grade-sof
description: 用于给一个 outcome 的证据确定性做 GRADE 评级、产出 Summary of Findings (SoF) 表。触发：证据确定性、certainty of evidence、GRADE、证据质量分级、这个结论有多可靠、SoF 表、summary of findings、要不要下推荐、指南证据分级、quality of evidence、能不能信这个结论、证据强度、evidence certainty、把证据评个级。也用于"这个疗法的证据到底有多强"、"帮我给这几个结局评 GRADE"、"这条推荐的证据基础可靠吗"。禁用于：单纯核对某个 PMID 真伪（那用 evidence-audit / evidence_verify）。
---

# GRADE / SoF 证据确定性（grade-sof）

顶级医学不满足于"有文献支持"——它问：**对这个具体结局（outcome），我们对效应估计的把握有多高？为什么？** GRADE 把这个问题标准化成四档确定性（High / Moderate / Low / Very Low），并要求逐条说明升/降级理由。这个 skill 让你按 GRADE 走，不许含糊。

## 铁律

1. **评的是 outcome，不是文献**。一篇 RCT 对"死亡率"可能是 High、对"生活质量"可能是 Low（后者盲法难、测量间接）。**每个关键结局单独评**。
2. **每次升/降级都要写「为什么」**。判"不一致性 serious"就要指出 I² 多高 / 哪几项方向相反。`grade_outcome` 会对没给理由的降级发 warning——别无视。
3. **确定性 ≠ 效应大小 ≠ 统计显著**。effect 很大但只有一项小样本观察性研究，确定性仍可能是 Low。别把"结果好看"当成"证据强"。
4. **RCT 不能升级**。升级只用于无严重局限的观察性证据（大效应 / 剂量反应 / 残余混杂只会削弱）。工具会拦截对 RCT 的误升级。
5. **确定性驱动措辞**。High→"能降低"；Moderate→"很可能降低"；Low→"可能降低"；Very Low→"证据极不确定，效应方向都可能改变"。别在 Low 证据上用确定语气。

## 工作流

**Step 1：先审计证据**（若还没做）。用 `evidence-audit` / `evidence_graph` 把每条结论的引用核实、拿到证据类型（RCT / observational / …）与物种/人群/样本量。GRADE 的起始档就来自这里。

**Step 2：列出关键结局**。别把所有东西混成一句"有效"。典型：主要疗效结局、关键安全结局、生活质量各评一个。

**Step 3：对每个结局调 `grade_outcome`**。给全：
- `design`（rct / cohort / meta-analysis + underlying_design …）、`n_studies` / `n_participants`
- 5 个降级域 `domains`：risk_of_bias / inconsistency / indirectness / imprecision / publication_bias，每个 `{rating: not_serious|serious|very_serious, reason}`
- 观察性证据可给 `upgrades`（大效应 / 剂量反应 / 残余混杂）
- `effect`（RR/HR/OR/MD + CI）供 SoF 用

拿不准某个域的定义就先调 `grade_explain`。

**Step 4：读结果 + 处理 warnings**。`grade_outcome` 返回确定性符号（⊕⊕⊕⊝）+ 逐域 `why` + `warnings`。warnings 里说"样本 <300 应考虑 imprecision"这类要回去复核。

**Step 5：调 `grade_sof_table`** 把所有结局的评级汇总成 SoF 表，贴进答复。

**Step 6：若要给推荐，调 `etd_recommendation`（EtD 层）**。**确定性 ≠ 推荐**——`certainty` 只回答"证据多确定"，要不要推荐、推荐得多强（strong / conditional）还得看获益/危害平衡、价值观与偏好、资源/成本、公平性/可接受性/可行性。工具按 GRADE EtD 规则把这些判断映射成推荐方向（for/against）+ 强度，并守卫"低确定性上的强推荐"（属 GRADE 不一致推荐，需符合 5 类特殊情形或降为 conditional）。措辞遵循 GRADE 惯例：strong→"we recommend"，conditional→"we suggest"。

**Step 7：把确定性/推荐强度写进结论措辞**，并接 `uncertainty_ledger` 出五段面板——低确定性的结局往往正是 Known unknowns / Next experiment 的来源。

## 起始档的坑（务必拆清）

- **不要把 meta-analysis / systematic-review 当成自动 High**。它的起始档取决于**纳入研究设计**：meta of RCTs → High，meta of observational → Low。调 `grade_outcome` 时用 `underlying_design=rct|observational` 声明；不声明工具会保守按 Low 并警告。
- **"clinical-trial" 是模糊词**。单臂 II 期、非随机对照都叫 "clinical trial"，但它们的起始档是 Low（按观察性），只有**随机对照**才是 High。把 `design` 写成 `rct` / `single-arm-trial` / `non-randomized-trial` 等明确类型，别写 `clinical-trial`。

## 一个正例

问题："SGLT2 抑制剂能降低心衰住院吗？证据有多强？"

- 主要结局「心衰住院」：多项大型 RCT，一致、精确、直接 → **High ⊕⊕⊕⊕**，措辞"能降低"。
- 结局「全因死亡」：CI 跨越无效线（imprecision serious）→ 从 High 降到 **Moderate ⊕⊕⊕⊝**，措辞"很可能降低，但对死亡的效应把握略低"。
- 给出 SoF 表，两行两个确定性，各写清降级理由。

## 一个反例（不要这样）

> SGLT2 抑制剂证据充分、质量高，建议使用。

问题：没分结局、没给确定性档次、没说凭什么"质量高"、把不同结局的证据强度混成一句。GRADE 的价值就是拆开评 + 说清为什么。

## 边界

- GRADE 的域判断（inconsistency 严不严重）需要**读研究**，工具替你算术、不替你读文献。判断错了，评级就错——所以 reason 要能被第三方复核。
- 本引擎不做 meta-analysis 的定量合并（不算 I² / 合并 RR）；这些数字应来自已发表的系统综述或你自己的定量分析，作为输入喂进来。
- 从确定性到「推荐强弱」（strong / conditional）是 GRADE 的下一步（EtD 框架），本 skill 只到确定性为止；推荐还要权衡获益/危害/价值观/成本。
