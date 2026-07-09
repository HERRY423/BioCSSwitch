---
name: sc-analysis
description: 端到端、分阶段验收的单细胞分析工作流，用于 scRNA-seq、10x Genomics、Scanpy/Seurat、单细胞差异表达、轨迹、RNA velocity、细胞通信、多模态、空间与 scFM。负责串联 single-cell-prep、sc-downstream-analysis、scfm-embed、bio-spatial 与实验验证。禁用于 bulk RNA-seq / microarray GEO（用 geo-triage）。
---

# 端到端单细胞分析（sc-analysis）

本 skill 按 **measure → contextualize → perturb → model** 组织分析。标准 scRNA-seq
是基础设施，不以“跑完 UMAP”为完成标准；每一阶段必须产生可审计产物并通过验收门槛。
优先回答生物学问题，前沿模型仅在能增加非冗余信息或可检验预测时接入。

## 0. 先定义推断单位与主问题

先记录：

- assay、物种、组织、解离方式、建库批次、样本/供体、条件、时间点与配对设计；
- 输入层是否有 raw UMI counts；是否有 raw/filtered droplet matrix、spliced/unspliced、
  ADT、ATAC、空间坐标或扰动 guide；
- 主终点属于组成变化、状态变化、谱系、空间邻域、扰动响应还是预测任务；
- biological replicate 是 sample/donor，不是 cell；同一供体的多文库不是独立重复。

如果 condition 与 batch 完全混杂，停止 condition-level claim。整合算法不能恢复实验设计中
不存在的可识别性。

## 1. 输入与实验设计 Gate

1. 用 `anndata_fingerprint` 锁定数据描述与真内容哈希。
2. 校验细胞和基因 ID 唯一、counts 非负且近似整数、sample→condition 映射唯一。
3. 输出 sample×condition、condition×batch、每样本细胞数和建库方式的设计审计表。
4. 有条件比较但无 sample/donor key 时阻断 DEG；不允许把细胞数当重复数。
5. 从 FASTQ 开始时先走 nf-core/scrnaseq；ambient RNA 校正需 raw/filtered droplets，
   不能从只有 filtered H5AD 的输入中“补做”。

**Gate 通过条件**：raw counts 可确认；推断单位明确；关键混杂已识别；输入哈希已记录。

## 2. 样本感知 QC Gate

1. 在过滤前计算 counts、genes、线粒体、核糖体、血红蛋白、top-gene concentration。
2. 调用 `sc_qc_thresholds`，按 sample/capture library 使用 MAD 阈值；固定阈值仅作硬上限，
   不作为跨组织通用规则。
3. 保存每个细胞的保留/排除及原因，比较过滤前后每个样本与条件的细胞组成。
4. droplet 数据调用 `sc_doublet_recipe`，按 capture library 分开检测；doublet 是建议标签，
   同时检查是否富集于真实过渡态或高 RNA 含量细胞。
5. raw droplets 可用时比较 SoupX/CellBender 与“无校正”基线；报告敏感性，不用一个模型结果
   直接覆盖原始 counts。

**Gate 通过条件**：无单一样本被阈值异常清空；排除率和原因可追溯；counts 层未被覆盖。

## 3. 表示、整合与聚类 Gate

使用 `sc_scanpy_pipeline.py` 生成可运行脚本。必须传 `--sample-key`；有条件比较时同时传
`--condition-key`；技术整合才传 `--batch-key`。生成器会输出：

```bash
python packs/bio-workflows/generators/sc_scanpy_pipeline.py \
  --h5ad data/input.h5ad --out analysis/run_scanpy.py --outdir results \
  --organism human --tissue lung \
  --sample-key donor_id --condition-key disease \
  --batch-key library_batch --doublet-key capture_id --include-doublet
python analysis/run_scanpy.py
```

- `design_audit.json` 与 condition×batch 表；
- 分样本 MAD 阈值、逐细胞 QC 决策和逐文库 doublet 报告；
- 未整合 PCA 与整合表示并存；
- 整合前后 batch silhouette、邻域 mixing entropy、condition geometry；
- 聚类随机种子稳定性、分辨率敏感性、cluster 的样本支持；
- `analysis_readiness.json` 与完整 provenance。

整合采用“最弱充分方法”：

- 同协议少量批次可先 Harmony；复杂计数建模才用 scVI；
- 不因“有 batch 字段”自动整合；先看未整合表示；
- 整合只用于 embedding/graph，不产生 corrected counts；
- 只有 batch mixing 改善且已知生物结构未明显丢失，才接受整合表示；
- 至少保留未整合基线。强整合导致 condition/稀有群体消失时视为 over-correction。

