/// 内置任务清单 → 前端。
#[tauri::command]
fn list_biomed_tasks() -> serde_json::Value {
    let arr: Vec<serde_json::Value> = packs::BIOMED_TASKS
        .iter()
        .map(|(id, label, hint)| json!({"id": id, "label": label, "hint": hint}))
        .collect();
    let cfg = config::load_from(&config::default_dir()).unwrap_or_default();
    let mut probe_map = serde_json::Map::new();
    for (k, v) in &cfg.probe_results {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(v) {
            probe_map.insert(k.clone(), val);
        }
    }
    json!({
        "tasks": arr,
        "routes": cfg.task_routes,
        "active_id": cfg.active_id,
        "probes": probe_map,
    })
}

/// 设置某任务的默认 profile。profile_id 为空 → 走 active_id。
#[tauri::command]
fn set_task_route(task: String, profile_id: String) -> Result<(), String> {
    // 校验 task
    if !packs::BIOMED_TASKS.iter().any(|(id, _, _)| *id == task) {
        return Err(format!("未知任务：{task}"));
    }
    let dir = config::default_dir();
    // 校验 profile_id（空 = 清除）
    if !profile_id.is_empty() {
        let cfg = config::load_from(&dir).map_err(|e| e.to_string())?;
        if cfg.profile_by_id(&profile_id).is_none() {
            return Err(format!("未知 profile：{profile_id}"));
        }
    }
    config::update(&dir, move |c| {
        if profile_id.is_empty() {
            c.task_routes.remove(&task);
        } else {
            c.task_routes.insert(task, profile_id);
        }
    })
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// 探针类型（本项目在 v0.3 时代已明确工具调用 / 长上下文 / JSON 稳定性是三大风险）。
#[derive(Deserialize)]
struct RunProbeReq {
    profile_id: String,
    /// tool_use / long_ctx / json_stable —— 前端可任选一批
    probes: Vec<String>,
}

/// 用代理起最小请求，评估三类能力。
///   - tool_use  : 带 tools + tool_choice=tool，看是否回 tool_use block
///   - long_ctx  : 32 KiB payload，看是否 200 / 400
///   - json_stable: 要求严格 JSON schema，看输出是否可解析
/// 结果写回 config.probe_results（key `<profile_id>:<probe>`）。
///
/// phase-2：**支持任意 profile**——active 走现有代理；非 active 走 `scratch::scratch_probe`
/// 起临时代理，探完杀净（复用 v0.3 的 nonactive-probe 内核）。
fn response_has_tool_use_block(body: &str) -> bool {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(body) else {
        return false;
    };
    v.get("content")
        .and_then(|c| c.as_array())
        .map(|blocks| {
            blocks
                .iter()
                .any(|b| b.get("type").and_then(|t| t.as_str()) == Some("tool_use"))
        })
        .unwrap_or(false)
}

fn response_text_blocks(body: &str) -> String {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(body) else {
        return String::new();
    };
    v.get("content")
        .and_then(|c| c.as_array())
        .map(|blocks| {
            blocks
                .iter()
                .filter(|b| b.get("type").and_then(|t| t.as_str()) == Some("text"))
                .filter_map(|b| b.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join("")
        })
        .unwrap_or_default()
}

fn response_has_probe_json(body: &str) -> bool {
    let text = response_text_blocks(body);
    let candidate = text.trim();
    let Ok(v) = serde_json::from_str::<serde_json::Value>(candidate) else {
        return false;
    };
    v.get("pmid").and_then(|p| p.as_str()) == Some("12345678")
        && v.get("year").and_then(|y| y.as_i64()) == Some(2024)
}

#[tauri::command]
fn run_probes(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    req: RunProbeReq,
) -> Result<serde_json::Value, String> {
    let dir = config::default_dir();
    let cfg_snapshot = config::load_from(&dir).map_err(|e| e.to_string())?;
    let profile = cfg_snapshot
        .profile_by_id(&req.profile_id)
        .cloned()
        .ok_or_else(|| format!("未知 profile：{}", req.profile_id))?;
    let is_active = cfg_snapshot.active_id == req.profile_id;

    // Active：起主代理；Non-active：为每个 probe 起临时代理（复用 scratch）。
    let (pport, secret, _action) = if is_active {
        let lifecycle = app.state::<lifecycle::Lifecycle>();
        ensure_proxy(&app, &state, &lifecycle)?
    } else {
        // 非 active 用 scratch 直接跑；不需要主代理。用一个假 (port, secret) 占位；下面
        // 每个 probe 分支自己起临时代理。
        (0u16, String::new(), ProxyAction::Reused)
    };

    let mut results = serde_json::Map::new();
    for probe in &req.probes {
        let (payload, verdict_fn): (&[u8], fn(u16, &str) -> (&'static str, &'static str)) =
            match probe.as_str() {
                "tool_use" => (
                    br#"{"model":"claude-opus-4-8","max_tokens":256,"tools":[{"name":"echo","description":"echo back","input_schema":{"type":"object","properties":{"msg":{"type":"string"}},"required":["msg"]}}],"tool_choice":{"type":"tool","name":"echo"},"messages":[{"role":"user","content":"call echo with msg='ok'"}]}"# as &[u8],
                    |code: u16, body: &str| -> (&'static str, &'static str) {
                        if code != 200 { return ("fail", "上游未返回 200 / 拒绝工具调用"); }
                        if response_has_tool_use_block(body) {
                            ("ok", "返回了 tool_use block")
                        } else {
                            ("degraded", "200 但未见 tool_use；可能被降级为文本或 DSML 泄漏")
                        }
                    },
                ),
                "long_ctx" => {
                    // 32k 字符（约 8k tokens）——比大部分小模型的 cap 高一档，验证是否被截或 400。
                    static PAYLOAD: std::sync::OnceLock<Vec<u8>> = std::sync::OnceLock::new();
                    let bytes: &Vec<u8> = PAYLOAD.get_or_init(|| {
                        let fill = "生物医学文献综述测试文本；".repeat(2000);
                        format!(
                            "{{\"model\":\"claude-opus-4-8\",\"max_tokens\":32,\"messages\":[{{\"role\":\"user\",\"content\":\"以下是长上下文测试：{fill}\\n请回复 OK。\"}}]}}"
                        )
                        .into_bytes()
                    });
                    (
                        bytes.as_slice(),
                        |code: u16, _body: &str| -> (&'static str, &'static str) {
                            if code == 200 { ("ok", "长上下文承接正常") }
                            else if code == 400 { ("fail", "上游拒绝长上下文（400，超 cap）") }
                            else { ("degraded", "长上下文异常状态") }
                        },
                    )
                }
                "json_stable" => (
                    br#"{"model":"claude-opus-4-8","max_tokens":128,"messages":[{"role":"user","content":"return ONLY compact JSON: {\"pmid\":\"12345678\",\"year\":2024}. no prose."}]}"# as &[u8],
                    |code: u16, body: &str| -> (&'static str, &'static str) {
                        if code != 200 { return ("fail", "上游未返回 200"); }
                        // 简单启发：body 里是否含合法 JSON 对象
                        if response_has_probe_json(body) {
                            ("ok", "返回可解析 JSON 骨架")
                        } else {
                            ("degraded", "200 但输出偏散文；JSON 稳定性弱")
                        }
                    },
                ),
                _ => {
                    results.insert(probe.clone(), json!({"verdict": "skip", "reason": "未知探针"}));
                    continue;
                }
            };
        let (code_opt, body) = if is_active {
            proc::http_post_status_body(pport, Some(&secret), "/v1/messages", payload, 30_000)
                .unwrap_or((None, String::new()))
        } else {
            // 非 active：起临时代理（scratch），发同样 payload；探完杀净。
            let root = asset_root(&app).ok_or("找不到 proxy/csswitch_proxy.py")?;
            let py = proc::find_exe("python3").ok_or("缺 python3")?;
            let script = root.join("proxy/csswitch_proxy.py");
            let adapter = templates::adapter_for(&profile.template_id);
            let key_env = key_env_for_adapter(adapter);
            let target = scratch::ScratchTarget {
                provider: adapter,
                key_env,
                base_url: &profile.base_url,
                key: &profile.api_key,
                model: if profile.model.is_empty() { None } else { Some(profile.model.as_str()) },
            };
            let r = scratch::scratch_probe(
                &py, &script, &target,
                scratch::ProbeKind::CustomPost(payload.to_vec(), true),
            );
            (r.status, r.body)
        };
        let code = code_opt.unwrap_or(0);
        let (verdict, reason) = verdict_fn(code, &body);
        let now_ms = config::now_ms();
        let entry = json!({
            "verdict": verdict,
            "reason": reason,
            "upstream_status": code,
            "ts": now_ms,
        });
        // 写回 config.probe_results
        let key = format!("{}:{}", req.profile_id, probe);
        let val = entry.to_string();
        let _ = config::update(&dir, |c| {
            c.probe_results.insert(key, val);
        });
        results.insert(probe.clone(), entry);
    }
    Ok(json!({"ok": true, "results": results}))
}

// ---------- phase-1 路径验证命令 ----------
//
// smoke test 流程：
//   1) `start_smoke_verification` 把 canary MCP + Skill 写进沙箱、返回 marker。
//      UI 提示用户："请手动重启沙箱（全部停止 → 一键开始），再回来点 poll。"
//      我们**不代重启**，因为用户可能有正在进行的对话。
//   2) `poll_smoke_verification` 读 `~/.csswitch/smoke/latest.json` 判断 marker 是否到位。
//   3) `confirm_skill_verified` 让用户手动确认：在 Science 里发触发词，看到 canary-ok 就点。
//   4) `cleanup_smoke_verification` 拆掉 canary，恢复干净状态。
