/// 非 active 连接编辑的上游校验裁决（纯函数，P2-d）：只有上游【明确】拒绝（Auth 401/403、
/// ModelError 400/404/422）才 Some(hint) 拦下不落盘；Ok / 含糊(429/5xx) / 无响应 → None 照常落盘
/// （best-effort：非 active 没有正在服务的链路可保护，卡在网络抖动上比放行更糟）。
/// 非 active 连接编辑的上游校验裁决（纯函数，P2-d）：
/// - `Ok(true)`  上游明确接受(200)，已校验；
/// - `Ok(false)` 无法确认(429/5xx/无响应)，best-effort 落盘、标记「未校验」（激活时会再验）；
/// - `Err(hint)` 上游明确拒绝(401/403/400/404/422)，拦下不落盘。
///
/// 选「如实标记后保存」：不因网络抖动/上游繁忙挡住保存，但也绝不假称已校验。
fn nonactive_probe_verdict(outcome: &scratch::ProbeOutcome) -> Result<bool, String> {
    match outcome {
        scratch::ProbeOutcome::Ok => Ok(true),
        scratch::ProbeOutcome::Auth(code) => {
            Err(format!("上游拒绝（{code}），key/权限有误，连接未保存。"))
        }
        scratch::ProbeOutcome::ModelError(code) => Err(format!(
            "上游拒绝该模型（{code}），连接未保存。请换一个模型或核对 base_url。"
        )),
        // 无法确认（405/429/5xx/无响应）：落盘但标记未校验，激活时再验。
        // Unsupported(405) 并入此类：save 走 Message 探测，405 罕见（端点/base_url 异常），保守标未校验（与旧行为一致）。
        scratch::ProbeOutcome::Ambiguous(_)
        | scratch::ProbeOutcome::NoResponse
        | scratch::ProbeOutcome::Unsupported(_) => Ok(false),
    }
}

/// 是否对候选连接跑上游 scratch 校验（纯函数，修真机 P1）：空 key → 免（无从验）；非原生且空
/// base_url → 免（relay 必须带 base_url）；原生（deepseek/qwen）即便 base_url 为空也【要】验
/// （用各自硬编码官方端点，坏 key 才能在保存时被拦，不再顺延到激活）。
fn should_scratch_candidate(adapter: &str, key: &str, base_url: &str) -> bool {
    if key.is_empty() {
        return false; // 无 key → 无从验，如实标记未校验。
    }
    if !is_native_adapter(adapter) && base_url.is_empty() {
        return false; // relay 家族缺 base_url → 无从验。
    }
    true
}

/// 保存前守卫（纯函数，修 P2）：relay 家族（非 native）空 base_url 的候选连接不可用——
/// 激活必失败（relay 无硬编码端点可回退）。0.3.1 起内置预设 base_url 可编辑，用户清空后
/// 旧路径会跳过校验、静默落盘并谎报「已保存」。此处在保存时就拦下，绝不落盘。
/// native(deepseek/qwen) 走各自硬编码官方端点，空 base_url 无妨 → 不拦。
fn relay_missing_base_url(adapter: &str, base_url: &str) -> bool {
    !is_native_adapter(adapter) && base_url.trim().is_empty()
}

/// 保存/激活前守卫（纯函数，修 #9 P1-a）：relay 家族（非 native）空（含纯空白）model 不可用——
/// 无 model → launcher 不注入 CSSWITCH_RELAY_MODEL → 无 force → 退回 passthrough → Science 显示 claude。
/// native(deepseek/qwen) 走内置映射/硬编码端点，model 可空 → 不拦。
fn relay_missing_model(adapter: &str, model: &str) -> bool {
    !is_native_adapter(adapter) && model.trim().is_empty()
}

/// 对候选连接做一次上游 scratch 校验（非 active 编辑用，P2-d）。起临时代理探完即杀，
/// **绝不碰 config / AppState / 正在服务的正式代理**。返回是否【已通过上游校验】（供调用方据实措辞）：
/// 空 key / relay 家族空 base_url → `Ok(false)`（无从预检，标记未校验）；
/// native(deepseek/qwen) 即便 base_url 空也【会】走各自官方端点探测（修真机 P1）；
/// 明确接受(200) → `Ok(true)`；明确拒绝 → `Err(hint)`；无法确认 → `Ok(false)`（见 [`nonactive_probe_verdict`]）。
fn scratch_validate_candidate(
    app: &tauri::AppHandle,
    candidate: &config::Profile,
) -> Result<bool, String> {
    let launch = proxy_args_for(candidate);
    if !should_scratch_candidate(&launch.adapter, &launch.key, &launch.base_url) {
        return Ok(false); // 跳过 = 未校验（如实标记）
    }
    let root = asset_root(app).ok_or("找不到代理脚本 proxy/csswitch_proxy.py。")?;
    let py = proc::find_exe("python3").ok_or("缺少依赖 python3（起临时代理需要）。")?;
    let script = root.join("proxy/csswitch_proxy.py");
    let res = scratch::scratch_probe(
        &py,
        &script,
        &scratch::ScratchTarget {
            provider: &launch.adapter,
            key_env: launch.key_env,
            base_url: &launch.base_url,
            key: &launch.key,
            model: Some(&launch.model),
            relay_thinking: launch.thinking_policy,
        },
        probe_kind_for(&launch.adapter, &launch.model),
    );
    nonactive_probe_verdict(&scratch::classify(res.status))
}

