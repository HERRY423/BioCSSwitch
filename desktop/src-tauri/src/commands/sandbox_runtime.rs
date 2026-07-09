/// 停沙箱。返回 Err 表示 stop 脚本非零退出（Science 可能没停干净），调用方据此如实报告。
fn stop_sandbox_inner(app: &tauri::AppHandle, st: &mut AppState) -> Result<(), String> {
    let mut err = None;
    match asset_root(app) {
        Some(root) => {
            let stop = root.join("scripts/stop-science-sandbox.sh");
            if stop.is_file() {
                match Command::new("zsh")
                    .arg(&stop)
                    .env("SANDBOX_HOME", sandbox_home())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                {
                    Ok(s) if s.success() => {}
                    Ok(s) => err = Some(format!("停止沙箱脚本非零退出（{:?}）。", s.code())),
                    Err(e) => err = Some(format!("调用停止沙箱脚本失败：{e}")),
                }
            } else {
                err = Some(format!(
                    "找不到停止脚本 {}，无法确认沙箱已停止（沙箱可能仍在运行）。",
                    stop.display()
                ));
            }
        }
        None => {
            err = Some(
                "定位不到资源根，取不到停止脚本，无法确认沙箱已停止（沙箱可能仍在运行）。"
                    .to_string(),
            );
        }
    }
    kill_child(&mut st.sandbox);
    st.sandbox_url = None;
    match err {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

/// 一键开始本体（经串行器）：确保代理在跑且健康 → 幂等虚拟登录 → 起沙箱 → 打开 UI。
fn one_click_login_inner(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
    lifecycle: &lifecycle::Lifecycle,
) -> Result<serde_json::Value, String> {
    // 0. 敏感 / 合规模式门（早退）：拒绝把 key 写进 MCP env 之前先拦。
    let cfg_pre = config::load_from(&config::default_dir()).map_err(|e| e.to_string())?;
    assert_sensitive_ok(&cfg_pre)?;

    // 1~3. 确保代理在跑且健康（内部已查生效 profile、key、探活）。带回本次是复用还是重启。
    let (pport, secret, proxy_action) = ensure_proxy(&app, &state, lifecycle)?;

    let dir = config::default_dir();
    let cfg = config::load_from(&dir).map_err(|e| e.to_string())?;
    let sport = cfg.sandbox_port;

    let sbx_home = sandbox_home();
    let auth_dir = sbx_home.join(".claude-science");

    // 沙箱已健康 → 但「daemon 活着」≠「登录态可用」：先只读校验虚拟登录是否自洽。
    if sandbox_running_ours(sport) {
        if oauth_forge::login_intact(&auth_dir, "virtual@localhost.invalid", &sbx_home) {
            let url = sandbox_url(sport);
            {
                let mut st = lock(&state);
                st.sandbox_port = sport;
                st.sandbox_url = Some(url.clone());
            }
            let base = match proxy_action {
                ProxyAction::Reused => "已在运行",
                ProxyAction::Restarted => "已用新配置重启代理，Science 沿用不变",
            };
            let msg = match open_in_browser(&url) {
                Ok(()) => format!("{base}，已重新打开 Science。"),
                Err(_) => format!("{base}，服务已就绪，请手动打开：{url}"),
            };
            return Ok(json!({ "url": url, "msg": msg, "action": "reopened" }));
        }
        {
            let mut st = lock(&state);
            let _ = stop_sandbox_inner(&app, &mut st);
        }
    }

    // 沙箱没起 / 挂了 / 登录失效已停 → 需要 launch 资源，此时才定位。确保虚拟登录（幂等）+ launch。
    let root = asset_root(&app)
        .ok_or("找不到 scripts/launch-virtual-sandbox.sh（打包资源或仓库根均未命中）。")?;

    let (forged, login_action) =
        oauth_forge::ensure_virtual_login(&auth_dir, "virtual@localhost.invalid", &sbx_home)
            .map_err(|e| format!("写虚拟登录失败：{e}"))?;

    // 3b. 把当前启用的 pack 装配进沙箱：Science 只在启动时读 mcp-servers.json，
    // 所以必须在 launch 之前落好文件。空启用集合也跑一次，把上次遗留的 bio-* 条目清掉。
    let pack_warnings: Vec<String> = {
        packs::apply(&root, &auth_dir, &cfg_pre.enabled_packs, &cfg_pre.pack_env)
            .map(|(_a, w)| w)
            .unwrap_or_else(|e| vec![format!("pack 装配失败：{e}")])
    };

    let launch = root.join("scripts/launch-virtual-sandbox.sh");
    if !launch.is_file() {
        return Err("找不到 scripts/launch-virtual-sandbox.sh。".into());
    }

    // 4. 起沙箱：脚本以 --detached 起 Science，然后返回。
    let proxy_url = format!("http://127.0.0.1:{pport}/{secret}");
    let logf = open_log("sandbox.log").map_err(|e| format!("建日志失败：{e}"))?;
    {
        use std::io::Write;
        let mut lw = &logf;
        let _ = writeln!(
            lw,
            "[oauth] 虚拟登录已就绪（Rust，零 node；action={:?}）：auth_dir={} account={} org={} enc={}",
            login_action,
            forged.auth_dir.display(),
            forged.account_uuid,
            forged.org_uuid,
            forged.enc_file.display()
        );
        for w in &pack_warnings {
            let _ = writeln!(lw, "[packs] warning: {}", w);
        }
    }
    let logf2 = logf.try_clone().map_err(|e| e.to_string())?;
    let status = Command::new("zsh")
        .arg(&launch)
        .arg("--port")
        .arg(sport.to_string())
        .arg("--proxy-url")
        .arg(&proxy_url)
        .arg("--skip-oauth-forge")
        .env("SANDBOX_HOME", sandbox_home())
        .stdout(Stdio::from(logf))
        .stderr(Stdio::from(logf2))
        .status()
        .map_err(|e| format!("起沙箱失败：{e}"))?;
    if !status.success() {
        let tail = redact(&tail_file(&log_path("sandbox.log"), 600), &secret);
        return Err(format!("起沙箱脚本失败。\n{tail}"));
    }

    // 5. 轮询沙箱 /health 直到就绪或超时（~8s）。
    let mut ok = false;
    for _ in 0..80 {
        std::thread::sleep(Duration::from_millis(100));
        if proc::http_health(sport, None, 400) {
            ok = true;
            break;
        }
    }
    if !ok {
        let tail = redact(&tail_file(&log_path("sandbox.log"), 600), &secret);
        {
            let mut st = lock(&state);
            let _ = stop_sandbox_inner(&app, &mut st);
        }
        return Err(format!(
            "沙箱起后探活超时（端口 {sport}）。已尝试停掉刚起的沙箱。\n{tail}"
        ));
    }

    // 5b. 身份确认：/health 200 只证明端口在服务，用 data-dir 强身份再确认一次。
    if !sandbox_running_ours(sport) {
        {
            let mut st = lock(&state);
            let _ = stop_sandbox_inner(&app, &mut st);
        }
        return Err(format!(
            "端口 {sport} 有服务响应，但按 data-dir 确认不是本沙箱 Science（疑似被其它服务占用）。已尝试停掉刚起的沙箱。"
        ));
    }

    // 6. 取 UI URL（登录态），交系统浏览器打开。
    let url = sandbox_url(sport);
    {
        let mut st = lock(&state);
        st.sandbox_port = sport;
        st.sandbox_url = Some(url.clone());
    }
    let started = match login_action {
        oauth_forge::LoginAction::Created => "已启动",
        _ => "沙箱已重新启动，沿用原有对话",
    };
    let msg = match open_in_browser(&url) {
        Ok(()) => format!("{started}。"),
        Err(_) => format!("{started}，服务已就绪，请手动打开：{url}"),
    };
    Ok(json!({ "url": url, "msg": msg, "action": "started" }))
}

/// 从 `claude-science url` 的 stdout 里取**第一条**合法 http(s) URL。
fn first_http_url(stdout: &str) -> Option<String> {
    for line in stdout.lines() {
        let t = line.trim();
        if t.starts_with("http://") || t.starts_with("https://") {
            let url = t.split_whitespace().next().unwrap_or(t);
            return Some(url.to_string());
        }
    }
    None
}

/// 取沙箱 UI 链接：`<bin> url --data-dir <home>/.claude-science`，HOME 指向沙箱 HOME。
fn sandbox_url(port: u16) -> String {
    let home = sandbox_home();
    let data_dir = home.join(".claude-science");
    if Path::new(SCIENCE_BIN).is_file() {
        if let Ok(out) = Command::new(SCIENCE_BIN)
            .arg("url")
            .arg("--data-dir")
            .arg(&data_dir)
            .env("HOME", &home)
            .output()
        {
            let s = String::from_utf8_lossy(&out.stdout);
            if let Some(url) = first_http_url(&s) {
                return url;
            }
        }
    }
    format!("http://127.0.0.1:{port}")
}

/// 判断「我们自己的」沙箱 Science 是否在跑（供一键健康分派）。优先用 Science 二进制按
/// 【我们的 data-dir】查 `{"running":true}`（强身份）；再叠加端口 /health 确认。
fn sandbox_running_ours(port: u16) -> bool {
    let home = sandbox_home();
    let data_dir = home.join(".claude-science");
    if Path::new(SCIENCE_BIN).is_file() {
        match Command::new(SCIENCE_BIN)
            .arg("status")
            .arg("--data-dir")
            .arg(&data_dir)
            .env("HOME", &home)
            .output()
        {
            Ok(out) => {
                let s = String::from_utf8_lossy(&out.stdout);
                let running = s.contains("\"running\":true") || s.contains("\"running\": true");
                return running && proc::http_health(port, None, 400);
            }
            Err(_) => return proc::http_health(port, None, 400),
        }
    }
    proc::http_health(port, None, 400)
}
