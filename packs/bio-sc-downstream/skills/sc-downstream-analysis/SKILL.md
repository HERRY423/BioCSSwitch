---
name: sc-downstream-analysis
description: 用于单细胞 embedding/聚类/注释之后的下游分析配方：scRNA-seq DEG、pseudobulk、trajectory、RNA velocity、pseudotime、PAGA、DPT、Monocle3、cell-cell communication、CellChat、LIANA、NicheNet、marker gene、single-cell enrichment。禁用于：QC、doublet、预处理、batch 整合、基因 ID 转换、细胞注释前置步骤（先用 single-cell-prep）。
---

# 单细胞下游分析（sc-downstream-analysis）

本 skill 接手 **embedding / clustering / cell type annotation 之后** 的分析分支。它不代跑分析，只生成可复现脚本、参数、provenance 骨架，并提醒用户哪些输入是必需的。

## 触发后先确认

1. 分析目标：DEG、marker、trajectory/RNA velocity、cell-cell communication、enrichment 中哪一种。
2. 输入是否就绪：是否有 `anndata_fingerprint`、预处理 `recipe_hash`、batch 处理说明、cell type / cluster key。
3. 数据层是否足够：DEG 是否有 biological replicate 和 raw counts；scVelo 是否有 spliced/unspliced；通信分析是否有可靠 cell type annotation。
4. 结果是否跨 sample/donor 复现；cluster 是否仅由单一样本、QC 或 doublet 驱动。

## 工具路由

- DEG / pseudobulk / Wilcoxon / MAST → `sc_deg_recipe`
- RNA velocity / PAGA / DPT / Monocle3 → `sc_trajectory_recipe`
- CellChat / LIANA / NicheNet → `sc_communication_recipe`
- marker 可视化与发现 → `sc_marker_recipe`
- per-cluster enrichment / gene set scoring / decoupler → `sc_enrichment_recipe`

## 方法边界

- pseudobulk DESeq2 是 condition-level DEG 的默认严肃路线，但需要 biological replicates；按 sample×cell type 聚合，配对/批次应进入设计式。
- Wilcoxon rank genes 是 cluster marker / 探索性排序，不等于有重复样本的 condition DEG。
- 细胞比例差异也要用 sample-level 组成模型；不把每个细胞当独立观测做 t-test。
- 轨迹和 velocity 是模型化推断，必须有明确 root / direction 的生物学依据。
- 细胞通信是配体-受体数据库推断，不等于实验证明；空间邻接、蛋白或扰动验证才提升机制可信度。
- 每个主结论至少报告一个关键敏感性分析，例如不整合基线、替代注释、替代轨迹或去除单个供体。

## 与其他 skill 的边界

- 前置 QC、doublet、batch、gene ID、cell type annotation：用 `single-cell-prep`。
- scFM embedding、fine-tuning、embedding quality：用 `scfm-embed`。
- bulk GEO DEG/GSEA：用 `geo-triage`，不要把 bulk 和 sc pseudobulk 混在一起。

## 收尾

结论必须走不确定性五段面板：Known knowns / Known unknowns / Conflicts / Missing data / Next experiment。缺少用户实际运行结果时，只能评价“配方是否合理”，不能编造 DEG、marker、通路或通信结果。
