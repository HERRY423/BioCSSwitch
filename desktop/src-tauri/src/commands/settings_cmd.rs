// ---------- 返回体组装（纯函数，便于测试） ----------
/// 组装 get_config 返回体：profiles 的 key 只回掩码，全 key 绝不出后端。
fn build_get_config(dir: &Path) -> Result<serde_json::Value, String> {
    let cfg = config::load_from(dir).map_err(|e| e.to_string())?;
    // 一次性迁移提示（#9 甲）：读出后立即清盘，避免每次 get_config 重复提示。
    let notice = cfg.pending_notice.clone();
    if notice.is_some() {
        config::update(dir, |c| c.pending_notice = None).map_err(|e| e.to_string())?;
    }
    let profiles: Vec<serde_json::Value> = cfg
        .profiles
        .iter()
        .map(|p| {
            json!({
                "id": p.id, "name": p.name, "template_id": p.template_id, "category": p.category,
                "api_format": p.api_format, "base_url": p.base_url, "model": p.model,
                "key": config::mask(&p.api_key), "icon": p.icon, "icon_color": p.icon_color,
                "website_url": p.website_url, "sort_index": p.sort_index, "notes": p.notes,
            })
        })
        .collect();
    Ok(json!({
        "schema_version": cfg.schema_version, "active_id": cfg.active_id, "profiles": profiles,
        "templates": build_list_templates(), "proxy_port": cfg.proxy_port,
        "sandbox_port": cfg.sandbox_port, "mode": cfg.mode, "agent_mode": cfg.agent_mode,
        "pending_notice": notice,
    }))
}

/// 模板注册表交前端铺 UI（单一来源，前端不复制常量）。
fn build_list_templates() -> Vec<serde_json::Value> {
    templates::all()
        .iter()
        .map(|t| {
            json!({
                "id": t.id, "name": t.name, "category": t.category, "api_format": t.api_format,
                "adapter": t.adapter, "base_url": t.base_url, "base_url_editable": t.base_url_editable,
                "requires_model_override": t.requires_model_override,
                "builtin_models": t.builtin_models, "icon": t.icon, "icon_color": t.icon_color,
                "website_url": t.website_url,
            })
        })
        .collect()
}

// ---------- Tauri commands ----------
#[tauri::command]
fn get_config() -> Result<serde_json::Value, String> {
    build_get_config(&config::default_dir())
}

/// 模板注册表交前端铺 UI（新建向导用）。
#[tauri::command]
fn list_templates() -> Vec<serde_json::Value> {
    build_list_templates()
}

/// 切换运行模式（"proxy" 第三方 / "official" 官方）。切官方要先拆第三方链路成功再落盘。
#[tauri::command]
fn set_mode(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    lifecycle: State<'_, lifecycle::Lifecycle>,
    mode: String,
) -> Result<(), String> {
    if mode != "proxy" && mode != "official" {
        return Err(format!("未知模式：{mode}（只支持 proxy / official）。"));
    }
    // 经串行器（修 P1-b）：切官方的「拆链路 + 落盘」必须与「一键开始」等互斥，否则一键起到一半时
    // 切官方会先停链路、一键随后又把沙箱/OAuth 起起来 → 显示官方却有第三方沙箱在跑。bump_generation
    // 作废任何在途启动，防被停后又拿旧配置写回运行态。
    lifecycle.with_serialized(|| {
        let dir = config::default_dir();
        if mode == "official" {
            lifecycle.bump_generation();
            let mut st = lock(&state);
            stop_sandbox_inner(&app, &mut st).map_err(|e| {
                format!("停止沙箱失败，未切换到官方模式：{e}（真实实例 8765 未受影响）")
            })?;
            kill_child(&mut st.proxy);
            st.secret.clear();
            st.provider.clear();
            st.key_fp = 0;
            drop(st);
            // 拆本项目管理的 bio-* MCP：官方模式下用户走真实 Science，我们不留印记。
            if let Some(root) = asset_root(&app) {
                let sbx_data = sandbox_home().join(".claude-science");
                if sbx_data.is_dir() {
                    let _ = packs::purge_bio_from_mcp(&root, &sbx_data);
                }
            }
        }
        config::update(&dir, {
            let mode = mode.clone();
            move |c| c.mode = mode
        })
        .map_err(|e| e.to_string())?;
        Ok(())
    })
}

