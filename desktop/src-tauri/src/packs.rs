//! 科研工具包（bio-* pack）装配 —— 适配 v0.3 多 profile 架构。
//!
//! 一个 pack = 资源目录下 `packs/<id>/pack.json` + 若干 Python MCP 脚本 + 可选 Skill。
//! 启用 = 把它的 MCP server 定义 merge 进沙箱 Claude Science 的 MCP 配置文件，
//! 并把它的 Skill 目录拷进沙箱的 skills 目录。停用 = 从沙箱里移除。
//!
//! # 铁律相关
//!   - 只写 `<SANDBOX_HOME>/.claude-science/`，绝不碰真实 `~/.claude-science`。
//!   - 官方模式下不装配（lib.rs 里 gate）。
//!   - MCP 子进程由 Claude Science 自己起，本模块只负责写 MCP 配置文件。
//!
//! # 与 v0.3 profile 架构的联系
//!   pack 不感知具体 profile；`cfg.active_profile()` 只用来做敏感模式的 host 白名单校验，
//!   与 pack 是否装配无关。这样切换 profile 不需要重装 pack。
//!
//! # 别名机制（feature 2：远程 MCP 的本地替身）
//!   ServerDef.aliases 允许一个 MCP 以多个名字挂进沙箱。bio-mcp-shim pack 用它让本地
//!   MCP 以 Anthropic 托管 MCP 的同名（pubmed / clinical-trials / chembl / biorxiv）
//!   出现在 Science 工具列表里，用户"仍然看到远程 MCP"但流量走本地。
//!
//! # 已知未验证点
//!   Claude Science 的 MCP 配置文件位置尚未从二进制逆向确认。当前按 Claude Code 惯例：
//!     - 路径：`<data-dir>/mcp-servers.json`
//!     - schema：`{"mcpServers": {"<name>": {"command", "args", "env"}}}`
//!   若逆向后发现 Science 用别的路径 / schema，改 `MCP_CONFIG_REL` + `write_mcp_config`。

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::config;

/// TODO(verify): 与 Science 二进制对齐后修正。
pub const MCP_CONFIG_REL: &str = "mcp-servers.json";
pub const SKILLS_REL: &str = "skills";

