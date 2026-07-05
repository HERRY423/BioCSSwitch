---
name: single-cell-prep
description: 用于单细胞 RNA-seq 数据的标准化预处理、QC 与内容指纹，为下游（聚类 / scFM embedding）准备可复现输入。触发：单细胞预处理、scRNA-seq QC、scanpy 预处理、AnnData、h5ad、质控、过滤低质量细胞、normalize、HVG、highly variable genes、MAD 过滤、单细胞质量控制、preprocess single cell、AnnData 指纹、数据可复现、给单细胞数据算哈希。禁用于：bulk RNA-seq（那用 omics / DESeq2 流程），或已预处理好只差建模的场景（直接进 scfm-embed）。
---

# 单细胞预处理与指纹（single-cell-prep）

单细胞分析最常被忽视、又最影响可复现的一步：**预处理参数没记、输入没指纹**。换个 `n_top_genes`、少记一步 normalize，下游聚类和 embedding 全变，却无从追溯。这个 skill 让预处理**确定性 + 可追溯**。

## 铁律

1. **参数即数据**。每一步预处理（filter / normalize / log1p / HVG / binning）的参数都要落进 `recipe_hash`，不能"随手调"。`sc_preprocess_recipe` 产出的哈希要一路带到下游 provenance。
2. **输入要指纹**。跑任何建模前，先 `anndata_fingerprint` + 本地算真·内容哈希。同一份数据在任何机器上指纹一致，才能判"我们跑的是同一份输入"。
3. **QC 阈值可解释**。用 MAD-based（median ± n×MAD），返回"剔除哪些、凭什么"，别用不透明的固定阈值一刀切。
4. **不代跑**。产出脚本，用户在自己机器上跑，中间对象（preprocessed.h5ad）落用户磁盘、可复现。

## 工作流

**Step 1：了解数据**。问清或读出：物种、assay（10x / Smart-seq）、基因 ID 类型（Ensembl / symbol）、大致细胞数、是否已有 raw。

**Step 2：指纹**。`anndata_fingerprint(descriptor=...)` 拿元数据指纹 + 真·内容哈希 snippet（交用户跑）。

**Step 3：QC 阈值**。`sc_qc_thresholds(stats=...)` 用每指标的 median/MAD 给出剔除界，解释给用户看。

**Step 4：预处理配方**。`sc_preprocess_recipe(target_model=...)`——若下游要喂 Geneformer/scGPT，传对应 model 拿模型对口的配方（Geneformer 不做 log/HVG；scGPT 要 HVG + binning）。拿到 `recipe_hash` + 脚本。

**Step 5：交接**。把 fingerprint + recipe_hash 传给 `scfm-embed`（如果下游是基础模型），或直接进常规聚类/注释流程——两条路都带着 provenance。

## 反例

> 我帮你把数据 normalize、log、选了 2000 个高变基因，然后聚类出了 8 个 cluster。

问题：在对话里"跑"了本该在用户机器上确定性执行的步骤，没留 recipe_hash、没指纹输入——换台机器结果就对不上，不可复现。

## 边界

- 本 skill **不装 scanpy/anndata**，只产出配方与脚本；实际计算在用户环境。
- doublet 检测、批次整合（scVI/harmony）等更重的步骤不在本配方默认里，需要时显式加入并同样记进 recipe。
