#[tauri::command]
fn list_packs(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let root = asset_root(&app).ok_or("找不到 packs/ 资源根")?;
    let all = packs::list_packs(&root);
    let cfg = config::load_from(&config::default_dir()).unwrap_or_default();
    let mut env_masked = serde_json::Map::new();
    for (k, v) in &cfg.pack_env {
        env_masked.insert(k.clone(), serde_json::Value::Bool(!v.is_empty()));
    }
    let dependency_status = packs::dependency_status(&all, &cfg.enabled_packs);
    let current_upstream = cfg
        .active_profile()
        .map(|p| upstream_host(&templates::adapter_for(&p.template_id), &p.base_url))
        .unwrap_or_default();
    Ok(json!({
        "packs": all,
        "enabled": cfg.enabled_packs,
        "dependency_status": dependency_status,
        "env_set": env_masked,
        "mode": cfg.mode,
        "sensitive_mode": cfg.sensitive_mode,
        "local_endpoint_hosts": cfg.local_endpoint_hosts,
        "current_upstream_host": current_upstream,
        "verification": verification_summary(&cfg),
    }))
}

#[tauri::command]
fn toggle_pack(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    id: String,
    enabled: bool,
) -> Result<serde_json::Value, String> {
    let dir = config::default_dir();
    let cfg = config::update(&dir, {
        let id = id.clone();
        move |c| {
            c.enabled_packs.insert(id, enabled);
        }
    })
    .map_err(|e| e.to_string())?;
    if cfg.mode == "official" {
        return Ok(json!({
            "ok": true, "applied": [], "warnings": [],
            "note": "已保存；官方模式下 pack 不装配，切回第三方模式即可生效。",
        }));
    }
    let (applied, warnings, sandbox_restarted) = reapply_packs(&app, &state, &cfg)?;
    Ok(json!({
        "ok": true, "applied": applied,
        "warnings": warnings, "sandbox_restarted": sandbox_restarted,
    }))
}

#[tauri::command]
fn set_pack_env(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    name: String,
    value: String,
) -> Result<serde_json::Value, String> {
    let dir = config::default_dir();
    let cfg = config::update(&dir, {
        let (n, v) = (name.clone(), value.clone());
        move |c| {
            if v.is_empty() {
                c.pack_env.remove(&n);
            } else {
                c.pack_env.insert(n, v);
            }
        }
    })
    .map_err(|e| e.to_string())?;
    if cfg.mode == "official" {
        return Ok(json!({
            "ok": true,
            "set": !value.is_empty(),
            "applied": [],
            "warnings": [],
            "note": "已保存；官方模式下 pack 不装配，切回第三方模式即可生效。",
        }));
    }
    let (applied, warnings, sandbox_restarted) = reapply_packs(&app, &state, &cfg)
        .map_err(|e| format!("pack 环境变量已保存，但重新装配失败：{e}"))?;
    Ok(json!({
        "ok": true,
        "set": !value.is_empty(),
        "applied": applied,
        "warnings": warnings,
        "sandbox_restarted": sandbox_restarted,
    }))
}