/// pack.json 顶层 schema。
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct PackDef {
    pub id: String,
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub requires_env: Vec<String>,
    #[serde(default)]
    pub optional_env: Vec<OptionalEnv>,
    #[serde(default)]
    pub depends_on: Vec<String>,
    #[serde(default)]
    pub servers: Vec<ServerDef>,
    #[serde(default)]
    pub skills: Vec<SkillDef>,
    /// pack 支持的任务标签（供任务路由 UI 展示）。
    #[serde(default)]
    pub task_tags: Vec<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct OptionalEnv {
    pub name: String,
    #[serde(default)]
    pub label: String,
    #[serde(default)]
    pub url: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ServerDef {
    pub name: String,
    pub script: String,
    #[serde(default)]
    pub env_pass: Vec<String>,
    /// 若非空：这个 MCP 会以 alias 名字**额外**注册，用于"远程 MCP 本地替身"。
    /// 例如 shim 里让 `bio-mcp-shim-pubmed` 也以 `pubmed` 名字挂进 Science。
    #[serde(default)]
    pub aliases: Vec<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct SkillDef {
    pub id: String,
    pub src: String,
}

/// 扫描资源根下 `packs/*/pack.json`。跳过下划线开头的目录（`_lib`）。
pub fn list_packs(asset_root: &Path) -> Vec<PackDef> {
    let packs_dir = asset_root.join("packs");
    let mut out = Vec::new();
    let Ok(entries) = fs::read_dir(&packs_dir) else {
        return out;
    };
    for e in entries.flatten() {
        let name = e.file_name();
        let name = name.to_string_lossy();
        if name.starts_with('_') || name.starts_with('.') {
            continue;
        }
        let pj = e.path().join("pack.json");
        if !pj.is_file() {
            continue;
        }
        match fs::read(&pj)
            .ok()
            .and_then(|b| serde_json::from_slice::<PackDef>(&b).ok())
        {
            Some(p) => out.push(p),
            None => eprintln!("[packs] 跳过：{} 无法解析", pj.display()),
        }
    }
    out.sort_by(|a, b| a.id.cmp(&b.id));
    out
}

fn collect_pack_and_deps(
    id: &str,
    by_id: &BTreeMap<String, &PackDef>,
    stack: &mut Vec<String>,
    active: &mut BTreeSet<String>,
) -> Result<(), String> {
    if active.contains(id) {
        return Ok(());
    }
    if let Some(pos) = stack.iter().position(|x| x == id) {
        let mut cycle = stack[pos..].to_vec();
        cycle.push(id.to_string());
        return Err(format!("pack dependency cycle detected: {}", cycle.join(" -> ")));
    }
    let pack = by_id
        .get(id)
        .ok_or_else(|| format!("enabled pack or dependency '{id}' is not installed"))?;

    stack.push(id.to_string());
    for dep in &pack.depends_on {
        collect_pack_and_deps(dep, by_id, stack, active)?;
    }
    stack.pop();
    active.insert(id.to_string());
    Ok(())
}

fn resolve_enabled_pack_ids(
    all: &[PackDef],
    enabled: &BTreeMap<String, bool>,
) -> Result<(BTreeSet<String>, Vec<String>), String> {
    let by_id: BTreeMap<String, &PackDef> =
        all.iter().map(|p| (p.id.clone(), p)).collect();
    let explicit: BTreeSet<String> = enabled
        .iter()
        .filter_map(|(id, on)| if *on { Some(id.clone()) } else { None })
        .collect();
    let mut active = BTreeSet::new();
    for id in &explicit {
        collect_pack_and_deps(id, &by_id, &mut Vec::new(), &mut active)?;
    }
    let warnings = active
        .difference(&explicit)
        .map(|id| format!("{id} auto-enabled because a selected pack depends on it"))
        .collect();
    Ok((active, warnings))
}

fn python3() -> Result<String, String> {
    crate::proc::find_exe("python3")
        .map(|p| p.to_string_lossy().to_string())
        .ok_or_else(|| {
            "缺少 python3（pack 的 MCP server 用 Python 实现）。macOS 一般自带 /usr/bin/python3。"
                .to_string()
        })
}

fn backup_corrupt_mcp(path: &Path, bytes: &[u8]) -> io::Result<PathBuf> {
    if let Some(parent) = path.parent() {
        config::assert_not_symlink(parent)?;
    }
    let name = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("mcp-servers.json");
    let backup = path.with_file_name(format!(
        "{name}.corrupt.{}.{}",
        std::process::id(),
        config::now_ms()
    ));
    config::write_bytes_atomic_0600(&backup, bytes)?;
    Ok(backup)
}

fn read_mcp_config(path: &Path) -> Result<serde_json::Value, String> {
    if !path.is_file() {
        return Ok(serde_json::json!({"mcpServers": {}}));
    }
    config::assert_not_symlink(path).map_err(|e| e.to_string())?;
    let bytes = fs::read(path).map_err(|e| e.to_string())?;
    let mut v: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(e) => {
            let backup = backup_corrupt_mcp(path, &bytes)
                .map_err(|be| format!("mcp-servers.json 解析失败：{e}；坏文件备份失败：{be}"))?;
            return Err(format!(
                "mcp-servers.json 解析失败：{e}；已备份坏文件到 {}，为避免覆盖用户配置，本次未写入。",
                backup.display()
            ));
        }
    };
    if v.get("mcpServers").is_none() {
        if let Some(m) = v.as_object_mut() {
            m.insert("mcpServers".into(), serde_json::json!({}));
        }
    }
    Ok(v)
}

fn write_mcp_config(path: &Path, v: &serde_json::Value) -> io::Result<()> {
    let json = serde_json::to_vec_pretty(v)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    config::write_bytes_atomic_0600(path, &json)
}

fn server_entry(
    py: &str,
    asset_root: &Path,
    srv: &ServerDef,
    pack_env: &BTreeMap<String, String>,
) -> serde_json::Value {
    let script_abs = asset_root.join(&srv.script);
    let mut env_obj = serde_json::Map::new();
    for k in &srv.env_pass {
        if let Some(v) = pack_env.get(k) {
            if !v.is_empty() {
                env_obj.insert(k.clone(), serde_json::Value::String(v.clone()));
            }
        }
    }
    env_obj.insert(
        "CSSWITCH_CACHE_DIR".to_string(),
        serde_json::Value::String(
            config::default_dir()
                .join("cache")
                .to_string_lossy()
                .into(),
        ),
    );
    env_obj.insert(
        "CSSWITCH_AUDIT_DIR".to_string(),
        serde_json::Value::String(
            config::default_dir()
                .join("audit")
                .to_string_lossy()
                .into(),
        ),
    );
    serde_json::json!({
        "command": py,
        "args": [script_abs.to_string_lossy()],
        "env": env_obj,
    })
}

fn install_skill(sandbox_data_dir: &Path, asset_root: &Path, s: &SkillDef) -> io::Result<()> {
    let src = asset_root.join(&s.src);
    if !src.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("Skill 源目录不存在：{}", src.display()),
        ));
    }
    let dst_root = sandbox_data_dir.join(SKILLS_REL);
    config::assert_not_symlink(&dst_root)?;
    fs::create_dir_all(&dst_root)?;
    let dst = dst_root.join(&s.id);
    if dst.exists() {
        config::assert_not_symlink(&dst)?;
        fs::remove_dir_all(&dst)?;
    }
    copy_dir_all(&src, &dst)?;
    Ok(())
}