#[tauri::command]
fn start_proxy(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    lifecycle: State<'_, lifecycle::Lifecycle>,
) -> Result<serde_json::Value, String> {
    // 经串行器：与切换/连接编辑/清 key/删/停等 ensure_proxy 竞争串行化，防陈旧读起旧配置代理
    // 又写回运行态（修 P1-a，比照 spec §8.1「ensure_proxy 都经一把 app 级 mutex」）。
    lifecycle.with_serialized(|| {
        let (port, _secret, _action) = ensure_proxy(&app, &state, lifecycle.inner())?;
        Ok(json!({ "port": port }))
    })
}

/// 「存 key 即验证」：确保代理在跑，再经代理向上游发一个最小请求，据状态码判断 key 是否可用。
#[tauri::command]
fn verify_key(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    lifecycle: State<'_, lifecycle::Lifecycle>,
) -> Result<serde_json::Value, String> {
    // 经串行器（修 P1-a）：ensure_proxy 与其它生命周期操作不并发交叠。
    lifecycle.with_serialized(|| {
        let (port, secret, _action) = ensure_proxy(&app, &state, lifecycle.inner())?;
        let body = br#"{"model":"claude-opus-4-8","max_tokens":1,"messages":[{"role":"user","content":"ping"}]}"#;
        match proc::http_post_status(port, Some(&secret), "/v1/messages", body, 15000) {
            Some(200) => Ok(json!({ "ok": true, "hint": "key 有效，上游已接受。" })),
            Some(code @ (401 | 403)) => Ok(
                json!({ "ok": false, "hint": format!("上游拒绝（{code}），key 可能无效或无权限。") }),
            ),
            Some(code) => Ok(json!({
                "ok": false,
                "hint": format!("上游返回 {code}，可能是 key 无效、额度不足或上游异常。")
            })),
            None => Err("验证请求无响应（多为网络或上游不通）。".to_string()),
        }
    })
}

#[derive(Deserialize)]
struct FetchModelsReq {
    /// 模板 id（决定 builtin / base_url 可编辑性 / 默认 base_url）。
    template_id: String,
    /// 自定义模板时用户填的 base_url（不可编辑模板忽略）。
    #[serde(default)]
    base_url: String,
    /// 用户新填的 key；为空表示沿用 profile_id 已存的 key（后端不回传完整 key）。
    #[serde(default)]
    key: String,
    /// 编辑已存 profile 时传其 id（用于沿用已存 key）。
    #[serde(default)]
    profile_id: Option<String>,
}

/// live 探测结果（id + 能力）∪ builtin，去重（按 id）+ 排序（true>null>false，主列表 id 微调靠前）。
fn merge_and_sort_models(
    live: Vec<(String, Option<bool>)>,
    builtin: &[&str],
) -> Vec<serde_json::Value> {
    let mut seen = std::collections::BTreeSet::new();
    let mut merged: Vec<(String, Option<bool>)> = Vec::new();
    for (id, st) in live {
        if seen.insert(id.clone()) {
            merged.push((id, st));
        }
    }
    for b in builtin {
        if seen.insert(b.to_string()) {
            merged.push((b.to_string(), None));
        }
    }
    merged.sort_by_key(|(id, st)| {
        let cap = match st {
            Some(true) => 0u8,
            None => 1,
            Some(false) => 2,
        };
        let main = if is_main_list_model(id) { 0u8 } else { 1 };
        (cap, main)
    });
    merged
        .into_iter()
        .map(|(id, st)| json!({ "id": id, "supports_tools": st }))
        .collect()
}

/// 解析探测用 key：新填的优先，否则沿用 profile_id 已存的（后端内部用，绝不回传前端）。
fn resolve_probe_key(profile_id: Option<&str>, candidate: &str) -> Result<String, String> {
    let c = candidate.trim();
    if !c.is_empty() {
        return Ok(c.to_string());
    }
    let pid = profile_id.ok_or("请先填写 API Key / Token。")?;
    let cfg = config::load_from(&config::default_dir()).map_err(|e| e.to_string())?;
    cfg.profile_by_id(pid)
        .map(|p| p.api_key.clone())
        .filter(|k| !k.is_empty())
        .ok_or_else(|| "请先填写 API Key / Token。".to_string())
}

