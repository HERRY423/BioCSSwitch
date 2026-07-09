/// 官方模式：干净地打开用户【真实】的 Claude Science（不碰/复制真实凭证，抹掉 ANTHROPIC_*）。
#[tauri::command]
fn open_official() -> Result<(), String> {
    let app_path = "/Applications/Claude Science.app";
    let mut cmd = Command::new("open");
    if Path::new(app_path).is_dir() {
        cmd.arg(app_path);
    } else {
        cmd.arg("-a").arg("Claude Science");
    }
    cmd.env_remove("ANTHROPIC_BASE_URL")
        .env_remove("ANTHROPIC_API_KEY")
        .env_remove("ANTHROPIC_AUTH_TOKEN");
    match cmd.status() {
        Ok(s) if s.success() => Ok(()),
        Ok(_) => Err("未能打开 Claude Science。请确认已安装官方 Claude Science。".into()),
        Err(e) => Err(format!("打开官方 Claude Science 失败：{e}")),
    }
}

#[tauri::command]
fn stop_all(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    lifecycle: State<'_, lifecycle::Lifecycle>,
) -> Result<(), String> {
    lifecycle.with_serialized(|| {
        lifecycle.bump_generation(); // 作废任何在途启动（防被停后又拿旧 key 复活）
        let mut st = lock(&state);
        let sandbox_res = stop_sandbox_inner(&app, &mut st);
        kill_child(&mut st.proxy);
        st.secret.clear();
        st.provider.clear();
        st.key_fp = 0;
        sandbox_res.map_err(|e| format!("代理已停；但{e}真实实例 8765 未受影响。"))
    })
}

#[tauri::command]
fn one_click_login(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    lifecycle: State<'_, lifecycle::Lifecycle>,
) -> Result<serde_json::Value, String> {
    lifecycle.with_serialized(|| one_click_login_inner(app, state, lifecycle.inner()))
}

#[tauri::command]
fn status(state: State<'_, Mutex<AppState>>) -> serde_json::Value {
    // 只在锁内取值，锁外做阻塞探活。
    let (pport, secret, sport, adapter, base_url) = {
        let st = lock(&state);
        let cfg = config::load_from(&config::default_dir()).unwrap_or_default();
        let pport = if st.proxy_port != 0 {
            st.proxy_port
        } else {
            cfg.proxy_port
        };
        let sport = if st.sandbox_port != 0 {
            st.sandbox_port
        } else {
            cfg.sandbox_port
        };
        // 上游灯读生效 profile 的 adapter/base_url；无生效配置 → 空（灯显黄，不误探）。
        let (adapter, base_url) = match cfg.active_profile() {
            Some(p) => (
                templates::adapter_for(&p.template_id).to_string(),
                p.base_url.clone(),
            ),
            None => (String::new(), String::new()),
        };
        (pport, st.secret.clone(), sport, adapter, base_url)
    };
    let proxy = if !secret.is_empty() && proc::http_health(pport, Some(&secret), 300) {
        "green"
    } else {
        "amber"
    };
    let sandbox = if sandbox_running_ours(sport) {
        "green"
    } else {
        "amber"
    };
    let uhost = upstream_host(&adapter, &base_url);
    let upstream = if !uhost.is_empty() && proc::tcp_reachable(&uhost, 443, 500) {
        "green"
    } else {
        "amber"
    };
    json!({ "proxy": proxy, "sandbox": sandbox, "upstream": upstream })
}

#[tauri::command]
fn open_url(state: State<'_, Mutex<AppState>>) -> Result<(), String> {
    let url = { lock(&state).sandbox_url.clone() };
    let url = url.ok_or("还没有沙箱 URL，请先「一键开始」。")?;
    open_in_browser(&url)
}

#[tauri::command]
fn run_doctor(app: tauri::AppHandle) -> Result<String, String> {
    let root = asset_root(&app).ok_or("找不到 scripts/doctor.sh（打包资源或仓库根均未命中）。")?;
    let cfg = config::load_from(&config::default_dir()).unwrap_or_default();
    let doctor = root.join("scripts/doctor.sh");
    // 生效 profile 的展示名（template_id）+ adapter + 有无 key；无生效配置则留空。
    let (provider_label, adapter, has_key) = match cfg.active_profile() {
        Some(p) => (
            p.template_id.clone(),
            templates::adapter_for(&p.template_id),
            !p.api_key.is_empty(),
        ),
        None => (String::new(), "", false),
    };
    let mut cmd = Command::new("bash");
    // 多 profile：传 template_id + adapter + key 有无（布尔）。doctor 不再按 provider 名写死、
    // 不再去 shell 环境找 key（key 存 config.json）。绝不把真实 key 值传进其环境。
    cmd.arg(&doctor)
        .env("CSSWITCH_PROVIDER", &provider_label)
        .env("CSSWITCH_ADAPTER", adapter)
        .env("CSSWITCH_KEY_PRESENT", if has_key { "1" } else { "0" })
        .env("CSSWITCH_PROXY_PORT", cfg.proxy_port.to_string())
        .env("CSSWITCH_SANDBOX_PORT", cfg.sandbox_port.to_string());
    let out = cmd.output().map_err(|e| e.to_string())?;
    let mut text = String::from_utf8_lossy(&out.stdout).to_string();
    let err = String::from_utf8_lossy(&out.stderr);
    if !err.trim().is_empty() {
        text.push_str("\n[stderr] ");
        text.push_str(err.trim());
    }
    Ok(text)
}

/// 当前 app 版本（供前端「检查更新」与页脚版本号用）。
#[tauri::command]
fn app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}
