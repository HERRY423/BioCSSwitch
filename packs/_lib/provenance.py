"""可复现来源（provenance）工具 —— 规范化 JSON + 内容哈希。

单细胞基础模型（Geneformer / scGPT）作为「计算工具适配层」而非聊天模型时，输入输出
必须可追溯：同样的输入 + 同样的参数 + 同样的模型版本 → 同样的 embedding。这个模块给出
**确定性**的规范序列化与 sha256，任何 provenance 记录都能算出一个稳定的 content hash，
第三方拿到记录能自己重算、验真。

设计：
  - `stable_json`：sort_keys + 紧凑分隔符 → 同一语义对象永远同一字节串（跨机器、跨进程）。
  - `content_hash`：对规范字节串取 sha256。
  - `hash_descriptor`：对"数据描述符"（shape / var_id / 关键统计量）取哈希——在没有 anndata/
    h5py 依赖、纯 stdlib 的前提下，作为 AnnData 内容指纹的可核对代理；同时能生成一段在用户
    机器上算"真·内容哈希"的 Python 片段。
  - 纯 stdlib、纯函数、可离线、可单测。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


def stable_json(obj: Any) -> str:
    """规范化 JSON：排序键 + 紧凑分隔符。跨机器字节级一致。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def content_hash(obj: Any, algo: str = "sha256") -> str:
    """对任意可 JSON 化对象取规范哈希，返回 '<algo>:<hex>'。"""
    h = hashlib.new(algo)
    h.update(stable_json(obj).encode("utf-8"))
    return f"{algo}:{h.hexdigest()}"


def hash_descriptor(descriptor: Dict[str, Any]) -> str:
    """对 AnnData 描述符取指纹。描述符是**纯元数据**（shape / obs_keys / var_keys /
    var_id_type / layers / 可选的 X_checksum 等），不含实际表达矩阵——因此这是"可核对代理
    指纹"，不是真·内容哈希。真·内容哈希由 `anndata_hash_snippet` 生成的代码在用户机器上算。"""
    # 只挑对复现有意义的字段，避免无关键（如临时路径）污染指纹
    keep = {}
    for k in ("n_obs", "n_var", "var_id_type", "obs_keys", "var_keys",
              "layers", "X_dtype", "X_checksum", "assay", "organism", "raw_present"):
        if k in descriptor and descriptor[k] is not None:
            v = descriptor[k]
            if isinstance(v, list):
                v = sorted(str(x) for x in v)  # 键顺序无关
            keep[k] = v
    return content_hash({"anndata_descriptor": keep})


def anndata_hash_snippet(layer: Optional[str] = None) -> str:
    """生成一段在用户机器上计算真·AnnData 内容哈希的 Python 代码。
    对 X（或指定 layer）+ var_names + obs_names 做确定性 sha256——用户跑一次就能把真实
    内容哈希填进 provenance 记录的 anndata_sha256。"""
    src = "X" if not layer else f"layers['{layer}']"
    return f'''# 计算 AnnData 内容哈希（在你的机器上跑，把结果填进 provenance.anndata_sha256）
import hashlib, numpy as np, scipy.sparse as sp, anndata as ad
adata = ad.read_h5ad("YOUR_FILE.h5ad")
M = adata.{src}
if sp.issparse(M):
    M = M.tocsr()
    buf = np.concatenate([M.data, M.indices.astype(np.int64), M.indptr.astype(np.int64)])
else:
    buf = np.ascontiguousarray(M)
h = hashlib.sha256()
h.update(np.asarray(buf).tobytes())
h.update(("\\n".join(map(str, adata.var_names))).encode())
h.update(("\\n".join(map(str, adata.obs_names))).encode())
print("sha256:" + h.hexdigest())'''


def required_fields_missing(record: Dict[str, Any], required: List[str]) -> List[str]:
    """检查 provenance 记录是否含全部必填字段（支持点路径 a.b.c）。返回缺失路径列表。"""
    missing: List[str] = []
    for path in required:
        cur: Any = record
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur and cur[part] not in (None, "", []):
                cur = cur[part]
            else:
                ok = False
                break
        if not ok:
            missing.append(path)
    return missing