聚类不是生物学真值。报告分辨率网格、不同 seed 的 ARI、每群跨样本支持；单一样本驱动、
不稳定或仅由 QC 指标定义的 cluster 不进入正式命名。

## 4. 注释 Gate

调用 `sc_celltype_recipe`，采用三角验证：

1. reference mapping（CellTypist/SingleR 或匹配组织 atlas）；
2. 正、负 marker 与 marker module；
3. 跨样本复现、doublet/QC 富集和生物学上下文。

输出 cell-level confidence、cluster×reference 交叉表、冲突原因。设置信心阈值并保留
`unknown` / `ambiguous`，不强制每个细胞获得精确标签。稀有细胞需跨样本出现或有正交验证；
只在 UMAP 上成岛不构成新细胞类型证据。

## 5. 下游推断按问题分支

### 条件差异与组成

- condition-level 表达变化：`sc_deg_recipe`，默认 sample×cell type pseudobulk；
  每组通常至少 3 个 biological replicates，并在设计式中加入配对/批次协变量。
- Wilcoxon 仅用于 cluster marker/探索排序，不称为有重复样本的 condition DEG。
- 组成差异使用 sample-level proportion/count 模型并考虑 compositionality；不对细胞做
  独立 t-test。

### 动态与谱系

- `sc_trajectory_recipe`；root、方向和分支必须来自时间点、lineage tracing 或明确生物学先验。
- RNA velocity 先确认 spliced/unspliced 与动力学假设；至少与不依赖 velocity 的轨迹结果比较。
- trajectory/velocity 是推断，不是实际谱系；代谢标记或 lineage barcode 才提供更直接时间证据。

### 细胞通信

- `sc_communication_recipe`；至少比较一种数据库方法与表达/空间邻接基线。
- 配体-受体共表达是候选机制，不是通信证明；优先用空间邻近、蛋白、扰动或阻断实验验证。

### 富集与调控

- marker 用 `sc_marker_recipe`，富集用 `sc_enrichment_recipe`；
- gene-set test 的统计单位和背景基因集必须明确；避免把同一 marker list 循环解释成多个“发现”。

## 6. 前沿能力的接入原则

### 多模态与空间

- CITE-seq/multiome 用 `sc_multimodal_recipe`，每个模态先独立 QC，再做 WNN/totalVI/MultiVI；
- spatial 交给 `bio-spatial`。细胞分割、背景和 transcript assignment 是首要误差源；
- 只有 same-cell / same-section 信息才可声称跨调控层关联，分别测量的数据只能做对齐推断。

### scFM 与 virtual-cell

scFM 不是默认步骤。用 `scfm-embed` 时必须：

- donor/sample-stratified split，禁止同供体泄漏；
- 与 PCA/scVI、均值预测和轻量监督模型比较；
- 用任务相关指标和 bootstrap CI，而非只展示 UMAP；
- 对 perturbation prediction 做 held-out gene/compound/cell-state 的 OOD 测试；
- 未超过简单基线时明确报告负结果，不能用参数规模替代证据。

### 扰动与因果

观测性单细胞数据产生假设；Perturb-seq、空间扰动、rescue 或正交实验负责因果验证。
优先输出“可区分竞争解释的下一实验”，而不是把 trajectory、communication 或模型 attention
写成机制。

## 7. 交付与结论 Gate

交付至少包含：

- 输入哈希、参数/seed、软件版本、设计审计、QC 排除表；
- 未整合与整合基线、整合验收指标、聚类稳定性与样本支持；
- 注释置信度/冲突、pseudobulk 或其他 sample-level 统计设计；
- 主分析加一项关键敏感性分析；
- Known knowns / Known unknowns / Conflicts / Missing data / Next experiment。

没有用户实际运行产物时，只能评价配方与可识别性，不能编造 cluster、marker、DEG、轨迹、
通信或模型性能。

## 禁止事项

- 不从 normalized/log X 伪造 raw counts；
- 不把 cell 当 replicate；
- 不以 UMAP 分离度挑选整合算法；
- 不覆盖未整合表示或 counts；
- 不把 cluster marker 当 condition DEG；
- 不强制注释所有细胞；
- 不把 scFM、velocity、通信或空间共定位写成因果证明；
- 不让超大模型跳过简单基线、外部验证和失败报告。