/// 切换代理层 agent 编排模式。normal 保持旧路径；ultra_* 由 start_proxy_for 注入代理环境。
#[tauri::command]
fn set_agent_mode(
    state: State<'_, Mutex<AppState>>,
    lifecycle: State<'_, lifecycle::Lifecycle>,
    mode: String,
) -> Result<(), String> {
    match mode.as_str() {
        "normal" | "ultra_conservative" | "ultra_deep" => {}
        _ => return Err(format!(
            "未知 agent 模式：{mode}（只支持 normal / ultra_conservative / ultra_deep）。"
        )),
    }
    lifecycle.with_serialized(|| {
        let dir = config::default_dir();
        config::update(&dir, {
            let mode = mode.clone();
            move |c| c.agent_mode = mode
        })
        .map_err(|e| e.to_string())?;
        lifecycle.bump_generation();
        let mut st = lock(&state);
        kill_child(&mut st.proxy);
        st.provider.clear();
        st.key_fp = 0;
        Ok(())
    })
}

#[derive(Deserialize)]
struct UiSettings {
    proxy_port: u16,
    sandbox_port: u16,
}

/// 端口变更是否需要拆掉现有链路（纯函数，P1-c）。代理/沙箱任一端口变了，正在跑的代理就绑在
/// 旧端口、正在跑的沙箱又把旧代理 URL 烘死了，二者与新配置不一致 → 拆掉逼下次「一键开始」按新端口重建。
fn settings_change_needs_teardown(
    old_proxy: u16,
    new_proxy: u16,
    old_sandbox: u16,
    new_sandbox: u16,
) -> bool {
    old_proxy != new_proxy || old_sandbox != new_sandbox
}

/// 端口设置（provider/连接改走 profile CRUD + set_active_profile）。
/// 经串行器（修 P1-c）：端口一旦变化，正在跑的代理绑在旧端口、正在跑的沙箱又烘死了旧代理 URL，
/// 与新端口不一致；此处把这条陈旧链路拆掉（只停我们的沙箱、绝不碰 8765），逼下次「一键开始」按新端口重建，
/// 杜绝「复用旧沙箱指向死端口、UI 却报沿用不变」。
#[tauri::command]
fn set_settings(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    lifecycle: State<'_, lifecycle::Lifecycle>,
    cfg: UiSettings,
) -> Result<(), String> {
    if cfg.proxy_port == 8765 || cfg.sandbox_port == 8765 {
        return Err("端口 8765 是真实 Science 实例保留端口，不能用。".into());
    }
    if cfg.proxy_port == 0 || cfg.sandbox_port == 0 {
        return Err("端口不能为 0。".into());
    }
    if cfg.proxy_port == cfg.sandbox_port {
        return Err("代理端口与沙箱端口不能相同。".into());
    }
    lifecycle.with_serialized(|| {
        let dir = config::default_dir();
        let old = config::load_from(&dir).map_err(|e| e.to_string())?;
        let teardown = settings_change_needs_teardown(
            old.proxy_port,
            cfg.proxy_port,
            old.sandbox_port,
            cfg.sandbox_port,
        );
        // 拆链路【先】于落盘，且停沙箱结果必须据实处理（修增量 P1）：停不掉就【不改端口】——
        // 否则会留下「config 已是新端口、旧沙箱仍在旧端口指向旧代理」的不一致态，下次一键还会复用这条死链路。
        // 保持端口不变则一切仍自洽（旧沙箱指旧代理端口、下次一键在旧端口重建代理，链路照通）。
        if teardown {
            let mut st = lock(&state);
            stop_sandbox_inner(&app, &mut st).map_err(|e| {
                format!(
                    "端口未更改：无法停止指向旧端口的沙箱（{e}），为避免留下失效链路，端口保持不变。请手动停止沙箱或重启 app 后重试。（真实实例 8765 未受影响）"
                )
            })?;
            lifecycle.bump_generation(); // 停成功后作废在途启动
            kill_child(&mut st.proxy);
            st.secret.clear();
            st.provider.clear();
            st.key_fp = 0;
        }
        // 拆链路成功（或无需拆）→ 才落盘新端口，保证 config 与运行态一致。
        config::update(&dir, move |c| {
            c.proxy_port = cfg.proxy_port;
            c.sandbox_port = cfg.sandbox_port;
        })
        .map_err(|e| e.to_string())?;
        Ok(())
    })
}
