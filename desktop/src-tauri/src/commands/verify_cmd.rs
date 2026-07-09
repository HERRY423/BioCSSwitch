/// 取 Claude Science 版本（`<bin> --version` 首个 token）。二进制不在 → "unknown"。
/// 用于 verification 环境指纹：Science 更新后版本变了 → 提示"验证结果可能过期"。
fn science_version() -> String {
    if !Path::new(SCIENCE_BIN).is_file() {
        return "unknown".to_string();
    }
    match Command::new(SCIENCE_BIN).arg("--version").output() {
        Ok(out) => {
            let s = String::from_utf8_lossy(&out.stdout);
            // 输出可能是 "claude-science 1.2.3" 或纯版本号；取含数字的首行首 token
            for line in s.lines() {
                let t = line.trim();
                if t.chars().any(|c| c.is_ascii_digit()) {
                    // 取最后一个 whitespace-token（一般是版本号）
                    return t.split_whitespace().last().unwrap_or(t).to_string();
                }
            }
            "unknown".to_string()
        }
        Err(_) => "unknown".to_string(),
    }
}

#[tauri::command]
fn start_smoke_verification(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let root = asset_root(&app).ok_or("找不到 packs/ 资源根")?;
    let sbx_data = sandbox_home().join(".claude-science");
    let py = proc::find_exe("python3")
        .ok_or("缺少 python3（canary MCP 需要）。")?
        .to_string_lossy()
        .to_string();
    let marker = verification::prepare_smoke(
        &root,
        &sbx_data,
        packs::MCP_CONFIG_REL,
        packs::SKILLS_REL,
        &py,
    )?;
    // 写回 config：pending 状态 + 环境指纹。指纹让"Science 更新后路径可能变"能被自动发现。
    let dir = config::default_dir();
    let m2 = marker.clone();
    let now = config::now_ms();
    let sci_ver = science_version();
    let py2 = py.clone();
    config::update(&dir, move |c| {
        c.verification.insert("mcp_verdict".into(), "mcp_path_pending".into());
        c.verification.insert("skill_verdict".into(), "unverified".into());
        c.verification.insert("last_marker".into(), m2);
        c.verification.insert("last_run_ms".into(), now.to_string());
        c.verification
            .insert("reason".into(), "canary 已写入，等待用户重启沙箱后 poll".into());
        // ── 环境指纹 ──
        c.verification.insert("science_version".into(), sci_ver);
        c.verification
            .insert("mcp_config_rel".into(), packs::MCP_CONFIG_REL.into());
        c.verification.insert("skills_rel".into(), packs::SKILLS_REL.into());
        c.verification.insert("python_path".into(), py2);
    })
    .map_err(|e| e.to_string())?;
    Ok(json!({
        "marker": marker,
        "next_step": "请停沙箱（全部停止），再点一键开始重启，然后回本页点「检查 canary」。",
    }))
}

#[tauri::command]
fn poll_smoke_verification() -> Result<serde_json::Value, String> {
    let dir = config::default_dir();
    let cfg = config::load_from(&dir).map_err(|e| e.to_string())?;
    let marker = cfg
        .verification
        .get("last_marker")
        .cloned()
        .unwrap_or_default();
    if marker.is_empty() {
        return Err("尚未跑过 canary。请先点「开始验证」。".into());
    }
    let result = verification::poll_smoke(&marker);
    let (verdict, reason) = match result {
        Some(true) => (
            "mcp_path_ok",
            "canary MCP 已被 Science 启动 → mcp-servers.json 路径正确 ✓",
        ),
        Some(false) => (
            "mcp_path_fail",
            "找到 marker 文件但 marker 值是旧的。可能沙箱未重启，请先重启再试。",
        ),
        None => (
            "mcp_path_fail",
            "未见 marker 文件。可能：(a) 沙箱未重启 (b) Science 用了别的 MCP 配置路径 (c) python3 不在 PATH",
        ),
    };
    let (v2, r2) = (verdict.to_string(), reason.to_string());
    let passed = verdict == "mcp_path_ok";
    let now = config::now_ms();
    config::update(&dir, move |c| {
        c.verification.insert("mcp_verdict".into(), v2);
        c.verification.insert("reason".into(), r2);
        if passed {
            // 记通过时间 + 通过时的 Science 版本（指纹基线），供以后判过期。
            c.verification.insert("mcp_pass_ms".into(), now.to_string());
        } else {
            c.verification
                .insert("last_fail_reason".into(), reason.to_string());
        }
    })
    .map_err(|e| e.to_string())?;
    Ok(json!({
        "verdict": verdict,
        "reason": reason,
        "marker": marker,
    }))
}