fn uninstall_skill(sandbox_data_dir: &Path, s: &SkillDef) -> io::Result<()> {
    let dst = sandbox_data_dir.join(SKILLS_REL).join(&s.id);
    if dst.exists() {
        config::assert_not_symlink(&dst)?;
        fs::remove_dir_all(&dst)?;
    }
    Ok(())
}

fn copy_dir_all(src: &Path, dst: &Path) -> io::Result<()> {
    fs::create_dir_all(dst)?;
    for e in fs::read_dir(src)? {
        let e = e?;
        let ft = e.file_type()?;
        let sp = e.path();
        let dp = dst.join(e.file_name());
        if ft.is_symlink() {
            continue;
        }
        if ft.is_dir() {
            copy_dir_all(&sp, &dp)?;
        } else {
            fs::copy(&sp, &dp)?;
        }
    }
    Ok(())
}

/// 按 enabled 集合重建沙箱 MCP 配置 + Skill 目录。幂等。
pub fn apply(
    asset_root: &Path,
    sandbox_data_dir: &Path,
    enabled: &BTreeMap<String, bool>,
    pack_env: &BTreeMap<String, String>,
) -> Result<(Vec<String>, Vec<String>), String> {
    let all = list_packs(asset_root);
    let py = python3()?;
    let (active_pack_ids, dep_warnings) = resolve_enabled_pack_ids(&all, enabled)?;

    // 保留【非本项目管理的】server（用户可能自加了别的 MCP）。归属判断：
    //   1) `bio-` 前缀 → 我们的
    //   2) 出现在任何 pack 的 aliases 里 → 我们的（如 shim 别名 pubmed）
    let mcp_path = sandbox_data_dir.join(MCP_CONFIG_REL);
    let mut cfg = read_mcp_config(&mcp_path)?;
    let servers_obj = cfg
        .get_mut("mcpServers")
        .and_then(|v| v.as_object_mut())
        .ok_or("mcp-servers.json schema 异常：mcpServers 不是 object")?;

    let managed_aliases: HashSet<String> = all
        .iter()
        .flat_map(|p| p.servers.iter())
        .flat_map(|s| s.aliases.clone())
        .collect();
    let keep: Vec<String> = servers_obj
        .keys()
        .filter(|k| !k.starts_with("bio-") && !managed_aliases.contains(*k))
        .cloned()
        .collect();
    let mut new_servers = serde_json::Map::new();
    for k in keep {
        if let Some(v) = servers_obj.remove(&k) {
            new_servers.insert(k, v);
        }
    }

    let mut applied: Vec<String> = Vec::new();
    let mut warnings = dep_warnings;

    for pack in &all {
        let on = active_pack_ids.contains(&pack.id);
        if on {
            let mut missing = Vec::new();
            for k in &pack.requires_env {
                if pack_env.get(k).map(|v| v.is_empty()).unwrap_or(true) {
                    missing.push(k.clone());
                }
            }
            if !missing.is_empty() {
                warnings.push(format!(
                    "{} 已勾选，但缺必填环境变量 {}，未装配",
                    pack.id,
                    missing.join(", ")
                ));
                continue;
            }
            for srv in &pack.servers {
                let entry = server_entry(&py, asset_root, srv, pack_env);
                new_servers.insert(srv.name.clone(), entry.clone());
                for alias in &srv.aliases {
                    if new_servers.contains_key(alias) {
                        warnings.push(format!(
                            "{}: 别名 {} 与既有 MCP 冲突，跳过（本项目管理的 server {} 仍装配）",
                            pack.id, alias, srv.name
                        ));
                        continue;
                    }
                    new_servers.insert(alias.clone(), entry.clone());
                }
            }
            for sk in &pack.skills {
                if let Err(e) = install_skill(sandbox_data_dir, asset_root, sk) {
                    warnings.push(format!("{}: Skill 装配失败：{}", pack.id, e));
                }
            }
            applied.push(pack.id.clone());
        } else {
            for sk in &pack.skills {
                let _ = uninstall_skill(sandbox_data_dir, sk);
            }
        }
    }

    *servers_obj = new_servers;
    write_mcp_config(&mcp_path, &cfg).map_err(|e| format!("写 MCP 配置失败：{e}"))?;
    Ok((applied, warnings))
}

