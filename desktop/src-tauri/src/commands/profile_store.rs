// ---------- profile CRUD 纯实现（*_inner，便于用临时 dir 单测） ----------
fn create_profile_inner(
    dir: &Path,
    template_id: &str,
    name: &str,
    key: Option<&str>,
    base_url_override: Option<&str>,
    model: Option<&str>,
) -> Result<String, String> {
    let tpl = templates::by_id(template_id).ok_or_else(|| format!("未知模板：{template_id}"))?;
    let id = config::new_id();
    let base_url = base_url_override
        .map(str::to_string)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| tpl.base_url.to_string());
    reject_openai_custom_anthropic_base(template_id, &base_url)?;
    let p = config::Profile {
        id: id.clone(),
        name: name.to_string(),
        template_id: template_id.to_string(),
        category: tpl.category.to_string(),
        api_format: tpl.api_format.to_string(),
        base_url,
        api_key: key.unwrap_or("").to_string(),
        model: model.unwrap_or("").to_string(),
        website_url: Some(tpl.website_url.to_string()),
        icon: Some(tpl.icon.to_string()),
        icon_color: Some(tpl.icon_color.to_string()),
        sort_index: Some(config::now_ms()),
        created_at: Some(config::now_ms()),
        notes: None,
    };
    assert_format_supported(&p)?; // custom 选了不支持格式则拒
                                  // 守卫（修 #9 P1-a）：relay/自定义端点必须带 model（force 前提）。
    if relay_missing_model(tpl.adapter, &p.model) {
        return Err("中转 / 自定义端点必须选择或填写一个模型，未创建。".to_string());
    }
    config::update(dir, |c| c.profiles.push(p)).map_err(|e| e.to_string())?;
    Ok(id)
}

fn update_profile_metadata_inner(
    dir: &Path,
    id: &str,
    name: &str,
    notes: Option<&str>,
) -> Result<(), String> {
    // 未命中 id → Err（不静默 Ok，修 MP-1 Minor [4]）。
    if config::load_from(dir)
        .map_err(|e| e.to_string())?
        .profile_by_id(id)
        .is_none()
    {
        return Err(format!("找不到 profile：{id}"));
    }
    config::update(dir, |c| {
        if let Some(p) = c.profile_by_id_mut(id) {
            p.name = name.to_string();
            p.notes = notes.map(str::to_string);
        }
    })
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn clear_profile_key_inner(dir: &Path, id: &str) -> Result<(), String> {
    config::update(dir, |c| {
        if let Some(p) = c.profile_by_id_mut(id) {
            p.api_key.clear();
        }
    })
    .map_err(|e| e.to_string())?;
    config::drop_rolling_backup(dir); // 清 key 后净化滚动备份，旧明文不可从 .bak 恢复
    Ok(())
}

fn delete_profile_inner(dir: &Path, id: &str) -> Result<(), String> {
    config::update(dir, |c| {
        c.profiles.retain(|p| p.id != id);
        if c.active_id == id {
            c.active_id.clear(); // 删 active → 置空
        }
    })
    .map_err(|e| e.to_string())?;
    config::drop_rolling_backup(dir);
    Ok(())
}

fn update_profile_connection_inner(
    dir: &Path,
    id: &str,
    base_url: Option<&str>,
    api_format: Option<&str>,
    model: Option<&str>,
    key: Option<&str>,
) -> Result<(), String> {
    if let Some(fmt) = api_format {
        let probe = config::Profile {
            api_format: fmt.to_string(),
            ..Default::default()
        };
        assert_format_supported(&probe)?;
    }
    // 未命中 id → Err（不静默 Ok，修 MP-1 Minor [4]）。
    if config::load_from(dir)
        .map_err(|e| e.to_string())?
        .profile_by_id(id)
        .is_none()
    {
        return Err(format!("找不到 profile：{id}"));
    }
    config::write_rolling_backup(dir).ok(); // 覆盖前留底
    config::update(dir, |c| {
        if let Some(p) = c.profile_by_id_mut(id) {
            if let Some(u) = base_url {
                p.base_url = u.to_string();
            }
            if let Some(f) = api_format {
                p.api_format = f.to_string();
            }
            if let Some(m) = model {
                p.model = m.to_string();
            }
            if let Some(k) = key {
                if !k.is_empty() {
                    p.api_key = k.to_string(); // 空=不改（留占位不覆盖已存 key）
                }
            }
        }
    })
    .map_err(|e| e.to_string())?;
    Ok(())
}