#[tauri::command]
fn confirm_skill_verified(user_confirmed: bool) -> Result<serde_json::Value, String> {
    let dir = config::default_dir();
    let (verdict, reason) = if user_confirmed {
        ("skill_path_ok", "用户在 Science 里发触发词，看到 canary-ok → skills 路径正确 ✓")
    } else {
        ("skill_path_fail", "用户报告未看到 canary-ok。Skill 路径可能不对。")
    };
    let (v2, r2) = (verdict.to_string(), reason.to_string());
    config::update(&dir, move |c| {
        c.verification.insert("skill_verdict".into(), v2);
        c.verification.insert("reason".into(), r2);
    })
    .map_err(|e| e.to_string())?;
    Ok(json!({"verdict": verdict, "reason": reason}))
}

#[tauri::command]
fn cleanup_smoke_verification(app: tauri::AppHandle) -> Result<(), String> {
    let sbx_data = sandbox_home().join(".claude-science");
    verification::cleanup_smoke(&sbx_data, packs::MCP_CONFIG_REL, packs::SKILLS_REL)
        .map_err(|e| e.to_string())?;
    let _ = app;
    Ok(())
}

/// 供 `list_packs` 附带回传：整体是否已通过验证（决定 pack chip 是否显示"实验中"）。
/// phase-5：带回环境指纹 + **过期判断**。Science 版本 / MCP 路径常量 / skills 路径常量
/// 任一与通过时不同 → `stale=true`，UI 提示"验证结果可能过期，请重跑"。
fn verification_summary(cfg: &config::Config) -> serde_json::Value {
    let v = &cfg.verification;
    let get = |k: &str| v.get(k).cloned();
    let mcp = get("mcp_verdict").unwrap_or_else(|| "unverified".into());
    let skill = get("skill_verdict").unwrap_or_else(|| "unverified".into());
    let verified = mcp == "mcp_path_ok" && skill == "skill_path_ok";

    // 过期判定：只有已验证时才谈过期。比对通过时记录的指纹 vs 当前。
    let stored_sci = get("science_version");
    let stored_mcp_rel = get("mcp_config_rel");
    let stored_skills_rel = get("skills_rel");
    let cur_sci = science_version();
    let mut stale_reasons: Vec<String> = Vec::new();
    if verified {
        if let Some(s) = &stored_sci {
            // 通过时 Science 是 unknown（dev 机）就不谈版本过期，避免噪声。
            if s != "unknown" && *s != cur_sci {
                stale_reasons.push(format!("Science 版本变了（验证时 {s} → 现在 {cur_sci}）"));
            }
        }
        if stored_mcp_rel.as_deref() != Some(packs::MCP_CONFIG_REL) {
            stale_reasons.push("MCP 配置路径常量已变".into());
        }
        if stored_skills_rel.as_deref() != Some(packs::SKILLS_REL) {
            stale_reasons.push("Skill 路径常量已变".into());
        }
    }
    let stale = !stale_reasons.is_empty();

    json!({
        "mcp_verdict": mcp,
        "skill_verdict": skill,
        "reason": get("reason"),
        "last_run_ms": get("last_run_ms").and_then(|s| s.parse::<i64>().ok()),
        "is_experimental": !verified,
        // ── 环境指纹（phase-5）──
        "fingerprint": {
            "science_version_at_pass": stored_sci,
            "science_version_now": cur_sci,
            "mcp_config_rel": stored_mcp_rel,
            "skills_rel": stored_skills_rel,
            "python_path": get("python_path"),
            "marker": get("last_marker"),
            "mcp_pass_ms": get("mcp_pass_ms").and_then(|s| s.parse::<i64>().ok()),
            "last_fail_reason": get("last_fail_reason"),
        },
        "stale": stale,
        "stale_reasons": stale_reasons,
    })
}