/// 拆掉所有本项目管理的 MCP 条目（切官方模式时用）。
pub fn purge_bio_from_mcp(asset_root: &Path, sandbox_data_dir: &Path) -> io::Result<()> {
    let mcp_path = sandbox_data_dir.join(MCP_CONFIG_REL);
    if !mcp_path.is_file() {
        return Ok(());
    }
    let all = list_packs(asset_root);
    let managed_aliases: HashSet<String> = all
        .iter()
        .flat_map(|p| p.servers.iter())
        .flat_map(|s| s.aliases.clone())
        .collect();
    let mut cfg = read_mcp_config(&mcp_path)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    if let Some(obj) = cfg.get_mut("mcpServers").and_then(|v| v.as_object_mut()) {
        let bio_keys: Vec<String> = obj
            .keys()
            .filter(|k| k.starts_with("bio-") || managed_aliases.contains(*k))
            .cloned()
            .collect();
        for k in bio_keys {
            obj.remove(&k);
        }
    }
    write_mcp_config(&mcp_path, &cfg)
}

// ---------- 任务路由（feature 1）辅助 ----------

/// 预设的科研任务清单。前端渲染路由 UI 时会 iterate 这个 slice；用户能覆盖每个任务的
/// profile 指向，但**不能新增/删除**任务本身（任务枚举与 Skill 触发词耦合，随代码走）。
///
/// 每项：(task_id, 中文显示名, 一句描述 / 关键能力需求)
pub const BIOMED_TASKS: &[(&str, &str, &str)] = &[
    ("lit-review",       "文献综述",             "搜索 + 综合 + 证据分级；长上下文优先"),
    ("clinical-trials",  "临床试验检索",         "NCT / 试验设计 benchmark；工具调用密集"),
    ("target-discovery", "靶点发现 / 老药新用",  "Open Targets / ChEMBL 组合查询；工具调用密集"),
    ("omics-code",       "组学代码 / 数据分析",  "DESeq2 / limma / clusterProfiler 脚本生成；代码保真优先"),
    ("long-context-pdf", "长上下文 PDF",         "全文 PDF 分析；长上下文优先"),
    ("tool-heavy",       "工具调用密集任务",     "多源多轮拉取；tool_use 稳定性优先"),
    ("evidence-check",   "引用 / 证据审计",      "PMID/DOI/NCT 校验；JSON 稳定性优先"),
    ("phi-sensitive",    "含 PHI 的临床数据",    "受 sensitive_mode 门控，只允许本地端点"),
];
