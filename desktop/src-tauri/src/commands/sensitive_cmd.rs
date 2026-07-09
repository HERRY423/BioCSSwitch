/// 敏感模式门：拒绝把请求发给不在白名单里的 upstream。
/// 判断标准：`upstream_host(adapter, base_url)` 全小写命中白名单任一项。
fn assert_sensitive_ok(cfg: &config::Config) -> Result<(), String> {
    if !cfg.sensitive_mode {
        return Ok(());
    }
    let (adapter, base_url) = match cfg.active_profile() {
        Some(p) => (
            templates::adapter_for(&p.template_id).to_string(),
            p.base_url.clone(),
        ),
        None => return Err("敏感模式已开启，但无生效 profile。请先选一个受信端点。".into()),
    };
    // 规范化当前 upstream host（与白名单存的形式一致：去 scheme/port、小写、IDNA）。
    let raw_host = upstream_host(&adapter, &base_url);
    let host = netcanon::canonicalize_host(&raw_host).unwrap_or_else(|_| raw_host.to_lowercase());
    // 白名单里存的已是规范化 host；两边都规范化后精确比对。
    let ok = cfg.local_endpoint_hosts.iter().any(|h| {
        netcanon::canonicalize_host(h)
            .map(|c| c == host)
            .unwrap_or(false)
    });
    if ok {
        return Ok(());
    }
    let hint = if cfg.local_endpoint_hosts.is_empty() {
        "（白名单为空）".to_string()
    } else {
        format!("（白名单：{}）", cfg.local_endpoint_hosts.join(", "))
    };
    Err(format!(
        "敏感模式：拒绝把请求发到 `{host}`（未在受信端点白名单里）{hint}。\n\
         请把机构自建端点 host 加进白名单，或关闭敏感模式。真实实例 8765 未受影响。",
        host = host,
        hint = hint
    ))
}

/// 敏感 / 合规模式开关。开启后：白名单外的 upstream 一律拒绝起沙箱。
#[tauri::command]
fn set_sensitive_mode(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    enabled: bool,
) -> Result<serde_json::Value, String> {
    let dir = config::default_dir();
    let cfg = config::update(&dir, move |c| {
        c.sensitive_mode = enabled;
    })
    .map_err(|e| e.to_string())?;
    let mut sandbox_stopped = false;
    if enabled && assert_sensitive_ok(&cfg).is_err() && sandbox_running_ours(cfg.sandbox_port) {
        let mut st = lock(&state);
        let _ = stop_sandbox_inner(&app, &mut st);
        sandbox_stopped = true;
    }
    let bio_privacy_on = cfg
        .enabled_packs
        .get("bio-privacy")
        .copied()
        .unwrap_or(false);
    Ok(json!({
        "ok": true, "sensitive_mode": enabled,
        "sandbox_stopped": sandbox_stopped,
        "suggest_enable_bio_privacy": enabled && !bio_privacy_on,
    }))
}

/// 设置受信端点白名单。输入可以是 URL 或裸 host（带不带 port / scheme 都行）。
///
/// phase-5 加固：
///   1. 每条经 `netcanon::canonicalize_host` 统一成 host（去 scheme/port/末尾点、小写、IDNA）。
///   2. 分类：localhost / 私网 IP → 自动收；公网域名 → **只在 `confirm_public=true` 时收**
///      （"用户明确确认的机构域名"）。
///   3. denylist（公有大模型 API host，含子域）硬拒。
///
/// 返回 `{ok, hosts, needs_confirm, denied, invalid}`，让 UI 能提示用户逐条确认。
#[tauri::command]
fn set_local_endpoint_hosts(
    hosts: Vec<String>,
    confirm_public: Option<bool>,
) -> Result<serde_json::Value, String> {
    let confirm = confirm_public.unwrap_or(false);
    let mut accepted: Vec<String> = Vec::new();
    let mut needs_confirm: Vec<String> = Vec::new();
    let mut denied: Vec<String> = Vec::new();
    let mut invalid: Vec<String> = Vec::new();

    for raw in &hosts {
        match netcanon::vet_one(raw) {
            netcanon::HostVerdict::AutoAccept(h) => accepted.push(h),
            netcanon::HostVerdict::NeedsConfirm(h) => {
                if confirm {
                    accepted.push(h);
                } else {
                    needs_confirm.push(h);
                }
            }
            netcanon::HostVerdict::Denied(h) => denied.push(h),
            netcanon::HostVerdict::Invalid(_) => invalid.push(raw.clone()),
        }
    }
    accepted.sort();
    accepted.dedup();

    // denylist 命中 → 直接失败（绝不静默丢弃，避免用户以为加成功了）。
    if !denied.is_empty() {
        return Err(format!(
            "拒绝把公有 API host {} 加入白名单（这会绕过敏感模式）。",
            denied.join(", ")
        ));
    }
    // 有公网域名待确认且未确认 → 不落盘，回报让 UI 二次确认。
    if !needs_confirm.is_empty() && !confirm {
        return Ok(json!({
            "ok": false,
            "needs_confirm": needs_confirm,
            "invalid": invalid,
            "hint": "以下是公网域名，敏感模式默认只允许 localhost / 私网。确认这些是你机构的受控端点后，再带 confirm_public=true 保存。",
        }));
    }

    let dir = config::default_dir();
    let c2 = accepted.clone();
    config::update(&dir, move |c| c.local_endpoint_hosts = c2).map_err(|e| e.to_string())?;
    Ok(json!({
        "ok": true,
        "hosts": accepted,
        "invalid": invalid,
    }))
}

/// 应用当前 config 的 pack 状态到沙箱，并在沙箱在跑时停沙箱让下次一键读到新配置。
fn reapply_packs(
    app: &tauri::AppHandle,
    state: &State<'_, Mutex<AppState>>,
    cfg: &config::Config,
) -> Result<(Vec<String>, Vec<String>, bool), String> {
    let root = asset_root(app).ok_or("找不到 packs/ 资源根")?;
    let sbx_data = sandbox_home().join(".claude-science");
    let (applied, warnings) = packs::apply(&root, &sbx_data, &cfg.enabled_packs, &cfg.pack_env)?;
    let sport = cfg.sandbox_port;
    let restarted = if sandbox_running_ours(sport) {
        let mut st = lock(state);
        let _ = stop_sandbox_inner(app, &mut st);
        true
    } else {
        false
    };
    Ok((applied, warnings, restarted))
}

// ---------- 任务级模型路由（feature 1）----------
