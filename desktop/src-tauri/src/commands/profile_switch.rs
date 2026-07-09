/// active 连接编辑的内存候选值（validate-before-persist 用）：不改的字段为 None。
/// 校验时把它套到旧 profile 的克隆上做 scratch/起正式；提交成功时用**同一套** [`ConnectionEdit::apply`]
/// 逻辑连同 active_id 一起落盘，杜绝「先落盘后校验」导致的「盘新运行旧」（P1-4）。
#[derive(Default)]
struct ConnectionEdit {
    base_url: Option<String>,
    api_format: Option<String>,
    model: Option<String>,
    key: Option<String>,
}

impl ConnectionEdit {
    /// 把非空编辑值套到目标 profile（内存候选与落盘共用同一逻辑）。
    /// 语义与 `update_profile_connection_inner` 一致：None=不改；key 为空串=不改（留占位不覆盖已存 key）。
    fn apply(&self, p: &mut config::Profile) {
        if let Some(u) = &self.base_url {
            p.base_url = u.clone();
        }
        if let Some(f) = &self.api_format {
            p.api_format = f.clone();
        }
        if let Some(m) = &self.model {
            p.model = m.clone();
        }
        if let Some(k) = &self.key {
            if !k.is_empty() {
                p.api_key = k.clone();
            }
        }
    }
}

/// 激活/切换是否跳过 scratch 上游校验（纯函数，修真机 P1）：只有用户显式 `skip_verify` 才跳；
/// 原生 adapter 不再豁免（旧行为 `native || skip_verify` 会让原生无效 key 提交为 active 并谎报「已切到」，
/// 首个真实推理才 401）。`native` 参数刻意保留：记录它曾是豁免条件、现已作废。
fn skip_scratch_verify(native: bool, skip_verify: bool) -> bool {
    let _ = native; // native 曾是豁免条件，现已作废（保留参数以固化回归防线）。
    skip_verify
}