/// 「获取可用模型」——纯 scratch 探测：只用临时代理探候选 base_url/key 的 /v1/models，
/// 绝不写 config、不改 AppState、不碰正在服务 Science 的正式代理。
#[tauri::command]
fn fetch_models(app: tauri::AppHandle, req: FetchModelsReq) -> Result<serde_json::Value, String> {
    let tid = req.template_id.trim();
    let tpl = templates::by_id(tid).ok_or_else(|| format!("未知模板：{tid}"))?;
    let base_url = if tpl.base_url_editable {
        req.base_url.trim().to_string()
    } else {
        tpl.base_url.to_string()
    };
    if base_url.is_empty() || !(base_url.starts_with("http://") || base_url.starts_with("https://"))
    {
        return Err("请先填写 base_url（http:// 或 https:// 开头）。".into());
    }
    reject_openai_custom_anthropic_base(tid, &base_url)?;
    let key = resolve_probe_key(req.profile_id.as_deref(), &req.key)?;
    let root = asset_root(&app).ok_or("找不到代理脚本 proxy/csswitch_proxy.py。")?;
    let py = proc::find_exe("python3").ok_or("缺少依赖 python3（起临时代理需要）。")?;
    let script = root.join("proxy/csswitch_proxy.py");
    let adapter = templates::adapter_for(tid);

    let res = scratch::scratch_probe(
        &py,
        &script,
        &scratch::ScratchTarget {
            provider: adapter,
            key_env: key_env_for_adapter(adapter),
            base_url: &base_url,
            key: &key,
            model: None,
            relay_thinking: tpl.thinking_policy,
        },
        scratch::ProbeKind::Models,
    );
    let builtin = tpl.builtin_models;
    match scratch::classify(res.status) {
        scratch::ProbeOutcome::Ok => {
            let v: serde_json::Value =
                serde_json::from_str(&res.body).map_err(|e| format!("解析模型列表失败：{e}"))?;
            let live: Vec<(String, Option<bool>)> = v
                .get("data")
                .and_then(|d| d.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|m| {
                            let id = m.get("id")?.as_str()?.to_string();
                            let st = m.get("supports_tools").and_then(|b| b.as_bool());
                            Some((id, st))
                        })
                        .collect()
                })
                .unwrap_or_default();
            if live.is_empty() {
                return Ok(json!({
                    "models": merge_and_sort_models(vec![], builtin),
                    "source": "builtin", "error_kind": null, "upstream_status": 200
                }));
            }
            Ok(json!({
                "models": merge_and_sort_models(live, builtin),
                "source": "live", "error_kind": null, "upstream_status": 200
            }))
        }
        scratch::ProbeOutcome::Auth(code) => {
            Err(format!("上游拒绝（{code}），key 或权限可能有误。"))
        }
        // 非 200 且非 Auth：一律 builtin 兜底，但按语义分「发现不支持」(4xx) 与「网络/上游临时」(5xx/429/无响应)，
        // 供前端区分提示（spec v3 §3.4.3）。绝不把 Auth 混进来掩盖坏 key。
        other => {
            let source = scratch::discovery_fallback_source(&other);
            let error_kind = if source == "network" {
                json!("network")
            } else {
                json!(null)
            };
            Ok(json!({
                "models": merge_and_sort_models(vec![], builtin),
                "source": source,
                "error_kind": error_kind,
                "upstream_status": res.status
            }))
        }
    }
}

/// 探测类型选择（纯函数，修真机 P1）：
/// - 原生 adapter（deepseek/qwen）的 `/v1/models` 是【静态列表、不回源】，探不出坏 key，故一律用
///   Message 探测（打 `/v1/messages` 会真发上游，坏 key → 401）。
/// - relay：留空用 Models（`/v1/models` 回源即可验端点+鉴权）；选了具体模型用 Message 验该模型。
fn probe_kind_for(adapter: &str, model: &str) -> scratch::ProbeKind {
    if is_native_adapter(adapter) {
        return scratch::ProbeKind::Message; // native /v1/models 静态，只有 Message 打上游能验 key。
    }
    probe_kind_for_model(model)
}

/// 选了模型 → 验具体模型（POST /v1/messages）；留空 → 验端点+鉴权（GET /v1/models）。
fn probe_kind_for_model(model: &str) -> scratch::ProbeKind {
    if model.trim().is_empty() {
        scratch::ProbeKind::Models
    } else {
        scratch::ProbeKind::Message
    }
}
