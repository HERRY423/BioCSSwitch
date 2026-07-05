---
name: scfm-embed
description: 用于用单细胞基础模型（Geneformer / scGPT）给单细胞数据算 embedding，并强制记录 provenance。触发：Geneformer、scGPT、single-cell foundation model、scFM、cell embedding、细胞表征、单细胞基础模型、给单细胞数据算 embedding、用大模型编码单细胞、cell type annotation with foundation model、reference mapping、scGPT 微调、Geneformer 提取特征、单细胞 embedding provenance。禁用于：常规 scanpy 聚类/DEG（那用 single-cell-prep / omics 流程），或把这些模型当聊天问答。
---

# 单细胞基础模型 embedding（scfm-embed）

**Geneformer / scGPT 是计算工具，不是聊天模型。** 它们把表达谱编码成 embedding，没有"观点"。所以用它们只有一个纪律：**每份 embedding 必须能被复现**——否则下游聚类、注释、reference mapping 全都不可信。这个 skill 强制走 provenance。

## 铁律

1. **不在对话里"假装"跑模型**。你不能凭空说"Geneformer 认为这些细胞是 T 细胞"。你的产出是**可复现的运行脚本 + provenance 记录**，用户在自己（有 GPU 的）机器上跑，embedding 落用户磁盘。
2. **provenance 五件套缺一不可**：输入 AnnData 内容哈希、预处理参数哈希、模型版本/checkpoint、embedding 维度/输出哈希/pooling、运行环境 + seed。少一项 = `scfm_provenance_verify` 判 `not_reproducible`。
3. **输入 ID 类型必须对**：Geneformer 要 Ensembl ID + rank-value（不做 log/HVG）；scGPT 要 gene symbol + HVG + value binning。喂错预处理，embedding 无意义。用 `scfm_registry` 核对。
4. **版本要钉到 commit**。"Geneformer" 不是版本；`gf-12L-30M-i2048` + 本地 commit 才是。把确切版本记进 provenance.model。

## 工作流

**Step 1：指纹输入**。调 `anndata_fingerprint`（bio-singlecell）拿到元数据指纹，并把它给出的 snippet 交用户在本地跑，算出**真·内容哈希** `anndata_sha256`。

**Step 2：定预处理**。调 `sc_preprocess_recipe(target_model=geneformer|scgpt)` 拿到模型对口的确定性配方 + `recipe_hash` + 脚本。

**Step 3：核版本**。调 `scfm_registry(model=...)` 确认 checkpoint 与输入要求。

**Step 4：出 embedding 计划**。调 `scfm_embed_plan(model, anndata_sha256, preprocessing_hash, seed, ...)`。你会拿到：运行脚本 + provenance 骨架（输入指纹/预处理哈希已接好）+ `gaps`（还缺什么）。

**Step 5：用户跑脚本**（本地 GPU）。脚本末尾会打印 `embedding_dim` 与 `output_sha256`。

**Step 6：定稿 provenance**。把运行后的值（版本、维度、output_sha256、python/包版本、device）补进，调 `scfm_provenance_record` 生成带 `provenance_hash` 的正式记录。

**Step 7：验真再用**。任何拿到 embedding + 记录的人，用 `scfm_provenance_verify` 重算哈希 + 查必填。`verdict != trustworthy` 就不许进下游分析。

## 一个正例

用户："用 Geneformer 给我这个 PBMC 数据集算 cell embedding 做 reference mapping。"

你：先 `anndata_fingerprint`（发现 var_id_type 是 symbol → 提醒 Geneformer 需要 Ensembl，给转换步骤）→ `sc_preprocess_recipe(geneformer)` → `scfm_embed_plan(geneformer, gf-12L-30M-i2048, ...)` → 交脚本 + provenance 骨架，说明"在你的 GPU 机上跑，跑完把 output_sha256 等填回，我帮你定稿 provenance"。

## 一个反例（不要这样）

> 我用 Geneformer 分析了你的数据，这些是 CD8 T 细胞，marker 是 CD8A/GZMB。

问题：你没有也不该在对话里"跑"模型；没有输入哈希、没有版本、没有 seed——不可复现、不可信，还把"计算工具"当成了会下结论的聊天模型。

## 边界

- 适配层**不含模型权重**，也不装 torch/geneformer/scgpt——这些在用户环境。工具只负责钉版本、锁 provenance 结构、算哈希、生成脚本。
- embedding 的**下游解释**（这是什么细胞、批次是否混了）不属于本 skill 的编码步；那要另走 QC / 注释流程，且同样要引用这份 provenance。