/// 切换事务本体（spec §7）：scratch 校验候选 → 起正式代理探活 → 探活健康【才】提交 active_id；
/// 任一步失败杀候选 + 恢复旧代理，`active_id` 不动，**不停沙箱**（path-secret 持久，端口+secret
/// 不变，沙箱链路不断，停沙箱只会扩大失败面）。**本函数不取串行器锁**（调用方命令已持有）。
fn set_active_profile_txn(
    app: &tauri::AppHandle,
    state: &State<'_, Mutex<AppState>>,
    lifecycle: &lifecycle::Lifecycle,
    id: &str,
    skip_verify: bool,
    conn_edit: Option<&ConnectionEdit>,
) -> Result<serde_json::Value, String> {
    let dir = config::default_dir();
    let cfg = config::load_from(&dir).map_err(|e| e.to_string())?;
    let mut candidate = cfg
        .profile_by_id(id)
        .cloned()
        .ok_or_else(|| format!("找不到 profile：{id}"))?;
    // active 连接编辑：把新连接字段套到【内存候选】做校验（validate-before-persist）——
    // 磁盘此刻仍是旧连接；只有探活健康提交时才落盘（见下方 Commit 分支）。
    if let Some(edit) = conn_edit {
        edit.apply(&mut candidate);
    }
    reject_openai_custom_anthropic_base(&candidate.template_id, &candidate.base_url)?;
    let is_edit = conn_edit.is_some();
    // 失败措辞：连接编辑说「未保存/仍在用原配置运行」，普通切换说「未切换/当前配置不变」。
    let (verb, tail): (&str, &str) = if is_edit {
        ("未保存", "仍在用原配置运行")
    } else {
        ("未切换", "当前配置不变")
    };
    assert_format_supported(&candidate)?;
    let launch = proxy_args_for(&candidate);
    if launch.key.is_empty() {
        return Err(format!("「{}」还没填 API key，请先填写。", candidate.name));
    }
    let native = is_native_adapter(&launch.adapter);
    if !native && launch.base_url.is_empty() {
        return Err("该配置需要填 base_url（http:// 或 https:// 开头）。".into());
    }
    // 守卫（修 #9 P1-a）：relay/自定义端点空 model 无法激活（无 force → 退回 passthrough 显示 claude）。
    if relay_missing_model(&launch.adapter, &candidate.model) {
        return Err(
            "该配置需要选择或填写一个模型（中转/自定义端点必填），请在连接编辑里补上。".into(),
        );
    }
    // 快照旧 active（回滚锚点）：旧 profile 仍在盘上未动、active_id 未改，恢复据它重起旧代理。
    let old_active = cfg.active_id.clone();

    // 1) scratch 校验候选（临时端口+secret+候选 key，避开 8765；绝不碰正式链路）。
    //    所有 adapter 都预检：native(deepseek/qwen) 用各自官方端点 + Message 探测（其 /v1/models 静态，
    //    探不出坏 key）；只有用户显式 skip_verify 才跳过（修真机 P1：原生免校验会让无效 key 提交为
    //    active 并谎报「已切到」，首个真实推理才 401）。分类失败保留结构化提示（committed:false/can_skip）。
    let scratch_ok = if skip_scratch_verify(native, skip_verify) {
        true
    } else {
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
        match scratch::classify(res.status) {
            scratch::ProbeOutcome::Ok => true,
            scratch::ProbeOutcome::Auth(code) => {
                return Ok(json!({ "committed": false,
                    "hint": format!("上游拒绝（{code}），key/权限有误，{verb}（{tail}）。") }));
            }
            scratch::ProbeOutcome::ModelError(code) => {
                return Ok(json!({ "committed": false,
                    "hint": format!("上游拒绝该模型（{code}），{verb}。请换一个模型或核对 base_url。") }));
            }
            scratch::ProbeOutcome::Ambiguous(_)
            | scratch::ProbeOutcome::NoResponse
            | scratch::ProbeOutcome::Unsupported(_) => {
                return Ok(json!({ "committed": false, "can_skip": true,
                    "hint": format!("无法确认（网络/上游繁忙），{verb}。可重试，或用「跳过验证」。") }));
            }
        }
    };

    // 2/3) 用候选起【正式代理】并探活。bump_generation 使并发中的旧启动（如同时的 verify_key）作废。
    lifecycle.bump_generation();
    let real_healthy = scratch_ok && start_proxy_for(app, state, lifecycle, &candidate).is_ok();

    match decide_switch(scratch_ok, real_healthy) {
        SwitchOutcome::Commit => {
            // 探活健康【才】落盘：连接编辑连同 active_id 一起提交（validate-before-persist），
            // 盘上与运行态一致，杜绝「盘新运行旧」。
            if is_edit {
                config::write_rolling_backup(&dir).ok(); // 覆盖连接前留底（仅编辑路径需要）
            }
            if let Err(e) = config::update(&dir, |c| {
                c.active_id = id.to_string();
                if let Some(edit) = conn_edit {
                    if let Some(p) = c.profile_by_id_mut(id) {
                        edit.apply(p);
                    }
                }
            }) {
                // spec §7 步 5：config 提交失败也要回滚进程——正式代理已起，若不回滚就成「运行新/盘旧」。
                // 恢复旧 active 代理，active_id 仍为旧值，用户可重试。
                let restored = restore_proxy_for_active(app, state, lifecycle, &cfg, &old_active);
                return Err(format!(
                    "校验通过、代理已起，但写盘失败（{e}），{}。请检查磁盘空间/权限后重试。",
                    rollback_status_clause(restored)
                ));
            }
            let hint = if is_edit {
                format!("已保存并应用「{}」的新连接。", candidate.name)
            } else {
                format!("已切到「{}」。", candidate.name)
            };
            Ok(json!({ "committed": true, "active_id": id, "hint": hint }))
        }
        SwitchOutcome::RollbackToOld => {
            // 候选正式代理起/探活失败：恢复旧代理，active_id 不动，连接不落盘，不停沙箱。
            let restored = restore_proxy_for_active(app, state, lifecycle, &cfg, &old_active);
            let clause = rollback_status_clause(restored);
            if is_edit {
                Err(format!(
                    "连接已校验通过，但正式代理启动/探活失败，连接未保存，{clause}。"
                ))
            } else {
                Err(format!(
                    "候选配置校验通过，但正式代理启动/探活失败，{clause}。"
                ))
            }
        }
        SwitchOutcome::AbortBeforeStart => {
            // scratch 校验未过；旧态零改动、连接不落盘。（明确拒绝/含糊态在上面已 committed:false 早返，
            // 此分支是 scratch_ok=false 的兜底措辞。）
            if is_edit {
                Err("连接上游校验失败（key/base_url/网络？），连接未保存。".into())
            } else {
                Err("候选上游校验失败（key/base_url/网络？），未切换。".into())
            }
        }
    }
}

/// 回滚结果措辞（纯函数，P2-e）：restored=true 才说「已回滚到原配置」；恢复失败必须如实说明代理已停，
/// 绝不谎称回滚成功（比照本项目「如实报告」铁律，掩盖代理已停会误导用户）。
fn rollback_status_clause(restored: bool) -> &'static str {
    if restored {
        "已回滚到原配置（沙箱未受影响）"
    } else {
        "回滚未成功：代理当前已停，请重试或手动「一键开始」（沙箱未受影响）"
    }
}

/// 切换失败回滚：按【旧 active】重起旧代理（旧 profile 仍在盘上）；best-effort，失败则代理暂停、
/// active_id 仍为旧值，用户可重试。旧 active 为空（此前未配置生效）→ 不复活，保持代理停着。
/// 返回是否已把旧代理恢复到位（供调用方据实措辞，修 P2-e）。
fn restore_proxy_for_active(
    app: &tauri::AppHandle,
    state: &State<'_, Mutex<AppState>>,
    lifecycle: &lifecycle::Lifecycle,
    cfg: &config::Config,
    old_active: &str,
) -> bool {
    if old_active.is_empty() {
        return true; // 此前无生效配置 → 本就无代理可恢复，状态与切换前一致
    }
    match cfg.profile_by_id(old_active) {
        Some(old) => {
            lifecycle.bump_generation();
            start_proxy_for(app, state, lifecycle, old).is_ok()
        }
        None => false, // 旧 active 指向已不存在的 profile（罕见）→ 无法恢复，代理已停
    }
}
